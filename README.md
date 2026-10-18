# WoW Fishing Bot (YOLO + debug GUI)

**Version 1.12.4** — see `CHANGELOG.md`. The number is `__version__` in `fishing_bot.py` and is printed in the window title and the first line of the debug log.

Windows-only desktop tool: watches one or more World of Warcraft client windows,
casts fishing, uses a YOLO model (`best.pt`, classes `Bobber` / `CatchClick`)
to find the bobber on screen, listens to the game's audio for the bite splash,
and right-clicks the bobber. A CustomTkinter GUI shows a live debug log.

> Automating gameplay violates Blizzard's Terms of Service and can get an
> account banned. Use at your own risk.

## Setup

1. Python 3.10+ on Windows.
2. `pip install -r requirements.txt` (the script also auto-installs anything missing on first run).
3. Put your trained model `best.pt` next to `fishing_bot.py`. It should have a
   `Bobber` class (used to locate the bobber); `CatchClick` is only needed for vision mode.
4. In WoW: run **windowed / windowed-fullscreen**, bind Fishing to action key `1`
   (or change "Cast key" in the GUI), and turn on **Auto Loot** (Options > Controls)
   or tick "Hold Shift while clicking" in the GUI.
5. For sound mode (default), in WoW **System > Sound**: tick **Enable Sound in Background**,
   Sound Effects on and loud, Music and Ambience down to 0, and output to your
   default Windows playback device. Mute Discord/Spotify or any other app that
   might produce loud sounds (a false trigger is harmless but wastes time).
6. `python fishing_bot.py` and press START.

Keep the WoW window(s) unobscured: the bot captures the screen region of the
client area, so anything drawn on top of it (including this GUI) ends up in the
frame the model sees.

## The FishBotLink addon (recommended)

The YOLO model only knows the bobbers it was trained on. If your log says
`0 boxes drawn` even with the camera zoomed in, the model simply does not
recognise your bobber (toy bobber skins, unusual water, unusual angle), and no
amount of camera fiddling fixes that. The `addon/FishBotLink` addon makes the
**game itself** tell the bot what it needs, so the model becomes optional.

Install: copy the `addon/FishBotLink` folder into your client's
`Interface/AddOns/` folder, then `/reload`. The TOC targets **Legion 7.3.5**
(`## Interface: 70300`); for a Retail or Classic client tick **Load out of date
AddOns** in the AddOns list, or change that number. The Lua uses only APIs that
exist from 7.0 onward. You should see a tiny strip of five coloured
squares in the extreme top-left corner of the game view (magenta, then
red/green). Do not cover it with other UI.

What it does — a WoW addon cannot move the mouse, click, or write a file while
you play, but it can draw. So it paints a 16-pixel square per fact, and the bot
reads those pixels out of its screenshot:

| square | meaning | what the bot does with it |
|---|---|---|
| 1 magenta | addon is loaded | switches to addon mode automatically |
| 2 | the game's tooltip under the cursor says **Fishing Bobber** | sweeps the cursor over the water, nearest-the-centre first, until this turns green. Finds the bobber **exactly**, in any zoom, with any skin, no model needed. The cursor then stays parked on it. |
| 3 | you are channeling Fishing | confirms the cast started (re-presses the key if not); ends the cast the moment the channel runs out instead of waiting the full watch window |
| 4 | a loot window just opened | **definitive catch**. A click that produces no loot was not your bite — no more guessing from "is the bobber still visible" |
| 5 | you are in combat | stops casting |
| 6 | you are flagged AFK | jumps immediately to clear it |

**How the sweep finds a 15-pixel bobber.** The hitbox is tiny, so a coarse
cursor grid steps right over it. The bot looks, in order:

1. Is the cursor already on it (2 px nudge, then tooltip read)?
2. **Red cap.** The bobber's cap is the only saturated red thing on open water,
   so red blobs in the sweep region are probed first with a tight cursor
   spiral. On a standard bobber this finds it in well under a second.
3. **What changed** since the frame grabbed just before the cast key (two
   post-cast frames ANDed so moving sparkle drops out), limited to the height
   band where the bobber usually lands.
4. **Band sweep.** Two rows straddling the usual landing height, fanning out
   from the usual x (learned from earlier casts, kept in `landings.json`),
   then outer rows, then wider.

The log line `Cursor is on the bobber at ... (N red spot(s), M change spot(s))`
tells you which stage did the work. If your bobber skin has no red, stage 2
simply finds nothing and stages 3–4 take over.

**Deciding when to click.** Clicking the bobber before the bite ends the cast
with nothing caught. The bot tracks the red cap in a 64 px square around the
parked cursor: a bite is the cap sinking, or most of it going under, held for
a few frames, together with a splash sound within 1 s (either order). Only a
cap that stays mostly under for three frames in a row clicks with no sound.
Sparkle is not red, so it no longer counts as the bobber moving. A splash with
the cap staying put is logged as someone else's bite.

