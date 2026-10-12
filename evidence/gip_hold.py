#!/usr/bin/env python3
"""Drive the wheel slowly to a position, hold it there, and bring it back. Linux, raw USB.

Built on gip_wheel_driver's loop rather than gip_usb_host --drive, which reloads the whole
effect on every demand change of 0.02: each reload tears the effect down, and on 2026-09-23
that was seen as the wheel swinging right -> centre -> right instead of holding.

Here the effect is loaded once and then only its magnitude changes, the target moves slowly,
and the real position is logged four times a second.
"""

import argparse
import os
import sys
import time

# The gip package sits at the repository root, or beside this file in a flat copy.
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ffb_render  # noqa: E402
from gip import arming as gip_arming  # noqa: E402
from gip.host import Wheel  # noqa: E402
from gip.report import CENTRE, steering  # noqa: E402
from gip.wire import GIP_CMD_INPUT, OPT_ACKNOWLEDGE, decode_header  # noqa: E402

STALE_READING = 0.02   # as gip_wheel_driver: no input this long means still, not fast
OFF_TARGET = 0.03      # a still wheel closer than this to its target is holding, not stalled
ON_TARGET = 0.01       # closer than this, command exactly zero rather than a tiny force
JUMP = 0.3             # a lone sample this far from the last is dropped unless the next agrees
# gip_wheel_driver's unstick, same values: commanded force on a still rotor trips the firmware's
# stall protection, after which it accepts everything and drives nothing (stiction_test.py).
MOVED = 0.01
STALL_SECONDS = 0.6
STALL_REST = 0.8
STALL_KICK = 0.30
STALL_KICK_SECONDS = 0.6
REFRESH = 10.0         # full reload interval, as gip_wheel_driver's default
FORCE_STEP = 0.001     # smallest magnitude change worth sending, as gip_wheel_driver


def arm(wheel):
    """gip_wheel_driver's arming, unchanged."""
    for command, body, options, delay in gip_arming.arming_sequence():
        wheel.send(command, body, options=options)
        if delay:
            time.sleep(delay)
        data = wheel.read(timeout=1)
        if data:
            head = decode_header(data)
            if head and head["options"] & OPT_ACKNOWLEDGE:
                wheel.acknowledge(head)


def target_at(t, settle, ramp, hold, goals):
    """Centre while settling, then for each goal a straight ramp and a hold, then back to 0."""
    t -= settle
    here = 0.0
    if t < 0.0:
        return here
    for goal in list(goals) + [0.0]:
        if t < ramp:
            return here + (goal - here) * t / ramp
        t -= ramp
        if t < hold:
            return goal
        t -= hold
        here = goal
    return 0.0


