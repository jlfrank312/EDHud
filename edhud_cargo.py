"""
Cargo-bar helpers (pure Python, no Tk): per-commodity colours and a dynamic name shortener.

shorten_name(name, fits) squeezes a commodity name until `fits(text)` is true, so the label keeps as many
recognisable letters as the space allows:

    Land Enrichment Systems  ->  Land Enrichment Systems / Land.Enrich.Systm / ... / L.En.Syst / L.E.S
    Non-Lethal Weapons       ->  ... / Non.Leth.Weapo / ... / N.L.Weap / N.L.W
    Advanced Medicines       ->  ... / Adv.Med / A.M
"""
from __future__ import annotations

import colorsys
import re
import zlib

VOWELS = "aeiouAEIOU"
FILLER = {"of", "the", "and", "a", "an", "for"}
LADDER = (None, 7, 6, 5, 4, 3, 2, 1)          # progressively shorter per-word prefixes (None = whole word)

# Keyword (matched against the lower-case commodity name) -> colour. First match wins, so specific words go first.
COMMODITY_COLORS = [
    ("gold", "#f2c230"), ("silver", "#c9d1da"), ("platinum", "#9fd7ff"), ("palladium", "#b39ddb"),
    ("osmium", "#7aa2d6"), ("rhodium", "#d9b3ff"), ("tritium", "#6df2b0"), ("liquid oxygen", "#8fd8ff"),
    ("water", "#2ec4e6"), ("hydrogen", "#7fe0ff"), ("oxygen", "#8fd8ff"), ("biowaste", "#9acd32"),
    ("bertrandite", "#d98a5f"), ("bauxite", "#c9743f"), ("rutile", "#e0b07a"), ("indite", "#b5651d"),
    ("cobalt", "#4a7de0"), ("copper", "#d9822b"), ("aluminium", "#aab6c2"), ("beryllium", "#8fb08f"),
    ("titanium", "#8d99a6"), ("uranium", "#8be04e"), ("thorium", "#d6e04e"), ("lithium", "#e07a7a"),
    ("tantalum", "#7a8fb0"), ("gallium", "#8aa8c0"), ("indium", "#a0a8d0"), ("steel", "#9aa3ad"),
    ("limpet", "#ff9a1f"), ("drone", "#ff9a1f"),
    ("medicine", "#3fd9c8"), ("pharma", "#3fd9c8"), ("algae", "#74c23c"), ("fish", "#4cb7c9"), ("grain", "#d8c26a"),
    ("meat", "#d9645a"), ("fruit", "#e07a9a"), ("vegetable", "#7bd36b"), ("coffee", "#a67c52"), ("tea", "#9ccf6a"),
    ("food", "#d9b36a"), ("wine", "#b05fa0"), ("liquor", "#c97a3a"), ("beer", "#e0b04a"),
    ("weapon", "#ff5a4a"), ("explosive", "#ff7a3a"), ("battle", "#ff5a4a"), ("military", "#ff5a4a"),
    ("slave", "#c24a4a"), ("narcotic", "#c24a8a"), ("tobacco", "#a08060"), ("drug", "#c24a8a"),
    ("machinery", "#8aa0b8"), ("generator", "#f0a030"), ("power", "#f0a030"), ("battery", "#f0a030"),
    ("computer", "#6fa8ff"), ("technology", "#6fa8ff"), ("semiconductor", "#6fa8ff"), ("superconductor", "#a98bff"),
    ("robotic", "#8aa0b8"), ("scrap", "#8a8f94"), ("waste", "#9acd32"), ("polymer", "#c6a4e0"), ("plastic", "#c6a4e0"),
    ("textile", "#e0a4c6"), ("clothing", "#e0a4c6"), ("fabric", "#e0a4c6"), ("appliance", "#9fb4c7"),
    ("land enrichment", "#b0e060"), ("fertili", "#b0e060"), ("agronomic", "#8bd36b"), ("agri", "#8bd36b"),
    ("black box", "#e0e0e0"), ("salvage", "#c0c0c0"), ("survival", "#e0c070"), ("evacuation", "#e0c070"),
    ("reactive", "#d98a5f"), ("armour", "#d98a5f"), ("cell", "#9fe0c0"),     # also covers Progenitor Cells
    ("diamond", "#bff0ff"), ("opal", "#8ee8e0"), ("jade", "#5fd08a"),
    ("painite", "#e0507a"), ("alexandrite", "#9a7ae0"), ("benitoite", "#4a8aff"), ("musgravite", "#a05fd0"),
]
_overrides: dict[str, str] = {}


def set_overrides(mapping):
    """Replace user colour overrides ({'gold': '#ffcc00', ...}); invalid entries are ignored."""
    _overrides.clear()
    for k, v in (mapping or {}).items():
        if isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            _overrides[k.lower()] = v


def _hls(hexcol):
    r, g, b = (int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)


def _hex(h, l, s):
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _avoid_ui_green(h):
    """Keep cargo hues out of the HUD's own 'good' green (mission info) so the two never look alike."""
    deg = (h % 1.0) * 360
    if 118 <= deg <= 162:
        deg = 110 if deg < 140 else 178      # lime-green side or teal side, whichever is nearer
    return deg / 360


