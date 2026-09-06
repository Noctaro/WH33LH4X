"""
motor_sink.py: the one place that touches the wheel's motor.

Everything else computes a number, a single signed force for the steering axis, -1.0 to +1.0.
This module turns that number into torque and is the only module that knows how.

CONSTRAINT: WGI force output and position reads are both foreground gated, so a background
process cannot drive this motor. IpcMotorSink is the answer: a dinput8.dll proxy loads inside
the game, calls WGI from there, and takes its force from a shared section. See
docs/hardware.md#the-foreground-owns-the-motor.

CONSTRAINT: every implementation clamps to max_force before commanding anything, and close()
leaves the motor released. This runs hands-on with a wheel strong enough to whip itself to
full lock.
"""

import ctypes
import mmap
import struct
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

    CONSTRAINT: never raise into the caller. A failed sink reports healthy == False and goes
    quiet, because a bridge that crashes mid-corner is worse than one that goes limp. Called
    at bridge rate, about 100 Hz, from one thread.
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

    def keepalive(self):
        """
        Say "still here" without commanding anything. Called every tick, unconditionally.

        CONSTRAINT: not optional for the IPC sink. The shim watches this heartbeat, and
        stamping it only in set_force made a menu or loading screen look like the bridge
        dying, so the shim released and re-claimed the motor until it went silent. See
        docs/hardware.md#the-foreground-owns-the-motor.
        """

    def close(self):
        """Release the motor. Must be safe to call twice."""

    def describe(self):
        return "%s (max force %.2f)" % (self.name, self.max_force)


class WgiMotorSink(MotorSink):
    """
    Windows.Gaming.Input: hold one ConstantForceEffect open and rewrite its magnitude.

    The firmware honours constant force and ramp natively and is silent on periodics, so every
    other effect is synthesised by rewriting this one effect's magnitude at about 100 Hz. See
    docs/hardware.md#effect-support.

    CONSTRAINT: master_gain is latched when the effect loads, so it is applied before
    load_effect_async and there is no set_gain. Changing gain means reloading, and reloading
    silences this motor after about two cycles.

    CONSTRAINT: this sink does not chase the foreground. It exposes foreground_hint so the
    bridge can say why the wheel went quiet.
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

        WGI has no infinite duration, so the effect is created with an hour, which outlasts
        any session. An effect that expired mid-session would look exactly like the
        foreground gate.
        """
        import winrt.windows.gaming.input.forcefeedback as ff
        from winrt.windows.foundation.numerics import Vector3
        self._vector3 = Vector3
        self._ff = ff

        # CONSTRAINT: reset and enable before loading, always. A motor that was held and
        # released, or that lost the foreground while holding an effect, accepts effects and
        # produces no torque: load succeeds, state reads Running, the wheel does not move.
        # See docs/hardware.md#the-foreground-owns-the-motor.
        for name, call in (("reset", lambda: self.motor.try_reset_async()),
                           ("enable", lambda: self.motor.try_enable_async())):
            try:
                log.event("sink.%s" % name, ok=self.loop.run_until_complete(call()))
            except Exception as exc:
                log.event("sink.%s_failed" % name, error=repr(exc))
        try:
            if self.motor.are_effects_paused:
                self.motor.resume_all_effects()
                log.event("sink.resumed")
        except Exception as exc:
            log.event("sink.resume_failed", error=repr(exc))

        try:
            self.motor.master_gain = self.gain
        except Exception as exc:
            log.event("sink.gain_failed", error=repr(exc))

        # Loaded at ZERO and rewritten afterwards. This is the order shim/wgi.c uses, and
        # the order that drives DiRT 4 for hours.
        #
        # It is NOT a recovery mechanism. Once this motor goes silent, nothing inside the
        # process brings it back: measured 2026-08-26 with evidence/revive_test.py, a
        # fresh effect loaded at zero then commanded to 0.30 revived it 1 time in 5, a
        # fresh effect loaded already carrying 0.30 revived it 0 times in 2, and simply
        # waiting revived it 0 times in 2. The single success is best read as the
        # spontaneous recovery seen elsewhere in those runs. A NEW PROCESS always works.
        #
        # This used to be an `initial_force` knob, kept because wgi_probe pre-charges its
        # effect and looked the more reliable of the two. That reading is contradicted,
        # and no caller ever set it.
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
        self._last = 0.0
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
                # One bad write is not fatal, the wheel can be mid-reset. A run of them is.
                if self.failures > 50:
                    self.healthy = False
                    log.event("sink.unhealthy", failures=self.failures, error=repr(exc))
                return False

    def describe(self):
        return ("wgi constant-force sink (max force %.2f, master gain %.2f)"
                % (self.max_force, self.gain))


class _ShimReading(object):
    """
    A wheel reading that came over the shared section instead of from WGI directly.

    Field names match `RacingWheelReading` so `WheelReader` and the axis map can consume
    either without a special case.
    """

    __slots__ = ("wheel", "throttle", "brake", "clutch", "handbrake", "buttons")

    def __init__(self, wheel, throttle, brake, clutch, handbrake, buttons=0):
        self.wheel = wheel
        self.throttle = throttle
        self.brake = brake
        self.clutch = clutch
        self.handbrake = handbrake
        # Not fed to vJoy: the game has its own path to these buttons. This copy lets the
        # bridge be driven from the wheel while a game owns the foreground and the keyboard.
        self.buttons = buttons


