# Tuning

The four sliders in the window cover what most people want to change, and
[the README](../README.md#tuning) describes them. This page is everything else.

All of it lives in `tune.json`, next to the scripts. **The bridge re-reads that file while it
runs**, so a saved change reaches the wheel within half a second, mid corner. You do not have
to restart anything to try a value.

## Every key

| Key | What it does |
|---|---|
| `strength` | Multiplies the game's force. This, not the WGI gain, is the working volume knob: WGI latches gain when the effect loads, so writing it mid-session does nothing. The window's Strength slider caps at `1.00`; this file does not |
| `invert` | Flips the game's force direction. Games disagree about whether a force's sign lives in its magnitude or its direction angle |
| `dir_mode` | How a force's direction angle is turned into a signed value. Per game |
| `max_force` | Ceiling on commanded force. Live, unlike `--gain` |
| `min_force` | Floor on non-zero output, so small forces still overcome the motor's own stiction instead of vanishing |
| `spring`, `damper`, `friction` | Synthetic centring, damping and drag, computed from real wheel position. For games that send none, and DiRT 4 sends a single constant force and nothing else. `0` is off |
| `btn_down`, `btn_up`, `btn_next` | Wheel buttons that adjust tuning mid corner, as 1-based bit numbers. A game owns the foreground and the keyboard with it, so the wheel is the only device that can still reach the bridge. Run it, press buttons, and each new bitfield is printed |

`invert` and `dir_mode` are properties of the **game**, not of your taste, so their known good
values are recorded per title in [GAMES.md](../GAMES.md) rather than being something to
experiment with.

## How dir_mode reads a game's direction field

A force arrives with a magnitude and a direction, and games disagree about which of the two
carries the sign. Getting this wrong rectifies the output: the wheel pulls one way and never
centres, while kerbs and gravel still feel correct, which is what makes it hard to spot.

| Mode | Reads the field as |
|---|---|
| `sin` | A true polar angle, taking the X component. Correct for a game that points forces around a full circle |
| `span` | A steering axis across the 90 to 180 degree quarter circle, interpolating between the ends |
| `sign` | The same two directions with no interpolation, so nothing nulls out halfway |

### What was measured

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

## The gain stage the software cannot see

The wheel holds a force feedback strength setting of its own, in the **HORI FFB RWD-Devicemanager
für Xbox Series X Series S** app from the Microsoft Store. It is the HORI app that can talk to
the wheel in Xbox mode; the other HORI apps cannot. **It is driven with the wheel itself and
does not respond to keyboard or mouse**, which is confusing the first time you open it.

Nothing in this project can read or change that setting, and it sits above everything in
`tune.json`. Measured 2026-08-26: at a fixed commanded force of `0.30`, the wheel travelled
**0.099** at strength 8, **0.029** at strength 1, and **0.106** back at 8 again. About 3.5x,
from identical software settings.

So if two machines feel different with the same `tune.json`, that is the first place to look.
Note what yours is set to before changing anything here.

## Reading back what actually happened

After a session:

```powershell
.\python\python.exe tune_report.py        # newest bridge log
```

It reports which effects the game sent, whether output ever went negative, and the correlation
between steering angle and force.

**That last number is the objective test for centring.** Force must *oppose* steering angle, and
a wheel whose output never changes sign cannot centre no matter how it is tuned. A rectified
output once survived a full session described as "bumps and gravel feel ok", because
rectification is inaudible on symmetric effects. Only arithmetic on a log caught it.

## Where the numbers came from

`min_force` comes from `stiction_test.py`, which measures the smallest force that moves the
wheel at all. The per-game values live in [GAMES.md](../GAMES.md); what was measured to arrive
at them is here.

### DiRT 4

At `strength: 0.2`, `max_force: 0.45`, the game asked for the full ±1.000 and the wheel got
±0.200. Across 1699 non-zero samples, 10.0% fell below breakaway and 0.3% hit the ceiling. So
the bottom of the range is not the problem and the headroom is at the top, which is why raising
`strength` is the change that matters and `min_force: 0.01` is a small one.

`invert: true` was measured with our own output at zero and the car driving: DiRT 4's force
points the same way as the steering angle, 140 samples, mean magnitude 0.596, 96% matching
sign, symmetric on both sides. Applied unchanged that is positive feedback, and the wheel runs
away into whatever corner it is turned into. The likely cause is our own plumbing rather than
the game, since this wheel's motor drives opposite to the sign of its own position reading, so
one global flip corrects every effect at once.

The game sends friction at roughly the same rate as constant force, 13,970 operations against
13,950 over 374 telemetry samples, with two spring and two damper operations and no periodics.
An earlier session recorded constant force only, and the likeliest explanation is that it
predates registering vJoy as a wheel, which changes the profile the game hands out. Untested
either way.
