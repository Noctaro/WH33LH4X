r"""
centring_test.py -- does holding an effect really suspend the wheel's own auto-centring?

THE CLAIM UNDER TEST (README.md:96, relied on by wheel_profile.free_wheel):
    holding the motor makes the wheel go limp, i.e. the firmware stops pulling it to centre.

It has never been measured, and it is now LOAD-BEARING FOR A FEATURE: the GUI's spring slider
is justified by "the bridge takes the wheel's own centring away, so we put one back".

    block A   effect loaded at magnitude 0 and started   <- what the bridge does all game
    block B   effect unloaded, motor reset               <- the wheel on its own

Each trial: you push the wheel a quarter turn off centre and let go; the script logs where it
goes for three seconds. If it walks back to centre in B but stays put in A, the claim holds.

RESULT, 2026-08-26, Hori Force Feedback Racing Wheel DLX: NEITHER BLOCK CENTRED.

    baseline: motor moved 0.047 -> DRIVES     so block A is not a silent-motor artefact
    HELD  +0.695 -> +0.695  recovered 0.0%
    HELD  -0.427 -> -0.483  recovered 0.0%    drifted FURTHER from centre
    FREE  +0.364 -> +0.364  recovered 0.0%
    FREE  -0.424 -> -0.424  recovered 0.0%
    FREE  +0.683 -> +0.682  recovered 0.2%    reader live, so "no change" is a still wheel

READ BLOCK B CAREFULLY -- THIS SCRIPT'S FIRST VERDICT WAS WRONG. It concluded "this wheel does
not self-centre at all". It does: with NOTHING running, the wheel springs back to centre hard
and precisely. So block B never restored firmware control -- unloading the effect and resetting
the motor does not release the device, which stays claimed for the life of the process.

The claim under test is therefore CONFIRMED (our process suspends the factory spring), and the
sentence that was actually false was the one about giving it back. Fixed in README.md and in
wheel_profile.free_wheel.

WHY BOTH BLOCKS LOOK THE SAME: the pump window holds the FOREGROUND for the whole run, and
the foreground is what holds the motor. Unloading the effect was never the variable that
mattered. Observed independently during play: alt-tab out of a running game and the
firmware spring returns, alt-tab back and force resumes. Same gate as enumeration,
position reads and force output, all of which WGI ties to the foreground.

So this script cannot separate 'suspended by holding an effect' from 'suspended by being
in front with the motor open', and it never could. To test that properly, drop the
foreground rather than the effect.

The ONLY honest control for "does this wheel centre on its own" is to run no software at all
and push it by hand.

NO CONSOLE INPUT. Windows.Gaming.Input only reads position for a foregrounded process, so
clicking the console to answer a prompt would break the very measurement being taken. All
instructions go to the probe window's TITLE BAR, with beeps to mark push / let-go / done.

Run from the repo root with PYTHONPATH set to it. One process, both blocks.
"""

import argparse
import asyncio
import ctypes
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

try:
    import winsound
except ImportError:
    winsound = None

user32 = ctypes.windll.user32

MIN_PUSH = 0.15      # a quarter turn on a 900 deg wheel is about 0.20 of full range
WATCH_SECONDS = 3.0
PUSH_SECONDS = 6.0
RETURNED = 0.30      # recovered >= 30% of the offset counts as "walked back to centre"
PROBE_FORCE = 0.30
PROBE_SECONDS = 1.5
MOVED = 0.01


def pos(wheel):
    try:
        return wheel.get_current_reading().wheel
    except Exception:
        return None


def say(pump, text):
    """Instructions go in the title bar -- it is the only text visible while we hold focus."""
    if pump.hwnd:
        user32.SetWindowTextW(pump.hwnd, text)


def beep(freq, ms):
    if winsound is not None:
        try:
            winsound.Beep(freq, ms)
        except Exception:
            pass


def trial(pump, wheel, label):
    """One push-and-release. Returns (start, end, closest_to_centre) or None if not pushed."""
    for remaining in range(int(PUSH_SECONDS), 0, -1):
        pump.ensure_foreground()
        p = pos(wheel) or 0.0
        say(pump, "%s  --  PUSH the wheel a quarter turn and HOLD  (%ds)   pos %+0.3f"
            % (label, remaining, p))
        if remaining == int(PUSH_SECONDS):
            beep(880, 120)
        time.sleep(1.0)

    start = pos(wheel)
    if start is None or abs(start) < MIN_PUSH:
        say(pump, "%s  --  not pushed far enough, skipping" % label)
        beep(300, 300)
        time.sleep(1.5)
        return None

    say(pump, "%s  --  LET GO NOW, hands off  (watching %.0fs)" % (label, WATCH_SECONDS))
    beep(440, 200)

    closest = abs(start)
    end = start
    deadline = time.monotonic() + WATCH_SECONDS
    while time.monotonic() < deadline:
        pump.ensure_foreground()
        now = pos(wheel)
        if now is not None:
            end = now
            closest = min(closest, abs(now))
        time.sleep(0.01)
    beep(1200, 80)
    return start, end, closest


def run_block(pump, wheel, label, trials):
    rows = []
    for n in range(1, trials + 1):
        r = trial(pump, wheel, "%s trial %d/%d" % (label, n, trials))
        if r is None:
            print("  %-8s trial %d  SKIPPED (push was too small)" % (label, n))
            continue
        start, end, closest = r
        recovered = (abs(start) - closest) / abs(start)
        rows.append(recovered)
        print("  %-8s trial %d  start %+0.3f  end %+0.3f  closest %0.3f  recovered %5.1f%%  -> %s"
              % (label, n, start, end, closest, recovered * 100.0,
                 "RETURNED" if recovered >= RETURNED else "stayed put"))
        time.sleep(0.8)
    return rows


