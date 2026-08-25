r"""
stiction_test.py -- how much force does it take to move this wheel at all?

WHY THIS EXISTS
---------------
DiRT 4's centring force is proportional to steering angle, so it is SMALLEST NEAR CENTRE --
exactly where it has to overcome the wheel's own friction to bring it back. If the motor
cannot break stiction at those small forces, the wheel simply holds wherever you left it, and
no amount of tuning the game's signal changes that. "It does not really centre, it holds the
position" is what that looks like from the driver's seat.

That is a property of the hardware, not of the game, so it is measurable without one: ramp
commanded force up from zero until the wheel actually moves, and read off the threshold.

WHAT IT MEASURES
----------------
  * BREAKAWAY force -- the level at which a stationary wheel starts to move.
  * Both directions, since a belt or gear train is rarely symmetric.
  * Repeated, because stiction scatters and one sample is an anecdote.

WHAT IT DOES NOT NEED
---------------------
No game, no vJoy, no shim. It drives the motor directly through Windows.Gaming.Input, which
means THIS WINDOW MUST STAY IN FRONT -- WGI gates force output on foreground, which is the
gate this whole project exists to work around. Nothing here is subtle about it: the test
grabs the foreground itself and tells you not to click away.

SAFETY
------
Force is ramped gently from zero, capped by --max, and released on every exit path including
Ctrl+C. Let the wheel go while this runs; holding it defeats the measurement.

Usage:
    .\.venv\Scripts\python.exe stiction_test.py
    .\.venv\Scripts\python.exe stiction_test.py --max 0.5 --passes 5
"""

import argparse
import asyncio
import sys
import time

try:
    from winrt.runtime import init_apartment
except ImportError:
    init_apartment = None

import probe_log as log
from motor_sink import WgiMotorSink
from wgi_probe import PumpThread, has_motor, report, rule, wait_for_devices

# How far the wheel must move before we call it "moving". Position is -1..+1 across the whole
# travel, and the reading is quantised, so this has to clear the noise floor without being so
# large that a genuine slow creep is missed. 0.01 is roughly 5 degrees on a 900 degree wheel.
MOVED = 0.01

# Seconds to hold each force level before deciding it did nothing. Stiction releases are not
# instant: the motor has to wind up against the belt before anything turns.
SETTLE = 0.35


def read_position(wheel):
    try:
        reading = wheel.get_current_reading()
    except Exception:
        return None
    return reading.wheel


def ramp_once(sink, wheel, direction, maximum, step, pump):
    """
    Raise force one step at a time until the wheel moves. Returns (breakaway, travel) or None.

    Returns the force level at which motion was first seen, not the level that was commanded
    when motion finished -- those differ by a step and the first one is the useful number.
    """
    sink.set_force(0.0)
    time.sleep(0.6)                       # let it settle before taking the reference
    start = read_position(wheel)
    if start is None:
        return None

    level = 0.0
    while level < maximum:
        level = round(level + step, 4)
        sink.set_force(direction * level)
        # Keep the foreground for the whole ramp: WGI silently produces no torque without it,
        # and a silent zero would read as "the wheel never moved", i.e. infinite stiction.
        pump.ensure_foreground()
        deadline = time.monotonic() + SETTLE
        while time.monotonic() < deadline:
            now = read_position(wheel)
            if now is not None and abs(now - start) > MOVED:
                sink.set_force(0.0)
                return (level, abs(now - start))
            time.sleep(0.01)
    sink.set_force(0.0)
    return None


# Force used to prove the motor is actually driving before any measurement is trusted. Well
# above the breakaway seen in wgi_probe's magnitude sweep (the wheel moved at 0.10, its lowest
# step), so "this did nothing" means the effect is not driving rather than that the wheel is
# merely stiff.
ENGAGE_FORCE = 0.30
ENGAGE_SECONDS = 1.2


def moved_within(sink, wheel, force, seconds, pump):
    """Command a force briefly and report whether the wheel actually turned."""
    sink.set_force(0.0)
    time.sleep(0.5)
    start = read_position(wheel)
    if start is None:
        return None
    sink.set_force(force)
    pump.ensure_foreground()
    deadline = time.monotonic() + seconds
    seen = 0.0
    while time.monotonic() < deadline:
        now = read_position(wheel)
        if now is not None:
            seen = max(seen, abs(now - start))
            if seen > MOVED:
                break
        time.sleep(0.01)
    sink.set_force(0.0)
    time.sleep(0.3)
    return seen


