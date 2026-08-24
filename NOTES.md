# Why it works this way

Design notes for anyone reading the code. [GAMES.md](GAMES.md) tells you how to get a game
working; this file explains why any of it is necessary, and records the measurements behind
decisions that look arbitrary.

Most of what follows was discovered the expensive way. It is here so it does not have to be
discovered twice.

## The shim has to live inside the game

`Windows.Gaming.Input` gates force output on the calling process being in the **foreground**.
Not muted — gated in the driver stack, kernel-side, with no user-mode way around it. There is
no focus API imported by `Windows.Gaming.Input.dll` to hook, and GameInput reports zero
force-feedback motors for this wheel.

**Position reading is gated the same way.** `get_current_reading().wheel` returns `0.0` from a
background process, so a background feeder cannot even read the wheel to forward its axes.

Together those mean a separate-process bridge is impossible. While a game is in front, only the
game's own process can talk to this motor — so the output stage runs inside it, as a
`dinput8.dll` proxy that a game loads from its own directory ahead of the system one.

The Python bridge still exists, outside the game: it owns vJoy, decodes the DirectInput effects
the game sends, and renders them to a single force value. The shim reads that value out of
shared memory and commands the motor. Input flows the other way through the same section.

## Claiming the motor is easy; letting go of it is not

Loading a WGI effect and then unloading it can leave the motor **accepting effects while
producing no torque**. Load reports `Succeeded`, the effect reports `Running`, the process is
foreground, and the wheel is completely still. It is indistinguishable from a hardware fault,
and only restarting the game clears it.

Two consequences, both of which cost real debugging time:

**The effect is loaded once and held for the life of the game.** An earlier version released it
whenever the bridge looked quiet, on the theory that holding a motor nothing is driving was
antisocial. That killed the wheel 46 times in one evening. When the bridge goes quiet the shim
now commands zero force and keeps the claim — a limp wheel is the right answer to a dead bridge;
a permanently dead one is not.

**`wgi_release_effect` resets the motor as part of releasing.** Previously the reset lived only
in `wgi_close`, with a comment explaining exactly why it was mandatory — so the one path that
ran once was correct and the path that ran constantly was not. It is now inside the release, and
no caller has to remember.

## Losing focus kills a held effect permanently

Leaving the game does not merely mute the wheel: **the firmware takes the motor back**, and you
can feel it snap to its own centring spring at the moment focus goes. What the shim is holding
afterwards is a corpse — still `Running`, still accepting magnitudes, driving nothing — and it
never recovers on its own.

So the shim watches the foreground and rebuilds the effect on the way back in. This is the one
place where releasing is correct, and it is safe because the release now resets.

Verified 2026-08-24 across two long absences:

```
focus: back after  432891 ms away -- rebuilding the effect (the firmware had the motor)
focus: back after 2728047 ms away -- rebuilding the effect (the firmware had the motor)
```

7 minutes and 45 minutes; force returned by itself both times.

**`FOCUS_REBUILD_MS` is 750 ms and that number is a guess.** Short absences have been observed
recovering without a rebuild, so the threshold exists to avoid paying for a reset and a load on
every flicker of focus. Where the real boundary sits is unknown: no absence short enough to test
it has happened yet. A `focus: back after N ms away -- kept the effect` line in
`%TEMP%\wh33lh4x_shim.log` is the case that would tell us.

## The `0 ms stale` heartbeat race

The bridge stamps a heartbeat into shared memory every tick; the shim treats a stamp older than
500 ms — or exactly zero — as a dead bridge. A few times an hour the shim declares the bridge
quiet and picks it up again 11 ms later, one tick, reporting `0 ms stale`.

That number never made sense. `0 ms` should mean *fresh*, and the accessor conflates the two
cases by returning `0` for a zero stamp as well. The cause turned out to be the write itself:

```
>>> buf = ctypes.create_string_buffer(b'\xff'*16, 16)
>>> struct.pack_into('@BI', buf, 0, 1, 2)
>>> buf.raw[:8].hex()
'0100000002000000'
```

