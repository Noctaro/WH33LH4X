# Command reference

Every runnable script in this project, what it is for, and every flag it takes. Generated from
the source, so the flags and defaults here are the ones the code actually parses.

Ordered by what you are trying to do rather than alphabetically. If you only ever read one
section, read the first.

**In the downloadable bundle**, run things through the bundled interpreter, which needs no
Python installed:

```
WH33LH4X.cmd -Game "C:\...\dirt4.exe"
.\python\python.exe vjoy_ffb_spike.py
```

**In a source checkout**, use the venv:

```
.\play.ps1 -Game "C:\...\dirt4.exe"
.\.venv\Scripts\python.exe vjoy_ffb_spike.py
```

Force feedback tuning is not a flag. It lives in `tune.json` and is re-read within half a
second of a save, so it changes mid-corner without restarting anything — see
[docs/tuning.md](docs/tuning.md).

---

## 1. Playing a game

### `WH33LH4X-GUI.cmd` — the window

Ships in the downloadable bundle. Pick a game, press Start, watch three status badges. It shells out to `play.ps1` below
rather than reimplementing it, so everything true of `play.ps1` is true here too.

What it adds over the command line:

- **Steam titles are handled for you.** It passes `-NoLaunch` and asks the client to start
  the game, because Steam DRM relaunches the exe as a different process and a direct
  launch looks like the game quitting instantly.
- **Per-game notes** from `games.json` — the quirks that otherwise cost you an evening.
- **Spring, Damper and Friction** sliders, written straight to `tune.json` as you move them.
- **Status badges** read from the shim's shared section: vJoy, bridge, shim.

Takes no arguments. Runs under `pythonw.exe` so no console sits behind it, and it starts
the bridge with `-Quiet` so none appears for that either.

### `play.ps1` — start the bridge, run a game, clean up after

Ships in the downloadable bundle. Also reachable as `WH33LH4X.cmd`, which is the same script with the PowerShell execution
policy handled — a `.ps1` that arrived inside a downloaded zip will not run on a double-click
without it.

Deploys the shim into the game folder before launch and **removes it again when the game
exits**, even on a crash or Ctrl+C. If another tool's `dinput8.dll` is already there (ReShade
uses the same filename) it is moved aside and put back afterwards.

Run it with no arguments for usage.

| Parameter | Type | Default | What it does |
|---|---|---|---|
| `-Game` | string | — | Path to the game exe. Deploys the shim beside it and launches it. |
| `-Gain` | double | `1.0` | Motor master gain. LATCHED when the shim loads the effect, so it cannot change while you drive -- leave it open and let `max_force` do the limiting. |
| `-MaxForce` | double | `0.6` | STARTING cap on commanded force. `tune.json` overrides this live. |
| `-KeepDll` | switch | — | Leave the shim in the game folder on exit instead of removing it. |
| `-NoBridge` | switch | — | Deploy the shim but do not start the Python bridge. |
| `-BridgeOnly` | switch | — | Start the bridge and nothing else. The old no-argument behaviour. |
| `-NoLaunch` | switch | — | Deploy and start the bridge, but launch the game yourself -- what you want for a Steam title. |
| `-NoFfb` | switch | — | Feed the axes but never take the motor. USE THIS WHILE BINDING CONTROLS. |
| `-StartTimeout` | int | `120` | Seconds to wait for the game process to appear. Steam can be slow. |
| `-Quiet` | switch | — | — |

### `WH33LH4X.cmd`

Ships in the downloadable bundle. Takes no arguments of its own — everything is forwarded to `play.ps1` above.

---

## 2. The bridge itself

### `vjoy_bridge.py` — present the wheel to DirectInput games through vJoy

Ships in the downloadable bundle. Normally started for you by `play.ps1`. Run it directly to debug, or to use a flag
`play.ps1` does not expose.

