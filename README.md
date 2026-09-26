<p align="left"><img src="logo/logo.jpg" alt="WH33LH4X logo" width="240"></p>

# WH33LH4X

**Force feedback for the Hori Force Feedback Racing Wheel DLX in PC racing games.**

The wheel has two USB modes and both are dead ends for sims. In PC mode DirectInput sees the
wheel, but its joystick collection is input only, so there is no force feedback for any driver
to expose. In Xbox mode force feedback works, but only through `Windows.Gaming.Input`, and
racing games drive wheels through DirectInput. The result is a force feedback wheel with no
force feedback in any PC sim.

This closes that gap. It talks to the wheel directly over USB, presents it to games as an
ordinary DirectInput force feedback wheel through vJoy, and drives the real motor itself.
Nothing is injected into the game.

## Does this work for me?

- **The wheel** is a Hori Force Feedback Racing Wheel DLX (`VID 0x0F0D`), in **Xbox mode**
  (`PID 0x015C`). A long press of the PROFILE button switches out of PC mode.
- **The game** has to be one that works. [GAMES.md](GAMES.md) has the list and the per-game
  setup. DiRT 4 is confirmed on hardware.
- **The wheel belongs to this tool while it is set up.** Talking to it over USB means swapping
  Microsoft's Xbox driver for WinUSB, and while WinUSB is bound, Xbox games and the HORI app no
  longer see the wheel. The window can put the Microsoft driver back in one click.

## Quickstart

**1. Download** the latest `WH33LH4X-windows-x64.zip` from
[Releases](../../releases) and extract it anywhere. It carries its own Python, so there is
nothing else to install.

**2. Install [vJoy 2.2.2.0](https://github.com/BrunnerInnovation/vJoy)** and configure device 1.
The version matters, and so do a few checkboxes. You do not have to get it right from memory:
the window checks all of it and tells you exactly what to change. Full walkthrough in
[docs/vjoy.md](docs/vjoy.md).

**3. Run `WH33LH4X-GUI.cmd`.** Its Setup section checks vJoy and the wheel. If the wheel line
says *Microsoft driver*, press **How to fix**: it walks you through binding WinUSB with
[Zadig](https://zadig.akeo.ie), which takes a minute and is needed once.

**4. Press Test force**, hands off the wheel. It turns a little right and left and tells you
whether the whole chain works.

**5. Do the per-game setup** from [GAMES.md](GAMES.md), pick the game's profile, press
**Start**, and start the game however you normally do.

**6. Drive.**

Prefer a terminal? `WH33LH4X.cmd` runs the bridge without the window. See
[COMMANDS.md](COMMANDS.md).

## Tuning

Pick a **profile** (Default, DiRT 4, RC car), then adjust. The sliders work **while you
drive**: a change reaches the wheel within half a second, mid corner. **Save** keeps it in the
profile, **Save as new** makes your own.

| Slider | What it does |
|---|---|
| **Strength** | How hard the game's own force is felt. `1.00` is exactly what the game asked for. Set this first |
| **Max force** | A ceiling on everything below it |
| **Spring** | Pulls back to centre. For games that send no centring force of their own, which includes DiRT 4 |
| **Damper** | Resists how fast you turn. This is what stops the spring overshooting and hunting |
| **Friction** | Constant drag whenever the wheel moves: weight, rather than centring |

Everything else, including the settings that have no slider, is in
[docs/tuning.md](docs/tuning.md).

## Why your antivirus may complain

Nothing here is code signed. The download is Microsoft's own signed `python.exe` running
readable Python scripts, and nothing is copied into a game folder or loaded into a game.

What you can check instead of trusting a signature:

- **Read it.** Every file in the download is plain source.
- **Or verify what you downloaded.** Releases are built by GitHub Actions from public source,
  and each one ships `SHA256SUMS.txt` plus a GitHub artifact attestation, so you can prove the
  zip you have is the one CI built from a named commit.

The reasoning and the verification commands are in [SECURITY.md](SECURITY.md).

## The previous approach

Up to [v0.1.0](../../releases/tag/v0.1.0) this project put a `dinput8.dll` into the game folder
and drove the motor from inside the game, because Windows only lets the window in front talk to
the motor. Raw USB has no such gate, so that DLL, and the antivirus and anti-cheat trouble it
brought, are gone. v0.1.0 stays available for anyone who cannot swap the driver.

## Where everything else is

| Document | What is in it |
|---|---|
| [GAMES.md](GAMES.md) | Which games work, the per-game setup, and the quirks of each |
| [docs/vjoy.md](docs/vjoy.md) | Installing and configuring vJoy, and the three ways it silently goes wrong |
| [docs/tuning.md](docs/tuning.md) | Every tuning key, and reading back what the force actually did |
| [docs/hardware.md](docs/hardware.md) | What this wheel's firmware does and does not implement, measured |
| [docs/development.md](docs/development.md) | Working on the code: venv, tests, building |
| [COMMANDS.md](COMMANDS.md) | Every script and every flag |
| [evidence/](evidence/README.md) | Why the other APIs cannot drive this wheel, and how the raw USB route was found |

## Thanks ♥

This sits on top of other people's work, all of it open source, and some of it is the reason
the project exists at all.

**[vJoy](https://github.com/shauleiz/vJoy)**, Shaul Eizikovich, MIT. The virtual joystick
driver the bridge is built on. Upstream is abandoned, but the forks kept it alive:
**[njz3](https://github.com/njz3/vJoy)** added the force feedback effect block index in 2.2.0,
without which concurrent effects cannot be told apart and this bridge does not work, and
**[BrunnerInnovation](https://github.com/BrunnerInnovation/vJoy)** carried that into a signed
Windows 11 build.

**[pyvjoyffb](https://github.com/Ultrawipf/pyvjoy)**, Yannick Richter, MIT, forked from
**[tidzo/pyvjoy](https://github.com/tidzo/pyvjoy)**. Decodes force feedback through vJoy's own
exports instead of reimplementing HID PID, and bundles the matching DLL.

**[libusb](https://libusb.info)** and **[pyusb](https://github.com/pyusb/pyusb)**, which carry
every byte to and from the wheel, and **[Zadig](https://zadig.akeo.ie)**, which makes binding
WinUSB a one-minute job.

**[xone](https://github.com/medusalix/xone)**, whose open implementation of Microsoft's Game
Input Protocol made the wheel's USB traffic readable.

**[Sun Valley ttk theme](https://github.com/rdbende/Sun-Valley-ttk-theme)**, rdbende, MIT, which
makes the window look like it should.

No HORI code or assets are used anywhere in this project.

## License

MIT, see [LICENSE](LICENSE). What the project depends on, who holds copyright on it and under
what terms, is recorded in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
