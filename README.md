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
| `b` | Show the firmware effects that don't work (hidden by default; they still run if typed) |
| `c` | Force one condition sign convention on all effects instead of the measured per-effect one |
| `r` | Release the motor, restore firmware centering |
| `s` | Stop all effects |
| `q` | Quit |

## Effect support on this firmware

| Effect | Result |
|---|---|
| Constant force | Works — direction and magnitude both honoured |
| Ramp force | Works — must sweep through zero to be felt |
| Sine / square / triangle / sawtooth | Silent on firmware — loads, runs, no torque. Sine and square are synthesised instead (`y` / `z`) |
| Spring | Works — pulls back to centre, and does so on either sign convention |
| Damper | Works — resistance scales with turning speed |
| Friction | Works, but **only on a flipped sign**. On the documented convention it drives the wheel to full lock on its own |
| Inertia | Produces force, but it feels like cogging rather than resistance to acceleration. Safe — it never runs away — but not usable as inertia |

**The sign convention is not the same for every condition effect.** Damper needs the
documented direction and friction needs it flipped, so no single global setting is correct
for both. The tool applies a measured per-effect sign automatically; `c` forces one
convention across all four, and exists only to re-run that sweep if the firmware changes.

These verdicts come from logged position traces, not from how the wheel felt. The test:
while an effect holds the motor the wheel parks wherever it is left, so once your hands come
off, any sustained motion is the effect's doing — a passive effect ends at rest, an inverted
one drives to the end stop and is still moving seconds later. Each condition run prints a
`MOTION` summary and writes a downsampled trace to the log, so any verdict here can be
re-checked without re-running the hardware.

All four also exist as software effects (`1s`–`4s`), which are unaffected by any of this.

The software effects hold one `ConstantForceEffect` open and rewrite its magnitude ~80×/sec
from the wheel's own position reading — the same technique games use to drive hardware that
only implements constant force. Because the force is computed from the motion it's meant to
oppose, a sign error makes an effect inert rather than dangerous.

One expected side effect: while a software effect holds the motor there's no centering, so a
zero-mean waveform (sine/square) lets the wheel drift. A real game would run a spring effect
underneath to counter that.

## In progress: a bridge into real games

Both of the wheel's USB modes are dead ends for sims. In PC mode DirectInput sees the wheel
but its joystick collection is input-only, so there is no force feedback for any driver to
expose. In Xbox mode force feedback works, but only through `Windows.Gaming.Input`, and
Assetto Corsa, rFactor, LFS and ETS2 all drive wheels through DirectInput. So this wheel
currently has **no force feedback in any PC sim**.

The bridge presents a virtual force-feedback wheel to DirectInput and renders the effects
games send it on the real motor via WGI. It is being built now; nothing below is finished.

