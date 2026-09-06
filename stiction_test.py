r"""
stiction_test.py: how much force does it take to move this wheel at all?

Breakaway force is the level at which a stationary wheel starts to move. A centring force
proportional to steering angle is smallest near centre, exactly where it has to overcome the
wheel's own friction, so a motor that cannot break stiction there leaves the wheel holding
wherever it was left. That is hardware, so it is measurable without a game: ramp commanded
force up from zero until the wheel moves. Both directions, repeated, since stiction scatters.

CONSTRAINT: this window must stay in front. It drives the motor directly through WGI, which
gates force output on foreground. The test grabs the foreground itself.

WARNING: this firmware goes quiet when commanded a force it cannot act on, audible as two
short hums, and no WGI call reports it. What triggers it is a position the wheel will not
leave. A level that does not move the wheel is therefore followed by a kick off the detent and
a re-centre, never another push from the same place. See unstick, and
docs/hardware.md#stiction-and-positions-the-wheel-will-not-leave.

Force is ramped from zero, capped by --max, and released on every exit path including Ctrl+C.
Let the wheel go while this runs; holding it defeats the measurement.

Usage:
    .venv/Scripts/python.exe stiction_test.py
    .venv/Scripts/python.exe stiction_test.py --max 0.5 --passes 5
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

import probe_log as log
from wgi_probe import (
    LOAD_RESULT_NAMES,
    STATE_NAMES,
    PumpThread,
    has_motor,
    report,
    rule,
    user32,
    wait_for_devices,
)

# CONSTRAINT: this script holds the foreground, so Ctrl+C cannot reach the console. Killing
# the process instead would skip the teardown that releases the motor. GetAsyncKeyState reads
# the physical key regardless of focus, so ESC works from anywhere.
VK_ESCAPE = 0x1B
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]


def abort_requested():
    return bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)


class ForegroundLost(Exception):
    """
    Windows took the foreground away, so force output is silent.

    Raised rather than returned because EVERY measurement in flight becomes meaningless the
    instant this happens, and the old behaviour was to carry on: PumpThread.ensure_foreground
    warns once and then fails quietly forever, so a run that lost focus kept ramping against a
    motor receiving nothing, found no movement at any level, and spent a minute per pass doing
    it. That reads as a hung script, and any number it eventually prints is fiction.
    """


def require_foreground(pump):
    """Block until our window is genuinely in front. False means the user pressed ESC."""
    if pump.ensure_foreground():
        return True
    print()
    print("  PAUSED. Windows will not let this window take the foreground, and WGI")
    print("  gates force output on it. Nothing measured now would mean anything.")
    print("  CLICK THE 'FFB probe' WINDOW to continue, or press ESC to stop.")
    while not pump.ensure_foreground():
        if abort_requested():
            return False
        time.sleep(0.1)
    print("  resumed.")
    print()
    return True


def hold_foreground(pump):
    """Assert the foreground, and refuse to keep measuring without it."""
    if not pump.ensure_foreground():
        raise ForegroundLost
    return True


# How far the wheel must move before we call it "moving". Position is -1..+1 across the whole
# travel, and the reading is quantised, so this has to clear the noise floor without being so
# large that a genuine slow creep is missed. 0.01 is roughly 5 degrees on a 900 degree wheel.
MOVED = 0.01

# CONSTRAINT: a pass must start near centre. Every pass pushes the wheel the same way, and a
# wheel against the end stop cannot move at any force, so the ramp keeps stepping up and
# reports the end stop as breakaway.
CENTRE_BAND = 0.08     # near enough to centre to begin a pass
LOCK_GUARD = 0.55      # stop pressing the moment a pass gets this far out
RECENTRE_FORCE = 0.25
RECENTRE_TIMEOUT = 4.0

# CONSTRAINT: never push twice from a position that did not move. That is what trips the
# firmware protection, after which the motor is done for the process. Rest at zero, kick the
# wheel off the detent, re-centre.
#
# The kick doubles as a liveness proof. If it moves the wheel the motor is fine and the next
# level measures real stiction; if it does not, the protection has already latched and the
# remaining levels would measure nothing.
STALL_REST = 0.8           # rest at zero before commanding anything again
STALL_KICK = 0.30          # well above any breakaway this wheel has shown
STALL_KICK_SECONDS = 0.6


def read_position(wheel):
    try:
        reading = wheel.get_current_reading()
    except Exception:
        return None
    return reading.wheel


def motor_state(motor, when):
    """
    Log what the motor says about itself. Cheap, and the first thing worth knowing.

    Modelled on wheel_profile.motor_state, which takes a Session this script does not have.
    These three fields are readable the whole time and NOTHING HERE EVER READ THEM: every
    theory about why the wheel went silent was guessed from "it did not move", which cannot
    tell a disabled motor from a paused one from an effect that never started.
    """
    fields = {"when": when}
    for attr in ("is_enabled", "are_effects_paused", "master_gain"):
        try:
            fields[attr] = getattr(motor, attr)
        except Exception as exc:
            fields[attr] = "ERR(%s)" % exc
    log.event("motor.state", **fields)
    return fields


def describe_state(fields):
    return "enabled=%s paused=%s gain=%s" % (fields.get("is_enabled"),
                                             fields.get("are_effects_paused"),
                                             fields.get("master_gain"))


class LocalMotor(object):
    """
    This script's own hold on the motor, in the order the two RELIABLE paths use.

    WHY NOT WgiMotorSink
    --------------------
    Local rather than reusing WgiMotorSink, which is barely exercised: during a game the
    bridge uses IpcMotorSink and the WGI calls happen in the shim. Keeping this local means
    the bridge and shim cannot be affected by anything tried here.

    CONSTRAINT: assert the foreground immediately before Start(). That is what the shim and
    wgi_probe have and the sink lacks, and enumeration and two async awaits sit between the
    last foreground check and Start(), long enough for focus to be taken back.
    """

    def __init__(self, motor, loop, pump, max_force=1.0, gain=1.0, hold_seconds=3600.0):
        import winrt.windows.gaming.input.forcefeedback as ff
        from winrt.windows.foundation.numerics import Vector3
        self._ff = ff
        self._vector3 = Vector3
        self.motor = motor
        self.loop = loop
        self.pump = pump
        self.max_force = min(1.0, abs(max_force))
        self.gain = min(1.0, abs(gain))
        self.hold_seconds = hold_seconds
        self.effect = None
        self._last = None

    def _sync(self, coro):
        return self.loop.run_until_complete(coro)

    def open(self):
        """Load and start the effect. Returns True only if it is verifiably Running."""
        ff, Vector3 = self._ff, self._vector3
        motor_state(self.motor, "before open")

        # A motor left holding an effect by a previous process, or by a run that was killed,
        # accepts new effects while producing no torque. Reset and enable restore the state a
        # fresh session has. wheel_profile.recover documents this and wgi_probe calls
        # try_enable_async at session start.
        for name, call in (("reset", lambda: self.motor.try_reset_async()),
                           ("enable", lambda: self.motor.try_enable_async())):
            try:
                log.event("motor.%s" % name, ok=self._sync(call()))
            except Exception as exc:
                log.event("motor.%s_failed" % name, error=repr(exc))
        motor_state(self.motor, "after enable")

        # Latched at load time. Setting it under a running effect does nothing.
        try:
            self.motor.master_gain = self.gain
        except Exception as exc:
            log.event("motor.gain_failed", error=repr(exc))

        effect = ff.ConstantForceEffect()
        effect.set_parameters(Vector3(0.0, 0.0, 0.0),
                              timedelta(seconds=self.hold_seconds))
        try:
            result = self._sync(self.motor.load_effect_async(effect))
        except Exception as exc:
            log.event("motor.load_raised", error=repr(exc))
            return False
        log.event("motor.load", result=LOAD_RESULT_NAMES.get(result, str(result)))
        if result != ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
            return False

        self.effect = effect

        # after the load, the way wgi_probe does it: a paused motor swallows every effect.
        try:
            if self.motor.are_effects_paused:
                self.motor.resume_all_effects()
                log.event("motor.resumed")
        except Exception as exc:
            log.event("motor.resume_failed", error=repr(exc))

        # THE STEP THE SINK IS MISSING. Assert the foreground in the gap between load and
        # start, then settle, then assert again immediately before Start().
        fg_before = self.pump.ensure_foreground()
        time.sleep(START_SETTLE)
        fg_at_start = self.pump.ensure_foreground()

        effect.start()
        self._last = 0.0
        time.sleep(0.15)

        state = None
        try:
            state = STATE_NAMES.get(effect.state, str(effect.state))
        except Exception as exc:
            state = "ERR(%s)" % exc
        log.event("motor.started", state=state, fg_before_settle=fg_before,
                  fg_at_start=fg_at_start)
        motor_state(self.motor, "after start")
        self.started_state = state
        self.fg_at_start = fg_at_start
        return True

    def set_force(self, x):
        """Rewrite the held effect's magnitude. Same shape as the shim: one effect, held."""
        if self.effect is None:
            return False
        value = max(-self.max_force, min(self.max_force, float(x)))
        if self._last is not None and abs(value - self._last) < 1e-4:
            return True
        try:
            self.effect.set_parameters(self._vector3(value, 0.0, 0.0),
                                       timedelta(seconds=self.hold_seconds))
            self._last = value
            return True
        except Exception as exc:
            log.event("motor.set_failed", error=repr(exc))
            return False

    def close(self):
        """
        Release the motor. Safe to call twice.

        The unload result is logged rather than swallowed, and it rules out the obvious
        explanation: a silently failing unload leaving a stale effect on the motor. Both the
        unload and the final reset report success every time, including on the cycles that
        came back silent.

        So the release succeeds by every account WGI gives, and the motor is dead
        anyway. Keep logging it: an unload that starts failing would be a real finding, and
        this is the only place it would show.
        """
        effect, self.effect = self.effect, None
        if effect is None:
            return
        for step in (lambda: effect.set_parameters(self._vector3(0.0, 0.0, 0.0),
                                                   timedelta(seconds=1)),
                     effect.stop):
            try:
                step()
            except Exception:
                pass
        try:
            log.event("motor.unload",
                      ok=self._sync(self.motor.try_unload_effect_async(effect)))
        except Exception as exc:
            log.event("motor.unload_raised", error=repr(exc))
        try:
            log.event("motor.final_reset", ok=self._sync(self.motor.try_reset_async()))
        except Exception as exc:
            log.event("motor.final_reset_raised", error=repr(exc))
        log.event("motor.closed")


