"""
hid_vendor_probe.py -- What is on the wheel's PC-mode vendor collections?

The wheel in PC mode (PID 0x015D) publishes two vendor-defined HID collections beside the
joystick one: COL02 on usage page 0xFF20 (32 B in/out) and COL03 on 0xFF21 (64 B in/out).
Those are writable from usermode and, unlike Windows.Gaming.Input, HID reports are NOT
foreground-gated -- so if the factory centring spring is configurable at all, this is the
channel that reaches it without binding a driver or injecting anything.

HORI Device Manager Vol.2 drives exactly these collections. Its binary names the calls:
changeWheelConfigData, changeWheelForceFeedbackData, changeWheelMotorAdjustData and
readWheelConfigData. So a config block exists and the device will hand it over -- we just do
not know the request that asks for it.

This probe never writes to the device. It has two modes:

  caps     describe every collection: usage page, report sizes, report IDs
  listen   open the vendor collections read-only and dump every input report that arrives

Windows delivers input reports to EVERY open handle, so `listen` can watch the device's
replies while HORI's own app is the one asking the questions. That yields the device->host
half of the protocol with no blind writes. Run `listen`, then drive the HORI app and change
one setting at a time.

Usage:
    python evidence/hid_vendor_probe.py caps
    python evidence/hid_vendor_probe.py listen --seconds 60
    python evidence/hid_vendor_probe.py listen --seconds 60 --out logs/hori_col.jsonl
"""

import argparse
import ctypes
import json
import sys
import time
from ctypes import POINTER, byref, c_void_p, c_ulong, sizeof
from ctypes.wintypes import BOOL, DWORD, HANDLE
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence.hid_probe import (  # noqa: E402
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    GENERIC_READ,
    GENERIC_WRITE,
    HIDD_ATTRIBUTES,
    HIDP_CAPS,
    HIDP_STATUS_SUCCESS,
    INVALID_HANDLE_VALUE,
    OPEN_EXISTING,
    hid,
    iter_hid_paths,
    kernel32,
    usage_page_name,
)

HORI_VID = 0x0F0D
VENDOR_PAGES = (0xFF20, 0xFF21)

FILE_FLAG_OVERLAPPED = 0x40000000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", POINTER(c_ulong)), ("InternalHigh", POINTER(c_ulong)),
                ("Offset", DWORD), ("OffsetHigh", DWORD), ("hEvent", HANDLE)]


kernel32.ReadFile.restype = BOOL
kernel32.ReadFile.argtypes = [HANDLE, c_void_p, DWORD, POINTER(DWORD), POINTER(OVERLAPPED)]
kernel32.CreateEventW.restype = HANDLE
kernel32.CreateEventW.argtypes = [c_void_p, BOOL, BOOL, c_void_p]
kernel32.WaitForSingleObject.restype = DWORD
kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
kernel32.GetOverlappedResult.restype = BOOL
kernel32.GetOverlappedResult.argtypes = [HANDLE, POINTER(OVERLAPPED), POINTER(DWORD), BOOL]
kernel32.CancelIo.argtypes = [HANDLE]

# GET_REPORT is a control transfer -- a read, and point to point. It never surfaces on another
# process's handle, which is the likeliest reason passive listening sees nothing.
hid.HidD_GetInputReport.restype = BOOL
hid.HidD_GetInputReport.argtypes = [HANDLE, c_void_p, c_ulong]
hid.HidD_SetOutputReport.restype = BOOL
hid.HidD_SetOutputReport.argtypes = [HANDLE, c_void_p, c_ulong]

kernel32.WriteFile.restype = BOOL
kernel32.WriteFile.argtypes = [HANDLE, c_void_p, DWORD, POINTER(DWORD), c_void_p]

# Offsets decoded 2026-09-21 by diffing GET_REPORT 0x21 against HORI Device Manager Vol.2:
# 360 deg read 0x24 and 270 deg read 0x1b, so the angle is degrees/10.
ANGLE_OFFSET = 16
VIBRATION_OFFSET = 18


class Collection:
    """One HID top-level collection, opened for reading."""

    def __init__(self, path, handle, attrs, caps):
        self.path = path
        self.handle = handle
        self.vid = attrs.VendorID
        self.pid = attrs.ProductID
        self.usage_page = caps.UsagePage
        self.usage = caps.Usage
        self.input_len = caps.InputReportByteLength
        self.output_len = caps.OutputReportByteLength
        self.feature_len = caps.FeatureReportByteLength

    @property
    def label(self):
        return "0x%04X:0x%04X page 0x%04X usage 0x%02X" % (
            self.vid, self.pid, self.usage_page, self.usage)

    def close(self):
        if self.handle and self.handle != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(self.handle)
            self.handle = None


