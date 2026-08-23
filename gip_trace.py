"""
Observe the device I/O Windows.Gaming.Input performs when it commands a force.

WHY

Track D has to run inside the game's process, and the device layer there has to be C. The
default plan is to transcribe the WinRT ABI calls our Python already makes -- roughly 400-600
lines of vtable C. But `Windows.Gaming.Input.dll` imports CreateFileW, WriteFile, ReadFile,
ReadFileEx and DeviceIoControl through its own IAT, which means it talks to the GIP driver
in-process rather than through a service. If we can watch it do that while commanding a KNOWN
force, we may be able to reproduce the packet ourselves -- and the shim collapses from "WinRT
in C" to "CreateFile + WriteFile".

The GIP-on-Windows writeup documents the device (`\\\\.\\XboxGIP`), the 20-byte header and force
feedback as message 0x09, but only publishes the gamepad rumble payload. The wheel's payload is
undocumented. It is, however, observable, which is what this does.

HOW, AND WHY THIS IS SAFE

IAT hooking is PER MODULE. We rewrite the import slots of Windows.Gaming.Input.dll only, so
this process's own file I/O -- including the log this writes -- goes through untouched pointers
and cannot recurse into the hooks. That property is the reason this is an IAT hook and not an
inline detour.

The hooks do as little as possible: they append a tuple to a list and call the original. All
formatting happens after the run. ReadFile is polled at input rate and a Python callback on
every one of those would distort what we are measuring, so reads are OFF unless asked for.

WHAT TO EXPECT

If force goes out as WriteFile or DeviceIoControl on a handle we can name, the payload will
differ between the commanded magnitudes and the diff points straight at the force byte(s).
If nothing is captured, WGI reaches the driver some other way (a kernel transition we cannot
see from the IAT) and D2 stays on the WinRT-in-C path. Both answers are useful; the second one
is worth ten minutes to rule out before writing several hundred lines of vtable C.

THIS NEEDS FOREGROUND. Force output is foreground-gated, so keep this window focused for the
whole run or the wheel will be commanded and nothing will be sent.

Usage:
    .\\.venv\\Scripts\\python.exe gip_trace.py
    .\\.venv\\Scripts\\python.exe gip_trace.py --reads      # also hook ReadFile (noisy)
"""

import argparse
import asyncio
import ctypes
import os
import struct
import sys
import tempfile
import time
from ctypes import POINTER, c_void_p, c_wchar_p
from ctypes.wintypes import BOOL, DWORD, HANDLE, LPVOID
from datetime import timedelta

try:
    from winrt.runtime import init_apartment
except ImportError:
    init_apartment = None

import winrt.windows.gaming.input.forcefeedback as ff  # noqa: F401
from winrt.windows.foundation.numerics import Vector3

import probe_log as log
from gip_protocol import GIP_TYPES, decode_gip, describe_pairs
from wgi_probe import (
    PumpThread,
    describe_motor,
    has_motor,
    report,
    rule,
    user32,
    wait_for_devices,
)

WGI_DLL = "Windows.Gaming.Input.dll"

# The calls worth watching. ReadFile/ReadFileEx are separated out because they fire at input
# rate and a Python callback on each would change the timing of the thing being measured.
OUTPUT_CALLS = ("CreateFileW", "WriteFile", "DeviceIoControl")
# GetOverlappedResult is where an overlapped read's data actually becomes available, so it is
# part of the input set rather than an optional extra -- without it, reads capture nothing.
INPUT_CALLS = ("ReadFile", "ReadFileEx", "GetOverlappedResult")

# Capture whole packets. The first run truncated at 64 bytes, which was enough to FIND the
# force field but not enough to REPLAY a packet -- an 80-byte message lost its tail. A GIP
# message is 20 bytes of header plus a payload this wheel keeps well under 256.
MAX_CAPTURE = 512

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleHandleW.restype = c_void_p
kernel32.GetModuleHandleW.argtypes = [c_wchar_p]
kernel32.VirtualProtect.restype = BOOL
kernel32.VirtualProtect.argtypes = [c_void_p, ctypes.c_size_t, DWORD, POINTER(DWORD)]
kernel32.GetModuleFileNameW.restype = DWORD
kernel32.GetModuleFileNameW.argtypes = [c_void_p, ctypes.c_wchar_p, DWORD]

