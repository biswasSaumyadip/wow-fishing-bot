# Changelog

Version lives in one place: `__version__` at the top of `fishing_bot.py`.
Bump it when you change behaviour, and add a dated section here.

The window title, the heading, the first log line, and each worker's
`Bot N | fishing_bot X.Y.Z` line all print that number, so a pasted log
tells you which build actually ran.

## 1.12.5 — 2026-10-18

Current build.

- WoW Forever does not need FishBotLink. A splash presses Interact and the
  next cast starts. The "addon not detected" line is not logged on that
  client. If the addon is installed anyway, a loot window still counts as
  the catch.

## 1.12.4 — 2026-10-18

- WoW Forever uses the splash field again. 1.12.3 lowered a +43 field to
  +20, so water and the tail of a splash (+20 to +31) pressed Interact. The
  bite in these logs is the +42 to +44 spike. A spike that rounds to the
  field still counts, so a +43 reading is not rejected as below +43.
- One splash presses the key once. The next four seconds are ignored, so the
  same bite cannot press Interact five times.

## 1.12.3 — 2026-10-18

- On WoW Forever a splash presses the Interact key with a real keypress.
  A posted key message was not firing Interact. The mouse is not used, and
  moving the mouse no longer pauses listening.
- If the Interact field is blank, the same splash clicks the bobber instead.
- A splash field above +20 (the log had +43) no longer skips bites of about
  +40. On Forever the catch line is +20 dB. A spike that rounds to the
  required number counts, so "+43 below +43" cannot happen.
- After the key is pressed the threshold stays put. It was climbing to +46
  and then ignoring the rest of the cast. The model is not run on this path,
  so there is no "bobber not found" line.

## 1.12.2 — 2026-10-18

- WoW Forever detection covers the beta and the release. Beta stays
  `WowB.exe` or the `_classic_beta_` folder. The release client is the
  `_forever_` flavor folder, a `WowForever.exe`, or a build still using the
  `camelot` codename. If the folder name is none of those, the `Product`
  column in `.build.info` next to the exe is used (`wow_forever`,
  `wow_camelot`, `wow_classic_beta`).
- Retail (`_retail_`), Classic (`_classic_`, `_classic_era_`), and a Legion
  `wow-64.exe` stay other WoW. The log says which one matched:
  `WoW Forever beta` or `WoW Forever release`.

## 1.12.1 — 2026-10-18

- WoW Forever is recognised when the window title and the process name are
  both just `World of Warcraft`. The beta is `WowB.exe` in
  `World of Warcraft\_classic_beta_`, so the word `forever` is not in the
  title or the path. `WowB.exe`, a `_classic_beta_` folder, or a path/title
  that does contain `forever` is Forever. `wow-64.exe` (Legion) and
  `Wow.exe` in `_retail_` stay other WoW and keep the mouse sweep.
- The log says which signal matched, for example
  `Client: WoW Forever (WowB.exe beta client) — D:\...\WowB.exe`.

## 1.12.0 — 2026-10-18

- The bot reads the full install path and the window title and logs
  `Client: WoW Forever` or `Client: other WoW`, plus the path. 1.12.0 only
  matched the word `forever`, which the real beta client does not use.
- New field **Interact key**. On WoW Forever, with that key filled in, a
  splash presses Interact instead of sweeping and clicking. FishBotLink still
  confirms the catch from the loot pixel. Leave the field blank and Forever
  keeps clicking the bobber. On any other client the field is ignored.
- Type `space` in that field to bind the space bar.

## 1.11.2 — 2026-10-17

- A splash and the cap losing about a third of its area now counts as the bite.
  The old rule wanted 55% gone, so a +23 dB splash with the cap 42% under was
  logged as someone else's bite and the channel then ended with the fish gone.
  The sound and the dip may be up to 1.5 s apart.

## 1.11.1 — 2026-10-17

- A sound below the splash threshold no longer clicks. The quiet branch left
  the measured rise in place, so every sample, including sounds quieter than
  the room (`+-27 dB`), fell through into the click. That is what made WoW
  Forever look like it was catching on nothing, and it crashed with
  `send_mouse_click() argument after * must be an iterable, not NoneType`
  when the model had no bobber position.
- A click with no position is refused. The second click is skipped when the
  fresh position is missing.
- Warns when the splash field is above +20 dB.

## 1.11.0 — 2026-10-05

- Bite trigger tracks the bobber's red cap (height and how much of it is
  visible) instead of the average change in a square. Sparkle and ripples are
  not red, which is what the "bobber area moved ... no splash sound" lines were.
  A click needs the cap to dip or mostly submerge and a splash within 1 s;
  only a cap that stays under for three frames clicks on its own.
- When the post-cast audio measurement is silence, a splash must be +18 dB
  (a small sound over the -60 dB floor was reading as a huge splash and
  clicking early).
- Warns once when the ambient level shows Music or Ambience is still on.

