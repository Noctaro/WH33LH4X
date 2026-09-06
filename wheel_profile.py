"""
Per-wheel calibration profile: measure the signs once, save them, reuse them forever.

A resist effect and an assist effect differ by one sign, and this firmware does not honour
the documented sign under any convention tried. So the only fact a closed loop needs is
measured instead: when Vector3(+x) is commanded, which way does reading.wheel move? Call that
sign k. See run_software_condition.

A closed loop cannot run away the way the firmware conditions do, because the force is
derived from the motion it opposes, so a wrong sign makes it inert rather than violent.

CONSTRAINT: never block on input. Position readings are foreground gated, a terminal is a
separate process, and any prompt waiting on Enter makes every later reading come back 0.0.
Manual steps are timed windows with a countdown. See
docs/hardware.md#the-foreground-owns-the-motor.
"""

import json
import os
import time
from datetime import datetime, timedelta

import winrt.windows.gaming.input.forcefeedback as ff
from winrt.windows.foundation.numerics import Vector3

import ffb_render as render
import probe_log as log

PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wheel_profile.json")

# Below this, a measurement is noise rather than movement, and we refuse to guess a sign
# from it. Wheel readings are normalised to -1..1, so 0.05 is 2.5% of full travel.
MOVE_EPSILON = 0.05

# (The velocity smoothing factor moved to ffb_render.WheelState with the control laws.)

PROFILE_VERSION = 2


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def device_key(vendor_id, product_id):
    return "%04X:%04X" % (vendor_id, product_id)


def load_profile(key):
    """Return the saved profile dict for this device, or None."""
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (IOError, OSError, ValueError):
        return None
    entry = data.get(key)
    if not isinstance(entry, dict):
        return None
    if entry.get("version") != PROFILE_VERSION:
        return None
    return entry


