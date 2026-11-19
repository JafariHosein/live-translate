"""PipeWire-friendly microphone capture using sounddevice + NumPy.

The recorder keeps audio in memory and hands the rest of the pipeline a complete
16-bit PCM WAV file.  Audio is captured in PortAudio's real-time callback thread,
which is intentionally never blocked by GUI work.
"""

from __future__ import annotations

import io
import logging
import threading
import time
import wave
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QObject, pyqtSignal

from config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InputDevice:
    index: int
    name: str
    channels: int
    host_api: str


def list_input_devices() -> list[InputDevice]:
    """Return every device with at least one input channel.

    PortAudio may fail to initialise (for example when PipeWire is not running),
    in which case we return an empty list instead of crashing the UI.
    """
    try:
        devices = sd.query_devices()
        host_apis = sd.query_hostapis()
    except Exception as exc:  # PortAudioError and friends
        log.warning("Could not enumerate audio devices: %s", exc)
        return []

    result: list[InputDevice] = []
    for index, device in enumerate(devices):
        try:
            channels = int(device.get("max_input_channels", 0))
        except (TypeError, ValueError):
            continue
        if channels <= 0:
            continue
        host_api = ""
        try:
            host_api = str(host_apis[int(device.get("hostapi", -1))]["name"])
        except (IndexError, KeyError, TypeError, ValueError):
            pass
        result.append(
            InputDevice(
                index=index,
                name=str(device.get("name", f"Device {index}")).strip(),
                channels=channels,
                host_api=host_api,
            )
        )
    return result


def default_input_index() -> int | None:
    """Best-effort index of the system default input device."""
    try:
        info = sd.query_devices(kind="input")
        return int(info["index"])
    except Exception:
        return None


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw little-endian signed 16-bit PCM in an in-memory WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm)
    return buffer.getvalue()


def _friendly_audio_error(exc: BaseException, device_name: str) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.lower()
    if "busy" in lowered or "device unavailable" in lowered:
        return (
            f"Microphone “{device_name}” is busy. Close the app using it or "
            "pick another microphone."
        )
    if "invalid number of channels" in lowered:
        return (
            f"Microphone “{device_name}” does not support mono capture. "
            "Try another microphone."
        )
    if "invalid sample rate" in lowered:
        return (
            f"Microphone “{device_name}” rejected the sample rate. "
            "Try another microphone."
        )
    if "no default input device" in lowered:
        return (
            "No input device found. Check that PipeWire is running and that "
            "a microphone is connected."
        )
    return f"Could not open microphone “{device_name}”: {text}"


