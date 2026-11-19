# Changelog

All notable changes to Live Translate are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-11-19

First public release.

### Added

- Frameless, translucent, always-on-top PyQt6 overlay with an RTL/LTR translation
  card and drag/resize support.
- Global push-to-talk via `evdev` (`Right Alt` by default) plus an in-app hold
  button; graceful permission guidance when `/dev/input` is not readable.
- 16 kHz mono recording with `sounddevice`/PipeWire, short-tap rejection and
  in-memory WAV encoding.
- Free provider mode using Google Web Speech through `SpeechRecognition` and
  Google Translate through `deep-translator`, with an automatic direct-endpoint
  fallback when the HTML endpoint is rate-limited.
- Optional Groq mode (`whisper-large-v3-turbo` + `llama-3.3-70b-versatile`) with
  timeouts and clear error reporting.
- Manual text input and translation with the same result card and history as
  voice input.
- Slim one-line **Mini** mode that temporarily expands to fit long translations.
- Scrollable history with per-item copy buttons, clear history, opacity control,
  microphone selector, system tray, and remembered window state.
- Bundled Vazirmatn fonts, SVG icon, desktop entry, KWin keep-above script, and
  GNOME Shell helper extension.
- Standalone PyInstaller build with `--self-test` and `--version`.
- Arch Linux/Wayland documentation for KDE Plasma, Hyprland, and GNOME.