**Sound setup matters.** In WoW, System > Sound: Sound Effects high, **Music
and Ambience at 0**, Enable Sound in Background on, output to the device the
bot prints at startup. If the log says `Ambient sound is loud (-20 dB)`, music
or ambience is burying the splash and bites will be missed. If it says
`ambient -60 dB, splash needs +18 dB`, the bot heard nothing during calibration
and is requiring a louder splash so quiet background noise is not mistaken for
one. Square 4 (loot) then says whether it was a catch. Each cast's log line ends
with `bobber motion idle X peak Y` so the thresholds (`MOTION_*` at the top of
the script) can be tuned from real numbers.

The strip sits over the corner of the player frame; that is unavoidable, since
the bot reads fixed pixels at the client's top-left. `/fbl` hides it when you
are not botting and shows it again (the bot needs it visible).

Turn on **Auto Loot** in WoW so the loot window closes by itself; otherwise the
next cast cannot start while it is open. If the sweep logs that it never saw the
tooltip, the bobber landed outside the swept area (middle 70% of the width, 20–75%
of the height): tilt the camera so the water fills the centre of the screen.

## WoW Forever and other clients

On START the log names the client. The window title is `World of Warcraft`
and Task Manager shows the same name, so those two strings are not the test.

- **Beta:** `WowB.exe`, or the folder `_classic_beta_`. The log says
  `Client: WoW Forever beta — D:\...\World of Warcraft\_classic_beta_\WowB.exe`.
- **Release:** the `_forever_` flavor folder, `WowForever.exe`, or a build
  whose folder is still the `camelot` codename. If none of those match, the
  bot reads the `Product` field in `.build.info` next to the exe
  (`wow_forever`, `wow_camelot`, `wow_classic_beta`). The log says
  `Client: WoW Forever release — D:\...\World of Warcraft\_forever_\Wow.exe`.

A Legion client (`wow-64.exe`), retail (`Wow.exe` in `_retail_`), and Classic
(`_classic_` or `_classic_era_`) log `Client: other WoW` and keep the mouse sweep.

WoW Forever listens for the splash and then catches.

- **Interact key filled in.** The splash presses that key. The mouse is not
  moved, and moving the mouse does not pause the bot. This is a real keypress;
  posting the key into the window does not fire Interact. Face the water, clear
  your target, and turn on Options > Controls > Enable Interact Key. Type
  `space` in the field to use the space bar.
- **Interact key blank.** The same splash clicks the bobber instead.

The splash field is the catch line. With the room measuring as silence, the
bobber splash in these logs is about **+42 to +44 dB** and the quieter water
is about +20 to +31. A field of +43 lets the splash through and leaves the
quieter sounds alone. One splash presses the key once. FishBotLink still
confirms the catch from the loot pixel. The field is ignored when the client
is not Forever.

## Where and how to stand

- **Fish alone.** Sound mode listens for the bobber splash, and another player's
  bobber makes the identical sound. At a crowded spot (Margoss's Retreat, the
  Dalaran fountain, a raid-group pool) the bot hears their bites and cannot tell
  them from yours. The log says `Heard N splash-level sound(s)` when that is happening.
- **Zoom the camera in and tilt it down** so the bobber is large and near the
  middle of the screen, and keep the WoW window large (maximised windowed). The
  model was trained on a close bobber; a far-away dot on a wide lake is
  invisible to it, and the bot refuses to click anything it is not confident
  about (`Bobber not found on screen after cast`). When the full frame finds
  nothing the bot re-scans in 2x2 tiles (effectively 2x zoom) before giving up.
- **Look in `debug_frames/`** when the bobber is not found: the bot saves the
  screenshot with every box the model produced drawn on it (green = confident
  enough to use, orange = too weak). If the bobber has no box at all, the model
  simply does not recognise it at that size and angle; if it has an orange box,
  it is close and a bit more zoom will do.
- **Enable Sound in Background**, otherwise WoW goes silent the moment it loses
  focus (`WoW is silent right now` in the log).

## How a cast works

1. Press the cast key (posted directly to the WoW window, so it works in the background).
2. Wait `min bite delay` seconds. During the second half of that window the bot
   locates the bobber (`Bobber` class, falling back to `CatchClick`) and, in sound
   mode, measures the **ambient** loudness of the game audio via a WASAPI loopback
   capture of your speakers.
3. Watch until `watch seconds`:
   - **Sound mode** (default): the bobber position is refreshed every 0.4 s, and
     once it is found only a square around it is scanned. Audio is measured in
     two bands (full and high-passed), each against its own ambient. A bite is a
     *short* spike at least "N dB above ambient" (default 8 dB) in either band,
     that also clears the median of the surrounding second, so a steady sound
     (music, a long spell) does not count. Spikes just below the margin are
     logged so you can tune it.
   - **Vision mode**: runs the model every frame and confirms a splash only after
     `required_hits` consecutive `CatchClick` frames above an adaptive threshold
     (idle-bobber score + 0.15). Use this only if sound capture is unavailable;
     in practice the `CatchClick` class does not separate idle from splash well.
