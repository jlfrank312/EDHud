"""
EDHud overlay window: a borderless, always-on-top, click-through tkinter window.

No external overlay program is needed - it lives inside EDMC's own process.
Works over Elite Dangerous in *Borderless* or *Windowed* display mode
(not over exclusive Fullscreen - that is a Windows/DirectX limitation).

All methods must be called from the Tk main thread.
"""
from __future__ import annotations

import logging
import sys
import tkinter as tk
from tkinter import font as tkfont

log = logging.getLogger("EDMarketConnector.EDHud.overlay")

COLORS = {
    "title": "#ff9a1f",   # Elite orange
    "head": "#ffffff",
    "text": "#e6e6e6",
    "dim": "#8a929c",
    "good": "#4cff8f",
    "warn": "#ffd24a",
    "bad": "#ff5a4a",
    "accent": "#4fc3ff",
    "dest": "#ff4d4d",    # destination system/station of away-from-home missions (couriers)
    "bg": "#090c11",      # window background
    "border": "#7a4a10",  # window border / divider lines
}
BORDER_UNLOCKED = "#00d0ff"
PAD = 9
BAR_PREFIX = "bar:"      # segment text "bar:0.40" is drawn as a 40% progress bar
MIN_WIDTH = 190
FONT_CANDIDATES = ("Consolas", "Lucida Console", "DejaVu Sans Mono", "Menlo", "Courier New")

# Windows extended window styles
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000


def load_theme(path):
    """
    Override any entry of COLORS from a JSON file like {"dest": "#ff8800", "good": "#00ff66"}.
    Missing file = defaults. This is the single place colours come from, so a game-HUD-colour
    source can later feed it.
    """
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        log.warning("could not read theme file %s", path)
        return 0
    n = 0
    for key, val in data.items():
        if key in COLORS and isinstance(val, str) and len(val) == 7 and val.startswith("#"):
            COLORS[key] = val
            n += 1
    return n


def pick_font_family(root, wanted=None):
    try:
        have = set(tkfont.families(root))
    except tk.TclError:
        have = set()
    for fam in ([wanted] if wanted else []) + list(FONT_CANDIDATES):
        if fam and fam in have:
            return fam
    return "TkFixedFont" if not have else "Courier"


