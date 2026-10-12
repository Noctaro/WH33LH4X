# Working on the code

Everything here is the **developer** path. If you only want to drive, the
[Quickstart](../README.md#quickstart) is shorter and does not need any of it.

## Setup

No compiler and no admin rights, just a venv:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The wheel must be bound to WinUSB for anything that talks to it; see the
[Quickstart](../README.md#quickstart). On Linux, `sudo sh packaging/linux/setup.sh` takes that
place: a udev rule for access to the wheel and `/dev/uinput`, and it keeps the wheel awake.
A checkout runs on the system Python there:

```
sudo apt install python3-usb python3-evdev python3-tk
/usr/bin/python3 -m ui
```

The full path matters when pyenv or a venv is active: apt installs for the system Python only.

Then run the window with `.\.venv\Scripts\pythonw.exe -m ui`, or the bridge alone with
`.\.venv\Scripts\python.exe -m bridge`.

## Building

```powershell
.\packaging\build_bundle.ps1     # the downloadable bundle -> dist\WH33LH4X
```

The bundle carries its own embeddable CPython, so a built `dist\WH33LH4X` needs no venv and no
Python installed. That is what a release is. CI builds the same bundle on every push.

The Linux release is a Flatpak, built by CI from `packaging/linux/io.github.Noctaro.WH33LH4X.yml`
on the Freedesktop 25.08 runtime. The runtime's Python has no tkinter, so the manifest builds
Tcl/Tk 8.6 and `_tkinter` itself. `/app` is read-only, so the launcher sets `WH33LH4X_DATA` and
`paths.py` sends tuning, settings, saved profiles and logs to the sandbox's data folder. On a
machine with `flatpak-builder` and the SDK:

```
flatpak-builder --user --install --force-clean build packaging/linux/io.github.Noctaro.WH33LH4X.yml
```

## Tests

None of these need hardware, and CI runs the first five:

```powershell
.\.venv\Scripts\python.exe test_ffb_render.py    # the control laws still do what they did
.\.venv\Scripts\python.exe test_gip.py            # the same bytes still go on the wire
.\.venv\Scripts\python.exe test_bridge_core.py    # the bridge loop against a fake wheel
.\.venv\Scripts\python.exe test_evdev_decoder.py  # Linux game effects into ffb_render units
.\.venv\Scripts\python.exe test_ui.py             # profiles, the bridge process, Linux setup
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe packaging\gen_commands.py --check
.\.venv\Scripts\python.exe packaging\gen_games_table.py --check
```

On hardware, **Test force** in the window (or `python -m bridge.nudge`) proves the binding,
the motor and the force sign in one go.

## How the pieces fit

```
wheel --USB--> bridge/device.py --reading--> bridge/core.py --axes--> bridge/vjoy.py --> vJoy --> game
wheel <--USB-- bridge/device.py <--force---- bridge/core.py <--effects-- bridge/vjoy.py <-- vJoy <-- game
```

On Linux `bridge/uinput.py` takes vJoy's place, and the game sees a virtual wheel under
`/dev/uinput` instead.

- `gip/` is the protocol: message framing, the generated arming and force sequences, the USB
  interface, the input report.
- `bridge/device.py` owns the wheel: a reader thread for input reports and a writer thread that
  sends force at the endpoint's 250 Hz.
- `bridge/core.py` is the loop: each tick feeds the reading to the front-end, drains the game's
  effects, renders them with `ffb_render`, applies `live_tune`, and commands the force. It polls
  the stop file itself, so every way of stopping zeroes the motor first.
- `bridge/vjoy.py` is the Windows front-end: the vJoy feeder and the decoder that turns vJoy's
  force feedback packets into `ffb_render` effects.
- `bridge/uinput.py` is the Linux front-end: a virtual wheel under `/dev/uinput` with vJoy's
  IDs, and the decoder for the evdev effects games upload to it. Uploads are answered on a
  thread of their own, because a game's upload blocks until it is answered.
- `ui/` is the window. It runs the bridge as a hidden child process and never holds the USB
  interface itself; Test force is a child process too (`bridge/nudge.py`).

## The window

`ui/` is deliberately small: profiles, Start and Stop, the feel sliders, and setup checks. It
runs on Windows and Linux.

### Why tkinter and not a web UI

The alternative was Edge in app mode talking to a local HTTP server. That opens a listening
socket, and means two processes, two languages, and a lifecycle where closing the window does
not stop the server.

tkinter is not in the embeddable runtime, so the bundle vendors tcl/tk: measured at +2.5 MB
zipped, and the three binaries (`tcl86t.dll`, `tk86t.dll`, `_tkinter.pyd`) are signed by the
Python Software Foundation. The Sun Valley theme (`sv-ttk`) is pure Tcl on top. One process,
one language, no port.

### Why the bridge is a separate process

The force loop ticks every 4 ms. In the same process as the window, the GIL would let a redraw
delay a tick, which is felt as a step. As a child process the bridge also keeps working without
the window: `WH33LH4X.cmd`, or headless on Linux.

Stopping it is a **stop file**, not a kill. The bridge sees the file within a tenth of a second,
zeroes the force and releases the wheel. `Popen.kill()` is only the fallback after ten seconds,
long enough for a bridge that is still arming the wheel.

### Tuning files

`tune.json` is the tracked template. The bridge and the window use `user-tune.json`, created
from it the first time, so a user's tuning never shows up as a change to the repository and
survives extracting a new bundle over the old one. Profiles are JSON in `profiles/`, applied by
writing their values into `user-tune.json`, which the bridge re-reads every half second.

## ffb_render

The arithmetic half of the bridge: a game sends its own coefficients, saturations and dead
bands, several effects at once, and expects them summed. `EffectMixer` holds every effect a
game created and renders their sum.

### Units

Everything inside is normalised:

| Quantity | Units |
|---|---|
| position, velocity, acceleration | reading units and reading units per second, -1.0 to +1.0 across full lock |
| coefficients, saturations, dead bands | fractions, not DirectInput's 0..10000 integers |
| output force | -1.0 to +1.0 |

DirectInput's integers are converted once, at the boundary, by `ConditionParams.from_di`.
Keeping DI units out of the interior is deliberate: the alternative is dividing by 10000 in a
dozen places and eventually forgetting one, which is a factor of 10000 error that presents as
"force feedback does nothing".

Coefficients are **not** clamped to DirectInput's range, because the fitted software conditions
legitimately use a spring gain of 2.0, measured rather than guessed. Saturation still bounds the
result, so an out of range coefficient only decides how quickly an effect reaches full force.

### Velocity smoothing

Differentiating a quantised reading is noisy, and that noise reaches the motor as audible
chatter. `WheelState.VELOCITY_SMOOTHING` is an exponential factor per sample, fitted against
logged ticks from this wheel: 1.0 is raw and makes inertia pure hash, lower is smoother but
lags the wheel.

`CentringLaw`, the tuned spring behind `centring: "tuned"`, samples velocity once per input
report instead, as the Linux driver it was tuned with does, so its feel does not depend on the
loop rate. `test_ffb_render.py` proves it gives the Linux driver's force at 60 and 250 Hz.

### The fitted laws

`legacy_condition_params` expresses the laws fitted to this wheel as `ConditionParams`, and
`test_ffb_render.py` compares them against the original formulas over a sweep. Any change in
their output is a bug rather than a tuning opportunity.

Friction is the odd one. The fitted law is direction only, a flat gain outside a velocity dead
band with no proportionality, which is expressible here as a very steep coefficient saturating
immediately. Same output, no special case in the formula.

One deliberate difference: at exactly the dead band edge the original law is already at full
drag, because its test was `abs(v) < 0.05`, while this returns 0 there and full drag an epsilon
beyond. The transition is 1e-6 wide, the function is discontinuous at that point either way,
and a velocity landing on exactly 0.05 has measure zero.

## Adding a game

A game's entry has two halves. `games.json` holds the machine-readable part and generates
GAMES.md's status table, so it is edited first and the table is regenerated with
`packaging\gen_games_table.py`. GAMES.md holds the setup steps by hand. If the game needs its
own tuning, add a profile in `profiles/`.

Keep GAMES.md to what a user does: the steps, the values, and what is still wrong. Measurements
belong in [Measured game values](#measured-game-values) or [hardware.md](hardware.md), and
unknowns stay unknowns, because a blank is more useful than a guess.

```markdown
## Game name

**Tested on:** store, build, and date, over raw USB.

### Setup
Numbered steps, exact paths and exact text. Say "nothing" if it just works.

### Settings
The profile's values, and the in-game settings that matter.

### Known issues
What is still wrong, one or two lines each.
```

Four things are worth measuring before writing any of it:

- **Does the game see the vJoy device as a wheel at all?** This decides everything else, and
  games answer it in ways their settings menu does not show.
- **Does force ever go negative?** Run `tune_report.py` after a session. A wheel whose output
  never changes sign cannot centre, whatever it feels like, and rectified force feels perfectly
  normal on kerbs and gravel.
- **Which way does the game's force point?** Measure with `strength: 0` so the wheel is free
  while steering by hand. With force applied, position and force drive each other and the
  correlation means nothing.
- **Which in-game sliders actually do something.** Some do nothing until the device is
  correctly classified.

## Measured game values

What GAMES.md, docs/tuning.md and the profiles rest on. User pages keep the steps; the
measurements are here.

### How dir_mode reads a game's direction field

A force arrives with a magnitude and a direction, and games disagree about which of the two
carries the sign. Getting this wrong rectifies the output: the wheel pulls one way and never
centres, while kerbs and gravel still feel correct, which is what makes it hard to spot.

| Mode | Reads the field as |
|---|---|
| `sin` | A true polar angle, taking the X component. Correct for a game that points forces around a full circle |
| `span` | A steering axis across the 90 to 180 degree quarter circle, interpolating between the ends |
| `sign` | The same two directions with no interpolation, so nothing nulls out halfway |

#### What was measured

Sending cartesian +X through DirectInput to vJoy arrives as `DirX=8191`, a quarter of 32768, so
the field is a full circle in 32768 steps and +X sits at 90 degrees. Sending -X arrives as 8191
as well, because DirectInput normalises a single axis cartesian vector and the sign cannot
survive that. **For a cartesian sender the sign travels in the magnitude**, and `+3000` and
`-3000` round trip exactly.

That is not the only convention. A game may send a positive magnitude and point it with a polar
angle, flipping two angles to mean left and right. DiRT 4 does this, and not at 0 and 180 as
expected but between **90 and 180**. Measured over one session:

```
8191  (90 deg)  x11780      <- one direction
16383 (180 deg) x2990       <- the other
12287 (135 deg) x29, then a long tail of ones and twos
```

Those two values are about 99% of every packet, and the 1253 others are transient sweeps
between them, which is why the field first looked like a continuous axis. Both dominant values
are non-negative under sine, since sin(90) is +1 and sin(180) is 0, so **a polar reading
rectifies this game no matter how carefully the boundary is handled**. The quarter circle
contains no negative sine to find.

Hence a mode rather than a fix: both conventions are real and one wheel meets both. It is read
live from `tune.json`, so a game that resends its direction every tick switches over within
milliseconds. The alternative was a relaunch per guess, and each guess costs a game load and a
drive back to the corner.

`span` polarity was measured, not chosen. With the other sign, force correlated **+0.365** with
steering angle, pushing deeper into the turn instead of back out of it.

### The gain stage the software cannot see

The wheel holds a force feedback strength setting of its own, in the **HORI FFB RWD-Devicemanager
für Xbox Series X Series S** app from the Microsoft Store. It is the HORI app that can talk to
the wheel in Xbox mode; the other HORI apps cannot. **It is driven with the wheel itself and
does not respond to keyboard or mouse**, which is confusing the first time you open it.

Nothing in this project can read or change that setting, and it sits above everything in
`tune.json`. Measured 2026-08-26: at a fixed commanded force of `0.30`, the wheel travelled
**0.099** at strength 8, **0.029** at strength 1, and **0.106** back at 8 again. About 3.5x,
from identical software settings.

If two machines feel different with the same settings, that is the first place to look.

Those numbers were measured through `Windows.Gaming.Input`. Whether the setting still applies
over raw USB is unmeasured, and while WinUSB is bound the HORI app cannot reach the wheel at
all, so set it before binding and note what it is.

### Where the numbers came from

`min_force` comes from the breakaway measurement in
[hardware.md](hardware.md#stiction-and-positions-the-wheel-will-not-leave), the smallest force
that moves the wheel at all. The per-game values live in `profiles/`; what was measured to
arrive at them is here.

#### DiRT 4

At `strength: 0.2`, `max_force: 0.45`, the game asked for the full ±1.000 and the wheel got
±0.200. Across 1699 non-zero samples, 10.0% fell below breakaway and 0.3% hit the ceiling. So
the bottom of the range is not the problem and the headroom is at the top, which is why raising
`strength` is the change that matters and `min_force: 0.01` is a small one.

`invert: true` was measured with our own output at zero and the car driving: DiRT 4's force
points the same way as the steering angle, 140 samples, mean magnitude 0.596, 96% matching
sign, symmetric on both sides. Applied unchanged that is positive feedback, and the wheel runs
away into whatever corner it is turned into. Over raw USB the same setting was confirmed on
2026-09-24: with `invert: true` the output opposed steering in 186 of 209 strong ticks.

The game sends friction at roughly the same rate as constant force, 13,970 operations against
13,950 over 374 telemetry samples, with two spring and two damper operations and no periodics.
An earlier session recorded constant force only, and the likeliest explanation is that it
predates registering vJoy as a wheel, which changes the profile the game hands out. Untested
either way.

### DiRT 4's device id

`type="wheel"` is the part that matters. The GUID is your device's PID and VID concatenated,
then `504944564944`, which is ASCII for `PIDVID`. vJoy is `VID_1234` `PID_BEAD`, hence
`{BEAD1234-...}`. To read your own IDs:

```powershell
Get-ChildItem "HKCU:\System\CurrentControlSet\Control\MediaProperties\PrivateProperties\Joystick\OEM" |
  ForEach-Object { "{0} -> {1}" -f $_.PSChildName, (Get-ItemProperty $_.PSPath).OEMName }
```

On Linux, Feral's port sends one constant force; the Windows version under Proton also sends
friction, spring and damper, as it does on Windows.

### Automobilista 2

What we send is clean. Measured against `logs/vjoy_bridge_20260827_030410.log`, the steering
fed to vJoy is the wheel position exactly, with no throttle term in it, at every position.

## What is where

Every script's flags and a one-line description of each are in [COMMANDS.md](../COMMANDS.md).
The short version:

| File | Purpose |
|---|---|
| `ui/` | The window, `python -m ui`: profiles, the bridge process, setup checks |
| `bridge/` | The bridge, `python -m bridge`: the loop, the wheel over USB, the vJoy and uinput front-ends, Test force |
| `gip/` | The GIP protocol over raw USB: framing, arming, USB host, input reports |
| `profiles/` | The window's profiles: Default, DiRT 4, RC car |
| `ffb_render.py` | The control laws: spring, damper, friction, inertia, periodics, envelopes, the tuned centring |
| `live_tune.py` | Tuning file reloading and the wheel-button tuning controls |
| `tune.json` | The template `user-tune.json` is created from |
| `games.json` | Per-game notes and quirks, the source of GAMES.md's status table |
| `vjoy_ffb_spike.py` | Verifies the vJoy force feedback path the bridge is built on |
| `tune_report.py` | Reads a bridge log and reports what the force feedback actually did |
| `probe_log.py` | Session logging to `logs/` |
| `paths.py` | Where the app's files are and where it writes: the same folder, except in the Flatpak |
| `dinput_abi.py` | ctypes binding for DirectInput 8, used by `vjoy_ffb_spike.py` |
| [`evidence/`](../evidence/README.md) | Why the other APIs cannot drive this wheel, and the raw USB instruments |

Anything load bearing has been written into `evidence/README.md` or into comments beside the
code it constrains, so the reasoning sits next to the thing it explains rather than in a
document that goes stale on its own.
