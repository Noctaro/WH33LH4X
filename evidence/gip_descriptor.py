"""
gip_descriptor.py -- Is there a HID report descriptor tunnelled inside the GIP device descriptor?

GIP tunnels HID. The Linux xbox_gip work (Vicki Pfau, based on Microsoft's GIP documentation)
states it plainly: "GIP allows tunneling of HID packets, with the HID descriptor embedded in the
GIP metadata exchanged during the initial handshake."

That matters because `hid_probe.py` correctly found no USB PID (usage page 0x0F) collection on
this wheel -- it was looking at the USB HID layer, and the descriptor is one level up, inside the
GIP type 0x04 device descriptor message. If that descriptor contains a PID collection, the wheel
is an ordinary HID force-feedback device and its force reports are a documented standard rather
than a HORI-specific format.

Windows never surfaces it: the HID child `dc1-controller` creates (`...&IG_00`) reports
input 0 / output 0 / feature 0 bytes, so DirectInput sees nothing to drive.

The type 0x04 body opens with a block offset table. Measured on this wheel from
`logs/gip_trace_20260823_063145.log:727`:

    0d 0f | 5c 01 | 10 00 01 00 | <10 zeros>
     VID     PID
    c1 01  ca 00  16 00  1b 00  1c 00  23 00  29 00  89 00
     449    202     22     27     28     35     41    137

Sorted those are 22, 27, 28, 35, 41, 137, 202, 449 -- ascending and ending at the message length,
which is what a table of block offsets looks like.

Usage:
    python evidence/gip_descriptor.py --dump logs/wgi_reads.txt
    python evidence/gip_descriptor.py --hex "0d0f5c01..."      # one message, for spot checks
    python evidence/gip_descriptor.py --dump ... --scan        # ignore the table, brute force
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence.gip_protocol import decode_gip  # noqa: E402

GIP_DEVICE_DESCRIPTOR = 0x04

# HID 1.11 section 6.2.2. A short item's size field encodes 0, 1, 2 or 4 bytes -- never 3.
ITEM_SIZES = (0, 1, 2, 4)
LONG_ITEM_PREFIX = 0xFE

MAIN, GLOBAL, LOCAL = 0, 1, 2

MAIN_TAGS = {0x8: "Input", 0x9: "Output", 0xA: "Collection",
             0xB: "Feature", 0xC: "End Collection"}
GLOBAL_TAGS = {0x0: "Usage Page", 0x1: "Logical Minimum", 0x2: "Logical Maximum",
               0x3: "Physical Minimum", 0x4: "Physical Maximum", 0x5: "Unit Exponent",
               0x6: "Unit", 0x7: "Report Size", 0x8: "Report ID", 0x9: "Report Count",
               0xA: "Push", 0xB: "Pop"}
LOCAL_TAGS = {0x0: "Usage", 0x1: "Usage Minimum", 0x2: "Usage Maximum",
              0x3: "Designator Index", 0x4: "Designator Minimum", 0x5: "Designator Maximum",
              0x7: "String Index", 0x8: "String Minimum", 0x9: "String Maximum",
              0xA: "Delimiter"}

USAGE_PAGES = {
    0x01: "Generic Desktop",
    0x02: "Simulation Controls",
    0x06: "Generic Device Controls",
    0x07: "Keyboard/Keypad",
    0x08: "LEDs",
    0x09: "Button",
    0x0C: "Consumer",
    0x0F: "PHYSICAL INTERFACE DEVICE (force feedback)",
}

PID_USAGE_PAGE = 0x0F


class Item:
    __slots__ = ("offset", "kind", "tag", "value", "size")

    def __init__(self, offset, kind, tag, value, size):
        self.offset = offset
        self.kind = kind
        self.tag = tag
        self.value = value
        self.size = size

    @property
    def name(self):
        table = {MAIN: MAIN_TAGS, GLOBAL: GLOBAL_TAGS, LOCAL: LOCAL_TAGS}.get(self.kind, {})
        return table.get(self.tag, "tag 0x%X (type %d)" % (self.tag, self.kind))


def parse_items(data):
    """Walk a HID report descriptor. Raises ValueError on anything malformed."""
    items = []
    i = 0
    while i < len(data):
        prefix = data[i]
        if prefix == LONG_ITEM_PREFIX:
            # Long items are legal but essentially unused; refuse rather than guess.
            raise ValueError("long item at offset %d" % i)
        size = ITEM_SIZES[prefix & 0x03]
        kind = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        if kind == 3:
            raise ValueError("reserved item type at offset %d" % i)
        if i + 1 + size > len(data):
            raise ValueError("item at offset %d runs past the end" % i)
        raw = data[i + 1:i + 1 + size]
        items.append(Item(i, kind, tag, int.from_bytes(raw, "little"), size))
        i += 1 + size
    return items


def looks_like_descriptor(items):
    """A real descriptor opens a collection, closes every one it opens, and says Usage Page."""
    if len(items) < 8:
        return False
    depth = 0
    opened = False
    for item in items:
        if item.kind != MAIN:
            continue
        if item.tag == 0xA:
            depth += 1
            opened = True
        elif item.tag == 0xC:
            depth -= 1
            if depth < 0:
                return False
    if not opened or depth != 0:
        return False
    return any(i.kind == GLOBAL and i.tag == 0x0 for i in items)


def summarise(items):
    pages, report_ids, collections, inputs, outputs, features = set(), set(), 0, 0, 0, 0
    for item in items:
        if item.kind == GLOBAL and item.tag == 0x0:
            pages.add(item.value)
        elif item.kind == GLOBAL and item.tag == 0x8:
            report_ids.add(item.value)
        elif item.kind == MAIN:
            if item.tag == 0xA:
                collections += 1
            elif item.tag == 0x8:
                inputs += 1
            elif item.tag == 0x9:
                outputs += 1
            elif item.tag == 0xB:
                features += 1
    return {"pages": pages, "report_ids": report_ids, "collections": collections,
            "inputs": inputs, "outputs": outputs, "features": features,
            "items": len(items)}


def report(block_name, data):
    """Try to read one byte range as a HID report descriptor; return True if it parsed."""
    try:
        items = parse_items(data)
    except ValueError as exc:
        print("    %-22s %d B -- not a descriptor (%s)" % (block_name, len(data), exc))
        return False

    if not looks_like_descriptor(items):
        print("    %-22s %d B -- parses, but no balanced collection tree" % (block_name, len(data)))
        return False

    info = summarise(items)
    print("    %-22s %d B -- %d items, %d collection(s), "
          "in/out/feature %d/%d/%d"
          % (block_name, len(data), info["items"], info["collections"],
             info["inputs"], info["outputs"], info["features"]))
    for page in sorted(info["pages"]):
        marker = "  <<< FORCE FEEDBACK" if page == PID_USAGE_PAGE else ""
        print("        usage page 0x%02X  %s%s"
              % (page, USAGE_PAGES.get(page, "unknown"), marker))
    if info["report_ids"]:
        print("        report ids: %s"
              % ", ".join("0x%02X" % r for r in sorted(info["report_ids"])))
    return True


def block_offsets(body):
    """Read the ascending u16 offset table that opens the type 0x04 body.

    The table starts after the 18-byte prologue (VID, PID, two u16s, ten zero bytes) and runs
    while the values ascend and stay inside the message. The first entry is the largest on this
    wheel (449 against 202, 22, ...), so it is treated as the end marker rather than a block.
    """
    values = []
    i = 18
    while i + 2 <= len(body):
        value = int.from_bytes(body[i:i + 2], "little")
        if value == 0 or value > len(body):
            break
        values.append(value)
        i += 2
    if not values:
        return []
    end = max(values)
    starts = sorted(v for v in values if v != end)
    return [(s, e) for s, e in zip(starts, starts[1:] + [end])]


def scan(body):
    """Brute force: the longest clean descriptor parse starting anywhere in the body."""
    best = None
    for start in range(len(body)):
        for finish in range(len(body), start + 8, -1):
            chunk = body[start:finish]
            try:
                items = parse_items(chunk)
            except ValueError:
                continue
            if looks_like_descriptor(items) and (best is None or len(chunk) > len(best[1])):
                best = (start, chunk)
            break
    return best


def examine(payload, do_scan):
    gip = decode_gip(payload)
    if not gip:
        print("  not a GIP message (%d bytes)" % len(payload))
        return False
    if gip["type"] != GIP_DEVICE_DESCRIPTOR:
        return False

    body = gip["body"]
    print("\n  type 0x%02X device descriptor, body %d B" % (gip["type"], len(body)))
    print("    VID 0x%04X  PID 0x%04X"
          % (int.from_bytes(body[0:2], "little"), int.from_bytes(body[2:4], "little")))

    blocks = block_offsets(body)
    if blocks:
        print("    block table: %s"
              % ", ".join("%d..%d (%d B)" % (s, e, e - s) for s, e in blocks))
        found = False
        for start, finish in sorted(blocks, key=lambda b: b[1] - b[0], reverse=True):
            if report("block @%d" % start, body[start:finish]):
                found = True
        if found:
            return True
        print("    no block parsed as a HID report descriptor")
    else:
        print("    no block offset table recognised")

    if do_scan:
        print("    brute-force scan...")
        best = scan(body)
        if best:
            print("    longest clean parse starts at offset %d" % best[0])
            return report("scan @%d" % best[0], best[1])
        print("    scan found nothing that parses as a descriptor")
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dump", help="hex dump from gip_trace.py --dump-reads")
    source.add_argument("--hex", help="one message as a hex string")
    parser.add_argument("--scan", action="store_true",
                        help="also brute force, ignoring the block offset table")
    args = parser.parse_args()

    if args.hex:
        payloads = [bytes.fromhex(args.hex.replace(" ", ""))]
    else:
        payloads = [bytes.fromhex(line.strip())
                    for line in Path(args.dump).read_text(encoding="ascii").splitlines()
                    if line.strip()]

    print("%d message(s) to examine" % len(payloads))
    hits = sum(1 for p in payloads if examine(p, args.scan))
    if not hits:
        print("\nNo HID report descriptor found in any type 0x04 message.")
        return 1
    print("\nFound a HID report descriptor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