Those `000000` bytes were `ff` before the call. **`struct.pack_into` memsets its target region
to zero before writing it.** So every heartbeat leaves an 8-byte window where `bridge_tick_ms`
reads exactly `0`, and the shim polls that address at 100 Hz from another core with no
synchronisation.

Not a timeout, not focus, and not the `close()` zeroing that was fixed earlier — three
hypotheses this signature supported and outlived. Cost today is one tick of zero force, well
below what a hand can feel. The fix is a single atomic store (a `ctypes.c_uint64` view over the
mmap) instead of memset-then-write, plus a sentinel from the staleness accessor so the two
cases stop looking alike.

**The lesson worth keeping:** a shared section addressed by name has no owner. Anything a
process writes there — including during a routine update — it writes on behalf of everyone
reading it.

## Why DiRT 4 needs its device registered

Codemasters' EGO engine classifies input devices from its own database,
`input/devices/device_defines.xml`, keyed by DirectInput GUID. Anything not listed falls back to
`type="unknown"` and gets a **generic, non-directional** force feedback profile: unsigned
magnitude with no usable direction.

The symptom is specific and misleading. The wheel pulls one way permanently and never centres,
both in-game force feedback sliders do nothing at all — and kerbs and gravel still feel
completely correct, because rectification is inaudible on symmetric effects. A full session was
described as "bumps and gravel feel ok" while the output was rectified. Only arithmetic on a log
caught it, which is what `tune_report.py` is for.

`type="wheel"` is the attribute that matters.

## Why the action map is a file, not a binding

The engine binds steering as **two halves of one axis** — `di_x_axis` with `type="lower"` and
`type="upper"`. The in-game controls menu cannot express that: assigning the first direction
works, and assigning the second drops the device with *"Steuerungsgerät geändert / the
connection to an input device was disconnected"*. Assigning them in the other order fails the
same way, so it is the second assignment that breaks, not a particular direction.

Declaring both halves in `input/actionmaps/vjoy_wheel.xml` skips the binding flow entirely.

## Why `invert: true` for DiRT 4

Measured open-loop: force output at zero, the game's centring spring disabled, car driving, so
the wheel was moved only by hand and nothing we did could contaminate the correlation.

```
140 samples, mean |force| 0.596
96% the same sign as steering
wheel LEFT   mean angle -0.31 -> mean force -0.547
wheel RIGHT  mean angle +0.31 -> mean force +0.554
```

Symmetric on both sides. Applied unchanged that is positive feedback — the wheel runs away into
whatever corner you turn into, hardest under throttle. Inverted, it loads up through a corner
and returns to centre.

The likely cause is our own plumbing rather than the game: this wheel's motor drives opposite to
the sign of its own position reading, so one global flip corrects every effect at once.

**Measure this open-loop or not at all.** Once we are applying force, wheel position and game
force are mutually causal and the correlation between them measures the loop, not the game. Two
confident and completely wrong sign verdicts came out of contaminated windows — one of them
`r = -0.998`, over forces averaging 0.038 of full scale, which was describing noise.

## Known rough edges

- **Per-game setup is a manual XML edit inside somebody's Steam folder.** Registering the device
  and installing the action map both mean finding the game directory by hand and editing files
  in it, then doing it again after every game update, since Steam reverts both. `play.ps1`
  already does this properly for the shim — deploy before launch, remove afterwards — and the
  per-game files deserve the same treatment: ask for (or detect) the game path, back up what is
  touched, apply, and be able to undo. Worth building before a second game is added, because
  that is where hand-written per-game instructions stop scaling.
- **The heartbeat race above** is documented but not yet fixed.
- **`FOCUS_REBUILD_MS` is unmeasured**, as described above.

## See also

- [GAMES.md](GAMES.md) — per-game setup and status.
- [README.md](README.md) — the wheel, the probe tool, and live tuning.
- `.claude/memory/` — the full discovery record, including the three APIs that turned out to be
  dead ends. `wgi-forcefeedback-api-gotchas.md` and `hori-wheel-ffb-probe-goal.md` are the two
  worth reading.
