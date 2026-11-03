# ED HUD – on-screen cargo & mission tracker for EDMarketConnector

Draws a small, always-on-top, click-through overlay over Elite Dangerous showing:

* **Cargo** – what's in the hold as a capacity bar (one colour per commodity, mission cargo outlined in green) or an
  itemised list, with `x/y tons full`.
* **Missions** – one compact line per active mission, in the order you accepted them. Deliveries to the same
  destination are grouped together:

  ```
  same system:   [Sol - Daedalus] - Deliver Gold, 20 tons / [12 tons in cargo, 12 tons remaining]
  other system:  [Sol - Daedalus] - Deliver Gold, 20 tons to Shinrarta Dezhra - Jameson Memorial / [...]
  massacre:      [Sol - Daedalus] - Massacre 24 Pirate Lords in Hip 1234 [9/24 fulfilled]
  ```

  * `[system - station]` is where the mission was accepted (and, for combat missions, where it is handed in);
    the destination of an away-from-home mission is drawn in red.
  * Cargo missions show how much of the *remaining* amount you are carrying – green when you have enough,
    yellow when you don't. When several missions need the same market-bought commodity, the cargo in the hold
    is shared out between them (in expiry order), so two 20 t Gold missions with 20 t aboard show one ready and
    one not.
  * `[all delivered - collect reward]` – everything is delivered (by you or a wing-mate) but the game has not
    paid out yet: go to the destination station. Wing-mate deliveries update the counts as they happen.
  * Massacre kills are counted from `Bounty` / `FactionKillBond` events (see *Known limitations*); `TURN IN` in
    the right-hand column means the game has redirected the mission because the kills are done.
  * Right-hand columns: payout (`$1.45mill`) and time left (yellow under 2 h, `EXPIRED` in red).
  * `▲` marks a wing mission.

No separate overlay program (EDMCOverlay etc.) is needed – the overlay is drawn by the plugin itself.

## Install

1. Close EDMC.
2. Copy this whole folder to `%LOCALAPPDATA%\EDMarketConnector\plugins\EDHud`
   (folder name must stay `EDHud` – no spaces or hyphens). Result:
   `...\plugins\EDHud\load.py`, `edhud_state.py`, …
3. Start EDMC. The main window gets a line **`ED HUD: N missions · hold X/Y t`** and an **Overlay** checkbox.
4. In Elite: **Options → Graphics → Display → Borderless** (or Windowed).
   Overlays cannot draw over *exclusive Fullscreen* – that's a Windows limitation, not a plugin bug.

## First-run checklist

1. EDMC → **File → Settings → ED HUD** → **Show 15 s demo**. Two panels appear with sample data.
   If you see them, the display side works.
2. Untick *Lock position*, drag the panels where you want them, re-tick *Lock position* (makes them click-through).
3. Start / load the game. The status line in EDMC shows live mission and cargo counts – if that line is correct,
   the data side works even if the overlay is hidden behind something.

## How it keeps live (why it should stay in sync)

| Source | Used for |
|---|---|
| EDMC `journal_entry` events | missions accepted/completed/abandoned/redirected, delivery progress (`CargoDepot`), location |
| `Cargo.json` (polled every 0.5 s) | exact hold contents *including which cargo belongs to which mission* |
| EDMC's own cargo totals | instant update between journal events and file writes |
| Startup replay of the current journal + back-fill of older journals | missions you accepted **before** EDMC/the plugin started |
| `edhud_state.json` (in the plugin folder) | remembers mission details between sessions, per commander |

## Troubleshooting

Log file: `%TEMP%\EDMarketConnector.log` (search for `EDHud`). Expect:

```
ED HUD loaded from ...
journal replay finished in 0.02s: 5 missions, 3 cargo stacks
```

* Status line says `waiting for cargo data` → the plugin can't find `Cargo.json`; check EDMC's journal folder setting.
* Missions show *"details unavailable"* → the mission was accepted more than 60 journal files ago; abandon/complete it or ignore.
* Overlay invisible but demo works in a window → game is in exclusive Fullscreen.

## Adding another HUD element later

Add one function to `edhud_panels.py` returning lines and register it in `PANELS` – it automatically gets its own
window, position, on/off setting and drag support. Examples: fuel/jump range, route, materials, bounties, ship integrity.
`dashboard_entry` (Status.json) is already captured in `state.status` for those.

## Known limitations

* Massacre kill progress is **inferred**, not reported by the game (the journal has no progress counter): the HUD counts
  `Bounty` / `FactionKillBond` events against the target faction, in the target system, after you accepted the mission.
  Missions from the same issuing faction do not stack in the game, so only the earliest-accepted unfinished one is
  credited; missions from different factions each count the kill. Kills made while EDMC was not running, in a journal
  file older than the current one, or by wing-mates are not seen. Treat the count as a guide; the mission board in
  game is authoritative.
* Overlay window styling for click-through/no-focus-steal uses Windows APIs; on Linux/Mac the panels display but are not click-through.
* Everything was tested inside a real EDMC run on Linux (virtual display, simulated journals). The Windows-only part (click-through / no focus steal) was verified with a stubbed Windows API, not on a real Windows + Elite install - if panels steal focus or block mouse clicks on your machine, report it.

## Settings (File > Settings > ED HUD)
Everything below applies immediately, no need to press OK.
- **Overlay on/off hotkey** - click the box and press a combination (e.g. Ctrl+Alt+H); Backspace clears. Windows only.
- **Combine Cargo with the Missions panel** - one window, or two.
- **Cargo display** - proportional capacity bar (names above it, one colour per commodity) or an itemised list.
- **Window opacity / Text opacity** - background and text fade separately (two stacked windows on Windows).
- **Lock position** - untick to drag panels; your real data stays on screen. Only "Show 15 s demo" shows test data.
- Colours: `edhud_theme.json` (see the file). `"commodity_colors": {"gold": "#ffcc00"}` recolours cargo-bar items.
