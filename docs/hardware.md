# What this wheel's firmware actually does

Measured on a **Hori Force Feedback Racing Wheel DLX** (`VID 0x0F0D`) in Xbox mode
(`PID 0x015C`). The effect table was measured through `Windows.Gaming.Input`, the raw USB facts
over GIP directly. Every verdict here comes from logged position traces rather than from how
the wheel felt.

## Modes

The wheel must be in **Xbox mode**. A long press of the PROFILE button switches out of PC mode
(`PID 0x015D`), which has no force feedback interface at all. The window's wheel line says
*in PC mode* when it is.

## Raw USB

The bridge speaks the wheel's own protocol, GIP, straight to its USB endpoint.
[`evidence/RAW_USB.md`](../evidence/RAW_USB.md) is the full write-up; the facts that shape the
code are these.

- **One constant force, rewritten.** The effect is loaded once at zero, and every change after
  that is a bare parameter block, written at the endpoint's 4 ms interval (250 Hz). Reloading
  the effect tears it down for about 30 ms, which is felt as a step, so it is never reloaded
  while the bridge runs.
- **Every effect a game asks for is rendered in software** and summed into that one force. The
  firmware's own effects are not used, which is why the table below is a record rather than a
  constraint.
- **A positive force moves the position reading positive**, the same as under WGI.
- **Commanded force is capped at 0.35** of raw full scale until raw 1.0 has been compared with
  WGI's 1.0. In a DiRT 4 drive the cap was reached in under 2% of ticks.
- **Power-on recentres the wheel** when it starts off centre, a slow firmware sweep. While it
  runs the wheel sends no input and ignores force.
- **On Linux, USB autosuspend triggers that sweep every time.** With no kernel driver bound,
  Linux suspends the wheel two seconds after its last use, and every resume boots it into the
  sweep. Setting the device's `power/control` to `on` keeps it awake; the window's wheel check
  prints the command.
- **The first input report of a session is junk** and is discarded.

## Effect support

| Effect | Result |
|---|---|
| Constant force | Works. Direction and magnitude both honoured |
| Ramp force | Works, but must sweep through zero to be felt |
| Sine / square / triangle / sawtooth | **Silent on firmware.** Loads, runs, produces no torque |
| Spring | Works. Pulls back to centre, and does so on either sign convention |
| Damper | Works. Resistance scales with turning speed |
| Friction | Works, but **only on a flipped sign**. On the documented convention it drives the wheel to full lock on its own |
| Inertia | Produces force, but feels like cogging rather than resistance to acceleration |

**The sign convention is not the same for every condition effect.** Damper needs the documented
direction and friction needs it flipped, so no single global setting is correct for both. This
is one reason the bridge renders everything itself: a force computed from the motion it is meant
to oppose cannot run away, so a sign error makes an effect inert rather than dangerous.

**How those verdicts were reached:** while an effect holds the motor, the wheel parks wherever
it is left. So once your hands come off, any sustained motion is the effect's doing. A passive
effect ends at rest; an inverted one drives to the end stop and is still moving seconds later.

## The foreground gate, and why raw USB

`Windows.Gaming.Input` ties **enumeration, position reads, force output and the motor claim
itself** to holding the foreground window. A game always holds the foreground, so no background
program can drive the wheel through WGI. The old shim worked around that by running inside the
game; raw USB has no such gate at all, measured with another application in front.

**While a program holds the motor, the firmware's own centring spring is suspended**, so the
wheel feels looser than at rest even under a gentle effect. That is also true over raw USB,
which is why the profiles supply a spring of their own.

## Stiction, and positions the wheel will not leave

Breakaway force, the level below which the motor cannot move the wheel at all, came out **below
0.02 and below 0.01**. That is a ceiling rather than a measurement: every pass broke away on the
*first* step tried, at both step sizes. This wheel has no meaningful stiction problem.

**But there are positions it will not leave.** Seen repeatedly near centre, the wheel does not
move at `0.30`, thirty times the breakaway figure. Cogging is the obvious explanation and it is
*not* established; belt binding or a moulded detent would look identical.

That matters for centring specifically, because a spring is weakest exactly where those
positions are. "It holds position instead of centring" is at least as likely to be one of those
spots as it is to be stiction.

**Under WGI, commanding a force the wheel cannot act on went silent** after about a second,
audible as two short hums, while every call kept reporting healthy. Only diagnostics ever
produced it, by commanding tiny forces at a wheel held still. It has not been seen over raw USB.

## Why the other APIs are dead ends

GameInput, DirectInput and raw HID were each tried and each fails, for a different reason.
[`evidence/README.md`](../evidence/README.md) has the answer to each question in a sentence,
with the script that produced it beside it.