PAGE_READWRITE = 0x04


# ---------------------------------------------------------------------------
# PE walking, against the LOADED image
# ---------------------------------------------------------------------------

def iat_slots(module_base, module_path):
    """
    Every imported function of a loaded module as {name: address-of-IAT-slot}.

    Names come from the FILE, slot addresses from the LOADED IMAGE, and mixing the two is the
    whole point:

      * On disk, both thunk arrays hold RVAs to IMAGE_IMPORT_BY_NAME records, so names are
        readable. The catch is that file offsets and RVAs differ, hence the section walk.
      * In memory, sections are mapped at their virtual addresses so an RVA is just an offset
        from the base -- but the loader has ALREADY OVERWRITTEN FirstThunk with resolved
        function addresses. That array is what we want to patch and the last thing we can read
        names from.

    Reading names from the loaded FirstThunk is exactly the bug this function was first
    written with: `Windows.Gaming.Input.dll` has a descriptor whose OriginalFirstThunk is 0,
    the fallback read live code pointers as name RVAs, and dereferencing one faulted ~48 MB
    past the end of an 864 KB image. A resolved address does not have the ordinal bit set, so
    nothing catches it -- it just reads wild memory. Take names from the file.

    The two agree by index: the descriptor's FirstThunk RVA plus i*ptr_size is the slot for
    the i'th imported name, in both copies.
    """
    data = open(module_path, "rb").read()
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    n_sections = struct.unpack_from("<H", data, e_lfanew + 6)[0]
    opt_size = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    opt = e_lfanew + 24
    pe32plus = struct.unpack_from("<H", data, opt)[0] == 0x20B
    import_dir_rva = struct.unpack_from("<I", data, opt + (112 if pe32plus else 96) + 8)[0]
    if not import_dir_rva:
        return {}

    sections = []
    for i in range(n_sections):
        off = opt + opt_size + i * 40
        v_size, v_addr, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
        sections.append((v_addr, v_size, raw_ptr, raw_size))

    def to_offset(rva):
        for v_addr, v_size, raw_ptr, raw_size in sections:
            if v_addr <= rva < v_addr + max(v_size, raw_size):
                off = raw_ptr + (rva - v_addr)
                return off if off < len(data) else None
        return None

    ptr_size = 8 if pe32plus else 4
    ordinal_flag = 1 << (63 if pe32plus else 31)
    fmt = "<Q" if pe32plus else "<I"

    slots = {}
    desc = to_offset(import_dir_rva)
    while desc is not None:
        oft, _ts, _fc, name_rva, first = struct.unpack_from("<IIIII", data, desc)
        if not (oft or name_rva or first):
            break
        lookup = to_offset(oft or first)
        i = 0
        while lookup is not None:
            value = struct.unpack_from(fmt, data, lookup + i * ptr_size)[0]
            if value == 0:
                break
            if not value & ordinal_flag:
                # IMAGE_IMPORT_BY_NAME: WORD hint, then a NUL-terminated name.
                name_off = to_offset(value & 0x7FFFFFFF)
                if name_off is not None:
                    end = data.find(b"\0", name_off + 2)
                    name = data[name_off + 2:end].decode("latin1")
                    slots.setdefault(name, module_base + first + i * ptr_size)
            i += 1
        desc += 20
    return slots


def module_path_of(module_base):
    buf = ctypes.create_unicode_buffer(1024)
    kernel32.GetModuleFileNameW(ctypes.c_void_p(module_base), buf, 1024)
    return buf.value


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------

LPOVERLAPPED = LPVOID
LPDWORD = POINTER(DWORD)

PROTOTYPES = {
    "CreateFileW": ctypes.WINFUNCTYPE(HANDLE, c_wchar_p, DWORD, DWORD, LPVOID, DWORD,
                                      DWORD, HANDLE),
    "WriteFile": ctypes.WINFUNCTYPE(BOOL, HANDLE, LPVOID, DWORD, LPDWORD, LPOVERLAPPED),
    "ReadFile": ctypes.WINFUNCTYPE(BOOL, HANDLE, LPVOID, DWORD, LPDWORD, LPOVERLAPPED),
    "ReadFileEx": ctypes.WINFUNCTYPE(BOOL, HANDLE, LPVOID, DWORD, LPOVERLAPPED, LPVOID),
    "DeviceIoControl": ctypes.WINFUNCTYPE(BOOL, HANDLE, DWORD, LPVOID, DWORD, LPVOID,
                                          DWORD, LPDWORD, LPOVERLAPPED),
    "GetOverlappedResult": ctypes.WINFUNCTYPE(BOOL, HANDLE, LPOVERLAPPED, LPDWORD, BOOL),
}