def color_for(key, name=""):
    """
    Colour for a commodity: user override, else keyword table, else a hue derived from the name. Items that merely
    contain a keyword (Basic Medicines, Advanced Medicines...) get their own shade of that colour, varied
    deterministically by name, so related commodities are related but never identical. Whole-word matches
    (Gold, Water) keep the table colour exactly.
    """
    nm = (name or key or "?").lower().strip()
    text = f"{key or ''} {name or ''}".lower()
    base, whole = None, False
    for table in (list(_overrides.items()), COMMODITY_COLORS):
        for needle, col in table:
            if needle in text:
                base, whole = col, nm == needle
                break
        if base:
            break
    crc = zlib.crc32(nm.encode())
    if base is None:
        h = _avoid_ui_green((crc % 360) / 360.0)
        return _hex(h, 0.62, 0.55)
    if whole:
        return base
    h, l, sat = _hls(base)
    h = _avoid_ui_green(h)
    u1, u2, u3 = [((crc >> n) & 0xFF) / 255.0 for n in (0, 8, 16)]
    return _hex(h + (u1 - 0.5) * (10 / 360), max(0.5, min(0.76, l + (u2 - 0.5) * 0.18)), max(0.3, min(1.0, sat + (u3 - 0.5) * 0.14)))


_known: list[str] = []          # every commodity name the game has (from FDevIDs/commodity.csv), for stable sibling slots


def set_known_names(names):
    """Tell the colour logic the full commodity list so a commodity's shade does not depend on what else is in the hold."""
    _known[:] = sorted({n for n in names if n}, key=str.lower)


def _match(key, name):
    """(keyword, base colour, whole-word?) for the first table entry that matches, else (None, None, False)."""
    text = f"{key or ''} {name or ''}".lower()
    nm = (name or key or "").lower().strip()
    for table in (list(_overrides.items()), COMMODITY_COLORS):
        for needle, col in table:
            if needle in text:
                return needle, col, nm == needle
    return None, None, False


def palette_for(items):
    """
    items: [(key, name), ...] in bar order -> colours, guaranteed pairwise different.
    Commodities that share a family keyword (Basic / Advanced / Agri- Medicines) are spread evenly across that
    family's hue so they stay clearly distinguishable: the slot comes from the commodity's place among all
    known commodities of the family, so its shade is the same whatever else is in the hold.
    """
    present = [str(n) for _, n in items]
    out, used = [], set()
    for key, name in items:
        needle, base, whole = _match(key, name)
        if base is None or whole:
            col = color_for(key, name)
        else:
            sibs = sorted({k for k in _known if needle in k.lower()} | {n for n in present if needle in n.lower()},
                          key=str.lower)
            if len(sibs) < 2 or str(name) not in sibs:
                col = color_for(key, name)
            else:
                i, n = sibs.index(str(name)), len(sibs)
                t = i / (n - 1)
                h, _l, sat = _hls(base)
                h = _avoid_ui_green(h) + (t - 0.5) * (20 / 360)           # +-10 degrees of hue across the family
                col = _hex(h, 0.50 + t * 0.24, max(0.55, min(1.0, sat)) * (1.0 if i % 2 == 0 else 0.72))
        n_try = 0
        while col in used and n_try < 40:
            n_try += 1
            h, l, sat = _hls(col)
            col = _hex(h, l + (0.03 if n_try % 2 else -0.03) * ((n_try + 1) // 2), sat)
        used.add(col)
        out.append(col)
    return out


def text_color_on(fill, dark="#06080c", light="#f4f6f8", avoid=None):
    """
    Readable text colour for a given fill (by luminance). `avoid` is the overlay's colour-key (its background
    colour): on Windows any pixel of exactly that colour in the text window is see-through, so if the chosen
    colour would equal it, it is nudged by one blue step (visually identical).
    """
    r, g, b = (int(fill[i:i + 2], 16) / 255 for i in (1, 3, 5))
    col = dark if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.5 else light
    if avoid and col.lower() == str(avoid).lower():
        blue = int(col[5:7], 16)
        col = col[:5] + "%02x" % (blue + 1 if blue < 255 else blue - 1)
    return col


def _cut(word, limit):
    """Prefix of at most `limit` letters, dropping a dangling vowel when that leaves a consonant stem (Powe->Pow)."""
    if limit is None or len(word) <= limit:
        return word
    pre = word[:limit]
    if len(pre) > 2 and pre[-1] in VOWELS and pre[-2] not in VOWELS:
        pre = pre[:-1]
    return pre


def shorten_name(name, fits):
    """
    Return the longest readable form of `name` that satisfies fits(text), or None if even initials do not fit.
    Words are shortened one ladder step at a time, always the currently longest word first, with the last word
    (usually the noun: Weapons, Generators) protected a little longer; joined with '.' once any word is cut.
    """
    name = (name or "").strip()
    if not name:
        return None
    if fits(name):
        return name
    words = [w for w in re.split(r"[\s\-/]+", name) if w]
    if len(words) > 1:
        kept = [w for w in words if w.lower() not in FILLER]
        words = kept or words
    full = " ".join(words)
    if full != name and fits(full):
        return full
    idx = [0] * len(words)

    def render():
        parts = [_cut(w, LADDER[i]) for w, i in zip(words, idx)]
        return " ".join(parts) if not any(idx) else ".".join(parts)

    def eff(i):
        return len(_cut(words[i], LADDER[idx[i]])) - (1.5 if len(words) > 1 and i == len(words) - 1 else 0)

    while True:
        movable = [i for i in range(len(words)) if idx[i] < len(LADDER) - 1]
        if not movable:
            break
        i = max(movable, key=lambda j: (eff(j), -j))
        before = _cut(words[i], LADDER[idx[i]])
        while idx[i] < len(LADDER) - 1:
            idx[i] += 1
            if _cut(words[i], LADDER[idx[i]]) != before:
                break
        text = render()
        if fits(text):
            return text
    return None
