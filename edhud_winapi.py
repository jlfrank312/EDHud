"""
Private ctypes handles to user32 / kernel32 for EDHud (Windows only).

ctypes.windll.user32 is one shared object for the whole EDMC process: setting .argtypes / .restype on its functions
changes them for every other plugin (and EDMC) that uses the same function. WinDLL() gives this plugin its own
library instance, so the prototypes below affect nothing but EDHud.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

_cache = {}
_build_lock = threading.Lock()      # the hotkey thread and the Tk thread may both ask first


class _Apis:
    def __init__(self, user32, kernel32):
        self.user32, self.kernel32 = user32, kernel32


def apis():
    """Return an object with .user32 and .kernel32 (declared prototypes, private to EDHud). Raises off-Windows."""
    with _build_lock:
        return _apis_locked()


def _apis_locked():
    if "apis" not in _cache:
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        ptr = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW   # 32-bit Python only has the non-Ptr names
        sptr = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
        ptr.restype, ptr.argtypes = ctypes.c_ssize_t, [wintypes.HWND, ctypes.c_int]
        sptr.restype, sptr.argtypes = ctypes.c_ssize_t, [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        u.GetParent.restype, u.GetParent.argtypes = wintypes.HWND, [wintypes.HWND]
        u.SetWindowPos.restype = wintypes.BOOL
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.UINT]
        u.RegisterHotKey.restype = wintypes.BOOL
        u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        u.UnregisterHotKey.restype = wintypes.BOOL
        u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetMessageW.restype = wintypes.BOOL        # signed: -1 means error, so callers test "> 0"
        u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u.PostThreadMessageW.restype = wintypes.BOOL
        u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        k.GetCurrentThreadId.restype, k.GetCurrentThreadId.argtypes = wintypes.DWORD, []
        _cache["apis"] = _Apis(u, k)
    return _cache["apis"]