class AudioRecorder(QObject):
    """Push-to-talk audio recorder.

    Signals
    -------
    finished(wav_bytes, duration_seconds):
        A usable recording, already wrapped as WAV.
    too_short(duration_seconds):
        The key was released before ``min_recording_seconds``; nothing is sent.
    error(message):
        The microphone could not be opened or produced no audio.
    """

    finished = pyqtSignal(bytes, float)
    too_short = pyqtSignal(float)
    error = pyqtSignal(str)

    def __init__(self, settings: Settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._device: int | None = None
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None
        self._recording = False
        self._chunks: list[np.ndarray] = []
        self._sample_count = 0
        self._record_rate = settings.sample_rate
        self._record_channels = 1
        self._started_at = 0.0
        self._overflow_blocks = 0

    # ------------------------------------------------------------------ devices
    @property
    def device(self) -> int | None:
        return self._device

    def set_device(self, index: int | None) -> None:
        """Select an input device (``None`` means PortAudio's default)."""
        if index is not None and index < 0:
            index = None
        if self._recording:
            self.cancel()
        self._device = index

    def _device_name(self) -> str:
        try:
            info = sd.query_devices(self._device, "input")
            return str(info.get("name", "default input")).strip()
        except Exception:
            return "default input"

    def _candidate_formats(self) -> list[tuple[int, int]]:
        """Formats to try, most desirable first (16 kHz mono per the spec)."""
        preferred: list[tuple[int, int]] = [
            (self._settings.sample_rate, 1),
            (48_000, 1),
            (44_100, 1),
            (32_000, 1),
        ]
        try:
            info = sd.query_devices(self._device, "input")
            native_rate = int(round(float(info.get("default_samplerate", 48_000))))
            max_channels = int(info.get("max_input_channels", 1))
            preferred.extend(
                [(native_rate, 1), (native_rate, 2 if max_channels >= 2 else 1)]
            )
        except Exception:
            preferred.append((48_000, 2))

        seen: set[tuple[int, int]] = set()
        unique: list[tuple[int, int]] = []
        for rate, channels in preferred:
            if rate <= 0 or channels <= 0:
                continue
            key = (rate, channels)
            if key in seen:
                continue
            seen.add(key)
            unique.append(key)
        return unique

    def _open_stream(self) -> sd.InputStream | None:
        last_error: BaseException | None = None
        for rate, channels in self._candidate_formats():
            try:
                sd.check_input_settings(
                    device=self._device,
                    samplerate=rate,
                    channels=channels,
                    dtype="int16",
                )
                stream = sd.InputStream(
                    device=self._device,
                    samplerate=rate,
                    channels=channels,
                    dtype="int16",
                    latency=self._settings.input_latency,
                    blocksize=0,
                    callback=self._audio_callback,
                )
            except Exception as exc:  # PortAudioError / ValueError
                last_error = exc
                continue
            self._record_rate = rate
            self._record_channels = channels
            return stream

        message = _friendly_audio_error(
            last_error or RuntimeError("unknown audio error"), self._device_name()
        )
        log.error(message)
        self.error.emit(message)
        return None

    # ----------------------------------------------------------------- recording
    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> bool:
        """Start capturing.  Returns ``False`` if the microphone is unavailable."""
        if self._recording:
            return False

        with self._lock:
            self._chunks = []
            self._sample_count = 0
            self._overflow_blocks = 0

        stream = self._open_stream()
        if stream is None:
            return False

        self._stream = stream
        with self._lock:
            # Set before start() so the first PortAudio callback is accepted.
            self._recording = True
        self._started_at = time.monotonic()

        try:
            stream.start()
        except Exception as exc:
            with self._lock:
                self._recording = False
            self._close_stream()
            message = _friendly_audio_error(exc, self._device_name())
            log.error(message)
            self.error.emit(message)
            return False
        return True

    def stop(self) -> None:
        """Stop capturing and emit the recording if it passed the minimum length."""
        if not self._recording:
            return

        with self._lock:
            self._recording = False

        self._close_stream()

        with self._lock:
            chunks = self._chunks
            sample_count = self._sample_count
            rate = self._record_rate
            channels = self._record_channels
            overflows = self._overflow_blocks
            self._chunks = []
            self._sample_count = 0

        duration = sample_count / float(rate) if rate else 0.0
        if duration < self._settings.min_recording_seconds:
            self.too_short.emit(duration)
            return
        if not chunks or sample_count == 0:
            self.error.emit(
                "No audio was captured. Check your microphone and PipeWire routing."
            )
            return
        if overflows:
            log.warning("Audio input reported %d overflow block(s)", overflows)

        pcm = np.concatenate(chunks).tobytes()
        wav_bytes = pcm_to_wav(pcm, rate, channels)
        log.info(
            "Captured %.2fs of audio (%d Hz, %d ch, %.1f KiB)",
            duration,
            rate,
            channels,
            len(wav_bytes) / 1024,
        )
        self.finished.emit(wav_bytes, duration)

    def cancel(self) -> None:
        """Abort the current recording without emitting audio."""
        with self._lock:
            was_recording = self._recording
            self._recording = False
            self._chunks = []
            self._sample_count = 0
        self._close_stream()
        if was_recording:
            log.debug("Recording cancelled")

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop(ignore_errors=True)
        except Exception:
            pass
        try:
            stream.close(ignore_errors=True)
        except Exception:
            pass

    def shutdown(self) -> None:
        self.cancel()

    # ------------------------------------------------------------------ callback
    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,  # noqa: ARG002 - PortAudio signature
        time_info: object,  # noqa: ARG002
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            with self._lock:
                self._overflow_blocks += 1

        samples = np.asarray(indata)
        if samples.ndim == 2 and samples.shape[1] > 1:
            mono = samples.astype(np.float32).mean(axis=1)
            pcm = np.clip(mono, -32768.0, 32767.0).astype(np.int16)
        else:
            pcm = samples.reshape(-1).astype(np.int16, copy=True)

        with self._lock:
            if not self._recording:
                return
            self._chunks.append(pcm)
            self._sample_count += int(pcm.size)
