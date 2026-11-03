"""
EDHud controller: wires the state tracker to the overlay windows and EDMC.

Kept free of hard EDMC imports (a `cfg` adapter and `journal_dir_fn` are injected)
so it can be tested outside EDMC.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import tkinter as tk
from tkinter import ttk

from edhud_overlay import OverlayWindow, load_theme
from edhud_panels import PANELS, demo_snapshot
from edhud_state import HudState, load_commodity_names

log = logging.getLogger("EDMarketConnector.EDHud")

UNSET = -99999
TICK_MS = 500
TOPMOST_REASSERT_S = 3.0
DEFAULTS = {"enabled": 1, "font_size": 11, "opacity": 88, "locked": 1}


class Settings:
    """Thin adapter over EDMC's `config` (get_int/set) with an 'edhud_' prefix."""

    def __init__(self, config):
        self.config = config

    def get(self, key, default=None):
        d = DEFAULTS.get(key, default if default is not None else 0)
        return self.config.get_int(f"edhud_{key}", default=d)

    def set(self, key, value):
        self.config.set(f"edhud_{key}", int(value))

    def pos(self, panel):
        x = self.config.get_int(f"edhud_{panel['id']}_x", default=UNSET)
        y = self.config.get_int(f"edhud_{panel['id']}_y", default=UNSET)
        if x == UNSET or y == UNSET:
            return panel["default_x"], panel["default_y"]
        return x, y

    def panel_on(self, panel):
        return bool(self.config.get_int(f"edhud_{panel['id']}_on", default=1))