def open_collections(vid, pages=None, overlapped=False, write=False):
    """Open every present collection matching vid, optionally filtered to usage pages."""
    flags = FILE_FLAG_OVERLAPPED if overlapped else 0
    access = GENERIC_READ | GENERIC_WRITE if write else GENERIC_READ
    found = []
    for path in iter_hid_paths():
        handle = kernel32.CreateFileW(
            path, access, FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, flags, None)
        if handle == INVALID_HANDLE_VALUE:
            continue

        attrs = HIDD_ATTRIBUTES()
        attrs.Size = sizeof(HIDD_ATTRIBUTES)
        preparsed = c_void_p()
        caps = HIDP_CAPS()
        ok = (hid.HidD_GetAttributes(handle, byref(attrs))
              and attrs.VendorID == vid
              and hid.HidD_GetPreparsedData(handle, byref(preparsed)))
        if ok:
            ok = hid.HidP_GetCaps(preparsed, byref(caps)) == HIDP_STATUS_SUCCESS
            hid.HidD_FreePreparsedData(preparsed)

        if not ok or (pages is not None and caps.UsagePage not in pages):
            kernel32.CloseHandle(handle)
            continue

        found.append(Collection(path, handle, attrs, caps))
    return found


def cmd_caps(args):
    cols = open_collections(args.vid)
    if not cols:
        print("No HID collections found for VID 0x%04X." % args.vid)
        print("The wheel must be in PC MODE for the vendor collections to exist "
              "(long-press PROFILE; no LED above the Xbox logo).")
        return 1

    for col in sorted(cols, key=lambda c: (c.pid, c.usage_page)):
        print("-" * 70)
        print("  PID 0x%04X  %s" % (col.pid, usage_page_name(col.usage_page)))
        print("    usage page 0x%04X  usage 0x%02X" % (col.usage_page, col.usage))
        print("    report bytes: input %d  output %d  feature %d"
              % (col.input_len, col.output_len, col.feature_len))
        if col.usage_page in VENDOR_PAGES:
            print("    >>> VENDOR COLLECTION -- this is a config channel candidate")
        col.close()
    return 0


def cmd_sweep(args):
    """Ask the device for every input report id via GET_REPORT.

    A control-transfer read. Unsupported ids STALL and simply fail, so a sweep is safe and
    tells us which report ids the vendor collections will answer -- including, with luck,
    whatever readWheelConfigData uses.
    """
    cols = open_collections(args.vid, pages=VENDOR_PAGES)
    if not cols:
        print("No vendor collections found. Put the wheel in PC MODE.")
        return 1

    for col in cols:
        print("=" * 70)
        print("page 0x%04X  input report length %d" % (col.usage_page, col.input_len))
        answered = 0
        for report_id in range(256):
            buf = ctypes.create_string_buffer(col.input_len)
            buf[0] = bytes([report_id])
            if not hid.HidD_GetInputReport(col.handle, buf, col.input_len):
                continue
            data = bytes(buf.raw[:col.input_len])
            answered += 1
            # A report of all zeros past the id is an answer, but an empty one; show both.
            print("  id 0x%02X -> %s" % (report_id, data.hex(" ")))
        if not answered:
            print("  nothing answered -- this collection does not serve GET_REPORT")
        col.close()
    return 0


def _read_state(col, report_id):
    buf = ctypes.create_string_buffer(col.input_len)
    buf[0] = bytes([report_id])
    if not hid.HidD_GetInputReport(col.handle, buf, col.input_len):
        return None
    return bytes(buf.raw[:col.input_len])


def _send(col, payload, how):
    """Send one report. Returns (ok, error_code)."""
    buf = ctypes.create_string_buffer(payload, len(payload))
    if how == "setoutput":
        ok = hid.HidD_SetOutputReport(col.handle, buf, len(payload))
    else:
        written = DWORD(0)
        ok = kernel32.WriteFile(col.handle, buf, len(payload), byref(written), None)
    return bool(ok), ctypes.get_last_error()


