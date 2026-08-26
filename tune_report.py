r"""
tune_report.py -- read a bridge session log and say what the force feedback actually did.

WHY THIS EXISTS
---------------
Feel is the goal, but feel is a terrible instrument for finding faults. A rectified output --
every force pointing the same way, no matter which way the game meant it -- survived a full
session of driving described as "bumps and gravel feel ok", because rectification is inaudible
on symmetric effects. It took thirty seconds of arithmetic on a log to see it.

So after every drive, run this. It answers the questions that feel cannot:

  * does the output ever go NEGATIVE? (if not, the wheel cannot centre, whatever it feels like)
  * does force OPPOSE steering angle? (that relationship IS self-aligning torque)
  * how much of the time are we clipping, or sitting under the motor's stiction?
  * which effects did the game actually send, and how did it point them?

Usage:
    .\.venv\Scripts\python.exe tune_report.py                 # newest bridge log
    .\.venv\Scripts\python.exe tune_report.py logs\some.log
"""

import glob
import os
import re
import sys

# The force below which this wheel does not move at all, so anything under it is commanded and
# then silently does nothing. MEASURED on a Hori Force Feedback Racing Wheel DLX (VID 0x0F0D,
# Xbox mode) with stiction_test.py on 2026-08-25 -- ANOTHER WHEEL NEEDS ITS OWN RUN. This is
# not a
# constant of the API or of racing wheels in general.
#
# Every pass broke away on the FIRST step tried, at --step 0.02 and again at 0.01, so each run
# only ever reported its own increment. 0.01 is therefore an upper bound rather than a reading,
# and this wheel has no meaningful stiction problem. Its real trouble is elsewhere: there are
# positions near centre it will not leave at 0.30, cause not established.
#
# It has been wrong twice, in opposite directions, which is why the provenance is written down:
#   * 0.1 was a placeholder that was never measured at all, and made a session look like 75%
#     of its output was wasted.
#   * 0.02 came from a real run whose later passes had walked the wheel into its end stop. A
#     wheel against the stop cannot move at any force, so those passes measured the stop and
#     inflated the average. stiction_test.py now recentres before every pass.
#
# If you run this against a different wheel, re-measure rather than trusting this number.
STICTION = 0.01

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# Structured log lines are `key=value` pairs; floats are written %+.3f by probe_log.event.
FIELD = re.compile(r"(\w+)=([+-]?[\d.]+)")


def newest_log():
    logs = glob.glob(os.path.join(LOG_DIR, "vjoy_bridge_*.log"))
    if not logs:
        return None
    return max(logs, key=os.path.getmtime)


def read(path):
    ticks, kinds, directions, tunes = [], {}, [], []
    # Logs written before the telemetry was widened have no `game` field. Reporting those as
    # a force of zero would read as "the game asked for nothing", which is a different and
    # much more alarming claim than "this log predates the field".
    have_game = False
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "bridge.tick" in line:
                d = dict(FIELD.findall(line))
                have_game = have_game or "game" in d
                try:
                    ticks.append((float(d["steering"]), float(d["force"]),
                                  float(d.get("game", 0.0)), float(d.get("pos", 0.0)),
                                  float(d.get("dirx", 0.0)), float(d.get("vel", 0.0))))
                except (KeyError, ValueError):
                    pass
            elif "decode.effect_op" in line:
                m = re.search(r"kind=(\S+)", line)
                if m:
                    kinds[m.group(1)] = kinds.get(m.group(1), 0) + 1
            elif "decode.direction" in line:
                directions.append(line.strip())
            elif "tune.changed" in line:
                tunes.append(line.strip())
    return ticks, kinds, directions, tunes, have_game


def correlation(pairs):
    """Pearson r. Returns None when there is nothing to correlate."""
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(a for a, _ in pairs) / n
    my = sum(b for _, b in pairs) / n
    cov = sum((a - mx) * (b - my) for a, b in pairs)
    sx = sum((a - mx) ** 2 for a, _ in pairs) ** 0.5
    sy = sum((b - my) ** 2 for _, b in pairs) ** 0.5
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