def verdict(rows):
    if not rows:
        return None
    return sum(1 for r in rows if r >= RETURNED) > len(rows) / 2.0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--trials", type=int, default=3, help="pushes per block (default 3)")
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
    loop, effect, motor = None, None, None
    try:
        raw, wheels = wait_for_devices(args.wait, pump)
        if not has_motor(raw, wheels):
            print("no motor -- click the 'FFB probe' window and run again")
            return 1
        motor, _l, wheel, _i = report(raw, wheels)
        loop = asyncio.new_event_loop()

        for name, call in (("reset", motor.try_reset_async), ("enable", motor.try_enable_async)):
            try:
                print("  %s -> %s" % (name, loop.run_until_complete(call())))
            except Exception as exc:
                print("  %s raised %s" % (name, exc))

        # ---- block A: effect loaded at zero and started, exactly like the bridge --------
        rule("block A -- effect HELD at magnitude 0")
        effect = ff.ConstantForceEffect()
        effect.set_parameters(Vector3(0.0, 0.0, 0.0), timedelta(seconds=3600))
        result = loop.run_until_complete(motor.load_effect_async(effect))
        print("  load -> %s" % result)
        if result != ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
            return 1
        try:
            if motor.are_effects_paused:
                motor.resume_all_effects()
        except Exception:
            pass
        time.sleep(0.5)
        effect.start()
        time.sleep(0.15)
        print("  state -> %s" % effect.state)

        # Does the motor actually drive? If not, "the wheel did not move" proves much less.
        start = pos(wheel) or 0.0
        direction = -1.0 if start > 0 else 1.0
        effect.set_parameters(Vector3(direction * PROBE_FORCE, 0.0, 0.0), timedelta(seconds=60))
        moved, deadline = 0.0, time.monotonic() + PROBE_SECONDS
        while time.monotonic() < deadline:
            pump.ensure_foreground()
            now = pos(wheel)
            if now is not None:
                moved = max(moved, abs(now - start))
            time.sleep(0.01)
        effect.set_parameters(Vector3(0.0, 0.0, 0.0), timedelta(seconds=3600))
        drives = moved > MOVED
        print("  baseline: motor moved %.3f -> %s" % (moved, "DRIVES" if drives else "SILENT"))
        if not drives:
            print("  NOTE: the motor is silent, so block A only shows that a silent motor")
            print("        does not centre the wheel. Treat A as weak evidence.")
        time.sleep(1.0)

        a = run_block(pump, wheel, "HELD", args.trials)

        # ---- block B: nothing held, the wheel on its own -------------------------------
        rule("block B -- effect UNLOADED, wheel on its own")
        try:
            effect.set_parameters(Vector3(0.0, 0.0, 0.0), timedelta(seconds=1))
            effect.stop()
        except Exception:
            pass
        try:
            print("  unload -> %s" % loop.run_until_complete(motor.try_unload_effect_async(effect)))
        except Exception as exc:
            print("  unload raised %s" % exc)
        effect = None
        try:
            print("  reset  -> %s" % loop.run_until_complete(motor.try_reset_async()))
        except Exception as exc:
            print("  reset raised %s" % exc)
        time.sleep(1.5)

        b = run_block(pump, wheel, "FREE", args.trials)

        # ---- verdict -------------------------------------------------------------------
        say(pump, "done -- read the console")
        rule("RESULT")
        va, vb = verdict(a), verdict(b)
        print("  A (effect held)     %s" % ("no valid trials" if va is None else
              ("RETURNS to centre" if va else "STAYS where you left it")))
        print("  B (nothing held)    %s" % ("no valid trials" if vb is None else
              ("RETURNS to centre" if vb else "STAYS where you left it")))
        print("")
        print("  REMINDER: block B unloads the effect but does NOT release the device --")
        print("  measured 2026-08-26. Compare against pushing the wheel with NO software")
        print("  running; that is the only real control for 'does this wheel centre'.")
        print("")
        if va is None or vb is None:
            print("  INCONCLUSIVE -- not enough valid pushes. Push a full quarter turn.")
        elif not va and not vb:
            print("  EXPECTED. Neither block centred, which is what a device claimed for the")
            print("  whole process looks like. If the wheel centres with nothing running,")
            print("  this CONFIRMS that our process suspends the factory spring.")
        elif not va and vb:
            print("  NEW: unloading DID hand centring back this time. That contradicts the")
            print("  2026-08-26 measurement -- re-run before believing it.")
        elif va and vb:
            print("  CLAIM WRONG. Firmware centring is live even while we hold the motor, so")
            print("  a software spring STACKS on top of it -- cap the slider, and revisit the")
            print("  oscillation note in GAMES.md.")
        else:
            print("  ODD: returns while held, stays when free. Suspect the motor was driving")
            print("  during A. Re-run and watch the baseline line.")
        return 0
    finally:
        say(pump, "FFB probe (leave me open)")
        if effect is not None and loop is not None:
            try:
                effect.set_parameters(Vector3(0.0, 0.0, 0.0), timedelta(seconds=1))
                effect.stop()
                loop.run_until_complete(motor.try_unload_effect_async(effect))
            except Exception:
                pass
        if loop is not None and motor is not None:
            try:
                loop.run_until_complete(motor.try_reset_async())
            except Exception:
                pass
        pump.stop()


if __name__ == "__main__":
    sys.exit(main())
