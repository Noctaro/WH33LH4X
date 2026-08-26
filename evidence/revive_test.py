r"""
revive_test.py -- does a PRE-CHARGED effect load revive a motor that has gone silent?

THE CLAIM UNDER TEST (.claude/memory/wgi-forcefeedback-api-gotchas.md):
    "A fresh effect loaded already carrying 0.30 moved the same wheel" after a load-at-zero
    effect drove nothing. Recorded from two runs, an hour apart, and never controlled.

WHY THE CONTROL MATTERS. The original observation changed TWO things at once: it reloaded the
effect, and it loaded that effect with a magnitude. If a fresh load at ZERO revives the motor
just as well, the pre-charge is doing nothing and `initial_force` can go.

    --variant preload   unload, load a fresh effect ALREADY CARRYING 0.30, start
    --variant zero      unload, load a fresh effect at 0.0, start, then set 0.30
    --variant wait      change nothing, just wait the same time and probe again

`wait` is the third control: if the motor comes back on its own, neither reload proves anything.

RESULT, 2026-08-26, Hori Force Feedback Racing Wheel DLX:

    zero      REVIVED       1 of 1 valid runs
    preload   still silent  0 of 2
    wait      still silent  0 of 2

So the pre-charge is not the thing that works -- it is the thing that fails. The 2026-08-25
claim this script was written to check said the opposite, and it was two uncontrolled runs.
`initial_force` was dropped from WgiMotorSink the same day.

n is small and the positive is a single trial: the second `zero` run never silenced the motor,
so it returned no verdict. One run also went SILENT -> DRIVES by itself mid-kill, which is why
`wait` has to stay in any future version of this test.

HOW THE MOTOR IS KILLED. Two strategies, because the first one was wrong.

    --kill low    (default) creep up from 0.002 in 0.002 steps, backing off the moment the
                  wheel moves, so the motor sits just under the level that would move it.
    --kill stop   drive to an end stop at 0.35 and lean on it at 0.05.

`stop` was the original idea and IT DOES NOT REPRODUCE THE FAILURE on this wheel -- the
firmware is perfectly happy to shove hard against a mechanical limit. Every real observation of
the motor going silent was the opposite regime: `stiction_test` at `--step 0.01`, forces around
breakaway (below 0.01), two short hums and then no torque for the rest of the process. `--step
0.02` never once triggered it in 24 passes. So the trigger looks like SMALL force that fails to
move the wheel, not large force that cannot.

ONE TRIAL PER PROCESS, because a fresh process is the only known way back.
"""

import argparse
import asyncio
import sys
import time
from datetime import timedelta

try:
    from winrt.runtime import init_apartment
except ImportError:
    init_apartment = None

import winrt.windows.gaming.input.forcefeedback as ff
from winrt.windows.foundation.numerics import Vector3

from wgi_probe import PumpThread, has_motor, report, rule, wait_for_devices

MOVED = 0.01
PROBE_FORCE = 0.30
PROBE_SECONDS = 1.5
TRAVEL_FORCE = 0.35
STALL_FORCE = 0.05
STALL_SECONDS = 2.0
LOCK = 0.80

# The low-force kill. Breakaway on this wheel is below 0.01, so these steps straddle it.
LOW_START = 0.002
LOW_STEP = 0.002
LOW_CEILING = 0.05
LOW_DWELL = 1.2
CREEP = 0.003        # smaller than MOVED: at these forces a real move is a few thousandths
PROBE_EVERY = 3


def pos(wheel):
    try:
        return wheel.get_current_reading().wheel
    except Exception:
        return None