def bar(value, width=40):
    """A -1..+1 value as a centred bar, so a sign is visible at a glance."""
    half = width // 2
    n = int(round(abs(value) * half))
    if value < 0:
        return " " * (half - n) + "#" * n + "|" + " " * half
    return " " * half + "|" + "#" * n + " " * (half - n)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else newest_log()
    if not path or not os.path.exists(path):
        print("No bridge log found. Run the bridge first.")
        return 1

    ticks, kinds, directions, tunes, have_game = read(path)
    print("session: %s" % os.path.basename(path))
    print("samples: %d" % len(ticks))
    if not ticks:
        print("\nNo telemetry in this log -- the bridge never reached its run loop.")
        return 1

    print("\neffects the game sent")
    if kinds:
        for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1]):
            print("  %-12s x%d" % (kind, count))
        if set(kinds) <= {"constant", "?"}:
            print("  -> constant force ONLY: no spring, damper or periodic. Centring has to")
            print("     arrive inside that one signed stream, or be synthesised here.")
    else:
        print("  none -- the game sent no effects at all.")

    if directions:
        print("\ndirections the game used")
        for line in directions[:8]:
            print("  %s" % line.split("decode.direction", 1)[-1].strip())

    out = [t[1] for t in ticks]
    game = [t[2] for t in ticks]
    print("\nforce")
    if have_game:
        print("  game asked : %+.3f .. %+.3f" % (min(game), max(game)))
    else:
        print("  game asked : not recorded (log predates the `game` telemetry field)")
    print("  wheel got  : %+.3f .. %+.3f" % (min(out), max(out)))

    neg = sum(1 for v in out if v < -0.001)
    pos = sum(1 for v in out if v > 0.001)
    print("  negative %d / positive %d" % (neg, pos))
    if neg == 0 and pos > 0:
        print("  ** RECTIFIED: output never goes negative. The wheel physically cannot")
        print("     centre, and will pull one way permanently. This is a bug, not a setting.")
    elif pos == 0 and neg > 0:
        print("  ** RECTIFIED (negative side only) -- same bug, mirrored.")

    nz = [abs(v) for v in out if abs(v) > 0.001]
    if nz:
        clipped = sum(1 for v in nz if v > 0.995 * max(nz))
        tiny = sum(1 for v in nz if v < STICTION)
        print("  non-zero samples %d; %.1f%% under %.2f (below breakaway), %.1f%% clipping"
              % (len(nz), 100.0 * tiny / len(nz), STICTION, 100.0 * clipped / len(nz)))

    # THE test, WITH ONE BIG CAVEAT.
    #
    # Position and force on a force-feedback wheel are mutually causal: the game's force
    # responds to where the wheel is, and the wheel goes where the force pushes it. So this
    # correlation is only a measurement of the GAME when we are not driving the wheel. Once
    # the loop is closed and running away, both saturate together and it reads strongly
    # positive no matter what the game intended -- which is exactly how it was misread for
    # three rounds of "flip the sign and try again".
    #
    # To measure the game's intent, break the loop: set strength 0 in tune.json, steer by
    # hand, and read the OPEN LOOP number below.
    # Only the LAST unbroken stretch with no force applied. A session usually contains
    # several, and in-game force feedback settings change between them -- one taken with the
    # game's centring spring on and one with it off describe different games, and averaging
    # them together produced a confident wrong answer once already. The most recent stretch
    # is the one that matches how the game is configured now.
    open_loop = []
    for tick in ticks:
        if abs(tick[1]) <= 0.001:
            open_loop.append(tick)
        elif open_loop:
            open_loop = []
    closed = len(ticks) - len(open_loop)
    pairs = [(t[0], t[1]) for t in ticks if abs(t[0]) > 0.05 and abs(t[1]) > 0.02]
    r = correlation(pairs)

    if have_game:
        ol = [(t[0], t[2]) for t in open_loop if abs(t[0]) > 0.10 and abs(t[2]) > 0.05]
        r_open = correlation(ol)
        # A correlation over near-zero forces is a verdict about noise. This printed
        # "invert must be TRUE" from samples whose largest force was 0.066 of full scale and
        # whose signs agreed 55% of the time -- a coin flip dressed up as r = +0.390. So the
        # force has to be big enough to mean something before its sign is worth reporting.
        strongest = max((abs(g) for _s, g in ol), default=0.0)
        agree = (sum(1 for s, g in ol if s * g > 0) / len(ol)) if ol else 0.0
        decisive = strongest > 0.15 and (agree > 0.8 or agree < 0.2)
        if r_open is not None and len(ol) >= 10 and not decisive:
            print("\nOPEN LOOP -- inconclusive")
            print("  Largest force seen was %.3f and signs agreed %.0f%% of the time, so"
                  % (strongest, 100 * agree))
            print("  there is no sign to read here. The game sent almost nothing while the")
            print("  wheel was held at an angle -- which is itself a finding: its force may")
            print("  depend on wheel MOVEMENT rather than wheel ANGLE. Check the velocity")
            print("  section below.")
        elif r_open is not None and len(ol) >= 10:
            same = sum(1 for s, g in ol if s * g > 0)
            print("\nOPEN LOOP -- the game's intent, measured while we drove the wheel with"
                  " nothing")
            print("  corr(steering, game force) = %+.3f over %d samples" % (r_open, len(ol)))
            print("  force points the SAME way as steering in %d of %d (%.0f%%)"
                  % (same, len(ol), 100.0 * same / len(ol)))
            if r_open > 0.3:
                print("  -> the game's force FOLLOWS steering, so applying it as-is is")
                print("     positive feedback. invert must be TRUE.")
            elif r_open < -0.3:
                print("  -> the game's force OPPOSES steering: real self-aligning torque.")
                print("     invert must be FALSE.")
            else:
                print("  -> no clear relationship; drive more corners at strength 0.")

    # A game's force can depend on wheel MOVEMENT rather than wheel ANGLE, and the two need
    # opposite handling. DiRT 4 with its centring spring disabled sends pure damping: nothing
    # while the wheel is held, force strictly opposing motion once it moves (measured at
    # r = -0.998, 0 of 34 samples agreeing in sign). Inverting a damping force turns it into
    # NEGATIVE damping, which feeds energy into any disturbance -- a wheel that oscillates
    # lock to lock from the smallest nudge. That is not a gain problem and no amount of
    # lowering strength fixes it.
    if have_game:
        vel = [(t[5], t[2]) for t in open_loop if abs(t[5]) > 0.05 and abs(t[2]) > 0.01]
        still = [t for t in open_loop if abs(t[5]) <= 0.02]
        if len(vel) >= 10:
            r_vel = correlation(vel)
            agree = sum(1 for v, g in vel if v * g > 0)
            print("\nOPEN LOOP -- force against wheel MOVEMENT")
            if still:
                print("  wheel held still: mean force %.3f over %d samples"
                      % (sum(abs(t[2]) for t in still) / len(still), len(still)))
            print("  wheel moving    : mean force %.3f over %d samples"
                  % (sum(abs(g) for _v, g in vel) / len(vel), len(vel)))
            if r_vel is not None:
                print("  corr(velocity, game force) = %+.3f" % r_vel)
                print("  force points WITH the motion in %d of %d (%.0f%%)"
                      % (agree, len(vel), 100.0 * agree / len(vel)))
                if r_vel < -0.5:
                    print("  -> the game is sending DAMPING. Apply it as-is: invert must be")
                    print("     FALSE. Inverting damping makes the wheel self-oscillate.")
                elif r_vel > 0.5:
                    print("  -> force follows the motion, which is negative damping and")
                    print("     unstable on its own. invert must be TRUE.")

    print("\ncentring (does force oppose steering angle?)")
    if closed:
        print("  NOTE: %d of %d samples had force applied, so this number is contaminated by"
              % (closed, len(ticks)))
        print("  our own output. Trust the OPEN LOOP section above for the game's intent.")
    if r is None:
        print("  not enough samples with both steering and force -- drive some corners.")
    else:
        print("  corr(steering, force) = %+.3f over %d samples" % (r, len(pairs)))
        print("  %s" % bar(r))
        if r < -0.3:
            print("  -> GOOD. Force opposes steering: that is self-aligning torque.")
        elif r > 0.3:
            print("  -> INVERTED. Force pushes deeper into the turn. Set invert=true in")
            print("     tune.json -- no restart needed.")
        else:
            print("  -> NO RELATIONSHIP. Either the sign is being destroyed, or the game is")
            print("     not sending centring at all. Check the directions section above.")

    # Does the direction field track STEERING? If it does, it is a steering axis and must be
    # read as a span; a true polar angle would not follow the wheel. This is the measurement
    # that tells the two encodings apart without another drive per guess.
    aimed = [(t[0], t[4]) for t in ticks if t[4] > 0 and abs(t[1]) > 0.02]
    if aimed:
        angles = [a for _s, a in aimed]
        print("\ndirection encoding")
        print("  dirx range %.0f .. %.0f  (%.1f .. %.1f degrees)"
              % (min(angles), max(angles),
                 360.0 * min(angles) / 32768.0, 360.0 * max(angles) / 32768.0))
        span = max(angles) - min(angles)
        if span and max(angles) <= 16384:
            print("  the game never leaves the first half circle, where sin cannot go")
            print("  negative -- so 'sin' mode CANNOT produce a leftward force here.")
        r_dir = correlation(aimed)
        if r_dir is not None:
            print("  corr(steering, dirx) = %+.3f" % r_dir)
            if abs(r_dir) > 0.3:
                print("  -> direction FOLLOWS the wheel: it is a steering axis. Use")
                print("     dir_mode \"span\"%s."
                      % (" with invert=true" if r_dir > 0 else ""))
            else:
                print("  -> direction does not track steering; it is aimed by something else")
                print("     (the car's physics, not the wheel). Judge it by feel instead.")

    if tunes:
        print("\ntuning changes during this session")
        for line in tunes[-10:]:
            print("  %s" % line.split("tune.changed", 1)[-1].strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