def save_profile(key, entry):
    """Merge one device's profile into the file, preserving other devices' entries."""
    data = {}
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            data = loaded
    except (IOError, OSError, ValueError):
        pass

    entry = dict(entry)
    entry["version"] = PROFILE_VERSION
    entry["calibrated"] = datetime.now().isoformat(timespec="seconds")
    data[key] = entry

    with open(PROFILE_PATH, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    return entry


def describe_profile(entry):
    if not entry:
        return ["  no saved calibration for this device. Press 'k' to measure it"]
    k = entry.get("force_to_reading_sign", 0)
    lines = [
        "  calibrated : %s" % entry.get("calibrated", "?"),
        "  force->reading sign : %+d   (Vector3(+x) makes reading.wheel go %s)"
        % (k, "UP" if k > 0 else "DOWN"),
        "  live_update         : %s   (set_parameters on a RUNNING effect)"
        % ("YES" if entry.get("live_update") else "NO"),
    ]
    span = entry.get("reading_span")
    if span:
        lines.append("  reading span        : %.3f of the -1..1 range seen while turning"
                     % span)
    return lines


# ---------------------------------------------------------------------------
# Measurement primitives
# ---------------------------------------------------------------------------

def motor_state(session, when):
    """Log the motor's own view of itself. Cheap, and the first thing worth knowing."""
    fields = {"when": when}
    for attr in ("is_enabled", "are_effects_paused", "master_gain"):
        try:
            fields[attr] = getattr(session.motor, attr)
        except Exception as exc:
            fields[attr] = "ERR(%s)" % exc
    log.event("motor.state", **fields)
    return fields


def _drop(session, effect):
    """Stop and unload an effect, swallowing everything. Never leave the motor holding."""
    try:
        effect.stop()
    except Exception:
        pass
    try:
        # Logged because a silently failing unload leaves a stale effect owning the motor,
        # and every later effect then loads "successfully" while producing no torque.
        unloaded = session.sync(session.motor.try_unload_effect_async(effect))
        log.event("drop.unload", ok=unloaded)
    except Exception as exc:
        log.event("drop.unload_raised", error=exc)
    if effect in session.loaded:
        session.loaded.remove(effect)


def recover(session, why):
    """
    Put the motor back into the state it has at session start.

    Releasing a hold can leave the motor accepting effects while producing no torque, with
    load Succeeded and state Running. Resetting and re-enabling restores a known good
    state.
    """
    log.event("recover.begin", why=why)
    motor_state(session, "before recover")
    try:
        log.event("recover.reset", ok=session.sync(session.motor.try_reset_async()))
    except Exception as exc:
        log.event("recover.reset_raised", error=exc)
    try:
        log.event("recover.enable", ok=session.sync(session.motor.try_enable_async()))
    except Exception as exc:
        log.event("recover.enable_raised", error=exc)
    try:
        if session.motor.are_effects_paused:
            session.motor.resume_all_effects()
            log.event("recover.resumed")
    except Exception as exc:
        log.event("recover.resume_raised", error=exc)
    time.sleep(0.3)
    motor_state(session, "after recover")


def _settle(session, seconds=0.6):
    """Let the wheel stop moving, then report where it is."""
    time.sleep(seconds)
    return session.read_wheel()


class free_wheel(object):
    """
    Context manager that makes the wheel light enough to turn by hand.

    At rest the firmware applies a stiff auto-centering that swamps any manual movement
    being measured. Claiming the motor, even with a zero magnitude effect, suspends it.

    CONSTRAINT: releasing the effect does not hand the stock centering back. Only losing the
    foreground does. __exit__ still drops the effect, but the wheel stays slack while this
    process is in front. See docs/hardware.md#the-foreground-owns-the-motor.
    """

    def __init__(self, session, seconds):
        self.session = session
        self.seconds = seconds
        self.effect = None

    def __enter__(self):
        effect = ff.ConstantForceEffect()
        effect.set_parameters(Vector3(0.0, 0.0, 0.0), timedelta(seconds=self.seconds + 2.0))
        try:
            result = self.session.sync(self.session.motor.load_effect_async(effect))
        except Exception:
            return self
        if result != ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
            return self
        self.session.loaded.append(effect)
        self.effect = effect
        try:
            self.session.unpause()
            self.session.grab_foreground()
            effect.start()
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        if self.effect is not None:
            _drop(self.session, self.effect)
            # Handing the motor back is exactly where it seems to get stuck producing no
            # torque, so restore the known-good state rather than assume unload was enough.
            recover(self.session, "free_wheel released")
        return False


def _sample_span(session, seconds, note):
    """
    Watch the wheel's reading for a while and report (low, high, samples_seen).

    Re-grabs foreground on every tick: readings are foreground-gated, and a reading of
    exactly 0.0 forever is the signature of having lost it.
    """
    low = high = None
    end_time = time.monotonic() + seconds
    last_print = 0.0
    log.event("span.begin", seconds=seconds, note=note)
    while time.monotonic() < end_time:
        session.grab_foreground()
        value = session.read_wheel()
        # Every sample goes to the log even though only one a second is printed. The
        # shape of the signal is what tells a dead reading from a stiff wheel.
        log.event("span.sample", reading=value if value is not None else float("nan"),
                  fg=session.is_foreground())
        if value is not None:
            low = value if low is None else min(low, value)
            high = value if high is None else max(high, value)
        now = time.monotonic()
        if now - last_print > 1.0:
            remaining = end_time - now
            print("    %s  %.0fs left   now %+.3f  range %+.3f..%+.3f"
                  % (note, remaining, value if value is not None else float("nan"),
                     low if low is not None else 0.0, high if high is not None else 0.0),
                  flush=True)
            last_print = now
        time.sleep(0.02)
    log.event("span.end", low=low if low is not None else float("nan"),
              high=high if high is not None else float("nan"))
    return low, high


def _wait_for_centre(session, tolerance=0.25, steady=1.0, timeout=25.0):
    """
    Block until the wheel is resting near the middle of its travel.

    CONSTRAINT: nothing can be measured from an end stop. The reading clamps at +-1.000
    there, so a push into the stop and a motor producing no torque look alike.

    Waits for the reading to stay inside the tolerance rather than pass through it, so a
    wheel swinging past centre does not count. The caller should hold the motor, see
    free_wheel, so the wheel is light to move.
    """
    log.event("centre.begin", tolerance=tolerance, timeout=timeout)
    deadline = time.monotonic() + timeout
    inside_since = None
    last_print = 0.0

    while time.monotonic() < deadline:
        session.grab_foreground()
        value = session.read_wheel()
        now = time.monotonic()

        if value is None:
            inside_since = None
        elif abs(value) <= tolerance:
            if inside_since is None:
                inside_since = now
            elif now - inside_since >= steady:
                print("    centred at %+.3f" % value)
                log.event("centre.ok", reading=value)
                return True
        else:
            inside_since = None

        if now - last_print > 0.7:
            bar = _position_bar(value)
            print("    %s  %+.3f   %s" % (bar, value if value is not None else float("nan"),
                                          "holding..." if inside_since else "centre it"),
                  flush=True)
            last_print = now
        time.sleep(0.02)

    print("    Gave up waiting for the wheel to be centred.")
    log.event("centre.timeout")
    return False


def _position_bar(value, width=21):
    """A tiny text gauge, so 'centre it' is obvious without reading numbers."""
    if value is None:
        return "[" + "?" * width + "]"
    middle = width // 2
    index = int(round((max(-1.0, min(1.0, value)) + 1.0) / 2.0 * (width - 1)))
    cells = ["-"] * width
    cells[middle] = "|"
    cells[index] = "#"
    return "[" + "".join(cells) + "]"


def _push_and_watch(session, magnitude, seconds):
    """
    Apply a constant force and report how far the wheel's own reading travelled.

    Returns (delta, error). delta is signed in reading units, and the wheel may hit its end
    stop partway through, which is fine: only the direction is needed.

    CONSTRAINT: master_gain is latched when the effect loads, so it is set here rather than
    inherited. The firmware's auto-centering returns between effects and the push has to
    overcome it from a standing start, so a strong magnitude is used deliberately.
    """
    try:
        session.motor.master_gain = 1.0
    except Exception as exc:
        print("      (could not set master_gain: %s)" % exc)

    effect = ff.ConstantForceEffect()
    effect.set_parameters(Vector3(magnitude, 0.0, 0.0), timedelta(seconds=seconds + 1.0))

    log.event("push.begin", magnitude=magnitude, seconds=seconds,
              gain=getattr(session.motor, "master_gain", None))
    try:
        result = session.sync(session.motor.load_effect_async(effect))
    except Exception as exc:
        log.event("push.load_raised", error=exc)
        return None, "LoadEffectAsync raised: %s" % exc
    log.event("push.load", result=result)
    if result != ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
        return None, "load -> %s" % result

    session.loaded.append(effect)
    try:
        session.unpause()
        session.grab_foreground()
        # The menu path that reliably produces torque counts down ~3s between loading an
        # effect and starting it. This one used to start immediately. Matching it is
        # cheap insurance against a driver that needs settling time after a load.
        time.sleep(0.5)
        motor_state(session, "push before start")
        start = session.read_wheel()
        if start is None:
            return None, "no position reading available"
        if abs(start) > 0.97:
            # Against a stop the reading is clamped. A push AWAY from it still measures
            # fine, so this is recorded rather than treated as a failure. Only the very
            # first push has to start clear of the stops, and measure_force_sign checks
            # that before it begins.
            log.event("push.at_stop", start=start, magnitude=magnitude)

        effect.start()
        log.event("push.started", start=start, state=getattr(effect, "state", None))
        # Track the FURTHEST point reached, not the final one: once the force stops the
        # wheel may drift back, and a wheel already resting against a stop barely moves.
        furthest = start
        end_time = time.monotonic() + seconds
        last_print = 0.0
        while time.monotonic() < end_time:
            session.grab_foreground()
            value = session.read_wheel()
            log.event("push.sample", magnitude=magnitude,
                      reading=value if value is not None else float("nan"),
                      delta=(value - start) if value is not None else float("nan"),
                      fg=session.is_foreground(), state=getattr(effect, "state", None))
            if value is not None and abs(value - start) > abs(furthest - start):
                furthest = value
            now = time.monotonic()
            if now - last_print > 0.35:
                # Printed live so that "the wheel did not move" and "the wheel moved but
                # the reading was not updating" cannot be confused for each other.
                print("      push %+.2f   reading %+.3f  (from %+.3f)"
                      % (magnitude, value if value is not None else float("nan"), start),
                      flush=True)
                last_print = now
            time.sleep(0.02)
        effect.stop()
        log.event("push.end", magnitude=magnitude, start=start, furthest=furthest,
                  delta=furthest - start)
        return furthest - start, None
    finally:
        _drop(session, effect)


def measure_force_sign(session, magnitude=0.85, seconds=1.6):
    """
    Which way does Vector3(+x) move the wheel's own reading? Returns +1 or -1.

    Pushes both ways and subtracts. Doing both directions makes this robust to starting
    against an end stop, where one of the two pushes cannot move at all.
    """
    # CONSTRAINT: the first push needs room. From an end stop the reading clamps at +-1.000,
    # so pushing into it registers nothing whether the motor works or is dead.
    start = session.read_wheel()
    if start is not None and abs(start) > 0.9:
        return None, ("wheel is resting against an end stop (reading %+.3f).\n"
                      "      Nothing can be measured from there. Centre it and retry."
                      % start)

    # Two passes each way. The first push starts against the firmware's centering from
    # wherever the wheel happens to be resting; by the second, the motor has been held
    # continuously enough that the wheel is free and the reading is unambiguous.
    plus = minus = 0.0
    for attempt in range(2):
        value, err = _push_and_watch(session, magnitude, seconds)
        if err:
            return None, err
        plus = value if abs(value) > abs(plus) else plus
        _settle(session, 0.4)
        value, err = _push_and_watch(session, -magnitude, seconds)
        if err:
            return None, err
        minus = value if abs(value) > abs(minus) else minus
        _settle(session, 0.4)
        if abs(plus - minus) >= MOVE_EPSILON:
            break

    net = plus - minus
    detail = "push +%.2f moved %+.3f, push -%.2f moved %+.3f, net %+.3f" % (
        magnitude, plus, magnitude, minus, net)
    if abs(net) < MOVE_EPSILON:
        return None, ("wheel barely moved (%s).\n"
                      "      Either the motor is not producing torque right now, or this\n"
                      "      process lost foreground. Check that a plain constant force\n"
                      "      (menu 1) at magnitude 1.0 still moves the wheel." % detail)
    return (1 if net > 0 else -1), detail


def measure_live_update(session, magnitude=0.55, seconds=1.1):
    """
    Does set_parameters affect a running effect, or is it latched at load like master_gain?

    This decides whether software computed effects are possible at all. Automatic: push one
    way, rewrite the magnitude to the opposite sign mid-flight, see whether the wheel
    reverses.
    """
    effect = ff.ConstantForceEffect()
    effect.set_parameters(Vector3(magnitude, 0.0, 0.0), timedelta(seconds=4 * seconds))

    try:
        result = session.sync(session.motor.load_effect_async(effect))
    except Exception as exc:
        return None, "LoadEffectAsync raised: %s" % exc
    if result != ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
        return None, "load -> %s" % result

    session.loaded.append(effect)
    try:
        session.unpause()
        session.grab_foreground()
        effect.start()

        def travel(hold):
            start = session.read_wheel()
            furthest = start
            end_time = time.monotonic() + hold
            while time.monotonic() < end_time:
                session.grab_foreground()
                value = session.read_wheel()
                if value is not None and start is not None \
                        and abs(value - start) > abs(furthest - start):
                    furthest = value
                time.sleep(0.02)
            return (furthest - start) if (start is not None and furthest is not None) else 0.0

        first = travel(seconds)
        # The rewrite under test.
        effect.set_parameters(Vector3(-magnitude, 0.0, 0.0), timedelta(seconds=4 * seconds))
        second = travel(seconds)
        effect.stop()

        detail = "before rewrite %+.3f, after rewrite %+.3f" % (first, second)
        if abs(second) < MOVE_EPSILON:
            return False, "no movement after the rewrite (%s)" % detail
        if (first > 0) == (second > 0) and abs(first) >= MOVE_EPSILON:
            return False, "kept pushing the same way after the rewrite (%s)" % detail
        return True, detail
    finally:
        _drop(session, effect)


# ---------------------------------------------------------------------------
# The calibration routine
# ---------------------------------------------------------------------------

def _countdown(session, prefix, seconds=3):
    # Re-grabs foreground throughout: anything clicked during the pause would take it, and
    # that silently zeroes readings and torque alike.
    for n in range(seconds, 0, -1):
        session.grab_foreground()
        print("    %s %d..." % (prefix, n), flush=True)
        time.sleep(1.0)


def calibrate(session):
    """
    Measure what a closed loop needs and return a profile dict (unsaved).

    Nothing here ever waits on Enter. Readings are foreground-gated, and a terminal is a
    separate process, so blocking on input would zero out the very numbers being measured.
    Manual steps are timed windows instead. Returns None if calibration cannot complete.
    """
    print()
    print("=" * 72)
    print("  CALIBRATION")
    print("=" * 72)
    print("  Do not click on the terminal or any other window during this. Readings")
    print("  and force output both stop the moment this process loses foreground.")

    if session.read_wheel() is None:
        print("  No wheel position reading available (RacingWheel not exposed).")
        print("  Calibration needs it: it is how the wheel's own movement is measured.")
        return None

    profile = {"device": session.device_name}

    # --- Step 1: does the position reading track the wheel at all? -----------
    # Not a direction test, direction is measured in step 2. This only establishes that
    # the reading is live, because if it is not, steps 2 and 3 would silently produce
    # confident nonsense.
    print()
    print("  STEP 1 of 3: force direction  (measured, not asked)")
    print()
    print("  This runs FIRST and on an untouched motor, deliberately. Claiming and then")
    print("  releasing the motor has been observed to leave it accepting effects while")
    print("  producing no torque: load Succeeded, state Running, wheel dead. So the one")
    print("  measurement that needs real torque happens before anything else touches it.")
    print()
    print("  The wheel should be centred by its own failsafe spring already.")
    print("  >> HANDS OFF THE WHEEL. It will be pushed each way and may reach its")
    print("     end stop. That is expected and harmless, just keep hands clear.")

    motor_state(session, "calibration start")
    resting = session.read_wheel()
    if resting is not None and abs(resting) > 0.4:
        print()
        print("  The wheel is at %+.3f, not centred. Let go of it so its own" % resting)
        print("  centering can pull it back.")
        if not _wait_for_centre(session, tolerance=0.4):
            return None

    _countdown(session, "pushing in", 4)

    force_sign, detail = measure_force_sign(session)
    if force_sign is None:
        print("    FAILED: %s" % detail)
        print()
        print("    Sanity check: quit, restart, and try menu 1 (constant force) at")
        print("    magnitude 1.0 as the very first thing. If that moves the wheel but")
        print("    calibration does not, the motor is being left dead by something the")
        print("    probe did earlier in the session, and the log will show what.")
        return None
    profile["force_to_reading_sign"] = force_sign
    print("    %s" % detail)
    print("    -> Vector3(+x) drives reading.wheel %s"
          % ("UP" if force_sign > 0 else "DOWN"))

    # --- Step 2: can a running effect be rewritten? --------------------------
    print()
    print("  STEP 2 of 3: live parameter updates  (measured, not asked)")
    print("  Decides whether software-computed effects are possible at all.")
    print("  >> STILL HANDS OFF.")
    _countdown(session, "starting in", 3)

    live, detail = measure_live_update(session)
    if live is None:
        print("    FAILED: %s" % detail)
        profile["live_update"] = False
    else:
        profile["live_update"] = bool(live)
        print("    %s" % detail)
        print("    -> set_parameters on a running effect: %s"
              % ("WORKS" if live else "IGNORED (latched at load, like master_gain)"))

    # --- Step 3: how much of the reading range does the wheel actually use? --
    # Informational, and last on purpose: it holds the motor open, which is what appears to
    # leave it unable to produce torque. Nothing needing real force runs after this.
    print()
    print("  STEP 3 of 3: reading range  (optional, nothing depends on it)")
    print("  The wheel goes LOOSE now. Turn it fully each way.")
    _countdown(session, "get ready to turn in", 3)
    print()
    print("  >>> TURN THE WHEEL FULLY LEFT, THEN FULLY RIGHT, NOW <<<", flush=True)

    with free_wheel(session, 12.0):
        low, high = _sample_span(session, 8.0, "turning")

    if low is not None and high is not None and (high - low) >= 0.2:
        profile["reading_span"] = high - low
        print("    reading moved %+.3f .. %+.3f" % (low, high))
    else:
        print("    reading did not move much, skipped, nothing depends on it")

    _settle(session)
    print()
    print("  Calibration complete.")
    return profile


# ---------------------------------------------------------------------------
# Software condition effects
# ---------------------------------------------------------------------------

SOFTWARE_CONDITIONS = ["spring", "damper", "friction", "inertia"]

# CONSTRAINT: fitted to logged ticks from this wheel, not chosen. Hand-turning produces
# |velocity| around 0.3 reading-units/sec (peaks near 1.0) and |acceleration| in the low
# tens. The original guesses (velocity/8, acceleration/60) commanded about 1% of full
# torque at normal turning speed: present in the logs, imperceptible in the hands.
#
# Each is scaled by the menu magnitude afterwards, then clamped to +-1.0, so raising one
# costs nothing at the extremes: it only decides how quickly the effect reaches full.
CONDITION_GAINS = {
    "spring": 2.0,      # per unit of displacement: half-lock -> nearly full force
    "damper": 1.0,      # per unit of velocity: brisk turning -> full force
    "friction": 1.0,    # direction only, so this is just an on/off scale
    "inertia": 0.10,    # per unit of acceleration
}

def run_software_condition(session, kind, profile, magnitude, duration, rate_hz=80.0):
    """
    A condition effect computed here rather than in the firmware.

    One constant-force effect is held open and its magnitude rewritten ~80x a second from
    the wheel's own position, using the signs measured by calibrate(). The force is always
    derived from the motion it opposes, so unlike the firmware's condition effects this
    physically cannot run away with you.

    Requires profile["live_update"]. Without it, rewriting a running effect does nothing.
    """
    if kind not in SOFTWARE_CONDITIONS:
        raise ValueError("unknown condition %r" % kind)
    if not profile:
        print("  No calibration profile. Press 'k' first.")
        return
    if not profile.get("live_update"):
        print("  This wheel ignores set_parameters on a running effect (measured during")
        print("  calibration), so a software loop cannot drive it. Nothing to run.")
        return
    if session.read_wheel() is None:
        print("  No position reading available; a closed loop needs one.")
        return

    # The one measured fact: pushing with Vector3(+x) moves reading.wheel by this sign.
    # Everything below is expressed in READING units, then converted with this at the end.
    k = profile["force_to_reading_sign"]

    effect = ff.ConstantForceEffect()
    effect.set_parameters(Vector3(0.0, 0.0, 0.0), timedelta(seconds=duration + 2.0))

    print()
    print("  SOFTWARE %s: %s" % (kind.upper(), render.CONDITION_HELP[kind]))
    print("  computed here at %.0f Hz from the wheel's own position" % rate_hz)
    print("  strength %.2f, %.0fs. HOLD AND TURN, it only reacts to movement."
          % (magnitude, duration))

    try:
        result = session.sync(session.motor.load_effect_async(effect))
    except Exception as exc:
        print("  LoadEffectAsync raised: %s" % exc)
        return
    if result != ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
        print("  load -> %s" % result)
        return

    session.loaded.append(effect)
    period = 1.0 / rate_hz
    peak = 0.0
    try:
        session.unpause()
        session.grab_foreground()
        for n in (3, 2, 1):
            print("    %d..." % n, flush=True)
            time.sleep(1.0)
        print("  >>> TURN THE WHEEL <<<", flush=True)

        effect.start()

        # "Centre" is wherever the wheel is sitting when the effect starts, so a spring
        # pulls back to the position you began at rather than to an assumed zero.
        centre = session.read_wheel()

        # The control law itself now lives in ffb_render, parameterised, so the bridge can
        # drive it with a game's coefficients instead of these fitted ones. The numbers are
        # unchanged: test_ffb_render.py compares the two implementations across the full
        # input range and requires exact equality.
        params = render.legacy_condition_params(kind, CONDITION_GAINS[kind], offset=centre)
        state = render.WheelState()
        state.update(centre, time.monotonic())

        previous_time = time.monotonic()
        end_time = previous_time + duration
        last_foreground = 0.0

        while time.monotonic() < end_time:
            time.sleep(period)
            now = time.monotonic()
            dt = now - previous_time
            if dt <= 0.0:
                continue

            position = session.read_wheel()
            if position is None:
                continue

            state.update(position, now)
            command = render.condition_force(kind, params, state) * magnitude
            peak = max(peak, abs(command))

            log.event("cond.tick", kind=kind, dt=dt, position=position,
                      displacement=position - centre, velocity=state.velocity,
                      raw_velocity=state.raw_velocity, accel=state.acceleration,
                      command=command, vector=command * k)

            # Throttled: re-asserting foreground 80x a second is pure overhead, and the
            # loop's timing matters more than instant recovery from a lost focus.
            if now - last_foreground > 0.1:
                session.grab_foreground()
                last_foreground = now
            try:
                # command says which way the READING should be driven; k converts that
                # into the force vector that actually drives it that way.
                effect.set_parameters(
                    Vector3(command * k, 0.0, 0.0),
                    timedelta(seconds=duration + 2.0))
            except Exception as exc:
                print("  set_parameters failed mid-loop: %s" % exc)
                break

            previous_time = now

        effect.stop()
        print("  done, peak commanded force %.2f" % peak)
        if peak < 0.02:
            print("  (the wheel barely moved, so almost no force was called for --")
            print("   these only react to movement, so turn it during the run)")
    except KeyboardInterrupt:
        print("\n  Interrupted.")
    finally:
        _drop(session, effect)
