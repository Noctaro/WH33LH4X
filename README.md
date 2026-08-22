# WH33LH4X

Drives a HORI FFB Racing Wheel's motor from Windows, outside any game engine, using
`Windows.Gaming.Input`. Constant force and ramp work natively; spring/damper/friction/
inertia are computed in software because the firmware's own versions push the wrong way.

Hardware: HORI FFB Racing Wheel Series X (`VID 0x0F0D`). The wheel must be in **Xbox mode**
(`PID 0x015C`) — a long-press of the PROFILE button switches from PC mode, which has no
force-feedback interface at all. `wgi_probe.py --list-only` will tell you which mode it's in.

## Setup

No compiler, no admin rights — just a venv with the WinRT projections:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install winrt-Windows.Gaming.Input `
    winrt-Windows.Gaming.Input.ForceFeedback winrt-Windows.Foundation `
    winrt-Windows.Foundation.Collections winrt-Windows.Foundation.Numerics
```

## Running it

```powershell
.\.venv\Scripts\python.exe wgi_probe.py            # detect, report, interactive menu
.\.venv\Scripts\python.exe wgi_probe.py --list-only # detect and report only, play nothing
```

**First run: press `k` to calibrate.** This measures which way the wheel's own position
reading moves when force is applied, and saves it to `wheel_profile.json` keyed by the
wheel's VID:PID. It only needs to run once per wheel; the software effects (`1s`–`4s`,
`y`, `z`) refuse to run without it.

**Keep the probe window in front while an effect plays.** Force output — and, during
calibration, position readings — both stop the instant the window loses foreground. The
window re-grabs it automatically and prints `[foreground OK]` / `[!! NOT foreground !!]`
each second so you can see the state instead of guessing.

**Releasing the wheel feels looser, not weaker.** At idle the firmware runs its own strong
centering spring. The moment any effect takes the motor, that spring is suspended — so even
a gentle effect feels looser than the resting wheel. Press `r` to give the motor back to the
firmware and get stock centering again.

## Menu reference

| Keys | What |
|---|---|
| `1`–`11` | Firmware effects (constant, ramp, periodics, conditions) |
| `1s` `2s` `3s` `4s` | Software conditions: spring, damper, friction, inertia |
| `y` `z` | Software sine / square wave |
| `k` | Calibrate (run this first) |
| `i` | Motor info |
| `w` `e` `x` | Gain sweep / magnitude sweep / direction test |
| `g` `m` `d` `f` | Set gain / magnitude / duration / frequency |
| `c` | Cycle firmware condition sign convention (firmware ignores it — diagnostic only) |
| `r` | Release the motor, restore firmware centering |
| `s` | Stop all effects |
| `q` | Quit |

## Effect support on this firmware

| Effect | Result |
|---|---|
| Constant force | Works — direction and magnitude both honoured |
| Ramp force | Works — must sweep through zero to be felt |
| Sine / square / triangle / sawtooth | Silent on firmware — loads, runs, no torque. Sine and square are synthesised instead (`y` / `z`) |
| Spring / damper / friction / inertia | **Inverted** on firmware — adds force to your motion instead of resisting it. All four work as software effects (`1s`–`4s`) |

The software effects hold one `ConstantForceEffect` open and rewrite its magnitude ~80×/sec
from the wheel's own position reading — the same technique games use to drive hardware that
only implements constant force. Because the force is computed from the motion it's meant to
oppose, a sign error makes an effect inert rather than dangerous.

One expected side effect: while a software effect holds the motor there's no centering, so a
zero-mean waveform (sine/square) lets the wheel drift. A real game would run a spring effect
underneath to counter that.

## Logging

Every run writes `logs/wgi_probe_<timestamp>.log` — console output with elapsed timestamps,
plus dense per-tick data (position, velocity, commanded force, foreground state, effect
state) that isn't printed live. Flushed per line, so `Ctrl+C` still leaves a complete log.
Pass `--no-log` to disable it.

## Files

| File | Purpose |
|---|---|
| `wgi_probe.py` | The tool: detection, message pump, effect menu, sweeps, cleanup |
| `wheel_profile.py` | Calibration, `wheel_profile.json` persistence, software condition effects |
| `probe_log.py` | Session logging to `logs/` |
| `hid_probe.py` | Reads raw HID report descriptors — shows why DirectInput/GameInput can't reach this wheel |
| `probe.py` + `gameinput_abi.py` | GameInput diagnostic (negative result, kept as evidence) |
| `dinput_probe.py` + `dinput_abi.py` | DirectInput 8 diagnostic (negative result, kept as evidence) |

Why the other three APIs fail, the discovery process, and every gotcha found along the way
are written up in `.claude/memory/` rather than here — see `hori-wheel-ffb-probe-goal.md`
and `wgi-forcefeedback-api-gotchas.md` if you want the full story instead of just the
result.
