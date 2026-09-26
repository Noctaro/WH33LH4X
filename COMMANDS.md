# Command reference

Every runnable script in this project, what it is for, and every flag it takes. Generated from
the source, so the flags and defaults here are the ones the code actually parses.

Ordered by what you are trying to do rather than alphabetically. If you only ever read one
section, read the first.

**In the downloadable bundle**, use the two launchers, or the bundled interpreter, which needs
no Python installed:

```
WH33LH4X-GUI.cmd
.\python\python.exe vjoy_ffb_spike.py
```

**In a source checkout**, use the venv:

```
.\.venv\Scripts\pythonw.exe -m ui
.\.venv\Scripts\python.exe vjoy_ffb_spike.py
```

Force feedback tuning is not a flag. It lives in `user-tune.json`, created from `tune.json` the
first time, and is re-read within half a second of a save, so it changes mid-corner without
restarting anything — see [docs/tuning.md](docs/tuning.md).

---

## 1. Driving

### `WH33LH4X-GUI.cmd` / `python -m ui` — the window

Ships in the downloadable bundle. Pick a profile, press Start, drive. Start and Stop run the bridge below as a hidden process,
and closing the window stops it too.

- **Profiles**: Default, DiRT 4 and RC car, chosen by hand. The sliders apply live; a profile
  file only changes on Save or Save as new.
- **Setup checks** for vJoy device 1 and the wheel's WinUSB binding, each with a How to fix.
  On Linux it checks that xone has let go of the wheel and that USB autosuspend is off.
- **Test force** pushes the wheel briefly right and left and reports whether it moved the
  right way.
- **Restore Microsoft driver** removes the WinUSB driver so Xbox games and the HORI app see the
  wheel again.

Takes no arguments. Runs under `pythonw.exe` so no console sits behind it.

### `WH33LH4X.cmd` / `python -m bridge` — the bridge without the window

Ships in the downloadable bundle. Owns the wheel over raw USB (WinUSB on Windows, xone unbound on Linux) and presents it
through vJoy. For running headless or with flags the window does not expose; `WH33LH4X.cmd`
passes its arguments through.