# LPOVERLAPPED_COMPLETION_ROUTINE: (dwErrorCode, dwNumberOfBytesTransferred, lpOverlapped).
COMPLETION_ROUTINE = ctypes.WINFUNCTYPE(None, DWORD, DWORD, LPOVERLAPPED)


class Tracer(object):
    """
    Installs IAT hooks and collects what passed through them.

    `_keepalive` is not tidiness: a ctypes callback object owns the native thunk, and if it is
    garbage collected the IAT slot points at freed memory and the next call from WGI takes the
    process down. The originals list is kept for the same reason and for unhooking.
    """

    def __init__(self):
        self.events = []          # (monotonic, kind, detail...) appended from WGI's threads
        self.handles = {}         # HANDLE -> path, from CreateFileW
        # HANDLE -> (access, share, disposition, flags). Kept separately from `handles`
        # because reproducing an open needs the arguments, not just the name: a device can
        # refuse a second opener purely on share mode, and guessing that costs a hardware
        # round trip to find out. `gip_direct.py` replays these.
        self.open_args = {}
        self.installed = {}       # name -> (slot_address, original_pointer)
        self._keepalive = []
        self.marker = None        # what force is being commanded right now
        self.dropped = 0
        self.pending = {}         # OVERLAPPED* -> (handle, buffer) for in-flight reads
        self._completions = {}    # original completion routine -> our wrapper
        self.read_pending = 0
        self.read_completed = 0

    # -- installation ------------------------------------------------------

    def install(self, module_base, module_path, names):
        slots = iat_slots(module_base, module_path)
        for name in names:
            slot = slots.get(name)
            if slot is None:
                print("  %-16s not imported -- nothing to hook" % name)
                continue
            proto = PROTOTYPES[name]
            current = ctypes.cast(slot, POINTER(c_void_p)).contents.value
            if not current:
                print("  %-16s slot is NULL -- not yet resolved, skipping" % name)
                continue
            original = proto(current)
            thunk = proto(self._make(name, original))
            self._keepalive.append((thunk, original))

            old = DWORD(0)
            if not kernel32.VirtualProtect(ctypes.c_void_p(slot), ctypes.sizeof(c_void_p),
                                           PAGE_READWRITE, ctypes.byref(old)):
                print("  %-16s VirtualProtect failed (%d)" % (name, ctypes.get_last_error()))
                continue
            ctypes.cast(slot, POINTER(c_void_p))[0] = ctypes.cast(thunk, c_void_p).value
            kernel32.VirtualProtect(ctypes.c_void_p(slot), ctypes.sizeof(c_void_p),
                                    old, ctypes.byref(old))
            self.installed[name] = (slot, ctypes.cast(original, c_void_p).value)
            print("  %-16s hooked at slot 0x%x" % (name, slot))

    def uninstall(self):
        for name, (slot, original) in self.installed.items():
            old = DWORD(0)
            if kernel32.VirtualProtect(ctypes.c_void_p(slot), ctypes.sizeof(c_void_p),
                                       PAGE_READWRITE, ctypes.byref(old)):
                ctypes.cast(slot, POINTER(c_void_p))[0] = original
                kernel32.VirtualProtect(ctypes.c_void_p(slot), ctypes.sizeof(c_void_p),
                                        old, ctypes.byref(old))
        self.installed.clear()

    # -- overlapped completion ---------------------------------------------

    def _completion_for(self, routine):
        """
        A stand-in for one ReadFileEx completion routine, cached by the original's address.

        Returns None if there is nothing to wrap. The cache matters: WGI reissues a read on
        every input report, and minting a ctypes callback per read would leak a native thunk
        each time and eventually take the process down.
        """
        if not routine:
            return None
        key = ctypes.cast(routine, c_void_p).value
        if key in self._completions:
            return self._completions[key]

        original = COMPLETION_ROUTINE(key)

        def completion(error, transferred, overlapped):
            try:
                entry = self.pending.pop(overlapped, None)
                if entry and transferred and not error:
                    source, buffer = entry
                    if buffer:
                        payload = ctypes.string_at(buffer,
                                                   min(transferred, MAX_CAPTURE))
                        self.events.append((time.perf_counter(), "read", source, payload,
                                            self.marker))
                        self.read_completed += 1
            except Exception:
                self.dropped += 1
            return original(error, transferred, overlapped)

        shim = COMPLETION_ROUTINE(completion)
        self._completions[key] = shim
        self._keepalive.append((shim, original))
        return shim

    # -- the hooks themselves ----------------------------------------------

    def _make(self, name, original):
        """
        Build the replacement for one import.

        Every one of these runs on a thread WGI owns. They record and forward, and do no
        formatting, no I/O and no locking -- list.append is atomic under the GIL, which is all
        the synchronisation this needs.
        """
        if name == "CreateFileW":
            def hook(path, access, share, sa, disp, flags, template):
                handle = original(path, access, share, sa, disp, flags, template)
                try:
                    self.handles[handle] = path
                    self.open_args[handle] = (access, share, disp, flags)
                    self.events.append((time.perf_counter(), "open", handle, path,
                                        self.marker))
                except Exception:
                    self.dropped += 1
                return handle

        elif name == "WriteFile":
            def hook(handle, buffer, count, written, overlapped):
                # A write's buffer is full BEFORE the call, so snapshot first.
                try:
                    payload = ctypes.string_at(buffer, min(count, MAX_CAPTURE)) if buffer else b""
                except Exception:
                    payload = b""
                result = original(handle, buffer, count, written, overlapped)
                try:
                    self.events.append((time.perf_counter(), "write", handle, payload,
                                        self.marker))
                except Exception:
                    self.dropped += 1
                return result

        elif name == "ReadFile":
            def hook(handle, buffer, count, read_count, overlapped):
                """
                A read's buffer is filled BY the call, and these reads are overlapped.

                The first version of this snapshotted the buffer before calling through,
                exactly as the write hook does, and captured 482 empty payloads -- correct for
                a write and meaningless for a read. Reading after the call is necessary but
                not sufficient: WGI issues these overlapped, so the call returns with nothing
                transferred and the data lands later. The buffer is therefore remembered
                against its OVERLAPPED pointer and collected in the GetOverlappedResult hook.
                """
                if overlapped and len(self.pending) < 4096:
                    self.pending[overlapped] = (handle, buffer)
                result = original(handle, buffer, count, read_count, overlapped)
                try:
                    n = read_count[0] if read_count else 0
                    if n and buffer:
                        # Completed synchronously; no completion callback will follow.
                        self.pending.pop(overlapped, None)
                        payload = ctypes.string_at(buffer, min(n, MAX_CAPTURE))
                        self.events.append((time.perf_counter(), "read", handle, payload,
                                            self.marker))
                    else:
                        self.read_pending += 1
                except Exception:
                    self.dropped += 1
                return result

        elif name == "GetOverlappedResult":
            def hook(handle, overlapped, transferred, wait):
                result = original(handle, overlapped, transferred, wait)
                try:
                    if result and overlapped in self.pending:
                        source, buffer = self.pending.pop(overlapped)
                        n = transferred[0] if transferred else 0
                        if n and buffer:
                            payload = ctypes.string_at(buffer, min(n, MAX_CAPTURE))
                            self.events.append((time.perf_counter(), "read", source,
                                                payload, self.marker))
                            self.read_completed += 1
                except Exception:
                    self.dropped += 1
                return result

        elif name == "ReadFileEx":
            def hook(handle, buffer, count, overlapped, routine):
                """
                This is how the wheel's input actually arrives, so the completion routine has
                to be wrapped -- there is nowhere else to see the data.

                ReadFileEx takes an APC that Windows queues on the issuing thread when the
                read completes. It never passes through GetOverlappedResult, which is why
                hooking that captured nothing: a run showed 387 read events, zero pending and
                zero completed, meaning ReadFile was never called and every read was this.

                So we substitute our own completion routine, read the buffer when it fires,
                and then call WGI's. One wrapper is cached per original routine pointer:
                allocating a callback per read would leak a thunk on every input report.
                """
                if overlapped and len(self.pending) < 4096:
                    self.pending[overlapped] = (handle, buffer)
                shim = self._completion_for(routine)
                result = original(handle, buffer, count, overlapped, shim or routine)
                try:
                    self.events.append((time.perf_counter(), "readex", handle, b"",
                                        self.marker))
                except Exception:
                    self.dropped += 1
                return result

        elif name == "DeviceIoControl":
            def hook(handle, code, inbuf, insize, outbuf, outsize, returned, overlapped):
                try:
                    payload = ctypes.string_at(inbuf, min(insize, MAX_CAPTURE)) if inbuf else b""
                except Exception:
                    payload = b""
                result = original(handle, code, inbuf, insize, outbuf, outsize, returned,
                                  overlapped)
                try:
                    self.events.append((time.perf_counter(), "ioctl", handle,
                                        (code, payload), self.marker))
                except Exception:
                    self.dropped += 1
                return result
        else:
            raise ValueError(name)

        return hook


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

