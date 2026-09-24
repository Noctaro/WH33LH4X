r"""
gip_wheel_driver.py -- A usable Linux driver for the HORI Force Feedback Racing Wheel.

Owns the wheel over raw USB, publishes a virtual joystick other programs can read, and holds
condition effects (spring, damper) on the real motor. No Microsoft driver, no GIP
authentication, no foreground gate, and no capture file: every byte of the protocol comes from
`gip_arming`, which is verified byte-identical to a real USB wire capture.

WHY A VIRTUAL JOYSTICK IS NEEDED. Claiming the USB interface means no kernel driver binds the
wheel, so the system presents no input device at all. Everything else on the machine would see
nothing. uinput gives it back a joystick while we keep the motor.

THE POINT OF THE EXERCISE. This wheel's firmware holds the steering at centre with a spring far
too strong for driving anything delicate, and Windows only lets the foreground application touch
the motor. Here the firmware spring is suppressed by the arming and replaced with one of our
own, at whatever stiffness suits -- which is what the whole investigation was for.

Requires: pyusb, python3-evdev, and write access to /dev/uinput.
    sudo apt install -y python3-usb python3-evdev
    sudo modprobe uinput
    echo 'KERNEL=="uinput", MODE="0660", GROUP="plugdev"' | sudo tee /etc/udev/rules.d/99-uinput.rules
    sudo udevadm control --reload && sudo udevadm trigger

Usage:
    python3 gip_wheel_driver.py --spring 0.6 --damper 0.15
    python3 gip_wheel_driver.py --spring 0.0            # free wheel, input only

SAFETY: force is capped and zeroed on exit. The wheel is strong; do not run it unattended.
"""

import argparse
import functools
import math
import os
import sys
import time

import ffb_render
import gip_arming
from gip_usb_host import (
    CENTRE,
    DEFAULT_CAP,
    GIP_CMD_INPUT,
    OPT_ACKNOWLEDGE,
    Wheel,
    decode_header,
    steering,
    wait_calibration,
)

try:
    from evdev import AbsInfo, UInput
    from evdev import ecodes as e
except ImportError:
    sys.exit("python3-evdev missing -- sudo apt install python3-evdev")

print = functools.partial(print, flush=True)  # noqa: A001 -- ssh gives us a pipe, not a tty

AXIS_RANGE = 32767

# Measured by stiction_test.py, 2026-08-25. Breakaway on this wheel is below 0.01, so a minimum
# force floor is not the fix. The real problem is specific rotor positions near centre that it
# will not leave even at 0.30 -- which is exactly what "it stops at the last position change"
# is. Rest before kicking: commanding again immediately is what trips the firmware's stall
# protection, after which the motor accepts everything and drives nothing.
MOVED = 0.01               # position change that counts as having moved
STALL_SECONDS = 0.6        # commanded force with no movement for this long means stuck
STALL_REST = 0.8           # rest at zero before commanding anything again
STALL_KICK = 0.30          # well above any breakaway this wheel has shown
STALL_KICK_SECONDS = 0.6
STALE_READING = 0.02      # no input for this long means the wheel is still, not fast


def make_virtual_joystick():
    """A virtual joystick carrying the wheel's steering axis.

    Only steering is published for now: it is the one input field whose encoding is proven
    (16-bit little-endian at payload bytes 2-3, centred on 0x8000). Pedals and buttons live
    somewhere in the same report and can be added once their offsets are measured rather than
    guessed -- guessing input layouts has already cost this project a retraction.
    """
    capabilities = {
        e.EV_ABS: [
            (e.ABS_X, AbsInfo(value=0, min=-AXIS_RANGE, max=AXIS_RANGE,
                              fuzz=0, flat=0, resolution=0)),
        ],
        e.EV_KEY: [e.BTN_JOYSTICK],
    }
    return UInput(capabilities, name="HORI Force Feedback Racing Wheel (raw USB)",
                  vendor=0x0F0D, product=0x015C, version=1)


