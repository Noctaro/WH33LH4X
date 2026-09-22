r"""
usbpcap_parse.py -- Read a USBPcap capture and pull out the GIP messages on the wire.

Everything this project knows about the wheel's arming came from `logs/wgi.txt`, which was
captured at the `\\.\XboxGIP` driver interface -- ABOVE the driver. That file therefore has
zero flags, zero sequence numbers and no timing, because the driver had not yet assigned them.
Every wire-level detail we reimplemented (options byte, sequence numbering, chunking of the
0x0d uploads, heartbeat state, pacing) was inferred from a layer that does not contain it.

This reads the real thing: interrupt transfers on the wheel's endpoints, as they actually
crossed the bus while Windows' own driver drove the motor.

Usage:
    python evidence/usbpcap_parse.py logs/wire.pcap                 # summary
    python evidence/usbpcap_parse.py logs/wire.pcap --out logs/wire_out.txt
    python evidence/usbpcap_parse.py logs/wire.pcap --device 7      # one device address

The --out file is one transmitted packet per line in hex, the same shape `--dump-sent` writes,
so the two can be diffed directly.
"""

import argparse
import struct
import sys

# USBPCAP_BUFFER_PACKET_HEADER, from USBPcap's USBPcapBuffer.h. Packed, little-endian.
#   u16 headerLen | u64 irpId | u32 status | u16 function | u8 info
#   u16 bus | u16 device | u8 endpoint | u8 transfer | u32 dataLength
USBPCAP_HEADER = struct.Struct("<HQIHBHHBBI")

TRANSFER_NAMES = {0: "isoc", 1: "interrupt", 2: "control", 3: "bulk"}
DLT_USBPCAP = 249


def read_pcap(path):
    """Yield (timestamp, payload) for each record. Handles both endiannesses and pcap-ng's absence."""
    with open(path, "rb") as handle:
        magic = handle.read(4)
        if magic == b"\x0a\x0d\x0d\x0a":
            raise SystemExit("this is a pcapng file; capture with USBPcapCMD -o (plain pcap) "
                             "or convert it first")
        if magic == b"\xa1\xb2\xc3\xd4":
            endian, scale = ">", 1000000      # microseconds
        elif magic == b"\xd4\xc3\xb2\xa1":
            endian, scale = "<", 1000000
        elif magic == b"\xa1\xb2\x3c\x4d":
            endian, scale = ">", 1000000000   # nanoseconds
        elif magic == b"\x4d\x3c\xb2\xa1":
            endian, scale = "<", 1000000000
        else:
            raise SystemExit("not a pcap file (magic %s)" % magic.hex())

        # The global header is 24 bytes: magic, major, minor, thiszone, sigfigs, snaplen,
        # linktype. The magic is already consumed, so read the remaining 20.
        _major, _minor, _zone, _sigfigs, _snaplen, linktype = struct.unpack(
            endian + "HHiIII", handle.read(20))
        if linktype != DLT_USBPCAP:
            print("warning: link type is %d, expected %d (DLT_USBPCAP) -- is this a USB capture?"
                  % (linktype, DLT_USBPCAP), file=sys.stderr)

        record = struct.Struct(endian + "IIII")
        while True:
            head = handle.read(16)
            if len(head) < 16:
                return
            seconds, fraction, captured, _original = record.unpack(head)
            data = handle.read(captured)
            if len(data) < captured:
                return
            yield seconds + fraction / scale, data


def decode(payload):
    """One USBPcap record -> a dict, or None if it is too short to be one."""
    if len(payload) < USBPCAP_HEADER.size:
        return None
    (header_len, irp, status, function, info, bus, device,
     endpoint, transfer, data_length) = USBPCAP_HEADER.unpack_from(payload)
    if header_len > len(payload):
        return None
    return {
        "irp": irp, "status": status, "function": function,
        # info bit 0 set means the IRP is travelling PDO -> FDO, i.e. completing.
        "complete": bool(info & 0x01),
        "bus": bus, "device": device,
        "endpoint": endpoint, "in": bool(endpoint & 0x80),
        "transfer": transfer, "transfer_name": TRANSFER_NAMES.get(transfer, "?"),
        "data": payload[header_len:header_len + data_length],
    }


def gip_summary(data):
    """Best-effort GIP decode of a wire payload, for the human reading the summary."""
    if len(data) < 4:
        return ""
    command, options, sequence = data[0], data[1], data[2]
    index, length, shift = 3, 0, 0
    while index < len(data):
        byte = data[index]
        length |= (byte & 0x7F) << shift
        index += 1
        shift += 7
        if not byte & 0x80:
            break
    return "cmd=0x%02x opt=0x%02x seq=%3d len=%3d" % (command, options, sequence, length)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("capture", help="the .pcap written by USBPcapCMD")
    parser.add_argument("--device", type=int,
                        help="only this USB device address (find it in the summary first)")
    parser.add_argument("--endpoint", type=lambda v: int(v, 0),
                        help="only this endpoint, e.g. 0x01")
    parser.add_argument("--out", metavar="FILE",
                        help="write host->device payloads as hex, one per line, for diffing "
                             "against --dump-sent")
    parser.add_argument("--max", type=int, default=60, help="how many packets to print")
    args = parser.parse_args()

    devices = {}
    kept = []
    first = None
    for stamp, payload in read_pcap(args.capture):
        record = decode(payload)
        if record is None:
            continue
        if first is None:
            first = stamp
        record["t"] = stamp - first
        key = (record["bus"], record["device"])
        devices.setdefault(key, {"count": 0, "endpoints": set()})
        devices[key]["count"] += 1
        devices[key]["endpoints"].add((record["endpoint"], record["transfer_name"]))
        if args.device is not None and record["device"] != args.device:
            continue
        if args.endpoint is not None and record["endpoint"] != args.endpoint:
            continue
        # A URB appears twice, going down and completing. For OUT transfers the data is on the
        # way down; taking both would double every packet.
        if record["data"] and not (record["complete"] and not record["in"]):
            kept.append(record)

    print("=== devices seen ===")
    for (bus, device), info in sorted(devices.items()):
        endpoints = ", ".join("0x%02x %s" % ep for ep in sorted(info["endpoints"]))
        print("  bus %d device %-3d  %5d packets   %s" % (bus, device, info["count"], endpoints))

    if args.device is None:
        print("\nPick one with --device N to see its traffic.")
        return 0

    print("\n=== %d packet(s) with data ===" % len(kept))
    for record in kept[:args.max]:
        print("  %8.3fs  %s  ep 0x%02x  %-9s  %s  %s"
              % (record["t"], "IN " if record["in"] else "OUT", record["endpoint"],
                 record["transfer_name"], gip_summary(record["data"]),
                 record["data"][:24].hex()))
    if len(kept) > args.max:
        print("  ... and %d more" % (len(kept) - args.max))

    if args.out:
        outgoing = [r for r in kept if not r["in"]]
        with open(args.out, "w", encoding="ascii") as handle:
            for record in outgoing:
                handle.write("%s\n" % record["data"].hex())
        print("\nwrote %d host->device payload(s) to %s" % (len(outgoing), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