# Distinct, easily separated magnitudes. Zero first and last so the packet for "no force" is
# captured too -- the difference between that and a commanded force is the clearest signal
# available for locating the magnitude field.
SEQUENCE = [
    (0.0, "rest"),
    (0.25, "quarter right"),
    (0.5, "half right"),
    (1.0, "full right"),
    (-0.5, "half LEFT"),
    (0.0, "rest again"),
]


def command_sequence(motor, loop, tracer, hold, pump):
    """
    Load one constant-force effect and rewrite its magnitude, as the bridge does.

    Foreground is re-asserted and SAMPLED throughout, not just at startup. Force output is
    foreground-gated, so a run that quietly loses focus mid-sequence commands forces that
    never reach the driver and captures nothing -- which is indistinguishable from "the device
    does not work that way" unless the loss is reported. An earlier version asserted focus
    once at startup and never again, which is exactly the intermittent failure that produced.

    Note the gate is on the PROCESS, not a window: clicking the terminal does not help,
    because the terminal is a different process. It has to be the probe window.
    """
    import winrt.windows.gaming.input.forcefeedback as ffb

    motor.master_gain = 1.0
    effect = ffb.ConstantForceEffect()
    effect.set_parameters(Vector3(0.0, 0.0, 0.0), timedelta(seconds=60))
    loop.run_until_complete(motor.load_effect_async(effect))
    effect.start()

    samples = 0
    held = 0
    for magnitude, label in SEQUENCE:
        tracer.marker = magnitude
        effect.set_parameters(Vector3(magnitude, 0.0, 0.0), timedelta(seconds=60))

        # Slice the hold so foreground is re-asserted often enough to matter, and so the
        # fraction below is measured across the whole window rather than sampled once.
        slices = max(1, int(hold / 0.25))
        ours = 0
        for _ in range(slices):
            if pump is not None:
                pump.ensure_foreground()
                if user32.GetForegroundWindow() == pump.hwnd:
                    ours += 1
                    held += 1
            samples += 1
            time.sleep(hold / slices)
        print("    %-14s x = %+.2f    foreground %d%%"
              % (label, magnitude, round(100.0 * ours / slices)))

    tracer.marker = None
    effect.stop()
    return effect, (held / samples if samples else 0.0)


