"""
EDHud panels: turn a HudState snapshot into drawable lines.

A panel is just a function  render(snapshot, options) -> list[Line]
and an entry in PANELS. To add a new HUD element later (fuel, route, materials...)
write one more render function and register it - nothing else needs to change.

Line = {"left": [(text, style), ...], "right": [(text, style), ...]}
Styles: title head text dim good warn bad accent
"""
from __future__ import annotations

import time

CARGO_KINDS_UI = ("delivery", "collect", "mining", "salvage")

BAR_WIDTH = 8


def _line(left=(), right=(), **flags):
    """flags: bold=<bool> (default: only the first line), rule=True (draw the divider under this line)"""
    return {"left": list(left), "right": list(right), **flags}


def _fmt_remaining(seconds):
    if seconds is None:
        return "", "dim"
    if seconds <= 0:
        return "EXPIRED", "bad"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        txt = f"{d}d {h}h"
    elif h:
        txt = f"{h}h {m:02d}m"
    else:
        txt = f"{m}m"
    return txt, ("warn" if seconds < 2 * 3600 else "dim")


def _fmt_credits(n):
    """Payout in millions, e.g. 500000 -> '$0.5mill', 1450000 -> '$1.45mill'. Empty when there is no payout."""
    if not n:
        return ""
    txt = f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".")
    return f"${txt}mill"


def _bar(done, total):
    """Progress-bar segment; the overlay draws it as a rectangle ('bar:<fraction>')."""
    frac = 0.0 if not total else max(0.0, min(1.0, done / total))
    return f"bar:{frac:.3f}"


# ----------------------------------------------------------------------------
# Cargo panel
# ----------------------------------------------------------------------------
def cargo_bar_line(snap, combined=False):
    """One row: CARGO [--GOLD 42 TONS--][--SILVER 8--]        50/100 tons full (segments sized by share of capacity)."""
    items = sorted((i for i in snap["cargo_items"] if i["count"] > 0), key=lambda i: (-i["count"], i["name"]))
    spec = {"label": "CARGO", "total": snap["cargo_total"], "capacity": snap.get("capacity"),
            "items": [(i["name"], i["count"], i["mission_id"] is not None, i.get("key", "")) for i in items]}
    return {"left": [], "right": [], "cbar": spec, "rule_above": bool(combined)}


def render_cargo(snap, opts):
    """
    opts: cargo_style 'bar' (default) | 'list'; combined=True when appended under the missions panel
    (then the section gets its own divider above it instead of acting as the window's title row).
    """
    combined = bool(opts.get("combined"))
    if opts.get("cargo_style", "bar") == "bar":
        return [cargo_bar_line(snap, combined)]
    items = snap["cargo_items"]
    total, cap = snap["cargo_total"], snap.get("capacity")
    if cap:
        style = "bad" if total >= cap else ("warn" if total >= 0.9 * cap else "text")
        head_right = [(f"{total}/{cap} t", style)]
    else:
        head_right = [(f"{total} t", "text")]
    flags = {"bold": True, "rule": True, "rule_above": True} if combined else {}
    lines = [_line([("CARGO", "title")], head_right, **flags)]
    if not items:
        lines.append(_line([("Hold empty", "dim")]))
        return lines

    rows = sorted(items, key=lambda i: (i["mission_id"] is not None, -i["count"], i["name"]))
    limit = opts.get("max_cargo_rows", 12)
    for it in rows[:limit]:
        left = [(it["name"], "accent" if it["mission_id"] is not None else "text")]
        if it["mission_id"] is not None:
            left.append(("  mission", "dim"))
        if it["stolen"]:
            left.append(("  stolen", "bad"))
        lines.append(_line(left, [(str(it["count"]), "text")]))
    if len(rows) > limit:
        lines.append(_line([(f"+{len(rows) - limit} more", "dim")]))
    return lines


# ----------------------------------------------------------------------------
# Missions panel (single, compact one-line layout)
#   same system:  [{source system} - {destination station}] - {Verb} {Commodity}, {N} tons / [{x} tons in cargo, {y} tons remaining]
#   other system: [{source system} - {source station}] - {task} to {destination system} - {destination station}
#   (the destination part is drawn in the "dest" colour)
# ----------------------------------------------------------------------------
def _verb(m):
    n = (m.get("name") or "").lower()
    if "altruism" in n:
        return "Donate"
    if "mining" in n:
        return "Mine"
    if "collect" in n:
        return "Collect"
    return "Deliver"


