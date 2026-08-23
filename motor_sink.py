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

The fix landed as `IpcMotorSink`: a `dinput8.dll` proxy loads inside the game, calls WGI from
there -- where foreground is satisfied by definition -- and takes its commanded force from a
shared section. Confirmed on hardware 2026-08-23, force felt in Dirt 4. The entire rest of the
bridge -- effect decoding, condition rendering, summing, the input feeder -- never had to know.
That is what the two-method interface bought.

POSITION READING IS ALSO GATED. An earlier version of this note said it was not, on the
strength of a measurement that could not tell tracking from values gathered across focus
transitions; the same data showed 65% of background samples at exactly 0.000 against 0% while
foregrounded. So feeding vJoy's axes from a background process does not work either, and that
half is still open -- see the plan. Do not build on the assumption that reading is free.

SAFETY
------
Every implementation clamps to `max_force` before commanding anything, and `close()` is
expected to leave the motor released. The user runs this hands-on with a wheel strong enough
to whip itself to full lock.
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

    def keepalive(self):
        """
        Say "still here" without commanding anything. Called every tick, unconditionally.

        Only the IPC sink has anything to do here, and for it this is not optional. The shim
        watches a heartbeat to decide whether we are alive, and it stamps that heartbeat in
        `set_force` -- which the bridge only reaches when a wheel reading arrived. A menu, a
        loading screen, or any pause in readings therefore looked like the bridge dying, and
        the shim RELEASED THE MOTOR and re-claimed it on the next reading.

        That churn is what makes the wheel go silent: claiming and releasing this motor
        repeatedly leaves it accepting effects and producing no torque, with the effect still
        reporting Running. Correct force, loaded effect, dead wheel -- and nothing in any log
        looks wrong. See the WGI notes in .claude/memory.
        """

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
        # Not part of RacingWheelReading's axis set, and deliberately not fed to vJoy -- the
        # game already has its own path to these buttons. This copy exists so the bridge can be
        # driven from the wheel while a game owns the foreground and every keystroke with it.
        self.buttons = buttons


class IpcMotorSink(MotorSink):
    """
    Publish force to the shim running inside the game, which applies it through WGI.

    This is the sink that actually works during gameplay. `WgiMotorSink` drives the motor from
    THIS process, which only produces torque while our own window is in front -- fine for the
    diagnostics, useless with a game running. The shim is in the game's process, so foreground
    is satisfied by definition.

    The wire format is a fixed 48-byte shared section, defined in shim/ipc.h. It is frozen:
    change one side and you change both, or the shim reads garbage floats and turns them
    straight into torque. `_STRUCT` below and the C struct describe the same bytes.

    Liveness runs both ways, and it has to. `set_force(0.0)` and "the bridge crashed" are the
    same float, and they must behave differently -- the first is obeyed, the second has to make
    the shim let go of the motor. So each side stamps a heartbeat the other checks.
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
        # tagname makes this a NAMED section rather than an anonymous one, which is the whole
        # point -- the shim opens the same name from inside the game. Whoever gets there first
        # creates it; the other attaches.
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

    def set_gain(self, gain):
        """Gain is latched by the shim when it loads the effect, so this takes effect then."""
        self.gain = clamp(abs(gain))
        if self._mm is not None:
            struct.pack_into("<f", self._mm, 12, float(self.gain))
        return True

    def read(self):
        """
        The wheel reading the shim published, or None.

        This exists because position reading is foreground-gated exactly like force output.
        A background bridge reads zeros while a game is in front, so vJoy's axes never move,
        so the game cannot bind steering -- and a game that cannot bind steering never sends
        force feedback either. The input half is a precondition for the output half, not a
        convenience.

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
        """(state, alive) as last published by the shim -- for reporting, not control flow."""
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
