# Game compatibility

What each game needs, and what is still wrong with it. Tried a game? Send a
[test report](../../issues/new?template=test-report.yml).

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
| 🟢 Works | Steering and force feedback both correct |
| 🟡 Playable | Drivable, but something is degraded or still being worked on |
| 🔴 Broken | Does not work, and the reason is recorded |
| ⚪ Untested | Nobody has tried it with this version yet |

Games with kernel anti-cheat (EasyAntiCheat, BattlEye) are untested.

---

## DiRT 4

**Tested on:** Steam, 2026-09-26 on Windows, 2026-09-27 on Linux (Feral's port and the
Windows version under Proton).

### Setup on Windows

`<game>` is the folder Steam opens under *Manage > Browse local files*. Back up
`device_defines.xml` first: Steam's *Verify integrity of game files* restores both files, so
redo these steps after a game update.

1. **Register vJoy as a wheel.** In `<game>\input\devices\device_defines.xml`, add this line
   before the closing `</device_list>`:

   ```xml
   <device id="{BEAD1234-0000-0000-0000-504944564944}" name="vjoy_wheel" priority="100" type="wheel" ffb="enabled" />
   ```

2. **Copy [`docs/dirt4/vjoy_wheel.xml`](docs/dirt4/vjoy_wheel.xml)** to
   `<game>\inputctionmapsjoy_wheel.xml`. It binds steering for you.
3. **In the window,** pick the **DiRT 4** profile and press **Start**.
4. **Start the game.** Set the input preset to the vJoy wheel, turn the in-game force feedback
   strength down before the first drive, and leave the centring spring off.

### Setup on Linux

Do the [one-time Linux setup](README.md#on-linux) first, and use the **DiRT 4 (Linux)**
profile. The game sees the wheel only while the bridge runs, so **press Start before starting
the game**.

**Feral's Linux port** (what Steam installs by default):

1. In `<game>/share/inputdevices.json`, add this line as the first entry after the `_Comment`
   line:

   ```json
   {"Name": "WH33LH4X virtual wheel", "VendorID": "0x1234", "ProductIDs": ["0xbead"], "Category": "Wheel", "Type": "ThrustmasterWheel"},
   ```

2. Do steps 1 and 2 of the Windows setup, with the files under `<game>/share/data/input/`.

**The Windows version under Proton** (chosen under *Properties > Compatibility*): do steps 1 and
2 of the Windows setup, with the files under `<game>/input/`.

### Settings

The profile has the right values. Change **Strength** first, in small steps, while driving. If
two PCs feel different with the same settings, check the
[strength in the HORI app](docs/tuning.md#the-strength-setting-in-the-hori-app).

### Known issues

- Too much Strength makes the wheel oscillate. Lower it.
- The on-screen wheel turns less than the real one. DiRT 4 has no setting to match a wheel's
  rotation.

---

## RaceRoom Racing Experience

**Tested on:** Steam, with the old shim (v0.1.0) only. Free to play, no kernel anti-cheat.

### Setup

1. Press **Start** in the window.
2. Start the game and bind steering in its controls.

---

## Automobilista 2 Demo

**Tested on:** Steam, `AMS2DemoAVX.exe`, with the old shim (v0.1.0) only. No anti-cheat. The
full game is untested.

### Setup

1. **Set Controller Damping to 0** under *Options > Controls > Configuration*. At 100 the
   steering is dead on track. The Custom Wheel preset can set it to 100.
2. **Leave Speed Sensitivity at 0.**
3. **Bind steering** in the game, then drive.

### Known issues

- **A centre deadzone and throttle bleeding into steering.** The game filters wheels it does not
  recognise, and there is no fix
  ([Reiza's thread](https://forum.reizastudios.com/threads/vjoy-not-working.10026/)).
- **Tried without effect:** Steam Input, deleting the Documents folder, Return to Defaults, and
  the over-rotate calibration trick.