# Force used to prove the motor is driving before any measurement is trusted. Well above the
# breakaway this wheel has ever shown, so "this did nothing" means the effect is not driving
# rather than that the wheel is merely stiff.
ENGAGE_FORCE = 0.30
ENGAGE_SECONDS = 1.2

# Rest between selftest probes, not a retry interval: closing and reopening the motor kills
# it after about two releases, so this opens once and reports a silent open rather than
# retrying.
OPEN_RETRY_REST = 1.5

# Seconds between loading the effect and starting it. wgi_probe counts down 3 s here and has
# never failed; that is longer than a measurement can spend, and the point is the foreground
# assertion that happens in the gap rather than the waiting itself.
START_SETTLE = 1.0

# What an open attempt can conclude. Four outcomes that "the wheel did not move" used to
# collapse into one, which is why three separate root-cause theories all looked plausible.
OPEN_VERDICTS = {
    "torque": "the wheel moved",
    "load": "the effect never loaded or never reached Running",
    "silent": "effect Running and motor enabled, but the wheel did not move",
    "foreground": "we did not own the foreground, so no force was ever going to arrive",
}

# TIMING. Modelled on wgi_probe's sweep, which has never shown a problem and is leisurely by
# comparison: a 3 s countdown before the first effect, 2.5 s per step, 0.4 s between steps.
# These are the same shape, trimmed to what a measurement can afford.
#
# A gentle ramp is right for a stiction measurement. It does not prevent the motor going
# silent: see force_and_watch.
# HOW LONG A FORCE LEVEL IS GIVEN TO DO SOMETHING.
#
# HOLD_WINDOW is patience: stiction releases are not instant, the motor winds up against the
# belt first. CONFIRM is the opposite, long enough that a quantisation blip is not read as
# movement and short enough that the wheel does not travel halfway to lock.
HOLD_WINDOW = 1.00
CONFIRM = 0.15
COOLDOWN = 0.40   # rest at zero force between levels