| Flag | Default | What it does |
|---|---|---|
| `--device` | `1` | vJoy device id (default 1) |
| `--sweep` | — | BINDING HELPER: move one vJoy control synthetically, ignoring the real wheel entirely, so a game can bind it while the GAME has focus. Works around the catch-22 where the game only watches for movement while focused but we can only read the wheel while WE are. Bind with this, then play with the normal feeder. |
| `--sweep-seconds` | `0.0` | stop sweeping after N seconds (default: until Ctrl+C) |
| `--rate` | `100.0` | feed rate in Hz (default 100) |
| `--wait` | `30.0` | detection timeout |
| `--dry-run` | — | read the wheel and report, but write nothing to vJoy |
| `--no-ffb` | — | input path only; never take the motor |
| `--sink` `ipc`/`wgi` | `ipc` | where force goes. 'ipc' (default) publishes to the shim inside the game, which is the only thing that works with a game running. 'wgi' drives the motor from this process and only produces torque while OUR window is in front -- diagnostics only. |
| `--no-grab` | — | never take the foreground. Force output will be silent unless you click the probe window yourself, but nothing steals your keyboard. |
| `--run-seconds` | `0.0` | stop automatically after N seconds (default: run until Ctrl+C). Useful because an effect playing takes the foreground, which is also where your Ctrl+C would have gone. |
| `--gain` | `0.5` | motor master gain 0.0-1.0 (default 0.5). Latched when the effect is loaded, so changing it needs a reload. |
| `--max-force` | `0.6` | STARTING cap on commanded force 0.0-1.0 (default 0.6). Multiplies with --gain, so the default is about 30%% of what the wheel can do. Unlike --gain this one is live: it seeds tune.json's max_force, and the file wins from then on. |
| `--tune` | — | live tuning file, re-read while running (default: tune.json next to this script). Edit mid-corner; changes apply within half a second. |
| `--no-log` | — | — |

### `tune_report.py` — say what the force feedback actually did

Ships in the downloadable bundle. Reads a bridge session log and reports which effects the game sent, whether output ever
went negative, and the correlation between steering angle and force. That last number is the
objective test for centring: a wheel whose output never changes sign cannot centre, however it
felt.

| Argument | Default | What it does |
|---|---|---|
| _(positional)_ | newest log in `logs/` | Path to the bridge session log to analyse |

---

## 3. Checking the setup

### `vjoy_ffb_spike.py` — does the vJoy force-feedback path work?

Ships in the downloadable bundle. **Run this first when anything is wrong.** It sends DirectInput effects to the virtual
device and logs what comes back out of vJoy's callback, verifying the whole path without a game
or the real wheel. If it does not report `PASS`, nothing built on top of vJoy will work.

| Flag | Default | What it does |
|---|---|---|
| `--device` | `1` | vJoy device id (default 1) |
| `--listen` | — | do not send anything; just log what arrives (drive it from a game) |
| `--feel` | — | play the effects slowly, one at a time, with instructions, so a human can judge whether each is rendered correctly. The default run is paced for a packet log and is far too fast to feel. |
| `--hold` | `9.0` | seconds per effect in --feel mode (default 9) |
| `--gap` | `5.0` | seconds of countdown between effects in --feel mode (default 5) |
| `--send-only` | — | act purely as a DirectInput client: send effects to vJoy and do NOT acquire it or register a callback. This is how the bridge gets tested -- only one process can own a vJoy device, so the bridge holds it and this stands in for the game. |
| `--gain` | — | DirectInput effect gain 0.0-1.0. Defaults to 0.5 for the packet run and 1.0 for --feel: this gain arrives at the bridge as a byte and multiplies with the bridge's own limits, so 0.5 here is already halved before anything reaches the motor. |
| `--no-log` | — | do not write a log file |

### `wgi_probe.py` — drive the real motor directly

Ships in the downloadable bundle. Detection, an interactive effect menu, sweeps and calibration, all through
`Windows.Gaming.Input` with no game and no vJoy involved. Use it to answer "is the wheel itself
alive?" — and press `k` once per wheel to calibrate, which the software condition effects
require.

| Flag | Default | What it does |
|---|---|---|
| `--gain` | `1.0` | master gain 0.0-1.0, set before each load (default 1.0) |
| `--magnitude` | `0.3` | effect magnitude 0.0-1.0 -- the intensity control (default 0.30) |
| `--duration` | `6.0` | seconds per effect, 0 = hold until Enter (default 6) |
| `--wait` | `30.0` | detection timeout (default 30) |
| `--list-only` | — | report only, play nothing |
| `--no-log` | — | do not write a session log under logs/ |

### `shim/test_proxy.py` — is the dinput8.dll proxy transparent?

Repo only — not in the bundle. Drives the proxy directly and compares its device enumeration against the system DLL in the
same process, so a crippled proxy is caught here rather than looking like a game bug.

| Flag | Default | What it does |
|---|---|---|
| `--keep-log` | — | do not clear the shim log before running |

### `shim/run_shim.py` — exercise the shim without a game

Repo only — not in the bundle. Hosts the shim outside a game so its WGI layer can be tested without launching one.

