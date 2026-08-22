# WH33LH4X

**Driving a HORI FFB Racing Wheel from Windows**

**Answer: use `Windows.Gaming.Input`, with the wheel in Xbox mode.**

This started as "can GameInput drive force feedback on this wheel outside a game engine?"
The answer to that turned out to be no — but three other APIs got tested along the way and
one of them works completely: real, directional, scalable torque.

Hardware: HORI FFB Racing Wheel Series X (`VID 0x0F0D`), which enumerates as `PID 0x015D`
in PC mode and `PID 0x015C` in Xbox mode. A long-press of the PROFILE button switches.

## Result

| API | Result |
|---|---|
| **`Windows.Gaming.Input`** | ✅ **Works.** Directional torque, smooth intensity scaling |
| GameInput | ❌ `forceFeedbackMotorCount = 0` in both modes |
| DirectInput 8 | ❌ No `DIDC_FORCEFEEDBACK`; not enumerated at all in Xbox mode |
| Raw HID (USB PID) | ❌ No usage page `0x0F` collection anywhere |

Confirmed by hand on the wheel:

- **Direction** — alternating `+1.0` / `-1.0` pulls hard left and hard right, correctly
- **Full force** — the wheel is genuinely strong at magnitude `1.0`
- **Intensity scaling** — magnitude `0.1 → 1.0` scales smoothly

## Why three APIs failed and this one works

DirectInput and GameInput both route PC force feedback through the **USB PID HID class**.
`hid_probe.py` reads the wheel's actual report descriptors, and in PC mode it publishes:

| Collection | Usage page | Input | Output |
|---|---|---|---|
| COL01 | `0x01` Generic Desktop / Joystick | 18 B | **0** |
| COL02 | `0xFF20` vendor-defined | 32 B | 32 B |
| COL03 | `0xFF21` vendor-defined | 64 B | 64 B |

**Usage page `0x0F` appears nowhere**, and the joystick collection is input-only. So no
PID-based API can ever drive this wheel. `joy.cpl` showing no Force Feedback tab,
DirectInput reporting no `DIDC_FORCEFEEDBACK`, and GameInput reporting zero motors were
all *correct behaviour* — nothing was broken or blocked.

`Windows.Gaming.Input` is a different stack: its `ForceFeedbackMotor` comes from the **GIP
driver** for Xbox-licensed devices, not from HID descriptors. That is also why HORI's own
Microsoft Store app can drive the wheel — Store/UWP apps cannot use DirectInput at all, so
WGI is the only API available to them.

WGI also understands the hardware better than GameInput did, reporting a clutch and
handbrake that GameInput said were absent:

```
Xbox One Game Controller   VID 0x0F0D  PID 0x015C
  force_feedback_motors : 1
  is_enabled            : True
  supported_axes        : X
RacingWheel[0]  max_wheel_angle=900.0  clutch=True  handbrake=True
  wheel_motor : present
```

## Three gotchas that make this look like unsupported hardware

Each of these produces a silent failure that is easy to misread as "the wheel can't do it".

**1. You need a Win32 message pump.** WGI delivers device-arrival notifications through
the message loop. A console process has none — the console window belongs to `conhost.exe`,
not to your program — so the device lists stay permanently empty. `wgi_probe.py` creates
its own window on a background thread and pumps it.

**2. The window must be foregrounded.** Showing it without activating
(`SW_SHOWNOACTIVATE`) is not enough; enumeration stays empty, and force output stops when
the process loses foreground. This is why an effect can load and take the motor — the
wheel goes loose — while producing no torque at all.

**3. `master_gain` is latched at effect *load* time.** Setting it under a running effect
does nothing. Set it *before* `LoadEffectAsync`, or ignore it and scale the effect vector
instead, which is what this tool now does by default.

## Also worth knowing: "loose" means you have control

At idle the wheel applies its own firmware auto-centering — the strong spring you normally
feel. The moment an effect takes the motor, that firmware behaviour is **suspended** and
your effect owns the wheel. A gentle effect therefore feels *looser* than the resting
wheel. That is what taking control feels like, not a weak effect. Press `r` in the menu to
release the motor and get the stock spring back.

