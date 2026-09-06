"""
ffb_render.py: force feedback control laws, in normalised units.

Given what a game asked for and what the wheel is doing, produce one number in -1.0 .. +1.0
for the motor. Touches no hardware, no vJoy and no WinRT.

CONSTRAINT: everything here is normalised. DirectInput 0..10000 integers are converted once,
at the boundary, by ConditionParams.from_di. See docs/development.md#ffb_render for the unit
table, why this module is separate, and why coefficients are not clamped.
"""

import math

# DirectInput full scale for coefficients, saturations, offsets and dead bands.
DI_FULL_SCALE = 10000.0


def clamp(value, limit=1.0):
    if value != value:                      # NaN
        return 0.0
    return max(-limit, min(limit, value))


class WheelState(object):
    """Position, velocity and acceleration of the real wheel, with velocity smoothed."""

    # CONSTRAINT: fitted to logged ticks from this wheel. 1.0 is no smoothing and makes
    # inertia audibly chatter; lower is smoother but lags. See docs/development.md#ffb_render.
    VELOCITY_SMOOTHING = 0.25

    def __init__(self):
        self.position = 0.0
        self.velocity = 0.0
        self.acceleration = 0.0
        # Unsmoothed, kept so logs can show what the smoothing removed.
        self.raw_velocity = 0.0
        self._last_position = None
        self._last_time = None

    def update(self, position, now):
        """Feed one sample. Returns self so callers can chain."""
        if position is None:
            return self
        if self._last_time is None:
            self.position = position
            self._last_position, self._last_time = position, now
            return self

        dt = now - self._last_time
        if dt <= 0.0:
            return self

        self.raw_velocity = (position - self._last_position) / dt
        smoothed = (self.velocity
                    + self.VELOCITY_SMOOTHING * (self.raw_velocity - self.velocity))
        self.acceleration = (smoothed - self.velocity) / dt
        self.velocity = smoothed
        self.position = position
        self._last_position, self._last_time = position, now
        return self


class ConditionParams(object):
    """One axis of a DirectInput condition effect, in normalised units."""

    __slots__ = ("offset", "pos_coeff", "neg_coeff", "pos_saturation",
                 "neg_saturation", "deadband")

    def __init__(self, offset=0.0, pos_coeff=0.0, neg_coeff=0.0,
                 pos_saturation=1.0, neg_saturation=1.0, deadband=0.0):
        self.offset = offset
        self.pos_coeff = pos_coeff
        self.neg_coeff = neg_coeff
        self.pos_saturation = pos_saturation
        self.neg_saturation = neg_saturation
        self.deadband = deadband

    @classmethod
    def from_di(cls, offset, pos_coeff, neg_coeff, pos_saturation, neg_saturation,
                deadband):
        """Convert one DirectInput DICONDITION (0..10000 integers) into normalised units."""
        return cls(offset=offset / DI_FULL_SCALE,
                   pos_coeff=pos_coeff / DI_FULL_SCALE,
                   neg_coeff=neg_coeff / DI_FULL_SCALE,
                   pos_saturation=abs(pos_saturation) / DI_FULL_SCALE,
                   neg_saturation=abs(neg_saturation) / DI_FULL_SCALE,
                   deadband=abs(deadband) / DI_FULL_SCALE)

    def force(self, x):
        """
        The DirectInput condition formula, in normalised units.

        CONSTRAINT: deadband is the full width, so each side extends offset +- deadband/2.
        Getting it wrong halves or doubles every dead zone a game asks for.
        """
        delta = x - self.offset
        half_band = self.deadband / 2.0

        if delta > half_band:
            force = self.pos_coeff * (delta - half_band)
            return clamp(force, self.pos_saturation)
        if delta < -half_band:
            force = self.neg_coeff * (delta + half_band)
            return clamp(force, self.neg_saturation)
        return 0.0

    def __repr__(self):
        return ("ConditionParams(offset=%+.3f coeff=%+.3f/%+.3f sat=%.3f/%.3f band=%.3f)"
                % (self.offset, self.pos_coeff, self.neg_coeff,
                   self.pos_saturation, self.neg_saturation, self.deadband))


