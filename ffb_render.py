"""
ffb_render.py -- force-feedback control laws, in normalised units.

This is the arithmetic half of the bridge: given what a game asked for and what the wheel is
doing, produce one number in -1.0 .. +1.0 for the motor. It touches no hardware, no vJoy and
no WinRT, which is what makes it testable and what makes it survive the bridge's architecture
changing underneath it.

WHY IT EXISTS SEPARATELY
-----------------------
`wheel_profile.run_software_condition` already implements these laws, but with the strengths
baked in as `CONDITION_GAINS` -- values fitted to this wheel for a menu where the user picks
one effect and one magnitude. A game does not work that way: it sends its own coefficients,
saturations and dead bands, several effects at once, and expects them summed. So the laws are
lifted here and parameterised, and `wheel_profile` delegates.

UNITS -- read this before changing anything
-------------------------------------------
Everything here is NORMALISED:

  * position, velocity, acceleration -- reading units and reading units/sec, as
    `RacingWheel.get_current_reading().wheel` produces them (-1.0 .. +1.0 across full lock)
  * coefficients, saturations, dead bands -- fractions, NOT DirectInput's 0..10000 integers
  * output force -- -1.0 .. +1.0

DirectInput's integers are converted once, at the boundary, by `ConditionParams.from_di`.
Keeping DI units out of the interior is deliberate: the alternative is dividing by 10000 in
a dozen places and eventually forgetting one, which is a factor-of-10000 error that presents
as "force feedback does nothing".

Coefficients are NOT clamped to DirectInput's ±1.0-equivalent range, because the software
conditions in `wheel_profile` legitimately use a spring gain of 2.0 -- measured, not guessed.
Saturation still bounds the result, so an out-of-range coefficient only decides how quickly
the effect reaches full force.
"""

import math

# DirectInput expresses coefficients, saturations, offsets and dead bands as integers with
# this full-scale value. It is the only place the number appears.
DI_FULL_SCALE = 10000.0


def clamp(value, limit=1.0):
    if value != value:                      # NaN
        return 0.0
    return max(-limit, min(limit, value))


class WheelState(object):
    """
    Position, velocity and acceleration of the real wheel.

    Differentiating a quantised reading ~100x a second is noisy, and that noise goes straight
    to the motor as audible chatter. Velocity is smoothed before being differentiated again,
    without which inertia is pure hash -- measured while building the software conditions.
    """

    # Exponential smoothing factor for velocity. 1.0 is no smoothing (raw and jittery);
    # lower is smoother but lags the wheel. Fitted against logged ticks from this wheel and
    # moved here from wheel_profile with the laws -- it is the difference between a smooth
    # inertia effect and audible chatter, so do not adjust it casually.
    VELOCITY_SMOOTHING = 0.25

    def __init__(self):
        self.position = 0.0
        self.velocity = 0.0
        self.acceleration = 0.0
        # Unsmoothed, kept only so logs can show what the smoothing actually removed.
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
    """
    One axis of a DirectInput condition effect, in normalised units.

    The four condition kinds differ only in WHICH signal they are fed -- displacement,
    velocity, acceleration, or velocity for friction -- so they share this one parameter set
    and one formula.
    """

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
        The DirectInput condition formula, verbatim in normalised units.

        Outside a dead band centred on `offset`, force is proportional to how far past the
        dead band the signal is, with a separate coefficient and saturation each side. Inside
        it, zero -- which is what stops a friction effect buzzing when the wheel is still.

        The dead band is the FULL width, so each side extends offset +- deadband/2. Getting
        that wrong halves or doubles every dead zone a game asks for.
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


# Which wheel signal each condition kind reacts to. This table IS the difference between the
# four effects; everything else about them is shared.
CONDITION_SIGNAL = {
    "spring": lambda s: s.position,
    "damper": lambda s: s.velocity,
    "inertia": lambda s: s.acceleration,
    "friction": lambda s: s.velocity,
}

CONDITION_HELP = {
    "spring": "pulls back to centre -- force grows with how far off-centre you are",
    "damper": "resists SPEED -- turning fast is heavy, turning slowly is light",
    "friction": "constant drag whenever the wheel moves, at any speed",
    "inertia": "resists ACCELERATION -- heavy to start or stop turning, free once moving",
}


def condition_force(kind, params, state):
    """
    Force for one condition effect, -1.0 .. +1.0.

    NOTE THE SIGN. A condition RESISTS the signal it reacts to, so the force is the negative
    of the formula's output. Expressed this way a positive coefficient always means "resist
    more", which is what a game means by it, and it makes every one of these effects
    physically incapable of running away: force is derived from the motion it opposes, so a
    wrong coefficient makes an effect inert rather than violent. That is not true of the
    firmware's own condition effects, two of which were measured driving the wheel into the
    end stop.
    """
    signal = CONDITION_SIGNAL[kind](state)
    return clamp(-params.force(signal))


def legacy_condition_params(kind, gain, offset=0.0, deadband=0.10):
    """
    Reproduce `wheel_profile.CONDITION_GAINS` behaviour exactly, as ConditionParams.

    The menu's software conditions (`1s`-`4s`) must feel identical after this refactor --
    their gains were fitted to logged ticks from this wheel, so any change in feel is a bug,
    not a tuning opportunity. This is the bridge between that world and the parameterised one.

    Friction is the odd one: the old law was direction-only, a flat +-gain outside a velocity
    dead band, with no proportionality at all. That is expressible here as a very steep
    coefficient saturating immediately -- same output, no special case in the formula.

    One deliberate difference, and only one: AT EXACTLY the dead-band edge the old law is
    already at full drag (its test was `abs(v) < 0.05`, so 0.05 itself is outside), whereas
    this returns 0 there and full drag an epsilon beyond. The transition is 1e-6 wide, the
    function is discontinuous at that point either way, and a velocity landing on exactly
    0.05 has measure zero. Documented rather than special-cased, because a `friction` branch
    in the formula would have to be maintained forever to buy nothing.

    `offset` is the SPRING's centre, and applies to the spring alone. The menu deliberately
    springs back to wherever the wheel was sitting when the effect started rather than to an
    assumed zero, so the caller passes that in.

    It must not leak into the others: damper and friction react to velocity and inertia to
    acceleration, and those have no centre to be offset from. Applying it to all four -- as
    this function first did -- silently biases them, e.g. a damper at rest commanding force
    because its "velocity" was being measured relative to a wheel position. Caught by
    comparing against the original laws over a sweep, which is exactly what that check is for.

    (`ConditionParams.offset` itself stays general: DirectInput lets a game set a centre point
    on any condition, and `from_di` passes whatever it sends.)
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
    One periodic sample. The firmware is silent on every periodic, so these are synthesised
    by rewriting a constant force -- `wgi_probe.synth_wave` proved the technique works.

    A zero period would divide by zero and, more usefully, means "no wave", so it returns the
    offset alone rather than raising into an audio-rate loop.
    """
    if period_s <= 0.0:
        return clamp(offset)
    phase = ((elapsed_s / period_s) + (phase_deg / 360.0)) % 1.0
    return clamp(offset + magnitude * WAVEFORMS[shape](phase))


def envelope_scale(elapsed_s, duration_s, attack_level, attack_time_s,
                   fade_level, fade_time_s):
    """
    DirectInput envelope: ramp in from `attack_level`, hold at 1.0, ramp out to `fade_level`.

    Levels are fractions of the effect's own magnitude. An infinite effect (duration <= 0)
    can attack but can never fade, because there is no end to fade towards -- a fade computed
    against an unknown end time is what makes a held effect mysteriously decay.
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
