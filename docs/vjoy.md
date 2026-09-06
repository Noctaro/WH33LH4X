# vJoy: installing it, and the three ways it goes quietly wrong

The bridge presents your wheel to games as a vJoy virtual device, so vJoy has to be installed
and configured before anything else works.

**The window checks all of this for you.** If something on this page is wrong, the vJoy badge
in `WH33LH4X-GUI.cmd` says which thing and offers a *How do I fix this?* button. This page is
the long version, for when you would rather read than click.

## Install version 2.2.2.0 specifically

Install **[`BrunnerInnovation/vJoy` v2.2.2.0](https://github.com/BrunnerInnovation/vJoy)**.

The version matters more than it looks. vJoy 2.1.9.x has no force feedback *effect block
index*: every effect reports index `1`, so concurrent effects cannot be told apart. That is
invisible while testing one effect at a time and fatal in a real game, which runs a centring
spring, a damper and road texture simultaneously.

`njz3` added the block index in 2.2.0, and Brunner's 2.2.2.0 adds EV signing for Windows 11.
**The driver and the interface DLL are not compatible across the 2.1.9 to 2.2.0 boundary**, so
do not mix them.

## Configure one device

Installing the driver is not enough. vJoy ships with **no device configured**, so the bridge
has nothing to feed. Open **vJoyConf** (*Configure vJoy*), select device **1** on the tab
strip, and set:

| Section | What to set |
|---|---|
| **Axes (max 8)** | Tick **X, Y, Z, Rx, Ry**: steering, throttle, brake, clutch, handbrake |
| **Number of Buttons** | Enough for your wheel. Needed for the mid-corner tuning buttons |
| **Force Feedback** | Tick **Enable Effects**, and leave the individual effects ticked |
| bottom of the window | **Enable vJoy** ticked, then **Apply** |

Device 1 is what the bridge uses unless you pass `--device`.

## Three things that will mislead you

**The axis list also offers `Steering`, `Brake`, `Clutch` and `Throttle`**, which look like
exactly what a wheel wants. They are not what this bridge sends. It writes to **X, Y, Z, Rx and
Ry**, so ticking the plausible looking ones instead gives you a device that works and never
moves.

**vJoyConf says at the bottom that enabling force feedback on one device makes *all* vJoy
devices report as force feedback capable.** So "Windows shows it as a force feedback device" is
not evidence that device 1 is set up correctly. Only `vjoy_ffb_spike.py` reporting `PASS` is.

**Missing *Enable Effects* is the quietest failure of the three.** The axes still work, so the
wheel steers and the pedals respond, and there is simply no force. That reads exactly like a
broken wheel rather than a checkbox.

One thing that is *not* a trap: the axis list is a maximum, not a requirement. The bridge asks
vJoy which axes exist and feeds only those, so fewer axes means fewer controls rather than a
failure.

## Checking it end to end

```powershell
.\python\python.exe vjoy_ffb_spike.py     # from an extracted bundle
```

`vjoy_ffb_spike.py` is both halves of the test: it sends DirectInput effects to the virtual
device and logs what comes back out of vJoy's force feedback callback, so it verifies the whole
path without a game or a control panel tab. It never touches the real wheel.

**If it does not report `PASS`, stop.** Nothing built on top of vJoy will work, and adding a
game only makes it harder to see that.
