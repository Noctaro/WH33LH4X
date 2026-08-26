r"""
test_ffb_render.py -- proves the control laws still do what they used to.

WHY THIS EXISTS
---------------
`ffb_render.py` was extracted from `wheel_profile.run_software_condition` so that a game's
coefficients could drive the same laws. Those laws were fitted against logged ticks from this
wheel, so **any change in what they output is a bug, not a tuning opportunity** -- and the
only other way to check is to have someone hold the wheel and report a feeling, which is
exactly the kind of evidence that produced three wrong verdicts earlier in this project.

It has already earned its keep: the first version of `legacy_condition_params` applied the
spring's centre offset to all four conditions, biasing damper, friction and inertia. Nothing
about that is visible by reading the code, and by feel it would have been "the damper seems
a bit odd".

No test framework -- this is one file with no dependencies:

    .\.venv\Scripts\python.exe test_ffb_render.py
"""

import sys

import ffb_render as R

# Fitted to this wheel; mirrors wheel_profile.CONDITION_GAINS.
GAINS = {"spring": 2.0, "damper": 1.0, "friction": 1.0, "inertia": 0.10}


def legacy(kind, position, centre, velocity, acceleration):
    """The original laws, copied verbatim from wheel_profile.run_software_condition."""
    gain = GAINS[kind]
    if kind == "spring":
        command = -(position - centre) * gain
    elif kind == "damper":
        command = -velocity * gain
    elif kind == "friction":
        command = 0.0 if abs(velocity) < 0.05 else (-gain if velocity > 0 else gain)
    else:
        command = -acceleration * gain
    return max(-1.0, min(1.0, command))


class FakeState(object):
    def __init__(self, position, velocity, acceleration):
        self.position = position
        self.velocity = velocity
        self.acceleration = acceleration


def check(name, condition, detail=""):
    print("  %s %s%s" % ("PASS" if condition else "FAIL", name,
                         "" if condition else "   <-- " + detail))
    return bool(condition)


def test_matches_legacy():
    """Sweep every condition across its full input range against the original law."""
    print("\nlegacy equivalence (the B2 checkpoint, checked numerically)")
    ok = True
    for kind in ("spring", "damper", "friction", "inertia"):
        worst, where = 0.0, None
        for centre in (-0.5, 0.0, 0.25, 0.9):
            params = R.legacy_condition_params(kind, GAINS[kind], offset=centre)
            for i in range(-120, 121):
                x = i / 100.0
                # Friction's dead-band EDGE is a known, documented difference: the old law
                # is already at full drag at exactly +-0.05, the new one an epsilon later.
                # Both are discontinuous there. Skipping the two exact points keeps the rest
                # of the sweep held to exact equality rather than loosening the whole check.
                if kind == "friction" and abs(abs(x) - params.deadband / 2.0) < 1e-12:
                    continue
                # Drive all three signals at once; each kind picks the one it reacts to, so
                # a kind reading the WRONG signal shows up as a mismatch.
                state = FakeState(x, x, x * 10.0)
                new = R.condition_force(kind, params, state)
                old = legacy(kind, x, centre, x, x * 10.0)
                if abs(new - old) > worst:
                    worst, where = abs(new - old), (x, centre, old, new)
        ok &= check("%-9s max deviation %.2e" % (kind, worst), worst < 1e-9,
                    "at x=%+.2f centre=%+.2f: old %+.4f new %+.4f" % where if where else "")
    return ok


def test_friction_is_a_step():
    """
    Friction must be flat drag, not proportional -- that is what distinguishes it from a
    damper. It is built from a very steep coefficient rather than its own branch, so this
    checks the approximation really does behave like a step.
    """
    print("\nfriction is a step, not a ramp")
    params = R.legacy_condition_params("friction", 1.0)
    edge = params.deadband / 2.0
    ok = check("silent at rest",
               R.condition_force("friction", params, FakeState(0, 0.0, 0)) == 0.0)
    ok &= check("silent just inside the dead band",
                R.condition_force("friction", params, FakeState(0, edge * 0.99, 0)) == 0.0)
    just_out = R.condition_force("friction", params, FakeState(0, edge + 1e-4, 0))
    far_out = R.condition_force("friction", params, FakeState(0, 1.0, 0))
    ok &= check("full drag immediately outside (%.3f)" % just_out, abs(just_out + 1.0) < 1e-6)
    ok &= check("no harder when much faster (step, not ramp)",
                abs(just_out - far_out) < 1e-6)
    ok &= check("drag reverses with direction",
                R.condition_force("friction", params, FakeState(0, -1.0, 0)) > 0)
    return ok


