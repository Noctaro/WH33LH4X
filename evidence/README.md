# Evidence — what was tried, and what the hardware actually does

Four APIs were tested before `Windows.Gaming.Input` plus an in-process shim turned out to be
the only route to this wheel's motor. **The answers are below. The code is here so the answers
can be re-checked, not because anyone needs to run it.**

Nothing here ships in the download. Nothing here is imported by the bridge, the shim, or any
diagnostic in the repo root.

> If you are wondering "why didn't they just use X?" — X is probably in this table.

| Question | Answer | Script |
|---|---|---|
| Does **GameInput** expose force-feedback motors on this wheel? | **No.** Both the inbox v0 runtime and the v3 redistributable report `forceFeedbackMotorCount = 0`, with the struct-layout check passing — so it is a real zero, not a mis-read. GameInput was the one API with a background focus policy, which would have removed the need for the shim entirely. | `probe.py`, `gameinput_abi.py` |
| Does **DirectInput 8** expose force feedback on it? | **No.** The device enumerates as a joystick with no FFB capability in Xbox mode. | `dinput_probe.py` |
| Does the device publish a **USB HID PID** force-feedback collection? | **No** — and this is *why* the two answers above are no. The report descriptors carry no PID collection; force lives in vendor-defined collections instead. | `hid_probe.py` |
| Can **WGI drive the motor from a background process**? | **No.** Force output *and* position reading are both gated on the process owning the foreground. This is the measurement the entire shim design rests on: it is why the final write has to happen inside the game's own process. | `wgi_background_test.py` |
| Can the **GIP protocol** be spoken directly, with no WGI in the process? | **No.** The arming sequence was captured from WGI, decoded, and replayed byte-for-byte. It produced no torque. **Do not retry this without new information** — it cost days. | `gip_direct.py`, `gip_trace.py`, `gip_diff.py`, `gip_protocol.py` |

## Behaviour measurements

These are not dead ends. They answer questions about how the wheel behaves, and the answers are
load-bearing for features that ship — which is exactly why they had to be measured rather than
assumed.

| Question | Answer | Script |
|---|---|---|
| Does holding an effect suspend the wheel's own centring spring? | **Yes.** Stiff and precisely self-centring with no software running; slack while an effect is held. But **releasing the effect does not give it back** — after `try_unload_effect_async` and `try_reset_async` both returned `True` the wheel stayed just as slack, so the device is claimed for the life of the PROCESS. Measured 2026-08-26. | `centring_test.py` |

| Does a PRE-CHARGED effect load revive a motor that has gone silent? | **No -- the opposite.** Reloading at zero and then commanding the force revived it; loading an effect already carrying the force did not (0 of 2), nor did waiting (0 of 2). Reverses a 2026-08-25 claim taken from two uncontrolled runs. | `revive_test.py` |

The first is why the GUI offers a software spring at all, and why `README.md` no longer claims
`r` restores stock centering. The second is why `WgiMotorSink` no longer has an `initial_force`
argument.

**Silencing this motor on demand takes LOW force, not high.** Creep up from 0.002 and back off
whenever the wheel moves, so the motor sits just under the level that would move it. Leaning on
an end stop at 0.35 never reproduced it — `revive_test.py --kill stop` keeps that version
around to show the difference.

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
this wheel actually speaks, captured from WGI. It documents the hardware, whatever anyone
decides to do next.

## What stayed in the root

`dinput_abi.py` — it is a working DirectInput 8 binding that `vjoy_ffb_spike.py` uses to test
the vJoy path, so it is live code, not evidence.