# HOW LONG A STALLED FORCE IS ALLOWED TO KEEP FLOWING.
#
# CONSTRAINT: this is what prevents the cut-out. The protection trips on one stalled second
# and anything done afterwards is too late, unstick included. A wheel that is going to move
# clears MOVED well inside CONFIRM, so waiting longer only pushes current through a rotor
# that is not turning.
STALL_CUTOFF = 0.35


def force_and_watch(sink, wheel, pump, force, seconds):
    """
    Command a force on the ALREADY-HELD effect and report how far the wheel moved.

    CONSTRAINT: nothing is loaded or unloaded here. That matches the shim, which holds one
    effect for the life of the game, and it is the shape the only complete run ever used.

    WARNING: a motor too weak to break stiction and a motor producing no torque give this
    script the same reading. What correlates with the failures is the wheel's own strength
    setting, which is upstream of everything here. See docs/tuning.md.
    """
    sink.set_force(0.0)
    time.sleep(0.4)
    hold_foreground(pump)
    start_pos = read_position(wheel)
    if start_pos is None:
        return None

    sink.set_force(force)
    hold_foreground(pump)
    started = time.monotonic()
    moved = 0.0
    deadline = started + seconds
    while time.monotonic() < deadline:
        if abort_requested():
            sink.set_force(0.0)
            raise KeyboardInterrupt
        try:
            hold_foreground(pump)
        except ForegroundLost:
            sink.set_force(0.0)
            raise
        now = read_position(wheel)
        if now is not None:
            moved = max(moved, abs(now - start_pos))
            # Never keep driving into the end stop. Past this point the wheel is not going to
            # move any further whatever we command, so holding the force only teaches the next
            # pass to start from a place where nothing can be measured.
            if abs(now) >= LOCK_GUARD:
                break
            if moved > MOVED and (time.monotonic() - started) >= CONFIRM:
                break
            # STOP PUSHING A ROTOR THAT IS NOT TURNING. One stalled second is enough to trip
            # the firmware cut-out for the rest of the process, and nothing measured after
            # that is real. `moved` is still whatever was seen, so a level that crept a
            # little is reported honestly as having crept a little.
            if moved <= MOVED and (time.monotonic() - started) >= STALL_CUTOFF:
                break
        time.sleep(0.01)

    sink.set_force(0.0)
    end_pos = read_position(wheel)
    time.sleep(COOLDOWN)

    # Every level, not just the pass. A pass reporting "nothing moved" hides whether the
    # motor did nothing or merely too little, and those are different failures.
    log.event("force.level", force=round(force, 4),
              start=-9.0 if start_pos is None else round(start_pos, 4),
              end=-9.0 if end_pos is None else round(end_pos, 4),
              moved=round(moved, 4))
    return moved


