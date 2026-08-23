"""
Talk to the GIP driver directly, with no Windows.Gaming.Input in the process at all.

WHY THIS EXISTS

Track D assumes the bridge must be injected into the game, because WGI gates both force output
and position reading on the calling process being in the foreground. That gate is real and was
measured A->B->A. But it was measured *through WGI*. The GIP-on-Windows writeup states the
focus requirement for INPUT REPORTS, and explicitly not for other message types -- which
raises a question nobody here has answered: is the gate in the driver, or in WGI?

`gip_trace.py` captured everything needed to ask directly. WGI opens `\\\\.\\XboxGIP` and drives
this wheel with plain WriteFile. So this opens the same device on its own handle and replays
those bytes. The answer decides how much of Track D needs to exist:

  * writes gated       -> the shim still needs to live inside the game, but it is
                          CreateFileW + WriteFile instead of several hundred lines of WinRT
                          vtable C. D2 shrinks, D3/D4 stand.
  * writes ungated     -> force needs no injection at all. Only the position read does.
  * both ungated       -> Track D collapses entirely. `vjoy_bridge.py` opens the device
                          itself and there is no shim, no shared memory, no injection.

Do not assume which. This project has already recorded one wrong conclusion drawn from an
aggregate over a window whose conditions changed; the `--background` mode here is A->B->A for
exactly that reason.

WHAT IS REPLAYED, AND HOW MUCH OF IT IS UNDERSTOOD

The force parameter is understood: one float32 in -1..+1, verified across five magnitudes. The
186-byte table uploaded before it is NOT understood -- it is replayed byte-for-byte as
captured. See `gip_protocol.py`, which marks each item verified or inferred.

Usage:
    .\\.venv\\Scripts\\python.exe gip_direct.py                  # open, learn the device, listen
    .\\.venv\\Scripts\\python.exe gip_direct.py --force          # replay the force sequence
    .\\.venv\\Scripts\\python.exe gip_direct.py --background     # A->B->A: is output gated?
"""

import argparse
import ctypes
import sys
import time
from ctypes import POINTER, byref, c_void_p, c_wchar_p
from ctypes.wintypes import BOOL, DWORD, HANDLE, LPVOID

import probe_log as log
from gip_protocol import (
    FFB_TABLE,
    GIP_TYPES,
    HORI_PID,
    HORI_VID,
    SHORT_CMD,
    STATE_LOADED,
    STATE_RUNNING,
    announce_ids,
    decode_gip,
    encode_gip,
    force_block,
    init_param_blocks,
    table_chunks,
)

GIP_PATH = r"\\.\XboxGIP"

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

WAIT_TIMEOUT = 0x00000102
ERROR_IO_PENDING = 997

# GIP_ADD_REENUMERATE_CALLER_CONTEXT, from the GIP-on-Windows writeup:
# CTL_CODE(0x4000, 0x734, METHOD_BUFFERED, FILE_ANY_ACCESS) == (0x4000 << 16) | (0x734 << 2).
#
# Without this the driver never re-announces devices to a freshly opened handle, so no type
# 0x02 arrives and the device id cannot be learned. That is exactly what the first run saw:
# twelve reads at twelve buffer sizes, all left pending, no errors -- the handle was fine and
# there was simply nothing queued for it.
#
# WGI was never seen calling this, but the trace only hooked kernel32's DeviceIoControl; a
# call through ntdll's NtDeviceIoControlFile would not appear. Absence there is not evidence.
GIP_ADD_REENUMERATE_CALLER_CONTEXT = (0x4000 << 16) | (0x734 << 2)

# The codes worth naming inline, because each points somewhere different: 5 means elevation,
# 32 means someone else holds it, 87/122 mean the request shape is wrong rather than refused,
# and 1167 means the wheel is not on the GIP bus at all (PC mode, most likely).
WIN_ERRORS = {
    1: "ERROR_INVALID_FUNCTION -- the device does not support this operation",
    5: "ERROR_ACCESS_DENIED",
    6: "ERROR_INVALID_HANDLE",
    22: "ERROR_BAD_COMMAND",
    31: "ERROR_GEN_FAILURE",
    32: "ERROR_SHARING_VIOLATION",
    87: "ERROR_INVALID_PARAMETER",
    122: "ERROR_INSUFFICIENT_BUFFER -- buffer too small for one message",
    995: "ERROR_OPERATION_ABORTED",
    1167: "ERROR_DEVICE_NOT_CONNECTED",
}

