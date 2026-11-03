"""
EDHud controller: wires the state tracker to the overlay windows and EDMC.

Kept free of hard EDMC imports (a `cfg` adapter and `journal_dir_fn` are injected)
so it can be tested outside EDMC.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from edhud_hotkey import HotkeyListener, combo_from_event, normalise
from edhud_overlay import OverlayWindow, load_theme
from edhud_panels import PANELS, demo_snapshot
import edhud_cargo
import edhud_curve
from edhud_state import COMMODITY_NAMES, HudState, load_commodity_names

log = logging.getLogger("EDMarketConnector.EDHud")

UNSET = -99999
TICK_MS = 500
TOPMOST_REASSERT_S = 3.0
DEFAULTS = {"enabled": 1, "font_size": 11, "opacity": 88, "text_opacity": 100, "locked": 1,
            "cargo_combined": 1,     # 1 = cargo sits under the missions panel, 0 = its own window
            "cargo_bar": 1,          # 1 = proportional bar, 0 = itemised list
            "curve": 0,              # 1 = cockpit curvature: panels angled + slightly soft like the in-game screens
            "curve_strength": 50,    # 0-100 (stored as a magnitude; the side is curve_side), how far the panel is turned
            "curve_tilt": 30,        # 0-100, in-plane tilt about the inner-bottom corner
            "curve_soft": 40,        # 0-100, depth-of-field softness towards the far edge
            "curve_side": 1}         # 1 = panels sit on the right of the view, 0 = on the left (negative strength in the UI)


class Settings:
    """Thin adapter over EDMC's `config` (get_int/set) with an 'edhud_' prefix."""

    def __init__(self, config):
        self.config = config

    def get(self, key, default=None):
        d = DEFAULTS.get(key, default if default is not None else 0)
        return self.config.get_int(f"edhud_{key}", default=d)

    def set(self, key, value):
        self.config.set(f"edhud_{key}", int(value))

    def get_str(self, key, default=""):
        try:
            return self.config.get_str(f"edhud_{key}", default=default) or default
        except Exception:
            return default

    def set_str(self, key, value):
        self.config.set(f"edhud_{key}", str(value))

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
        self._prefs_top = None
        self._hotkey_q = queue.SimpleQueue()
        self.hotkey = HotkeyListener(lambda: self._hotkey_q.put(1))
        self._hotkey_status = None

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
        self._apply_hotkey()
        self.master.after(100, self._poll_hotkey)
        return frame

    def _curve_params(self):
        """CurveParams for the overlay windows, or None when the effect is off / Pillow is unavailable."""
        if not self.settings.get("curve") or not edhud_curve.available():
            return None
        try:
            screen_w = int(self.master.winfo_screenwidth())
        except Exception:
            screen_w = 1920
        return edhud_curve.CurveParams(
            strength=max(0, min(100, self.settings.get("curve_strength"))),
            soft=max(0, min(100, self.settings.get("curve_soft"))),
            side=1 if self.settings.get("curve_side") else -1,
            screen_w=screen_w,
            tilt=max(0, min(100, self.settings.get("curve_tilt"))))

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
                    text_opacity=self.settings.get("text_opacity") / 100.0,
                    locked=bool(self.settings.get("locked")),
                    curve=self._curve_params(),
                )
            except Exception:
                log.exception("could not create overlay window %s", p["id"])

    # -- global hotkey: toggles the overlay ---------------------------------
    def _apply_hotkey(self):
        combo = normalise(self.settings.get_str("hotkey", ""))
        ok = self.hotkey.start(combo) if combo else (self.hotkey.stop() or False)
        if combo and not ok:
            log.warning("overlay hotkey %s not active: %s", combo, self.hotkey.error)
        return combo, ok

    def _poll_hotkey(self):
        if self._stopping:
            return
        pressed = False
        try:
            while True:
                self._hotkey_q.get_nowait()
                pressed = not pressed            # two presses between polls cancel out
        except queue.Empty:
            pass
        if pressed:
            self.toggle_overlay()
        self.master.after(100, self._poll_hotkey)

    def toggle_overlay(self):
        on = not bool(self.settings.get("enabled"))
        self.settings.set("enabled", int(on))
        if hasattr(self, "toggle_var"):
            self.toggle_var.set(int(on))
        if not on:
            self._demo_until = 0.0
        self._last_version = None
        self._refresh()

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
                            edhud_cargo.set_known_names(COMMODITY_NAMES.values())
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
                nm, nc = self.state.stats()
                log.info("journal replay finished in %.2fs: %d missions, %d cargo stacks", time.time() - t0, nm, nc)
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
            opts = {"cargo_style": "bar" if self.settings.get("cargo_bar") else "list"}
            by_id = {p["id"]: p for p in PANELS}
            show = {p["id"]: (enabled or demo) and self.settings.panel_on(p) and self._game_ok(demo) for p in PANELS}
            combined = bool(self.settings.get("cargo_combined")) and show.get("missions") and show.get("cargo")
            for p in PANELS:
                win = self.windows.get(p["id"])
                if not win:
                    continue
                if combined and p["id"] == "cargo":
                    win.hide()                       # its content is appended to the missions panel
                elif show[p["id"]]:
                    try:
                        lines = p["render"](snap, opts)
                        if combined and p["id"] == "missions":
                            lines = lines + by_id["cargo"]["render"](snap, dict(opts, combined=True))
                        win.set_lines(lines)
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
        try:
            self._prefs_top = parent.winfo_toplevel()
        except Exception:
            self._prefs_top = None
        v = self._pref_vars = {
            "enabled": tk.IntVar(value=self.settings.get("enabled")),
            "locked": tk.IntVar(value=self.settings.get("locked")),
            "font_size": tk.IntVar(value=self.settings.get("font_size")),
            "opacity": tk.IntVar(value=self.settings.get("opacity")),
            "text_opacity": tk.IntVar(value=self.settings.get("text_opacity")),
            "cargo_combined": tk.IntVar(value=self.settings.get("cargo_combined")),
            "cargo_bar": tk.IntVar(value=self.settings.get("cargo_bar")),
            "curve": tk.IntVar(value=self.settings.get("curve")),
            # signed in the dialog: negative = panel on the left of the view (stored as magnitude + side flag, because
            # EDMC's Windows config store is not reliable for negative numbers)
            "curve_strength": tk.IntVar(value=self.settings.get("curve_strength") * (1 if self.settings.get("curve_side") else -1)),
            "curve_tilt": tk.IntVar(value=self.settings.get("curve_tilt")),
            "curve_soft": tk.IntVar(value=self.settings.get("curve_soft")),
            "hotkey": tk.StringVar(value=normalise(self.settings.get_str("hotkey", ""))),
        }
        for p in PANELS:
            v[f"{p['id']}_on"] = tk.IntVar(value=1 if self.settings.panel_on(p) else 0)

        r = 0
        nb.Label(frame, text="ED HUD - on-screen cargo & missions").grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 2))
        r += 1
        nb.Checkbutton(frame, text="Show overlay", variable=v["enabled"], command=self.apply_prefs).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        nb.Label(frame, text="Overlay on/off hotkey").grid(row=r, column=0, sticky="w")
        hk = ttk.Entry(frame, textvariable=v["hotkey"], width=18)
        hk.grid(row=r, column=1, sticky="w")
        hk.bind("<KeyPress>", self._hotkey_keypress)
        nb.Button(frame, text="Clear", command=lambda: (v["hotkey"].set(""), self.apply_prefs())).grid(
            row=r, column=2, sticky="w", padx=(6, 0))
        r += 1
        self._hotkey_status = nb.Label(frame, text="Click the box, then press the combination (e.g. Ctrl+Alt+H).")
        self._hotkey_status.grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        for p in PANELS:
            nb.Checkbutton(frame, text=f"  {p['label']} panel", variable=v[f"{p['id']}_on"], command=self.apply_prefs).grid(
                row=r, column=0, columnspan=3, sticky="w")
            r += 1
        nb.Checkbutton(frame, text="Combine Cargo with the Missions panel (one window)",
                       variable=v["cargo_combined"], command=self.apply_prefs).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        nb.Label(frame, text="Cargo display").grid(row=r, column=0, sticky="w")
        nb.Radiobutton(frame, text="Capacity bar", variable=v["cargo_bar"], value=1, command=self.apply_prefs).grid(row=r, column=1, sticky="w")
        nb.Radiobutton(frame, text="Itemised list", variable=v["cargo_bar"], value=0, command=self.apply_prefs).grid(row=r, column=2, sticky="w")
        r += 1
        nb.Label(frame, text="Font size").grid(row=r, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(frame, from_=8, to=28, width=5, textvariable=v["font_size"]).grid(row=r, column=1, sticky="w", pady=(8, 0))
        r += 1
        nb.Label(frame, text="Window opacity %").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(frame, from_=20, to=100, increment=5, width=5, textvariable=v["opacity"]).grid(row=r, column=1, sticky="w")
        r += 1
        nb.Label(frame, text="Text opacity %").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(frame, from_=30, to=100, increment=5, width=5, textvariable=v["text_opacity"]).grid(row=r, column=1, sticky="w")
        nb.Label(frame, text="(separate from window opacity on Windows)").grid(row=r, column=2, sticky="w", padx=(6, 0))
        r += 1
        have_pil = edhud_curve.available()
        nb.Checkbutton(frame, text="Cockpit curvature: angle the panels like the in-game cockpit screens (slightly soft)"
                       if have_pil else "Cockpit curvature (unavailable: Pillow not found)",
                       variable=v["curve"], command=self.apply_prefs,
                       state="normal" if have_pil else "disabled").grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        r += 1
        nb.Label(frame, text="   Curve strength %").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(frame, from_=-100, to=100, increment=5, width=5, textvariable=v["curve_strength"]).grid(row=r, column=1, sticky="w")
        nb.Label(frame, text="(positive = panel on the right of the screen, negative = on the left)").grid(
            row=r, column=2, sticky="w", padx=(6, 0))
        r += 1
        nb.Label(frame, text="   Tilt %").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(frame, from_=0, to=100, increment=5, width=5, textvariable=v["curve_tilt"]).grid(row=r, column=1, sticky="w")
        nb.Label(frame, text="(rotates about the inner-bottom corner)").grid(row=r, column=2, sticky="w", padx=(6, 0))
        r += 1
        nb.Label(frame, text="   Softness (depth of field) %").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(frame, from_=0, to=100, increment=5, width=5, textvariable=v["curve_soft"]).grid(row=r, column=1, sticky="w")
        r += 1
        nb.Checkbutton(frame, text="Lock position (click-through). Untick to drag panels with the mouse.",
                       variable=v["locked"], command=self._lock_toggled).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        r += 1
        nb.Button(frame, text="Show 15 s demo", command=lambda: self.demo(15)).grid(row=r, column=0, sticky="w", pady=(10, 0))
        nb.Button(frame, text="Reset positions", command=self.reset_positions).grid(row=r, column=1, sticky="w", pady=(10, 0))
        r += 1
        for name in ("font_size", "opacity", "text_opacity", "curve_strength", "curve_tilt", "curve_soft"):
            v[name].trace_add("write", lambda *a: self._live())      # spinboxes apply as you change them
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
        self.settings.set("cargo_combined", v["cargo_combined"].get())
        self.settings.set("cargo_bar", v["cargo_bar"].get())
        self.settings.set("curve", v["curve"].get())
        try:
            signed = max(-100, min(100, int(v["curve_strength"].get())))
            self.settings.set("curve_strength", abs(signed))
            if signed:                                  # 0 has no side: keep the last one
                self.settings.set("curve_side", 1 if signed > 0 else 0)
            self.settings.set("curve_tilt", max(0, min(100, int(v["curve_tilt"].get()))))
            self.settings.set("curve_soft", max(0, min(100, int(v["curve_soft"].get()))))
        except (tk.TclError, ValueError):
            pass
        try:
            self.settings.set("font_size", max(8, min(28, int(v["font_size"].get()))))
            self.settings.set("opacity", max(20, min(100, int(v["opacity"].get()))))
            self.settings.set("text_opacity", max(30, min(100, int(v["text_opacity"].get()))))
        except (tk.TclError, ValueError):
            pass
        for p in PANELS:
            self.settings.set(f"{p['id']}_on", v[f"{p['id']}_on"].get())
        for win in self.windows.values():
            win.set_font(None, self.settings.get("font_size"))
            win.set_opacity(self.settings.get("opacity") / 100.0)
            win.set_text_opacity(self.settings.get("text_opacity") / 100.0)
            win.set_locked(bool(self.settings.get("locked")))
            win.set_curve(self._curve_params())
        if hasattr(self, "toggle_var"):
            self.toggle_var.set(self.settings.get("enabled"))
        self._last_version = None
        self._refresh()
        if "hotkey" in v:
            self.settings.set_str("hotkey", normalise(v["hotkey"].get()))
            combo, ok = self._apply_hotkey()
            if self._hotkey_status is not None:
                try:
                    self._hotkey_status.configure(text=(
                        f"Hotkey {combo} is active." if combo and ok else
                        f"Hotkey {combo} is not active: {self.hotkey.error}." if combo else
                        "Click the box, then press the combination (e.g. Ctrl+Alt+H). Backspace clears."))
                except tk.TclError:
                    pass

    def _live(self):
        try:
            self.apply_prefs()
        except Exception:
            log.exception("live settings update failed")

    def _lock_toggled(self):
        """Unlocking must work immediately: EDMC's settings dialog holds a modal grab that would swallow drags."""
        self.apply_prefs()
        top = self._prefs_top
        if top is not None:
            try:
                if self._pref_vars["locked"].get():
                    top.grab_set()
                else:
                    top.grab_release()
            except tk.TclError:
                pass

    def _hotkey_keypress(self, ev):
        if ev.keysym in ("BackSpace", "Delete") and not (ev.state & 0x5):
            self._pref_vars["hotkey"].set("")
        else:
            combo = combo_from_event(ev.keysym, ev.state)
            if combo:
                self._pref_vars["hotkey"].set(combo)
        self.apply_prefs()
        return "break"

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
        try:
            self.hotkey.stop()
        except Exception:
            pass
        for win in self.windows.values():
            win.destroy()
        self.windows.clear()
