"""Wayland-native global push-to-talk listener built on Linux evdev.

Wayland deliberately blocks global key grabs, so toolkits such as pynput or
keyboard cannot see keys while another window is focused.  Reading the kernel's
``/dev/input/event*`` devices works because it sits below the display server.

The user must be able to read those devices (normally via the ``input`` group):
``sudo usermod -aG input $USER`` followed by a fresh login.
"""

from __future__ import annotations

import glob
import logging
import select
import threading
import time
from typing import Iterable

from evdev import InputDevice, ecodes
from PyQt6.QtCore import QThread, pyqtSignal

from config import Settings

log = logging.getLogger(__name__)

_KEY_ALIASES = {
    "RIGHT_ALT": "KEY_RIGHTALT",
    "RIGHTALT": "KEY_RIGHTALT",
    "ALT_R": "KEY_RIGHTALT",
    "RALT": "KEY_RIGHTALT",
    "LEFT_ALT": "KEY_LEFTALT",
    "LEFTALT": "KEY_LEFTALT",
    "ALT_L": "KEY_LEFTALT",
    "LALT": "KEY_LEFTALT",
    "RIGHT_CTRL": "KEY_RIGHTCTRL",
    "RIGHTCTRL": "KEY_RIGHTCTRL",
    "CTRL_R": "KEY_RIGHTCTRL",
    "LEFT_CTRL": "KEY_LEFTCTRL",
    "LEFTCTRL": "KEY_LEFTCTRL",
    "CTRL_L": "KEY_LEFTCTRL",
    "RIGHT_SHIFT": "KEY_RIGHTSHIFT",
    "RIGHTSHIFT": "KEY_RIGHTSHIFT",
    "SHIFT_R": "KEY_RIGHTSHIFT",
    "LEFT_SHIFT": "KEY_LEFTSHIFT",
    "LEFTSHIFT": "KEY_LEFTSHIFT",
    "SHIFT_L": "KEY_LEFTSHIFT",
    "SUPER": "KEY_LEFTMETA",
    "META": "KEY_LEFTMETA",
    "WIN": "KEY_LEFTMETA",
    "WINDOWS": "KEY_LEFTMETA",
    "RIGHT_SUPER": "KEY_RIGHTMETA",
    "CAPS": "KEY_CAPSLOCK",
}

_DISPLAY_NAMES = {
    "KEY_RIGHTALT": "Right Alt",
    "KEY_LEFTALT": "Left Alt",
    "KEY_RIGHTCTRL": "Right Ctrl",
    "KEY_LEFTCTRL": "Left Ctrl",
    "KEY_RIGHTSHIFT": "Right Shift",
    "KEY_LEFTSHIFT": "Left Shift",
    "KEY_LEFTMETA": "Super",
    "KEY_RIGHTMETA": "Right Super",
    "KEY_CAPSLOCK": "Caps Lock",
    "KEY_SCROLLLOCK": "Scroll Lock",
    "KEY_PAUSE": "Pause",
    "KEY_SPACE": "Space",
    "KEY_INSERT": "Insert",
    "KEY_DELETE": "Delete",
    "KEY_HOME": "Home",
    "KEY_END": "End",
    "KEY_PAGEUP": "Page Up",
    "KEY_PAGEDOWN": "Page Down",
}


def normalise_key_name(name: str) -> str:
    """Turn user input into a canonical evdev key name (``KEY_*``)."""
    raw = name.strip()
    if not raw:
        raise ValueError("The push-to-talk key is empty.")

    # Allow a numeric evdev code for unusual keyboards.
    if raw.isdigit():
        return raw

    token = raw.upper().replace(" ", "_").replace("-", "_")
    if token.startswith("KEY_"):
        token = token[4:]
    token = _KEY_ALIASES.get(token, token)
    if not token.startswith("KEY_"):
        token = f"KEY_{token}"
    return token


def resolve_key_code(name: str) -> int:
    """Resolve a key name to an evdev key code, with a helpful error."""
    token = normalise_key_name(name)
    if token.isdigit():
        code = int(token)
        if code < 0:
            raise ValueError(f"Invalid key code: {name!r}")
        return code
    code = ecodes.ecodes.get(token)
    if code is None:
        examples = "KEY_RIGHTALT, KEY_F8, KEY_PAUSE"
        raise ValueError(
            f"Unknown push-to-talk key “{name}”. Use an evdev name such as "
            f"{examples}."
        )
    return int(code)


def key_display_name(name: str) -> str:
    """Human-friendly label used in the UI."""
    try:
        token = normalise_key_name(name)
    except ValueError:
        return name
    if token in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[token]
    pretty = token.removeprefix("KEY_").replace("_", " ").title()
    return pretty or name


def _input_group_pending() -> bool:
    """True when /etc/group says we belong to ``input`` but this login does not.

    Group changes only apply to newly started login sessions.  Detecting this
    lets the UI explain that the fix is a re-login rather than another
    ``usermod``.
    """
    try:
        import grp
        import os
        import pwd

        account = pwd.getpwuid(os.getuid())
        group = grp.getgrnam("input")
    except (KeyError, OSError):
        return False
    if group.gr_gid in os.getgroups() or group.gr_gid == account.pw_gid:
        return False
    return account.pw_name in group.gr_mem


