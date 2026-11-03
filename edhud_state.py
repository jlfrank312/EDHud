"""
EDHud game-state tracker.

Pure Python (no tkinter, no EDMC imports) so it can be unit-tested headless.

It reduces Elite Dangerous journal events into:
  * cargo   - what is in the hold (from Cargo.json, which carries per-mission info)
  * missions - every active mission, where to deliver / turn in, and progress

Design notes (the reasons the previous attempt failed):
  * EDMC does NOT hand plugins the journal entries that were written before EDMC
    started. We therefore replay the current journal ourselves (replay_journal),
    and back-fill mission details from older journals when needed.
  * Mission details only exist in the MissionAccepted event. The "Missions" event
    written at login has only id/name/expiry, so details are persisted to disk.
  * All handlers are idempotent so replay + live events can overlap safely.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("EDMarketConnector.EDHud.state")

STATE_FILE_VERSION = 1


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def canon(name):
    """'$BasicMedicines_Name;' / 'BasicMedicines' / 'basicmedicines' -> 'basicmedicines'"""
    if not name:
        return ""
    s = str(name).strip().lower()
    if s.startswith("$"):
        s = s[1:]
    if s.endswith(";"):
        s = s[:-1]
    if s.endswith("_name"):
        s = s[:-5]
    return s


COMMODITY_NAMES = {}      # canonical symbol -> display name, from EDMC's FDevIDs/commodity.csv


def load_commodity_names(path):
    """Load EDCD's symbol->name table (columns: id,symbol,category,name). Returns number of entries."""
    import csv
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("symbol") and row.get("name"):
                    COMMODITY_NAMES[canon(row["symbol"])] = row["name"]
    except (OSError, csv.Error):
        log.warning("commodity name table not readable: %s", path)
    return len(COMMODITY_NAMES)


def pretty(name):
    """Display name when the journal gives no *_Localised field: name table first, then a tidy-up of the symbol."""
    s = canon(name)
    if not s:
        return "?"
    return COMMODITY_NAMES.get(s) or re.sub(r"[_]+", " ", s).title()