def recentre(sink, wheel, pump):
    """
    Drive the wheel back near centre so the next pass has room to travel.

    Returns True if it got there, which is not an engagement proof. A wheel already inside
    CENTRE_BAND returns True on the first read without any force being commanded, so a run
    can recentre "successfully" through a motor that is producing nothing. The log records
    `drove` so the two can be told apart afterwards.
    """
    start_pos = read_position(wheel)
    deadline = time.monotonic() + RECENTRE_TIMEOUT
    while time.monotonic() < deadline:
        if abort_requested():
            sink.set_force(0.0)
            raise KeyboardInterrupt
        try:
            hold_foreground(pump)
        except ForegroundLost:
            sink.set_force(0.0)
            raise
        pos = read_position(wheel)
        if pos is None:
            break
        if abs(pos) <= CENTRE_BAND:
            sink.set_force(0.0)
            time.sleep(COOLDOWN)
            # Logged with `drove` because returning True proves nothing on its own: a wheel
            # already inside the band is returned immediately without commanding anything, so
            # a successful recentre is NOT evidence the motor still works.
            log.event("recentre.ok",
                      start=-9.0 if start_pos is None else round(start_pos, 4),
                      end=round(pos, 4),
                      drove=start_pos is not None and abs(start_pos) > CENTRE_BAND)
            return True
        sink.set_force(-RECENTRE_FORCE if pos > 0 else RECENTRE_FORCE)
        time.sleep(0.01)

    sink.set_force(0.0)
    time.sleep(COOLDOWN)
    pos = read_position(wheel)
    log.event("recentre.failed", pos=-9.0 if pos is None else round(pos, 4))
    return False


def unstick(sink, wheel, pump):
    """
    Move the wheel off a cogging position after a level failed to shift it.

    Returns True if the wheel moved. False means the cut-out has latched, and the motor will
    accept everything and drive nothing until the process restarts.

    CONSTRAINT: rest first, then kick away from the nearest end stop. The rest matters as much
    as the kick, because commanding again immediately is what trips the protection.
    """
    sink.set_force(0.0)
    time.sleep(STALL_REST)

    pos = read_position(wheel)
    direction = -1.0 if (pos is not None and pos > CENTRE_BAND) else 1.0
    moved = force_and_watch(sink, wheel, pump, direction * STALL_KICK, STALL_KICK_SECONDS)
    log.event("unstick", force=round(direction * STALL_KICK, 4),
              start=-9.0 if pos is None else round(pos, 4),
              moved=-1.0 if moved is None else round(moved, 4))
    if moved is None or moved <= MOVED:
        return False
    recentre(sink, wheel, pump)
    return True


