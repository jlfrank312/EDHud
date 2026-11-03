"""
EDHud "cockpit curvature": makes a panel look like it sits on the curved cockpit view-screen instead of a flat sheet,
like the game's own Info / side panels - the panel is turned about the vertical axis (so the edge nearer the pilot is a
little taller than the far one) and gets a very slight depth-of-field softness that grows towards the far edge.

Tk windows cannot be skewed, so the panel is drawn into a Pillow image instead of onto the Tk canvas, warped with a
perspective transform, softened, and handed back to the overlay as a picture. Pillow ships with EDMC; if it is missing
(or anything here fails) the overlay simply stays flat.

Everything in this module is plain Python/Pillow - no Tk - so it can be unit-tested on its own.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

log = logging.getLogger("EDMarketConnector.EDHud.curve")

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
except Exception:                       # Pillow missing: the overlay falls back to the flat panel
    Image = ImageChops = ImageDraw = ImageFilter = ImageFont = None


def available():
    return Image is not None


SUPERSAMPLE = 2          # draw/warp at 2x, then shrink: smooth edges on the slanted lines
MAX_YAW_DEG = 28.0       # panel turn at "strength 100"
EYE_DIST_FACTOR = 0.9    # viewer distance as a multiple of the screen width (smaller = stronger perspective)
MAX_BLUR_PX = 1.6        # blur radius (px) on the far edge at "softness 100"
NEAR_BLUR = 0.2          # the near edge keeps this fraction of the far-edge blur (the whole panel is a bit off focus)
SNAP_TOL = 3             # pixels this close to the transparency key are snapped to it exactly (see snap_to_key)


@dataclass(frozen=True)
class CurveParams:
    strength: int = 50       # 0-100: how far the panel is turned (100 = MAX_YAW_DEG)
    soft: int = 40           # 0-100: depth-of-field softness (100 = MAX_BLUR_PX on the far edge)
    side: int = 1            # +1 = panel on the right of the view (right edge nearer), -1 = on the left
    screen_w: int = 1920     # used for the perspective strength

    @property
    def yaw_deg(self):
        return max(0, min(100, int(self.strength))) / 100.0 * MAX_YAW_DEG * (1 if self.side >= 0 else -1)


# ---------------------------------------------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Geometry:
    size: tuple        # (w, h) of the warped picture, 1x pixels
    offset: tuple      # top-left of that picture relative to the flat panel's top-left
    quad: tuple        # the panel's 4 corners (TL, TR, BR, BL) inside the picture, 1x pixels
    far_left: bool     # True when the left edge is the far (softer) one


def geometry(w, h, params):
    """
    Where the flat w x h panel lands after being turned by params.yaw_deg about its own vertical centre line and
    viewed in perspective. The panel centre does not move, so switching the effect on/off does not shift the panel.
    """
    th = math.radians(params.yaw_deg)
    dist = max(200.0, EYE_DIST_FACTOR * max(200, params.screen_w))
    cx, cy = w / 2.0, h / 2.0
    pts = []
    for x, y in ((-cx, -cy), (cx, -cy), (cx, cy), (-cx, cy)):
        xr = x * math.cos(th)
        z = -x * math.sin(th)                    # positive yaw: the right edge comes towards the viewer
        s = dist / (dist + z)
        pts.append((xr * s + cx, y * s + cy))
    ox = math.floor(min(p[0] for p in pts)) - 1
    oy = math.floor(min(p[1] for p in pts)) - 1
    bw = int(math.ceil(max(p[0] for p in pts))) - ox + 1
    bh = int(math.ceil(max(p[1] for p in pts))) - oy + 1
    quad = tuple((px - ox, py - oy) for px, py in pts)
    return Geometry((bw, bh), (ox, oy), quad, th > 0)


def solve_perspective(dst, src):
    """
    Coefficients (a..h) for PIL's Image.transform(PERSPECTIVE): for each OUTPUT pixel (x, y) PIL samples the input at
        u = (a x + b y + c) / (g x + h y + 1),   v = (d x + e y + f) / (g x + h y + 1)
    `dst` / `src` are 4 (x, y) pairs that correspond to each other.
    """
    m, rhs = [], []
    for (x, y), (u, v) in zip(dst, src):
        m.append([x, y, 1, 0, 0, 0, -x * u, -y * u])
        rhs.append(u)
        m.append([0, 0, 0, x, y, 1, -x * v, -y * v])
        rhs.append(v)
    n = 8
    a = [row[:] + [r] for row, r in zip(m, rhs)]
    for col in range(n):                                           # Gaussian elimination, partial pivoting
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            raise ValueError("degenerate perspective quad")
        a[col], a[piv] = a[piv], a[col]
        for r in range(n):
            if r != col:
                f = a[r][col] / a[col][col]
                if f:
                    a[r] = [x - f * y for x, y in zip(a[r], a[col])]
    return tuple(a[i][n] / a[i][i] for i in range(n))


def hex_rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def pick_key(bg, border, preferred=(1, 1, 1)):
    """A transparency-key colour for the background window that differs from everything drawn on it."""
    for cand in (preferred, (255, 0, 255), (0, 255, 0)):
        if all(max(abs(c - o) for c, o in zip(cand, other)) > 4 * SNAP_TOL for other in (bg, border)):
            return cand
    return (255, 0, 255)


# ---------------------------------------------------------------------------------------------------------------
# drawing into a Pillow image with the same calls the overlay uses on a Tk canvas
# ---------------------------------------------------------------------------------------------------------------
_FONT_FILES = {   # Tk family -> (regular, bold) file names Pillow can find in the system font folders
    "consolas": (("consola.ttf",), ("consolab.ttf",)),
    "lucida console": (("lucon.ttf",), ("lucon.ttf",)),
    "courier new": (("cour.ttf",), ("courbd.ttf",)),
    "dejavu sans mono": (("DejaVuSansMono.ttf",), ("DejaVuSansMono-Bold.ttf",)),
    "menlo": (("Menlo.ttc", "/System/Library/Fonts/Menlo.ttc"), ("Menlo.ttc", "/System/Library/Fonts/Menlo.ttc")),
}
_FONT_FALLBACK = (("DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "cour.ttf", "consola.ttf"),
                  ("DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf", "courbd.ttf", "consolab.ttf"))


def load_font(family, bold, advance_px, scale):
    """
    Pillow font that matches a Tk monospace font: same family where a file can be found, sized so one character is
    `advance_px` * scale wide (the overlay lays everything out with Tk's measurements, so the glyphs must fit them).
    None if no usable font file exists.
    """
    reg, bld = _FONT_FILES.get((family or "").lower(), ((), ()))
    names = list(bld if bold else reg) + list(_FONT_FALLBACK[1 if bold else 0])
    for name in names:
        try:
            probe = ImageFont.truetype(name, 40)
            adv = probe.getlength("0")
            if adv <= 0:
                continue
            size = 40.0 * advance_px * scale / adv
            try:
                return ImageFont.truetype(name, size)               # fractional sizes need Pillow >= 10.1
            except (OSError, TypeError, ValueError):
                return ImageFont.truetype(name, max(1, int(round(size))))
        except OSError:
            continue
    return None


class PilCanvas:
    """
    The few tk.Canvas methods OverlayWindow._redraw uses (delete / create_line / create_rectangle / create_text /
    configure). Calls are recorded; rasterize() paints them once the panel size is known.
    """
    _ANCHOR = {"nw": "la", "ne": "ra", "center": "mm"}

    def __init__(self, fonts):
        self.fonts = fonts           # id(tk font object) -> Pillow font (already scaled)
        self.ops = []

    def delete(self, *_):
        self.ops.clear()

    def configure(self, **_):
        pass

    def create_line(self, x0, y0, x1, y1, fill="#fff", **_):
        self.ops.append(("line", (x0, y0, x1, y1), fill))

    def create_rectangle(self, x0, y0, x1, y1, outline="#000", fill="", **_):
        self.ops.append(("rect", (x0, y0, x1, y1), outline, fill))

    def create_text(self, x, y, text="", font=None, fill="#fff", anchor="center", **_):
        self.ops.append(("text", (x, y), text, font, fill, anchor))

    def rasterize(self, width, height, bg, scale, border=None, content=True, offset=(1, 1)):
        """
        RGB picture, `scale` times the panel size (width x height, which includes the 1px frame): background colour,
        then the recorded drawing (shifted by `offset` - the frame), then the frame in `border` if given.
        """
        S = scale
        im = Image.new("RGB", (width * S, height * S), bg)
        d = ImageDraw.Draw(im)
        ox, oy = offset
        for op in (self.ops if content else ()):
            kind = op[0]
            if kind == "line":
                x0, y0, x1, y1 = op[1]
                d.line([((x0 + ox) * S, (y0 + oy) * S + S / 2), ((x1 + ox + 1) * S, (y1 + oy) * S + S / 2)],
                       fill=op[2], width=S)
            elif kind == "rect":
                x0, y0, x1, y1 = op[1]
                d.rectangle([(x0 + ox) * S, (y0 + oy) * S, (x1 + ox + 1) * S - 1, (y1 + oy + 1) * S - 1],
                            fill=op[3] or None, outline=op[2] or None, width=S)
            else:
                (x, y), text, font, fill, anchor = op[1:]
                pf = self.fonts.get(id(font))
                if pf is not None and text:
                    d.text(((x + ox) * S, (y + oy) * S), text, font=pf, fill=fill,
                           anchor=self._ANCHOR.get(anchor, "la"))
        if border:
            d.rectangle([0, 0, width * S - 1, height * S - 1], outline=border, width=S)
        return im


# ---------------------------------------------------------------------------------------------------------------
# warp + depth of field
# ---------------------------------------------------------------------------------------------------------------
def _resample(name):
    ns = getattr(Image, "Resampling", Image)
    return getattr(ns, name)


def snap_to_key(img, key, tol=SNAP_TOL):
    """
    Pixels within `tol` of the transparency key become exactly the key. A colour key only hides exact matches, and
    resampling/blurring a flat area can land one level off - which would show as dark specks around the panel.
    """
    flat = Image.new("RGB", img.size, key)
    r, g, b = ImageChops.difference(img, flat).split()
    near = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v <= tol else 0)
    img.paste(flat, (0, 0), near)
    return img


def warp(src, geom, params, key, scale=SUPERSAMPLE):
    """
    `src`: RGB picture of the flat panel at `scale` x. Returns the warped, softened picture at 1x (geom.size),
    with everything outside the panel exactly `key`.
    """
    bw, bh = geom.size
    dst = [(x * scale, y * scale) for x, y in geom.quad]
    sw, sh = src.size
    coeffs = solve_perspective(dst, [(0, 0), (sw, 0), (sw, sh), (0, sh)])
    rgba = src.convert("RGBA")
    perspective = getattr(getattr(Image, "Transform", Image), "PERSPECTIVE")
    out = rgba.transform((bw * scale, bh * scale), perspective, coeffs, _resample("BICUBIC"),
                         fillcolor=tuple(key) + (0,))
    flat = Image.new("RGB", out.size, tuple(key))
    flat.paste(out.convert("RGB"), (0, 0), out.getchannel("A"))
    if params.soft > 0:
        flat = _depth_of_field(flat, geom, params, scale)
    flat = flat.resize((bw, bh), _resample("LANCZOS"))
    return snap_to_key(flat, tuple(key))


def _depth_of_field(img, geom, params, scale):
    """Blend towards a blurred copy: a little everywhere, most at the far edge (the nearer edge stays the sharpest)."""
    radius = MAX_BLUR_PX * max(0, min(100, params.soft)) / 100.0 * scale
    if radius <= 0:
        return img
    blurred = img.filter(ImageFilter.GaussianBlur(radius))
    w, h = img.size
    row = bytearray(w)
    for x in range(w):
        t = x / max(1, w - 1)                    # 0 = left edge ... 1 = right edge
        far = (1.0 - t) if geom.far_left else t
        row[x] = int(round(255 * (NEAR_BLUR + (1.0 - NEAR_BLUR) * far)))
    mask = Image.frombytes("L", (w, 1), bytes(row)).resize((w, h), _resample("NEAREST"))
    return Image.composite(blurred, img, mask)


def ppm_bytes(img):
    """Binary PPM of an RGB image: Tk's PhotoImage reads this natively (no PNG codec needed)."""
    return b"P6 %d %d 255\n" % img.size + img.tobytes()