class Effect(object):
    """One loaded effect. Nothing here is shared with the repo's sinks on purpose."""

    def __init__(self, motor, loop):
        self.motor, self.loop = motor, loop
        self.effect = None

    def load(self, magnitude, gain=1.0):
        try:
            self.motor.master_gain = gain
        except Exception:
            pass
        e = ff.ConstantForceEffect()
        e.set_parameters(Vector3(magnitude, 0.0, 0.0), timedelta(seconds=3600))
        result = self.loop.run_until_complete(self.motor.load_effect_async(e))
        if result != ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
            print("  load FAILED: %s" % result)
            return False
        self.effect = e
        try:
            if self.motor.are_effects_paused:
                self.motor.resume_all_effects()
        except Exception:
            pass
        time.sleep(0.5)
        e.start()
        time.sleep(0.15)
        return True

    def set(self, magnitude):
        if self.effect is None:
            return
        try:
            self.effect.set_parameters(Vector3(magnitude, 0.0, 0.0), timedelta(seconds=3600))
        except Exception as exc:
            print("  set_parameters raised: %s" % exc)

    def unload(self):
        if self.effect is None:
            return
        e, self.effect = self.effect, None
        try:
            e.set_parameters(Vector3(0.0, 0.0, 0.0), timedelta(seconds=1))
            e.stop()
        except Exception:
            pass
        try:
            ok = self.loop.run_until_complete(self.motor.try_unload_effect_async(e))
            print("  unload -> %s" % ok)
        except Exception as exc:
            print("  unload raised: %s" % exc)


def watch(eff, wheel, pump, force, seconds):
    """Command a force and report how far the wheel moved."""
    eff.set(0.0)
    time.sleep(0.35)
    pump.ensure_foreground()
    start = pos(wheel)
    if start is None:
        return 0.0
    eff.set(force)
    moved, deadline = 0.0, time.monotonic() + seconds
    while time.monotonic() < deadline:
        pump.ensure_foreground()
        now = pos(wheel)
        if now is not None:
            moved = max(moved, abs(now - start))
        time.sleep(0.01)
    eff.set(0.0)
    time.sleep(0.3)
    return moved


def away_from_stop(wheel):
    p = pos(wheel)
    return -1.0 if (p is not None and p > 0) else 1.0


def probe(eff, wheel, pump, label):
    """Does the motor drive at all? Always pushes away from the nearest stop."""
    d = away_from_stop(wheel)
    moved = watch(eff, wheel, pump, d * PROBE_FORCE, PROBE_SECONDS)
    alive = moved > MOVED
    print("  %-22s force %+0.2f  moved %.3f  -> %s"
          % (label, d * PROBE_FORCE, moved, "DRIVES" if alive else "SILENT"))
    return alive


def kill_low(eff, wheel, pump, tries=12):
    """
    Silence the motor the way it was actually seen to go silent: LOW force that achieves nothing.

    Creeps up from LOW_START, dwelling at each level, and BACKS OFF two steps as soon as the
    wheel actually moves. The point is to sit just under the level that would move it, so the
    motor is commanded continuously and produces no motion -- the condition `stiction_test`
    was in every time the firmware gave up.

    Force is never dropped to zero between levels: the effect stays loaded and running and only
    its magnitude changes, which is what the bridge and stiction_test both do. Zeroing between
    attempts would hand the firmware an idle moment it does not normally get.
    """
    level = LOW_START
    for n in range(1, tries + 1):
        d = away_from_stop(wheel)
        before = pos(wheel)
        used = level
        eff.set(d * used)
        peak, deadline = 0.0, time.monotonic() + LOW_DWELL
        while time.monotonic() < deadline:
            pump.ensure_foreground()
            now = pos(wheel)
            if now is not None and before is not None:
                peak = max(peak, abs(now - before))
            time.sleep(0.01)

        if peak > CREEP:
            level = max(LOW_START, used - 2 * LOW_STEP)
            note = "moved %.3f -> back off to %.3f" % (peak, level)
        else:
            level = min(LOW_CEILING, used + LOW_STEP)
            note = "no movement -> up to %.3f" % level
        print("  low-force cycle %2d  level %+0.3f  %s" % (n, d * used, note))

        if n % PROBE_EVERY == 0:
            if not probe(eff, wheel, pump, "  silent yet?"):
                if not probe(eff, wheel, pump, "  confirm silent"):
                    return True
    return False