def write_shim_config(tracer):
    """
    Hand the shim the device id, via %TEMP%\\wh33lh4x.cfg.

    The shim discovers the id itself when it can, but that needs the GIP driver to deliver an
    announce -- and it will not do that for a process without a focused window. This file is
    the fallback so a failed discovery cannot block the whole experiment.

    The id is taken from the HEADER of any captured message, not from a read: every GIP
    message carries the device id in its first eight bytes, so a plain trace run supplies it.
    That matters because `--reads` installs an APC wrapper that throttled WGI's input loop
    badly enough to kill force output, and this must not depend on it.
    """
    device = None
    for _t, kind, _h, payload, _m in tracer.events:
        if kind != "write":
            continue
        gip = decode_gip(payload)
        if gip and gip["device"] != b"\0" * 8:
            device = gip["device"]
            break
    if device is None:
        return None

    path = os.path.join(tempfile.gettempdir(), "wh33lh4x.cfg")
    existing = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    # Preserve whatever selftest= the user set; only the id is ours to refresh.
    selftest = "1" if "selftest=1" in existing else "0"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# written by gip_trace.py -- consumed by shim/dinput8.c\n")
        fh.write("device=%s\n" % device.hex())
        fh.write("selftest=%s\n" % selftest)
    print("  shim config: %s  (device=%s, selftest=%s)" % (path, device.hex(), selftest))
    return path


