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
