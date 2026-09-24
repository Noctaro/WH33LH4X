#!/usr/bin/env python3
"""Map the input report: which bytes each pedal, lever and button moves. No force is commanded.

Input is event-driven, so an untouched wheel sends nothing and every control the operator
works shows up as its own burst of reports, bounded by silence. The recording is split at those
gaps and each burst reports the bytes that changed, per message type, with their range.
Operate one control per burst, in an agreed order, and leave a pause between them.

The raw log (time, command, payload hex) goes to --log, in the current directory by default.
"""

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

from gip_usb_host import OPT_ACKNOWLEDGE, Wheel, decode_header  # noqa: E402

GAP = 1.5          # seconds of silence that ends a burst
IGNORE = {0x01}    # acknowledgements carry no input


def bursts(records):
    out = []
    for record in records:
        if not out or record[0] - out[-1][-1][0] > GAP:
            out.append([])
        out[-1].append(record)
    return out


def describe(burst, baseline):
    """Offsets that changed within the burst or away from the idle baseline, per command."""
    lines = []
    by_command = {}
    for _, command, payload in burst:
        by_command.setdefault(command, []).append(payload)
    for command, payloads in sorted(by_command.items()):
        width = min(len(p) for p in payloads)
        base = baseline.get(command)
        moved = []
        for offset in range(width):
            values = [p[offset] for p in payloads]
            changed = len(set(values)) > 1 or (
                base is not None and offset < len(base) and values[0] != base[offset])
            if changed:
                moved.append("%d:0x%02x..0x%02x" % (offset, min(values), max(values)))
        lines.append("      0x%02x x%-4d %s" % (command, len(payloads),
                                               " ".join(moved) or "(no byte changed)"))
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--log", default="input_map_log.txt")
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
    records = []
    try:
        wheel.power_on()
        started = time.time()
        print("   recording %g s -- operate one control per burst, pause between" % args.seconds,
              flush=True)
        while time.time() - started < args.seconds:
            data = wheel.read(timeout=20)
            if not data:
                continue
            head = decode_header(data)
            if not head:
                continue
            if head["options"] & OPT_ACKNOWLEDGE:
                wheel.acknowledge(head, received=head["length"])
            if head["command"] in IGNORE:
                continue
            records.append((time.time() - started, head["command"], bytes(head["payload"])))
    finally:
        wheel.close()

    with open(args.log, "w", encoding="ascii") as handle:
        for stamp, command, payload in records:
            handle.write("%8.3f 0x%02x %s\n" % (stamp, command, payload.hex()))
    print("   %d message(s), raw log in %s" % (len(records), args.log))

    # The first report of a session is junk (2026-09-23), so the baseline per command is the first
    # payload after it; start operating controls only once recording has begun, or it is not idle.
    groups = bursts(records)
    baseline = {}
    for _, command, payload in records[1:]:
        baseline.setdefault(command, payload)
    for index, burst in enumerate(groups):
        print("\n   burst %d: %.1f..%.1f s, %d message(s)"
              % (index, burst[0][0], burst[-1][0], len(burst)))
        for line in describe(burst, baseline):
            print(line)


if __name__ == "__main__":
    main()
