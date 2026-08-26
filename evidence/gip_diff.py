"""
Compare what Windows.Gaming.Input sends to the wheel against what we send.

WHY

Our replay produces the same *distinct* payloads WGI does -- verified byte-for-byte against
the capture. And it does not move the motor, while WGI's does. Since the driver accepts every
message with zero errors, and the wheel is healthy and in Xbox mode, the difference has to be
something the deduplicated summary could not show: a message we never send, one we send in the
wrong order, one we send the wrong number of times, or one WGI sends on a different handle.

"Same set of distinct bytes" was never the same claim as "same bytes". This tells them apart.

USAGE

    .\\.venv\\Scripts\\python.exe gip_trace.py  --dump logs\\wgi.txt      # force WORKS
    .\\.venv\\Scripts\\python.exe gip_direct.py --force --dump logs\\ours.txt   # force does NOT
    .\\.venv\\Scripts\\python.exe gip_diff.py logs\\wgi.txt logs\\ours.txt

Both dumps are one hex-encoded message per line, in the order they were written.
"""

import argparse
import sys
from collections import Counter

from evidence.gip_protocol import GIP_TYPES, decode_gip, describe_pairs


def load(path):
    out = []
    with open(path, "r", encoding="ascii") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(bytes.fromhex(line))
    return out


def label(payload):
    msg = decode_gip(payload)
    if msg is None:
        return "undecodable (%d bytes)" % len(payload)
    return "0x%02x %-16s len=%-4d" % (msg["type"], GIP_TYPES.get(msg["type"], "?"),
                                      msg["length"])


def type_of(payload):
    msg = decode_gip(payload)
    return msg["type"] if msg else None


def rule(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def compare_counts(theirs, ours):
    rule("Message types: how many of each")
    tc = Counter(type_of(p) for p in theirs)
    oc = Counter(type_of(p) for p in ours)
    print("  %-6s %-18s %8s %8s" % ("type", "meaning", "WGI", "ours"))
    missing = []
    for mtype in sorted(set(tc) | set(oc), key=lambda t: (t is None, t)):
        name = GIP_TYPES.get(mtype, "?") if mtype is not None else "undecodable"
        mark = "" if tc[mtype] and oc[mtype] else "   <-- ONLY ONE SIDE"
        print("  0x%02x   %-18s %8d %8d%s"
              % (mtype or 0, name, tc[mtype], oc[mtype], mark))
        if tc[mtype] and not oc[mtype]:
            missing.append(mtype)
    return missing


def compare_distinct(theirs, ours):
    """Payloads one side sends and the other never does -- the likeliest place for the answer."""
    rule("Distinct payloads only one side ever sends")
    ts = {p for p in theirs}
    os_ = {p for p in ours}
    only_wgi = ts - os_
    only_ours = os_ - ts

    for title, group in (("WGI sends, we never do", only_wgi),
                         ("we send, WGI never does", only_ours)):
        print("  %s: %d" % (title, len(group)))
        for payload in sorted(group)[:12]:
            msg = decode_gip(payload)
            print("    %s" % label(payload))
            if msg and msg["pairs"]:
                for line in describe_pairs(msg["pairs"], indent="        "):
                    print(line)
            elif msg:
                print("        body %s" % msg["body"].hex(" "))
        if len(group) > 12:
            print("    ... and %d more" % (len(group) - 12))
        print()
    return only_wgi


def compare_order(theirs, ours):
    """Where the two sequences first stop agreeing, by message type."""
    rule("First divergence in order")
    ts = [type_of(p) for p in theirs]
    os_ = [type_of(p) for p in ours]
    limit = min(len(ts), len(os_))
    for i in range(limit):
        if ts[i] != os_[i]:
            print("  Sequences agree for %d message(s), then differ at index %d:" % (i, i))
            lo = max(0, i - 3)
            for j in range(lo, min(limit, i + 4)):
                flag = "  <--" if j == i else ""
                print("    [%3d] WGI %-28s ours %-28s%s"
                      % (j, label(theirs[j]), label(ours[j]), flag))
            return i
    print("  The first %d message(s) are the same types in the same order." % limit)
    if len(ts) != len(os_):
        longer, n = ("WGI", len(ts)) if len(ts) > len(os_) else ("ours", len(os_))
        print("  %s continues for %d more message(s)." % (longer, n - limit))
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wgi", help="dump from gip_trace.py --dump (force works)")
    ap.add_argument("ours", help="dump from gip_direct.py --dump (force does not)")
    args = ap.parse_args()

    theirs = load(args.wgi)
    ours = load(args.ours)
    print("  WGI : %-40s %d message(s)" % (args.wgi, len(theirs)))
    print("  ours: %-40s %d message(s)" % (args.ours, len(ours)))

    missing_types = compare_counts(theirs, ours)
    only_wgi = compare_distinct(theirs, ours)
    compare_order(theirs, ours)

    rule("Verdict")
    if missing_types:
        print("  WGI sends message types we never send: %s"
              % ", ".join("0x%02x" % t for t in missing_types))
        print("  That is the first thing to replay.")
    elif only_wgi:
        print("  Same message types, but %d payload(s) WGI sends are never sent by us."
              % len(only_wgi))
        print("  Look at the parameter values above -- something in the load differs.")
    else:
        print("  We send every distinct payload WGI does. If force still does not reach the")
        print("  motor, the difference is NOT in these bytes: look at the other handle WGI")
        print("  opened, or at ordering/timing rather than content.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
