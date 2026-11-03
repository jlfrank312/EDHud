"""
Global hotkey for toggling the overlay (Windows only - uses RegisterHotKey, so it works while the game has focus).

parse_hotkey("Ctrl+Alt+H") -> (modifier_mask, virtual_key) | None
combo_from_event(event, ...) -> "Ctrl+Alt+H" | None     (for the settings capture box)
HotkeyListener(callback).start("Ctrl+Alt+H")           (runs a message loop in a daemon thread)
"""
from __future__ import annotations

import logging
import sys
import threading

log = logging.getLogger("EDMarketConnector.EDHud.hotkey")

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_NAMED = {"space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B, "insert": 0x2D, "delete": 0x2E, "home": 0x24,
          "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
          "backspace": 0x08, "pause": 0x13, "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63,
          "numpad4": 0x64, "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67, "numpad8": 0x68, "numpad9": 0x69}
# Tk keysym -> our key name
_KEYSYMS = {"space": "space", "Tab": "tab", "Return": "enter", "Escape": "esc", "Insert": "insert", "Delete": "delete",
            "Home": "home", "End": "end", "Prior": "pageup", "Next": "pagedown", "Left": "left", "Up": "up",
            "Right": "right", "Down": "down", "BackSpace": "backspace", "Pause": "pause"}
_MODIFIER_KEYSYMS = {"Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R",
                     "Super_L", "Super_R", "Caps_Lock", "Num_Lock", "ISO_Level3_Shift"}


def _key_vk(key):
    k = key.lower()
    if len(k) == 1 and (k.isalpha() or k.isdigit()):
        return ord(k.upper())
    if k.startswith("f") and k[1:].isdigit() and 1 <= int(k[1:]) <= 24:
        return 0x70 + int(k[1:]) - 1
    return _NAMED.get(k)


def parse_hotkey(combo):
    """'Ctrl+Alt+H' -> (MOD_CONTROL|MOD_ALT, 0x48). None if empty/invalid, or if it has neither a modifier nor an F-key."""
    if not combo or not isinstance(combo, str):
        return None
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        return None
    mods, key = 0, parts[-1]
    for p in parts[:-1]:
        m = _MODS.get(p.lower())
        if m is None:
            return None
        mods |= m
    vk = _key_vk(key)
    if vk is None or (not mods and not key.lower().startswith("f")):
        return None
    return mods, vk


def normalise(combo):
    """Canonical text for a valid combo ('control+h' -> 'Ctrl+H'), else ''."""
    parsed = parse_hotkey(combo)
    if not parsed:
        return ""
    mods, vk = parsed
    names = [n for n, bit in (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN)) if mods & bit]
    key = combo.split("+")[-1].strip()
    key = key.upper() if (len(key) == 1 or (key.lower().startswith("f") and key[1:].isdigit())) else key.capitalize()
    return "+".join(names + [key])


def combo_from_event(keysym, state, windows=None):
    """Build 'Ctrl+Alt+H' from a Tk <KeyPress> keysym and state mask; None for a bare modifier / unsupported key."""
    if keysym in _MODIFIER_KEYSYMS:
        return None
    windows = (sys.platform == "win32") if windows is None else windows
    parts = []
    if state & 0x4:
        parts.append("Ctrl")
    if state & (0x20000 if windows else 0x8):          # Tk: Alt is 0x20000 on Windows, Mod1 (0x8) elsewhere
        parts.append("Alt")
    if state & 0x1:
        parts.append("Shift")
    if keysym in _KEYSYMS:
        key = _KEYSYMS[keysym]
    elif len(keysym) == 1 and keysym.isalnum():
        key = keysym.upper()
    elif keysym.upper().startswith("F") and keysym[1:].isdigit():
        key = keysym.upper()
    elif keysym.startswith("KP_") and keysym[3:].isdigit():
        key = "numpad" + keysym[3:]
    else:
        return None
    combo = "+".join(parts + [key])
    return normalise(combo) or None


class HotkeyListener:
    """Registers one global hotkey and calls `callback()` (on its own thread) each time it is pressed."""

    def __init__(self, callback, api=None):
        self.callback = callback
        self._api = api                 # object with .user32 / .kernel32 (injectable for tests)
        self._thread = None
        self._tid = None
        self.error = ""

    def _apis(self):
        if self._api is not None:
            return self._api
        import edhud_winapi
        return edhud_winapi.apis()

    def start(self, combo):
        """True if the hotkey is active. self.error says why not."""
        self.stop()
        self.error = ""
        parsed = parse_hotkey(combo)
        if not combo:
            return False
        if parsed is None:
            self.error = "not a valid combination"
            return False
        if self._api is None and sys.platform != "win32":
            self.error = "global hotkeys need Windows"
            return False
        mods, vk = parsed
        ready, result = threading.Event(), {}

        def run():
            api = self._apis()
            u, k = api.user32, api.kernel32
            try:
                self._tid = k.GetCurrentThreadId()
                result["ok"] = bool(u.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk))
            except Exception as exc:                              # pragma: no cover - platform specific
                result["ok"] = False
                log.exception("RegisterHotKey failed")
            finally:
                ready.set()
            if not result.get("ok"):
                return
            try:
                from ctypes import wintypes, byref
                msg = wintypes.MSG()
                while u.GetMessageW(byref(msg), None, 0, 0) > 0:
                    if msg.message == WM_HOTKEY:
                        try:
                            self.callback()
                        except Exception:
                            log.exception("hotkey callback failed")
            finally:
                try:
                    u.UnregisterHotKey(None, 1)
                except Exception:
                    pass

        self._thread = threading.Thread(target=run, name="EDHud-hotkey", daemon=True)
        self._thread.start()
        ready.wait(2.0)
        if not result.get("ok"):
            self.error = "already used by another program" if "ok" in result else "could not register"
            self._thread = None
            return False
        return True

    def stop(self):
        t, self._thread = self._thread, None
        if t is not None and t.is_alive() and self._tid is not None:
            try:
                self._apis().user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            except Exception:
                pass
            t.join(1.0)