def run(wheel, goals, settle, ramp, hold, spring, damping, cap):
    total = settle + (len(goals) + 1) * (ramp + hold) + 1.0
    state = ffb_render.WheelState()
    damper = ffb_render.legacy_condition_params("damper", damping)
    beat_command, beat_body = gip_arming.heartbeat()

    started = time.time()
    state.update(0.0, started)
    last_reading = started
    next_beat = started
    next_force = started
    next_log = started
    last_sent = None
    last_time = 0.0
    reports = 0
    kicks = 0
    stall_since = None
    stall_position = 0.0
    pending = None
    episode = 0
    print("\n   t(s)  target  position  demand")
    try:
        while time.time() - started < total:
            now = time.time()
            t = now - started
            if now - last_reading > STALE_READING:
                state.update(state.position, now)
                last_reading = now
            data = wheel.read(timeout=5)
            if data:
                head = decode_header(data)
                if head and head["command"] == GIP_CMD_INPUT:
                    reading = steering(head["payload"])
                    if reading is not None:
                        value = (reading - CENTRE) / float(CENTRE)
                        reports += 1
                        # The first report of a session is junk, and lone sign-flipped samples
                        # occur mid-run (2026-09-23): accept a big jump only once confirmed.
                        if reports == 1:
                            pass
                        elif abs(value - state.position) > JUMP and (
                                pending is None or abs(value - pending) > JUMP / 3):
                            pending = value
                        else:
                            pending = None
                            state.update(value, now)
                            last_reading = now
                elif head and head["options"] & OPT_ACKNOWLEDGE:
                    wheel.acknowledge(head)

            if now >= next_beat:
                wheel.send(beat_command, beat_body)
                next_beat = now + 1.0 / 16.0

            target = target_at(t, settle, ramp, hold, goals)

            if abs(state.position - stall_position) > MOVED:
                if episode:
                    print("   moving again at %+.3f after step %d" % (state.position, episode))
                stall_position, stall_since, episode = state.position, None, 0
            elif (last_sent is not None and abs(last_sent) > 0.02 and stall_since is None
                  and abs(target - state.position) > OFF_TARGET):
                stall_since = now
            if stall_since is not None and now - stall_since > STALL_SECONDS:
                kicks += 1
                episode += 1
                wheel.send(gip_arming.GIP_PARAM, gip_arming.force_block(0.0))
                time.sleep(STALL_REST)
                direction = 1.0 if target > state.position else -1.0
                if episode > 2:
                    # The silent motor (evidence/revive_test.py): nothing in-process
                    # revives it and, by decision, nothing here tries. Stop and say so.
                    print("   stalled at %+.3f -- motor silent; force off" % state.position)
                    break
                print("   stalled at %+.3f -- episode %d: rest, then kick %+.2f"
                      % (state.position, episode, direction * STALL_KICK))
                for command, body, options, delay in gip_arming.force_sequence(
                        direction * STALL_KICK):
                    wheel.send(command, body, options=options)
                    if delay:
                        time.sleep(delay)
                time.sleep(STALL_KICK_SECONDS)
                wheel.send(gip_arming.GIP_PARAM, gip_arming.force_block(0.0))
                stall_since, stall_position = None, state.position
                last_sent, last_time = 0.0, time.time()
                continue

            if t < settle:
                demand = None    # no force at all until any power-on calibration is over
            elif now >= next_force:
                params = ffb_render.legacy_condition_params("spring", spring, offset=target,
                                                            deadband=0.0)
                demand = ffb_render.condition_force("spring", params, state)
                demand += ffb_render.condition_force("damper", damper, state)
                demand = max(-cap, min(cap, demand))
                if abs(target - state.position) < ON_TARGET:
                    # A creeping sub-breakaway force on a still wheel is exactly how the motor
                    # is silenced on demand (evidence/revive_test.py); zero is safe.
                    demand = 0.0
                next_force = now + 1.0 / 60.0
            else:
                demand = None

            if demand is not None:
                if last_sent is None or now - last_time > REFRESH:
                    for command, body, options, delay in gip_arming.force_sequence(demand):
                        wheel.send(command, body, options=options)
                        if delay:
                            time.sleep(delay)
                    last_sent, last_time = demand, now
                elif abs(demand - last_sent) > FORCE_STEP:
                    wheel.send(gip_arming.GIP_PARAM, gip_arming.force_block(demand))
                    last_sent = demand

            if now >= next_log:
                print("  %5.2f  %+.3f   %+.3f   %s"
                      % (t, target, state.position,
                         "  --  " if last_sent is None else "%+.3f" % last_sent), flush=True)
                next_log = now + 0.25
    finally:
        for command, body, options, delay in gip_arming.force_sequence(0.0):
            wheel.send(command, body, options=options)
            if delay:
                time.sleep(delay)
        print("   %d position report(s), %d unstick kick(s); force zeroed" % (reports, kicks))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", default="0.5",
                        help="comma-separated positions, full scale, positive right; lock is "
                             "~180 deg so 0.5 is ~90 deg (default 0.5)")
    parser.add_argument("--settle", type=float, default=8.0,
                        help="seconds after arming with no force at all (default 8)")
    parser.add_argument("--ramp", type=float, default=5.0, help="seconds to move out and back")
    parser.add_argument("--hold", type=float, default=15.0, help="seconds at the target")
    parser.add_argument("--spring", type=float, default=2.0, help="gain per unit of error")
    parser.add_argument("--damper", type=float, default=0.1)
    parser.add_argument("--cap", type=float, default=0.35)
    args = parser.parse_args()

    wheel = Wheel()
    try:
        bound = wheel.dev.is_kernel_driver_active(0)
    except NotImplementedError:
        bound = False
    if bound:
        raise SystemExit("xone is still bound. Unbind it first:\n"
                         "  echo -n '1-1:1.0' | sudo tee /sys/bus/usb/drivers/xone-wired/unbind")
    wheel.open()
    try:
        wheel.power_on()
        time.sleep(0.3)
        arm(wheel)
        goals = [float(x) for x in args.targets.split(",") if x.strip()]
        run(wheel, goals, args.settle, args.ramp, args.hold, args.spring, args.damper,
            min(args.cap, 0.35))
    finally:
        wheel.close()


if __name__ == "__main__":
    main()
