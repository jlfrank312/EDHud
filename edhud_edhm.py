"""EDHM theme matching for EDHud (palette builder).

Settings model (user-specified):
  match_theme       default OFF -> EDHud keeps its v0.1.4 colours (built-in COLORS + edhud_theme.json)
  keep_state_colors default ON  -> only used when match_theme is ON; keeps EDHud's own
                                   good/warn/bad/dest (mission/cargo STATE colours) instead of
                                   re-colouring them from the theme.

Facts this relies on (validated against real EDHM v22.03 `EDHM-ini` folders):
  * INI colours are LINEAR floats; the standard sRGB curve converts them to the 8-bit values
    EDHM UI shows (153/153 exact vs ThemeSettings.json).
  * Startup-Profile.ini mode per element: 100/101 = custom colour from Advanced.ini,
    199 = XML colour matrix, 200 = Elite default (theme does not override).
  * EDHud's palette is the COLORS dict in edhud_overlay.py: title head text dim good warn bad
    accent dest bg border.  `bg` is also the text window's transparent colour key, so it is
    never themed.
"""
import json
import os
import re

PALETTE_KEYS = ("title", "head", "text", "dim", "good", "warn", "bad", "accent", "dest", "pay", "bg", "border")
STATE_KEYS = ("good", "warn", "bad", "dest")

# role -> (mode_file, mode_key, color_file, (r_key, g_key, b_key)); keys are EDHM v22.03.
ROLE_SOURCES = {
    "title":  ("Startup-Profile", "x75",  "Advanced", ("x76", "y76", "z76")),     # Side Panels / Main Lines
    "text":   ("Startup-Profile", "w75",  "Advanced", ("x77", "y77", "z77")),     # HUD Settings / Main Text
    "accent": ("Startup-Profile", "y137", "Advanced", ("x233", "y233", "z233")),  # Chat Panel / Icons
}
TESTED_VERSIONS = {"22.03"}
WATCHED = ("Advanced.ini", "Startup-Profile.ini", "XML-Profile.ini", "ThemeSettings.json")
THEMED = ("custom", "xml-matrix")

# Elite's stock HUD orange, per the EDHM wiki ("official Elite orange is RGB(1, 0.4352, 0)").
# ASSUMPTION: matrix is applied in display (sRGB-encoded) space; not calibrated against renders.
ELITE_ORANGE = (1.0, 0.4352, 0.0)
MATRIX_KEYS = (("x150", "y150", "z150"), ("x151", "y151", "z151"), ("x152", "y152", "z152"))

# Hand-set tunables (not derived): a theme is "monochrome" if title is within MONO_DISTANCE
# (sRGB euclidean, 0..441) of text; text then steps MONO_TEXT_STEP toward bg so title/head stay on top.
MONO_DISTANCE = 25
MONO_TEXT_STEP = 0.15
# Minimum contrast ratio vs bg for text-bearing roles (WCAG: 4.5 normal text, 3 large/bold text).
MIN_CONTRAST = {"text": 4.5, "title": 3.0, "accent": 3.0, "dim": 3.0}

# When state colours are NOT preserved, state styles take theme colours (PROVISIONAL: in a
# monochrome theme the distinction needs emphasis cues - bold/prefix - that panels don't have yet).
STATE_FROM_THEME = {"good": "head", "warn": "title", "bad": "title", "dest": "accent"}
# Payout column: its own style (not mission state). Default = EDHud's current green (base["pay"]);
# with the match_payout switch (and theme matching ON) it takes this theme role instead.
PAYOUT_FROM_THEME = "head"

# Default EDHM install locations (EDHM UI wiki); a manual path overrides these.
_PRODUCTS = (
    r"C:\Program Files (x86)\Steam\steamapps\common\Elite Dangerous\Products",
    r"C:\Program Files\Epic Games\EliteDangerous\Products",
    r"C:\Program Files (x86)\Frontier\Products",
)
_ODYSSEY_DIRS = ("elite-dangerous-odyssey-64", "FORC-FDEV-DO-1000")
DEFAULT_CANDIDATES = tuple(os.path.join(p, d, "EDHM-ini") for p in _PRODUCTS for d in _ODYSSEY_DIRS)

_KV = re.compile(r"^\s*([xyzw]\d+)\s*=\s*([-+0-9.eE]+)")


# ---- colour maths --------------------------------------------------------------------
def srgb_to_linear(c8):
    c = c8 / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb8(lin):
    lin = min(1.0, max(0.0, lin))
    c = lin * 12.92 if lin <= 0.0031308 else 1.055 * lin ** (1 / 2.4) - 0.055
    return int(round(c * 255))


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    """t=0 -> a, t=1 -> b (plain sRGB blend; fine for UI chrome)."""
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ra, rb))


