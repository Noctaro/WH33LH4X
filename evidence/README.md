# Evidence — what was tried, and what the hardware actually does

Four APIs were tested on Windows, and the only one that reached this wheel's motor,
`Windows.Gaming.Input`, did so only from the foreground window, which is why versions up to
v0.1.0 ran a shim inside the game. Raw USB turned out to be the route with no such gate, and it
is what the project uses now: [`RAW_USB.md`](RAW_USB.md) is the write-up. **The answers are
below. The code is here so the answers can be re-checked, not because anyone needs to run it.**

Nothing here ships in the download, and nothing here is imported by the bridge or the window.
Scripts marked *(removed)* went with the WGI tools in the switch to raw USB; git history has
them.

> If you are wondering "why didn't they just use X?" — X is probably in this table.

| Question | Answer | Script |
|---|---|---|
| Does **GameInput** expose force-feedback motors on this wheel? | **No.** Both the inbox v0 runtime and the v3 redistributable report `forceFeedbackMotorCount = 0`, with the struct-layout check passing — so it is a real zero, not a mis-read. GameInput was the one API with a background focus policy, which would have removed the need for the shim entirely. | `probe.py`, `gameinput_abi.py` |
| Does **DirectInput 8** expose force feedback on it? | **No.** The device enumerates as a joystick with no FFB capability in Xbox mode. | `dinput_probe.py` |
| Does the device publish a **USB HID PID** force-feedback collection? | **No** — and this is *why* the two answers above are no. The report descriptors carry no PID collection; force lives in vendor-defined collections instead. | `hid_probe.py` |
| Can **WGI drive the motor from a background process**? | **No.** Force output *and* position reading are both gated on the process owning the foreground. This is why the old shim had to run inside the game's own process, and why raw USB replaced it. | `wgi_background_test.py` *(removed)* |
| Can the **GIP protocol** be spoken directly, with no WGI in the process? | **Yes, over raw USB** — see [`RAW_USB.md`](RAW_USB.md). Torque, no Microsoft driver, no authentication, no foreground gate. Every earlier attempt failed for one reason: they replayed `logs/wgi.txt`, which was captured *above* the driver and is not what reaches the wire. Talking to the driver's own handle (`gip_direct.py`) remains a dead end. | [`../gip/arming.py`](../gip/arming.py), `gip_wheel_driver.py`, `gip_usb_host.py`, `usbpcap_parse.py` |

## Behaviour measurements

These are not dead ends. They answer questions about how the wheel behaves, and the answers are
load-bearing for features that ship — which is exactly why they had to be measured rather than
assumed.

| Question | Answer | Script |
|---|---|---|
| Does holding an effect suspend the wheel's own centring spring? | **Yes.** Stiff and precisely self-centring with no software running; slack while an effect is held. But **releasing the effect does not give it back** — after `try_unload_effect_async` and `try_reset_async` both returned `True` the wheel stayed just as slack, so the device is claimed for the life of the PROCESS. Measured 2026-08-26 under WGI. | `centring_test.py` *(removed)* |

| Does a PRE-CHARGED effect load revive a motor that has gone silent? | **No -- the opposite.** Reloading at zero and then commanding the force revived it; loading an effect already carrying the force did not (0 of 2), nor did waiting (0 of 2). Reverses a 2026-08-25 claim taken from two uncontrolled runs. | `revive_test.py` *(removed)* |

The first is why the profiles supply a software spring at all. The second applied to WGI only.

**Silencing this motor on demand took LOW force, not high**, under WGI: creep up from 0.002 and
back off whenever the wheel moves, so the motor sits just under the level that would move it.
Leaning on an end stop at 0.35 never reproduced it. It has not been seen over raw USB.

## Running them

They import modules from the repo root, so run them as modules **from the repo root**:

```powershell
.\.venv\Scripts\python.exe -m evidence.hid_probe
.\.venv\Scripts\python.exe -m evidence.probe --help
```

`python evidence\probe.py` will not work — the root would not be on `sys.path`.

## Why keep any of it

A negative result is only worth having if it is checkable. "GameInput reports zero motors" is a
claim; `probe.py` is what lets the next person confirm it against a newer runtime, a different
wheel, or a Windows update — instead of spending the same days rediscovering it.

`gip_protocol.py` is the exception worth reading for its own sake: it decodes the wire format
this wheel actually speaks. It documents the hardware, whatever anyone decides to do next.
[`gip/arming.py`](../gip/arming.py) goes further and *generates* that format, verified
byte-identical to a real USB capture. It has moved out of `evidence/` because the bridge uses it.

## What stayed in the root

`dinput_abi.py` — it is a working DirectInput 8 binding that `vjoy_ffb_spike.py` uses to test
the vJoy path, so it is live code, not evidence.