def check_engagement(motor, loop, wheel, pump, gain, maximum):
    """
    Prove the motor is producing torque before measuring anything, and if it is not, work out
    WHY rather than reporting infinite stiction.

    THE FAILURE THIS EXISTS FOR: on 2026-08-25 this script reported "never moved up to 0.60"
    three times running, with foreground held, reset and enable both returning True, 311
    successful writes and zero failures -- while wgi_probe moved the same wheel at 0.10 a
    minute later. A dead effect and a stiff wheel printed identically, which made a confident
    RESULT block out of a measurement that never happened.

    Two differences between the two paths could explain it, and this tells them apart:
      A. this sink LOADS the effect at magnitude 0 and rewrites afterwards;
         wgi_probe loads a fresh effect already carrying the magnitude it wants.
      B. rewriting magnitude on a held effect may simply not drive outside the game process.

    Returns (sink, note) with a sink that is known to produce torque, or (None, reason).
    """
    held = WgiMotorSink(motor, loop, max_force=maximum, gain=gain)
    held.open()
    seen = moved_within(held, wheel, ENGAGE_FORCE, ENGAGE_SECONDS, pump)
    log.event("engage.held", force=ENGAGE_FORCE, moved=round(seen or -1.0, 4))
    if seen is not None and seen > MOVED:
        return held, "held effect drives normally"

    print("  the held effect commanded %.2f and the wheel did not move." % ENGAGE_FORCE)
    print("  retrying with the magnitude set BEFORE the effect is loaded...")
    held.close()

    preloaded = WgiMotorSink(motor, loop, max_force=maximum, gain=gain,
                             initial_force=ENGAGE_FORCE)
    preloaded.open()
    pump.ensure_foreground()
    start = read_position(wheel)
    seen2 = 0.0
    deadline = time.monotonic() + ENGAGE_SECONDS
    while time.monotonic() < deadline and start is not None:
        now = read_position(wheel)
        if now is not None:
            seen2 = max(seen2, abs(now - start))
            if seen2 > MOVED:
                break
        time.sleep(0.01)
    preloaded.set_force(0.0)
    log.event("engage.preloaded", force=ENGAGE_FORCE, moved=round(seen2, 4))

    if seen2 > MOVED:
        print("  IT MOVED. So the magnitude must be present when the effect is LOADED --")
        print("  rewriting it afterwards on an effect that started at zero drives nothing.")
        return preloaded, "effect must be loaded carrying its magnitude"

    preloaded.close()
    return None, ("the motor accepted %.2f two different ways and never turned the wheel"
                  % ENGAGE_FORCE)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max", type=float, default=0.60,
                   help="highest force to try, 0.0-1.0 (default 0.60)")
    p.add_argument("--step", type=float, default=0.02,
                   help="force increment per attempt (default 0.02)")
    p.add_argument("--passes", type=int, default=3,
                   help="measurements per direction (default 3); stiction scatters")
    p.add_argument("--gain", type=float, default=1.0,
                   help="motor master gain (default 1.0 -- measure the hardware, not a gain)")
    p.add_argument("--wait", type=float, default=20.0, help="device wait timeout")
    p.add_argument("--no-log", action="store_true")
    args = p.parse_args()

    if not args.no_log:
        log.start(prefix="stiction_test")
    if init_apartment is not None:
        # winrt-runtime 3.x requires the apartment type; 2.x took no argument. Every other
        # WGI entry point in this repo carries this fallback -- this one was missed.
        try:
            init_apartment()
        except TypeError:
            init_apartment(0)

    pump = PumpThread()
    pump.start()
    pump.ready.wait(5.0)

    sink = None
    loop = None
    try:
        rule("stiction_test -- the force it takes to move this wheel")
        print("  Ramps force up from zero until the wheel turns, both ways.")
        print("  LET GO OF THE WHEEL and keep this window in front.")
        print()

        raw, wheels = wait_for_devices(args.wait, pump)
        if not has_motor(raw, wheels):
            rule("RESULT")
            print("  No wheel with a motor found. Put it in Xbox mode and press a button.")
            return 1
        motor, _label, wheel, _identity = report(raw, wheels)
        if motor is None or wheel is None:
            print("  Found a device but not both a motor and a readable wheel.")
            return 1

        loop = asyncio.new_event_loop()

        # Never measure through a motor that is not driving. A silent effect and an
        # immovable wheel are indistinguishable from here, and reporting the second when it
        # was the first is how this script produced three confident wrong answers.
        rule("Checking the motor actually drives")
        sink, note = check_engagement(motor, loop, wheel, pump, args.gain, args.max)
        if sink is None:
            rule("RESULT")
            print("  NO MEASUREMENT. %s." % note)
            print()
            print("  This is NOT a stiction reading -- the motor never produced torque, so")
            print("  there is nothing to measure. Check that wgi_probe.py can move the wheel")
            print("  (press 1 for constant force). If it can and this cannot, the difference")
            print("  is in this script, not the hardware. If it cannot either, power-cycle")
            print("  the wheel; some motor states never recover by waiting.")
            log.event("engage.failed", reason=note)
            return 1
        print("  OK -- %s." % note)
        log.event("engage.ok", note=note)

        rule("Measuring")
        results = {1: [], -1: []}
        died = False
        for direction in (1, -1):
            if died:
                break
            name = "right" if direction > 0 else "left"
            for attempt in range(args.passes):
                got = ramp_once(sink, wheel, direction, args.max, args.step, pump)
                if got is None:
                    # A pass that saw nothing is ambiguous in exactly the way the check before
                    # the run exists to resolve -- except the motor can also die PART WAY
                    # THROUGH, which it did on 2026-08-25 after two good passes. So re-prove
                    # the motor here rather than trusting a check made minutes ago. Without
                    # this the remaining passes report "never moved" and the summary presents
                    # a dead motor as infinite stiction.
                    again = moved_within(sink, wheel, ENGAGE_FORCE, ENGAGE_SECONDS, pump)
                    if again is None or again <= MOVED:
                        print("  %-5s pass %d: MOTOR STOPPED DRIVING -- abandoning the run"
                              % (name, attempt + 1))
                        log.event("stiction.motor_died", direction=name,
                                  after_pass=attempt + 1)
                        died = True
                        break
                    print("  %-5s pass %d: NEVER MOVED up to %.2f" % (name, attempt + 1,
                                                                      args.max))
                    log.event("stiction.pass", direction=name, breakaway=-1.0)
                else:
                    level, travel = got
                    results[direction].append(level)
                    print("  %-5s pass %d: broke away at %.2f  (moved %.3f)"
                          % (name, attempt + 1, level, travel))
                    log.event("stiction.pass", direction=name, breakaway=level,
                              travel=round(travel, 4))
                time.sleep(0.4)

        rule("RESULT")
        if died:
            print("  RUN ABANDONED -- the motor stopped producing torque part way through.")
            print("  Numbers below cover only the passes before that, and any direction")
            print("  reported as 'never moved' after it is NOT a stiction measurement.")
            print("  Power-cycle the wheel and run again for a figure worth trusting.")
            print()
        for direction in (1, -1):
            name = "right" if direction > 0 else "left"
            got = results[direction]
            if got:
                print("  %-5s breakaway: %.3f  (min %.2f, max %.2f, n=%d)"
                      % (name, sum(got) / len(got), min(got), max(got), len(got)))
            else:
                print("  %-5s breakaway: never moved at or below %.2f" % (name, args.max))

        both = results[1] + results[-1]
        if both:
            mean = sum(both) / len(both)
            print()
            print("  WHAT THIS MEANS")
            print("  Any commanded force below about %.2f moves this wheel not at all." % mean)
            print("  A centring force is proportional to steering angle, so it is weakest")
            print("  near centre -- if the game's force there is under %.2f, the wheel will"
                  % mean)
            print("  hold position instead of returning, however the signal is tuned.")
            print()
            print("  min_force in tune.json exists for exactly this: it lifts small non-zero")
            print("  forces up to something the motor can express. %.2f is the number this"
                  % mean)
            print("  measurement suggests, and it is hardware compensation, not an effect.")
            log.event("stiction.result", mean=round(mean, 4),
                      right=round(sum(results[1]) / len(results[1]), 4) if results[1] else -1,
                      left=round(sum(results[-1]) / len(results[-1]), 4) if results[-1] else -1)
        return 0

    except KeyboardInterrupt:
        print("\n  stopped.")
        return 0
    finally:
        if sink is not None:
            try:
                sink.set_force(0.0)
            except Exception:
                pass
            sink.close()
        if loop is not None:
            loop.close()
        pump.stop()
        if not args.no_log:
            log.stop()


if __name__ == "__main__":
    sys.exit(main())