def ramp_once(sink, wheel, pump, direction, maximum, step, avoid_stall=True):
    """
    Raise force one step at a time until the wheel moves. Returns (breakaway, travel).

    A level that does not move the wheel is followed by `unstick`, so the next level is
    commanded from a DIFFERENT rotor position rather than pushing again into the same
    cogging detent. That is what tripped the firmware stall protection: see the module
    docstring. It also samples several positions, which is a better measurement of a
    quantity that genuinely varies around the rotation.

    avoid_stall=False restores the old push-again-from-here behaviour, kept so runs made
    before 2026-08-25 stay comparable.
    """
    level = 0.0
    while level < maximum:
        level = round(level + step, 4)
        # A ramp is ~1.8 s per level and prints nothing until it finds something, so a fine
        # --step over a wide --max is minutes of total silence, indistinguishable from a
        # hung script, which is exactly how it was read. Keep it visibly alive.
        sys.stdout.write("\r    trying %.3f ...   " % level)
        sys.stdout.flush()
        moved = force_and_watch(sink, wheel, pump, direction * level, HOLD_WINDOW)
        if moved is None:
            sys.stdout.write("\r" + " " * 30 + "\r")
            return None
        if moved > MOVED:
            sys.stdout.write("\r" + " " * 30 + "\r")
            return (level, moved)

        # Nothing moved. Do not push again from here: that is the second stall that trips
        # the cut-out. Leave the detent first; a kick that fails means it has already
        # latched, and every further level would measure the protection, not the wheel.
        if avoid_stall and level < maximum:
            sys.stdout.write("\r    stuck at %.3f, freeing ... " % level)
            sys.stdout.flush()
            if not unstick(sink, wheel, pump):
                sys.stdout.write("\r" + " " * 34 + "\r")
                return None
    sys.stdout.write("\r" + " " * 34 + "\r")
    return None


def check_engagement(sink, wheel, pump):
    """
    Prove the motor produces torque before measuring, and again after any silent result.

    A silent effect and an immovable wheel are indistinguishable from here, and reporting the
    second when it was the first is how this script produced confident wrong answers.

    CONSTRAINT: push away from the nearest end stop. A fixed direction sends the wheel to full
    lock, and a wheel already at that lock cannot move, which calls a healthy motor dead.
    """
    pos = read_position(wheel)
    direction = 1.0
    if pos is not None and abs(pos) > CENTRE_BAND:
        direction = -1.0 if pos > 0 else 1.0

    moved = force_and_watch(sink, wheel, pump, direction * ENGAGE_FORCE, ENGAGE_SECONDS)
    log.event("engage.check", force=direction * ENGAGE_FORCE,
              start=-9.0 if pos is None else round(pos, 4),
              moved=-1.0 if moved is None else round(moved, 4))
    if moved is None or moved <= MOVED:
        return False
    recentre(sink, wheel, pump)
    return True


def open_once(motor, loop, wheel, pump, args):
    """
    One open attempt, ending in a verdict rather than a guess.

    Returns (sink, verdict) where verdict is one of:
        "torque"     the wheel actually moved. The only success.
        "load"       the effect never loaded or never reached Running.
        "silent"     effect Running, motor enabled, wheel did not move.
        "foreground" the foreground was lost, so nothing was ever going to move.

    Telling these apart is the whole point of the exercise. Every previous theory about this
    failure was inferred from "the wheel did not move", which is all four of these at once.
    """
    sink = LocalMotor(motor, loop, pump, max_force=args.max, gain=args.gain)
    try:
        opened = sink.open()
    except Exception as exc:
        log.event("open.raised", error=repr(exc))
        return (sink, "load")
    if opened is False:
        return (sink, "load")

    state = sink.started_state
    if state != "Running":
        log.event("open.not_running", state=state)
        return (sink, "load")

    try:
        if check_engagement(sink, wheel, pump):
            return (sink, "torque")
    except ForegroundLost:
        return (sink, "foreground")

    if not pump.ensure_foreground():
        return (sink, "foreground")
    return (sink, "silent")


def open_driving_sink(motor, loop, wheel, pump, args):
    """
    Open the motor ONCE and report whether it produced torque. No retry.

    Returns (sink, engaged). The sink comes back either way so the caller can tear it down.

    CONSTRAINT: no retry. A retry that closes and reopens does not recover the failure, it
    causes it, and one bad open becomes a permanently dead motor. Measured, ten probes per
    run:

        one effect held, probed 10 times      10/10 torque, 10/10 torque
        closed and reopened per probe          2/10 torque,  2/10 torque

    The reopen failure is DETERMINISTIC, not flaky: torque on probes 1 and 2, silence from
    probe 3 on, both times. It does not recover inside the process, and a fresh process
    clears it with no power cycle.

    WHAT IT IS NOT. Every WGI observable reports success throughout, including on the silent
    probes: reset ok, enable ok, load Succeeded, state Running, foreground true, and
    `try_unload_effect_async` / `try_reset_async` BOTH ok=True. So the stale-effect theory
    from wheel_profile, the obvious candidate and the one this instrumentation was added to
    test, is disproven. Whatever breaks is invisible from this API.

    The mechanism is still unexplained. The behaviour is not, and it is enough to build on:
    hold one effect and never let go. That is what shim/wgi.c does for a whole game session,
    and what the one clean measurement run in the logs did.
    """
    sink, verdict = open_once(motor, loop, wheel, pump, args)
    log.event("open.attempt", verdict=verdict)
    if verdict == "torque":
        log.event("open.engaged")
        return (sink, True)

    print("  %s" % OPEN_VERDICTS[verdict])
    print("  %s" % describe_state(motor_state(motor, "open failed")))
    if verdict == "foreground" and not require_foreground(pump):
        raise KeyboardInterrupt
    return (sink, False)