def kill_stop(eff, wheel, pump, tries=6):
    """
    The ORIGINAL kill, kept for comparison. It does not reproduce the failure on this wheel.

    Drives to an end stop and leans on it. The firmware tolerates that indefinitely, which is
    itself worth knowing: it means "commanded force with no resulting motion" is NOT sufficient
    to make the motor give up, or this would have worked too.
    """
    for n in range(1, tries + 1):
        direction = 1.0
        # Drive to the right stop, then lean on it gently.
        eff.set(direction * TRAVEL_FORCE)
        for _ in range(30):
            pump.ensure_foreground()
            time.sleep(0.1)
            if abs(pos(wheel) or 0.0) >= LOCK:
                break
        eff.set(direction * STALL_FORCE)
        t = time.monotonic() + STALL_SECONDS
        while time.monotonic() < t:
            pump.ensure_foreground()
            time.sleep(0.02)
        eff.set(0.0)
        time.sleep(0.5)

        if not probe(eff, wheel, pump, "kill attempt %d" % n):
            if not probe(eff, wheel, pump, "  confirm silent"):
                return True
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--variant", choices=("preload", "zero", "wait"), required=True)
    p.add_argument("--kill", choices=("low", "stop"), default="low",
                   help="how to silence the motor first (default: low)")
    p.add_argument("--wait", type=float, default=20.0)
    args = p.parse_args()

    if init_apartment is not None:
        try:
            init_apartment()
        except TypeError:
            init_apartment(0)

    pump = PumpThread()
    pump.start()
    pump.ready.wait(5.0)
    loop = None
    eff = None
    try:
        raw, wheels = wait_for_devices(args.wait, pump)
        if not has_motor(raw, wheels):
            print("no motor")
            return 1
        motor, _l, wheel, _i = report(raw, wheels)
        loop = asyncio.new_event_loop()

        rule("variant: %s" % args.variant)
        eff = Effect(motor, loop)
        for name, call in (("reset", motor.try_reset_async), ("enable", motor.try_enable_async)):
            try:
                print("  %s -> %s" % (name, loop.run_until_complete(call())))
            except Exception as exc:
                print("  %s raised %s" % (name, exc))
        if not eff.load(0.0):
            return 1

        if not probe(eff, wheel, pump, "baseline"):
            print("\n  RESULT: inconclusive -- the motor was not driving before the test.")
            return 1

        print("\n  killing the motor with %s force ..."
              % ("low, just under breakaway" if args.kill == "low" else "an end stop"))
        killer = kill_low if args.kill == "low" else kill_stop
        if not killer(eff, wheel, pump):
            print("\n  RESULT: inconclusive -- could not silence the motor this run.")
            return 1
        print("  motor is silent.\n")

        if args.variant == "wait":
            time.sleep(3.0)
            revived = probe(eff, wheel, pump, "after waiting")
        else:
            eff.unload()
            time.sleep(1.0)
            d = away_from_stop(wheel)
            if args.variant == "preload":
                ok = eff.load(d * PROBE_FORCE)
                print("  loaded a fresh effect ALREADY CARRYING %+.2f -> %s" % (d * PROBE_FORCE, ok))
                if not ok:
                    return 1
                # It is already commanding force; just watch.
                start = pos(wheel)
                moved, deadline = 0.0, time.monotonic() + PROBE_SECONDS
                while time.monotonic() < deadline:
                    pump.ensure_foreground()
                    now = pos(wheel)
                    if now is not None and start is not None:
                        moved = max(moved, abs(now - start))
                    time.sleep(0.01)
                eff.set(0.0)
                print("  %-22s moved %.3f  -> %s"
                      % ("pre-charged load", moved, "DRIVES" if moved > MOVED else "SILENT"))
                revived = moved > MOVED
            else:
                ok = eff.load(0.0)
                print("  loaded a fresh effect at ZERO -> %s" % ok)
                if not ok:
                    return 1
                revived = probe(eff, wheel, pump, "zero load, then 0.30")

        rule("RESULT")
        print("  variant %-8s -> %s" % (args.variant, "REVIVED" if revived else "still silent"))
        return 0
    finally:
        if eff is not None:
            eff.set(0.0)
            eff.unload()
        if loop is not None:
            try:
                loop.run_until_complete(motor.try_reset_async())
            except Exception:
                pass
            loop.close()
        pump.stop()


if __name__ == "__main__":
    sys.exit(main())
