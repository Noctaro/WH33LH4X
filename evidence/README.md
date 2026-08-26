# Evidence — approaches that were tried and do not work

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
