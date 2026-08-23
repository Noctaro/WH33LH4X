"""
motor_sink.py -- the one place that actually touches the wheel's motor.

Everything else in the bridge computes a number: a single signed force for the steering
axis, -1.0 (full one way) to +1.0 (full the other). This module is what turns that number
into torque, and it is deliberately the ONLY module that knows how.

WHY THIS EXISTS AS ITS OWN LAYER
--------------------------------
Windows.Gaming.Input force output is FOREGROUND-GATED. Measured 2026-08-23 with an A->B->A
test: force present at 100% foreground, gone at 0%, back at 100%, nothing else changing.
During real gameplay the GAME owns the foreground, permanently -- so a background bridge
process cannot drive this motor at all. That is not a bug we can fix in Python; it moves the
final write into some other process.

GameInput was the one API that could have dodged it (it has a background focus policy that
WGI lacks) and it is out: both the inbox v0 runtime and the v3 redist report
`forceFeedbackMotorCount = 0` for this GIP wheel, with the struct layout check passing.

So the fix will be an out-of-process arrangement, and the entire rest of the bridge -- effect
decoding, condition rendering, summing, the input feeder -- is completely indifferent to
which one we land on. Putting the motor behind a two-method interface means that change
lands here and nowhere else.

Position READING is not gated and needs no such treatment; the bridge reads the wheel
directly.

SAFETY
------
Every implementation clamps to `max_force` before commanding anything, and `close()` is
expected to leave the motor released. The user runs this hands-on with a wheel strong enough
to whip itself to full lock.
"""

import threading
import time
from datetime import timedelta

import probe_log as log


def clamp(value, limit=1.0):
    """Clamp to +-limit, mapping NaN to 0.0 rather than propagating it into the motor."""
    try:
        if value != value:          # NaN
            return 0.0
    except TypeError:
        return 0.0
    return max(-limit, min(limit, float(value)))


class MotorSink(object):
    """
    Accepts a steering-axis force, -1.0 to +1.0.

    Implementations must tolerate being called at bridge rate (~100 Hz) from one thread,
    and must never raise into the caller: a sink that has failed reports `healthy == False`
    and goes quiet, because a bridge that crashes mid-corner is worse than one that goes
    limp.
    """

    name = "none"

    def __init__(self, max_force=1.0):
        self.max_force = clamp(abs(max_force))
        self._last = None
        self._lock = threading.Lock()
        self.healthy = True
        self.writes = 0
        self.failures = 0

    def set_force(self, x):
        """Command a force. Returns True if it reached the hardware."""
        raise NotImplementedError

    def close(self):
        """Release the motor. Must be safe to call twice."""

    def describe(self):
        return "%s (max force %.2f)" % (self.name, self.max_force)

    def stats(self):
        return {"sink": self.name, "writes": self.writes, "failures": self.failures,
                "healthy": self.healthy}


class NullMotorSink(MotorSink):
    """
    Accepts forces and does nothing with them.

    Not a placeholder -- it is how the rest of the bridge gets developed and tested without
    the wheel attached, and how a run can be made provably harmless. It records the command
    so tests and logs can assert on what WOULD have been sent.
    """

    name = "null"

    def set_force(self, x):
        with self._lock:
            self._last = clamp(x, self.max_force)
            self.writes += 1
        return True

    @property
    def last_force(self):
        return self._last


