# WH33LH4X

**Force feedback for the Hori Force Feedback Racing Wheel DLX in PC racing games.**

The wheel has two USB modes and both are dead ends for sims. In PC mode DirectInput sees the
wheel, but its joystick collection is input only, so there is no force feedback for any driver
to expose. In Xbox mode force feedback works, but only through `Windows.Gaming.Input`, and
racing games drive wheels through DirectInput. The result is a force feedback wheel with no
force feedback in any PC sim.

This closes that gap. It presents the wheel to games as an ordinary DirectInput force feedback
wheel, and drives the real motor itself.

## Does this work for me?

- **The wheel** is a Hori Force Feedback Racing Wheel DLX (`VID 0x0F0D`), in **Xbox mode**
  (`PID 0x015C`). A long press of the PROFILE button switches out of PC mode.
- **The game** has to be one that works. [GAMES.md](GAMES.md) has the list and the per-game
  setup. DiRT 4 is confirmed on hardware.
- **Not a game with kernel anti-cheat** such as EasyAntiCheat or BattlEye. This tool puts a DLL
  in the game folder, which is exactly the thing they exist to stop, and meaning well is not a
  defence they accept.

## Quickstart

**1. Download** the latest `WH33LH4X-windows-x64.zip` from
[Releases](../../releases) and extract it anywhere. It carries its own Python, so there is
nothing else to install.

**2. Install [vJoy 2.2.2.0](https://github.com/BrunnerInnovation/vJoy)** and configure device 1.
The version matters, and so do a few checkboxes. You do not have to get it right from memory:
the window checks all of it and tells you exactly what to change. Full walkthrough in
[docs/vjoy.md](docs/vjoy.md).

**3. Do the per-game setup** from [GAMES.md](GAMES.md).

**4. Run `WH33LH4X-GUI.cmd`.** Pick the game's `.exe`, press **Start**.

The window handles the parts that are easy to get wrong. It launches Steam games the way Steam
insists on, shows what is known to be quirky about the title you picked, and reports whether
the bridge is really live rather than leaving you to guess.

**5. Drive.**

Prefer a terminal? Everything the window does, `WH33LH4X.cmd` does too. See
[COMMANDS.md](COMMANDS.md).

## Tuning
The sliders work **while you drive**: a change reaches the wheel within half a
second, mid corner.

| Slider | What it does |
|---|---|
| **Strength** | How hard the game's own force is felt. `1.00` is exactly what the game asked for. Set this first |
| **Spring** | Pulls back to centre. For games that send no centring force of their own, which includes DiRT 4 |
| **Damper** | Resists how fast you turn. This is what stops the spring overshooting and hunting |
| **Friction** | Constant drag whenever the wheel moves: weight, rather than centring |

One thing worth knowing: **the wheel has a strength setting of
its own**, in the *HORI FFB RWD-Devicemanager für Xbox Series X Series S* app from the
Microsoft Store. It is a real gain stage above everything here, worth about 3.5x between its
lowest and highest setting, and nothing in this project can read or change it. If two machines
feel different with identical settings, look there first.

Everything else, including the settings that have no slider, is in
[docs/tuning.md](docs/tuning.md).

## Why your antivirus may complain

**This copies an unsigned `dinput8.dll` into your game's folder while you play, and removes it
when the game exits.** That is the same mechanism malware uses to get code running inside
another program, so a scanner objecting to it is not being stupid. Nothing here is code signed
either. Both of those are worth saying plainly rather than hoping you do not notice.

What you can check instead of trusting a signature:

- **Build it yourself.** The DLL is compiled from `shim/*.c` in this repo by one script.
- **Or verify what you downloaded.** Releases are built by GitHub Actions from public source,
  and each one ships `SHA256SUMS.txt` plus a GitHub artifact attestation, so you can prove the
  zip you have is the one CI built from a named commit.

The reasoning and the verification commands are
in [SECURITY.md](SECURITY.md).

## Where everything else is

| Document | What is in it |
|---|---|
| [GAMES.md](GAMES.md) | Which games work, the per-game setup, and the quirks of each |
| [docs/vjoy.md](docs/vjoy.md) | Installing and configuring vJoy, and the three ways it silently goes wrong |
| [docs/tuning.md](docs/tuning.md) | Every `tune.json` key, and reading back what the force actually did |
| [docs/hardware.md](docs/hardware.md) | What this wheel's firmware does and does not implement, measured |
| [docs/development.md](docs/development.md) | Working on the code: venv, the probe tool, tests, building |
| [COMMANDS.md](COMMANDS.md) | Every script and every flag |
| [evidence/](evidence/README.md) | Four APIs that do **not** work on this wheel, and the probes that prove it |

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
exports instead of reimplementing HID PID, and bundles the matching DLL. That is the whole
usermode half of the problem, solved by someone else.

No HORI code or assets are used anywhere in this project.

## License

MIT, see [LICENSE](LICENSE). What the project depends on, who holds copyright on it and under
what terms, is recorded in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
