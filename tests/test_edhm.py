"""Tests for EDHM theme matching. Run from the plugin folder:  python3 tests/test_edhm.py
Uses small synthetic EDHM-ini folders, so no game files are needed."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import edhud_edhm as E

BASE = {"title": "#e8821e", "head": "#d5d5d5", "text": "#cccccc", "dim": "#7f8590", "good": "#4cff8f",
        "warn": "#ffd24a", "bad": "#ff5a4a", "accent": "#14d7e9", "dest": "#ff4d4d", "pay": "#4cff8f",
        "bg": "#08090e", "border": "#0e6f7a"}
fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def lin(c8):
    return E.srgb_to_linear(c8)


def write_theme(d, title=None, text=None, accent=None, matrix=None):
    """title/text/accent: '#rrggbb' custom colour (mode 100) or None (mode 200)."""
    adv, st = [], []
    for role, hexv, (mk, mc) in (("title", title, ("x75", ("x76", "y76", "z76"))),
                                 ("text", text, ("w75", ("x77", "y77", "z77"))),
                                 ("accent", accent, ("y137", ("x233", "y233", "z233")))):
        st.append("%s = %d" % (mk, 100 if hexv else (199 if matrix else 200)))
        if hexv:
            for k, i in zip(mc, (1, 3, 5)):
                adv.append("%s = %.6f" % (k, lin(int(hexv[i:i + 2], 16))))
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "Advanced.ini"), "w").write("[Constants]\n" + "\n".join(adv) + "\n")
    open(os.path.join(d, "Startup-Profile.ini"), "w").write("[Constants]\n" + "\n".join(st) + "\n")
    if matrix:
        keys = ("x150", "y150", "z150", "x151", "y151", "z151", "x152", "y152", "z152")
        open(os.path.join(d, "XML-Profile.ini"), "w").write("[Constants]\n" + "\n".join(
            "%s = %s" % (k, v) for k, v in zip(keys, matrix)) + "\n")


with tempfile.TemporaryDirectory() as tmp:
    d = os.path.join(tmp, "Products", "elite-dangerous-odyssey-64", "EDHM-ini")
    write_theme(d, title="#ffd0ff", text="#98ffff", accent="#a8dbff")

    # --- toggle semantics
    pal, rep = E.build_palette(BASE, d, match_theme=False)
    check("match OFF returns the base palette exactly", pal == BASE)
    pal, rep = E.build_palette(BASE, d, match_theme=True)
    check("12 palette keys, all #rrggbb", set(pal) == set(E.PALETTE_KEYS) and all(len(v) == 7 and v[0] == "#" for v in pal.values()))
    check("bg never themed", pal["bg"] == BASE["bg"])
    check("title/text/accent follow the theme", pal["title"] == "#ffd0ff" and pal["text"] == "#98ffff" and pal["accent"] == "#a8dbff")
    check("state colours kept by default", all(pal[k] == BASE[k] for k in E.STATE_KEYS))
    check("payout stays green by default", pal["pay"] == BASE["pay"])
    pal2, _ = E.build_palette(BASE, d, match_theme=True, match_payout=True)
    check("payout switch -> theme head colour", pal2["pay"] == pal2["head"] != BASE["pay"])
    pal3, _ = E.build_palette(BASE, d, match_theme=True, keep_state_colors=False)
    check("state colours themed when keep is off", any(pal3[k] != BASE[k] for k in E.STATE_KEYS))
    pal4, _ = E.build_palette(BASE, d, match_theme=True, match_payout=True)
    off, _ = E.build_palette(BASE, d, match_theme=False, match_payout=True)
    check("payout switch inert while matching is off", off == BASE)
    check("OFF again restores base", E.build_palette(BASE, d, match_theme=False)[0] == BASE)
    check("no folder falls back to base", E.build_palette(BASE, None, match_theme=True)[0] == BASE)
    check("legible on bg", all(E.contrast(pal[k], pal["bg"]) >= 3.0 for k in ("title", "text", "accent", "dim")))

    # --- unsupplied roles take the theme's text colour; matrix; monochrome
    d2 = os.path.join(tmp, "b", "EDHM-ini")
    write_theme(d2, text="#88ccff")
    p, r = E.build_palette(BASE, d2, match_theme=True)
    check("unsupplied title/accent -> theme text colour", p["accent"] == "#88ccff" or r["monochrome"])
    d3 = os.path.join(tmp, "c", "EDHM-ini")
    write_theme(d3, matrix=["1"] * 9)
    p, r = E.build_palette(BASE, d3, match_theme=True)
    check("all-ones matrix gives a white monochrome theme", r["monochrome"] and p["title"] == "#ffffff" and p["text"] != "#ffffff")
    check("matrix: identity = stock orange", E.apply_matrix([[1, 0, 0], [0, 1, 0], [0, 0, 1]]) == "#ff6f00")

    # --- path handling
    check("manual_options: as given then EDHM-ini", E.manual_options('"%s"' % os.path.dirname(d)) == [os.path.dirname(d), os.path.join(os.path.dirname(d), "EDHM-ini")])
    check("locate: exact folder", E.locate(d)[0] == d)
    check("locate: product folder -> EDHM-ini subfolder", E.locate(os.path.dirname(d))[0] == d)
    ren = os.path.join(tmp, "renamed copy")
    write_theme(ren, title="#ffffff", text="#ffffff")
    check("locate: renamed copy works", E.locate(ren)[0] == ren)
    bad, note = E.locate(os.path.join(tmp, "nope"))
    check("locate: bad manual path reported, not replaced", bad is None and note.startswith("Not an EDHM-ini folder"))
    check("locate: blank + no candidates -> hint", E.locate("", candidates=())[1].startswith("EDHM-ini not found"))
    check("locate: blank auto-detects from candidates", E.locate("", candidates=(d,))[0] == d)
    s1 = E.theme_stamp(d)
    os.utime(os.path.join(d, "Advanced.ini"), (1, 1))
    check("theme_stamp changes when Advanced.ini does", E.theme_stamp(d) != s1)

# --- integration with the real modules (needs tkinter importable; no window is created)
try:
    import edhud_overlay as O
    import edhud_panels as P
except Exception as exc:                                   # pragma: no cover
    print("SKIP overlay/panels integration (%s)" % exc)
else:
    check("COLORS has pay, default green", O.COLORS["pay"] == "#4cff8f")
    check("COLORS keys == palette keys", set(O.COLORS) == set(E.PALETTE_KEYS))
    O.snapshot_base()
    base = dict(O.BASE_COLORS)
    v0 = O._palette_version
    O.apply_palette({"title": "#123456", "bg": "#ffffff", "bogus": "#000000"})
    check("apply_palette sets colours, never bg, ignores unknown keys", O.COLORS["title"] == "#123456" and O.COLORS["bg"] == base["bg"] and "bogus" not in O.COLORS)
    check("apply_palette bumps the redraw version", O._palette_version == v0 + 1)
    O.apply_palette(base)
    check("restoring the base palette restores COLORS", O.COLORS == base)
    snap = {"missions": [], "cargo": [], "cargo_total": 0, "capacity": 0, "version": 1}
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "edhud_panels.py")).read()
    check("payout segment uses the pay style", '"  ", "pay")' in src and '"  ", "good")' not in src)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