def color_distance(a, b):
    return sum((x - y) ** 2 for x, y in zip(_rgb(a), _rgb(b))) ** 0.5


def luminance(h):
    r, g, b = (srgb_to_linear(c) for c in _rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def ensure_contrast(h, bg, ratio):
    """Return h unchanged if it already has `ratio`:1 against bg; else the least-white blend that does."""
    if contrast(h, bg) >= ratio:
        return h
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2
        if contrast(mix(h, "#ffffff", mid), bg) >= ratio:
            hi = mid
        else:
            lo = mid
    return mix(h, "#ffffff", hi)


def apply_matrix(matrix, base=ELITE_ORANGE):
    """out = r*MatrixRed + g*MatrixGreen + b*MatrixBlue per channel, clamped to 0..1.
    (EDHM wiki: [Custom RGB] * [Image RGB] = [Output RGB]; red row = colour of the red channel.)"""
    out = []
    for c in range(3):
        v = sum(base[row] * matrix[row][c] for row in range(3))
        out.append(min(1.0, max(0.0, v)))
    return "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in out)


# ---- reading the EDHM-ini folder -------------------------------------------------------
def read_ini(path):
    """Flat {key: float}.  Later duplicates win (matches how 3DMigoto reads them)."""
    out = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _KV.match(line.split(";", 1)[0])
            if m:
                out[m.group(1)] = float(m.group(2))
    return out


def clean_manual_path(raw):
    """Strip whitespace/quotes/trailing separators from a pasted path."""
    p = (raw or "").strip().strip('"').strip("'").strip()
    return (p.rstrip("\\/") or p) if p else ""


def manual_options(raw, pathmod=os.path):
    """Folders to try for a pasted path, in order: the path as given (it may be the EDHM-ini folder,
    even a renamed copy), then its EDHM-ini subfolder (when the game's product folder was pasted)."""
    p = clean_manual_path(raw)
    if not p:
        return []
    out = [p]
    if pathmod.basename(p).lower() != "edhm-ini":
        out.append(pathmod.join(p, "EDHM-ini"))
    return out


def locate(manual=None, candidates=DEFAULT_CANDIDATES):
    """-> (ini_dir or None, note).  note is None on success from a manual path, else a short
    message for the settings label.  A bad manual path is reported, never silently replaced."""
    if manual and manual.strip():
        d = find_ini_dir(manual, candidates)
        if d:
            return d, None
        return None, "Not an EDHM-ini folder: %s" % clean_manual_path(manual)
    d = find_ini_dir(None, candidates)
    if d:
        return d, "Auto-detected: %s" % d
    return None, "EDHM-ini not found - enter its path in settings"


def find_ini_dir(manual=None, candidates=DEFAULT_CANDIDATES):
    """Manual path wins; else the most recently modified candidate that looks like EDHM-ini."""
    def ok(d):
        return bool(d) and os.path.isfile(os.path.join(d, "Startup-Profile.ini")) \
            and os.path.isfile(os.path.join(d, "Advanced.ini"))
    if manual and manual.strip():
        return next((m for m in manual_options(manual) if ok(m)), None)
    found = [d for d in candidates if ok(d)]
    if not found:
        return None
    return max(found, key=lambda d: os.stat(os.path.join(d, "Startup-Profile.ini")).st_mtime)


def theme_stamp(ini_dir):
    """Cheap change detector for the 0.5 s tick (F11 in-game reloads rewrite these)."""
    stamp = []
    for name in WATCHED:
        try:
            st = os.stat(os.path.join(ini_dir, name))
            stamp.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append((name, None, None))
    return tuple(stamp)


def read_matrix(files):
    x = files.get("XML-Profile", {})
    if not all(k in x for row in MATRIX_KEYS for k in row):
        return None
    return [[x[k] for k in row] for row in MATRIX_KEYS]


def resolve_role(role, files):
    """-> (hex or None, reason).  reason is 'custom'/'xml-matrix' or why the theme does not supply it."""
    mfile, mkey, cfile, ckeys = ROLE_SOURCES[role]
    mode = files.get(mfile, {}).get(mkey)
    if mode is None:
        return None, "mode-missing"
    mode = int(mode)
    if mode == 199:
        m = read_matrix(files)
        return (apply_matrix(m), "xml-matrix") if m else (None, "xml-matrix-missing")
    if mode == 200:
        return None, "elite-default"
    if mode not in (100, 101):
        return None, "unknown-mode-%d" % mode
    cvals = files.get(cfile, {})
    if not all(k in cvals for k in ckeys):
        return None, "color-missing"
    rgb = [linear_to_srgb8(cvals[k]) for k in ckeys]
    return "#%02x%02x%02x" % tuple(rgb), "custom"


# ---- the one function the app calls -------------------------------------------------------
def build_palette(base, ini_dir=None, match_theme=False, keep_state_colors=True, match_payout=False):
    """Return (palette, report).  `base` = EDHud's v0.1.4 palette (COLORS after edhud_theme.json).

    palette always has exactly PALETTE_KEYS, each '#rrggbb', so it can be applied to COLORS.
    match_theme False (the default), no folder, or any read problem -> palette == base.
    match_payout (only with match_theme) moves the payout column's `pay` style to the theme;
    otherwise `pay` stays EDHud's own (default: its current green).
    """
    base = dict(base)
    base.setdefault("pay", base["good"])  # older base without a pay style: payout stayed `good`
    report = {"matched": False, "theme": None, "author": None, "version": None,
              "monochrome": False, "sources": {}, "lifted": [], "warnings": [], "error": None}
    pal = {k: base[k] for k in PALETTE_KEYS}
    if not match_theme:
        return pal, report
    if not ini_dir:
        report["error"] = "EDHM-ini folder not found"
        return pal, report
    files = {}
    try:
        for stem in ("Advanced", "Startup-Profile"):
            files[stem] = read_ini(os.path.join(ini_dir, stem + ".ini"))
    except OSError as exc:
        report["error"] = "cannot read EDHM ini: %s" % exc
        return pal, report
    try:  # optional: only mode-199 roles need it
        files["XML-Profile"] = read_ini(os.path.join(ini_dir, "XML-Profile.ini"))
    except OSError:
        files["XML-Profile"] = {}
    try:
        with open(os.path.join(ini_dir, "ThemeSettings.json"), encoding="utf-8-sig") as fh:
            ts = json.load(fh)
        cred = ts.get("credits") or {}
        report.update(theme=cred.get("theme"), author=cred.get("author"), version=ts.get("version"))
    except (OSError, ValueError):
        report["warnings"].append("ThemeSettings.json unreadable (theme name unknown)")
    if report["version"] not in TESTED_VERSIONS:
        report["warnings"].append("EDHM version %r not validated; key map may be off" % report["version"])

    for role in ROLE_SOURCES:
        hexv, why = resolve_role(role, files)
        report["sources"][role] = why
        if hexv:
            pal[role] = hexv
    # The theme's design wins: a role the theme does not supply takes the theme's TEXT colour,
    # never EDHud's static colour, so no hue is introduced that the theme does not contain.
    raw_text = pal["text"]
    for role in ("title", "accent"):
        if report["sources"][role] not in THEMED:
            pal[role] = raw_text
    # Monochrome themes keep hierarchy through brightness tiers instead of hue.
    mono = color_distance(pal["title"], raw_text) < MONO_DISTANCE
    if mono:
        pal["text"] = mix(raw_text, base["bg"], MONO_TEXT_STEP)
    report["monochrome"] = mono
    bg = base["bg"]  # transparent colour key of the text window: never themed
    raw_title = pal["title"]
    pal["head"] = mix(raw_text, "#ffffff", 0.35)
    pal["dim"] = mix(pal["text"], bg, 0.50)
    pal["border"] = mix(raw_title, bg, 0.45)  # lines/outlines: the theme's own line colour
    # Legibility floor (only acts when a themed colour is too dark for text on EDHud's dark bg):
    # lighten toward white by the smallest amount that reaches the ratio, keeping the hue.
    lifted = []
    for role, floor in (("text", MIN_CONTRAST["text"]), ("title", MIN_CONTRAST["title"]),
                        ("accent", MIN_CONTRAST["accent"]), ("dim", MIN_CONTRAST["dim"])):
        new = ensure_contrast(pal[role], bg, floor)
        if new != pal[role]:
            lifted.append(role)
            pal[role] = new
    report["lifted"] = lifted
    pal["bg"] = bg
    if keep_state_colors:
        for k in STATE_KEYS:
            pal[k] = base[k]
    else:
        for k, src in STATE_FROM_THEME.items():
            pal[k] = pal[src]
    pal["pay"] = pal[PAYOUT_FROM_THEME] if match_payout else base["pay"]
    report["matched"] = True
    return pal, report
