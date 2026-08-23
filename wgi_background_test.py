"""
wgi_background_test.py -- Does Windows.Gaming.Input still work when we are NOT in front?

This decides whether the vJoy bridge (Track B) can exist at all, so it is worth answering
before any bridge code is written.

The bridge is a background process. During play the GAME owns the foreground, and the
bridge must, from behind it:

    1. READ the real wheel's position, to feed the virtual device's axes
    2. WRITE force to the real wheel's motor, to render the game's effects

Both go through Windows.Gaming.Input. Our own notes record that WGI gates BOTH on
foreground -- `get_current_reading().wheel` returning 0.0 forever when the process is not in
front, and torque dying silently while the effect still reports Running. That was measured
against a console tool that could simply grab focus back. A bridge cannot: whatever it does,
the game is in front.

So there are only two outcomes, and they are very different:

  * Readings and force survive backgrounding -> the planned architecture works. Continue.
  * They do not -> no separate process can ever drive this wheel during gameplay, and the
    bridge has to move INSIDE the game's process (the dinput8-proxy escape hatch), which is
    a different project with a compiler requirement.

This tool deliberately does NOT fight for foreground -- unlike wgi_probe.py, which re-grabs
it constantly. Giving focus away is the entire experiment.

Safety: force is capped at gain x magnitude and stopped in a finally block, same as the
probe. Nothing here can run away.

Usage:
    python wgi_background_test.py
    python wgi_background_test.py --no-force    # test readings only, motor untouched
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

import probe_log as log
from wgi_probe import (
    PumpThread,
    describe_motor,
    has_motor,
    report,
    rule,
    user32,
    wait_for_devices,
)

# A reading is "alive" if it moves at all across a phase. Absolute values do not matter --
# the wheel may be parked anywhere -- so the test is variation, not magnitude. At rest the
# reading dithers by a bit or two, hence a threshold rather than != 0.
ALIVE_SPAN = 0.02


class Phase(object):
    """One timed window of samples, plus whether we held foreground during it."""

    def __init__(self, name, seconds):
        self.name = name
        self.seconds = seconds
        self.samples = []
        self.foreground_ticks = 0
        self.total_ticks = 0
        # Fraction of samples sitting at exactly 0.000. This is the discriminator the
        # original two-phase test lacked, and the one that would have caught its wrong
        # answer: a gated reading is not noisy, it is exactly zero.
        self.zero_fraction = 0.0

    @property
    def span(self):
        if not self.samples:
            return 0.0
        return max(self.samples) - min(self.samples)

    @property
    def alive(self):
        return self.span >= ALIVE_SPAN

    @property
    def all_zero(self):
        return bool(self.samples) and all(abs(s) < 1e-6 for s in self.samples)

    @property
    def foreground_fraction(self):
        if not self.total_ticks:
            return 0.0
        return self.foreground_ticks / self.total_ticks

    def summary(self):
        if not self.samples:
            return "no samples at all"
        return ("%d samples, range %+.3f .. %+.3f (span %.3f), foreground %d%% of ticks"
                % (len(self.samples), min(self.samples), max(self.samples), self.span,
                   round(100 * self.foreground_fraction)))


def sample_phase(wheel, hwnd, phase, prompt_lines):
    """Sample the wheel for phase.seconds, printing a live line. Never grabs foreground."""
    print()
    rule()
    for line in prompt_lines:
        print("  %s" % line)
    print()

    end = time.monotonic() + phase.seconds
    next_print = 0.0
    while time.monotonic() < end:
        try:
            value = wheel.get_current_reading().wheel
        except Exception:
            value = None

        fg = user32.GetForegroundWindow() == hwnd
        phase.total_ticks += 1
        if fg:
            phase.foreground_ticks += 1
        if value is not None:
            phase.samples.append(value)

        now = time.monotonic()
        if now >= next_print:
            remaining = int(end - now) + 1
            bar = "?" if value is None else _bar(value)
            print("    %2ds  reading %s  %s   %s"
                  % (remaining,
                     "  n/a " if value is None else "%+.3f" % value,
                     bar,
                     "[foreground: US]" if fg else "[foreground: ANOTHER WINDOW]"),
                  flush=True)
            next_print = now + 0.5

        log.event("bg.sample", phase=phase.name, reading=value if value is not None else -9.0,
                  foreground=fg)
        time.sleep(0.02)


def _bar(value):
    """A 21-cell position bar, centre at 10. Makes a frozen reading obvious at a glance."""
    cells = ["-"] * 21
    index = int(round((max(-1.0, min(1.0, value)) + 1.0) * 10))
    cells[max(0, min(20, index))] = "#"
    return "[" + "".join(cells) + "]"


def verdict(foreground_phase, background_phase, force_answer):
    rule("VERDICT -- can a background process drive this wheel?")

    print("  foreground phase : %s" % foreground_phase.summary())
    print("  background phase : %s" % background_phase.summary())
    print()

    if background_phase.foreground_fraction > 0.25:
        print("  >>> INCONCLUSIVE. We still held the foreground for %d%% of the background"
              % round(100 * background_phase.foreground_fraction))
        print("      phase, so nothing was actually tested. Re-run and click a DIFFERENT")
        print("      window -- a text editor, a browser -- and leave it focused.")
        log.event("bg.verdict", result="inconclusive")
        return None

    if not foreground_phase.alive:
        print("  >>> INCONCLUSIVE. The reading did not move even while we were in front,")
        print("      so there is nothing to compare against. Re-run and turn the wheel")
        print("      through a good part of its range during the FIRST phase.")
        log.event("bg.verdict", result="no-baseline")
        return None

    readings_ok = background_phase.alive

    if readings_ok:
        print("  READINGS: ALIVE in the background. The wheel position tracked while")
        print("            another window had focus.")
    elif background_phase.all_zero:
        print("  READINGS: DEAD in the background -- every sample was exactly 0.000.")
        print("            This is the documented WGI foreground gate.")
    else:
        print("  READINGS: FROZEN in the background -- samples never moved (span %.3f)."
              % background_phase.span)
        print("            The last value is being repeated, not re-read.")

    if force_answer is not None:
        print("  FORCE   : %s in the background, per your answer."
              % ("ALIVE" if force_answer else "DEAD"))

    print()
    both_ok = readings_ok and (force_answer is not False)
    if both_ok:
        print("  >>> The bridge architecture WORKS. A separate process can read the wheel")
        print("      and drive the motor while a game is in front. Proceed with B1.")
        result = "works"
    elif readings_ok and force_answer is False:
        print("  >>> PARTIAL. Readings survive but force does not, so the bridge could feed")
        print("      a game's INPUT but never render its force feedback -- which is the")
        print("      entire point. The output stage has to move inside the game's process.")
        result = "input-only"
    else:
        print("  >>> BLOCKED. A background process cannot read this wheel, so it cannot")
        print("      feed a virtual device either. The bridge cannot be a separate process.")
        print()
        print("      This does not kill the goal, it relocates it: the dinput8-proxy route")
        print("      puts the same code INSIDE the game, where it is foreground by")
        print("      definition. That needs a compiled DLL -- see the plan's escape hatch.")
        result = "blocked"

    log.event("bg.verdict", result=result, fg_span=round(foreground_phase.span, 3),
              bg_span=round(background_phase.span, 3),
              bg_foreground=round(background_phase.foreground_fraction, 3),
              force=force_answer)
    return result


def reading_gate_test(wheel, pump, seconds):
    """
    Is POSITION READING foreground-gated? Asked as A -> B -> A, for a specific reason.

    The two-phase version of this test produced a confidently wrong answer. It compared a
    "foreground" phase against a "background" phase, but focus was never actually held in
    the first one, and the summary statistic used -- how many distinct values appeared --
    counted values gathered during brief focus transitions. It reported readings alive in
    the background while the same data showed 65% of background samples sitting at exactly
    0.000 against 0% while foregrounded.

    So this measures the ONE thing that discriminates, per phase: what fraction of samples
    are exactly zero while the wheel is being turned continuously. And it takes the
    foreground back at the end, because a value returning on cue is far stronger evidence
    than a value merely disappearing.
    """
    rule("READING GATE TEST -- three phases")
    print("  TURN THE WHEEL CONTINUOUSLY for the whole test, through all three phases.")
    print("  A reading that is alive tracks the wheel; a gated one sits at exactly 0.000.")

    phases = []
    for key, title, hold_foreground, instruction in (
        ("A1", "PHASE A -- we hold the foreground", True,
         "Hands off mouse and keyboard. Keep turning the wheel."),
        ("B", "PHASE B -- foreground given away", False,
         ">>> CLICK ANOTHER WINDOW NOW <<< and keep turning the wheel."),
        ("A2", "PHASE A AGAIN -- we take the foreground back", True,
         "Hands off the mouse. Keep turning. Watch for the reading to COME BACK."),
    ):
        phase = Phase(key, seconds)
        print()
        rule()
        print("  %s" % title)
        print("    %s" % instruction)
        print()

        end = time.monotonic() + seconds
        next_print = 0.0
        zeros = 0
        while time.monotonic() < end:
            if hold_foreground:
                pump.ensure_foreground()
            try:
                value = wheel.get_current_reading().wheel
            except Exception:
                value = None
            fg = user32.GetForegroundWindow() == pump.hwnd
            phase.total_ticks += 1
            if fg:
                phase.foreground_ticks += 1
            if value is not None:
                phase.samples.append(value)
                if abs(value) < 1e-6:
                    zeros += 1

            now = time.monotonic()
            if now >= next_print:
                print("    %2ds  reading %s  %s  %s"
                      % (int(end - now) + 1,
                         " n/a " if value is None else "%+.3f" % value,
                         _bar(value if value is not None else 0.0),
                         "[US]" if fg else "[ANOTHER WINDOW]"), flush=True)
                next_print = now + 0.5
            log.event("readgate.sample", phase=key,
                      reading=value if value is not None else -9.0, foreground=fg)
            time.sleep(0.02)

        phase.zero_fraction = zeros / len(phase.samples) if phase.samples else 1.0
        phases.append(phase)
        print("    -> span %.3f, exactly-zero %d%%, foreground %d%%"
              % (phase.span, round(100 * phase.zero_fraction),
                 round(100 * phase.foreground_fraction)))
        log.event("readgate.phase", phase=key, span=round(phase.span, 3),
                  zero_fraction=round(phase.zero_fraction, 3),
                  foreground=round(phase.foreground_fraction, 3))

    a1, b, a2 = phases
    rule("READING GATE VERDICT")
    for p in phases:
        print("  %-3s span %.3f  exactly-zero %3d%%  foreground %3d%%"
              % (p.name, p.span, round(100 * p.zero_fraction),
                 round(100 * p.foreground_fraction)))
    print()

    if a1.foreground_fraction < 0.75 or a2.foreground_fraction < 0.75:
        print("  >>> INCONCLUSIVE -- we failed to hold the foreground in an A phase.")
        outcome = "no-foreground"
    elif b.foreground_fraction > 0.25:
        print("  >>> INCONCLUSIVE -- the foreground was not actually given away in phase B.")
        outcome = "not-backgrounded"
    elif not a1.alive:
        print("  >>> INCONCLUSIVE -- the reading never moved even with focus held.")
        print("      Turn the wheel through a wider range and re-run.")
        outcome = "no-baseline"
    elif not b.alive and a2.alive:
        print("  >>> CONFIRMED: position reading IS foreground-gated. It went dead when")
        print("      focus left and returned when focus came back.")
        print()
        print("      Combined with the force gate, NO background process can read or drive")
        print("      this wheel while a game is in front. The bridge must run INSIDE the")
        print("      game process.")
        outcome = "gated"
    elif b.alive:
        print("  >>> reading SURVIVES the background (span %.3f, only %d%% zeros)."
              % (b.span, round(100 * b.zero_fraction)))
        outcome = "not-gated"
    else:
        print("  >>> INCONCLUSIVE -- the reading did not return in phase A2.")
        outcome = "no-recovery"

    log.event("readgate.verdict", outcome=outcome)
    return outcome


def force_gate_test(motor, pump, loop, seconds, gain, magnitude):
    """
    Is FORCE OUTPUT foreground-gated? Asked as A -> B -> A, not A -> B.

    A one-way test cannot separate "force died because we lost focus" from "force died
    because the effect ended, the motor faulted, or it was never really on". Taking the
    foreground BACK and watching the force return does separate them: nothing else in the
    experiment changes at that moment.

    Foreground is re-asserted every tick during the A phases, because a single
    SetForegroundWindow at the start is exactly how the previous run ended up measuring
    background-vs-background and proving nothing.
    """
    rule("FORCE GATE TEST -- three phases, one question each")
    print("  A steady sideways pull runs for the whole test. All that changes between")
    print("  phases is which window has focus.")
    print()
    print("  Keep a hand on the wheel throughout. You are feeling for the pull, not for")
    print("  the wheel's own centering -- when the motor is handed back to the firmware")
    print("  the wheel CENTERS HARD, which is a different, springy feel.")

    try:
        motor.master_gain = max(0.0, min(1.0, gain))
    except Exception as exc:
        print("  could not set gain: %s" % exc)

    effect = ff.ConstantForceEffect()
    effect.set_parameters(Vector3(max(0.0, min(1.0, magnitude)), 0.0, 0.0),
                          timedelta(seconds=3 * seconds + 15))
    result = loop.run_until_complete(motor.load_effect_async(effect))
    if result != ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
        print("  effect load failed: %s" % result)
        return None, None

    answers = {}
    try:
        pump.ensure_foreground()
        effect.start()

        for key, title, hold_foreground, instructions in (
            ("A1", "PHASE A -- we hold the foreground", True,
             ["Do NOT touch the mouse or keyboard. This window is being kept in front.",
              "Just feel the wheel."]),
            ("B", "PHASE B -- foreground given away", False,
             [">>> CLICK ANOTHER WINDOW NOW <<< and leave it focused.",
              "Keep feeling the wheel."]),
            ("A2", "PHASE A AGAIN -- we take the foreground back", True,
             ["Hands off the mouse again. This window is grabbing focus back by itself.",
              "Feel whether the pull RETURNS."]),
        ):
            print()
            rule()
            print("  %s" % title)
            for line in instructions:
                print("    %s" % line)
            print()

            end = time.monotonic() + seconds
            fg_ticks = total = 0
            next_print = 0.0
            while time.monotonic() < end:
                if hold_foreground:
                    pump.ensure_foreground()
                fg = user32.GetForegroundWindow() == pump.hwnd
                total += 1
                fg_ticks += 1 if fg else 0
                now = time.monotonic()
                if now >= next_print:
                    print("    %2ds   %s" % (int(end - now) + 1,
                                             "[foreground: US]" if fg
                                             else "[foreground: ANOTHER WINDOW]"),
                          flush=True)
                    next_print = now + 1.0
                time.sleep(0.02)

            share = fg_ticks / total if total else 0.0
            state = "ok"
            if hold_foreground and share < 0.75:
                state = "we FAILED to hold foreground (%d%%)" % round(100 * share)
            elif not hold_foreground and share > 0.25:
                state = "you did NOT give the foreground away (%d%%)" % round(100 * share)
            print("    -> foreground held %d%% of ticks  %s"
                  % (round(100 * share), "" if state == "ok" else "  << " + state))

            try:
                reply = input("    Could you feel the sideways PULL? [y/n] > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                reply = ""
            answers[key] = (True if reply.startswith("y")
                            else False if reply.startswith("n") else None)
            log.event("force_gate.phase", phase=key, foreground_share=round(share, 3),
                      felt=answers[key])
    finally:
        try:
            effect.stop()
        except Exception:
            pass
        try:
            loop.run_until_complete(motor.try_unload_effect_async(effect))
        except Exception:
            pass

    rule("FORCE GATE VERDICT")
    a1, b, a2 = answers.get("A1"), answers.get("B"), answers.get("A2")
    print("  foreground, force felt : %s" % _yn(a1))
    print("  background, force felt : %s" % _yn(b))
    print("  foreground again       : %s" % _yn(a2))
    print()

    if a1 is False:
        print("  >>> INCONCLUSIVE. The force was not felt even with focus held, so there")
        print("      was nothing to lose. Raise --magnitude and re-run.")
        outcome = "no-baseline"
    elif a1 and b is False and a2:
        print("  >>> CONFIRMED: force output IS foreground-gated. It died when focus went")
        print("      away and came back when focus returned, with nothing else changing.")
        print("      A background process cannot drive this motor while a game is in front.")
        outcome = "gated"
    elif a1 and b:
        print("  >>> force SURVIVES the background. The gate does not apply to output on")
        print("      this setup -- the vJoy bridge architecture is viable after all.")
        outcome = "not-gated"
    elif a1 and b is False and a2 is False:
        print("  >>> force died and did NOT come back. That is not a foreground gate --")
        print("      it matches the known 'motor left dead after a hold' fault. Re-run;")
        print("      if it repeats, the motor needs a reset between phases.")
        outcome = "died-permanently"
    else:
        print("  >>> INCONCLUSIVE -- not all three phases were answered.")
        outcome = "incomplete"

    log.event("force_gate.verdict", outcome=outcome, a1=a1, b=b, a2=a2)
    return outcome, answers


def _yn(value):
    return "yes" if value is True else "no" if value is False else "unanswered"


def parse_args():
    p = argparse.ArgumentParser(
        description="Does WGI still read the wheel and drive the motor from the background?")
    p.add_argument("--seconds", type=float, default=12.0,
                   help="length of each phase (default 12)")
    p.add_argument("--no-force", action="store_true",
                   help="test readings only; never touch the motor")
    p.add_argument("--force-only", action="store_true",
                   help="skip the reading phases and run only the A->B->A force gate test")
    p.add_argument("--reading-only", action="store_true",
                   help="run only the A->B->A reading gate test; never touch the motor")
    p.add_argument("--gain", type=float, default=0.5, help="motor master gain (default 0.5)")
    p.add_argument("--magnitude", type=float, default=0.35,
                   help="constant-force magnitude (default 0.35)")
    p.add_argument("--wait", type=float, default=30.0, help="detection timeout")
    p.add_argument("--no-log", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    log_path = None if args.no_log else log.start(prefix="wgi_background")

    if init_apartment is not None:
        try:
            init_apartment()
        except TypeError:
            init_apartment(0)

    rule("Can a BACKGROUND process drive this wheel?")
    print("  This is the question the whole vJoy bridge depends on. It takes about a")
    print("  minute and needs you to click other windows on purpose.")
    if log_path:
        print("  Log: %s" % log_path)

    pump = PumpThread()
    pump.start()
    pump.ready.wait(timeout=5)
    if not pump.hwnd:
        print("  WARNING: no message-pump window; enumeration will probably stay empty.")

    loop = asyncio.new_event_loop()
    effect = None
    motor = None
    try:
        raw, wheels = wait_for_devices(args.wait, pump)
        if not has_motor(raw, wheels):
            rule("RESULT")
            print("  No wheel with a force-feedback motor found. Put the wheel in Xbox mode")
            print("  (long-press PROFILE) and press one of its buttons while this runs.")
            return 1

        motor, label, wheel, _identity = report(raw, wheels)
        if wheel is None:
            rule("RESULT")
            print("  Found a motor via %s, but no RacingWheel to read position from." % label)
            print("  This test needs the position reading. Cannot continue.")
            return 1

        rule("Wheel found")
        describe_motor(motor)

        if args.reading_only:
            reading_gate_test(wheel, pump, args.seconds)
            return 0

        if args.force_only:
            force_gate_test(motor, pump, loop, args.seconds, args.gain, args.magnitude)
            return 0

        foreground_phase = Phase("foreground", args.seconds)
        background_phase = Phase("background", args.seconds)

        # --- Phase 1: we are in front. Establishes that readings work at all. ---
        pump.ensure_foreground()
        sample_phase(wheel, pump.hwnd, foreground_phase, [
            "PHASE 1 of 2 -- baseline, with THIS window in front.",
            "",
            "TURN THE WHEEL back and forth, a good part of its range, until the",
            "countdown ends. The '#' below should sweep across the bar.",
        ])

        if not foreground_phase.alive:
            print()
            print("  (the reading did not move -- phase 2 will not mean much)")

        # --- Phase 2: hand the foreground away. This is the actual experiment. ---
        if not args.no_force:
            try:
                motor.master_gain = max(0.0, min(1.0, args.gain))
            except Exception as exc:
                print("  could not set gain: %s" % exc)
            effect = ff.ConstantForceEffect()
            effect.set_parameters(
                Vector3(max(0.0, min(1.0, args.magnitude)), 0.0, 0.0),
                timedelta(seconds=args.seconds + 5))
            result = loop.run_until_complete(motor.load_effect_async(effect))
            if result == ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
                effect.start()
                print()
                print("  A steady sideways force is now running. Note how it feels while")
                print("  we are still in front -- you will be asked whether it survives.")
                time.sleep(2.0)
            else:
                print("  effect load failed (%s); continuing without force." % result)
                effect = None

        sample_phase(wheel, pump.hwnd, background_phase, [
            "PHASE 2 of 2 -- THE TEST.  >>> CLICK ANOTHER WINDOW NOW <<<",
            "",
            "Click your editor, a browser, anything but this one, and leave it",
            "focused for the whole countdown. Keep turning the wheel.",
            "",
            "The right-hand tag must read 'ANOTHER WINDOW' or nothing is tested.",
            "Watch whether the '#' keeps tracking the wheel -- and, if force is on,",
            "whether you can still feel it.",
        ])

        force_answer = None
        if effect is not None:
            try:
                effect.stop()
            except Exception:
                pass
            print()
            rule()
            print("  While the OTHER window had focus -- could you still feel the force?")
            try:
                answer = input("  [y] yes, still there   [n] no, it went dead  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer.startswith("y"):
                force_answer = True
            elif answer.startswith("n"):
                force_answer = False
            else:
                print("  (no answer -- reporting on readings only)")

        verdict(foreground_phase, background_phase, force_answer)
        return 0

    except KeyboardInterrupt:
        print("\n  interrupted.")
        return 130
    finally:
        if effect is not None:
            try:
                effect.stop()
            except Exception:
                pass
            try:
                loop.run_until_complete(motor.try_unload_effect_async(effect))
            except Exception:
                pass
        if motor is not None:
            try:
                loop.run_until_complete(motor.try_reset_async())
            except Exception:
                pass
        loop.close()
        pump.stop()
        print("\n  Motor released.")
        if not args.no_log:
            log.stop()


if __name__ == "__main__":
    sys.exit(main())