class IpcMotorSink(MotorSink):
    """
    Publish force to the shim running inside the game, which applies it through WGI.

    The sink that works during gameplay. WgiMotorSink drives the motor from this process,
    which only produces torque while this window is in front: fine for diagnostics, useless
    with a game running. The shim runs inside the game, so foreground is satisfied.

    CONSTRAINT: the wire format is a fixed 48-byte shared section defined in shim/ipc.h.
    Change one side and both must change, or the shim turns garbage floats into torque.

    CONSTRAINT: liveness runs both ways. set_force(0.0) and a crashed bridge are the same
    float and must behave differently, so each side stamps a heartbeat the other checks.
    """

    name = "ipc"

    # off 0 magic, 4 version, 8 force, 12 gain, 16 bridge_tick, 24 shim_tick, 32 state,
    # 36 has_reading, 40 wheel, 44 throttle, 48 brake, 52 clutch, 56 handbrake, 60 buttons,
    # 64 shifter_gear, 68 reserved.  Must match shim/ipc.h exactly.
    _STRUCT = struct.Struct("<IIffQQIIfffffIiI")
    _NAME = "Local\\WH33LH4X_bridge_v2"
    _MAGIC = 0x33334857
    _VERSION = 2
    _STALE_MS = 500

    # has_reading, wheel/throttle/brake/clutch/handbrake, then the button bitfield. The shim
    # has always published buttons here; nothing unpacked them until tuning needed a control
    # surface that works while a game holds the foreground and the keyboard.
    _READING_OFF = 36
    _READING = struct.Struct("<IfffffI")

    STATE_NONE, STATE_MOTOR, STATE_ACTIVE = 0, 1, 2

    def __init__(self, max_force=1.0, gain=1.0):
        super().__init__(max_force)
        self.gain = clamp(abs(gain))
        self._mm = None
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.GetTickCount64.restype = ctypes.c_ulonglong

    def open(self):
        size = self._STRUCT.size
        # tagname makes this a named section, which is the point: the shim opens the same
        # name from inside the game. Whoever gets there first creates it.
        self._mm = mmap.mmap(-1, size, tagname=self._NAME)
        magic, version = struct.unpack_from("<II", self._mm, 0)
        if magic != self._MAGIC or version != self._VERSION:
            # Either fresh (zeroed) or left over from an incompatible build. Stamping the
            # header is safe in both cases; refusing to run because a dead process left a
            # stale section behind would be worse.
            struct.pack_into("<II", self._mm, 0, self._MAGIC, self._VERSION)
        struct.pack_into("<f", self._mm, 12, float(self.gain))
        self._beat()
        log.event("sink.open", sink=self.name, gain=self.gain, max_force=self.max_force)
        return self

    def _beat(self):
        struct.pack_into("<Q", self._mm, 16, self._kernel32.GetTickCount64())

    def keepalive(self):
        """Stamp the heartbeat alone. See MotorSink.keepalive for why this must never lapse."""
        if self._mm is None:
            return False
        with self._lock:
            self._beat()
        return True

    def set_force(self, x):
        """
        Publish the force and stamp the heartbeat. Called ~100x/sec; keep it cheap.

        Unlike WgiMotorSink this does NOT skip repeated values: the write is two stores into
        mapped memory, and skipping would also skip the heartbeat, which the shim reads as the
        bridge having died. Cheaper to write it than to reason about it.
        """
        if self._mm is None:
            return False
        value = clamp(x, self.max_force)
        with self._lock:
            struct.pack_into("<f", self._mm, 8, value)
            self._beat()
            self._last = value
            self.writes += 1
        return True

    def read(self):
        """
        The wheel reading the shim published, or None.

        Position reading is foreground gated exactly like force output, so a background
        bridge reads zeros while a game is in front, vJoy's axes never move, and the game
        cannot bind steering. The input half is a precondition for the output half.

        Returns an object with .wheel/.throttle/.brake/.clutch/.handbrake so it can stand in
        for a WGI RacingWheelReading without the caller caring which one it got.
        """
        if self._mm is None:
            return None
        _state, alive = self.shim_state()
        if not alive:
            return None
        have, wheel, throttle, brake, clutch, handbrake, buttons = \
            self._READING.unpack_from(self._mm, self._READING_OFF)
        if not have:
            return None
        return _ShimReading(wheel, throttle, brake, clutch, handbrake, buttons)

    def shim_state(self):
        """(state, alive) as last published by the shim. For reporting, not control flow."""
        if self._mm is None:
            return (self.STATE_NONE, False)
        shim_tick, state = struct.unpack_from("<QI", self._mm, 24)
        alive = bool(shim_tick) and \
            (self._kernel32.GetTickCount64() - shim_tick) < self._STALE_MS
        return (state, alive)

    def describe(self):
        state, alive = self.shim_state()
        names = {self.STATE_NONE: "no motor", self.STATE_MOTOR: "motor idle",
                 self.STATE_ACTIVE: "driving"}
        return "%s (max force %.2f, shim %s%s)" % (
            self.name, self.max_force, names.get(state, "?"),
            "" if alive else ", NOT RESPONDING")

    def close(self):
        if self._mm is None:
            return
        try:
            struct.pack_into("<f", self._mm, 8, 0.0)
            # Zero the heartbeat rather than letting it go stale: the shim then releases the
            # motor immediately instead of holding it for the staleness window.
            struct.pack_into("<Q", self._mm, 16, 0)
        except Exception:
            pass
        self._mm.close()
        self._mm = None
        log.event("sink.close", sink=self.name, writes=self.writes, failures=self.failures)


class RateLimiter(object):
    """
    Paces the bridge loop and reports what rate it actually achieved.

    The achieved rate is measured rather than assumed: a spring updated at 20 Hz feels like
    notches, not a spring.
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
