"""Central configuration for Live Translate.

Every tunable is exposed through an environment variable so the application can
be configured without editing code.  Values are loaded (in order) from the real
environment, the per-user config file and the project `.env` file.

Paths
-----
* User config + secrets: ``~/.config/live-translate/.env``
* Project/dev secrets:     ``<project>/.env``
* Optional overrides:      plain environment variables

The defaults are tuned for Arch Linux + PipeWire and require no API key.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

APP_ID = "live-translate"
APP_NAME = "Live Translate"
APP_VERSION = "1.0.0"


def _application_dir() -> Path:
    """Directory that contains the app, also for a PyInstaller build."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _assets_dir(application_dir: Path) -> Path:
    """Bundled read-only assets (PyInstaller extracts them to ``_MEIPASS``)."""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return Path(bundle_dir) / "assets"
    return application_dir / "assets"


BASE_DIR = _application_dir()
ASSETS_DIR = _assets_dir(BASE_DIR)


def user_config_dir() -> Path:
    """Return the XDG config directory for this application."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return root / APP_ID


def env_file_candidates() -> tuple[Path, ...]:
    """Environment files, in load order (the first value wins)."""
    candidates: list[Path] = [user_config_dir() / ".env"]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # For a PyInstaller build: next to the binary, and one level up so a
        # ``dist/live-translate`` build still finds the project's ``.env``.
        candidates.extend((exe_dir / ".env", exe_dir.parent / ".env"))
    else:
        candidates.append(BASE_DIR / ".env")
    return tuple(dict.fromkeys(candidates))


def load_environment() -> None:
    """Load .env files without overriding variables already in the environment."""
    for candidate in env_file_candidates():
        try:
            if candidate.is_file():
                load_dotenv(candidate, override=False)
        except OSError as exc:  # pragma: no cover - unreadable .env is non-fatal
            log.warning("Could not read %s: %s", candidate, exc)


load_environment()


def _env_text(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        log.warning("Ignoring invalid %s=%r (expected an integer)", name, raw)
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        log.warning("Ignoring invalid %s=%r (expected a number)", name, raw)
        return default
    return max(minimum, min(maximum, value))


def _normalise_provider(raw: str) -> str:
    value = raw.strip().lower().replace("_", "-")
    if value in {"groq", "api", "groq-api"}:
        return "groq"
    if value in {"free", "google", "google-free", "free-google", "default"}:
        return "free"
    log.warning("Unknown LIVE_TRANSLATE_PROVIDER=%r, falling back to 'free'", raw)
    return "free"


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable application settings, validated once at startup."""

    # Provider: "free" (Google Web Speech + Google Translate, no key) or "groq".
    provider: str = "free"

    # Push-to-talk.
    ptt_key: str = "KEY_RIGHTALT"
    ptt_rescan_seconds: float = 5.0

    # Audio capture.  16 kHz mono PCM s16le is the native format for the speech
    # services; the recorder falls back to the device's native format if needed.
    sample_rate: int = 16_000
    channels: int = 1
    min_recording_seconds: float = 0.3
    input_latency: str = "high"

    # Network.
    stt_timeout: float = 30.0
    translate_timeout: float = 20.0

    # UI.
    window_opacity: float = 0.95
    window_width: int = 520
    window_height: int = 680
    history_limit: int = 40

    # Languages.
    source_language: str = "en-US"
    target_language: str = "fa"

    # Groq (optional).
    groq_api_key: str = ""
    groq_stt_model: str = "whisper-large-v3-turbo"
    groq_translation_model: str = "llama-3.3-70b-versatile"

    @classmethod
    def from_env(cls) -> "Settings":
        provider = _normalise_provider(
            _env_text("LIVE_TRANSLATE_PROVIDER", "free")
        )
        ptt_key = _env_text("LIVE_TRANSLATE_PTT_KEY", "KEY_RIGHTALT").upper()
        if not ptt_key.startswith("KEY_") and not ptt_key.isdigit():
            ptt_key = f"KEY_{ptt_key}"

        return cls(
            provider=provider,
            ptt_key=ptt_key,
            ptt_rescan_seconds=_env_float(
                "LIVE_TRANSLATE_RESCAN_SECONDS", 5.0, 1.0, 60.0
            ),
            sample_rate=_env_int(
                "LIVE_TRANSLATE_SAMPLE_RATE", 16_000, 8_000, 192_000
            ),
            channels=_env_int("LIVE_TRANSLATE_CHANNELS", 1, 1, 2),
            min_recording_seconds=_env_float(
                "LIVE_TRANSLATE_MIN_SECONDS", 0.3, 0.1, 5.0
            ),
            input_latency=_env_text("LIVE_TRANSLATE_INPUT_LATENCY", "high").lower(),
            stt_timeout=_env_float("LIVE_TRANSLATE_STT_TIMEOUT", 30.0, 5.0, 120.0),
            translate_timeout=_env_float(
                "LIVE_TRANSLATE_TRANSLATE_TIMEOUT", 20.0, 3.0, 120.0
            ),
            window_opacity=_env_float(
                "LIVE_TRANSLATE_OPACITY", 0.95, 0.35, 1.0
            ),
            window_width=_env_int("LIVE_TRANSLATE_WIDTH", 520, 400, 1400),
            window_height=_env_int("LIVE_TRANSLATE_HEIGHT", 680, 420, 1400),
            history_limit=_env_int("LIVE_TRANSLATE_HISTORY_LIMIT", 40, 1, 200),
            source_language=_env_text("LIVE_TRANSLATE_SOURCE_LANGUAGE", "en-US"),
            target_language=_env_text("LIVE_TRANSLATE_TARGET_LANGUAGE", "fa"),
            groq_api_key=_env_text("GROQ_API_KEY", ""),
            groq_stt_model=_env_text(
                "GROQ_STT_MODEL", "whisper-large-v3-turbo"
            ),
            groq_translation_model=_env_text(
                "GROQ_TRANSLATION_MODEL", "llama-3.3-70b-versatile"
            ),
        )

    @property
    def is_groq(self) -> bool:
        return self.provider == "groq"

    @property
    def provider_label(self) -> str:
        if self.is_groq:
            return "Groq · Whisper + Llama 3.3"
        return "Google · Free mode"

    @property
    def has_groq_key(self) -> bool:
        return bool(self.groq_api_key.strip())


SETTINGS = Settings.from_env()