4. Bring WoW to the foreground, move the mouse with a real input event (so WoW
   registers the hover), right-click via `SendInput`.
5. Verify: shortly after the click the bobber should be gone. It counts as
   "still there" only if two frames in a row show it near the click at about the
   strength it was tracked at. In sound mode the bot then clicks once more at the
   bobber's fresh position. If it is still there, that sound was not the bite:
   the bot keeps watching the same cast and now requires a louder spike (a real
   splash is far louder than the noises that fool it). Turn on **Auto Loot**,
   otherwise the open loot window keeps the bobber on screen and every catch
   looks like a miss.
6. A lure key, if set, is pressed on a timer before casting. If the bobber is
   missing for N casts in a row the bot stops instead of recasting into empty
   water. Moving the mouse yourself pauses that cast until the cursor rests.
7. The form is written to `settings.json` next to the script and restored on
   the next launch. The status line counts casts, catches, missing bobbers, and
   false clicks, cumulative for as long as the window is open.

Multiple clients: each bot process listens to the same speakers, so a splash in
any client makes every bot click its own bobber. Right-clicking a bobber that
hasn't bitten does nothing, and the verification step tells each bot whether
*its* bobber was the one.

## Reading the debug log

| Message | Meaning |
| --- | --- |
| `Client: WoW Forever beta — D:\...\_classic_beta_\WowB.exe` | Forever beta. With an Interact key set, a splash presses that key. |
| `Client: WoW Forever release — D:\...\_forever_\Wow.exe` | Forever release. Same Interact key, no mouse sweep. |
| `Client: other WoW — C:\...\Wow.exe` | Not Forever. The bot sweeps and clicks. An Interact key in the form is ignored. |
| `Listening to 'Speakers (...)'` | Loopback capture started on that output device. WoW must play through it. |
| `Watching (sound). bobber at (537,176) via bobber conf=0.91, ambient -38 dB, splash needs +8 dB` | Bobber located; a spike 8 dB over the ambient counts as the splash. |
| `No audio reaching the loopback device` | Nothing is playing: enable "Sound in Background" in WoW or check the output device. |
| `Splash +11 dB over ambient (high band) at 12.3s -- clicking` | Bite detected by sound: a short spike, not a steady noise. |
| `Heard a +6 dB spike at 9.1s, below the +8 dB needed` | Near miss. If these line up with real bites, lower "N dB above ambient". |
| `Bobber still there -- clicking again` | First click didn't take; retrying at the bobber's current position. |
| `Bobber still there after two clicks -> not the bite. Now needs +18 dB` | A noise fooled it; the bar is raised for the rest of this cast and it keeps listening. |
| `Mouse moved. Pausing` | You moved the cursor. The bot waits until it stays still for 2 seconds. |
| `Bobber missing N casts in a row -- stopping` | No bobber was found N times. Face the water; the bobber may be off screen or under the UI. |
| `Applying lure` | The lure key was pressed. It repeats on the interval set in the form. |
| `No catch this cast. Biggest spike: +5 dB (splash needs +8 dB)` | Nothing loud enough. If real splashes are missed, lower "N dB above ambient" (try 6). If random sounds trigger, raise it. |
| `idle bobber conf=0.55, effective threshold=0.70` | (vision mode) The resting bobber scores 0.55; splash must beat 0.70. |
| `inference ~XXX ms/frame` | Per-frame model time. Above ~400 ms the splash may fall between frames; lower `IMGSZ` in the script or use a GPU. |
| `CatchClick hit 1/2 conf=0.81` | A frame cleared the threshold; one more consecutive frame confirms. |
| `Bobber still visible after click -> false positive` | Clicked, bobber didn't vanish; threshold raised, still watching. |
| `No catch this cast. Highest ... seen: 0.38` | Nothing crossed the threshold for the whole watch window. If real splashes are being missed, lower the min confidence or turn off adaptive mode. |
| `CRASHED:` | A worker process hit an exception; the traceback is printed in the panel. |

## Tunables (top of `fishing_bot.py`)

- `IMGSZ` - inference resolution (1280). Lower for speed.
- `TRACK_INTERVAL` - how often the bobber position is refreshed in sound mode (0.4 s).
- `BOBBER_MIN_CONF` - minimum `Bobber` confidence to trust a position (0.30).
- `BASELINE_MARGIN` - vision mode: how much a splash must beat the idle bobber (0.15).
- `VERIFY_DELAY`, `VERIFY_RADIUS`, `VERIFY_MIN_CONF` - post-click bobber-gone check.

## If detection is still flaky

- Sound mode: the signal is the jump between ambient and splash. Lower Music and
  Ambience to 0 in WoW so the ambient is quiet, keep Sound Effects at 100, and
  compare the `ambient` / `Loudest sound` numbers in the log to choose the dB margin.
- Vision mode: the `CatchClick` class is the limiter. Retrain with idle-bobber
  frames labelled only as `Bobber` so the two states separate cleanly.
