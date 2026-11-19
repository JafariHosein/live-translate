#!/usr/bin/env python3
"""Live Translate entry point.

Wires the recorder, global PTT listener, provider pipeline and overlay together.
All blocking work happens on worker threads; the GUI thread only handles signals.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import sys
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PyQt6.QtWidgets import QApplication, QMessageBox

from audio_recorder import AudioRecorder, list_input_devices
from config import APP_ID, APP_NAME, APP_VERSION, ASSETS_DIR, SETTINGS, Settings
from hotkey_listener import HotkeyListener, key_display_name
from providers import ProviderError, TranslationProvider, build_provider
from ui import OverlayWindow, make_app_icon

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- logging
def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / APP_ID


def configure_logging() -> None:
    level_name = os.environ.get("LIVE_TRANSLATE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                directory / "live-translate.log",
                maxBytes=1_000_000,
                backupCount=2,
                encoding="utf-8",
            )
        )
    except OSError:
        pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)-22s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    for noisy in ("httpx", "httpcore", "urllib3", "groq._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# -------------------------------------------------------------------------- styling
def _apply_palette(app: QApplication) -> None:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#101018"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e2e8f0"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#181824"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#20202f"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e2e8f0"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#1b1b28"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e2e8f0"))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#8b5cf6"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#1b1b29"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#e2e8f0"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#64748b"))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#64748b")
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor("#64748b"),
    )
    app.setPalette(palette)


def _load_fonts(app: QApplication) -> None:
    """Load bundled Vazirmatn fonts and pick a sane fallback stack."""
    font_dir = ASSETS_DIR / "fonts"
    for path in sorted(font_dir.glob("*.ttf")):
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            log.warning("Could not load bundled font %s", path)

    available = set(QFontDatabase.families())
    stack = [
        family
        for family in ("Vazirmatn", "Vazir", "Noto Sans Arabic", "DejaVu Sans")
        if family in available
    ]
    if not stack:
        stack = [app.font().family()]
    app_font = QFont()
    app_font.setFamilies(stack)
    app_font.setPointSizeF(10.0)
    app.setFont(app_font)
    log.info("UI font stack: %s", " → ".join(stack))


# ---------------------------------------------------------------------------- worker
class TranslateWorker(QThread):
    """Runs STT + translation for one recording, then exits."""

    succeeded = pyqtSignal(str, str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        provider: TranslationProvider,
        wav_bytes: bytes,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider
        self._wav_bytes = wav_bytes
        self.setObjectName("TranslateWorker")

    def run(self) -> None:
        try:
            english, persian = self._provider.process(self._wav_bytes)
        except ProviderError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never crash the worker silently
            log.exception("Unexpected provider failure")
            self.failed.emit(
                f"Unexpected error while translating: {exc.__class__.__name__}: {exc}"
            )
            return
        self.succeeded.emit(english, persian)


class TextTranslateWorker(QThread):
    """Translate one manually typed English string, then exits."""

    succeeded = pyqtSignal(str, str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        provider: TranslationProvider,
        text: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider
        self._text = text
        self.setObjectName("TextTranslateWorker")

    def run(self) -> None:
        try:
            persian = self._provider.translate(self._text)
        except ProviderError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never crash the worker silently
            log.exception("Unexpected manual translation failure")
            self.failed.emit(
                f"Unexpected error while translating: {exc.__class__.__name__}: {exc}"
            )
            return
        self.succeeded.emit(self._text, persian)


# ------------------------------------------------------------------------ controller
class LiveTranslateController(QObject):
    """Owns the app's objects, state machine and thread cleanup."""

    def __init__(
        self,
        app: QApplication,
        settings: Settings,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._settings = settings
        self._active_sources: set[str] = set()
        self._pending: deque[tuple[str, bytes | str]] = deque()
        self._worker: TranslateWorker | TextTranslateWorker | None = None
        self._closing = False

        provider, notice = build_provider(settings)
        self._provider = provider
        log.info("Provider: %s", provider.label)

        self.ui = OverlayWindow(settings, key_display_name(settings.ptt_key))
        self.ui.set_provider_label(provider.label)

        self._recorder = AudioRecorder(settings, self)
        self._hotkey = HotkeyListener(settings, self)

        self._wire_signals()

        devices = list_input_devices()
        self.ui.set_microphones(devices)
        selected = self.ui.selected_microphone()
        self._recorder.set_device(None if selected < 0 else selected)
        log.info(
            "Found %d input device(s); selected: %s",
            len(devices),
            "default" if selected < 0 else selected,
        )
        if not devices:
            self.ui.show_notice(
                "No microphone was found. Check that PipeWire/PulseAudio is "
                "running and a microphone is connected.",
                "warning",
            )
        if notice:
            self.ui.show_notice(notice, "warning")

    # ----------------------------------------------------------------- wiring
    def _wire_signals(self) -> None:
        self.ui.ptt_pressed.connect(lambda: self._on_ptt_pressed("ui"))
        self.ui.ptt_released.connect(lambda: self._on_ptt_released("ui"))
        self._hotkey.pressed.connect(lambda: self._on_ptt_pressed("key"))
        self._hotkey.released.connect(lambda: self._on_ptt_released("key"))

        self._hotkey.error.connect(self._on_hotkey_error)
        self._recorder.finished.connect(self._on_audio_ready)
        self._recorder.too_short.connect(self._on_too_short)
        self._recorder.error.connect(self._on_recorder_error)
        self.ui.mic_changed.connect(self._on_mic_changed)
        self.ui.manual_submitted.connect(self._on_manual_text)
        self.ui.quit_requested.connect(self._app.quit)

    def start(self) -> None:
        log.info("Live Translate %s starting (provider=%s)", APP_VERSION, self._provider.label)
        self.ui.show()
        self.ui.raise_()
        self._hotkey.start()

    # ------------------------------------------------------------ push to talk
    def _on_ptt_pressed(self, source: str) -> None:
        if self._closing:
            return
        if source in self._active_sources:
            return
        self._active_sources.add(source)
        if len(self._active_sources) > 1:
            return  # another PTT source is already recording this utterance
        if self._recorder.is_recording:
            return

        if not self.ui.isVisible():
            self.ui.show()
            self.ui.raise_()

        if not self._recorder.start():
            self._active_sources.clear()
            self.ui.set_microphone_enabled(True)
            return

        self.ui.set_microphone_enabled(False)
        self.ui.set_state("listening")

    def _on_ptt_released(self, source: str) -> None:
        if source not in self._active_sources:
            return
        self._active_sources.discard(source)
        if self._active_sources:
            return
        self.ui.set_microphone_enabled(True)
        if self._recorder.is_recording:
            self._recorder.stop()
        else:
            self._refresh_status()

    # -------------------------------------------------------------- audio events
    def _on_audio_ready(self, wav_bytes: bytes, duration: float) -> None:
        if self._closing:
            return
        log.info("Audio ready (%.2fs); queueing translation", duration)
        self._pending.append(("audio", wav_bytes))
        if self._worker is None:
            self._start_next_job()
        else:
            self._refresh_status()

    def _on_manual_text(self, text: str) -> None:
        if self._closing:
            return
        text = text.strip()
        if not text:
            return
        log.info("Manual translation requested (%d characters)", len(text))
        self._pending.append(("text", text))
        if self._worker is None:
            self._start_next_job()
        else:
            self._refresh_status()

    def _on_too_short(self, duration: float) -> None:
        minimum = self._settings.min_recording_seconds
        log.info("Ignoring tap of %.2fs (minimum %.1fs)", duration, minimum)
        # Do not replace an important permission/provider warning with a
        # transient hint about an accidental tap.
        if self.ui.notice_kind not in {"warning", "error"}:
            self.ui.show_notice(
                f"Too short ({duration:.2f}s). Hold the key for at least "
                f"{minimum:.1f} seconds.",
                "info",
                timeout_ms=2600,
            )
        self._refresh_status()

    def _on_recorder_error(self, message: str) -> None:
        log.error("Recorder error: %s", message)
        self._active_sources.clear()
        self.ui.set_microphone_enabled(True)
        self.ui.set_state("error", message)

    def _on_mic_changed(self, index: int) -> None:
        self._recorder.set_device(None if index < 0 else index)
        log.info("Microphone changed to %s", "default" if index < 0 else index)

    def _on_hotkey_error(self, message: str) -> None:
        log.warning("Hotkey warning: %s", message.replace("\n", " "))
        if self._recorder.is_recording:
            self.ui.show_notice(message, "warning")
        else:
            self.ui.set_state("warning", message)

    # ------------------------------------------------------------- job pipeline
    def _start_next_job(self) -> None:
        if self._closing or not self._pending:
            self._refresh_status()
            return
        kind, payload = self._pending.popleft()
        if kind == "text":
            worker: TranslateWorker | TextTranslateWorker = TextTranslateWorker(
                self._provider, str(payload), self
            )
        else:
            worker = TranslateWorker(self._provider, bytes(payload), self)
        worker.succeeded.connect(self._on_translation)
        worker.failed.connect(self._on_translation_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        self._refresh_status()
        worker.start()

    def _on_translation(self, english: str, persian: str) -> None:
        log.info("Translated %d characters of English", len(english))
        self.ui.set_result(english, persian)

    def _on_translation_failed(self, message: str) -> None:
        log.error("Translation failed: %s", message)
        self.ui.set_state("error", message)

    def _on_worker_finished(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        if self._closing:
            return
        if self._pending:
            self._start_next_job()
        else:
            self._refresh_status()

    def _refresh_status(self) -> None:
        if self._recorder.is_recording:
            self.ui.set_state("listening")
        elif self._worker is not None:
            self.ui.set_state("translating", queued=len(self._pending))
        else:
            self.ui.set_state("idle")

    # ---------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        log.info("Shutting down")
        self._active_sources.clear()
        try:
            self._hotkey.stop()
        except Exception:
            log.exception("Error stopping hotkey listener")
        try:
            self._recorder.shutdown()
        except Exception:
            log.exception("Error stopping recorder")

        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.requestInterruption()
            if not worker.wait(1_800):
                log.warning("Translation worker still running; terminating it")
                worker.terminate()
                worker.wait(700)
        try:
            self._provider.close()
        except Exception:
            log.exception("Error closing provider")
        self.ui.clear_notice()


# ---------------------------------------------------------------------------- main
def _install_signal_handlers(app: QApplication) -> QTimer:
    def handle(signum, frame) -> None:  # noqa: ARG001
        log.info("Received signal %s; quitting", signum)
        QTimer.singleShot(0, app.quit)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(signum, handle)
        except (ValueError, OSError):
            pass

    # A short heartbeat lets Python run signal handlers while Qt owns the loop.
    heartbeat = QTimer(app)
    heartbeat.setInterval(250)
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start()
    return heartbeat


def create_application() -> QApplication:
    os.environ.setdefault("PULSE_PROP_application.name", APP_NAME)
    os.environ.setdefault("PULSE_PROP_application.id", APP_ID)
    os.environ.setdefault("PULSE_PROP_media.role", "communication")
    os.environ.setdefault("PIPEWIRE_PROPS", "application.name=Live Translate")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("LiveTranslate")
    app.setOrganizationDomain("live-translate.local")
    QGuiApplication.setDesktopFileName(APP_ID)
    app.setWindowIcon(make_app_icon())
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(True)
    _apply_palette(app)
    _load_fonts(app)
    return app


def run_self_test() -> int:
    """Headless diagnostics for a source checkout or a frozen binary."""
    configure_logging()
    print(f"{APP_NAME} {APP_VERSION}")
    print(f"frozen: {getattr(sys, 'frozen', False)}")
    print(f"assets: {ASSETS_DIR} ({'ok' if ASSETS_DIR.is_dir() else 'missing'})")
    print(f"flac: {shutil.which('flac') or 'MISSING (Google STT needs it)'}")

    failures: list[str] = []
    for module in (
        "PyQt6.QtWidgets",
        "sounddevice",
        "evdev",
        "speech_recognition",
        "deep_translator",
        "requests",
        "groq",
        "dotenv",
    ):
        try:
            __import__(module)
            print(f"import {module}: ok")
        except Exception as exc:  # noqa: BLE001 - report every import failure
            print(f"import {module}: FAILED ({exc})")
            failures.append(module)

    provider, notice = build_provider(SETTINGS)
    print(f"provider: {provider.label}")
    if notice:
        print(f"notice: {notice}")
    try:
        translated = provider.translate("Hello, how are you today?")
        print(f"translation: {translated}")
    except ProviderError as exc:
        print(f"translation: FAILED ({exc})")
        failures.append("translation")
    finally:
        provider.close()

    if failures:
        print(f"SELF-TEST FAILED: {', '.join(failures)}")
        return 1
    print("SELF-TEST OK")
    return 0


def main() -> int:
    if "--version" in sys.argv[1:]:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    if "--self-test" in sys.argv[1:]:
        return run_self_test()

    configure_logging()
    log.info("Starting %s %s", APP_NAME, APP_VERSION)
    log.debug("Platform plugin: %s", os.environ.get("QT_QPA_PLATFORM", "auto"))

    settings = SETTINGS
    app = create_application()

    try:
        controller = LiveTranslateController(app, settings)
    except ProviderError as exc:
        QMessageBox.critical(None, APP_NAME, str(exc))
        return 1

    _install_signal_handlers(app)
    app.aboutToQuit.connect(controller.shutdown)
    controller.start()

    exit_code = app.exec()
    controller.shutdown()
    log.info("Goodbye")
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
