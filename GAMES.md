# Game compatibility

What this bridge does in each game that has been tried, and what each one needs before it
works. Games differ far more than they should: two titles using the same DirectInput API can
disagree about whether a virtual device is even a wheel, and that single disagreement decides
whether force feedback is real or useless.

**Everything here is measured, not assumed.** Where a claim came from a log, the numbers are
included. Where something is untested, it says untested rather than guessing.

New entries are welcome — copy the [template](#template-for-a-new-game) at the bottom.

## Status at a glance

<!-- BEGIN generated from games.json -- edit games.json, not this table -->
| Game | Status | Needs setup? | Verified |
|---|---|---|---|
| [DiRT 4](#dirt-4) | 🟡 Playable -- Steering, pedals and force feedback all work -- the wheel loads up in a corner and pulls back to centre. | Yes -- vJoy must be registered as a wheel | 2026-08-26 |
| [RaceRoom Racing Experience](#raceroom-racing-experience) | ⚪ Untested | Unknown | — |
| [Automobilista 2](#automobilista-2) | ⚪ Untested | Unknown | — |
<!-- END generated -->

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

**Verified:** 2026-08-25, Steam build, app id 421020. Re-confirmed from the packaged
bundle (embeddable Python, prebuilt shim) rather than a source checkout.

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

### Why the action map is provided

The engine binds steering as **two halves of one axis** (`di_x_axis` `type="lower"` and
`type="upper"`). The action map declares both halves up front, so a fresh install has working
steering without going near the settings screen.

This section used to say the action map was the *only* way to bind steering, because assigning
the second direction in the game's own UI dropped the device with *"Steuerungsgerät geändert /
the connection to an input device was disconnected"*. **That was a symptom of the unregistered
device, not of the UI.** Re-tested 2026-08-26 with vJoy registered as `type="wheel"`: steering
was rebound to a button and then reassigned to the axis from the game's own settings screen,
with no drop.

Untested: whether a fresh install with `device_defines.xml` edited but *no* action map can bind
steering through the UI alone. Both files were present in that test, so step 2 stays.

### Settings that work

In `tune.json`:

```json
{ "strength": 0.2, "invert": true, "dir_mode": "sin", "max_force": 0.45 }
```

**These are conservative, and measurably so.** A 2026-08-25 session at these values:

```
game asked : -1.000 .. +1.000
wheel got  : -0.200 .. +0.200
non-zero samples 1699; 10.0% under 0.01 (below breakaway), 0.3% at the ceiling
```

**Measured 2026-08-25 on a Hori Force Feedback Racing Wheel DLX** (`VID 0x0F0D`, Xbox mode). Every
number in this section is that one device -- the bridge is not HORI-specific, and another wheel
needs its own `stiction_test.py` run rather than these figures.

Breakaway force -- the level below which the motor does not move the wheel at all -- came out
**below 0.02, and below 0.01**. That is not a measurement so much as a ceiling: every pass broke
away on the *first* step tried, at both step sizes, so each run reports its own `--step`. This
wheel has no meaningful stiction problem.

**But there are positions it will not leave.** Seen repeatedly near centre, the wheel does not
move at **0.30** -- thirty times the breakaway figure. Cogging is the obvious explanation and it
is *not* established; belt binding or a moulded detent would look identical. That matters here
specifically:
DiRT 4's centring force is proportional to steering angle, so it is weakest exactly where those
positions are. "It holds position instead of centring" is at least as likely to be cogging at one
of those spots as it is to be stiction.

Commanding a force this wheel cannot act on also has a cost. After about a second of it, the
motor goes silent for the rest of the process -- audible as two short hums -- while reporting
itself perfectly healthy through every `Windows.Gaming.Input` call. Whether that is a driver
stall cut-out or something else is a guess; the behaviour is measured and reproducible. It is
why `stiction_test.py` defaults to `--step 0.02` and warns below it.

Against the session above, only **10% of non-zero output falls below breakaway and 0.3% clips**.
So the bottom of the range is not where the problem is, and the headroom at the top is where the
opportunity is:

- **`strength` above 0.2.** The game is asking for full scale and getting 0.2 of it, with
  almost nothing clipping. This raises peak force, so move it in steps and read the
  [oscillation note](#dirt-4-known-issues) first. **This is the change that matters.**
- **`min_force: 0.01`**, optional. Hardware compensation, not an effect -- it lifts small
  non-zero forces to something the motor can express. It recovers that 10% without raising
  peak force, so it carries no oscillation risk, but it is a small effect either way.

This figure has been wrong twice, in opposite directions, which is worth recording:

- **0.1** was a hardcoded placeholder in `tune_report.py` that was never measured. It made the
  same kind of session look like 75% of its output was wasted.
- **0.022 right / 0.010 left** came from a real run, but its later passes had walked the wheel
  into its end stop. A wheel against the stop cannot move at any force, so those passes
  measured the stop, not stiction, and inflated the right-hand average. `stiction_test.py` now
  recentres the wheel before every pass.

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

- **The motor can go silent, and nothing looks wrong when it does.** Correct force is
  commanded, the bridge log looks perfect, the effect still reports as running, and the wheel
  is dead. **Restarting the game clears it** -- the motor claim lives in the shim inside the
  game process, so a fresh game is a fresh motor. The bridge does not need restarting.

  Two mechanisms are known to produce exactly this, and they are told apart by evidence rather
  than by symptom:

  1. **Claim churn.** If the shim's log (`%TEMP%\wh33lh4x_shim.log`) shows `bridge went quiet
     -- releasing the motor`, the motor was released and re-claimed. Doing that repeatedly
     leaves it accepting effects and producing no torque.
  2. **A force the wheel cannot act on.** Measured 2026-08-25 with `stiction_test.py`:
     commanding force at a position the wheel will not leave, for about a second, silences the
     motor **for the rest of the process** while every `Windows.Gaming.Input` call keeps
     reporting healthy. Audible as **two short hums**. Nothing in any log records it, and it
     fits "a fresh game is a fresh motor" exactly. **Whether this ever happens during play is
     untested** -- it was produced by a diagnostic deliberately ramping tiny forces, which is
     not what the bridge does.

  If the shim log has no release line and you heard the hums, it was the cut-out. Lowering
  `strength` far enough that the wheel is commanded forces it cannot act on makes this *more*
  likely, not less.
- **Oscillation at high gain.** Too much `strength` makes the wheel hunt and, at worst, sweep
  lock to lock on its own. The game's force reflects wheel position from some tens of
  milliseconds ago, and a laggy spring with too much gain is unstable. Lower `strength`.
  Inverting a damping force does the same thing for a different reason, so get the sign right
  before blaming gain.
- **Sends friction as well as constant force.** An earlier session recorded constant force
  only; a 2026-08-25 session recorded **friction at roughly the same rate as constant**
  (13,970 friction operations against 13,950 constant, over 374 telemetry samples). No spring
  or damper worth the name — 2 operations each — and no periodics. Unexplained: the two
  sessions differ, and the likeliest cause is that the earlier one predates registering vJoy as
  a wheel in `device_defines.xml`, since an unrecognised device gets a different force feedback
  profile. **Untested either way** — it is recorded here as a measurement, not a conclusion.

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