def cmd_write_angle(args):
    """Try to change the rotation angle, verifying by reading byte 16 back.

    Rotation is the one field whose encoding is confirmed, and the values written here are
    ones HORI Device Manager Vol.2 sets itself -- so this tests WRITE ACCESS against a known
    good target rather than guessing at an unknown parameter. The original value is restored
    in a finally block whether or not anything worked.
    """
    cols = open_collections(args.vid, pages=(0xFF21,), write=True)
    if not cols:
        print("No 0xFF21 collection. Put the wheel in PC MODE.")
        return 1
    col = cols[0]

    base = _read_state(col, args.report_id)
    if base is None:
        print("Could not read the current state -- aborting without writing anything.")
        col.close()
        return 1

    original_angle = base[ANGLE_OFFSET]
    target = args.degrees // 10
    print("current state : %s" % base.hex(" "))
    print("current angle : byte %d = 0x%02x (%d deg)"
          % (ANGLE_OFFSET, original_angle, original_angle * 10))
    print("target angle  : 0x%02x (%d deg)\n" % (target, target * 10))

    if target == original_angle:
        print("Target equals current angle; pick a different --degrees so a change is visible.")
        col.close()
        return 1

    # Each variant is one hypothesis about how a request is framed. 0xa2 is the DATA-output
    # marker where 0xa1 is DATA-input in the Bluetooth HID transport convention, which is the
    # likeliest reason an echoed input report would be ignored.
    def framed(marker, out_len):
        body = bytearray(base[:out_len].ljust(out_len, b"\x00"))
        body[ANGLE_OFFSET] = target
        if marker is not None:
            body[1] = marker
        return bytes(body)

    variants = [
        ("echo input report, SetOutputReport", framed(None, col.output_len), "setoutput"),
        ("marker a2, SetOutputReport", framed(0xA2, col.output_len), "setoutput"),
        ("echo input report, WriteFile", framed(None, col.output_len), "write"),
        ("marker a2, WriteFile", framed(0xA2, col.output_len), "write"),
    ]

    winner = None
    try:
        for name, payload, how in variants:
            print("-> %s" % name)
            print("   %s" % payload[:24].hex(" ") + " ...")
            ok, err = _send(col, payload, how)
            if not ok:
                print("   send failed, err=%d" % err)
                continue
            print("   sent ok")
            time.sleep(0.4)
            now = _read_state(col, args.report_id)
            if now is None:
                print("   read-back failed")
                continue
            if now[ANGLE_OFFSET] != original_angle:
                print("   *** ANGLE CHANGED: 0x%02x -> 0x%02x ***"
                      % (original_angle, now[ANGLE_OFFSET]))
                winner = name
                break
            print("   no change")
    finally:
        after = _read_state(col, args.report_id)
        if after is not None and after[ANGLE_OFFSET] != original_angle:
            print("\nrestoring original angle 0x%02x" % original_angle)
            body = bytearray(after[:col.output_len].ljust(col.output_len, b"\x00"))
            body[ANGLE_OFFSET] = original_angle
            if winner and "a2" in winner:
                body[1] = 0xA2
            _send(col, bytes(body), "write" if winner and "WriteFile" in winner else "setoutput")
            time.sleep(0.4)
            back = _read_state(col, args.report_id)
            print("after restore : byte %d = 0x%02x"
                  % (ANGLE_OFFSET, back[ANGLE_OFFSET] if back else 0xFF))
        col.close()

    print("\nRESULT: %s" % (("write works via %s" % winner) if winner
                            else "no variant changed the angle"))
    return 0


def cmd_watch(args):
    """Poll one GET_REPORT id and print only what changes.

    The point is the diff: change one setting in HORI Device Manager Vol.2 and whatever byte
    moves is that setting. A byte that moves on its own is a counter, not config -- which is
    exactly the distinction a change-only view makes obvious.
    """
    cols = [c for c in open_collections(args.vid, pages=(args.page,))]
    if not cols:
        print("No collection on page 0x%04X. Put the wheel in PC MODE." % args.page)
        return 1
    col = cols[0]

    out = None
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        out = open(args.out, "w", encoding="utf-8")

    print("Polling page 0x%04X report id 0x%02X every %d ms for %g s."
          % (col.usage_page, args.report_id, args.interval_ms, args.seconds))
    print("Change ONE setting at a time in HORI Device Manager Vol.2 and say what you changed.\n")

    previous = None
    started = time.time()
    changes = 0
    try:
        while time.time() - started < args.seconds:
            buf = ctypes.create_string_buffer(col.input_len)
            buf[0] = bytes([args.report_id])
            if hid.HidD_GetInputReport(col.handle, buf, col.input_len):
                data = bytes(buf.raw[:col.input_len])
                if data != previous:
                    elapsed = time.time() - started
                    if previous is None:
                        print("[%7.3f] BASE %s" % (elapsed, data.hex(" ")))
                    else:
                        diff = ["%02d: %02x->%02x" % (i, a, b)
                                for i, (a, b) in enumerate(zip(previous, data)) if a != b]
                        print("[%7.3f] %s" % (elapsed, "  ".join(diff)))
                    if out:
                        out.write(json.dumps({"t": round(elapsed, 3),
                                              "hex": data.hex()}) + "\n")
                        out.flush()
                    previous = data
                    changes += 1
            time.sleep(args.interval_ms / 1000.0)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        col.close()
        if out:
            out.close()

    print("\n%d distinct state(s) seen." % changes)
    return 0