## 1.10.0 — 2026-10-04

- Bobber search: red-cap colour locator runs first (the bobber cap is the only
  saturated red on open water; found in <0.5 s on a standard bobber), change
  detection is limited to the usual landing band and ranks red blobs first,
  and the band sweep fans out from the usual x on two rows straddling the
  hitbox instead of walking a whole row from one side.
- Bite trigger now requires sound and motion to agree within 0.8 s (either
  order); only a very large plunge clicks alone. Fixes both the "plunged" false
  clicks that ended casts and the "+25 dB splash but bobber did not move"
  misses that were actually our bites.
- Missed-bite casts log the idle/peak bobber motion so thresholds can be tuned.

## 1.9.1 — 2026-10-03

- Sweep no longer starts from the YOLO model's guess (which put it in the
  bottom-left UI and made it search everywhere but the middle). Start is the
  last landing, else the upper-middle of the view.
- Landing history persists in `landings.json` (per window size) so the band
  sweep is available from the first cast of a run.
- Change-detection candidates near the character are tried last; candidates
  near the usual landing height first.

## 1.9.0 — 2026-10-03

- Bite trigger in addon mode is now bobber motion (72 px crop around the
  parked cursor, baseline from the idle bobber) fused with the splash sound;
  sound alone no longer clicks, because an early click ends the cast and
  neighbours' splashes were costing most casts.
- Stale-tooltip fix: WoW keeps showing "Fishing Bobber" under a stationary
  cursor after the bobber is gone, which made the bot "find" the old bobber in
  0.1 s and click the water. Every stationary read is now preceded by a 2 px
  cursor nudge.
- Change detection ANDs two post-cast frames (sparkle moves, bobber does not),
  skips blobs wider than 90 px (chat, players, loot window), and is capped at
  4 s; the fallback sweep is a fine horizontal band at the usual landing
  height learned from previous casts, capped at 6 s.

## 1.8.1 — 2026-10-03

- Bobber search no longer steps over the ~15 px hitbox: change detection
  between a pre-cast frame and the landed frame yields candidate spots, each
  searched with an 8 px spiral; the fallback grid is 24 px (was 48) with a
  6 s budget per sweep.
- Addon updates its cells every frame (was every 30 ms) so the hover read
  does not lag the cursor.
- First-run installer restarts the interpreter after installing packages, so
  pywin32's path hooks are active (fixes `No module named 'win32gui'` right
  after install); the postinstall script is looked up in Scripts/ instead of
  as a module.
- `__version__` was left at 1.7.1 in the 1.8.0 build; fixed.

## 1.8.0 — 2026-10-03

- Anti-AFK: between casts the bot jumps with a real key press every N minutes
  (GUI field, default 3, jittered). The addon gained a sixth cell reporting the
  AFK flag; when it is set the bot jumps at once. Older 5-cell addon builds
  still work.

## 1.7.1 — 2026-10-03

- Sweep starts where the bobber landed last cast, and is skipped entirely when
  the cursor is already on the bobber.
- After the sweep touches the bobber the cursor is centred on its hitbox
  instead of left on the first grid point that hit the edge.
- Re-sweeps only after the addon reports the cursor off the bobber on three
  consecutive reads (the tooltip blinks while the bobber bobs), and not at all
  after a click that landed on the bobber but opened no loot (someone else's splash).
- Channel-ended detection is debounced; if the channel ends while the bobber
  is still under the cursor, the bot clicks it before recasting.

## 1.7.0 — 2026-10-03

- New `addon/FishBotLink` WoW addon: paints five status pixels at the top-left of
  the game view (addon marker, "Fishing Bobber" tooltip under cursor, channeling
  Fishing, loot window opened, in combat).
- Bot reads those pixels when present. Bobber is located by sweeping the cursor
  until the game's own tooltip confirms it (no model required; works with any
  bobber skin / zoom); cursor stays parked on it so the click on the splash is
  immediate; catches are confirmed by the loot window instead of by the bobber
  disappearing; cast is re-pressed if no channel started and ended early when
  the channel runs out; combat stops casting.
- Without the addon the behaviour is unchanged (model + sound).

## 1.6.0 — 2026-10-03

- Sound-based bite detection (WASAPI loopback) with a high-passed band and a short-spike check.
- Bobber tracked in a crop after the first find; tiled 2× scan when the full frame sees nothing.
- Weak detections (below 0.35) are never clicked, so a miss cannot become a fake catch.
- After a failed click: one retry, then keep watching the same cast with a higher bar.
- Lure key, pause when you move the mouse, stop after N casts with no bobber.
- Settings saved to `settings.json`. Annotated frames written to `debug_frames/` when the bobber is not found.
- Fresh click lock and queue on every START; Shift released on STOP.

## 1.0.0

First rewrite of the original CustomTkinter + YOLO script (vision-only, adaptive CatchClick threshold).