def _permission_message(paths: Iterable[str]) -> str:
    sample = next(iter(paths), "/dev/input/event*")
    if _input_group_pending():
        return (
            f"Global push-to-talk cannot read {sample} yet: this login session "
            "started before your account joined the 'input' group.\n"
            "Your account is already a member. Log out and back in (or reboot) "
            "to activate it.\n"
            "Immediate workaround:  ./run.sh   (it activates the group for that "
            "launch).\n"
            "The Hold-to-Talk button still works."
        )
    return (
        f"Global push-to-talk cannot read {sample} (permission denied).\n"
        "Fix:  sudo usermod -aG input $USER   then log out and back in.\n"
        "The Hold-to-Talk button still works."
    )


class HotkeyListener(QThread):
    """Background thread that watches keyboards for the configured PTT key.

    Only transitions are emitted: evdev sends ``value == 1`` on key-down,
    ``value == 0`` on key-up and ``value == 2`` for auto-repeat.  Auto-repeat is
    ignored so the UI never starts a second recording while the key is held.
    """

    pressed = pyqtSignal()
    released = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._stop_event = threading.Event()
        self._key_code: int | None = None
        self._devices: dict[str, InputDevice] = {}
        self._is_down = False
        self._last_problem: str | None = None
        self._access_problem: str | None = None
        self.setObjectName("HotkeyListener")

    # ------------------------------------------------------------------ control
    def stop(self, timeout_ms: int = 1_500) -> None:
        """Ask the thread to finish and wait briefly for it."""
        self._stop_event.set()
        if self.isRunning():
            self.wait(timeout_ms)

    # --------------------------------------------------------------------- loop
    def run(self) -> None:  # noqa: C901 - a small, explicit event loop is clearer
        try:
            self._key_code = resolve_key_code(self._settings.ptt_key)
        except ValueError as exc:
            log.error("%s", exc)
            self.error.emit(str(exc))
            return

        display = key_display_name(self._settings.ptt_key)
        log.info("Global PTT listener started for %s (code %s)", display, self._key_code)

        rescan_every = max(1.0, self._settings.ptt_rescan_seconds)
        last_scan = 0.0
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now - last_scan >= rescan_every:
                    self._scan_devices()
                    last_scan = now
                    if not self._devices:
                        problem = self._access_problem or (
                            f"No keyboard exposing “{display}” was found in "
                            "/dev/input. The Hold-to-Talk button still works."
                        )
                        if problem != self._last_problem:
                            log.warning("%s", problem.replace("\n", " "))
                            self.error.emit(problem)
                            self._last_problem = problem
                        self._stop_event.wait(0.5)
                        continue
                    self._last_problem = None

                try:
                    ready, _, _ = select.select(
                        list(self._devices.values()), [], [], 0.25
                    )
                except (OSError, ValueError) as exc:
                    log.warning("Input device select failed (%s); rescanning", exc)
                    self._release_if_down()
                    self._close_all()
                    last_scan = 0.0
                    continue

                for device in ready:
                    self._read_device(device)
        finally:
            self._close_all()
            log.info("Global PTT listener stopped")

    # ------------------------------------------------------------------ devices
    def _scan_devices(self) -> None:
        try:
            # evdev.list_devices() only returns devices that are both readable
            # and writable, so it returns an empty list for users outside the
            # input group.  Glob directly to tell "no devices" apart from
            # "permission denied" and give the correct guidance.
            paths = set(glob.glob("/dev/input/event*"))
        except OSError as exc:
            log.warning("Cannot scan /dev/input: %s", exc)
            self._access_problem = _permission_message([])
            return

        for stale in set(self._devices) - paths:
            self._close_device(stale)

        permission_denied: list[str] = []
        for path in sorted(paths):
            if path in self._devices:
                continue
            try:
                device = InputDevice(path)
            except PermissionError:
                permission_denied.append(path)
                continue
            except OSError as exc:
                log.debug("Skipping %s: %s", path, exc)
                continue

            try:
                capabilities = device.capabilities(verbose=False)
                keys = capabilities.get(ecodes.EV_KEY, [])
                if self._key_code not in keys:
                    device.close()
                    continue
            except OSError as exc:
                log.debug("Cannot read capabilities of %s: %s", path, exc)
                device.close()
                continue

            self._devices[path] = device
            log.info(
                "Watching %s (%s) for PTT key",
                path,
                getattr(device, "name", "unknown"),
            )

        if self._devices:
            self._access_problem = None
        elif permission_denied:
            self._access_problem = _permission_message(permission_denied)
        else:
            self._access_problem = None

    def _read_device(self, device: InputDevice) -> None:
        try:
            for event in device.read():
                if event.type != ecodes.EV_KEY or event.code != self._key_code:
                    continue
                if event.value == 1:  # key down
                    if not self._is_down:
                        self._is_down = True
                        log.debug("PTT pressed")
                        self.pressed.emit()
                elif event.value == 0:  # key up
                    if self._is_down:
                        self._is_down = False
                        log.debug("PTT released")
                        self.released.emit()
                # event.value == 2 is auto-repeat: ignore
        except BlockingIOError:
            return
        except OSError as exc:
            log.warning(
                "Lost input device %s: %s",
                getattr(device, "path", "?"),
                exc,
            )
            self._release_if_down()
            self._close_device(getattr(device, "path", ""))

    def _release_if_down(self) -> None:
        if self._is_down:
            self._is_down = False
            self.released.emit()

    def _close_device(self, path: str) -> None:
        device = self._devices.pop(path, None)
        if device is None:
            return
        try:
            device.close()
        except OSError:
            pass

    def _close_all(self) -> None:
        for path in list(self._devices):
            self._close_device(path)