def summarise(tracer):
    rule("What Windows.Gaming.Input actually did")

    if not tracer.events:
        print("  NOTHING was captured on the hooked imports.")
        print()
        print("  That is a real answer: WGI does not reach the driver through the file APIs")
        print("  in its own IAT, at least not for force. D2 stays on the WinRT-in-C path.")
        return False

    opens = [e for e in tracer.events if e[1] == "open"]
    if opens:
        print("  handles opened (%d):" % len(opens))
        for _t, _k, handle, path, _m in opens:
            print("    0x%-14x %s" % (handle or 0, path))
            args = tracer.open_args.get(handle)
            if args:
                access, share, disp, flags = args
                print("        access=0x%08x share=0x%x disposition=%d flags=0x%08x"
                      % (access, share, disp, flags))
        print()

    writes = [e for e in tracer.events if e[1] in ("write", "ioctl")]
    print("  writes / ioctls: %d" % len(writes))
    if not writes:
        print()
        print("  Handles were opened but nothing was written through them while force was")
        print("  commanded -- so the force path is elsewhere. D2 stays on WinRT-in-C.")
        return False

    print()
    by_marker = {}
    for _t, kind, handle, payload, marker in writes:
        code = None
        if kind == "ioctl":
            code, payload = payload
        by_marker.setdefault(marker, []).append((kind, handle, code, payload))

    for magnitude, _label in SEQUENCE:
        rows = by_marker.get(magnitude, [])
        print("  x = %+.2f  -> %d packet(s)" % (magnitude, len(rows)))
        seen = set()
        for kind, handle, code, payload in rows:
            key = (kind, code, payload)
            if key in seen:
                continue
            seen.add(key)
            where = tracer.handles.get(handle, "handle 0x%x" % (handle or 0))
            gip = decode_gip(payload) if kind == "write" else None
            if gip is None:
                tag = "%s code=0x%08x" % (kind, code) if code is not None else kind
                print("      %-22s %-30s %s" % (tag, where[-30:], payload.hex(" ")))
                continue
            print("      type 0x%02x %-14s len=%-4d dev=%s  %s"
                  % (gip["type"], GIP_TYPES.get(gip["type"], ""), gip["length"],
                     gip["device"].hex(), where[-24:]))
            if gip["pairs"]:
                for line in describe_pairs(gip["pairs"]):
                    print(line)
            else:
                print("          body %s" % gip["body"].hex(" "))
        print()

    reads = [e for e in tracer.events if e[1] in ("read", "readex")]
    if reads:
        rule("Reads (how the device makes itself known)")
        with_data = [e for e in reads if e[3]]
        print("  %d read event(s); %d carried data." % (len(reads), len(with_data)))
        print("  issued overlapped and still in flight: %d;  collected at completion: %d"
              % (tracer.read_pending, tracer.read_completed))
        if not with_data:
            print()
            print("  No read payload was captured. These reads complete somewhere we are not")
            print("  watching -- most likely ReadFileEx completion routines, which run without")
            print("  passing through GetOverlappedResult.")
            return True
        print("  Distinct payloads, first 12 shown:")
        seen = set()
        shown = 0
        for _t, _k, handle, payload, _m in reads:
            if not payload or payload in seen:
                continue
            seen.add(payload)
            gip = decode_gip(payload)
            where = tracer.handles.get(handle, "handle 0x%x" % (handle or 0))
            if gip is not None:
                print("    type 0x%02x %-14s len=%-4d dev=%s  %s"
                      % (gip["type"], GIP_TYPES.get(gip["type"], ""), gip["length"],
                         gip["device"].hex(), where[-24:]))
                print("        %s" % gip["body"].hex(" ")[:150])
            else:
                print("    %-26s %s" % (where[-26:], payload.hex(" ")[:150]))
            shown += 1
            if shown >= 12:
                break
        print()
        print("  Look for the device id above appearing in a read: that is where a")
        print("  reimplementation would LEARN it rather than hard-coding this wheel's.")

    print("  The force field is param 0x0008 of a type 0x0b message: a float32 in -1..+1,")
    print("  the same units we already produce. D2 is CreateFile + WriteFile, not WinRT.")
    return True


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hold", type=float, default=1.5,
                   help="seconds to hold each magnitude (default 1.5)")
    p.add_argument("--reads", action="store_true",
                   help="also hook ReadFile/ReadFileEx (noisy; changes timing)")
    p.add_argument("--wait", type=float, default=8.0,
                   help="seconds to wait for the wheel to appear (default 8)")
    p.add_argument("--no-log", action="store_true", help="do not write a session log")
    return p.parse_args()


