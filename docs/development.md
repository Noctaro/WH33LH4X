# Working on the code

Everything here is the **developer** path. If you only want to drive, the
[Quickstart](../README.md#quickstart) is shorter and does not need any of it.

## Setup

No compiler and no admin rights, just a venv with the WinRT projections:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Building

```powershell
.\shim\build.ps1                 # the dinput8.dll proxy; needs zig on PATH
.\packaging\build_bundle.ps1     # the downloadable bundle -> dist\WH33LH4X
```

The bundle carries its own embeddable CPython, so a built `dist\WH33LH4X` needs no venv and no
Python installed. That is what a release is.

## Tests

None of these need hardware:

```powershell
.\.venv\Scripts\python.exe test_ffb_render.py    # the control laws still do what they did
.\.venv\Scripts\python.exe test_shimview.py      # the GUI must not CREATE the shared section
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe packaging\gen_commands.py --check
```

`test_shimview.py` skips itself if a bridge or a game is already running, because reading the
shared section is safe but the test's own setup is not.

## The probe

`wgi_probe.py` drives the real motor directly, with no game and no vJoy in the path. It is the
tool everything else was discovered with.

```powershell
.\.venv\Scripts\python.exe wgi_probe.py             # detect, report, interactive menu
.\.venv\Scripts\python.exe wgi_probe.py --list-only  # detect and report only, play nothing
```

**First run: press `k` to calibrate.** This measures which way the wheel's own position reading
moves when force is applied, and saves it to `wheel_profile.json` keyed by the wheel's VID:PID.
It only needs to run once per wheel, and the software effects (`1s` to `4s`, `y`, `z`) refuse to
run without it.

**Keep the probe window in front while an effect plays.** Force output, and during calibration
position readings too, both stop the instant the window loses foreground. The window re-grabs it
automatically and prints `[foreground OK]` or `[!! NOT foreground !!]` each second so you can
see the state instead of guessing. [docs/hardware.md](hardware.md) explains why.

### Menu reference

| Keys | What |
|---|---|
| `1`-`11` | Firmware effects (constant, ramp, periodics, conditions) |
| `1s` `2s` `3s` `4s` | Software conditions: spring, damper, friction, inertia |
| `y` `z` | Software sine / square wave |
| `k` | Calibrate (run this first) |
| `i` | Motor info |
| `w` `e` `x` | Gain sweep / magnitude sweep / direction test |
| `g` `m` `d` `f` | Set gain / magnitude / duration / frequency |
| `b` | Show the firmware effects that do not work (hidden by default; they still run if typed) |
| `c` | Force one condition sign convention on all effects instead of the measured per-effect one |
| `r` | Release the motor, restore firmware centering |
| `s` | Stop all effects |
| `q` | Quit |

## Logging

Every run writes `logs/wgi_probe_<timestamp>.log`: console output with elapsed timestamps, plus
dense per-tick data (position, velocity, commanded force, foreground state, effect state) that
is not printed live. Flushed per line, so `Ctrl+C` still leaves a complete log. Pass `--no-log`
to disable it.

## The window

`gui.py` is deliberately small: pick a game, start and stop it, three status badges, per-game
notes and the feel sliders. A setup wizard, a per-game installer and a live force meter were
all planned and cut. They can be added once something proves inadequate, which is a better
reason to build them than a plan written before anyone used it.

### Why tkinter and not a web UI

The alternative was Edge in app mode talking to a local HTTP server. That costs zero download,
but it opens a listening socket, in a tool whose central problem is convincing Windows and a
suspicious user that an unsigned `dinput8.dll` is not malware. It also means two processes, two
languages, and a lifecycle where closing the window does not stop the server.

tkinter is not in the embeddable runtime, so the bundle vendors tcl/tk: measured at +2.5 MB
zipped, and the three binaries (`tcl86t.dll`, `tk86t.dll`, `_tkinter.pyd`) are signed by the
Python Software Foundation. One process, one language, no port. It looks dated, and that was
accepted.

### Why it shells out to play.ps1

`play.ps1` deploys the shim, backs up a foreign `dinput8.dll`, starts the bridge, watches for
the game process and cleans up on every exit path including Ctrl+C. It is verified on hardware.
Porting it to Python is worth doing eventually, since one launcher is better than two, but a
rewrite whose only test is "play DiRT 4 and see" should not also be the thing carrying a new UI.

Stopping it is a **stop file**, not a kill. `Popen.terminate()` is `TerminateProcess` on
Windows, so PowerShell never unwinds and its `finally` block never runs. That block is what
kills the bridge and takes the shim back out of the game folder, so killing the child leaves a
bridge holding vJoy invisibly and an unsigned DLL in somebody's game directory. The window
touches a file `play.ps1` polls for, and kills only if that is ignored.

### Watching the shared section

`ShimView` reads the shim's shared memory and must not create it. It used to call
`mmap.mmap(-1, size, tagname=NAME, access=ACCESS_READ)`, and with fileno -1 that **creates** the
mapping when none exists, as PAGE_READONLY. Every later writer then fails: the bridge died on
`OSError [WinError 87]` a second after starting, the shim logged
`ipc: MapViewOfFile failed, err=87` from inside the game, and the window reported a running
bridge while the badge beside it said otherwise.

It only bit once the window became the thing that starts the bridge, because whoever touches
the section first creates it. `OpenFileMappingW` fails when there is nothing to open, which is
the behaviour wanted. It is opened and closed per read, so a dead bridge's section is not held
alive by the window that was only meant to watch it.

## ffb_render

The arithmetic half of the bridge. It exists as its own module because
`wheel_profile.run_software_condition` implements the same laws with the strengths baked in as
`CONDITION_GAINS`, fitted to this wheel for a menu where one effect is picked at one magnitude.
A game does not work that way: it sends its own coefficients, saturations and dead bands,
several effects at once, and expects them summed. The laws are lifted here and parameterised,
and `wheel_profile` delegates.

### Units

Everything inside is normalised:

| Quantity | Units |
|---|---|
| position, velocity, acceleration | reading units and reading units per second, as `RacingWheel.get_current_reading().wheel` produces them, -1.0 to +1.0 across full lock |
| coefficients, saturations, dead bands | fractions, not DirectInput's 0..10000 integers |
| output force | -1.0 to +1.0 |

DirectInput's integers are converted once, at the boundary, by `ConditionParams.from_di`.
Keeping DI units out of the interior is deliberate: the alternative is dividing by 10000 in a
dozen places and eventually forgetting one, which is a factor of 10000 error that presents as
"force feedback does nothing".

Coefficients are **not** clamped to DirectInput's range, because the software conditions in
`wheel_profile` legitimately use a spring gain of 2.0, measured rather than guessed. Saturation
still bounds the result, so an out of range coefficient only decides how quickly an effect
reaches full force.

### Velocity smoothing

Differentiating a quantised reading about 100 times a second is noisy, and that noise reaches
the motor as audible chatter. `WheelState.VELOCITY_SMOOTHING` is an exponential factor fitted
against logged ticks from this wheel: 1.0 is raw and makes inertia pure hash, lower is smoother
but lags the wheel.

### Parity with the old laws

`legacy_condition_params` reproduces `wheel_profile.CONDITION_GAINS` exactly, so the menu's
software conditions feel identical. Any change in feel there is a bug rather than a tuning
opportunity, and `test_ffb_render.py` compares the two over a sweep.

Friction is the odd one. The old law was direction only, a flat gain outside a velocity dead
band with no proportionality, which is expressible here as a very steep coefficient saturating
immediately. Same output, no special case in the formula.

One deliberate difference: at exactly the dead band edge the old law is already at full drag,
because its test was `abs(v) < 0.05`, while this returns 0 there and full drag an epsilon
beyond. The transition is 1e-6 wide, the function is discontinuous at that point either way,
and a velocity landing on exactly 0.05 has measure zero. Documented rather than special cased,
because a `friction` branch in the formula would have to be maintained forever to buy nothing.

## Adding a game

A game's entry has two halves. `games.json` holds the machine-readable part and generates both
the GUI's notes panel and GAMES.md's status table, so it is edited first and the table is
regenerated with `packaging\gen_games_table.py`. GAMES.md holds the setup steps by hand.

Keep GAMES.md to what a user does: the steps, the values, and what is still wrong. Measurements
belong in [tuning.md](tuning.md) or [hardware.md](hardware.md), and unknowns stay unknowns,
because a blank is more useful than a guess.

```markdown
## Game name

**Status:** one line on what does and does not work.

**Tested on:** store and build.

### Setup
Numbered steps, exact paths and exact text. Say "nothing" if it just works.

### Settings
The `tune.json` values, and the in-game settings that matter.

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
| `gui.py` | The window: pick a game, start it, watch bridge and shim state, tune the feel |
| `games.json` | Per-game notes and quirks. Source for the GUI's panel **and** for GAMES.md's status table |
| `vjoy_bridge.py` | Feeds the wheel's position into vJoy, and force back out through the shim |
| `motor_sink.py` | The two ways to reach the motor: WGI directly, or the shim over shared memory |
| `ffb_render.py` | The control laws: spring, damper, friction, inertia, periodics, envelopes |
| `wgi_probe.py` | The tool: detection, message pump, effect menu, sweeps, cleanup |
| `wheel_profile.py` | Calibration, `wheel_profile.json` persistence, software condition effects |
| `vjoy_ffb_spike.py` | Verifies the vJoy force feedback path the bridge is built on |
| `live_tune.py` | `tune.json` reloading and the wheel-button tuning controls |
| `tune_report.py` | Reads a session log and reports what the force feedback actually did |
| `probe_log.py` | Session logging to `logs/` |
| `stiction_test.py` | Measures the wheel's breakaway force, which is where `min_force` comes from |
| `dinput_abi.py` | ctypes binding for DirectInput 8, used by `vjoy_ffb_spike.py` |
| `shim/` | The `dinput8.dll` proxy: it is what runs inside the game process |
| [`evidence/`](../evidence/README.md) | Four APIs that do **not** work on this wheel, and the probes that prove it |

Anything load bearing has been written into `evidence/README.md` or into comments beside the
code it constrains, so the reasoning sits next to the thing it explains rather than in a
document that goes stale on its own.
