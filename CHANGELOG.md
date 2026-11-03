# Changelog

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
