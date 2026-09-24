#!/usr/bin/env python3
"""Calibrate the steering reading against the physical wheel. No force is ever commanded.

Power-on only, no arming: the operator moves the wheel by hand through known angles, holding
each for a few seconds, and every reading is logged with its time. Plateaus in the log are then
matched to the angles, which settles sign, scale, and whether the value is absolute or zeroed
wherever the wheel sat at power-on.

Measured 2026-09-23: a sweep ended physically ~100 degrees right while reading -0.121, and
every run's first reading is exactly 0x8000 whatever the wheel's position.
"""

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

from gip_usb_host import (  # noqa: E402
    CENTRE,
    GIP_CMD_INPUT,
    OPT_ACKNOWLEDGE,
    Wheel,
    decode_header,
    steering,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=40.0)
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
        started = time.time()
        first = None
        last_print = 0.0
        latest = None
        print("   t(s)    raw   position")
        while time.time() - started < args.seconds:
            data = wheel.read(timeout=20)
            now = time.time() - started
            if data:
                head = decode_header(data)
                if head and head["options"] & OPT_ACKNOWLEDGE:
                    wheel.acknowledge(head, received=head["length"])
                if head and head["command"] == GIP_CMD_INPUT:
                    reading = steering(head["payload"])
                    if reading is not None:
                        latest = reading
                        if first is None:
                            first = reading
                            print("   first reading %d (%+.3f) at %.2f s"
                                  % (reading, (reading - CENTRE) / float(CENTRE), now))
            if latest is not None and now - last_print >= 0.5:
                print("  %5.1f  %5d   %+.3f" % (now, latest, (latest - CENTRE) / float(CENTRE)),
                      flush=True)
                last_print = now
    finally:
        wheel.close()


if __name__ == "__main__":
    main()
