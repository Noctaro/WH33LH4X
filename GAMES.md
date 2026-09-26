# Game compatibility

What each game needs before it works, and what is still wrong with it once it does.

Adding a game? The template and what is worth measuring first are in
[docs/development.md](docs/development.md#adding-a-game).

## Status at a glance

<!-- BEGIN generated from games.json -- edit games.json, not this table -->
| Game | Status | Needs setup? | Verified |
|---|---|---|---|
| [DiRT 4](#dirt-4) | 🟡 Playable: Steering, pedals and force feedback all work: the wheel loads up in a corner and pulls back to centre. | Yes, vJoy must be registered as a wheel | 2026-09-26 |
| [RaceRoom Racing Experience](#raceroom-racing-experience) | ⚪ Untested | No, just bind steering in the game | — |
| [Automobilista 2 Demo](#automobilista-2-demo) | ⚪ Untested | Yes, set Controller Damping to 0 | — |
<!-- END generated -->

| Status | Meaning |
|---|---|
| 🟢 Works | Steering and force feedback both correct, no caveats worth listing |
| 🟡 Playable | Runs and is drivable, but something is degraded or still being worked on |
| 🔴 Broken | Does not work, and the reason is recorded |
| ⚪ Untested | Nobody has tried it over raw USB yet |

> Only results over raw USB count. RaceRoom and Automobilista 2 worked with the old shim
> (v0.1.0) and are untested again until someone drives them this way. Games with kernel
> anti-cheat (EasyAntiCheat, BattlEye) are untested; see [SECURITY.md](SECURITY.md#anti-cheat).

---

## DiRT 4

**Tested on:** Steam build, app id 421020, over raw USB with `python -m bridge`, 2026-09-26.

### Setup

Both files below are yours to edit, and Steam's *Verify integrity of game files* restores them,
so back up `device_defines.xml` first and expect to reapply after a game update.

**1. Register vJoy as a wheel**, or the game sends force that never centres. In
`<game>\input\devices\device_defines.xml`, before the closing `</device_list>`:

```xml
<device id="{BEAD1234-0000-0000-0000-504944564944}" name="vjoy_wheel" priority="100" type="wheel" ffb="enabled" />
```

`type="wheel"` is the part that matters. The GUID is your device's PID and VID concatenated,
then `504944564944`, which is ASCII for `PIDVID`. vJoy is `VID_1234` `PID_BEAD`, hence
`{BEAD1234-...}`. To read your own IDs:

```powershell
Get-ChildItem "HKCU:\System\CurrentControlSet\Control\MediaProperties\PrivateProperties\Joystick\OEM" |
  ForEach-Object { "{0} -> {1}" -f $_.PSChildName, (Get-ItemProperty $_.PSPath).OEMName }
```

**2. Copy [`docs/dirt4/vjoy_wheel.xml`](docs/dirt4/vjoy_wheel.xml)** to
`<game>\input\actionmaps\vjoy_wheel.xml`. The file name must match the `name` attribute above.
It binds steering for you, which the settings screen makes awkward, since the engine treats the
axis as two halves.

**3. In game:** set the input preset to the vJoy wheel, **turn the in-game force feedback
strength down** before the first drive, and **leave the centring spring off**.

**4. In the window,** pick the **DiRT 4** profile and press **Start**, then start the game
from Steam as usual.

### Settings

The DiRT 4 profile holds the values the 2026-09-26 drive used:

```json
{ "strength": 0.449, "invert": true, "dir_mode": "sin", "max_force": 0.45 }
```

- **Adjust `strength` first**, in small steps, while driving.
- **`invert` must stay on.** DiRT 4's force points the same way as the steering angle, so
  applied unchanged it pulls the wheel into the corner.
- The wheel's own strength setting in the HORI app was measured as a gain stage under the old
  path. Whether it still applies over raw USB is unmeasured:
  [docs/tuning.md](docs/tuning.md#the-gain-stage-the-software-cannot-see).

### Known issues

- **Oscillation at high gain.** Too much `strength` makes the wheel hunt, and at worst sweep
  lock to lock. Lower it. An inverted damping force looks identical, so check the sign first.

---

## RaceRoom Racing Experience

**Tested on:** Steam build, with the old shim (v0.1.0). Not yet driven over raw USB. Free to
play, no kernel anti-cheat.

### Setup

Nothing to edit. Start the bridge, start the game, **bind steering in the game**, then drive.

### Known issues

None outstanding. 

---

## Automobilista 2 Demo

**Tested on:** Steam, `AMS2DemoAVX.exe`, with the old shim (v0.1.0). Not yet driven over raw
USB. No anti-cheat. The full game is untested and may differ.

### Setup

**1. Set Controller Damping to 0**, under Options > Controls > Configuration. **At 100,
steering is completely dead on track** while the menus, calibration, pedals and force feedback
all look perfect. The Custom Wheel preset can leave it at 100, so the usual advice for an
unrecognised wheel is what puts it there.

**2. Leave Speed Sensitivity at 0**, which is correct for a wheel.

**3. Bind steering in the game**, then drive.

### Known issues

- **A centre deadzone that calibration does not show, and throttle bleeding into steering.**
  Both are the game's, and there is no fix. AMS2 treats a wheel it does not recognise as a
  gamepad and applies speed-dependent deadzone and sensitivity filtering, and unlike DiRT 4
  there is no device database to edit.
  [Reiza's own thread](https://forum.reizastudios.com/threads/vjoy-not-working.10026/) about
  this with vJoy ran from April 2020 to December 2021 and reached no fix: *"there is a 10%
  deadzone around center (for each side) and at each end. This is enforced, there is no option
  or workaround to disable it and it's there just while driving."*

  What we send is clean. Measured against `logs/vjoy_bridge_20260827_030410.log`, the steering
  fed to vJoy is the wheel position exactly, with no throttle term in it, at every position.

- **Not the problem.** All tried, none of them changed anything: Steam Input, deleting the
  Documents folder, Return to Defaults, and the over-rotate calibration trick.
