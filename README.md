<p align="left"><img src="logo/logo.jpg" alt="WH33LH4X logo" width="240"></p>

# WH33LH4X

**Force feedback for the Hori Force Feedback Racing Wheel DLX in PC racing games, on Windows
and Linux.**

WH33LH4X talks to the wheel directly over USB and drives its motor. Games see an ordinary force
feedback wheel: a vJoy device on Windows, a virtual wheel on Linux. A small window picks the
game's profile, tunes the feel while you drive and tests the wheel.

**This is a beta.** It has been tested on one wheel, one Windows PC and one Linux machine, with
DiRT 4. If you try it, please [report how it went](../../issues/new?template=test-report.yml),
whether it worked or not: that is what decides when it becomes 1.0.

## Does this work for me?

- **The wheel** is a Hori Force Feedback Racing Wheel DLX (`VID 0x0F0D`), in **Xbox mode**
  (`PID 0x015C`). A long press of the PROFILE button switches out of PC mode.
- **The game** has to be one that works. [GAMES.md](GAMES.md) has the list and the per-game
  setup. DiRT 4 is confirmed on hardware, on Windows and Linux. Games with kernel anti-cheat,
  such as EasyAntiCheat or BattlEye, are untested.
- **On Windows the wheel belongs to this tool while it is set up.** Talking to it over USB means
  swapping Microsoft's Xbox driver for WinUSB, and while WinUSB is bound, Xbox games and the
  HORI app no longer see the wheel. The window can put the Microsoft driver back in one click.
- **On Linux nothing is swapped.** A one-time setup script gives your user access to the wheel,
  and `xone`, if installed, keeps working for your other controllers.

## Quickstart

**1. Download** the latest `WH33LH4X-windows-x64.zip` from
[Releases](../../releases) and extract it anywhere. It carries its own Python, so there is
nothing else to install.

**2. Install vJoy 2.2.2.0 and set up device 1:** [docs/vjoy.md](docs/vjoy.md).

**3. Run `WH33LH4X-GUI.cmd`.** Its Setup section checks vJoy and the wheel. If the wheel line
says *Microsoft driver*, press **How to fix**: it walks you through binding WinUSB with
[Zadig](https://zadig.akeo.ie).

**4. Press Test force**, hands off the wheel. It turns a little right and left and tells you
whether the whole chain works.

**5. Do the per-game setup** from [GAMES.md](GAMES.md), pick the game's profile, press
**Start**, and start the game however you normally do.

**6. Drive.**

Prefer a terminal? `WH33LH4X.cmd` runs the bridge without the window. See
[COMMANDS.md](COMMANDS.md).

### On Linux

No driver swap and no vJoy: games see a virtual wheel through uinput.

1. Download `WH33LH4X.flatpak` and `wh33lh4x-setup.sh` from [Releases](../../releases).
2. Run the setup once. It gives your user access to the wheel and keeps the wheel awake.

   ```
   sudo sh wh33lh4x-setup.sh
   flatpak install --user WH33LH4X.flatpak
   ```

3. Log out and back in, replug the wheel and let its calibration sweep finish.
4. Start **WH33LH4X** from the app menu and carry on from step 4 above.

Headless, over SSH: `flatpak run io.github.Noctaro.WH33LH4X bridge`, with any of the bridge's
flags after it.

## Tuning

Pick a **profile** (Default, DiRT 4, RC car; on Linux DiRT 4 (Linux)), then adjust. The
sliders work **while you drive**: a change reaches the wheel within half a second. **Save** keeps it in the
profile, **Save as new** makes your own.

| Slider | What it does |
|---|---|
| **Strength** | How hard the game's own force is felt. `1.00` is exactly what the game asked for. Set this first |
| **Max force** | A ceiling on everything below it |
| **Spring** | Pulls back to centre. For games that send no centring force of their own, which includes DiRT 4 |
| **Damper** | Resists how fast you turn. Steadies the spring |
| **Friction** | Constant drag whenever the wheel moves: weight, rather than centring |

Everything else, including the settings that have no slider, is in
[docs/tuning.md](docs/tuning.md).

## Verify the download

Releases are built by GitHub Actions from this repository. Nothing is code signed, so Windows
may ask for confirmation on the first start.

To check that your download is the one CI built:

- **Windows:** `Get-FileHash WH33LH4X-windows-x64.zip` must match `SHA256SUMS.txt`, or run
  `gh attestation verify WH33LH4X-windows-x64.zip --repo Noctaro/WH33LH4X`.
- **Linux:** `sha256sum -c SHA256SUMS-linux.txt` in the download folder.

Details are in [SECURITY.md](SECURITY.md).

## The previous approach

[v0.1.0](../../releases/tag/v0.1.0) injected a DLL into the game instead. It is not
recommended.

## Where everything else is

**For users**

| Document | What is in it |
|---|---|
| [GAMES.md](GAMES.md) | Which games work, and the setup for each |
| [docs/vjoy.md](docs/vjoy.md) | Installing and setting up vJoy on Windows |
| [docs/tuning.md](docs/tuning.md) | Every tuning setting, and checking what the force did |
| [SECURITY.md](SECURITY.md) | What this tool changes on your system, and how to report a problem |

**For developers**

| Document | What is in it |
|---|---|
| [docs/development.md](docs/development.md) | Working on the code: venv, tests, building |
| [COMMANDS.md](COMMANDS.md) | Every script and every flag |
| [docs/hardware.md](docs/hardware.md) | What this wheel's firmware does and does not implement, measured |
| [evidence/](evidence/README.md) | Why the other APIs cannot drive this wheel, and how the raw USB route was found |

## Thanks ♥

This sits on top of other people's work, all of it open source, and some of it is the reason
the project exists at all.

**[vJoy](https://github.com/shauleiz/vJoy)**, Shaul Eizikovich, MIT. The virtual joystick
driver the Windows bridge is built on, kept alive by the forks of
**[njz3](https://github.com/njz3/vJoy)**, whose effect index this bridge depends on, and
**[BrunnerInnovation](https://github.com/BrunnerInnovation/vJoy)**, who signed it for
Windows 11.

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