| Flag | Default | What it does |
|---|---|---|
| `--seconds` | `15.0` | how long to keep the host alive (default 15) |
| `--keep-log` | — | do not clear the shim log first |
| `--winrt-assist` | — | also subscribe from Python, to test whether the C needs to at all |
| `--drive` | — | act as the bridge too: publish a force sweep over the section |

---

## 4. Building

### `shim/build.ps1` — build the dinput8.dll proxy

Repo only — not in the bundle. Needs zig (from `tools/zig`, `$env:ZIG`, or `PATH`) and a Windows SDK for its WinRT headers.
Takes no parameters.

### `packaging/build_bundle.ps1` — assemble the downloadable bundle

Repo only — not in the bundle. Downloads and verifies embeddable CPython, vendors the pinned dependencies, stages the
source and the shim, and zips the result.

| Parameter | Type | Default | What it does |
|---|---|---|---|
| `-OutDir` | string | — | Where to build. Defaults to `dist/`. |
| `-CacheDir` | string | — | Where the embeddable Python zip is cached between runs. |
| `-SkipZip` | switch | — | Leave the staged folder, skip creating the archive. |

---

## 5. Modules with no command line

Imported by the scripts above; nothing to run.

| Module | Purpose | In bundle |
|---|---|---|
| `ffb_render.py` | The force-feedback control laws, in normalised units | yes |
| `motor_sink.py` | The one place that actually touches the wheel's motor | yes |
| `live_tune.py` | `tune.json` reloading and the wheel-button tuning controls | yes |
| `wheel_profile.py` | Per-wheel calibration, saved once and reused | yes |
| `probe_log.py` | Session logging to `logs/` | yes |
| `dinput_abi.py` | ctypes transcription of the DirectInput 8 API surface | yes |
| `gameinput_abi.py` | ctypes transcription of Microsoft's GameInput API (v0 ABI) | no |
| `gip_protocol.py` | The GIP wire format for this wheel | no |

---

## 6. Diagnostics, and the dead ends kept as evidence

None of these ship in the bundle.

Everything below marked with a path lives in [`evidence/`](evidence/README.md) and
records an API that **does not work** on this wheel. Run those as modules, from the repo
root -- they import from it, so a direct path will not resolve:

```
.\.venv\Scripts\python.exe -m evidence.hid_probe
```

Read [`evidence/README.md`](evidence/README.md) first: it answers each question in a
sentence, which is usually all anyone needs. The code is there so the answers stay checkable
against a newer runtime or a different wheel.

### `test_ffb_render.py` — the control laws still do what they used to

Run before proposing a change to `ffb_render.py`. No test framework, no dependencies, no
hardware: `.\.venv\Scripts\python.exe test_ffb_render.py`. Takes no flags.

### `test_shimview.py` — the GUI must not create the shared section

No hardware, no wheel, no game. Asserts the ORDER that broke a real session rather than
the parts: a viewer touching the section before any writer must not bring it into
existence, or every later writer fails with `WinError 87` and the game gets no wheel
input.

**It skips itself when a bridge is already running.** It opens a real sink, and closing
one zeroes the bridge heartbeat -- against a live session that is a force-feedback
dropout. Takes no flags.

### `evidence/probe.py` — does GameInput expose force-feedback motors? (**no**)

| Flag | Default | What it does |
|---|---|---|
| `--vid` | — | select device by vendor id, e.g. 0x0F0D |
| `--pid` | — | select device by product id, e.g. 0x015D |
| `--gain` | `0.35` | motor master gain 0.0-1.0 (default 0.35, deliberately gentle) |
| `--magnitude` | `0.3` | effect magnitude 0.0-1.0 (default 0.30) |
| `--duration` | `2.0` | seconds to hold each effect (default 2.0) |
| `--motor` | `0` | motor index (default 0) |
| `--list-only` | — | enumerate and report only; create no effects at all |
| `--no-script` | — | skip the scripted constant/spring test, go straight to the menu |
| `--oem` | — | call EnableOemDeviceSupport for VID:PID before enumerating |
| `--dll` | `GameInput.dll` | which GameInput runtime to load. Default is the inbox v0 GameInput.dll. 'GameInputRedist.dll' is the newer v3 runtime, which exports GameInputCreate as well -- worth a try when v0 reports no force-feedback motors, but the v3 ABI is NOT the same and the struct layout check may (correctly) refuse it. |

### `evidence/dinput_probe.py` — does DirectInput expose force feedback on this wheel? (**no**)

