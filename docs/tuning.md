# Tuning

The profiles and sliders in the window cover what most people change; see
[the README](../README.md#tuning). Everything else is in `user-tune.json`:

| Install | Where `user-tune.json` is |
|---|---|
| Windows | Next to `WH33LH4X-GUI.cmd` |
| Linux (Flatpak) | `~/.var/app/io.github.Noctaro.WH33LH4X/data/` |

The bridge re-reads the file while it runs, so a saved change reaches the wheel within half a
second.

## Every setting

| Key | What it does |
|---|---|
| `strength` | Multiplies the game's force. The slider stops at `1.00`; the file goes higher |
| `max_force` | Ceiling on the force sent to the wheel |
| `min_force` | Smallest force sent, so weak effects are still felt |
| `spring` | Pulls the wheel back to centre. `0` is off |
| `damper` | Resists how fast you turn. `0` is off |
| `friction` | Drag whenever the wheel moves. `0` is off |
| `centring` | `"linear"` uses `spring` and `damper` as they are. `"tuned"` adds a minimum centring force of `centring_floor` once the wheel is off centre, as the RC car profile does |
| `invert`, `dir_mode` | Set by the game's profile. Change them only for a game that has none |
| `btn_down`, `btn_up`, `btn_next` | Wheel buttons that change a setting while you drive. `btn_next` picks the setting, `btn_up` and `btn_down` change it. `0` is unassigned; the numbers are below |

Button numbers for `btn_down`, `btn_up` and `btn_next`:

| Number | Button |
|---|---|
| 1, 2 | Two more buttons, not yet confirmed which; try them |
| 3 to 6 | D-pad: up, down, left, right |
| 7 to 10 | A, B, X, Y |

## The strength setting in the HORI app

The wheel has a strength setting of its own, in the **HORI FFB RWD-Devicemanager für Xbox Series
X Series S** app from the Microsoft Store. You operate the app with the wheel's buttons, not the
mouse. It works on top of everything here, so if two PCs feel different with the same settings,
compare it first. Set it before binding WinUSB: while WinUSB is bound, the app cannot reach the
wheel.

## Checking what the force did

After a drive, run:

| Install | Command |
|---|---|
| Windows | `.\python\python.exe tune_report.py` in the WH33LH4X folder |
| Linux (Flatpak) | `flatpak run io.github.Noctaro.WH33LH4X tune_report` |

It reports which effects the game sent and how force related to steering. The force must
**oppose** the steering angle; if it follows it, the wheel pulls into corners and the profile's
`invert` is wrong for that game.
