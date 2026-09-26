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
[Quickstart](../README.md#quickstart).

Then run the window with `.\.venv\Scripts\pythonw.exe -m ui`, or the bridge alone with
`.\.venv\Scripts\python.exe -m bridge`.

## Building

```powershell
.\packaging\build_bundle.ps1     # the downloadable bundle -> dist\WH33LH4X
```

The bundle carries its own embeddable CPython, so a built `dist\WH33LH4X` needs no venv and no
Python installed. That is what a release is. CI builds the same bundle on every push.

## Tests

None of these need hardware, and CI runs the first four:

```powershell
.\.venv\Scripts\python.exe test_ffb_render.py    # the control laws still do what they did
.\.venv\Scripts\python.exe test_gip.py            # the same bytes still go on the wire
.\.venv\Scripts\python.exe test_bridge_core.py    # the bridge loop against a fake wheel
.\.venv\Scripts\python.exe test_ui.py             # profiles and the bridge process
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

- `gip/` is the protocol: message framing, the generated arming and force sequences, the USB
  interface, the input report.
- `bridge/device.py` owns the wheel: a reader thread for input reports and a writer thread that
  sends force at the endpoint's 250 Hz.
- `bridge/core.py` is the loop: each tick feeds the reading to the front-end, drains the game's
  effects, renders them with `ffb_render`, applies `live_tune`, and commands the force. It polls
  the stop file itself, so every way of stopping zeroes the motor first.
- `bridge/vjoy.py` is the Windows front-end: the vJoy feeder and the decoder that turns vJoy's
  force feedback packets into `ffb_render` effects.
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
belong in [tuning.md](tuning.md) or [hardware.md](hardware.md), and unknowns stay unknowns,
because a blank is more useful than a guess.

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

## What is where

Every script's flags and a one-line description of each are in [COMMANDS.md](../COMMANDS.md).
The short version:

| File | Purpose |
|---|---|
| `ui/` | The window, `python -m ui`: profiles, the bridge process, setup checks |
| `bridge/` | The bridge, `python -m bridge`: the loop, the wheel over USB, the vJoy front-end, Test force |
| `gip/` | The GIP protocol over raw USB: framing, arming, USB host, input reports |
| `profiles/` | The window's profiles: Default, DiRT 4, RC car |
| `ffb_render.py` | The control laws: spring, damper, friction, inertia, periodics, envelopes, the tuned centring |
| `live_tune.py` | Tuning file reloading and the wheel-button tuning controls |
| `tune.json` | The template `user-tune.json` is created from |
| `games.json` | Per-game notes and quirks, the source of GAMES.md's status table |
| `vjoy_ffb_spike.py` | Verifies the vJoy force feedback path the bridge is built on |
| `tune_report.py` | Reads a bridge log and reports what the force feedback actually did |
| `probe_log.py` | Session logging to `logs/` |
| `dinput_abi.py` | ctypes binding for DirectInput 8, used by `vjoy_ffb_spike.py` |
| [`evidence/`](../evidence/README.md) | Why the other APIs cannot drive this wheel, and the raw USB instruments |

Anything load bearing has been written into `evidence/README.md` or into comments beside the
code it constrains, so the reasoning sits next to the thing it explains rather than in a
document that goes stale on its own.