def selftest(motor, loop, wheel, pump, args):
    """
    Probe for torque N times and report how often it was there.

    TWO MODES, AND THE DEFAULT IS THE ONE THAT MATTERS.

    Default: ONE open, then N probes against the held effect. That is what a measurement run
    actually does, and what the shim does for the life of a game.

    --reopen: close and reopen for every probe. Kept because it reproduces the bug, scoring
    2/10, and a regression this expensive to rediscover deserves to
    stay one flag away.

    Every fix for this was previously declared on a single run, which is how three wrong
    theories each looked right once. A change that cannot move this fraction is not a fix.
    """
    mode = "reopening every time" if args.reopen else "one open, held"
    rule("SELFTEST: %d torque probes (%s)" % (args.selftest, mode))
    print("  Counts how often the motor actually drives the wheel. LET GO OF THE WHEEL.")
    print()

    tally = {}
    sink = None
    try:
        if not args.reopen:
            sink, verdict = open_once(motor, loop, wheel, pump, args)
            tally[verdict] = 1
            print("  probe  1/%d: %-10s %s"
                  % (args.selftest, verdict,
                     describe_state(motor_state(motor, "probe 1"))))
            log.event("selftest.probe", n=1, verdict=verdict, held=True)
            if verdict != "torque":
                print("  The very first open produced nothing, so the rest measures nothing.")

        start = 1 if args.reopen else 2
        for n in range(start, args.selftest + 1):
            if abort_requested():
                print("  ESC pressed, stopping early.")
                break
            time.sleep(OPEN_RETRY_REST)

            if args.reopen:
                if sink is not None:
                    sink.close()
                    sink = None
                sink, verdict = open_once(motor, loop, wheel, pump, args)
            else:
                # No open, no close. Just ask the held effect for torque again.
                try:
                    verdict = "torque" if check_engagement(sink, wheel, pump) else "silent"
                except ForegroundLost:
                    verdict = "foreground"

            tally[verdict] = tally.get(verdict, 0) + 1
            print("  probe %2d/%d: %-10s %s"
                  % (n, args.selftest, verdict,
                     describe_state(motor_state(motor, "probe %d" % n))))
            log.event("selftest.probe", n=n, verdict=verdict, held=not args.reopen)
    finally:
        if sink is not None:
            sink.close()

    done = sum(tally.values())
    good = tally.get("torque", 0)
    rule("SELFTEST RESULT")
    print("  torque on %d/%d probes   (%s)" % (good, done, mode))
    for verdict, count in sorted(tally.items()):
        if verdict != "torque":
            print("  %-10s %d   %s" % (verdict, count, OPEN_VERDICTS[verdict]))
    log.event("selftest.result", good=good, probes=done, held=not args.reopen)
    print()
    if not done:
        pass
    elif good == done:
        print("  Every probe drove the wheel. Run it once more before believing it.")
    elif args.reopen:
        print("  Expected. Reopening kills this motor after about two releases, which is")
        print("  why the real run opens once. Compare against the default mode.")
    else:
        print("  NOT FIXED. A held effect was supposed to survive this. The verdict column")
        print("  says which failure it was. Do not guess past it.")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max", type=float, default=0.60,
                   help="highest force to try, 0.0-1.0 (default 0.60)")
    p.add_argument("--step", type=float, default=0.02,
                   help="force increment per attempt (default 0.02). MEASURED 2026-08-25: "
                        "on a Hori Force Feedback Racing Wheel DLX, 0.02 completed 9 of 9 "
                        "runs "
                        "while 0.01 completed 1 to 2 of 4 before the motor stalled, and both "
                        "report the first step tried, and a finer step buys risk, not detail. "
                        "Other wheels may take a smaller step safely.")
    p.add_argument("--passes", type=int, default=3,
                   help="measurements per direction (default 3); stiction scatters")
    p.add_argument("--gain", type=float, default=1.0,
                   help="motor master gain (default 1.0: measure the hardware, not a gain)")
    p.add_argument("--wait", type=float, default=20.0, help="device wait timeout")
    p.add_argument("--selftest", type=int, default=0, metavar="N",
                   help="skip measuring; probe for torque N times against one held effect "
                        "and report how many worked. Use this to judge a reliability change.")
    p.add_argument("--reopen", action="store_true",
                   help="with --selftest, close and reopen the motor for every probe. This "
                        "REPRODUCES THE BUG (2/10 on 2026-08-25) and is kept for that.")
    p.add_argument("--stall-floor", type=float, default=0.02,
                   help="smallest --step believed safe on THIS wheel (default 0.02, measured "
                        "on a Hori Force Feedback Racing Wheel DLX). Below it, levels that "
                        "cannot "
                        "move the wheel silence the motor for the rest of the process. "
                        "Raise or lower it for other hardware; it only warns.")
    p.add_argument("--ramp-in-place", action="store_true",
                   help="do not free the wheel between levels; push again from the same "
                        "rotor position. That is what silences the motor, and it is kept "
                        "only so pre-2026-08-25 runs stay comparable.")
    p.add_argument("--no-log", action="store_true")
    args = p.parse_args()

    if not args.no_log:
        log.start(prefix="stiction_test")
    if init_apartment is not None:
        # winrt-runtime 3.x requires the apartment type; 2.x took no argument. Every other
        # WGI entry point in this repo carries this fallback, and this one was missed.
        try:
            init_apartment()
        except TypeError:
            init_apartment(0)

    pump = PumpThread()
    pump.start()
    pump.ready.wait(5.0)

    loop = None
    sink = None
    try:
        rule("stiction_test: the force it takes to move this wheel")
        print("  Ramps force up from zero until the wheel turns, both ways.")
        print("  LET GO OF THE WHEEL and keep this window in front.")
        print("  Press ESC at any time to stop. Ctrl+C cannot reach the console while")
        print("  this holds the foreground.")
        print()

        # A step below what the wheel needs at its stiffest rotor position guarantees stalled
        # levels, and this firmware latches its output off when it sees one. That is hardware
        # behaviour, not something the script can work around. Three attempts to (holding
        # one effect, freeing the wheel between levels, cutting the stall short) each left
        # --step 0.01 finishing 1 to 2 runs in 4, while --step 0.02 finished 4 in 4.
        if args.step < args.stall_floor:
            print("  NOTE: --step %.3f is below --stall-floor %.3f, measured on a Hori"
                  % (args.step, args.stall_floor))
            print("        Force Feedback Racing Wheel DLX. Expect runs to stop early if the")
            print("        motor stalls. On a different wheel this floor may not apply --")
            print("        it also buys no resolution, both steps report the first level.")
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

        if not require_foreground(pump):
            raise KeyboardInterrupt

        if args.selftest > 0:
            return selftest(motor, loop, wheel, pump, args)

        # Never measure through a motor that is not driving.
        rule("Checking the motor actually drives")
        sink, engaged = open_driving_sink(motor, loop, wheel, pump, args)
        if not engaged:
            note = "the motor opened cleanly but produced no torque"
            rule("RESULT")
            print("  NO MEASUREMENT: %s." % note)
            print()
            print("  This is NOT a stiction reading. The verdict above says which failure it")
            print("  was; that is the thing to act on, not the wheel.")
            print()
            print("  RUN IT AGAIN. A fresh process is the only recovery ever observed for")
            print("  this, and it is why nothing is retried in here: releasing and")
            print("  reopening the motor is what kills it, measured at 2/10 on 2026-08-25.")
            print()
            print("  If a second run also fails:")
            print("    * --selftest 10, which turns 'it feels flaky' into a fraction")
            print("    * wgi_probe.py, press 1. If that moves the wheel the fault is here")
            log.event("engage.failed", reason=note)
            return 1
        print("  OK, the motor is driving.")
        log.event("engage.ok")

        rule("Measuring")
        results = {1: [], -1: []}
        died = False
        for direction in (1, -1):
            if died:
                break
            name = "right" if direction > 0 else "left"
            attempt = 0
            while attempt < args.passes:
                # A pass interrupted by losing the foreground measured nothing, so it is
                # retried rather than counted, hence the while loop. Counting it would
                # silently shrink the sample and blame the wheel for Windows.
                try:
                    # Every pass drives the wheel further the same way, so without this the
                    # last passes of a direction are taken against the end stop and measure
                    # nothing.
                    if not recentre(sink, wheel, pump):
                        print("  %-5s pass %d: could not bring the wheel back to centre --"
                              " abandoning" % (name, attempt + 1))
                        died = True
                        break

                    got = ramp_once(sink, wheel, pump, direction, args.max, args.step,
                                    avoid_stall=not args.ramp_in_place)
                except ForegroundLost:
                    sink.set_force(0.0)
                    log.event("foreground.lost", direction=name, pass_no=attempt + 1)
                    if not require_foreground(pump):
                        raise KeyboardInterrupt from None
                    continue

                if got is None:
                    # A pass that saw nothing is ambiguous in exactly the way the check before
                    # the run exists to resolve, except the motor can also die part way
                    # THROUGH, which it did on 2026-08-25 after two good passes. So re-prove
                    # the motor here rather than trusting a check made minutes ago. Without
                    # this the remaining passes report "never moved" and the summary presents
                    # a dead motor as infinite stiction.
                    try:
                        alive = check_engagement(sink, wheel, pump)
                    except ForegroundLost:
                        sink.set_force(0.0)
                        log.event("foreground.lost", direction=name, pass_no=attempt + 1)
                        if not require_foreground(pump):
                            raise KeyboardInterrupt from None
                        continue
                    if not alive:
                        print("  %-5s pass %d: MOTOR STALLED, firmware cut the output"
                              ", abandoning" % (name, attempt + 1))
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
                attempt += 1
                time.sleep(0.4)

        rule("RESULT")
        if died:
            print("  RUN ABANDONED: the motor stalled and its firmware cut the output.")
            print("  Numbers below cover only the passes before that, and any direction")
            print("  reported as 'never moved' after it is NOT a stiction measurement.")
            print()
            print("  DID YOU HEAR TWO SHORT HUMS? That is the cut-out, and it is the only")
            print("  direct evidence there is. Every WGI call keeps reporting healthy.")
            print("  It stays latched for the rest of the process; just run this again.")
            print()
        for direction in (1, -1):
            name = "right" if direction > 0 else "left"
            got = results[direction]
            if got:
                print("  %-5s breakaway: %.3f  (min %.2f, max %.2f, n=%d)"
                      % (name, sum(got) / len(got), min(got), max(got), len(got)))
            elif died:
                # Not "never moved": the motor stopped, so this direction was never measured.
                print("  %-5s breakaway: NOT MEASURED (motor stopped before this)" % name)
            else:
                print("  %-5s breakaway: never moved at or below %.2f" % (name, args.max))

        both = results[1] + results[-1]
        # An abandoned run has no usable number, and printing tuning advice from one is how a
        # dead motor turns into a min_force recommendation. Say nothing rather than something
        # confident and wrong.
        if died:
            print()
            print("  NO min_force RECOMMENDATION FROM THIS RUN: it did not finish.")
        elif both:
            mean = sum(both) / len(both)
            print()
            print("  WHAT THIS MEANS")
            # If nothing ever needed a second step, the ramp never bracketed anything: it
            # reported its own --step. Measured 2026-08-25, --step 0.02 returned exactly 0.02
            # on all 24 passes and --step 0.01 returned 0.01: the same wheel, two answers,
            # each equal to the increment. Calling that a measurement is how 0.02 ended up in
            # GAMES.md as a breakaway force.
            if max(both) <= args.step + 1e-9:
                print("  BREAKAWAY IS BELOW %.2f, SO THIS RUN DID NOT MEASURE IT." % args.step)
                print("  Every pass moved on the FIRST step tried, so all this establishes")
                print("  is an upper bound equal to --step. Re-run with a smaller --step to")
                print("  bracket it, at the cost of more stall risk near a cogging position.")
                print()
                print("  For min_force in tune.json that is good news either way: %.2f is a"
                      % args.step)
                print("  safe ceiling, and this wheel has no meaningful stiction problem.")
            else:
                print("  Any commanded force below about %.2f moves this wheel not at all."
                      % mean)
                print("  A centring force is proportional to steering angle, so it is weakest")
                print("  near centre. If the game's force there is under %.2f, the wheel"
                      % mean)
                print("  will hold position instead of returning, however it is tuned.")
                print()
                print("  min_force in tune.json exists for exactly this: it lifts small")
                print("  non-zero forces to something the motor can express. %.2f is what"
                      % mean)
                print("  this measurement suggests: hardware compensation, not an effect.")
            log.event("stiction.result", mean=round(mean, 4),
                      right=round(sum(results[1]) / len(results[1]), 4) if results[1] else -1,
                      left=round(sum(results[-1]) / len(results[-1]), 4)
                      if results[-1] else -1)
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