## Minimal working recipe

```python
import winrt.windows.gaming.input as gi
import winrt.windows.gaming.input.forcefeedback as ff
from winrt.windows.foundation.numerics import Vector3
from datetime import timedelta

# 1. Have a window pumping messages, foregrounded (see PumpThread in wgi_probe.py).
# 2. Then:
controller = list(gi.RawGameController.raw_game_controllers)[0]
motor = list(controller.force_feedback_motors)[0]

await motor.try_enable_async()
motor.master_gain = 1.0            # BEFORE loading, not after

effect = ff.ConstantForceEffect()
effect.set_parameters(Vector3(0.3, 0.0, 0.0),   # X axis; -1.0..1.0; sign = direction
                      timedelta(seconds=2))

if await motor.load_effect_async(effect) == ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
    effect.start()
    ...
    effect.stop()
    await motor.try_unload_effect_async(effect)

await motor.try_reset_async()      # hand the motor back to the firmware
```

`supported_axes` is `X` only on this wheel, so put force on `Vector3.x` and leave Y and Z
at zero.

## Running the tools

Setup is a venv with the WinRT projections — no compiler, no admin rights:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install winrt-Windows.Gaming.Input `
    winrt-Windows.Gaming.Input.ForceFeedback winrt-Windows.Foundation `
    winrt-Windows.Foundation.Collections winrt-Windows.Foundation.Numerics
