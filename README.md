# ED HUD – on-screen cargo & mission tracker for EDMarketConnector

Draws a small, always-on-top, click-through overlay over Elite Dangerous showing:

* **Cargo** – what's in the hold, free capacity, mission cargo tagged.
* **Missions** – every active mission **grouped by where you need to go**:
  * `DELIVER` – drop-off station, with a progress bar (`8/20`) and how much of the
    remaining amount you're already carrying (`hold 12`, green when you have enough).
  * `TURN IN` – missions that finish where you accepted them or at a station the game redirects you to
    (e.g. kill/massacre missions once the kills are done, credit donations). Cargo missions complete
    at the destination as soon as the delivery is made.
  * `COLLECT` – everything is delivered (by you or a wing-mate) but the game hasn't paid out yet: go to the
    destination station to collect the reward. Wing-mate deliveries update the progress bar as they happen.
  * `● HERE` marks the group you can complete at your current station.
  * Expiry countdown (turns yellow under 2 h, red when expired).

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

* Kill progress for massacre-type missions is not tracked (the journal does not report it reliably) – they show the target and expiry only.
* Overlay window styling for click-through/no-focus-steal uses Windows APIs; on Linux/Mac the panels display but are not click-through.
* Everything was tested inside a real EDMC run on Linux (virtual display, simulated journals). The Windows-only part (click-through / no focus steal) was verified with a stubbed Windows API, not on a real Windows + Elite install - if panels steal focus or block mouse clicks on your machine, report it.