def run(wheel, stiffness, damping, cap, seconds, rate=60.0, refresh=10.0,
        deadband=0.01, wait_for=None, smooth=True, trace=None, unstick=True,
        min_force=0.05, release=0.05, coast=0.0, floor_ramp=0.05,
        force_step=0.001):
    joystick = make_virtual_joystick()
    # UInput.device is not always populated -- evdev resolves it by rescanning /dev/input, which
    # can lose the race or lack permission. The device exists either way, so do not die for a
    # cosmetic path.
    path = getattr(getattr(joystick, "device", None), "path", None)
    print("virtual joystick created: %s" % (path or getattr(joystick, "syspath", "(created)")))

    print("\n-- arming (generated) --")
    seed = None
    for command, body, options, delay in gip_arming.arming_sequence():
        wheel.send(command, body, options=options)
        if delay:
            time.sleep(delay)
        data = wheel.read(timeout=1)
        if data:
            head = decode_header(data)
            if head and head["command"] == GIP_CMD_INPUT:
                seed = steering(head["payload"]) or seed

    beat_command, beat_body = gip_arming.heartbeat()
    state = ffb_render.WheelState()
    spring = ffb_render.legacy_condition_params("spring", stiffness, deadband=deadband)
    damper = ffb_render.legacy_condition_params("damper", damping)

    position = seed if seed is not None else CENTRE
    state.update((position - CENTRE) / float(CENTRE), time.time())

    next_beat = time.time()

    if wait_for:
        # Hold here until released. The operator cannot see this terminal, so without an
        # explicit trigger they have to guess when the calibration sweep has finished and the
        # spring is live -- and a measurement that depends on someone guessing is not one.
        print("ARMED AND WAITING -- the wheel is still; holding for the trigger")
        while not os.path.exists(wait_for):
            now = time.time()
            data = wheel.read(timeout=5)
            if data:
                head = decode_header(data)
                if head and head["options"] & OPT_ACKNOWLEDGE:
                    wheel.acknowledge(head)
            if now >= next_beat:
                wheel.send(beat_command, beat_body)
                next_beat = now + 1.0 / 16.0
        os.remove(wait_for)
        print("TRIGGERED")

    print("\n-- RUNNING: spring %.2f, damper %.2f, cap %.2f --" % (stiffness, damping, cap))
    print("   the wheel is now a joystick; turn it and it should feel light")
    started = time.time()
    next_force = started
    last_sent = None
    last_time = 0.0
    reports = 0
    stall_since = None
    stall_position = state.position
    settled = False
    kicks = 0
    last_reading = started
    try:
        while seconds <= 0 or time.time() - started < seconds:
            now = time.time()
            # Input is event-driven: a wheel that stops moving sends nothing, so state.velocity
            # would hold its last value for ever and every decision resting on it would act on a
            # speed the wheel no longer has. Feed the last position back in to decay it to zero.
            if now - last_reading > STALE_READING:
                state.update(state.position, now)
                last_reading = now
            data = wheel.read(timeout=5)
            if data:
                head = decode_header(data)
                if head and head["command"] == GIP_CMD_INPUT:
                    reading = steering(head["payload"])
                    if reading is not None:
                        position = reading
                        state.update((position - CENTRE) / float(CENTRE), now)
                        last_reading = now
                        joystick.write(e.EV_ABS, e.ABS_X, position - CENTRE)
                        joystick.syn()
                        reports += 1
                elif head and head["options"] & OPT_ACKNOWLEDGE:
                    wheel.acknowledge(head)

            if now >= next_beat:
                wheel.send(beat_command, beat_body)
                next_beat = now + 1.0 / 16.0

            if unstick:
                if abs(state.position - stall_position) > MOVED:
                    stall_position, stall_since = state.position, None
                elif last_sent is not None and abs(last_sent) > 0.02 and stall_since is None:
                    stall_since = now
                if stall_since is not None and now - stall_since > STALL_SECONDS:
                    # Rest, then kick off the detent towards the target, then carry on. Pushing
                    # harder from the same rotor position is what makes the firmware go quiet.
                    kicks += 1
                    wheel.send(gip_arming.GIP_PARAM, gip_arming.force_block(0.0))
                    time.sleep(STALL_REST)
                    direction = -1.0 if state.position > 0 else 1.0
                    for command, body, options, delay in gip_arming.force_sequence(
                            direction * STALL_KICK):
                        wheel.send(command, body, options=options)
                        if delay:
                            time.sleep(delay)
                    time.sleep(STALL_KICK_SECONDS)
                    wheel.send(gip_arming.GIP_PARAM, gip_arming.force_block(0.0))
                    stall_since, stall_position = None, state.position
                    last_sent, last_time = 0.0, time.time()

            if now >= next_force:
                centring = ffb_render.condition_force("spring", spring, state)
                # Floor the CENTRING force only. Applying it to spring+damper let the damper's
                # sign win when the spring was small, and the damper's sign comes from a
                # smoothed, lagging velocity -- so the floor was amplifying force in the
                # direction the wheel was already moving. That is positive feedback, and it
                # feels exactly like the wheel accelerating your own movement.
                if min_force:
                    # A hard floor SWITCHES ON at the deadband edge and flips sign across
                    # centre, so the force jumps by twice min_force in the one place the wheel
                    # is most sensitive. That is felt as a step in the middle. Ramp it instead:
                    # zero at centre, full floor once clear of it, no edge anywhere.
                    if floor_ramp > 0.0:
                        floor = min_force * math.tanh(abs(state.position) / floor_ramp)
                    elif abs(state.position) > deadband:
                        floor = min_force
                    else:
                        floor = 0.0
                    if abs(centring) < floor:
                        centring = -floor if state.position > 0 else floor

                # Hysteresis: once it has settled near centre, stop pushing until it is moved
                # well clear again. Commanding a fixed floor right outside a narrow deadband
                # drives past centre, flips sign and drives back -- a limit cycle, felt as
                # twitching.
                # Coasting: once the wheel is already travelling towards centre, stop pushing
                # and let momentum carry it. A fixed floor drives THROUGH centre, flips sign and
                # drives back -- felt as sweeping around mid. Cutting force on approach is what
                # a person does with a real wheel, and it costs nothing when it is stationary.
                approaching = (state.position * state.velocity) < 0
                if coast and approaching and abs(state.velocity) > coast:
                    centring = 0.0

                if settled:
                    if abs(state.position) > release:
                        settled = False
                elif deadband > 0.0 and abs(state.position) <= deadband:
                    # deadband 0 means NO gate. Without the guard, exact centre satisfies
                    # abs(position) <= 0, so every pass through the middle zeroed the force for
                    # one sample and snapped it back -- a step exactly where the wheel is most
                    # sensitive.
                    settled = True
                demand = 0.0 if settled else centring
                if damping and not settled:
                    demand += ffb_render.condition_force("damper", damper, state)
                demand = max(-cap, min(cap, demand))
                # On change, or refreshed periodically. Every force command re-uploads the
                # effect table; sending one 60 times a second restarts the effect before it
                # can act, and sending one only once lets it lapse.
                if last_sent is None or (refresh and now - last_time > refresh):
                    # Full effect load: clear, re-upload the table, set the magnitude, start.
                    for command, body, options, delay in gip_arming.force_sequence(demand):
                        wheel.send(command, body, options=options)
                        if delay:
                            time.sleep(delay)
                    last_sent, last_time = demand, now
                elif abs(demand - last_sent) > force_step:
                    # Once an effect is loaded AND RUNNING, update the magnitude with a bare
                    # parameter block. Re-running the whole load sequence for every change tears
                    # the effect down and rebuilds it ~30 ms at a time, which is felt as cogging
                    # and lags the loop into oscillating around centre.
                    if smooth:
                        wheel.send(gip_arming.GIP_PARAM, gip_arming.force_block(demand))
                    else:
                        for command, body, options, delay in gip_arming.force_sequence(demand):
                            wheel.send(command, body, options=options)
                            if delay:
                                time.sleep(delay)
                    last_sent = demand
                if trace is not None:
                    trace.append((now - started, state.position, demand))
                next_force = now + 1.0 / rate
    except KeyboardInterrupt:
        print("\n   stopping")
    finally:
        for command, body, options, delay in gip_arming.force_sequence(0.0):
            wheel.send(command, body, options=options)
            if delay:
                time.sleep(delay)
        joystick.close()
        print("   %d position report(s) published, %d unstick kick(s); force zeroed"
              % (reports, kicks))
        if trace:
            with open("wheel_trace.txt", "w", encoding="ascii") as handle:
                for stamp, pos, demand in trace:
                    handle.write("%.4f %.5f %.5f\n" % (stamp, pos, demand))
            print("   wrote %d trace sample(s) to wheel_trace.txt" % len(trace))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spring", type=float, default=0.25,
                        help="centring stiffness; 0 leaves the wheel free (default 0.25)")
    parser.add_argument("--damper", type=float, default=0.08,
                        help="resistance to turning speed (default 0.08)")
    parser.add_argument("--cap", type=float, default=DEFAULT_CAP,
                        help="never command more than this (default %.2f)" % DEFAULT_CAP)
    parser.add_argument("--min-force", type=float, default=0.05,
                        help="smallest magnitude worth commanding outside the "
                             "deadband; 0 disables (default 0.05)")
    parser.add_argument("--force-step", type=float, default=0.001,
                        help="smallest force change worth sending. Near centre the "
                             "demand is only 0.01-0.03, so a coarse step is a large "
                             "fraction of it and is felt as notching (default 0.001)")
    parser.add_argument("--floor-ramp", type=float, default=0.05,
                        help="distance over which --min-force fades in from centre; a hard "
                             "floor flips sign across centre and is felt as a step "
                             "(0 restores the hard floor)")
    parser.add_argument("--coast", type=float, default=0.0,
                        help="stop pushing once moving towards centre faster than "
                             "this, so momentum finishes the job (0 disables)")
    parser.add_argument("--release", type=float, default=0.0,
                        help="once settled near centre, stay quiet until the wheel "
                             "is moved this far out (default 0.05)")
    parser.add_argument("--no-unstick", action="store_true",
                        help="do not kick the wheel off cogging detents")
    parser.add_argument("--deadband", type=float, default=0.0,
                        help="spring dead zone around centre, and the settle gate that goes "
                             "with it; 0 disables both (default 0)")
    parser.add_argument("--refresh", type=float, default=0.0,
                        help="seconds between full effect reloads; each one tears "
                             "the effect down and rebuilds it, felt as a distinct "
                             "cogging step. 0 = never (default 0)")
    parser.add_argument("--no-smooth", action="store_true",
                        help="re-run the full effect load for every change")
    parser.add_argument("--trace", action="store_true",
                        help="log time, position and demand to wheel_trace.txt")
    parser.add_argument("--wait-for", metavar="FILE",
                        help="after arming, hold still until this file appears")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="run for this long; 0 means until Ctrl+C")
    parser.add_argument("--wait-calibration", type=float, default=0.0, metavar="CAP",
                        help="wait for a firmware calibration sweep first. Claiming the "
                             "device does NOT trigger one, so this normally just times out "
                             "(default 0)")
    args = parser.parse_args()

    wheel = Wheel()
    wheel.open()
    try:
        wheel.power_on()
        time.sleep(0.3)
        if args.wait_calibration:
            wait_calibration(wheel, cap=args.wait_calibration)
        run(wheel, args.spring, args.damper, args.cap, args.seconds,
            refresh=args.refresh, wait_for=args.wait_for,
            smooth=not args.no_smooth, trace=[] if args.trace else None,
            unstick=not args.no_unstick, deadband=args.deadband,
            min_force=args.min_force, release=args.release, coast=args.coast,
            floor_ramp=args.floor_ramp, force_step=args.force_step)
    finally:
        wheel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