| Flag | Default | What it does |
|---|---|---|
| `--frontend` `vjoy`/`none` | — | what games see: 'vjoy' (default on Windows) or 'none', the tune file's spring with no game (default elsewhere) |
| `--device` | `1` | vJoy device id (default 1) |
| `--rate` | `250.0` | bridge loop rate in Hz (default 250, the wheel's write rate) |
| `--gain` | `1.0` | motor gain 0.0-1.0 after max_force (default 1.0); the raw USB hard cap still applies |
| `--tune` | — | live tuning file, re-read while running (default: user-tune.json, created from tune.json) |
| `--stop-file` | — | stop cleanly when this file appears, and delete it |
| `--run-seconds` | `0.0` | stop after N seconds (default: run until Ctrl+C) |
| `--no-ffb` | — | input only; never command force |
| `--dry-run` | — | read the wheel and report; write nothing to vJoy, no force |
| `--trace` | — | write time, position and force per tick, like wheel_trace.txt |
| `--no-log` | — | — |

### `python -m bridge.nudge` — test force

Ships in the downloadable bundle. Arms the wheel, pushes it briefly right and then left with a spring in between, and prints
`nudge ok right=+0.2 left=-0.2`, or `reversed`, `still`, `unclear` or `silent` (no input).
Hands off the wheel. The window's Test force button runs this. Takes no flags.

### `tune_report.py` — say what the force feedback actually did

Ships in the downloadable bundle. Reads a bridge session log and reports which effects the game sent, whether output ever
went negative, and the correlation between steering angle and force. That last number is the
objective test for centring: a wheel whose output never changes sign cannot centre, however it
felt.

| Argument | Default | What it does |
|---|---|---|
| _(positional)_ | newest log in `logs/` | Path to the bridge session log to analyse |

---

## 2. Checking the setup

### `vjoy_ffb_spike.py` — does the vJoy force-feedback path work?

Ships in the downloadable bundle. **Run this first when a game gets no force feedback.** It sends DirectInput effects to the
virtual device and logs what comes back out of vJoy's callback, verifying the whole path
without a game or the real wheel. If it does not report `PASS`, nothing built on top of vJoy
will work.

| Flag | Default | What it does |
|---|---|---|
| `--device` | `1` | vJoy device id (default 1) |
| `--listen` | — | do not send anything; just log what arrives (drive it from a game) |
| `--feel` | — | play the effects slowly, one at a time, with instructions, so a human can judge whether each is rendered correctly. The default run is paced for a packet log and is far too fast to feel. |
| `--hold` | `9.0` | seconds per effect in --feel mode (default 9) |
| `--gap` | `5.0` | seconds of countdown between effects in --feel mode (default 5) |
| `--send-only` | — | act purely as a DirectInput client: send effects to vJoy and do NOT acquire it or register a callback. This is how the bridge gets tested: only one process can own a vJoy device, so the bridge holds it and this stands in for the game. |
| `--gain` | — | DirectInput effect gain 0.0-1.0. Defaults to 0.5 for the packet run and 1.0 for --feel: this gain arrives at the bridge as a byte and multiplies with the bridge's own limits, so 0.5 here is already halved before anything reaches the motor. |
| `--no-log` | — | do not write a log file |

---

## 3. Building

### `packaging/build_bundle.ps1` — assemble the downloadable bundle

Repo only — not in the bundle. Downloads and verifies embeddable CPython, vendors the pinned dependencies, stages the
source, writes the two launchers, and zips the result.

| Parameter | Type | Default | What it does |
|---|---|---|---|
| `-OutDir` | string | — | Where to build. Defaults to `dist/`. |
| `-CacheDir` | string | — | Where the embeddable Python zip is cached between runs. |
| `-SkipZip` | switch | — | Leave the staged folder, skip creating the archive. |

---

## 4. Modules with no command line

Imported by the commands above; nothing to run.

| Module | Purpose | In bundle |
|---|---|---|
| `bridge/` | The bridge loop, the wheel over raw USB, the vJoy front-end | yes |
| `gip/` | The GIP protocol: framing, arming, USB host, input reports | yes |
| `ui/` | The window: profiles, the bridge process, setup checks | yes |
| `ffb_render.py` | The force-feedback control laws, in normalised units | yes |
| `live_tune.py` | Tuning file reloading and the wheel-button tuning controls | yes |
| `probe_log.py` | Session logging to `logs/` | yes |
| `dinput_abi.py` | ctypes transcription of the DirectInput 8 API surface | yes |
| `evidence/gameinput_abi.py` | ctypes transcription of the GameInput API (v0 ABI) | no |
| `evidence/gip_protocol.py` | The GIP wire format as captured above the driver | no |

---

## 5. Tests

None of these need hardware, and CI runs all four.

### `test_ffb_render.py` — the control laws still do what they used to

Run before proposing a change to `ffb_render.py` or `live_tune.py`. No test framework, no
dependencies: `.\.venv\Scripts\python.exe test_ffb_render.py`. Takes no flags.

### `test_gip.py` — the same bytes still go on the wire

Run before proposing a change to `gip/`. Pins the arming and force bytes that drove the motor,
and the input report decoding. No pyusb needed: `.\.venv\Scripts\python.exe test_gip.py`.

### `test_bridge_core.py` — the bridge loop against a fake wheel

Run before proposing a change to `bridge/`. Force sign, the stop file, and zero force before
the wheel is released: `.\.venv\Scripts\python.exe test_bridge_core.py`.

### `test_ui.py` — profiles and the bridge process, without a window

Run before proposing a change to `ui/` or `profiles/`:
`.\.venv\Scripts\python.exe test_ui.py`.

---

## 6. Evidence

None of these ship in the bundle. They live in [`evidence/`](evidence/README.md): the dead ends
that prove why the other APIs cannot drive this wheel, and the raw USB instruments the working
route was found with. Run the Windows ones as modules from the repo root, since they import
from it:

```
.\.venv\Scripts\python.exe -m evidence.hid_probe
```

Read [`evidence/README.md`](evidence/README.md) first: it answers each question in a
sentence, which is usually all anyone needs.

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

### `evidence/gip_diff.py` — compare two GIP captures

| Flag | Default | What it does |
|---|---|---|
| `wgi` | — | dump from gip_trace.py --dump (force works) |
| `ours` | — | dump from gip_direct.py --dump (force does not) |

### `evidence/gip_wheel_driver.py` — the first raw USB driver (**Linux**)

Owns the wheel over raw USB, publishes a virtual joystick, and holds a spring and damper on
the motor. The defaults are the tuned values. [`evidence/RAW_USB.md`](evidence/RAW_USB.md)
explains how it got there.

| Flag | Default | What it does |
|---|---|---|
| `--spring` | `0.25` | centring stiffness; 0 leaves the wheel free (default 0.25) |
| `--damper` | `0.08` | resistance to turning speed (default 0.08) |
| `--cap` | — | — |
| `--min-force` | `0.05` | smallest magnitude worth commanding outside the deadband; 0 disables (default 0.05) |
| `--force-step` | `0.001` | smallest force change worth sending. Near centre the demand is only 0.01-0.03, so a coarse step is a large fraction of it and is felt as notching (default 0.001) |
| `--floor-ramp` | `0.05` | distance over which --min-force fades in from centre; a hard floor flips sign across centre and is felt as a step (0 restores the hard floor) |
| `--coast` | `0.0` | stop pushing once moving towards centre faster than this, so momentum finishes the job (0 disables) |
| `--release` | `0.0` | once settled near centre, stay quiet until the wheel is moved this far out (default 0.05) |
| `--no-unstick` | — | do not kick the wheel off cogging detents |
| `--deadband` | `0.0` | spring dead zone around centre, and the settle gate that goes with it; 0 disables both (default 0) |
| `--refresh` | `0.0` | seconds between full effect reloads; each one tears the effect down and rebuilds it, felt as a distinct cogging step. 0 = never (default 0) |
| `--no-smooth` | — | re-run the full effect load for every change |
| `--trace` | — | log time, position and demand to wheel_trace.txt |
| `--wait-for` | — | after arming, hold still until this file appears |
| `--seconds` | `0.0` | run for this long; 0 means until Ctrl+C |
| `--wait-calibration` | `0.0` | wait for a firmware calibration sweep first. Claiming the device does NOT trigger one, so this normally just times out (default 0) |

### `evidence/gip_hold.py` — drive to a position and hold it (**Linux**)

The sweep half of the Linux acceptance test: drives to each target slowly and logs where the
wheel really is.

| Flag | Default | What it does |
|---|---|---|
| `--targets` | `0.5` | comma-separated positions, full scale, positive right; lock is ~180 deg so 0.5 is ~90 deg (default 0.5) |
| `--settle` | `8.0` | seconds after arming with no force at all (default 8) |
| `--ramp` | `5.0` | seconds to move out and back |
| `--hold` | `15.0` | seconds at the target |
| `--spring` | `2.0` | gain per unit of error |
| `--damper` | `0.1` | — |
| `--cap` | `0.35` | — |

### `evidence/gip_usb_host.py` — the raw USB instrument (**Linux**)

Claiming, power-on, wire replay, force scaling and closed-loop position control. This is what
the findings were measured with.

### `evidence/usbpcap_parse.py` — read a USBPcap capture

Parses `DLT_USBPCAP` records and filters by device and endpoint. This is what showed that the
old WGI capture is not what reaches the wire, which is the discovery the raw USB route rests
on.