def test_di_conversion():
    """DirectInput integers land in normalised units, and the dead band is FULL width."""
    print("\nDirectInput unit conversion")
    p = R.ConditionParams.from_di(0, 5000, 5000, 10000, 10000, 2000)
    ok = check("coeff 5000 -> 0.5", abs(p.pos_coeff - 0.5) < 1e-9)
    ok &= check("saturation 10000 -> 1.0", abs(p.pos_saturation - 1.0) < 1e-9)
    ok &= check("deadband 2000 -> 0.2", abs(p.deadband - 0.2) < 1e-9)
    # Full width means the edge sits at deadband/2, not deadband. Getting this wrong
    # halves or doubles every dead zone a game asks for.
    ok &= check("inside dead band (x=0.10) is silent", p.force(0.10) == 0.0)
    ok &= check("just outside (x=0.11) is tiny", 0.0 < p.force(0.11) < 0.01)
    ok &= check("far out (x=1.00) is coeff*(x-band/2)", abs(p.force(1.00) - 0.45) < 1e-9)
    return ok


def test_saturation_and_sign():
    """Saturation bounds the output, and conditions RESIST rather than assist."""
    print("\nsaturation and sign")
    p = R.ConditionParams(pos_coeff=10.0, neg_coeff=10.0,
                          pos_saturation=0.3, neg_saturation=0.6)
    ok = check("positive side clamps to +0.3", abs(p.force(1.0) - 0.3) < 1e-9)
    ok &= check("negative side clamps to -0.6", abs(p.force(-1.0) + 0.6) < 1e-9)

    # The property that makes software conditions physically unable to run away: force
    # always opposes the signal. The firmware's own conditions do NOT have this -- two of
    # them were measured driving the wheel into the end stop.
    spring = R.legacy_condition_params("spring", 2.0)
    right = R.condition_force("spring", spring, FakeState(0.5, 0.0, 0.0))
    left = R.condition_force("spring", spring, FakeState(-0.5, 0.0, 0.0))
    ok &= check("displaced right -> force pushes left", right < 0)
    ok &= check("displaced left  -> force pushes right", left > 0)

    damper = R.legacy_condition_params("damper", 1.0)
    ok &= check("moving right -> damper opposes",
                R.condition_force("damper", damper, FakeState(0.0, 0.4, 0.0)) < 0)
    ok &= check("at rest -> damper silent",
                R.condition_force("damper", damper, FakeState(0.0, 0.0, 0.0)) == 0.0)
    return ok


def test_periodics():
    """Waveform shapes, and the divide-by-zero a zero period would otherwise cause."""
    print("\nperiodic waveforms")
    ok = check("sine quarter-phase peaks",
               abs(R.periodic_force("sine", 0.5, 0.0, 0.0, 1.0, 0.25) - 0.5) < 1e-9)
    ok &= check("sine half-phase crosses zero",
                abs(R.periodic_force("sine", 0.5, 0.0, 0.0, 1.0, 0.5)) < 1e-9)
    ok &= check("square is +mag then -mag",
                R.periodic_force("square", 0.5, 0.0, 0.0, 1.0, 0.1) == 0.5
                and R.periodic_force("square", 0.5, 0.0, 0.0, 1.0, 0.6) == -0.5)
    ok &= check("offset is honoured",
                abs(R.periodic_force("sine", 0.5, 0.2, 0.0, 1.0, 0.5) - 0.2) < 1e-9)
    ok &= check("zero period returns offset, does not divide by zero",
                R.periodic_force("sine", 0.5, 0.2, 0.0, 0.0, 1.0) == 0.2)
    ok &= check("output stays within +-1 when offset+magnitude would exceed it",
                abs(R.periodic_force("sine", 0.9, 0.8, 0.0, 1.0, 0.25)) <= 1.0)
    return ok