# Which wheel signal each condition kind reacts to. The only difference between the four.
CONDITION_SIGNAL = {
    "spring": lambda s: s.position,
    "damper": lambda s: s.velocity,
    "inertia": lambda s: s.acceleration,
    "friction": lambda s: s.velocity,
}

CONDITION_HELP = {
    "spring": "pulls back to centre: force grows with how far off-centre you are",
    "damper": "resists speed: turning fast is heavy, turning slowly is light",
    "friction": "constant drag whenever the wheel moves, at any speed",
    "inertia": "resists acceleration: heavy to start or stop turning, free once moving",
}


def condition_force(kind, params, state):
    """
    Force for one condition effect, -1.0 .. +1.0.

    CONSTRAINT: the formula output is negated, because a condition resists the signal it
    reacts to. Force derived from the motion it opposes cannot run away, so a wrong
    coefficient makes an effect inert rather than violent. See docs/hardware.md.
    """
    signal = CONDITION_SIGNAL[kind](state)
    return clamp(-params.force(signal))


def legacy_condition_params(kind, gain, offset=0.0, deadband=0.10):
    """
    Reproduce wheel_profile.CONDITION_GAINS behaviour exactly, as ConditionParams.

    CONSTRAINT: offset is the spring's centre and applies to the spring alone. Damper,
    friction and inertia react to velocity or acceleration, which have no centre, and
    offsetting those biases them. See docs/development.md#ffb_render.
    """
    centre = offset if kind == "spring" else 0.0
    if kind == "friction":
        return ConditionParams(offset=centre, pos_coeff=1e6, neg_coeff=1e6,
                               pos_saturation=gain, neg_saturation=gain,
                               deadband=deadband)
    return ConditionParams(offset=centre, pos_coeff=gain, neg_coeff=gain,
                           pos_saturation=1.0, neg_saturation=1.0,
                           deadband=0.0)


# ---------------------------------------------------------------------------
# Periodic waveforms
# ---------------------------------------------------------------------------

def _sawtooth_up(phase):
    return 2.0 * phase - 1.0


def _sawtooth_down(phase):
    return 1.0 - 2.0 * phase


WAVEFORMS = {
    "sine": lambda phase: math.sin(2.0 * math.pi * phase),
    "square": lambda phase: 1.0 if phase < 0.5 else -1.0,
    "triangle": lambda phase: 4.0 * abs(phase - 0.5) - 1.0,
    "sawtoothup": _sawtooth_up,
    "sawtoothdown": _sawtooth_down,
}


def periodic_force(shape, magnitude, offset, phase_deg, period_s, elapsed_s):
    """
    One periodic sample, synthesised by rewriting a constant force: the firmware is silent on
    every periodic. See docs/hardware.md. A zero period means no wave and returns the offset.
    """
    if period_s <= 0.0:
        return clamp(offset)
    phase = ((elapsed_s / period_s) + (phase_deg / 360.0)) % 1.0
    return clamp(offset + magnitude * WAVEFORMS[shape](phase))


# How a game's direction field is read: "sin", "span" or "sign". Set live from tune.json.
# See docs/tuning.md#how-dir_mode-reads-a-games-direction-field.
DIRECTION_MODE = "sin"

# MEASURED: the span DiRT 4 uses, dirx 8191 (90 deg) to 16383 (180 deg).
# See docs/tuning.md#how-dir_mode-reads-a-games-direction-field.
SPAN_LOW = 8191.0
SPAN_HIGH = 16383.0
SPAN_CENTRE = (SPAN_LOW + SPAN_HIGH) / 2.0        # 12287
SPAN_HALF = (SPAN_HIGH - SPAN_LOW) / 2.0          # 4096