class OverlayWindow:
    def __init__(self, master, panel_id, x, y, on_moved=None, font_family=None, font_size=11,
                 opacity=0.85, locked=True):
        self.master = master
        self.panel_id = panel_id
        self.x, self.y = int(x), int(y)
        self.on_moved = on_moved
        self.locked = locked
        self.visible = False
        self._last_key = None
        self._drag = None

        self.win = tk.Toplevel(master)
        self.win.withdraw()
        self.win.title(f"EDHud {panel_id}")
        self.win.overrideredirect(True)
        self.win.configure(bg=COLORS["bg"])
        self.canvas = tk.Canvas(self.win, bg=COLORS["bg"], highlightthickness=1, bd=0,
                                highlightbackground=COLORS["border"], highlightcolor=COLORS["border"])
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)

        self._font_family = pick_font_family(master, font_family)
        self._build_fonts(font_size)
        self.set_opacity(opacity)
        self._apply_platform_style()

    # -- fonts / style ---------------------------------------------------
    def _build_fonts(self, size):
        self.font = tkfont.Font(root=self.master, family=self._font_family, size=int(size))
        self.font_bold = tkfont.Font(root=self.master, family=self._font_family, size=int(size), weight="bold")
        self.line_h = self.font.metrics("linespace") + 3
        self._bar_w = self.font.measure("0") * 8

    def set_font(self, family, size):
        self._font_family = pick_font_family(self.master, family)
        self._build_fonts(size)
        self._last_key = None      # force redraw

    def set_opacity(self, value):
        try:
            self.win.attributes("-alpha", max(0.2, min(1.0, float(value))))
        except tk.TclError:
            pass

    def set_locked(self, locked):
        self.locked = bool(locked)
        self.canvas.configure(highlightbackground=COLORS["border"] if self.locked else BORDER_UNLOCKED,
                              highlightcolor=COLORS["border"] if self.locked else BORDER_UNLOCKED)
        self._apply_platform_style()
        self._last_key = None

    def set_position(self, x, y):
        self.x, self.y = int(x), int(y)
        self._place()

    # -- platform: topmost, no focus stealing, click-through -----------------
    def _apply_platform_style(self):
        try:
            self.win.attributes("-topmost", True)
        except tk.TclError:
            pass
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            self.win.update_idletasks()
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            get = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
            setw = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            get.restype, get.argtypes = ctypes.c_ssize_t, [wintypes.HWND, ctypes.c_int]
            setw.restype, setw.argtypes = ctypes.c_ssize_t, [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            style = get(hwnd, _GWL_EXSTYLE)
            style |= _WS_EX_LAYERED | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
            if self.locked:
                style |= _WS_EX_TRANSPARENT          # mouse clicks fall through to the game
            else:
                style &= ~_WS_EX_TRANSPARENT         # allow dragging
            setw(hwnd, _GWL_EXSTYLE, style)
            # SWP_NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
        except Exception:
            log.exception("could not apply Windows overlay styles")

    def keep_on_top(self):
        if self.visible:
            try:
                self.win.attributes("-topmost", True)
                self.win.lift()
            except tk.TclError:
                pass

    # -- drawing -----------------------------------------------------------
    def set_lines(self, lines):
        """Draw `lines` (see edhud_panels). Empty/None hides the window."""
        if not lines:
            self.hide()
            return
        key = (repr(lines), self.locked, self._font_family, self.font.cget("size"))
        if key == self._last_key and self.visible:
            return
        self._last_key = key
        self._redraw(lines)
        self.show()

    def _seg_width(self, segs, bold=False):
        f = self.font_bold if bold else self.font
        return sum(self._bar_w if t.startswith(BAR_PREFIX) else f.measure(t) for t, _ in segs)

    def _redraw(self, lines):
        c = self.canvas
        c.delete("all")
        gap = self.font.measure("    ")
        width = MIN_WIDTH
        rules = {i for i, l in enumerate(lines) if l.get("rule")} or {0}
        is_bold = lambda i, l: l.get("bold", i == 0)
        for i, ln in enumerate(lines):
            lw = self._seg_width(ln["left"], bold=is_bold(i, ln))
            rw = self._seg_width(ln["right"])
            width = max(width, lw + (gap if rw else 0) + rw)
        total_w = width + 2 * PAD
        hint = not self.locked
        gap = PAD // 2 + 2                      # padding either side of the line between a "box" header and the body
        has_box = any(l.get("box") for l in lines)
        total_h = PAD * 2 + len(lines) * self.line_h + (self.line_h if hint else 0) + ((2 * gap + 1) if has_box else 0) \
            + (2 * gap + 1) * sum(1 for l in lines if l.get("pad"))

        y = PAD
        for i, ln in enumerate(lines):
            x = PAD
            for text, style in ln["left"]:
                f = self.font_bold if (is_bold(i, ln) or style in ("head", "title")) else self.font
                x += self._draw_seg(c, text, style, f, x, y)
            xr = total_w - PAD - self._seg_width(ln["right"])
            for text, style in ln["right"]:
                xr += self._draw_seg(c, text, style, self.font, xr, y)
            y += self.line_h
            if i in rules and ln.get("pad"):      # padded title row: room below the text, then the divider, then room again
                y += gap
                c.create_line(PAD, y, total_w - PAD, y, fill=COLORS["border"])
                y += gap + 1
            elif i in rules:
                c.create_line(PAD, y - 1, total_w - PAD, y - 1, fill=COLORS["border"])
            if ln.get("box") and not (i + 1 < len(lines) and lines[i + 1].get("box")):
                # header drawn like its own window: full-width border line, attached to the body below it
                y += gap
                c.create_line(0, y, total_w + 2, y, fill=COLORS["border"])
                y += gap + 1
        if hint:
            c.create_text(PAD, y, anchor="nw", text="drag to move", font=self.font, fill=COLORS["accent"])

        c.configure(width=total_w, height=total_h)
        self._size = (total_w + 2, total_h + 2)   # + highlight border
        self._place()

    def _draw_seg(self, c, text, style, f, x, y):
        """Draw one segment at (x, y); returns its width."""
        color = COLORS.get(style, COLORS["text"])
        if text.startswith(BAR_PREFIX):
            try:
                frac = float(text[len(BAR_PREFIX):])
            except ValueError:
                frac = 0.0
            top, bottom = y + 3, y + self.line_h - 5
            c.create_rectangle(x, top, x + self._bar_w, bottom, outline=COLORS["border"], fill="#161b22")
            if frac > 0:
                c.create_rectangle(x, top, x + max(2, self._bar_w * frac), bottom, outline=color, fill=color)
            return self._bar_w
        c.create_text(x, y, anchor="nw", text=text, font=f, fill=color)
        return f.measure(text)

    def _place(self):
        w, h = getattr(self, "_size", (MIN_WIDTH, 60))
        self.win.geometry(f"{w}x{h}+{self.x}+{self.y}")

    def show(self):
        if not self.visible:
            self.visible = True
            self._place()
            self.win.deiconify()
            self._apply_platform_style()
        self.win.lift()

    def hide(self):
        if self.visible:
            self.visible = False
            self._last_key = None
            self.win.withdraw()

    def destroy(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    # -- dragging (only when unlocked) ---------------------------------------
    def _press(self, ev):
        if not self.locked:
            self._drag = (ev.x_root - self.x, ev.y_root - self.y)

    def _motion(self, ev):
        if self._drag and not self.locked:
            self.x, self.y = ev.x_root - self._drag[0], ev.y_root - self._drag[1]
            self._place()

    def _release(self, ev):
        if self._drag:
            self._drag = None
            if self.on_moved:
                self.on_moved(self.panel_id, self.x, self.y)
