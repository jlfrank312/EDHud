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

import edhud_cargo
import edhud_curve

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
    "pay": "#4cff8f",     # mission payout column (own style so it can follow the EDHM theme separately from "good")
    "bg": "#090c11",      # window background
    "border": "#7a4a10",  # window border / divider lines
}
BASE_COLORS = dict(COLORS)   # the colours EDHud starts with (built-ins + edhud_theme.json): what "match theme" OFF restores
_palette_version = 0         # bumped by apply_palette so open windows redraw even when their text did not change
BORDER_UNLOCKED = "#00d0ff"
PAD = 9
BAR_PREFIX = "bar:"      # segment text "bar:0.40" is drawn as a 40% progress bar
MIN_WIDTH = 190
CBAR_MIN_TRACK_CHARS = 36   # minimum width of the cargo bar track, in characters
BAR_TRACK_FILL = "#161b22"
FONT_CANDIDATES = ("Consolas", "Lucida Console", "DejaVu Sans Mono", "Menlo", "Courier New")

# Windows extended window styles
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000
_HWND_TOPMOST = -1
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010


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
    edhud_cargo.set_overrides(data.get("commodity_colors"))     # {"gold": "#ffcc00", ...} for the cargo bar
    for key, val in data.items():
        if key in COLORS and isinstance(val, str) and len(val) == 7 and val.startswith("#"):
            COLORS[key] = val
            n += 1
    return n


def snapshot_base():
    """Remember the current COLORS as the base palette (call after load_theme)."""
    BASE_COLORS.clear()
    BASE_COLORS.update(COLORS)


def apply_palette(palette):
    """
    Switch COLORS to `palette` in place (any subset of keys). `bg` is never changed: it is the text window's
    transparent colour key. Windows pick the new colours up on their next redraw.
    """
    global _palette_version
    for key, val in palette.items():
        if key in COLORS and key != "bg" and isinstance(val, str) and len(val) == 7 and val.startswith("#"):
            COLORS[key] = val
    _palette_version += 1