class WgiMotorSink(MotorSink):
    """
    Windows.Gaming.Input: hold ONE ConstantForceEffect open and rewrite its magnitude.

    This is the technique the whole project rests on. The firmware honours constant force
    and ramp natively and is silent on periodics, so every other effect -- including all four
    conditions -- is synthesised by rewriting this one effect's magnitude at ~100 Hz.
    `set_parameters` does live-update a running effect; that was verified by reversing a push
    mid-flight and watching the wheel turn around.

    Two traps encoded here, both of which fail silently:

      * `master_gain` is LATCHED WHEN THE EFFECT IS LOADED. Setting it under a running
        effect does nothing. So gain is applied before `load_effect_async`, and changing it
        later requires a reload -- `set_gain()` does exactly that.
      * Force output is foreground-gated. This sink does NOT chase the foreground; that is
        the caller's business and, in the real bridge, impossible anyway. It exposes
        `foreground_hint` so the bridge can say WHY the wheel went quiet instead of leaving
        the user guessing.
    """

    name = "wgi"

    def __init__(self, motor, loop, max_force=1.0, gain=1.0, hold_seconds=3600.0):
        super().__init__(max_force)
        self.motor = motor
        self.loop = loop
        self.gain = clamp(abs(gain))
        self.hold_seconds = hold_seconds
        self.effect = None
        self.foreground_hint = None
        self._vector3 = None
        self._timedelta = None
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def open(self):
        """
        Load and start the effect. Separate from __init__ so failure is reportable.

        The effect is created with a very long duration rather than an infinite one: WGI
        has no 'forever', and an effect that expires mid-session would go silent in a way
        that looks exactly like the foreground gate. An hour outlasts any session; the
        bridge reloads if it ever runs longer.
        """
        import winrt.windows.gaming.input.forcefeedback as ff
        from winrt.windows.foundation.numerics import Vector3
        self._vector3 = Vector3
        self._ff = ff

        try:
            self.motor.master_gain = self.gain
        except Exception as exc:
            log.event("sink.gain_failed", error=repr(exc))

        effect = ff.ConstantForceEffect()
        effect.set_parameters(Vector3(0.0, 0.0, 0.0),
                              timedelta(seconds=self.hold_seconds))

        result = self.loop.run_until_complete(self.motor.load_effect_async(effect))
        if result != ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
            self.healthy = False
            log.event("sink.load_failed", result=str(result))
            raise RuntimeError("WGI refused the bridge effect: %s" % result)

        self.effect = effect
        effect.start()
        self._started = True
        log.event("sink.open", sink=self.name, gain=self.gain, max_force=self.max_force)
        return self

    def close(self):
        if self.effect is None:
            return
        effect, self.effect = self.effect, None
        for step in (lambda: effect.set_parameters(self._vector3(0.0, 0.0, 0.0),
                                                   timedelta(seconds=1)),
                     effect.stop,
                     lambda: self.loop.run_until_complete(
                         self.motor.try_unload_effect_async(effect)),
                     lambda: self.loop.run_until_complete(self.motor.try_reset_async())):
            try:
                step()
            except Exception:
                pass
        self._started = False
        log.event("sink.close", sink=self.name, writes=self.writes, failures=self.failures)

    # -- the hot path ------------------------------------------------------

    def set_force(self, x):
        """
        Rewrite the held effect's magnitude. Called ~100x/sec; keep it cheap.

        Identical consecutive values are skipped. That is not just an optimisation: every
        call crosses into WinRT, and a bridge idling at zero force would otherwise spend its
        whole budget telling the driver nothing changed.
        """
        if self.effect is None:
            return False
        value = clamp(x, self.max_force)

        with self._lock:
            if self._last is not None and abs(value - self._last) < 1e-4:
                return True
            try:
                self.effect.set_parameters(self._vector3(value, 0.0, 0.0),
                                           timedelta(seconds=self.hold_seconds))
                self._last = value
                self.writes += 1
                return True
            except Exception as exc:
                self.failures += 1
                # One bad write is not fatal -- the wheel can be mid-reset. A run of them is.
                if self.failures > 50:
                    self.healthy = False
                    log.event("sink.unhealthy", failures=self.failures, error=repr(exc))
                return False

    def set_gain(self, gain):
        """
        Change master gain, which requires reloading because it is latched at load time.

        Returns False if the reload failed, in which case the old effect is already gone --
        the caller should treat the sink as closed.
        """
        self.gain = clamp(abs(gain))
        self.close()
        try:
            self.open()
            return True
        except Exception as exc:
            log.event("sink.regain_failed", error=repr(exc))
            self.healthy = False
            return False

    def describe(self):
        return ("wgi constant-force sink (max force %.2f, master gain %.2f)"
                % (self.max_force, self.gain))


class RateLimiter(object):
    """
    Paces the bridge loop and reports what rate it actually achieved.

    Commanded force is meaningless without knowing how often it was refreshed -- a spring
    updated at 20 Hz feels like notches, not a spring -- so the achieved rate is measured
    rather than assumed to equal the requested one.
    """

    def __init__(self, hz):
        self.period = 1.0 / float(hz)
        self.ticks = 0
        self.started = time.monotonic()
        self._next = self.started

    def wait(self):
        now = time.monotonic()
        self._next += self.period
        if self._next < now:
            # Fell behind: resync rather than accumulate a debt we would sprint to repay.
            self._next = now + self.period
        else:
            time.sleep(max(0.0, self._next - now))
        self.ticks += 1

    @property
    def achieved_hz(self):
        elapsed = time.monotonic() - self.started
        return self.ticks / elapsed if elapsed > 0 else 0.0