def main():
    args = parse_args()
    log_path = None if args.no_log else log.start(prefix="gip_trace")

    rule("Can we see the force packet Windows.Gaming.Input sends?")
    print("  KEEP THIS WINDOW FOCUSED for the whole run -- force output is foreground-gated,")
    print("  and an unfocused run will command forces that never reach the driver.")
    if log_path:
        print("  Log: %s" % log_path)
    print()

    # Load the DLL and hook BEFORE anything touches WinRT, so device discovery -- which is
    # where CreateFileW happens -- is captured too. Hooking after enumeration would miss the
    # one call that names the device.
    ctypes.WinDLL(WGI_DLL)
    base = kernel32.GetModuleHandleW(WGI_DLL)
    if not base:
        print("  Could not load %s" % WGI_DLL)
        return 1

    rule("Installing hooks")
    path = module_path_of(base)
    print("  %s at 0x%x" % (path, base))
    tracer = Tracer()
    names = list(OUTPUT_CALLS) + (list(INPUT_CALLS) if args.reads else [])
    tracer.install(base, path, names)
    if not tracer.installed:
        print("  Nothing could be hooked. Aborting.")
        return 1
    print()

    if init_apartment is not None:
        try:
            init_apartment()
        except TypeError:
            init_apartment(0)

    pump = PumpThread()
    pump.start()
    pump.ready.wait(timeout=5)

    loop = asyncio.new_event_loop()
    effect = None
    motor = None
    try:
        raw, wheels = wait_for_devices(args.wait, pump)
        if not has_motor(raw, wheels):
            rule("RESULT")
            print("  No wheel with a force-feedback motor found. Put the wheel in Xbox mode")
            print("  (long-press PROFILE) and press one of its buttons while this runs.")
            return 1

        motor, _label, _wheel, _identity = report(raw, wheels)
        rule("Wheel found")
        describe_motor(motor)

        rule("Commanding known forces")
        print("  Foreground is shown per phase. If it is not ~100%%, click the")
        print("  'FFB probe' window -- NOT this terminal, which is a different process.")
        effect, foreground = command_sequence(motor, loop, tracer, args.hold, pump)

        summarise(tracer)
        write_shim_config(tracer)
        if foreground < 0.95:
            rule("WARNING -- this run was not fully foregrounded")
            print("  Held foreground for only %d%% of the sequence. Force output is gated on"
                  % round(100 * foreground))
            print("  it, so some commanded forces never reached the driver and the capture")
            print("  above is incomplete. Re-run and keep the 'FFB probe' window focused;")
            print("  clicking the terminal does not count, it is a different process.")
        if tracer.dropped:
            print("\n  (%d events dropped inside a hook)" % tracer.dropped)
        return 0
    finally:
        # Unhook BEFORE the process starts tearing down: the callback thunks die with this
        # object, and a WGI worker thread calling a freed thunk during interpreter shutdown
        # would look like a random crash rather than the use-after-free it is.
        tracer.uninstall()
        try:
            if effect is not None:
                effect.stop()
            if motor is not None:
                # Leaving the motor loaded has been observed to leave it dead for the next
                # run, so always stop everything even on the failure paths.
                motor.stop_all_effects()
        except Exception:
            pass
        loop.close()
        pump.stop()
        log.stop()


if __name__ == "__main__":
    sys.exit(main())
