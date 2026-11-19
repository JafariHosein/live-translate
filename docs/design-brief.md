# Original design brief

> This is the brief the project was built from. It is included for context; the
> [README](../README.md) is the source of truth for the current behavior.

Act as a Senior Linux Systems & Python Desktop Developer. Build a complete, production-ready, real-time English-to-Persian voice translation floating overlay application specifically engineered for **Arch Linux running Wayland**.

### 1. Core Functionality & User Flow
- **Push-to-Talk (PTT) Voice Capture:** The user holds down a configurable global shortcut key (default: `Right Alt` / `KEY_RIGHTALT`, easily changeable in config) OR holds a dedicated button in the UI.
- **Audio Recording:** While the key is held, record microphone audio (supports both single English words and full, multi-second English sentences). Releasing the key stops recording and immediately sends the audio for online processing.
- **Online Speech-to-Text (STT) & Translation:**
  - Transcribe the spoken English audio to text.
  - Translate the English text into natural, fluent Persian (Farsi).
  - **Modular Provider Architecture (`config.py`):**
    1. **Default Mode (No API Key required):** Use `SpeechRecognition` (Google Web Speech API, passing in-memory WAV bytes recorded via `sounddevice`) + `deep-translator` (`GoogleTranslator(source='en', target='fa')`).
    2. **Optional High-Accuracy API Mode:** Include a clean provider option for **Groq API** (`whisper-large-v3-turbo` for ultra-fast STT + `llama-3.3-70b-versatile` for natural sentence translation) controlled via a simple config toggle and `.env` API key.
- **Display Inside the App:** Show both the recognized English sentence (LTR) and its Persian translation (RTL) inside the floating window, along with a scrollable in-app history of recent translations (with one-click copy buttons and a clear history button).

### 2. Tech Stack & Arch/Wayland Specific Engineering
- **Language & GUI:** Python 3 + **PyQt6** (running natively on Wayland via `qt6-wayland`).
- **Audio Backend:** `sounddevice` + `numpy` (16kHz, mono, 16-bit PCM) for clean, non-blocking compatibility with **PipeWire** on Arch Linux. Ignore accidental taps shorter than 0.3 seconds.
- **Global Push-to-Talk on Wayland:**
  - Do NOT rely on `pynput` or `keyboard` (they fail on Wayland when unfocused).
  - Implement a background global key listener using Python **`evdev`** that scans `/dev/input/event*` for keyboard devices and detects exact key-down (`value == 1`) and key-up (`value == 0`) events for the configured PTT key.
  - Gracefully handle permission errors: if `/dev/input` is not accessible, show a friendly non-crashing warning inside the app explaining how to add the user to the `input` group (`sudo usermod -aG input $USER`), while keeping the in-app hold-to-talk button fully functional.
- **Floating Overlay Window:**
  - Frameless, compact, translucent dark glassmorphic UI (`WA_TranslucentBackground`, rounded corners, subtle border).
  - Draggable from anywhere on the title/header bar.
  - Set `Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool`.
  - Include a compact header with: live status indicator (Idle / Listening with pulsing visual feedback / Translating / Error), opacity slider or minimize/close buttons, and a quick mic selector dropdown if multiple mics exist.
- **Persian (RTL) Typography:**
  - Properly enforce Right-to-Left layout (`Qt.LayoutDirection.RightToLeft`) and right alignment for Persian text, and Left-to-Right for English text.
  - Use a clean font fallback stack (e.g., `Vazirmatn`, `Vazir`, `DejaVu Sans`, `Sans-Serif`).
- **Strict Non-Blocking Concurrency:**
  - All audio recording, `evdev` listening, STT requests, and translation HTTP requests MUST run in worker threads (`QThread` / `QThreadPool`) communicating with the GUI strictly via Qt Signals/Slots so the UI never freezes.

### 3. Project Structure
Organize the codebase cleanly into modular files:
- `config.py` — Settings (PTT key, provider selection, sample rate, window opacity, API keys via env).
- `audio_recorder.py` — PipeWire-friendly `sounddevice` recorder returning in-memory WAV bytes.
- `hotkey_listener.py` — Wayland-native `evdev` PTT listener emitting press/release signals.
- `providers.py` — Abstract base classes + implementations for `FreeGoogleProvider` and `GroqProvider`, with proper network timeouts and error handling.
- `ui.py` — PyQt6 floating overlay window, custom dark stylesheet, status indicator, history panel, and RTL/LTR formatting.
- `main.py` — Application entry point wiring all modules cleanly with safe shutdown handling (`SIGINT` / thread cleanup).
- `requirements.txt` — Exact Python dependencies.

### 4. Deliverables Required
1. Complete, fully implemented code for every file (no placeholders, no `TODO`s, no omitted methods).
2. Step-by-step **Arch Linux setup instructions**:
   - Required `pacman` packages (e.g., `portaudio`, `qt6-wayland`, `python`, etc.).
   - Setting up a Python `venv` (respecting Arch's PEP 668 restriction).
   - Adding the user to the `input` group for `evdev` Wayland global hotkeys.
   - Exact **Wayland Window Rule snippets** (for KDE Plasma Wayland, Hyprland, and GNOME) to ensure the overlay window stays pinned always-on-top across all workspaces.