def test_direction_sign():
    """
    Opposite directions must produce opposite signs. THIS IS THE ONE THAT BIT US.

    DiRT 4 sends a positive magnitude and points it with a polar angle, flipping 0 <-> 180
    degrees for left and right. Both are zeros of sin, and the old degeneracy guard returned
    +1.0 for each -- so every force came out in the same direction. The wheel could not centre
    (a centring force must change sign across centre) and pulled permanently to one side.

    It survived a whole session of play because rectification is INAUDIBLE on symmetric
    effects: kerbs and gravel feel right either way. Only directional force reveals it, which
    is why it needs a test rather than a drive.
    """
    print("\ndirection sign (rectification regression)")
    north = R.direction_x(0)                     # 0 degrees
    south = R.direction_x(16384)                 # 180 degrees
    ok = check("0 and 180 degrees are opposite (%+.2f vs %+.2f)" % (north, south),
               north * south < 0)
    ok &= check("neither is zero -- a zero would silence a one-axis wheel",
                abs(north) > 0.05 and abs(south) > 0.05)

    # Cartesian senders land on 8191, where sin is 1.0 and the guard is never reached.
    ok &= check("cartesian +X (8191) is undisturbed",
                abs(R.direction_x(8191) - 1.0) < 0.01)

    # Sign must stay continuous the whole way round, including either side of both zeros.
    ok &= check("1 degree is positive", R.direction_x(91) > 0)
    ok &= check("359 degrees is negative", R.direction_x(32677) < 0)
    ok &= check("181 degrees is negative", R.direction_x(16475) < 0)
    ok &= check("270 degrees is full negative (%+.2f)" % R.direction_x(24576),
                abs(R.direction_x(24576) + 1.0) < 0.01)

    # DiRT 4 measured: it flips between 90 deg (x11780) and 180 deg (x2990) to mean left and
    # right, with unsigned magnitude. Both are non-negative under sin, so a polar reading
    # rectifies this game however carefully the 0/180 boundary is handled.
    try:
        for mode in ("span", "sign"):
            R.DIRECTION_MODE = mode
            left, right = R.direction_x(8191), R.direction_x(16383)
            ok &= check("%s: 90 and 180 deg are opposite (%+.2f vs %+.2f)"
                        % (mode, left, right), left * right < 0)
            ok &= check("%s: 90 deg is positive -- the polarity measured in the car" % mode,
                        left > 0)
            ok &= check("%s: neither end is zero" % mode,
                        abs(left) > 0.5 and abs(right) > 0.5)
            ok &= check("%s: outside the measured range is clamped, not wrapped" % mode,
                        abs(R.direction_x(0)) <= 1.0 and abs(R.direction_x(32767)) <= 1.0)
            rectified = all(R.direction_x(d) >= 0 for d in range(8191, 16384, 64))
            ok &= check("%s is not rectified across 90-180 deg" % mode, not rectified)

        R.DIRECTION_MODE = "span"
        ok &= check("span interpolates: 135 deg is the neutral point (%+.3f)"
                    % R.direction_x(12287), abs(R.direction_x(12287)) < 0.01)
        R.DIRECTION_MODE = "sign"
        ok &= check("sign does NOT null out at 135 deg (%+.2f)" % R.direction_x(12287),
                    abs(R.direction_x(12287)) > 0.5)
    finally:
        # A module-level mode left set would silently change every later test.
        R.DIRECTION_MODE = "sin"

    still_rectified = all(R.direction_x(d) >= 0 for d in range(8191, 16384, 64))
    ok &= check("sin mode over that same range IS rectified -- the bug, pinned down",
                still_rectified)
    return ok


def test_envelope():
    """Attack ramps in, fade ramps out, and an infinite effect never fades."""
    print("\nenvelopes")
    ok = check("attack starts at attack level",
               abs(R.envelope_scale(0.0, 4.0, 0.0, 1.0, 0.0, 1.0)) < 1e-9)
    ok &= check("attack half way", abs(R.envelope_scale(0.5, 4.0, 0.0, 1.0, 0.0, 1.0) - 0.5)
                < 1e-9)
    ok &= check("sustain is full", abs(R.envelope_scale(2.0, 4.0, 0.0, 1.0, 0.0, 1.0) - 1.0)
                < 1e-9)
    ok &= check("fades to fade level at the end",
                abs(R.envelope_scale(4.0, 4.0, 0.0, 1.0, 0.0, 1.0)) < 1e-9)
    # An effect with no end cannot fade towards one; computing a fade against an unknown
    # duration is what makes a held effect mysteriously decay.
    ok &= check("infinite effect never fades",
                abs(R.envelope_scale(9999.0, 0.0, 0.0, 1.0, 0.0, 1.0) - 1.0) < 1e-9)
    return ok


def test_wheel_state():
    """Velocity and acceleration derive sensibly; the first sample cannot divide by zero."""
    print("\nwheel state derivation")
    state = R.WheelState()
    state.update(0.0, 0.0)
    ok = check("first sample yields no velocity", state.velocity == 0.0)
    ok &= check("first sample records position", state.position == 0.0)

    # Constant-rate motion: smoothed velocity should converge towards the true rate.
    for i in range(1, 200):
        state.update(i * 0.01, i * 0.01)      # 1.0 units/sec
    ok &= check("steady motion converges to true velocity (%.3f)" % state.velocity,
                abs(state.velocity - 1.0) < 0.05)
    ok &= check("steady motion settles acceleration near zero (%.3f)" % state.acceleration,
                abs(state.acceleration) < 0.05)

    same = R.WheelState()
    same.update(0.5, 1.0)
    same.update(0.5, 1.0)                     # identical timestamp
    ok &= check("repeated timestamp does not divide by zero", True)
    return ok


def main():
    print("ffb_render control-law checks")
    results = [test_matches_legacy(), test_friction_is_a_step(), test_di_conversion(),
               test_saturation_and_sign(), test_periodics(), test_direction_sign(),
               test_envelope(), test_wheel_state()]
    print()
    if all(results):
        print("ALL CHECKS PASSED")
        return 0
    print(">>> FAILURES ABOVE -- the laws changed behaviour. Do not ship this.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