class Controller:
    def __init__(self, plugin_dir, config, journal_dir_fn, game_running_fn=None):
        self.plugin_dir = str(plugin_dir)
        self.settings = Settings(config)
        self.journal_dir_fn = journal_dir_fn
        self.game_running_fn = game_running_fn
        self.state = HudState(os.path.join(self.plugin_dir, "edhud_state.json"))
        self.master = None
        self.windows = {}
        self.status_var = None
        self._last_version = None
        self._demo_until = 0.0
        self._last_topmost = 0.0
        self._replay_thread = None
        self._stopping = False
        self._tick_id = None
        self._journal_dir = None
        self._pref_vars = {}

    # ------------------------------------------------------------------
    # EDMC callbacks
    # ------------------------------------------------------------------
    def on_journal(self, cmdr, system, station, entry, state):
        """Called from EDMC's journal_entry (Tk main thread)."""
        changed_cargo = self.state.handle_event(entry, edmc_system=system, edmc_station=station)
        if state is not None:
            try:
                self.state.set_edmc_cargo(state.get("Cargo"), state.get("CargoCapacity"))
            except Exception:
                log.exception("could not read EDMC cargo state")
        if changed_cargo:
            self.state.poll_cargo_file(force=True)

    def on_status(self, entry):
        """Called from EDMC's dashboard_entry (Status.json changed)."""
        self.state.set_status(entry)

    # ------------------------------------------------------------------
    # UI construction (needs Tk)
    # ------------------------------------------------------------------
    def build_main_ui(self, parent):
        """EDMC main-window row; also where we get our Tk root."""
        self.master = parent.winfo_toplevel()
        frame = tk.Frame(parent)
        self.status_var = tk.StringVar(value="ED HUD: starting…")
        tk.Label(frame, textvariable=self.status_var, anchor="w").grid(row=0, column=0, sticky="w")
        self.toggle_var = tk.IntVar(value=self.settings.get("enabled"))
        ttk.Checkbutton(frame, text="Overlay", variable=self.toggle_var, command=self._toggle_enabled).grid(
            row=0, column=1, sticky="e", padx=(8, 0))
        frame.columnconfigure(0, weight=1)
        self._create_windows()
        self.master.after(1500, self._start_replay)    # let EDMC finish starting its journal monitor
        self._schedule_tick()
        return frame

    def _create_windows(self):
        n = load_theme(os.path.join(self.plugin_dir, "edhud_theme.json"))
        if n:
            log.info("applied %d colour overrides from edhud_theme.json", n)
        for p in PANELS:
            x, y = self.settings.pos(p)
            try:
                self.windows[p["id"]] = OverlayWindow(
                    self.master, p["id"], x, y, on_moved=self._window_moved,
                    font_size=self.settings.get("font_size"),
                    opacity=self.settings.get("opacity") / 100.0,
                    locked=bool(self.settings.get("locked")),
                )
            except Exception:
                log.exception("could not create overlay window %s", p["id"])

    def _window_moved(self, panel_id, x, y):
        self.settings.config.set(f"edhud_{panel_id}_x", int(x))
        self.settings.config.set(f"edhud_{panel_id}_y", int(y))

    def _toggle_enabled(self):
        self.settings.set("enabled", self.toggle_var.get())
        if not self.toggle_var.get():
            self._demo_until = 0.0          # switching the overlay off also ends any demo
        self._last_version = None
        self._refresh()

    # ------------------------------------------------------------------
    # startup replay (worker thread - no Tk access in here)
    # ------------------------------------------------------------------
    def _load_names(self):
        """EDMC keeps the official symbol->name table at <app dir>/FDevIDs/commodity.csv."""
        try:
            app_dir = getattr(self.settings.config, "app_dir_path", None)
            for base in (app_dir, self.plugin_dir):
                if base:
                    for rel in (("FDevIDs", "commodity.csv"), ("commodity.csv",)):
                        path = os.path.join(str(base), *rel)
                        if os.path.isfile(path):
                            n = load_commodity_names(path)
                            log.info("loaded %d commodity names from %s", n, path)
                            return
            log.info("no commodity name table found; using journal names only")
        except Exception:
            log.exception("could not load commodity names")

    def _start_replay(self):
        self._load_names()
        jdir = self.journal_dir_fn()
        self._journal_dir = jdir
        if not jdir or not os.path.isdir(jdir):
            log.warning("journal directory not found (%r) - startup replay skipped", jdir)
            return
        self.state.set_cargo_file(os.path.join(jdir, "Cargo.json"))
        self.state.poll_cargo_file(force=True)

        def work():
            try:
                t0 = time.time()
                self.state.replay_journal(jdir)
                log.info("journal replay finished in %.2fs: %d missions, %d cargo stacks",
                         time.time() - t0, len(self.state.missions), len(self.state.cargo_items))
            except Exception:
                log.exception("journal replay failed")

        self._replay_thread = threading.Thread(target=work, name="EDHud-replay", daemon=True)
        self._replay_thread.start()

    # ------------------------------------------------------------------
    # tick loop (Tk main thread)
    # ------------------------------------------------------------------
    def _schedule_tick(self):
        if self._stopping or self.master is None:
            return
        self._tick_id = self.master.after(TICK_MS, self._tick)

    def _tick(self):
        try:
            if self.state.cargo_file is None and self.journal_dir_fn:
                jdir = self.journal_dir_fn()
                if jdir and os.path.isdir(jdir):
                    self.state.set_cargo_file(os.path.join(jdir, "Cargo.json"))
            self.state.poll_cargo_file()
            self._refresh()
            self.state.flush()
        except Exception:
            log.exception("tick failed")
        finally:
            self._schedule_tick()

    def _refresh(self):
        demo = time.time() < self._demo_until
        enabled = bool(self.settings.get("enabled"))
        snap = demo_snapshot() if demo else self.state.snapshot()
        # also redraw periodically so expiry countdowns advance
        stamp = (snap["version"], int(time.time() // 20), demo, enabled)
        if stamp != self._last_version:
            self._last_version = stamp
            opts = {}
            for p in PANELS:
                win = self.windows.get(p["id"])
                if not win:
                    continue
                if (enabled or demo) and self.settings.panel_on(p) and self._game_ok(demo):
                    try:
                        win.set_lines(p["render"](snap, opts))
                    except Exception:
                        log.exception("render failed for %s", p["id"])
                else:
                    win.hide()
            self._update_status_text(snap, demo)
        now = time.time()
        if now - self._last_topmost > TOPMOST_REASSERT_S:
            self._last_topmost = now
            for win in self.windows.values():
                win.keep_on_top()

    def _game_ok(self, demo):
        return True   # reserved for a future 'only show while Elite is running' option

    def _update_status_text(self, snap, demo):
        if not self.status_var:
            return
        n = len(snap["missions"])
        cap = snap.get("capacity")
        hold = f"{snap['cargo_total']}/{cap}t" if cap else f"{snap['cargo_total']}t"
        src = snap.get("cargo_source", "none")
        text = f"ED HUD: {n} mission{'s' if n != 1 else ''} · hold {hold}"
        if demo:
            text = "ED HUD: DEMO MODE"
        elif src == "none":
            text += " (waiting for cargo data)"
        self.status_var.set(text)

    # ------------------------------------------------------------------
    # settings dialog
    # ------------------------------------------------------------------
    def build_prefs(self, parent, nb):
        frame = nb.Frame(parent)
        v = self._pref_vars = {
            "enabled": tk.IntVar(value=self.settings.get("enabled")),
            "locked": tk.IntVar(value=self.settings.get("locked")),
            "font_size": tk.IntVar(value=self.settings.get("font_size")),
            "opacity": tk.IntVar(value=self.settings.get("opacity")),
        }
        for p in PANELS:
            v[f"{p['id']}_on"] = tk.IntVar(value=1 if self.settings.panel_on(p) else 0)

        r = 0
        nb.Label(frame, text="ED HUD - on-screen cargo & missions").grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 2))
        r += 1
        nb.Checkbutton(frame, text="Show overlay", variable=v["enabled"]).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        for p in PANELS:
            nb.Checkbutton(frame, text=f"  {p['label']} panel", variable=v[f"{p['id']}_on"]).grid(
                row=r, column=0, columnspan=3, sticky="w")
            r += 1
        nb.Label(frame, text="Font size").grid(row=r, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(frame, from_=8, to=28, width=5, textvariable=v["font_size"]).grid(row=r, column=1, sticky="w", pady=(8, 0))
        r += 1
        nb.Label(frame, text="Opacity %").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(frame, from_=30, to=100, increment=5, width=5, textvariable=v["opacity"]).grid(row=r, column=1, sticky="w")
        r += 1
        nb.Checkbutton(frame, text="Lock position (click-through). Untick to drag panels with the mouse.",
                       variable=v["locked"]).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        r += 1
        nb.Button(frame, text="Show 15 s demo", command=lambda: self.demo(15)).grid(row=r, column=0, sticky="w", pady=(10, 0))
        nb.Button(frame, text="Reset positions", command=self.reset_positions).grid(row=r, column=1, sticky="w", pady=(10, 0))
        r += 1
        nb.Label(frame, text="Tip: the game must be in Borderless or Windowed mode for overlays to appear over it.").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
        return frame

    def apply_prefs(self):
        """prefs_changed: persist and apply immediately."""
        if not self._pref_vars:
            return
        v = self._pref_vars
        self.settings.set("enabled", v["enabled"].get())
        self.settings.set("locked", v["locked"].get())
        try:
            self.settings.set("font_size", max(8, min(28, int(v["font_size"].get()))))
            self.settings.set("opacity", max(30, min(100, int(v["opacity"].get()))))
        except (tk.TclError, ValueError):
            pass
        for p in PANELS:
            self.settings.set(f"{p['id']}_on", v[f"{p['id']}_on"].get())
        for win in self.windows.values():
            win.set_font(None, self.settings.get("font_size"))
            win.set_opacity(self.settings.get("opacity") / 100.0)
            win.set_locked(bool(self.settings.get("locked")))
        if hasattr(self, "toggle_var"):
            self.toggle_var.set(self.settings.get("enabled"))
        self._last_version = None
        if not self.settings.get("locked"):
            self.demo(60)     # show something to drag around
        self._refresh()

    def demo(self, seconds):
        self._demo_until = time.time() + seconds
        self._last_version = None
        self._refresh()

    def reset_positions(self):
        for p in PANELS:
            self.settings.config.delete(f"edhud_{p['id']}_x")
            self.settings.config.delete(f"edhud_{p['id']}_y")
            win = self.windows.get(p["id"])
            if win:
                win.set_position(p["default_x"], p["default_y"])

    # ------------------------------------------------------------------
    def stop(self):
        self._stopping = True
        try:
            if self.master is not None and self._tick_id:
                self.master.after_cancel(self._tick_id)
        except Exception:
            pass
        self.state.flush(force=True)
        for win in self.windows.values():
            win.destroy()
        self.windows.clear()
