# Game compatibility

What this bridge does in each game that has been tried, and what each one needs before it
works. Games differ far more than they should: two titles using the same DirectInput API can
disagree about whether a virtual device is even a wheel, and that single disagreement decides
whether force feedback is real or useless.

Every entry records what was actually tried on the game it names. Where something has not been
tested, it says so rather than guessing.

New entries are welcome — copy the [template](#template-for-a-new-game) at the bottom. If you
want the reasoning behind any of this — why the bridge is built the way it is, why a game needs
the setup it does — that lives in [NOTES.md](NOTES.md), not here.

## Status at a glance

| Game | Status | Needs setup? | Verified |
|---|---|---|---|
| [DiRT 4](#dirt-4) | 🟢 Steering, pedals and force feedback all work | Yes — device registration | 2026-08-24 |
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

**Status:** 🟢 Steering, pedals and force feedback all work — the wheel loads up in a corner
and pulls back to centre. Alt-tab away for as long as you like; force comes back by itself
when you return to the game.

**Verified:** 2026-08-24, Steam build, app id 421020.

### Required setup

Both steps are manual edits inside your DiRT 4 install. **Back up `device_defines.xml` before
you start.** Steam's *Verify integrity of game files* will restore it if anything goes wrong,
and a game update reverts both files — expect to reapply after patches.

Without this setup DiRT 4 does not recognise the device and gives it a generic,
non-directional force feedback profile: the wheel pulls one way permanently, never centres,
and both in-game force feedback sliders do nothing. Kerbs and gravel still feel correct, which
makes it easy to mistake for a working setup.

**1. Register vJoy as a wheel.** In `<game>\input\devices\device_defines.xml`, add this line
before the closing `</device_list>` — add it, do not replace anything:

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

A copy of that line, with the same notes, is at
[`docs/dirt4/device_defines_snippet.xml`](docs/dirt4/device_defines_snippet.xml).

**2. Add an action map.** Copy [`docs/dirt4/vjoy_wheel.xml`](docs/dirt4/vjoy_wheel.xml) to
`<game>\input\actionmaps\vjoy_wheel.xml`. The file name must match the `name` attribute above.

**Do not bind steering through the in-game controls menu.** It cannot be done: you can assign
one direction, and assigning the opposite one drops the device with *"Steuerungsgerät geändert
/ the connection to an input device was disconnected"*. The action map exists to avoid that
menu entirely — see [NOTES.md](NOTES.md) for why.

### Settings that work

In `tune.json`:

```json
{ "strength": 0.2, "invert": true, "dir_mode": "sin", "max_force": 0.45 }
```

**Leave `invert` set to `true`.** It is measured, not preference — DiRT 4's force points the
same way as the steering angle, so without the flip the wheel runs away into whatever corner
you turn into instead of pulling back to centre.

In game:

- Set the input preset to the vJoy wheel device.
- **Turn the in-game force feedback strength down** before the first drive. Once the device is
  registered correctly those sliders work for the first time, and a setting left at maximum
  from when it did nothing produces forces at full scale.
- **Leave the centring spring OFF.** The car's own self-aligning torque is strong and behaves
  better than the artificial spring, which tends to hunt.

<a name="dirt-4-known-issues"></a>
### Known issues

- **Oscillation at high gain.** Too much `strength` makes the wheel hunt and, at worst, sweep
  lock to lock on its own. Lower `strength`. Inverting a damping force does the same thing for
  a different reason, so get the sign right before blaming gain.
- **Only ever sends constant force.** No spring, damper or periodic effects, in either
  profile — everything is summed into one signed constant-force stream. If you want a centring
  spring or damping beyond what the game sends, use the synthetic ones in `tune.json`.

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
What is still wrong, and what a player can do about it.

### Launching
Anything unusual about starting the game — DRM relaunches, store clients, launcher flags.
```

Keep entries to what a player needs. If you measured something surprising along the way, the
measurement is worth keeping — put it in [NOTES.md](NOTES.md) and link to it from here rather
than inlining it, so this file stays readable as instructions.

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