def _cargo_tail(snap, m):
    total = m["total_needed"]
    remaining = max(0, total - m["delivered_n"])
    if remaining == 0:
        return [(" / ", "text"), ("[all delivered - collect reward]", "good")]
    # m["hold"] is this mission's share of the hold (snapshot() splits shared market cargo between stacked
    # missions for the same commodity); fall back to the raw total only when no share was computed.
    in_cargo = m["hold"] if m.get("hold") is not None else snap["cargo_by_key"].get(m["commodity"], 0)
    parts = [f"{in_cargo} tons in cargo"]
    if m["delivered_n"]:
        parts.append(f"{remaining} tons remaining")
    return [(" / ", "text"), ("[" + ", ".join(parts) + "]", "good" if in_cargo >= remaining else "warn")]


def _destination(m):
    """(system, station) the player has to travel to for this mission's task."""
    if m["kind"] == "assassinate" and not m.get("redirected"):
        return m.get("dest_system"), m.get("dest_station")     # where the target is (turn-in is back at the source)
    return m.get("target_system"), m.get("target_station")


def _expiry(m):
    exp_txt, exp_style = _fmt_remaining(m.get("expires_in"))
    if m.get("redirected") and m["kind"] in ("massacre", "assassinate", "piracy"):
        exp_txt, exp_style = "TURN IN", "good"       # kills done: the game's timer reads 0 until you hand it in
    return exp_txt, exp_style


def _mission_line_compact(snap, m, pw=0, ew=0):
    """pw / ew: column widths for payout and expiry, so the right-hand columns line up on every row."""
    exp_txt, exp_style = _expiry(m)
    if not m["accepted"]:                            # login-time stub whose details are not in any journal we have
        return _line([("[details unavailable] - ", "dim"), (m["title"] or m["name"] or "Mission", "dim")],
                     [(" " * (pw + 2) + exp_txt.rjust(ew), exp_style)] if (exp_txt or pw or ew) else [])
    osys, ostn = m.get("origin_system") or "?", m.get("origin_station")
    dsys, dstn = _destination(m)
    cargo = m["kind"] in CARGO_KINDS_UI and m.get("total_needed")
    away = bool(dsys and m.get("origin_system") and dsys != m.get("origin_system")) and m["kind"] != "massacre"
    dest = " - ".join(x for x in (dsys, dstn) if x) or "?"
    if m["kind"] == "scan":
        dest = dsys or "?"           # the target settlement is found by scanning; only the system is useful

    if away:
        src = f"[{osys} - {ostn}]" if ostn else f"[{osys}]"
        left = [(src, "accent"), (" - ", "dim")]
        if cargo:
            name = m["commodity_name"] or m["title"]
            left += [(f"{_verb(m)} {name}, {m['total_needed']} tons to ", "text"), (dest, "dest")]
            left += _cargo_tail(snap, m)
        elif m["kind"] == "courier":
            left += [("Courier Job to ", "text"), (dest, "dest")]
        elif m["kind"] == "assassinate":
            who = m.get("target_type") or m.get("target_faction") or "target"
            left += [(f"Assassinate {who} at ", "text"), (dest, "dest")]
        else:
            left += [((m["title"] or m["name"] or "Mission") + " to ", "text"), (dest, "dest")]
    else:
        place = (m.get("target_station") or m.get("target_system") or "?")
        if ostn and not cargo:
            place = ostn                      # combat and other non-cargo missions are handled where they were accepted
        left = [(f"[{osys} - {place}]", "accent"), (" - ", "dim")]
        if cargo:
            name = m["commodity_name"] or m["title"]
            left += [(f"{_verb(m)} {name}, {m['total_needed']} tons", "text")] + _cargo_tail(snap, m)
        elif m["kind"] == "massacre":
            ksys = m.get("kill_system") or m.get("dest_system")
            where = f" in {ksys}" if ksys else ""
            left += [(f"Massacre {m['kill_count']} {m['target_faction'] or 'targets'}{where}", "text")]
            done = min(m.get("kills", 0), m["kill_count"])
            left += [(" ", "text"), (f"[{done}/{m['kill_count']} fulfilled]", "good" if done >= m["kill_count"] else "warn")]
        elif m["kind"] == "assassinate":
            who = m.get("target_type") or m.get("target_faction") or "target"
            left += [(f"Assassinate {who}", "text")]
        elif m["kind"] == "courier":
            left += [("Courier Job", "text")]
        else:
            left += [(m["title"] or m["name"] or "Mission", "text")]
    if m.get("wing"):
        left.append(("  \u25b2", "accent"))      # wing mission (shareable with / shared from a wing)
    cr = _fmt_credits(m.get("reward"))
    right = [(cr.rjust(pw) + "  ", "pay"), (exp_txt.rjust(ew), exp_style)] if (pw or ew) else []
    return _line(left, right)


