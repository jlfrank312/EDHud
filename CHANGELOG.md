# Changelog

## 0.1.3
- New, optional **Cockpit curvature** (File > Settings > ED HUD, off by default): the panels are drawn as if they sit on
  the curved cockpit view-screen like the game's own Info / side panels - turned a little about the vertical axis in
  perspective (the edge nearer the pilot is taller, the far edge smaller) with a very slight depth-of-field softness that
  grows towards the far edge. *Curve strength %* sets how far the panel is turned (50 = 14 degrees), *Softness %* the
  depth-of-field blur (0 = crisp). Both apply live. Panels are assumed to be on the right-hand side of the view; a
  mirrored left-hand look exists internally (`edhud_curve_side` = 0) but has no setting in the dialog yet.
- How it works: the panel is drawn into a Pillow image (new `edhud_curve.py`), warped with a perspective transform and
  shown through the same two click-through, separately-fading windows as before (colour-keyed picture), so opacity,
  text opacity, click-through and dragging behave as in the flat panel. The panel centre does not move when the effect is
  switched on. Pillow ships with EDMC; if it, or a TrueType monospace font, is missing - or anything in the effect fails -
  the overlay stays flat (the log says why) and the setting is greyed out when Pillow is absent.
- With the effect on, a redraw (only when the content changes) takes roughly 0.15-0.3 s on the test machine; the frame picture is cached.
- Added `tests/test_curve.py`.

## 0.1.2
- Startup replay: a kill in the same second as the last replayed kill is no longer dropped as a duplicate; parked live kills are de-duplicated against the lines the replay actually read instead of resetting the occurrence counter. (Two byte-identical kill lines in the same second straddling the end of the replay can't be told apart from a duplicate; real Bounty events carry ship/reward fields so this is vanishingly rare.)

## 0.1.1 (code-review fixes)
- Missions panel: "N tons in cargo" now uses each mission's own share of the hold, so stacked missions for the same
  commodity no longer both show the same cargo as theirs.
- Massacre kills: several kills inside one journal second all count; missions from the same issuing faction no longer
  count the same kill (earliest-accepted unfinished one only); replaying a journal can no longer re-credit kills
  (persisted per-commander watermark; state saved by 0.1 is migrated).
- Startup replay: live events that arrive during the replay are parked and applied afterwards, so older replayed lines
  can't overwrite newer location/mission state or resurrect a mission completed in the meantime.
- Cargo.json polling is serialised (tick, journal callback and replay worker no longer race).
- Journal files are ordered by the timestamp in their name, not file mtime (survives restored/copied folders).
- Windows: EDHud uses its own ctypes handle to user32 with declared prototypes instead of mutating the shared
  `ctypes.windll.user32`; topmost is re-asserted with SetWindowPos (HWND_TOPMOST, no-activate), falling back to Tk.
- Cargo-bar number colour can no longer equal the overlay's transparency key if a theme sets `bg` to the dark text colour.
- Logger name fixed to `<appname>.EDHud`; removed a duplicate/unreachable commodity colour entry; README brought in line
  with the compact one-line mission format and kill tracking.
- Added `tests/test_review_fixes.py`.