def shade(color, factor):
    """Darken (<1) or lighten (>1) a #rrggbb colour."""
    try:
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return color
    f = lambda v: max(0, min(255, int(v * factor)))
    return "#%02x%02x%02x" % (f(r), f(g), f(b))


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
    """
    One HUD panel. On Windows it is two stacked, click-through windows so that the background and the
    text can have different opacity: a background window (box + border, alpha = window opacity) and a text
    window on top (colour-keyed to the background colour, alpha = text opacity). Elsewhere - or if the
    colour key is not supported - it is a single window where one opacity applies to everything.
    """

    def __init__(self, master, panel_id, x, y, on_moved=None, font_family=None, font_size=11,
                 opacity=0.85, text_opacity=1.0, locked=True, dual=None, curve=None):
        self.master = master
        self.panel_id = panel_id
        self.x, self.y = int(x), int(y)
        self.on_moved = on_moved
        self.locked = locked
        self.visible = False
        self._last_key = None
        self._drag = None
        self._cur_lines = []
        self.dual = (sys.platform == "win32") if dual is None else bool(dual)
        # cockpit-curvature effect (edhud_curve): CurveParams or None = flat panel
        self.curve = curve
        self._curved = False            # what is on screen right now
        self._curve_failed = False      # latched if the effect ever errors: stay flat rather than retry every tick
        self._photos = []               # Tk keeps no reference to PhotoImages
        self._origin = (0, 0)           # window top-left relative to (x, y); non-zero when the warped panel overhangs
        self._twin_inset = 1            # text window offset inside the background window (1px frame when flat)
        self._pil_cache = None
        self._frame_cache = None        # (key, warped frame picture) for the background window
        self._applied_key = ""          # transparency key currently set on the background window ("" = none)
        self.debug_layers = None        # (geometry, {name: PIL image}) of the last curved draw - for tests/previews

        self.win = tk.Toplevel(master)
        self.win.withdraw()
        self.win.title(f"EDHud {panel_id}")
        self.win.overrideredirect(True)
        self.win.configure(bg=COLORS["bg"])
        self.bgcanvas = tk.Canvas(self.win, bg=COLORS["bg"], highlightthickness=1, bd=0,
                                  highlightbackground=COLORS["border"], highlightcolor=COLORS["border"])
        self.bgcanvas.pack()
        self.twin = None
        self.canvas = self.bgcanvas
        if self.dual:
            try:
                self.twin = tk.Toplevel(master)
                self.twin.withdraw()
                self.twin.title(f"EDHud {panel_id} text")
                self.twin.overrideredirect(True)
                self.twin.configure(bg=COLORS["bg"])
                self.twin.attributes("-transparentcolor", COLORS["bg"])
                self.canvas = tk.Canvas(self.twin, bg=COLORS["bg"], highlightthickness=0, bd=0)
                self.canvas.pack()
            except tk.TclError:
                log.info("colour-keyed text window not supported here; using one window for %s", panel_id)
                if self.twin is not None:
                    try:
                        self.twin.destroy()
                    except tk.TclError:
                        pass
                self.twin, self.canvas, self.dual = None, self.bgcanvas, False
        for cv in {self.bgcanvas, self.canvas}:
            cv.bind("<ButtonPress-1>", self._press)
            cv.bind("<B1-Motion>", self._motion)
            cv.bind("<ButtonRelease-1>", self._release)

        self._font_family = pick_font_family(master, font_family)
        self._build_fonts(font_size)
        self.set_opacity(opacity)
        self.set_text_opacity(text_opacity)
        self._apply_platform_style()

    def _windows(self):
        return [w for w in (self.win, self.twin) if w is not None]

    # -- fonts / style ---------------------------------------------------
    def _build_fonts(self, size):
        self.font = tkfont.Font(root=self.master, family=self._font_family, size=int(size))
        self.font_bold = tkfont.Font(root=self.master, family=self._font_family, size=int(size), weight="bold")
        self.line_h = self.font.metrics("linespace") + 3
        self._bar_w = self.font.measure("0") * 8
        self._pil_cache = None

    def set_curve(self, params):
        """Turn the cockpit-curvature effect on (CurveParams) or off (None). Takes effect on the next draw."""
        if params != self.curve:
            self.curve = params
            self._curve_failed = False
            self._last_key = None

    def _curve_ready(self):
        """CurveParams to draw with, or None when the panel should be flat."""
        if self.curve is None or self._curve_failed or not edhud_curve.available():
            return None
        return self.curve

    def _pil_fonts(self):
        """Pillow twins of the two Tk fonts (matched on character width), or None if no font file can be found."""
        key = (self._font_family, self.font.cget("size"))
        if self._pil_cache is None or self._pil_cache[0] != key:
            S = edhud_curve.SUPERSAMPLE
            fonts = {}
            for tkf, bold in ((self.font, False), (self.font_bold, True)):
                pf = edhud_curve.load_font(self._font_family, bold, tkf.measure("0"), S)
                if pf is None:
                    fonts = None
                    break
                fonts[id(tkf)] = pf
            self._pil_cache = (key, fonts)
        return self._pil_cache[1]

    def set_font(self, family, size):
        self._font_family = pick_font_family(self.master, family)
        self._build_fonts(size)
        self._last_key = None      # force redraw

    @staticmethod
    def _clamp(value, lo=0.2):
        return max(lo, min(1.0, float(value)))

    def set_opacity(self, value):
        """Window (background + border) opacity. In single-window mode this is the whole panel's opacity."""
        try:
            self.win.attributes("-alpha", self._clamp(value))
        except tk.TclError:
            pass

    def set_text_opacity(self, value):
        """Text opacity (only separable from the window opacity in two-window mode)."""
        self.text_opacity = self._clamp(value, 0.3)
        if self.twin is not None:
            try:
                self.twin.attributes("-alpha", self.text_opacity)
            except tk.TclError:
                pass

    def set_locked(self, locked):
        self.locked = bool(locked)
        col = COLORS["border"] if self.locked else BORDER_UNLOCKED
        self.bgcanvas.configure(highlightbackground=col, highlightcolor=col)
        self._apply_platform_style()
        self._last_key = None

    def set_position(self, x, y):
        self.x, self.y = int(x), int(y)
        self._place()

    # -- platform: topmost, no focus stealing, click-through -----------------
    def _apply_platform_style(self):
        for w in self._windows():
            try:
                w.attributes("-topmost", True)
            except tk.TclError:
                pass
        if sys.platform != "win32":
            return
        for w in self._windows():
            self._style_windows_window(w)

    @staticmethod
    def _hwnd_of(user32, w):
        """Win32 handle of the Tk toplevel (Tk wraps it in a parent window; that wrapper is the real top-level)."""
        return user32.GetParent(w.winfo_id()) or w.winfo_id()

    def _style_windows_window(self, w):
        try:
            import edhud_winapi
            w.update_idletasks()
            user32 = edhud_winapi.apis().user32        # private handle: never touches other plugins' prototypes
            hwnd = self._hwnd_of(user32, w)
            get = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
            setw = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            style = get(hwnd, _GWL_EXSTYLE)
            style |= _WS_EX_LAYERED | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
            if self.locked:
                style |= _WS_EX_TRANSPARENT          # mouse clicks fall through to the game
            else:
                style &= ~_WS_EX_TRANSPARENT         # allow dragging
            setw(hwnd, _GWL_EXSTYLE, style)
            # SWP_NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
        except Exception:
            log.exception("could not apply Windows overlay styles")

    def _raise_topmost_win32(self):
        """
        Re-assert topmost with one SetWindowPos(HWND_TOPMOST, NOACTIVATE) per window, background window first so
        the text window stays above it. Avoids toggling Tk's -topmost attribute. True if every call succeeded.
        """
        try:
            import edhud_winapi
            user32 = edhud_winapi.apis().user32
            for w in self._windows():
                hwnd = self._hwnd_of(user32, w)
                if not user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE):
                    return False
            return True
        except Exception:
            log.debug("SetWindowPos topmost failed; falling back to Tk", exc_info=True)
            return False

    def keep_on_top(self):
        if not self.visible:
            return
        if sys.platform == "win32" and self._raise_topmost_win32():
            return
        for w in self._windows():          # background first, text on top of it
            try:
                w.attributes("-topmost", True)
                w.lift()
            except tk.TclError:
                pass

    # -- drawing -----------------------------------------------------------
    def set_lines(self, lines):
        """Draw `lines` (see edhud_panels). Empty/None hides the window."""
        if not lines:
            self.hide()
            return
        key = (repr(lines), self.locked, self._font_family, self.font.cget("size"), self.curve, _palette_version)
        if key == self._last_key and self.visible:
            return
        self._last_key = key
        self._cur_lines = lines
        self._redraw(lines)
        self.show()

    def _seg_width(self, segs, bold=False):
        f = self.font_bold if bold else self.font
        return sum(self._bar_w if t.startswith(BAR_PREFIX) else f.measure(t) for t, _ in segs)

    def _cbar_texts(self, spec):
        label = spec.get("label", "CARGO")
        total, cap = spec.get("total", 0), spec.get("capacity")
        right = f"{total}/{cap} tons full" if cap else f"{total} tons"
        style = "text"
        if cap:
            style = "bad" if total >= cap else ("warn" if total >= 0.9 * cap else "text")
        return label, right, style

    def _row_h(self, ln):
        """The cargo bar row is two lines tall (names above the bar) when it has anything to label."""
        cb = ln.get("cbar")
        return self.line_h * (2 if cb and cb.get("items") else 1)

    def _line_widths(self, ln, i, is_bold):
        if ln.get("cbar"):
            label, right, _ = self._cbar_texts(ln["cbar"])
            lw = self.font_bold.measure(label + " ") + self.font.measure("0") * CBAR_MIN_TRACK_CHARS
            return lw, self.font.measure("  " + right)
        return self._seg_width(ln["left"], bold=is_bold(i, ln)), self._seg_width(ln["right"])

    def _redraw(self, lines):
        if self._curve_ready():
            fonts = self._pil_fonts()
            if fonts:
                try:
                    pc = edhud_curve.PilCanvas(fonts)
                    total_w, total_h = self._draw_panel(lines, pc)
                    self._present_curved(pc, total_w, total_h)
                    return
                except Exception:
                    log.exception("cockpit curvature failed; showing the flat panel instead")
            else:
                log.warning("cockpit curvature needs a TrueType monospace font Pillow can find; showing the flat panel")
            self._curve_failed = True
        total_w, total_h = self._draw_panel(lines, self.canvas)
        self._present_flat(total_w, total_h)

    def _draw_panel(self, lines, c):
        """Lay out and draw `lines` on `c` (the Tk canvas, or a PilCanvas for the curved path). Returns (w, h)."""
        c.delete("all")
        gap = self.font.measure("    ")
        pad_g = PAD // 2 + 2                    # padding either side of a divider that gets its own breathing room
        width = MIN_WIDTH
        rules = {i for i, l in enumerate(lines) if l.get("rule", i == 0)}
        is_bold = lambda i, l: l.get("bold", i == 0)
        for i, ln in enumerate(lines):
            lw, rw = self._line_widths(ln, i, is_bold)
            width = max(width, lw + (gap if rw and not ln.get("cbar") else 0) + rw)
        total_w = width + 2 * PAD
        hint = not self.locked
        extra = sum(2 * pad_g + 1 for l in lines if l.get("box") or l.get("pad"))      # padded divider rows
        extra += sum(pad_g * 2 for l in lines if l.get("rule_above"))                  # divider above a row
        total_h = PAD * 2 + sum(self._row_h(l) for l in lines) + (self.line_h if hint else 0) + extra

        y = PAD
        for i, ln in enumerate(lines):
            x = PAD
            if ln.get("rule_above"):
                y += pad_g
                c.create_line(PAD, y, total_w - PAD, y, fill=COLORS["border"])
                y += pad_g
            if ln.get("cbar"):
                self._draw_cbar(c, ln["cbar"], y, total_w)
            else:
                for text, style in ln["left"]:
                    f = self.font_bold if (is_bold(i, ln) or style in ("head", "title")) else self.font
                    x += self._draw_seg(c, text, style, f, x, y)
                xr = total_w - PAD - self._seg_width(ln["right"])
                for text, style in ln["right"]:
                    xr += self._draw_seg(c, text, style, self.font, xr, y)
            y += self._row_h(ln)
            if i in rules and ln.get("pad"):      # padded title row: room below the text, the divider, room again
                y += pad_g
                c.create_line(PAD, y, total_w - PAD, y, fill=COLORS["border"])
                y += pad_g + 1
            elif i in rules:
                c.create_line(PAD, y - 1, total_w - PAD, y - 1, fill=COLORS["border"])
            if ln.get("box") and not (i + 1 < len(lines) and lines[i + 1].get("box")):
                # header drawn like its own window: full-width border line, attached to the body below it
                y += pad_g
                c.create_line(0, y, total_w + 2, y, fill=COLORS["border"])
                y += pad_g + 1
        if hint:
            c.create_text(PAD, y, anchor="nw", text="drag to move", font=self.font, fill=COLORS["accent"])
        return total_w, total_h

    # -- presenting the drawn panel -----------------------------------------------------------------------
    def _set_window_mode(self, curved):
        """Switch the background window between 'flat box with a frame' and 'colour-keyed picture of the warped panel'."""
        changed = curved != self._curved
        self._curved = curved
        self._twin_inset = 0 if curved else 1
        self.bgcanvas.configure(highlightthickness=0 if curved else 1)
        if self.dual:
            key = self._bg_key_hex if curved else ""
            if key != self._applied_key:
                self._applied_key = key
                try:
                    self.win.attributes("-transparentcolor", key)
                except tk.TclError:
                    pass
                changed = True
        if changed:
            self._apply_platform_style()

    def _present_flat(self, total_w, total_h):
        self._set_window_mode(False)
        self._origin = (0, 0)
        if self.bgcanvas is not self.canvas:
            self.bgcanvas.delete("all")          # drop a curved picture left from before
        self.bgcanvas.configure(bg=COLORS["bg"])
        self.win.configure(bg=COLORS["bg"])
        self.canvas.configure(width=total_w, height=total_h)
        if self.bgcanvas is not self.canvas:
            self.bgcanvas.configure(width=total_w, height=total_h)
        self._size = (total_w + 2, total_h + 2)   # + highlight border of the background window
        self._inner = (total_w, total_h)
        self._photos = []
        self._place()

    def _curved_images(self, pc, total_w, total_h):
        """Warped, softened pictures for the current look: (geometry, {"bg": img, "text": img}) or {"all": img}."""
        S = edhud_curve.SUPERSAMPLE
        W, H = total_w + 2, total_h + 2                 # flat size including the 1px frame
        geom = edhud_curve.geometry(W, H, self.curve)
        bg = edhud_curve.hex_rgb(COLORS["bg"])
        border_hex = COLORS["border"] if self.locked else BORDER_UNLOCKED
        border = edhud_curve.hex_rgb(border_hex)
        if self.dual:
            self._bg_key = edhud_curve.pick_key(bg, border)
            # the frame picture depends only on size/colours/settings, so it is reused while the content changes
            fkey = (W, H, bg, border, self.curve, self._bg_key)
            if self._frame_cache is None or self._frame_cache[0] != fkey:
                frame = pc.rasterize(W, H, bg, S, border=border, content=False)
                self._frame_cache = (fkey, edhud_curve.warp(frame, geom, self.curve, self._bg_key))
            text = pc.rasterize(W, H, bg, S)
            layers = {"bg": self._frame_cache[1], "text": edhud_curve.warp(text, geom, self.curve, bg)}
        else:
            whole = pc.rasterize(W, H, bg, S, border=border)
            layers = {"all": edhud_curve.warp(whole, geom, self.curve, bg)}
        return geom, layers

    @property
    def _bg_key_hex(self):
        return "#%02x%02x%02x" % getattr(self, "_bg_key", (1, 1, 1))

    def _present_curved(self, pc, total_w, total_h):
        geom, layers = self._curved_images(pc, total_w, total_h)
        self.debug_layers = (geom, layers)
        self._set_window_mode(True)
        bw, bh = geom.size
        photos = []
        targets = {"bg": self.bgcanvas, "text": self.canvas} if self.dual else {"all": self.bgcanvas}
        for name, cv in targets.items():
            photo = tk.PhotoImage(master=self.master, data=edhud_curve.ppm_bytes(layers[name]))
            photos.append(photo)
            cv.delete("all")
            cv.configure(width=bw, height=bh, highlightthickness=0,
                         bg=(self._bg_key_hex if (self.dual and name == "bg") else COLORS["bg"]))
            cv.create_image(0, 0, anchor="nw", image=photo)
        if self.dual:
            self.win.configure(bg=self._bg_key_hex)
        self._photos = photos
        self._size = (bw, bh)
        self._inner = (bw, bh)
        self._origin = geom.offset
        self._place()

    def _draw_cbar(self, c, spec, y, total_w):
        """
        Two rows when there is cargo:
            names:   Gold      Silver
            bar:     CARGO [-- 42 --][- 8 -]                     50/100 tons full
        Each commodity has its own colour; its name sits above its segment (shortened to fit, see
        edhud_cargo.shorten_name). Stacks too small to label are summarised at the right of the names row.
        """
        items = spec.get("items", [])
        label, right, rstyle = self._cbar_texts(spec)
        by = y + (self.line_h if items else 0)                       # y of the bar row
        lab_w = self.font_bold.measure(label + " ")
        c.create_text(PAD, by, anchor="nw", text=label, font=self.font_bold, fill=COLORS["title"])
        rw = self.font.measure("  " + right)
        c.create_text(total_w - PAD, by, anchor="ne", text=right, font=self.font, fill=COLORS.get(rstyle, COLORS["text"]))
        x0, x1 = PAD + lab_w, total_w - PAD - rw
        top, bottom = by + 2, by + self.line_h - 3
        c.create_rectangle(x0, top, x1, bottom, outline=COLORS["border"], fill=BAR_TRACK_FILL)
        track = max(1, x1 - x0 - 2)
        cap = spec.get("capacity") or max(1, spec.get("total", 0))
        x = x0 + 1
        small, label_end = [], x0
        colors = edhud_cargo.palette_for([(it[3], it[0]) for it in items])
        for (name, count, mission, key), fill in zip(items, colors):
            w = max(3, int(round(track * count / cap)))
            w = min(w, x1 - 1 - x)
            if w <= 0:
                small.append((name, count))
                continue
            c.create_rectangle(x, top + 1, x + w, bottom - 1, outline=COLORS["good"] if mission else COLORS["bg"], fill=fill)
            if self.font.measure(str(count)) <= w - 4:               # tons inside the segment when it fits
                c.create_text(x + w / 2, by + self.line_h / 2 - 1, text=str(count), font=self.font,
                              fill=edhud_cargo.text_color_on(fill, avoid=COLORS["bg"]))
            short = edhud_cargo.shorten_name(str(name), lambda t, w=w: self.font.measure(t) <= w - 3)
            if short:
                c.create_text(x + 1, y, anchor="nw", text=short, font=self.font, fill=fill)
                label_end = max(label_end, x + 1 + self.font.measure(short))
            else:
                small.append((name, count))
            x += w
        if small:                                                    # unlabeled stacks: name them off to the right
            # first choice: right-aligned with the bar; if the labels leave too little room there, the names row
            # above the "x/y tons full" text is free, so let the hint extend over it
            done = False
            for edge in (x1, total_w - PAD):
                room = edge - label_end - 10
                for text in (", ".join(f"{n} {k}" for n, k in small), f"+{len(small)} small"):
                    text = ("also " + text) if not text.startswith("+") else text
                    if self.font.measure(text) <= room:
                        c.create_text(edge, y, anchor="ne", text=text, font=self.font, fill=COLORS["dim"])
                        done = True
                        break
                if done:
                    break

    def _draw_seg(self, c, text, style, f, x, y):
        """Draw one segment at (x, y); returns its width."""
        color = COLORS.get(style, COLORS["text"])
        if text.startswith(BAR_PREFIX):
            try:
                frac = float(text[len(BAR_PREFIX):])
            except ValueError:
                frac = 0.0
            top, bottom = y + 3, y + self.line_h - 5
            c.create_rectangle(x, top, x + self._bar_w, bottom, outline=COLORS["border"], fill=BAR_TRACK_FILL)
            if frac > 0:
                c.create_rectangle(x, top, x + max(2, self._bar_w * frac), bottom, outline=color, fill=color)
            return self._bar_w
        c.create_text(x, y, anchor="nw", text=text, font=f, fill=color)
        return f.measure(text)

    def _place(self):
        w, h = getattr(self, "_size", (MIN_WIDTH, 60))
        ox, oy = self._origin
        self.win.geometry(f"{w}x{h}+{self.x + ox}+{self.y + oy}")
        if self.twin is not None:
            iw, ih = getattr(self, "_inner", (w - 2, h - 2))
            k = self._twin_inset      # flat: inside the background window's 1px border; curved: same box exactly
            self.twin.geometry(f"{iw}x{ih}+{self.x + ox + k}+{self.y + oy + k}")

    def show(self):
        if not self.visible:
            self.visible = True
            self._place()
            for w in self._windows():
                w.deiconify()
            self._apply_platform_style()
        for w in self._windows():
            w.lift()

    def hide(self):
        if self.visible:
            self.visible = False
            self._last_key = None
            for w in self._windows():
                w.withdraw()

    def destroy(self):
        for w in self._windows():
            try:
                w.destroy()
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