```

```powershell
.\.venv\Scripts\python.exe wgi_probe.py            # the one that works (Xbox mode)
.\.venv\Scripts\python.exe wgi_probe.py --list-only # detect and report, play nothing
python hid_probe.py                                 # why the other APIs fail
```

Menu keys: `1`–`11` firmware effects · **`1s`–`4s` software conditions (spring, damper,
friction, inertia)** · `y`/`z` software sine/square · **`k` calibrate** · `x` direction
test · `e` magnitude sweep · `w` gain sweep · `g`/`m`/`d`/`f` adjust gain, magnitude,
duration, frequency · `r` release the motor · `s` stop all · `q` quit.

Run `k` once per wheel before using the software effects.

**Keep the probe window in front while an effect plays.** It re-grabs foreground
automatically and prints `[foreground OK]` or `[!! NOT foreground - no force !!]` each
second so you can see the state rather than guess.

## Files

| File | Purpose |
|---|---|
| `wgi_probe.py` | **The working tool.** Detection, message pump, effect menu, sweeps, cleanup guarantees. |
| `wheel_profile.py` | Calibration, `wheel_profile.json` persistence, and the software condition effects. |
| `probe_log.py` | Session logging: tee'd console output plus dense structured events. |
| `hid_probe.py` | Reads raw HID report descriptors. Explains *why* the PID-based APIs fail. Passive. |
| `probe.py` + `gameinput_abi.py` | GameInput diagnostic. Negative result, kept as evidence. |
| `dinput_probe.py` + `dinput_abi.py` | DirectInput 8 diagnostic. Negative result, kept as evidence. |

The `*_abi.py` files are deliberately boring 1:1 ctypes transcriptions of `GameInput.h`
and `dinput.h`, so their probes stay readable.

## Effect support on this firmware

Verified by hand on the wheel. The firmware honours far less than it *accepts* — most
kinds load successfully and run silently, which is the trap this whole exercise turned on.

| Effect | Firmware | Notes |
|---|---|---|
| Constant force | ✅ works | direction and magnitude both honoured |
| Ramp force | ✅ works | sweep must cross zero to be felt (`-m` → `+m`) |
| Sine / square / triangle / sawtooth up / down | ❌ silent | load `Succeeded`, state `Running`, no torque. Params verified. Synthesised in software instead (`y`/`z`) |
| Spring / damper / inertia / friction | ❌ **inverted** | produce force that *adds* to your motion instead of resisting it |

The condition effects are the worst case: not merely absent but actively wrong. The
firmware ignores the coefficient sign — all three sign conventions (docs `+/+`, flipped
direction, flipped coefficients) behave identically, accelerating the wheel in the
direction you are already turning. Nothing in the API reaches this.

## The fix: compute effects in software

Since constant force works perfectly and honours direction, everything else can be
synthesised from it. `wgi_probe.py` holds one `ConstantForceEffect` open and rewrites its
magnitude ~80×/sec from the wheel's own position reading.

**All four condition effects work this way** — spring, damper, friction and inertia,
confirmed by feel and in the logs. Menu keys `1s`–`4s`. Sine and square are synthesised the
same way (`y`/`z`), both driving the wheel at full commanded amplitude. This is how games
drive hardware that only implements constant force.

Verified from logged per-tick data rather than by feel alone:

| | commanded force | wheel reading swing |
|---|---|---|
| software sine | −0.30 .. +0.30 (mean \|f\| 0.19) | 0.632 |
| software square | −0.30 .. +0.30 (mean \|f\| 0.30) | 0.804 |

The damper's force opposed the wheel's motion on **299 of 299** moving ticks — exactly what
the firmware's own condition effects get wrong.

One expected asymmetry: while an effect holds the motor there is no centering, so a
zero-mean waveform lets the wheel drift. Real games run a spring underneath their waveforms
for this reason.

Two facts make it possible, both measured rather than assumed:

- **`set_parameters` live-updates a running effect** (unlike `master_gain`, which is
  latched at load). Verified by reversing a push mid-flight and watching the wheel turn
  around.
- **`RacingWheel.get_current_reading().wheel`** gives live position as a double, `-1.0` to
  `+1.0`, so the loop can close.

A software condition effect cannot run away with you the way the firmware ones do: the
force is derived from the motion it opposes, so a sign error makes it inert, not violent.

## Calibration

One number decides whether a closed loop resists or assists: **when we push with
`Vector3(+x)`, which way does `reading.wheel` move?** Note that "which way is physically
right" never enters it — no program can determine that, and no control law needs it.

Press `k`. Calibration measures that sign itself by pushing the wheel and watching its own
reading, plus whether live updates work, and saves both to `wheel_profile.json` keyed by
VID:PID. It is loaded automatically on every later run.

Three traps it exists to avoid, all found the hard way:

1. **Never block on Enter during a measurement.** Readings are foreground-gated exactly
   like force output, and a terminal is a separate process — so an input prompt zeroes the
   very numbers being measured. Manual steps are timed windows with a countdown.
2. **Never measure from an end stop.** The reading clamps at ±1.000 there, so a push into
   the stop registers nothing whether or not the motor is working. Calibration walks you to
   centre first, with a live gauge.
3. **Claiming and releasing the motor can leave it dead** — accepting effects, reporting
   `Succeeded` and `Running`, producing no torque. So the one measurement that needs real
   torque runs first, on an untouched motor, and every hold is followed by
   `try_reset_async` + `try_enable_async`.

## Logging

Every run writes `logs/wgi_probe_<timestamp>.log`: console output tee'd with elapsed
timestamps, plus structured per-tick data far too dense to print — position, velocity,
acceleration, commanded force, foreground state, effect state, load and unload results.
Flushed per line, so Ctrl+C still leaves a complete log. `--no-log` disables it.

This is what turned guesswork into diagnosis. Three separate dead ends were identified from
a single log each: `fg=True` on all 315 samples killed the foreground hypothesis;
`reading=+1.000` exactly, 315 times, exposed a measurement taken from an end stop; and the
same reading at `+0.000` with `state=Running` proved the motor was dead rather than
mis-commanded.

## Still open

- Nothing on the wheel itself. Every effect type is reachable, by firmware or in software.
- **PC mode force feedback.** Only Xbox mode works through WGI. Driving the wheel in PC
  mode would mean speaking HORI's vendor protocol over `0xFF20` / `0xFF21` directly — a
  reverse-engineering project, and unnecessary now that Xbox mode works.