**If you want to follow along, install [`BrunnerInnovation/vJoy`
v2.2.2.0](https://github.com/BrunnerInnovation/vJoy).** The version matters more than it
looks. vJoy 2.1.9.x has no force-feedback *effect block index*: every effect reports index
`1`, so concurrent effects cannot be told apart. That is invisible while testing one effect
at a time and fatal in a real game, which runs a centring spring, a damper and road texture
simultaneously. `njz3` added the block index in 2.2.0 and Brunner's 2.2.2.0 adds EV signing
for Windows 11. The driver and the interface DLL are not compatible across the 2.1.9 → 2.2.0
boundary, so do not mix them.

**Then configure one vJoy device.** Installing the driver is not enough — vJoy ships with no
device configured, so the bridge has nothing to feed. Open **vJoyConf** (*Configure vJoy*),
select device **1** on the tab strip, and set:

| Section | What to set |
|---|---|
| **Axes (max 8)** | Tick **X, Y, Z, Rx, Ry** — steering, throttle, brake, clutch, handbrake |
| **Number of Buttons** | Enough for your wheel. Needed for the mid-corner tuning buttons below |
| **Force Feedback** | Tick **Enable Effects**, and leave the individual effects ticked |
| bottom of the window | **Enable vJoy** ticked, then **Apply** |

Device 1 is what the bridge uses unless you pass `--device`.

**Two things in that dialog will mislead you.**

The axis list also offers `Steering`, `Brake`, `Clutch` and `Throttle`, which look like exactly
what a wheel wants. They are not what this bridge sends. It writes to **X, Y, Z, Rx and Ry**, so
ticking the plausible-looking ones instead gives you a device that works and never moves.

And vJoyConf says at the bottom that enabling force feedback on one device makes *all* vJoy
devices report as force-feedback capable. So "Windows shows it as a force feedback device" is
not evidence that device 1 is set up correctly — only `vjoy_ffb_spike.py` reporting `PASS` is.

Missing **Enable Effects** is the quietest failure of the three: the axes still work, so the
wheel steers and the pedals respond, and there is simply no force. That reads exactly like a
broken wheel rather than a checkbox.

The axis list is a maximum, not a requirement — the bridge asks vJoy which axes exist and feeds
only those, so fewer axes means fewer controls rather than a failure.

```powershell
.\.venv\Scripts\python.exe -m pip install pyvjoyffb
.\.venv\Scripts\python.exe vjoy_ffb_spike.py     # checks the vJoy foundation end to end
```

`vjoy_ffb_spike.py` is both halves of the test: it sends DirectInput effects to the virtual
device and logs what comes back out of vJoy's force-feedback callback, so it verifies the
whole path without a game or a control-panel tab. It never touches the real wheel. Run it
first — if it does not report `PASS`, nothing built on top of vJoy will work.

## Tuning force feedback while you drive

Force feedback is judged by feel, and feel cannot be judged across a restart — by the time the
game is loaded and you are back at the corner that felt wrong, you are comparing against a
memory. So the bridge re-reads `tune.json` while it runs; a saved change reaches the wheel
within half a second, mid-corner.

| Key | What it does |
|---|---|
| `strength` | Multiplies the game's force. This, not the WGI gain, is the working volume knob — WGI latches gain when the effect loads, so writing it mid-session does nothing |
| `invert` | Flips the game's force direction. Games disagree about whether a force's sign lives in its magnitude or its direction angle |
| `max_force` | Ceiling on commanded force. Live, unlike `--gain` |
| `min_force` | Floor on non-zero output, so small forces still overcome the motor's own stiction instead of vanishing |
| `spring`, `damper` | Synthetic centring and damping computed from real wheel position. For games that send none — DiRT 4 sends a single constant force and nothing else. `0` = off |
| `btn_down`, `btn_up`, `btn_next` | Wheel buttons that adjust tuning mid-corner, as 1-based bit numbers. A game owns the foreground and the keyboard with it, so the wheel is the only device that can still reach the bridge. Run it and press buttons — each new bitfield is printed |

After a session, `tune_report.py` says what the force feedback actually did:

```powershell
.\.venv\Scripts\python.exe tune_report.py        # newest bridge log
```

It reports which effects the game sent, whether output ever went negative, and the correlation
between steering angle and force. That last number is the objective test for centring: force
must **oppose** steering angle, and a wheel whose output never changes sign cannot centre no
matter how it is tuned. A rectified output survived a full session described as "bumps and
gravel feel ok", because rectification is inaudible on symmetric effects — only arithmetic on a
log caught it.

## Logging

Every run writes `logs/wgi_probe_<timestamp>.log` — console output with elapsed timestamps,
plus dense per-tick data (position, velocity, commanded force, foreground state, effect
state) that isn't printed live. Flushed per line, so `Ctrl+C` still leaves a complete log.
Pass `--no-log` to disable it.

## Files

Every script's flags and a one-line description of each live in
[COMMANDS.md](COMMANDS.md). The table below is the short version.


| File | Purpose |
|---|---|
| `wgi_probe.py` | The tool: detection, message pump, effect menu, sweeps, cleanup |
| `wheel_profile.py` | Calibration, `wheel_profile.json` persistence, software condition effects |
| `vjoy_ffb_spike.py` | Verifies the vJoy force-feedback path the game bridge is being built on |
| `live_tune.py` | `tune.json` reloading and the wheel-button tuning controls |
| `tune_report.py` | Reads a session log and reports what the force feedback actually did |
| `probe_log.py` | Session logging to `logs/` |
| `hid_probe.py` | Reads raw HID report descriptors — shows why DirectInput/GameInput can't reach this wheel |
| `probe.py` + `gameinput_abi.py` | GameInput diagnostic (negative result, kept as evidence) |
| `dinput_probe.py` + `dinput_abi.py` | DirectInput 8 diagnostic (negative result on the wheel, kept as evidence). `dinput_abi.py` is also the DirectInput binding the bridge test uses |

Why the other three APIs fail, the discovery process, and every gotcha found along the way
are written up in `.claude/memory/` rather than here — see `hori-wheel-ffb-probe-goal.md`
and `wgi-forcefeedback-api-gotchas.md` if you want the full story instead of just the
result.

## Thanks ♥

This sits on top of other people's work. Everything below is open source, and a few of them
are the reason the project exists at all.

**[vJoy](https://github.com/shauleiz/vJoy)** — Shaul Eizikovich, MIT. The virtual joystick
driver the game bridge is built on. Upstream is abandoned, but the forks kept it alive:
**[njz3](https://github.com/njz3/vJoy)** added the force-feedback effect block index in 2.2.0,
without which concurrent effects can't be told apart and this bridge doesn't work, and
**[BrunnerInnovation](https://github.com/BrunnerInnovation/vJoy)** carried that into a signed
Windows 11 build.

**[pyvjoyffb](https://github.com/Ultrawipf/pyvjoy)** — Yannick Richter, MIT, forked from
**[tidzo/pyvjoy](https://github.com/tidzo/pyvjoy)**. Decodes force feedback through vJoy's own
exports instead of reimplementing HID PID, and bundles the matching DLL. Saved us the whole
usermode half of the problem.

**[PyWinRT](https://github.com/pywinrt/pywinrt)** — MIT. This wheel's motor is reachable only
through `Windows.Gaming.Input`, and PyWinRT makes that a `pip install` rather than a C++
project.

**[Zig](https://ziglang.org/)** — MIT. Builds the shim on a machine with no Visual Studio, and
links it against nothing but OS libraries, so nobody needs a VC++ redistributable.

**[CPython](https://www.python.org/)**,
**[typing_extensions](https://github.com/python/typing_extensions)** and
**[Ruff](https://github.com/astral-sh/ruff)** round it out.

No HORI code or assets are used anywhere in this project.

## License

MIT — see [LICENSE](LICENSE). What the project depends on, who holds copyright on it and
under what terms is recorded in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