def _order(ms):
    """
    First come, first served. Deliveries (any cargo kind) to the same destination are grouped together;
    each group sits where its earliest-accepted contract is. Every other mission is its own group at the
    position it was accepted. Unrecovered stubs go last.
    """
    def accepted(m):
        return m.get("accepted_at") or 9e18

    def gkey(m):
        if m["kind"] in CARGO_KINDS_UI and m.get("total_needed") and (m.get("target_system") or m.get("target_station")):
            return ("dest", m.get("target_system"), m.get("target_station"))
        return ("one", m["id"])

    groups = {}
    for m in ms:
        groups.setdefault(gkey(m), []).append(m)
    for members in groups.values():
        members.sort(key=lambda m: (accepted(m), m["id"]))
    ordered = sorted(groups.values(), key=lambda g: (not g[0]["accepted"], g[0].get("accepted_at") or 9e18, g[0]["id"]))
    return [m for g in ordered for m in g]


def render_missions(snap, opts):
    ms = [m for m in snap["missions"] if m["accepted"] or (m.get("expires_in") or 1) > 0]   # drop expired stubs
    ms = _order(ms)
    lines = [_line([("MISSIONS", "title")], [(f"{len(ms)} active", "dim")])]
    if not ms:
        lines.append(_line([("No active missions", "dim")]))
        return lines
    limit = opts.get("max_mission_rows", 16)
    shown = ms[:limit]
    pw = max((len(_fmt_credits(m.get("reward"))) for m in shown), default=0)
    ew = max((len(_expiry(m)[0]) for m in shown), default=0)
    for m in shown:
        lines.append(_mission_line_compact(snap, m, pw, ew))
    if len(ms) > limit:
        lines.append(_line([(f"+{len(ms) - limit} more missions", "dim")]))
    return lines


render_missions_compact = render_missions      # old name kept for scripts


# ----------------------------------------------------------------------------
# Registry - add new panels here
# ----------------------------------------------------------------------------
PANELS = [
    {"id": "cargo", "label": "Cargo", "render": render_cargo, "default_x": 40, "default_y": 120},
    {"id": "missions", "label": "Missions", "render": render_missions, "default_x": 40, "default_y": 380},
]


# ----------------------------------------------------------------------------
# Demo data (for positioning the overlay without being in game)
# ----------------------------------------------------------------------------
def demo_snapshot():
    now = time.time()
    base = dict(accepted=True, faction="Demo Faction", wing=False, kill_count=0, target_faction="",
                target_type="", passengers=0, reward=0, redirected=False, origin_system="Sol",
                origin_station="Daedalus", dest_system=None, dest_station=None, name="Mission_Delivery",
                title="Delivery", kind="delivery", commodity="gold", commodity_name="Gold", count=0,
                collected=None, delivered=0, total=None, total_needed=0, delivered_n=0, needed=0,
                hold=0, ready=False, here=False)
    ms = [
        dict(base, id=1, commodity_name="Gold", total_needed=20, delivered_n=8, needed=12, hold=12, ready=True,
             role="deliver", target_system="Shinrarta Dezhra", target_station="Jameson Memorial",
             reward=1_450_000, expires_in=2 * 86400 + 3 * 3600, expiry=now + 2 * 86400),
        dict(base, id=2, commodity_name="Beryllium", total_needed=64, delivered_n=0, needed=64, hold=40, ready=False,
             role="deliver", target_system="Shinrarta Dezhra", target_station="Jameson Memorial",
             reward=820_000, expires_in=5 * 3600, expiry=now + 5 * 3600),
        dict(base, id=3, commodity_name="Biowaste", total_needed=800, delivered_n=800, needed=0, hold=0, ready=True,
             role="collect", target_system="Shinrarta Dezhra", target_station="Jameson Memorial", here=False,
             reward=3_100_000, expires_in=1800, expiry=now + 1800),
        dict(base, id=4, kind="massacre", name="Mission_Massacre", title="Massacre", kill_count=24,
             target_faction="Pirate Lords", role="turn_in", target_system="Sol", target_station="Daedalus",
             here=True, expires_in=3 * 86400, expiry=now + 3 * 86400),
    ]
    items = [
        {"key": "gold", "name": "Gold", "count": 12, "mission_id": None, "stolen": 0},
        {"key": "beryllium", "name": "Beryllium", "count": 40, "mission_id": None, "stolen": 0},
        {"key": "limpet", "name": "Limpet", "count": 4, "mission_id": None, "stolen": 0},
        {"key": "biowaste", "name": "Biowaste", "count": 0, "mission_id": 3, "stolen": 0},
    ][:3]
    return {"cmdr": "DEMO", "system": "Sol", "station": "Daedalus", "docked": True, "capacity": 128,
            "cargo_total": 56, "cargo_items": items, "cargo_by_key": {}, "missions": ms, "status": {},
            "cargo_source": "demo", "version": -1}
