# Game compatibility

What this bridge does in each game that has been tried, and what each one needs before it
works. Games differ far more than they should: two titles using the same DirectInput API can
disagree about whether a virtual device is even a wheel, and that single disagreement decides
whether force feedback is real or useless.

**Everything here is measured, not assumed.** Where a claim came from a log, the numbers are
included. Where something is untested, it says untested rather than guessing.

New entries are welcome — copy the [template](#template-for-a-new-game) at the bottom.

## Status at a glance

| Game | Status | Needs setup? | Verified |
|---|---|---|---|
| [DiRT 4](#dirt-4) | 🟡 Force feedback works; motor occasionally needs a game restart | Yes — device registration | 2026-08-24 |
| [RaceRoom Racing Experience](#raceroom-racing-experience) | ⚪ Untested | Unknown | — |
| [Automobilista 2](#automobilista-2) | ⚪ Untested | Unknown | — |

| Status | Meaning |
|---|---|
| 🟢 Works | Steering and force feedback both correct, no caveats worth listing |
| 🟡 Playable | Runs and is drivable, but something is degraded or still being worked on |
| 🔴 Broken | Does not work, and the reason is recorded |
| ⚪ Untested | Nobody has tried it yet |

> **Do not use this with a game that has kernel anti-cheat** (EasyAntiCheat, BattlEye). The
> bridge puts a `dinput8.dll` proxy in the game folder, which is exactly the shape of thing
> those systems block. Every game listed here ships without one.

---

## DiRT 4

**Status:** 🟡 Steering, pedals and force feedback all work — the wheel loads up in a corner
and pulls back to centre. The one rough edge is that the motor can stop producing torque and
need the game restarting; see [Known issues](#dirt-4-known-issues).

**Verified:** 2026-08-24, Steam build, app id 421020.

### Required setup

DiRT 4 classifies input devices from its own database and gives anything it does not
recognise a **generic, non-directional** force feedback profile. Without the entry below, the
game sends unsigned magnitude with no usable direction: the wheel pulls one way permanently,
never centres, and both in-game force feedback sliders do nothing. Kerbs and gravel still feel
correct, which is what makes this so easy to misdiagnose.

**1. Register vJoy as a wheel.** In `<game>\input\devices\device_defines.xml`, before the
closing `</device_list>`:

```xml
<device id="{BEAD1234-0000-0000-0000-504944564944}" name="vjoy_wheel" priority="100" type="wheel" ffb="enabled" />
```

`type="wheel"` is the part that matters. The GUID is your device's PID and VID concatenated,
then the constant `504944564944` (ASCII for `PIDVID`) — vJoy is `VID_1234` `PID_BEAD`, so it
becomes `{BEAD1234-...}`. To confirm your own IDs:

```powershell
Get-ChildItem "HKCU:\System\CurrentControlSet\Control\MediaProperties\PrivateProperties\Joystick\OEM" |
  ForEach-Object { "{0} -> {1}" -f $_.PSChildName, (Get-ItemProperty $_.PSPath).OEMName }
```

**2. Add an action map** at `<game>\input\actionmaps\vjoy_wheel.xml` — the file name must
match the `name` attribute above. See [`docs/dirt4/vjoy_wheel.xml`](docs/dirt4/vjoy_wheel.xml)
in this repo for the version that is known to work.

Back up `device_defines.xml` first. Steam's *Verify integrity of game files* restores both if
anything goes wrong, and will also remove them on a game update — expect to reapply after
patches.

### Why the action map is needed

The engine binds steering as **two halves of one axis** (`di_x_axis` `type="lower"` and
`type="upper"`). Binding that through the game's own UI fails: assigning the first direction
works, and assigning the second drops the device with *"Steuerungsgerät geändert / the
connection to an input device was disconnected"*. Declaring both halves in XML avoids the UI
path entirely.

### Settings that work

In `tune.json`:

```json
{ "strength": 0.2, "invert": true, "dir_mode": "sin", "max_force": 0.45 }
```

**`invert: true` is measured, not preference**, and confirmed by feel. With our force output
at zero, the game's centring spring disabled and the car driving, DiRT 4's force points the
*same* way as the steering angle:

```
140 samples, mean |force| 0.596
96% the same sign as steering
wheel LEFT   mean angle -0.31 -> mean force -0.547
wheel RIGHT  mean angle +0.31 -> mean force +0.554
```

Symmetric on both sides. Applied unchanged that is positive feedback: the wheel runs away into
whatever corner you turn into, hardest under throttle. Inverted, it pulls back to centre
through a corner, which is what it should do.

The likely reason is our own plumbing rather than the game: this wheel's motor drives in the
opposite direction to the sign of its own position reading, so one global flip corrects every
effect at once.

In game:

- Set the input preset to the vJoy wheel device.
- **Turn the in-game force feedback strength down** before the first drive. Once the device is
  registered correctly those sliders work for the first time, and a setting left at maximum
  from when it did nothing produces forces at ±1.0 of full scale.
- **Leave the centring spring OFF.** Real self-aligning torque is strong (0.596 mean while
  driving) and behaves far better in a loop with lag than the artificial spring, which tends
  to hunt.

<a name="dirt-4-known-issues"></a>
### Known issues

- **The motor can go silent, and nothing looks wrong when it does.** If the shim's log
  (`%TEMP%\wh33lh4x_shim.log`) shows `bridge went quiet -- releasing the motor`, the motor has
  been released and re-claimed; doing that repeatedly leaves it accepting effects and
  producing no torque while still reporting a running effect. Correct force is commanded, the
  bridge log looks perfect, and the wheel is dead. **Restarting the game clears it** -- the
  motor claim lives in the shim inside the game process, so a fresh game is a fresh motor. The
  bridge does not need restarting.
- **Oscillation at high gain.** Too much `strength` makes the wheel hunt and, at worst, sweep
  lock to lock on its own. The game's force reflects wheel position from some tens of
  milliseconds ago, and a laggy spring with too much gain is unstable. Lower `strength`.
  Inverting a damping force does the same thing for a different reason, so get the sign right
  before blaming gain.
- **Only ever sends constant force.** No spring, damper or periodic effects, in either
  profile — everything is summed into one signed constant-force stream.

### Launching

Steam DRM relaunches the game through the client, so start it from Steam and let the launcher
wait for it:

```powershell
.\play.ps1 -Game "...\DiRT 4\dirt4.exe" -NoLaunch
```

---

## RaceRoom Racing Experience

**Status:** ⚪ Untested.

Free to play and has no kernel anti-cheat, so it is a reasonable next target. Whether it needs
a device registration step like DiRT 4's is unknown — that mechanism is specific to
Codemasters' engine, and RaceRoom's may or may not have an equivalent.

---

## Automobilista 2

**Status:** ⚪ Untested.

Built on Madness Engine, which generally accepts any DirectInput device with force feedback
and exposes per-device settings in game. No anti-cheat.

---

## Template for a new game

Copy this, fill it in, and add a row to [Status at a glance](#status-at-a-glance). Keep
unknowns as unknowns — a blank is more useful than a guess.

```markdown
## Game name

**Status:** 🟢 / 🟡 / 🔴 / ⚪ one line on what does and does not work.

**Verified:** YYYY-MM-DD, store and build.

### Required setup
What has to be changed before it works, exact paths and exact text. Nothing if it just works.

### Settings that work
The `tune.json` values, and the relevant in-game settings.

### Known issues
What is still wrong, and what is known about why.

### Evidence
Where a claim is surprising, the measurement behind it: log numbers, what was compared with
what. This is the part that stops the next person repeating the work.
```

### What is worth recording

- **Does the game see the vJoy device as a wheel at all?** This is the question that decides
  everything else, and games answer it in ways that are not visible from their settings menu.
- **Does force ever go negative?** Run `tune_report.py` after a session. A wheel whose output
  never changes sign cannot centre, whatever it feels like — and rectified force feels
  perfectly normal on kerbs and gravel, so feel will not catch it.
- **Which way does the game's force point?** Measure it with `strength: 0` in `tune.json` so
  the wheel is free while you steer by hand. With force applied, wheel position and game force
  drive each other and the correlation means nothing.
- **Which in-game sliders actually do something.** Some do nothing until the device is
  correctly classified.
