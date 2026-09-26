"""
python -m bridge.nudge: push the wheel briefly right, then left, and report whether it moved.

Proves the binding, the motor and the force sign in one go. The last line printed is the
result, for the window to read: "nudge ok right=+0.081 left=-0.077".
"""

import sys
import time

import ffb_render as render
from bridge.device import RawUsbWheel

PUSH = 0.10             # well under the hard cap; a free wheel coasts far past a push
PUSH_SECONDS = 0.25
CENTRE_SECONDS = 1.5
MOVED = 0.02            # position change that counts as movement
TICK = 1.0 / 250.0


def centre(wheel, law, seconds):
    """Hold the tuned spring for a while so the wheel settles before the next push."""
    end = time.monotonic() + seconds
    reading = None
    while time.monotonic() < end:
        latest = wheel.read()
        now = time.monotonic()
        law.observe(latest.wheel, now, latest is not reading)
        reading = latest
        wheel.set_force(law.force(0.25, 0.08, 0.05))
        time.sleep(TICK)
    wheel.set_force(0.0)


def push(wheel, force):
    start = wheel.read().wheel
    wheel.set_force(force)
    time.sleep(PUSH_SECONDS)
    moved = wheel.read().wheel - start
    wheel.set_force(0.0)
    return moved


def verdict(right, left):
    if right > MOVED and left < -MOVED:
        return "ok"
    if right < -MOVED and left > MOVED:
        return "reversed"
    if abs(right) < MOVED and abs(left) < MOVED:
        return "still"
    return "unclear"


def main():
    wheel = RawUsbWheel()
    law = render.CentringLaw()
    try:
        wheel.open()
        centre(wheel, law, CENTRE_SECONDS)
        right = push(wheel, PUSH)
        centre(wheel, law, CENTRE_SECONDS)
        left = push(wheel, -PUSH)
        centre(wheel, law, CENTRE_SECONDS)
    except RuntimeError as exc:
        print("nudge error %s" % exc)
        return 2
    finally:
        wheel.close()
    # A wheel that sent nothing was not reporting, often mid calibration sweep.
    result = verdict(right, left) if wheel.reports > 1 else "silent"
    print("nudge %s right=%+.3f left=%+.3f" % (result, right, left))
    return 0 if result == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