# An input report is 49 bytes and the descriptor 453; a page is comfortably clear of both.
READ_BUFFER = 4096

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = HANDLE
kernel32.CreateFileW.argtypes = [c_wchar_p, DWORD, DWORD, LPVOID, DWORD, DWORD, HANDLE]
kernel32.CreateEventW.restype = HANDLE
kernel32.CreateEventW.argtypes = [LPVOID, BOOL, BOOL, c_wchar_p]
kernel32.WaitForSingleObject.restype = DWORD
kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
kernel32.CloseHandle.argtypes = [HANDLE]
kernel32.CancelIoEx.argtypes = [HANDLE, LPVOID]
kernel32.WaitForMultipleObjects.restype = DWORD
kernel32.WaitForMultipleObjects.argtypes = [DWORD, LPVOID, BOOL, DWORD]
kernel32.ResetEvent.argtypes = [HANDLE]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", c_void_p), ("InternalHigh", c_void_p),
                ("Offset", DWORD), ("OffsetHigh", DWORD), ("hEvent", HANDLE)]


kernel32.ReadFile.argtypes = [HANDLE, LPVOID, DWORD, POINTER(DWORD), POINTER(OVERLAPPED)]
kernel32.WriteFile.argtypes = [HANDLE, LPVOID, DWORD, POINTER(DWORD), POINTER(OVERLAPPED)]
kernel32.GetOverlappedResult.argtypes = [HANDLE, POINTER(OVERLAPPED), POINTER(DWORD), BOOL]
kernel32.DeviceIoControl.restype = BOOL
kernel32.DeviceIoControl.argtypes = [HANDLE, DWORD, LPVOID, DWORD, LPVOID, DWORD,
                                     POINTER(DWORD), POINTER(OVERLAPPED)]