| Flag | Default | What it does |
|---|---|---|
| `--gain` | `0.35` | effect gain 0.0-1.0 (default 0.35) |
| `--magnitude` | `0.3` | effect magnitude 0.0-1.0 (default 0.30) |
| `--duration` | `2.0` | seconds to hold each effect (default 2.0) |
| `--vid` | — | select by vendor id |
| `--list-only` | — | stage 1 only: report capabilities, create nothing |

### `evidence/hid_probe.py` — does the device publish a USB PID force-feedback collection?

Reads raw HID report descriptors, and shows *why* the two answers above are no.

| Flag | Default | What it does |
|---|---|---|
| `--vid` | `3853` | vendor id to inspect (default 0x0F0D, HORI) |
| `--all` | — | inspect every HID device |

### `evidence/wgi_background_test.py` — does WGI still work when we are not in front? (**no**)

The measurement behind the whole shim design: force output and position reading are both gated
on foreground, which is why the output stage has to live inside the game's process.

| Flag | Default | What it does |
|---|---|---|
| `--seconds` | `12.0` | length of each phase (default 12) |
| `--no-force` | — | test readings only; never touch the motor |
| `--force-only` | — | skip the reading phases and run only the A->B->A force gate test |
| `--reading-only` | — | run only the A->B->A reading gate test; never touch the motor |
| `--gain` | `0.5` | motor master gain (default 0.5) |
| `--magnitude` | `0.35` | constant-force magnitude (default 0.35) |
| `--wait` | `30.0` | detection timeout |
| `--no-log` | — | — |

### `stiction_test.py` — how much force does it take to move this wheel at all?

Measures the motor's own breakaway friction, which is where `tune.json`'s `min_force` comes
from.

| Flag | Default | What it does |
|---|---|---|
| `--max` | `0.6` | highest force to try, 0.0-1.0 (default 0.60) |
| `--step` | `0.02` | force increment per attempt (default 0.02). MEASURED 2026-08-25: on a Hori Force Feedback Racing Wheel DLX, 0.02 completed 9 of 9 runs while 0.01 completed 1 to 2 of 4 before the motor stalled, and both report the first step tried -- a finer step buys risk, not detail. Other wheels may take a smaller step safely. |
| `--passes` | `3` | measurements per direction (default 3); stiction scatters |
| `--gain` | `1.0` | motor master gain (default 1.0 -- measure the hardware, not a gain) |
| `--wait` | `20.0` | device wait timeout |
| `--selftest` | `0` | skip measuring; probe for torque N times against one held effect and report how many worked. Use this to judge a reliability change. |
| `--reopen` | — | with --selftest, close and reopen the motor for every probe. This REPRODUCES THE BUG (2/10 on 2026-08-25) and is kept for that. |
| `--stall-floor` | `0.02` | smallest --step believed safe on THIS wheel (default 0.02, measured on a Hori Force Feedback Racing Wheel DLX). Below it, levels that cannot move the wheel silence the motor for the rest of the process. Raise or lower it for other hardware; it only warns. |
| `--ramp-in-place` | — | do not free the wheel between levels; push again from the same rotor position. That is what silences the motor, and it is kept only so pre-2026-08-25 runs stay comparable. |
| `--no-log` | — | — |

### `evidence/gip_trace.py` — watch what WGI sends the wheel

| Flag | Default | What it does |
|---|---|---|
| `--hold` | `1.5` | seconds to hold each magnitude (default 1.5) |
| `--reads` | — | also hook ReadFile/ReadFileEx (noisy; changes timing) |
| `--wait` | `8.0` | seconds to wait for the wheel to appear (default 8) |
| `--no-log` | — | do not write a session log |
| `--dump` | — | write every captured write, in order, for comparison with gip_direct |

### `evidence/gip_direct.py` — talk to the GIP driver with no WGI in the process (**dead end**)

The arming sequence was replayed byte-for-byte and still produced no torque. Do not retry this
without new information.

| Flag | Default | What it does |
|---|---|---|
| `--diag` | — | why are reads producing nothing? (needs no device id) |
| `--force` | — | replay the load and the magnitude sequence |
| `--background` | — | A->B->A foreground test on direct output |
| `--hold` | `2.5` | seconds per phase (default 2.5) |
| `--listen` | `4.0` | seconds to listen in the default mode (default 4) |
| `--no-log` | — | do not write a log file |
| `--dump` | — | write every message sent, in order, for comparison with gip_trace |

### `evidence/gip_diff.py` — compare the two captures above

| Flag | Default | What it does |
|---|---|---|
| `wgi` | — | dump from gip_trace.py --dump (force works) |
| `ours` | — | dump from gip_direct.py --dump (force does not) |