DURATION_INFINITE = 0xFFFF


def duration_seconds(raw):
    """HID PID duration to seconds. 0xFFFF is the infinite sentinel, not 65.535 s."""
    return 0.0 if raw >= DURATION_INFINITE else raw / 1000.0


def direction_x(dir_raw):
    """
    X-axis component of a HID PID direction field, as a multiplier in -1.0 .. +1.0.

    CONSTRAINT: never return 0, and resolve the two sine zeros by which half of the circle
    the angle is in. A game may encode left and right as a polar flip rather than in the
    magnitude, and rectifying that removes centring entirely. See docs/tuning.md.
    """
    if DIRECTION_MODE == "span":
        # MEASURED polarity: the other sign correlated +0.365 with steering angle, pushing
        # deeper into the turn. Clamped because a game may send outside the measured range.
        return clamp((SPAN_CENTRE - dir_raw) / SPAN_HALF)
    if DIRECTION_MODE == "sign":
        # No interpolation, so no force null at 135 degrees where "span" drops out.
        return -1.0 if dir_raw > SPAN_CENTRE else 1.0

    turn = (dir_raw % 32768) / 32768.0          # 0.0 .. 1.0 of a full circle
    x = math.sin(2.0 * math.pi * turn)
    if abs(x) < 0.05:
        return -1.0 if turn >= 0.5 else 1.0
    return x


def envelope_scale(elapsed_s, duration_s, attack_level, attack_time_s,
                   fade_level, fade_time_s):
    """
    DirectInput envelope: ramp in from attack_level, hold at 1.0, ramp out to fade_level.

    Levels are fractions of the effect's magnitude. An infinite effect (duration <= 0) attacks
    but never fades, because there is no end to fade towards.
    """
    scale = 1.0
    if attack_time_s > 0.0 and elapsed_s < attack_time_s:
        progress = elapsed_s / attack_time_s
        scale = attack_level + (1.0 - attack_level) * progress
    if duration_s > 0.0 and fade_time_s > 0.0:
        fade_start = duration_s - fade_time_s
        if elapsed_s > fade_start:
            progress = min(1.0, (elapsed_s - fade_start) / fade_time_s)
            scale = min(scale, 1.0 + (fade_level - 1.0) * progress)
    return scale


# ---------------------------------------------------------------------------
# The effect model: what a game asked for, and what it is worth right now
# ---------------------------------------------------------------------------

CONDITION_KINDS = ("spring", "damper", "inertia", "friction")
PERIODIC_KINDS = tuple(WAVEFORMS)