def rule(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# The device
# ---------------------------------------------------------------------------

# Tried in order. WGI holds the device open whenever any WGI client is running, so the
# permissive share mode is first -- a sharing violation here would otherwise look like
# "the driver refuses us" when it actually means "someone else got there first".
OPEN_MODES = [
    ("read+write, shared", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE),
    ("read+write, exclusive", GENERIC_READ | GENERIC_WRITE, 0),
    ("write only, shared", GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE),
    ("read only, shared", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE),
    ("no access flags, shared", 0, FILE_SHARE_READ | FILE_SHARE_WRITE),
]


READ_RING = 4


class _ReadSlot(object):
    """One overlapped read that stays armed: its own buffer, event and OVERLAPPED."""

    def __init__(self, size=READ_BUFFER):
        self.size = size
        self.buf = ctypes.create_string_buffer(size)
        self.ov = OVERLAPPED()
        self.event = kernel32.CreateEventW(None, True, False, None)
        self.ov.hEvent = self.event
        self.pending = False

    def close(self):
        if self.event:
            kernel32.CloseHandle(self.event)
            self.event = None


class GipDevice(object):
    """
    One open handle on `\\\\.\\XboxGIP`, with the captured force protocol on top.

    Reads use a RING of permanently-armed overlapped requests. That is not tidiness -- the
    GIP-on-Windows writeup states the driver "silently discards events when no ReadFile
    request is pending", so a loop that cancels on timeout and re-issues drops whatever
    arrives in the gap. The ring keeps a request outstanding at all times.

    They are overlapped against events rather than APCs, which is what WGI uses. That is a
    deliberate difference: an APC needs an alertable wait and would put a Python callback on
    the I/O path, and hooking WGI's APC is what throttled its input loop badly enough to kill
    force output in an earlier run. Nothing here needs that shape.
    """

    def __init__(self, handle, mode_name):
        self.handle = handle
        self.mode_name = mode_name
        self.device_id = None
        # Writes and one-shot diagnostic reads get their own events. Sharing one event across
        # concurrent operations would have each completion clobber the other's signal.
        self.event = kernel32.CreateEventW(None, True, False, None)
        self.solo_event = kernel32.CreateEventW(None, True, False, None)
        self.slots = [_ReadSlot() for _ in range(READ_RING)]
        self.reenumerated = None      # None = not tried, else the error code (0 = success)
        self.sent = 0
        self.write_errors = []
        # Read outcomes, counted separately because they mean completely different things.
        # The first version returned None for all three and made "the driver rejected us"
        # indistinguishable from "nothing arrived" -- which is exactly the question.
        self.read_errors = {}     # error code -> count
        self.read_empty = 0       # succeeded, zero bytes: accepted, nothing to give
        self.read_timeouts = 0    # still pending when we gave up
        self.read_ok = 0

    @classmethod
    def open(cls, path=GIP_PATH, verbose=True):
        """Open the device, reporting which access mode the driver accepted."""
        last = None
        for name, access, share in OPEN_MODES:
            handle = kernel32.CreateFileW(path, access, share, None, OPEN_EXISTING,
                                          FILE_FLAG_OVERLAPPED, None)
            if handle and handle != INVALID_HANDLE_VALUE:
                if verbose:
                    print("  opened %s as %s (handle 0x%x)" % (path, name, handle))
                log.event("gip.open", path=path, mode=name)
                return cls(handle, name)
            last = ctypes.get_last_error()
            if verbose:
                print("  %-24s refused, error %d" % (name, last))
        raise OSError(last, "could not open %s (last error %d)" % (path, last))

    def close(self):
        if self.handle:
            kernel32.CancelIoEx(self.handle, None)
            kernel32.CloseHandle(self.handle)
            self.handle = None
        for slot in self.slots:
            slot.close()
        for attr in ("event", "solo_event"):
            handle = getattr(self, attr, None)
            if handle:
                kernel32.CloseHandle(handle)
                setattr(self, attr, None)

    def reenumerate(self, verbose=True):
        """
        Ask the driver to restart device enumeration, so it announces devices to THIS handle.

        `GIP_ADD_REENUMERATE_CALLER_CONTEXT`. Without it a freshly opened handle sits in
        silence -- devices announced themselves long ago, to whoever was listening then, and
        nothing re-sends on open. This is the message that makes the type 0x02 announce (and
        therefore the device id) reachable.
        """
        returned = DWORD(0)
        ctypes.set_last_error(0)
        ok = kernel32.DeviceIoControl(self.handle, GIP_ADD_REENUMERATE_CALLER_CONTEXT,
                                      None, 0, None, 0, byref(returned), None)
        self.reenumerated = 0 if ok else ctypes.get_last_error()
        if verbose:
            if ok:
                print("  reenumerate IOCTL accepted (0x%08x)"
                      % GIP_ADD_REENUMERATE_CALLER_CONTEXT)
            else:
                print("  reenumerate IOCTL refused, error %d (%s)"
                      % (self.reenumerated, WIN_ERRORS.get(self.reenumerated, "?")))
        log.event("gip.reenumerate", ok=bool(ok), error=self.reenumerated)
        return bool(ok)

    # -- the read ring -----------------------------------------------------

    def _arm(self, slot):
        """Issue one overlapped read into `slot`. Synchronous completion still signals."""
        if slot.pending:
            return
        kernel32.ResetEvent(slot.event)
        got = DWORD(0)
        ctypes.set_last_error(0)
        ok = kernel32.ReadFile(self.handle, slot.buf, slot.size, byref(got), byref(slot.ov))
        if ok:
            slot.pending = True
            return
        err = ctypes.get_last_error()
        if err == ERROR_IO_PENDING:
            slot.pending = True
        else:
            self.read_errors[err] = self.read_errors.get(err, 0) + 1

    def _collect(self, slot):
        got = DWORD(0)
        ctypes.set_last_error(0)
        ok = kernel32.GetOverlappedResult(self.handle, byref(slot.ov), byref(got), False)
        slot.pending = False
        if not ok:
            err = ctypes.get_last_error()
            self.read_errors[err] = self.read_errors.get(err, 0) + 1
            return None
        if not got.value:
            self.read_empty += 1
            return None
        self.read_ok += 1
        return slot.buf.raw[:got.value]

    def poll(self, timeout_ms=200):
        """
        Every message that has completed, with the ring left fully armed.

        Drains all signalled slots rather than only the one WaitForMultipleObjects names,
        which would otherwise leave completed reads sitting until the next call.
        """
        for slot in self.slots:
            self._arm(slot)
        pending = [s for s in self.slots if s.pending]
        if not pending:
            return []

        handles = (HANDLE * len(pending))(*[s.event for s in pending])
        result = kernel32.WaitForMultipleObjects(len(pending), handles, False, timeout_ms)
        if result >= len(pending):
            self.read_timeouts += 1
            return []

        out = []
        for slot in pending:
            if kernel32.WaitForSingleObject(slot.event, 0) != 0:
                continue
            data = self._collect(slot)
            if data:
                out.append(data)
        for slot in self.slots:
            self._arm(slot)
        return out

    # -- raw I/O -----------------------------------------------------------

    def read(self, timeout_ms=250, size=READ_BUFFER):
        """
        One message, or None if none arrived. Bytes as the driver delivered them.

        This is the ONE-SHOT form, kept for `--diag`, which deliberately probes what a single
        isolated request does at a given buffer size. Everything else should use `poll()`:
        cancelling and re-issuing loses whatever arrives in the gap, because the driver
        discards events with no read pending.

        Every way this can fail is counted, because they are different answers: an error code
        means the driver refused the request, a zero-byte success means it accepted the
        request and had nothing to send, and a timeout means it is holding the request open
        waiting for data. Collapsing those into None is what made the first run unreadable.
        """
        buf = ctypes.create_string_buffer(size)
        ov = OVERLAPPED()
        kernel32.ResetEvent(self.solo_event)
        ov.hEvent = self.solo_event
        got = DWORD(0)
        ctypes.set_last_error(0)
        ok = kernel32.ReadFile(self.handle, buf, size, byref(got), byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                self.read_errors[err] = self.read_errors.get(err, 0) + 1
                return None
            if kernel32.WaitForSingleObject(self.solo_event, timeout_ms) == WAIT_TIMEOUT:
                # Leaving a read in flight would have it complete into a freed buffer.
                kernel32.CancelIoEx(self.handle, byref(ov))
                kernel32.GetOverlappedResult(self.handle, byref(ov), byref(got), True)
                self.read_timeouts += 1
                return None
            ctypes.set_last_error(0)
            if not kernel32.GetOverlappedResult(self.handle, byref(ov), byref(got), False):
                err = ctypes.get_last_error()
                self.read_errors[err] = self.read_errors.get(err, 0) + 1
                return None
        if not got.value:
            self.read_empty += 1
            return None
        self.read_ok += 1
        return buf.raw[:got.value]

    def read_report(self, indent="  "):
        """What the read attempts actually did, in the words the API used."""
        lines = ["%sreads: %d with data, %d empty, %d timed out"
                 % (indent, self.read_ok, self.read_empty, self.read_timeouts)]
        for err, n in sorted(self.read_errors.items()):
            lines.append("%s  error %d (%s) x%d" % (indent, err, WIN_ERRORS.get(err, "?"), n))
        return lines

    def write(self, payload):
        """One message out. Returns True on success and records the error otherwise."""
        ov = OVERLAPPED()
        ov.hEvent = self.event
        written = DWORD(0)
        buf = ctypes.create_string_buffer(payload, len(payload))
        ctypes.set_last_error(0)
        ok = kernel32.WriteFile(self.handle, buf, len(payload), byref(written), byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_IO_PENDING:
                self.write_errors.append(err)
                return False
            if not kernel32.GetOverlappedResult(self.handle, byref(ov), byref(written), True):
                self.write_errors.append(ctypes.get_last_error())
                return False
        self.sent += 1
        return True

    def send(self, mtype, body):
        if self.device_id is None:
            raise RuntimeError("device id not learned yet -- call learn_device() first")
        return self.write(encode_gip(self.device_id, mtype, body))

    # -- discovery ---------------------------------------------------------

    def learn_device(self, seconds=3.0, vid=HORI_VID, pid=HORI_PID, verbose=True):
        """
        Learn the 8-byte device id by listening, never by hard-coding it.

        Prefers a type 0x02 announce, because that is the only message carrying VID/PID and so
        the only one that proves WHICH device we found. Falls back to the header id of any
        message only if no announce arrives, and says so -- on a machine with one GIP device
        that is correct, and on a machine with two it would be a coin flip.
        """
        deadline = time.time() + seconds
        seen = {}
        fallback = None
        while time.time() < deadline:
            for raw in self.poll(timeout_ms=200):
                msg = decode_gip(raw)
                if msg is None:
                    continue
                seen[msg["type"]] = seen.get(msg["type"], 0) + 1
                ids = announce_ids(msg)
                if ids and ids[1] == vid and ids[2] == pid:
                    self.device_id = ids[0]
                    if verbose:
                        print("  device id %s learned from a 0x02 announce (VID %04X PID %04X)"
                              % (self.device_id.hex(), ids[1], ids[2]))
                    log.event("gip.device", id=self.device_id.hex(), vid=ids[1], pid=ids[2])
                    return self.device_id
                if fallback is None and msg["device"] != b"\0" * 8:
                    fallback = msg["device"]

        if verbose:
            if seen:
                print("  saw: " + ", ".join(
                    "0x%02x %s x%d" % (t, GIP_TYPES.get(t, "?"), n)
                    for t, n in sorted(seen.items())))
            for line in self.read_report():
                print(line)
        if fallback is not None:
            self.device_id = fallback
            if verbose:
                print("  no 0x02 announce arrived; using the id from message headers: %s"
                      % fallback.hex())
                print("  (correct with one GIP device attached, a guess with more than one)")
            return fallback
        if verbose:
            print("  nothing readable arrived -- no device id.")
        return None

    def listen(self, seconds, on_message=None):
        """Drain messages for a while; returns {type: count}."""
        counts = {}
        deadline = time.time() + seconds
        while time.time() < deadline:
            raw = self.read(timeout_ms=200)
            if not raw:
                continue
            msg = decode_gip(raw)
            if msg is None:
                continue
            counts[msg["type"]] = counts.get(msg["type"], 0) + 1
            if on_message:
                on_message(msg, raw)
        return counts

    # -- the captured effect ----------------------------------------------

    def load_effect(self):
        """
        Replay the one-time load: table upload, full parameter bank, then state + command.

        This is the part that is replayed rather than understood. If direct output works at
        all but only after this, that is itself the finding -- it means the firmware needs the
        table and the shim must carry these bytes.
        """
        for body in table_chunks():
            self.send(0x0D, body)
        for body in init_param_blocks():
            self.send(0x0B, body)
        self.send(0x0C, STATE_LOADED)
        self.send(0x0C, STATE_RUNNING)
        self.send(0x0A, SHORT_CMD)

    def set_force(self, magnitude):
        """One force update: the three messages WGI repeats at ~16 Hz."""
        self.send(0x0B, force_block(magnitude))
        self.send(0x0C, STATE_RUNNING)
        self.send(0x0A, SHORT_CMD)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

SEQUENCE = [
    (0.00, "rest"),
    (0.25, "quarter right"),
    (0.50, "half right"),
    (1.00, "full right"),
    (-0.50, "half LEFT"),
    (0.00, "rest again"),
]


def mode_diag(dev):
    """
    Why is nothing readable? Ask the API instead of guessing.

    Three candidate explanations, each with a different fingerprint:

      * the request shape is wrong      -> a specific error, and a buffer size that changes it
      * the driver accepts but is quiet -> zero-byte successes, or reads that stay pending
      * we are not entitled to the data -> a refusal code, the same at every size

    A read that stays PENDING is the interesting one: it means the driver took the request and
    is holding it open for data that never comes, which points at entitlement or focus rather
    than at anything malformed.
    """
    rule("Read diagnostics")
    print("  Trying one read at each buffer size, 400 ms each.")
    print()
    for size in (16, 20, 32, 49, 64, 256, 453, 473, 512, 1024, 4096, 8192):
        before = dict(dev.read_errors)
        ok_before, empty_before, to_before = dev.read_ok, dev.read_empty, dev.read_timeouts
        raw = dev.read(timeout_ms=400, size=size)
        if raw:
            msg = decode_gip(raw)
            kind = ("type 0x%02x %s" % (msg["type"], GIP_TYPES.get(msg["type"], ""))
                    if msg else "undecodable")
            print("    %-6d %d bytes  %s" % (size, len(raw), kind))
        elif dev.read_ok > ok_before:
            print("    %-6d data, but not decodable" % size)
        elif dev.read_empty > empty_before:
            print("    %-6d succeeded with ZERO bytes" % size)
        elif dev.read_timeouts > to_before:
            print("    %-6d still PENDING after 400 ms (driver accepted, sent nothing)" % size)
        else:
            new = [e for e in dev.read_errors if dev.read_errors[e] != before.get(e)]
            err = new[0] if new else 0
            print("    %-6d error %d  %s" % (size, err, WIN_ERRORS.get(err, "?")))
    print()
    for line in dev.read_report():
        print(line)


def mode_listen(dev, seconds):
    rule("Listening (no writes)")
    print("  Turn the wheel. Input reports are types 0x20/0x21/0x25.")
    samples = []

    def note(msg, raw):
        if msg["type"] in (0x20, 0x21, 0x25):
            samples.append(raw)

    counts = dev.listen(seconds, note)
    if not counts:
        print("  Nothing arrived in %.0f s." % seconds)
        print("  Reads on our own handle deliver nothing -- either the driver only pushes to a")
        print("  registered client, or reads are gated. --background answers which.")
        return
    for mtype, n in sorted(counts.items()):
        print("    0x%02x %-22s %d" % (mtype, GIP_TYPES.get(mtype, ""), n))
    if samples:
        print()
        print("  %d input report(s). First and last, for comparison:" % len(samples))
        for tag, raw in (("first", samples[0]), ("last ", samples[-1])):
            body = decode_gip(raw)["body"]
            print("    %s %s" % (tag, body.hex(" ")))
        if samples[0] != samples[-1]:
            print("  They differ, so the reports are live rather than a repeated cached frame.")


def mode_force(dev, hold):
    rule("Driving the motor directly")
    print("  Replaying the captured load, then the magnitude sequence.")
    print("  HANDS ON THE WHEEL. Say what you feel, not what the log says.")
    print()
    dev.load_effect()
    print("  load: %d messages sent, %d error(s)" % (dev.sent, len(dev.write_errors)))
    time.sleep(0.3)

    for magnitude, label in SEQUENCE:
        print("    %-14s x = %+.2f" % (label, magnitude))
        log.event("gip.force", x=magnitude)
        # WGI repeats the update at ~16 Hz rather than setting it once, so do the same --
        # a single write may well be treated as a stale command and dropped.
        deadline = time.time() + hold
        while time.time() < deadline:
            dev.set_force(magnitude)
            time.sleep(1.0 / 16.0)

    dev.set_force(0.0)
    print()
    print("  %d messages written, %d error(s)" % (dev.sent, len(dev.write_errors)))
    if dev.write_errors:
        counts = {}
        for err in dev.write_errors:
            counts[err] = counts.get(err, 0) + 1
        for err, n in sorted(counts.items()):
            print("    error %d x%d" % (err, n))


def mode_background(dev, hold):
    """
    A->B->A on foreground, which is the only form that answers this.

    A statistic gathered across a window whose conditions changed has already produced one
    wrong conclusion in this project. So: same force, same code path, one variable switched
    off and back on, and the verdict comes from the user's hands, not from a count of writes
    that all "succeeded" either way.
    """
    rule("Is direct output foreground-gated?  (A -> B -> A)")
    dev.load_effect()
    time.sleep(0.3)

    phases = [
        ("A  foreground", "Keep THIS window focused."),
        ("B  background", "Click another window NOW -- anything but this one."),
        ("A' foreground", "Click back on this window."),
    ]
    felt = []
    for name, instruction in phases:
        print()
        print("  %s" % name)
        print("    %s" % instruction)
        for remaining in (3, 2, 1):
            print("    starting in %d..." % remaining, end="\r", flush=True)
            time.sleep(1.0)
        print("    holding +0.50 for %.0f s ..." % hold)
        log.event("gip.background.phase", phase=name)
        deadline = time.time() + hold
        while time.time() < deadline:
            dev.set_force(0.50)
            time.sleep(1.0 / 16.0)
        dev.set_force(0.0)
        felt.append(name)

    dev.set_force(0.0)
    print()
    print("  Writes reported %d error(s) across all three phases." % len(dev.write_errors))
    print()
    print("  THE VERDICT IS YOURS, not the log's. A write that returns success proves the")
    print("  driver accepted the bytes, not that the motor moved.")
    print()
    print("    force in A and A' but NOT in B  -> output is gated. The shim stays.")
    print("    force in all three              -> output is NOT gated. Only reading needs")
    print("                                       injection, and D3/D4 shrink a lot.")
    print("    force in none                   -> the replay is incomplete, not a gate result.")


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--diag", action="store_true",
                    help="why are reads producing nothing? (needs no device id)")
    ap.add_argument("--force", action="store_true",
                    help="replay the load and the magnitude sequence")
    ap.add_argument("--background", action="store_true",
                    help="A->B->A foreground test on direct output")
    ap.add_argument("--hold", type=float, default=2.5,
                    help="seconds per phase (default 2.5)")
    ap.add_argument("--listen", type=float, default=4.0,
                    help="seconds to listen in the default mode (default 4)")
    ap.add_argument("--no-log", action="store_true", help="do not write a log file")
    return ap.parse_args()


def main():
    args = parse_args()
    if not args.no_log:
        log.start(prefix="gip_direct")

    rule("Direct GIP access -- no Windows.Gaming.Input in this process")
    print("  Nothing here imports WGI, so whatever happens is the driver's own behaviour.")
    print()

    try:
        dev = GipDevice.open()
    except OSError as exc:
        print()
        print("  Could not open %s: %s" % (GIP_PATH, exc))
        print()
        print("  This is a real answer, not a bug. If every mode was refused with error 5,")
        print("  the device wants elevation; error 32 means another process holds it")
        print("  exclusively; error 2 means the GIP driver is not loaded (wheel in PC mode?).")
        return 2

    try:
        # Without this the driver never announces devices to a fresh handle, and every read
        # sits pending forever. Measured: twelve reads at twelve buffer sizes, no errors, no
        # data -- then the announce arrived 41 ms after adding this call. It is the whole
        # difference, and it needs neither elevation nor a window.
        dev.reenumerate()

        # Diagnostics run before anything needs a device id -- when reads produce nothing,
        # learning the id is precisely what cannot happen, so gating this on it would put the
        # tool behind the failure it exists to explain.
        if args.diag:
            mode_diag(dev)
            return 0

        if dev.learn_device() is None:
            print()
            print("  Without a device id nothing can be addressed. Stopping here rather than")
            print("  writing messages to a made-up id.")
            print()
            print("  Next:  .\\.venv\\Scripts\\python.exe gip_direct.py --diag")
            print("  That asks the API why, instead of guessing at buffer sizes.")
            return 1

        print("  table %d bytes, %d chunk(s); parameter bank %d block(s)"
              % (len(FFB_TABLE), len(table_chunks()), len(init_param_blocks())))

        if args.background:
            mode_background(dev, args.hold)
        elif args.force:
            mode_force(dev, args.hold)
        else:
            mode_listen(dev, args.listen)
    finally:
        try:
            if dev.device_id:
                dev.set_force(0.0)
        except Exception:
            pass
        dev.close()
        if not args.no_log:
            log.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
