# What this wheel's firmware actually does

Measured on a **Hori Force Feedback Racing Wheel DLX** (`VID 0x0F0D`) in Xbox mode
(`PID 0x015C`), through `Windows.Gaming.Input`. Every verdict here comes from logged position
traces rather than from how the wheel felt.

## Modes

The wheel must be in **Xbox mode**. A long press of the PROFILE button switches out of PC mode,
which has no force feedback interface at all. `wgi_probe.py --list-only` will tell you which
mode it is in.

## Effect support

| Effect | Result |
|---|---|
| Constant force | Works. Direction and magnitude both honoured |
| Ramp force | Works, but must sweep through zero to be felt |
| Sine / square / triangle / sawtooth | **Silent on firmware.** Loads, runs, produces no torque. Sine and square are synthesised in software instead (`y` / `z` in the probe) |
| Spring | Works. Pulls back to centre, and does so on either sign convention |
| Damper | Works. Resistance scales with turning speed |
| Friction | Works, but **only on a flipped sign**. On the documented convention it drives the wheel to full lock on its own |
| Inertia | Produces force, but feels like cogging rather than resistance to acceleration. Safe, it never runs away, but not usable as inertia |

**The sign convention is not the same for every condition effect.** Damper needs the documented
direction and friction needs it flipped, so no single global setting is correct for both. The
tool applies a measured per-effect sign automatically. The probe's `c` key forces one convention
across all four, and exists only to re-run that sweep if the firmware changes.

**How those verdicts were reached:** while an effect holds the motor, the wheel parks wherever
it is left. So once your hands come off, any sustained motion is the effect's doing. A passive
effect ends at rest; an inverted one drives to the end stop and is still moving seconds later.
Each condition run prints a `MOTION` summary and writes a downsampled trace to the log, so any
verdict here can be re-checked without re-running the hardware.

All four conditions also exist as software effects, which are unaffected by any of this. They
hold one `ConstantForceEffect` open and rewrite its magnitude about 80 times a second from the
wheel's own position reading, which is the same technique games use to drive hardware that only
implements constant force. Because the force is computed from the motion it is meant to oppose,
a sign error makes an effect inert rather than dangerous.

## The foreground owns the motor

`Windows.Gaming.Input` ties **enumeration, position reads, force output and the motor claim
itself** to holding the foreground window. This is the single rule behind most of the surprising
behaviour here.

**Releasing the wheel feels looser, not weaker.** At idle the firmware runs its own strong
centring spring. With nothing claiming the device, the wheel returns to centre by itself,
precisely and hard. The moment a process takes the motor, that spring is suspended and the wheel
goes slack, so even a gentle effect feels looser than the resting wheel.

**What gives the spring back is losing the foreground, not unloading the effect.** Alt-tab away
from a running game and the firmware spring returns; go back and force works again. Unloading
the effect changes nothing on its own: measured 2026-08-26 with
[`evidence/centring_test.py`](../evidence/centring_test.py), after `try_unload_effect_async` and
`try_reset_async` both returned `True`, six hand-pushed quarter turns held their position
exactly as they had with the effect still loaded, because the probe window still held the
foreground throughout.

## Stiction, and positions the wheel will not leave

Breakaway force, the level below which the motor cannot move the wheel at all, came out **below
0.02 and below 0.01**. That is a ceiling rather than a measurement: every pass broke away on the
*first* step tried, at both step sizes, so each run reports its own `--step`. This wheel has no
meaningful stiction problem.

**But there are positions it will not leave.** Seen repeatedly near centre, the wheel does not
move at `0.30`, thirty times the breakaway figure. Cogging is the obvious explanation and it is
*not* established; belt binding or a moulded detent would look identical.

That matters for centring specifically, because a spring is weakest exactly where those
positions are. "It holds position instead of centring" is at least as likely to be one of those
spots as it is to be stiction.

## Why the other APIs are dead ends

GameInput, DirectInput, raw HID and raw GIP were each tried and each fails, for a different
reason. [`evidence/README.md`](../evidence/README.md) has the answer to each question in a
sentence, with the script that produced it beside it.