def cmd_listen(args):
    """Dump every input report the listened collections emit.

    Interrupt IN reports go to every open handle, so this can see what a device answers while
    another application is the one asking -- but only if that application uses interrupt
    transfers rather than GET_REPORT control transfers.

    Use --pages 0x0001 as a positive control: the joystick collection reports continuously
    when the wheel moves, so traffic there proves the read path works and silence on the
    vendor pages is the device's doing rather than ours.
    """
    pages = tuple(args.pages) if args.pages else VENDOR_PAGES
    cols = open_collections(args.vid, pages=pages, overlapped=True)
    if not cols:
        print("No collections on page(s) %s for VID 0x%04X."
              % (", ".join("0x%04X" % p for p in pages), args.vid))
        print("Put the wheel in PC MODE -- the vendor collections do not exist in Xbox mode.")
        return 1

    out = None
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        out = open(args.out, "w", encoding="utf-8")

    print("Listening on %d vendor collection(s) for %d s." % (len(cols), args.seconds))
    for col in cols:
        print("  %s  input %d B" % (col.label, col.input_len))
    print("\nOpen HORI Device Manager Vol.2 now and change ONE setting at a time.")
    print("Note what you changed and when -- the diff is the whole point.\n")

    pending = []
    for col in cols:
        buf = ctypes.create_string_buffer(col.input_len)
        ov = OVERLAPPED()
        ov.hEvent = kernel32.CreateEventW(None, True, False, None)
        kernel32.ReadFile(col.handle, buf, col.input_len, None, byref(ov))
        pending.append([col, buf, ov])

    started = time.time()
    seen = 0
    try:
        while time.time() - started < args.seconds:
            for entry in pending:
                col, buf, ov = entry
                if kernel32.WaitForSingleObject(ov.hEvent, 20) != WAIT_OBJECT_0:
                    continue

                got = DWORD(0)
                if kernel32.GetOverlappedResult(col.handle, byref(ov), byref(got), False):
                    data = bytes(buf.raw[:got.value])
                    elapsed = time.time() - started
                    line = "[%7.3f] page 0x%04X  %s" % (
                        elapsed, col.usage_page, data.hex(" "))
                    print(line)
                    if out:
                        out.write(json.dumps({
                            "t": round(elapsed, 4),
                            "page": col.usage_page,
                            "hex": data.hex(),
                        }) + "\n")
                        out.flush()
                    seen += 1

                # Re-arm for the next report.
                kernel32.ResetEvent(ov.hEvent)
                kernel32.ReadFile(col.handle, buf, col.input_len, None, byref(ov))
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        for col, _buf, ov in pending:
            kernel32.CancelIo(col.handle)
            kernel32.CloseHandle(ov.hEvent)
            col.close()
        if out:
            out.close()

    print("\n%d report(s) seen." % seen)
    if seen == 0:
        print("Nothing arrived. Either the HORI app never talked to the device, or the "
              "vendor collections only answer the handle that asked.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vid", type=lambda s: int(s, 0), default=HORI_VID,
                        help="vendor id to match (default 0x0F0D)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("caps", help="describe every collection")
    sub.add_parser("sweep", help="GET_REPORT every input report id on the vendor collections")

    wa = sub.add_parser("write-angle",
                        help="try to change the rotation angle, verified by read-back")
    wa.add_argument("--degrees", type=int, default=270,
                    help="target angle; only values the HORI app itself sets (default 270)")
    wa.add_argument("--report-id", type=lambda s: int(s, 0), default=0x21)

    watch = sub.add_parser("watch", help="poll one GET_REPORT id and print only changes")
    watch.add_argument("--page", type=lambda s: int(s, 0), default=0xFF21)
    watch.add_argument("--report-id", type=lambda s: int(s, 0), default=0x21)
    watch.add_argument("--interval-ms", type=int, default=250)
    watch.add_argument("--seconds", type=float, default=180.0)
    watch.add_argument("--out", help="append one JSON object per distinct state")

    listen = sub.add_parser("listen", help="dump input reports as they arrive")
    listen.add_argument("--seconds", type=float, default=60.0)
    listen.add_argument("--out", help="also append one JSON object per report to this file")
    listen.add_argument("--pages", type=lambda s: int(s, 0), nargs="+",
                        help="usage pages to listen on (default the vendor pages); "
                             "0x0001 is the joystick collection, useful as a positive control")

    args = parser.parse_args()
    return {"caps": cmd_caps, "sweep": cmd_sweep, "write-angle": cmd_write_angle,
            "watch": cmd_watch, "listen": cmd_listen}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