def parse_ts(value):
    """Journal timestamp ('2026-11-02T23:53:11Z') -> epoch seconds (UTC) or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def pretty_mission(internal_name):
    """'Mission_Delivery_Boom_name' -> 'Delivery Boom' (only used when no localised title is known)."""
    s = re.sub(r"^mission_|_name$", "", (internal_name or ""), flags=re.I)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)            # MassacreWing -> Massacre Wing, CivilLiberty -> Civil Liberty
    return re.sub(r"_+", " ", s).strip().title() or "Mission"


def mission_kind(internal_name):
    n = (internal_name or "").lower()
    for needle, kind in (
        ("massacre", "massacre"), ("assassin", "assassinate"), ("piracy", "piracy"),
        ("delivery", "delivery"), ("courier", "courier"), ("collect", "collect"),
        ("mining", "mining"), ("salvage", "salvage"), ("rescue", "rescue"),
        ("sightseeing", "passenger"), ("passenger", "passenger"), ("vip", "passenger"),
        ("scan", "scan"), ("disable", "disable"), ("hack", "hack"), ("altruism", "donation"),
        ("longdistanceexpedition", "passenger"),
    ):
        if needle in n:
            return kind
    return "other"


CARGO_KINDS = ("delivery", "collect", "mining", "salvage")
COMBAT_KINDS = ("massacre", "piracy", "assassinate")   # fight at the target, turn in where accepted

# events that mean "the hold may have changed"
CARGO_EVENTS = {
    "Cargo", "CargoTransfer", "CollectCargo", "EjectCargo", "MarketBuy", "MarketSell",
    "MiningRefined", "CargoDepot", "MissionCompleted", "MissionAbandoned", "Died", "BuyDrones",
    "SellDrones", "LaunchDrone", "SearchAndRescue", "Resurrect", "Collect Cargo", "Continued",
}

# Every event the HUD is specified to react to (kept in one place so it can be tested).
# "Collect Cargo" is accepted as an alias of the real journal name "CollectCargo".
# "Status" is not a journal event: it is Status.json, delivered through EDMC's dashboard_entry.
LISTENED_EVENTS = {
    "Cargo", "CargoDepot", "CargoTransfer", "CollectCargo", "Collect Cargo", "Continued", "Died",
    "DisbandedSquadron", "EjectCargo", "MarketBuy", "MarketSell", "Missions", "MissionAbandoned",
    "MissionAccepted", "MissionCompleted", "MissionFailed", "MissionRedirected", "Status",
    "Bounty", "FactionKillBond",     # kill progress for massacre missions
}

# events we care about when scanning older journals for mission details
MISSION_EVENTS = {
    "MissionAccepted", "MissionRedirected", "MissionCompleted", "MissionAbandoned",
    "MissionFailed", "CargoDepot",
}


class HudState:
    """Thread-safe store. Call handle_event() for each journal entry; snapshot() to read."""

    def __init__(self, data_file=None):
        self._lock = threading.RLock()
        self.data_file = data_file
        self.cmdr = None
        self.system = None
        self.station = None
        self.docked = False
        self.capacity = None          # ship cargo capacity (t)
        self.missions = {}            # int id -> dict
        self.version = 0              # bumped on every visible change
        self._dirty = False
        self._last_flush = 0.0
        # cargo
        self.cargo_items = []         # [{key,name,count,mission_id,stolen}]
        self.cargo_names = {}         # key -> localised name
        self.cargo_source = "none"    # file | edmc | none
        self.cargo_file = None        # path to Cargo.json
        self._cargo_mtime = None
        self._cargo_file_t = 0.0      # mtime (epoch s) of the Cargo.json we last loaded
        self._edmc_totals = None
        self._edmc_totals_t = 0.0
        # latest Status.json (future HUD elements: fuel, pips, ...)
        self.status = {}
        self._profiles = self._load_profiles()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load_profiles(self):
        if not self.data_file:
            return {}
        try:
            with open(self.data_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("version") == STATE_FILE_VERSION:
                return data.get("profiles", {})
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("could not read %s (starting fresh)", self.data_file)
        return {}

    def flush(self, force=False):
        """Write state to disk (debounced). Safe to call often."""
        with self._lock:
            if not self.data_file or not (self._dirty or force):
                return
            if not force and time.time() - self._last_flush < 2.0:
                return
            if self.cmdr:
                self._profiles[self.cmdr] = {
                    "missions": {str(k): v for k, v in self.missions.items()},
                    "saved": time.time(),
                }
            payload = {"version": STATE_FILE_VERSION, "profiles": self._profiles}
            tmp = self.data_file + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                os.replace(tmp, self.data_file)
                self._dirty = False
                self._last_flush = time.time()
            except Exception:
                log.exception("could not write %s", self.data_file)

    def _touch(self):
        self.version += 1
        self._dirty = True

    def _switch_profile(self, cmdr):
        if not cmdr or cmdr == self.cmdr:
            return
        if self.cmdr:  # stash the outgoing commander
            self._profiles[self.cmdr] = {
                "missions": {str(k): v for k, v in self.missions.items()},
                "saved": time.time(),
            }
        self.cmdr = cmdr
        prof = self._profiles.get(cmdr, {})
        self.missions = {int(k): v for k, v in prof.get("missions", {}).items()}
        self._touch()

    # ------------------------------------------------------------------
    # journal reduction
    # ------------------------------------------------------------------
    def handle_event(self, entry, replay=False, edmc_system=None, edmc_station=None):
        """Apply one journal entry. Returns True if it may have changed the hold."""
        ev = entry.get("event")
        if not ev:
            return False
        with self._lock:
            try:
                return self._handle(ev, entry, replay, edmc_system, edmc_station)
            except Exception:
                log.exception("error handling %s", ev)
                return False

    def _handle(self, ev, e, replay, edmc_system, edmc_station):
        # --- identity / location ---------------------------------------
        if ev in ("Commander", "LoadGame"):
            name = e.get("Name") or e.get("Commander")
            if name:
                self._switch_profile(name)
        elif ev == "Location":
            self.system = e.get("StarSystem") or self.system
            self.docked = bool(e.get("Docked"))
            self.station = e.get("StationName") if self.docked else None
            self._touch()
        elif ev == "FSDJump":
            self.system = e.get("StarSystem") or self.system
            self.station, self.docked = None, False
            self._touch()
        elif ev == "CarrierJump":
            self.system = e.get("StarSystem") or self.system
            self.docked = bool(e.get("Docked", True))
            self.station = e.get("StationName") if self.docked else None
            self._touch()
        elif ev == "Docked":
            self.system = e.get("StarSystem") or self.system
            self.station, self.docked = e.get("StationName"), True
            self._touch()
        elif ev == "Undocked":
            self.station, self.docked = None, False
            self._touch()
        elif ev == "Loadout":
            if e.get("CargoCapacity") is not None:
                self.capacity = e.get("CargoCapacity")
                self._touch()

        # live EDMC context is authoritative for "where am I"
        if not replay:
            if edmc_system:
                self.system = edmc_system
            if edmc_station:
                self.station = edmc_station

        if ev == "Cargo" and e.get("Vessel", "Ship") == "Ship" and "Inventory" in e:
            self._set_items_from_inventory(e["Inventory"] or [])
            self.cargo_source = "file"
            self._cargo_file_t = time.time()
            self._touch()

        elif ev == "Cargo" and e.get("Vessel", "Ship") == "Ship" and e.get("Count") == 0:
            self._set_items_from_inventory([])        # bare "Cargo" with Count 0 = empty hold
            self.cargo_source = "file"
            self._cargo_file_t = time.time()
            self._touch()

        # --- other listened events ----------------------------------------
        if ev == "Status":
            self.set_status(e)
        elif ev == "Continued":
            self._touch()                     # journal rolled to a new file: state carries over, re-read cargo
        elif ev == "Died":
            self._touch()                     # hold is lost; Cargo/Cargo.json follows on respawn. Missions persist.
        elif ev == "DisbandedSquadron":
            self._touch()                     # squadron/wing context changed; nothing tracked per squadron yet

        # --- missions ---------------------------------------------------
        if ev == "MissionAccepted":
            self._mission_accepted(e)
        elif ev == "Missions":
            self._missions_bulk(e)
        elif ev == "MissionRedirected":
            m = self.missions.get(e.get("MissionID"))
            if m is not None:
                m.setdefault("kill_system", m.get("dest_system"))     # where the targets are, before the turn-in redirect
                m["dest_system"] = e.get("NewDestinationSystem") or m.get("dest_system")
                m["dest_station"] = e.get("NewDestinationStation") or m.get("dest_station")
                m["redirected"] = True
                if m.get("kind") in COMBAT_KINDS and m.get("kill_count"):
                    m["kills"] = m["kill_count"]          # the game redirects once the kills are done
                self._touch()
        elif ev in ("Bounty", "FactionKillBond"):
            self._kill(e)
        elif ev in ("MissionCompleted", "MissionAbandoned", "MissionFailed"):
            if self.missions.pop(e.get("MissionID"), None) is not None:
                self._touch()
        elif ev == "CargoDepot":
            self._cargo_depot(e)

        return ev in CARGO_EVENTS

    # -- mission handlers -------------------------------------------------
    def _blank_mission(self, mid):
        return {
            "id": mid, "accepted": False, "name": "", "title": "", "kind": "other",
            "faction": "", "expiry": None, "wing": False,
            "origin_system": None, "origin_station": None,
            "dest_system": None, "dest_station": None, "redirected": False,
            "commodity": "", "commodity_name": "", "count": 0,
            "kill_count": 0, "target_faction": "", "target_type": "",
            "accepted_at": None,
            "kills": 0, "kill_ts": "",      # kills credited so far / timestamp of the last one counted (replay-safe)
            "passengers": 0, "reward": 0,
            "delivered": None, "collected": None, "total": None,
        }

    def _kill(self, e):
        """
        Credit a kill to every active massacre mission against that faction (the journal has no progress
        counter). Only kills in the mission's target system, after it was accepted, count; capped at the
        target. Events at or before the mission's last counted timestamp are ignored so a replay of the
        same journal never double counts.
        """
        victim, ts = e.get("VictimFaction"), str(e.get("timestamp") or "")
        if not victim:
            return
        changed = False
        for m in self.missions.values():
            if (m.get("kind") == "massacre" and m.get("accepted") and not m.get("redirected")
                    and m.get("target_faction") == victim and m.get("kill_count")
                    and (not m.get("dest_system") or m["dest_system"] == self.system)
                    and ts > (m.get("kill_ts") or "") and m.get("kills", 0) < m["kill_count"]):
                m["kills"] = m.get("kills", 0) + 1
                m["kill_ts"] = ts
                changed = True
        if changed:
            self._touch()

    def _mission_accepted(self, e):
        mid = e.get("MissionID")
        if mid is None:
            return
        m = self.missions.get(mid) or self._blank_mission(mid)
        name = e.get("Name", "")
        m.update({
            "accepted": True,
            "name": name,
            "title": e.get("LocalisedName") or m.get("title") or name,
            # "Mission_Altruism" donating N units of a commodity is a cargo mission; AltruismCredits is not.
            "kind": "delivery" if (mission_kind(name) == "donation" and e.get("Commodity")) else mission_kind(name),
            "faction": e.get("Faction", ""),
            "expiry": parse_ts(e.get("Expiry")) or m.get("expiry"),
            "wing": bool(e.get("Wing")),
            "origin_system": e.get("_origin_system") or self.system,
            "origin_station": e.get("_origin_station") or self.station,
            "dest_system": e.get("DestinationSystem") or None,
            "dest_station": e.get("DestinationStation") or e.get("DestinationSettlement") or None,
            "redirected": False,
            "commodity": canon(e.get("Commodity")),
            "commodity_name": e.get("Commodity_Localised") or (pretty(e.get("Commodity")) if e.get("Commodity") else ""),
            "count": int(e.get("Count") or 0),
            "kill_count": int(e.get("KillCount") or 0),
            "target_faction": e.get("TargetFaction", ""),
            "target_type": e.get("TargetType_Localised") or e.get("TargetType", ""),
            "passengers": int(e.get("PassengerCount") or 0),
            "reward": int(e.get("Reward") or 0),
        })
        m["kill_system"] = m["dest_system"]
        if not m.get("accepted_at"):
            m["accepted_at"] = parse_ts(e.get("timestamp"))      # for first-come-first-served ordering on the HUD
        if not m.get("kill_ts"):
            m["kills"], m["kill_ts"] = 0, str(e.get("timestamp") or "")
        self.missions[mid] = m
        self._touch()

    def _missions_bulk(self, e):
        """Login-time 'Missions' event: authoritative list of ACTIVE ids (details missing)."""
        active = e.get("Active") or []
        ts = parse_ts(e.get("timestamp")) or time.time()
        ids = set()
        for a in active:
            mid = a.get("MissionID")
            if mid is None:
                continue
            ids.add(mid)
            m = self.missions.get(mid)
            if m is None:
                m = self._blank_mission(mid)
                m["name"] = a.get("Name", "")
                m["title"] = a.get("LocalisedName") or pretty_mission(a.get("Name"))
                m["kind"] = mission_kind(a.get("Name"))
                self.missions[mid] = m
            if a.get("Expires") is not None:
                m["expiry"] = ts + float(a["Expires"])
        for mid in [k for k in self.missions if k not in ids]:
            del self.missions[mid]
        self._touch()

    def _cargo_depot(self, e):
        mid = e.get("MissionID")
        m = self.missions.get(mid)
        if m is None:
            return          # not one of our active missions (e.g. a wing-mate's); never create ghost rows
        m["collected"] = e.get("ItemsCollected", m.get("collected"))
        m["delivered"] = e.get("ItemsDelivered", m.get("delivered"))
        m["total"] = e.get("TotalItemsToDeliver", m.get("total"))
        ct = e.get("CargoType")
        if ct and not m.get("commodity"):
            m["commodity"] = canon(ct)
            m["commodity_name"] = e.get("CargoType_Localised") or pretty(ct)
        self._touch()

    # ------------------------------------------------------------------
    # EDMC-provided data
    # ------------------------------------------------------------------
    def set_edmc_cargo(self, totals, capacity=None):
        """EDMC's own running cargo totals ({canonical_name: count}). Newest-source-wins vs Cargo.json."""
        with self._lock:
            clean = {k: int(v) for k, v in (totals or {}).items() if int(v or 0) > 0}
            if clean != self._edmc_totals:
                self._edmc_totals = clean
                self._edmc_totals_t = time.time()
                self._touch()
            if capacity:
                self.capacity = capacity

    def set_status(self, status):
        with self._lock:
            self.status = dict(status or {})

    # ------------------------------------------------------------------
    # Cargo.json
    # ------------------------------------------------------------------
    def set_cargo_file(self, path):
        with self._lock:
            if path != self.cargo_file:
                self.cargo_file = path
                self._cargo_mtime = None

    def poll_cargo_file(self, force=False):
        """Reload Cargo.json if it changed on disk. Cheap (one stat call). Returns True if reloaded."""
        path = self.cargo_file
        if not path:
            return False
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return False
        if not force and mtime == self._cargo_mtime:
            return False
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False  # half-written; we will retry next tick (mtime not recorded)
        with self._lock:
            self._cargo_mtime = mtime
            if data.get("Vessel", "Ship") != "Ship":
                return False  # SRV / fighter hold - ignore
            self._set_items_from_inventory(data.get("Inventory") or [])
            self.cargo_source = "file"
            self._cargo_file_t = mtime / 1e9
            self._touch()
        return True

    def _set_items_from_inventory(self, inventory):
        items = []
        for it in inventory:
            key = canon(it.get("Name"))
            if not key:
                continue
            name = it.get("Name_Localised") or pretty(it.get("Name"))
            self.cargo_names[key] = name
            items.append({
                "key": key, "name": name, "count": int(it.get("Count") or 0),
                "mission_id": it.get("MissionID"), "stolen": int(it.get("Stolen") or 0),
            })
        self.cargo_items = items

    def current_cargo_items(self):
        """
        Cargo.json is richer (it knows which cargo belongs to which mission); EDMC's running
        totals update instantly from journal events. If they agree use the file; if they
        disagree, whichever was updated most recently wins.
        """
        file_items = list(self.cargo_items)
        edmc = self._edmc_totals
        if edmc is None:
            return file_items
        if self.cargo_source != "file":
            use_edmc = True
        else:
            file_totals = {}
            for it in file_items:
                file_totals[it["key"]] = file_totals.get(it["key"], 0) + it["count"]
            file_totals = {k: v for k, v in file_totals.items() if v > 0}
            use_edmc = file_totals != edmc and self._edmc_totals_t > self._cargo_file_t
        if not use_edmc:
            return file_items
        return [{"key": k, "name": self.cargo_names.get(k) or pretty(k), "count": n,
                 "mission_id": None, "stolen": 0} for k, n in sorted(edmc.items())]

    # ------------------------------------------------------------------
    # view model
    # ------------------------------------------------------------------
    def snapshot(self, now=None):
        now = now or time.time()
        with self._lock:
            items = self.current_cargo_items()
            by_key, by_mission = {}, {}
            for it in items:
                by_key[it["key"]] = by_key.get(it["key"], 0) + it["count"]
                if it["mission_id"] is not None:
                    by_mission[it["mission_id"]] = by_mission.get(it["mission_id"], 0) + it["count"]
            total = sum(by_key.values())

            # shared pool per commodity = market cargo not tied to a specific mission
            pool = {}
            for it in items:
                if it["mission_id"] is None:
                    pool[it["key"]] = pool.get(it["key"], 0) + it["count"]

            ms = [dict(m) for m in self.missions.values()]
            ms.sort(key=lambda m: (m["expiry"] or 9e18, m["id"]))
            out = []
            for m in ms:
                kind = m["kind"]
                total_needed = m["total"] if m["total"] is not None else (m["count"] or None)
                delivered = m["delivered"] or 0
                needed = max(0, (total_needed or 0) - delivered) if kind in CARGO_KINDS and total_needed else None
                hold = None
                if needed is not None and m["commodity"]:
                    own = by_mission.get(m["id"], 0)
                    take = min(max(0, needed - own), pool.get(m["commodity"], 0))
                    pool[m["commodity"]] = pool.get(m["commodity"], 0) - take
                    hold = own + take
                m["needed"], m["hold"] = needed, hold
                m["total_needed"] = total_needed
                m["delivered_n"] = delivered
                m["ready"] = bool(needed is not None and hold is not None and hold >= needed)
                m["expires_in"] = (m["expiry"] - now) if m["expiry"] else None

                # where do I go?
                role, tsys, tstn = self._target(m, needed)
                m["role"], m["target_system"], m["target_station"] = role, tsys, tstn
                m["here"] = bool(
                    tstn and self.station and tstn == self.station and (not tsys or tsys == self.system)
                )
                out.append(m)

            return {
                "cmdr": self.cmdr, "system": self.system, "station": self.station, "docked": self.docked,
                "capacity": self.capacity, "cargo_total": total, "cargo_items": items,
                "cargo_by_key": by_key, "missions": out, "status": dict(self.status),
                "cargo_source": self.cargo_source, "version": self.version,
            }

    @staticmethod
    def _target(m, needed):
        """Return (role, system, station). role in deliver|turn_in|go."""
        dsys, dstn = m.get("dest_system"), m.get("dest_station")
        osys, ostn = m.get("origin_system"), m.get("origin_station")
        if not m.get("accepted") and not (dsys or dstn or m.get("redirected")):
            return "unknown", None, None          # login-time stub we could not recover details for
        if m.get("redirected"):
            # Combat missions (massacre...) are redirected to the turn-in station when the kills are done.
            at_origin = (not ostn) or dstn == ostn or m["kind"] in COMBAT_KINDS
            return ("turn_in" if at_origin else "go"), dsys, dstn
        if m["kind"] in COMBAT_KINDS:
            return "turn_in", osys, ostn
        if m["kind"] in CARGO_KINDS and needed == 0 and m.get("total_needed") and (dsys or dstn):
            # Everything is delivered (by me or a wing-mate) but the game has not yet sent MissionCompleted:
            # the reward still has to be collected at the destination station.
            return "collect", dsys, dstn
        if dsys or dstn:
            return ("deliver" if m["kind"] in CARGO_KINDS else "go"), dsys, dstn
        return "turn_in", osys, ostn

    # ------------------------------------------------------------------
    # journal replay (startup catch-up)
    # ------------------------------------------------------------------
    @staticmethod
    def journal_files(journal_dir):
        try:
            files = glob.glob(os.path.join(journal_dir, "Journal*.log"))
            files.sort(key=lambda p: (os.path.getmtime(p), p))
            return files
        except OSError:
            return []

    def replay_journal(self, journal_dir, max_backfill_files=60):
        """
        Rebuild state from the newest journal and back-fill mission details from older ones.
        Safe to run in a worker thread while live events keep arriving.
        """
        files = self.journal_files(journal_dir)
        if not files:
            return False
        self.set_cargo_file(os.path.join(journal_dir, "Cargo.json"))
        latest = files[-1]
        mission_lines = []
        try:
            with open(latest, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    self.handle_event(entry, replay=True)
                    if entry.get("event") in MISSION_EVENTS:
                        mission_lines.append(entry)
        except OSError:
            log.exception("could not read %s", latest)
            return False

        with self._lock:
            wanted = {mid for mid, m in self.missions.items() if not m["accepted"]}
        if wanted:
            self._backfill(files[:-1][::-1][:max_backfill_files], wanted)
            for entry in mission_lines:           # re-apply newest file so ordering is correct
                self.handle_event(entry, replay=True)
        self.poll_cargo_file(force=True)
        self.flush(force=True)
        return True

    def _backfill(self, older_files_newest_first, wanted):
        """Find MissionAccepted/Redirected/CargoDepot for ids we only know as stubs."""
        need = set(wanted)
        collected = []      # chronological blocks (newest file first -> reversed later)
        for path in older_files_newest_first:
            if not need:
                break
            block = []
            sysn, stn = None, None
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if not any(k in line for k in ('"Mission', '"CargoDepot', '"Location"', '"Docked"',
                                                       '"Undocked"', '"FSDJump"', '"CarrierJump"')):
                            continue
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        ev = entry.get("event")
                        if ev == "Location":
                            sysn = entry.get("StarSystem") or sysn
                            stn = entry.get("StationName") if entry.get("Docked") else None
                        elif ev == "FSDJump":
                            sysn, stn = entry.get("StarSystem") or sysn, None
                        elif ev == "CarrierJump":
                            sysn = entry.get("StarSystem") or sysn
                            stn = entry.get("StationName") if entry.get("Docked", True) else None
                        elif ev == "Docked":
                            sysn, stn = entry.get("StarSystem") or sysn, entry.get("StationName")
                        elif ev == "Undocked":
                            stn = None
                        elif ev in MISSION_EVENTS and entry.get("MissionID") in wanted:
                            if ev == "MissionAccepted":
                                entry["_origin_system"], entry["_origin_station"] = sysn, stn
                                need.discard(entry["MissionID"])
                            block.append(entry)
            except OSError:
                continue
            if block:
                collected.append(block)
        for block in reversed(collected):         # oldest file first
            for entry in block:
                if entry.get("event") in ("MissionCompleted", "MissionAbandoned", "MissionFailed"):
                    continue                       # already resolved by Missions reconcile
                self.handle_event(entry, replay=True)
        log.info("backfill finished; %d mission(s) still without details", len(need))