class Effect(object):
    """
    One effect a game created, as it currently stands.

    CONSTRAINT: mutable, patched in place by each packet. A game builds an effect across
    several reports and may update one later without resending the others, so rebuilding per
    packet loses values set once at load time, such as gain.
    """

    def __init__(self, block):
        self.block = block
        self.kind = None                # 'constant' | 'ramp' | a periodic | a condition
        self.gain = 1.0                 # 0..1, the effect's own gain
        self.duration = 0.0             # seconds; 0 means infinite
        self.start_delay = 0.0
        self.direction = 1.0            # X multiplier, see direction_x()

        self.magnitude = 0.0            # constant force, signed
        self.ramp_start = 0.0
        self.ramp_end = 0.0

        self.periodic_magnitude = 0.0
        self.periodic_offset = 0.0
        self.periodic_phase_deg = 0.0
        self.periodic_period = 0.0      # seconds

        # One condition block per axis. A wheel has one, and the first is the one that steers.
        self.conditions = {}            # axis index -> ConditionParams

        self.attack_level = 1.0
        self.attack_time = 0.0
        self.fade_level = 1.0
        self.fade_time = 0.0

        self.running = False
        self.started_at = 0.0
        self.loop_count = 1

    # -- state ------------------------------------------------------------

    def start(self, now, loop_count=1):
        self.running = True
        self.started_at = now
        self.loop_count = max(1, loop_count)

    def stop(self):
        self.running = False

    def elapsed(self, now):
        return max(0.0, now - self.started_at - self.start_delay)

    def expired(self, now):
        """
        True once a finite effect has played out its duration and loops.

        A game need not send a stop for an effect that ran out, so an effect that never
        expires here keeps commanding force forever.
        """
        if not self.running or self.duration <= 0.0:
            return False
        return now - self.started_at - self.start_delay > self.duration * self.loop_count

    # -- output -----------------------------------------------------------

    def force(self, now, state):
        """This effect's contribution, -1.0 .. +1.0, before device gain."""
        if not self.running or self.kind is None:
            return 0.0
        elapsed = self.elapsed(now)
        if now - self.started_at < self.start_delay:
            return 0.0

        if self.kind == "constant":
            value = self.magnitude
        elif self.kind == "ramp":
            # With no duration there is nothing to interpolate over, so hold at the start.
            if self.duration > 0.0:
                progress = min(1.0, elapsed / self.duration)
            else:
                progress = 0.0
            value = self.ramp_start + (self.ramp_end - self.ramp_start) * progress
        elif self.kind in PERIODIC_KINDS:
            value = periodic_force(self.kind, self.periodic_magnitude,
                                   self.periodic_offset, self.periodic_phase_deg,
                                   self.periodic_period, elapsed)
        elif self.kind in CONDITION_KINDS:
            params = self.conditions.get(0)
            if params is None:
                return 0.0
            # CONSTRAINT: no envelope on conditions. A condition is a continuous response,
            # and fading a spring would make the wheel go slack mid-corner.
            return clamp(condition_force(self.kind, params, state)
                         * self.gain * self.direction)
        else:
            return 0.0

        envelope = envelope_scale(elapsed, self.duration, self.attack_level,
                                  self.attack_time, self.fade_level, self.fade_time)
        return clamp(value * envelope * self.gain * self.direction)

    def describe(self):
        return "blk%-3d %-12s gain %.2f %s" % (
            self.block, self.kind or "(undefined)", self.gain,
            "running" if self.running else "stopped")


class EffectMixer(object):
    """
    Every effect the game has loaded, and their sum.

    CONSTRAINT: needs vJoy 2.2.0 or newer. Older builds report effect block index 1 for
    everything, and this dictionary collapses to one entry. See docs/vjoy.md.

    Every effect renders in software and sums into one command, including conditions the
    firmware could run natively: the motor is driven through one held constant force effect,
    and a firmware condition alongside it would add torque nothing here can account for.
    """

    def __init__(self):
        self.effects = {}
        self.device_gain = 1.0          # the game's device-wide gain, 0..1
        self.paused = False
        self.actuators_enabled = True

    def get(self, block):
        """The effect for this block, created on first mention."""
        effect = self.effects.get(block)
        if effect is None:
            effect = Effect(block)
            self.effects[block] = effect
        return effect

    def free(self, block):
        self.effects.pop(block, None)

    def reset(self):
        """Device Reset: everything forgotten, actuators back on, not paused."""
        self.effects.clear()
        self.paused = False
        self.actuators_enabled = True
        self.device_gain = 1.0

    def stop_all(self):
        for effect in self.effects.values():
            effect.stop()

    def force(self, now, state):
        """
        Sum of every running effect, clamped once at the end.

        CONSTRAINT: clamp once, not per effect. Three effects at 0.5 saturate to 1.0 together
        rather than being flattened to 0.5 each and summed to 1.5.
        """
        if self.paused or not self.actuators_enabled:
            return 0.0
        total = 0.0
        for effect in list(self.effects.values()):
            if effect.expired(now):
                effect.stop()
                continue
            total += effect.force(now, state)
        return clamp(total * self.device_gain)

    def running_effects(self):
        return [e for e in self.effects.values() if e.running]
