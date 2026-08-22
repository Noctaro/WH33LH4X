"""
vjoy_ffb_spike.py -- Track B phase B0: does the vJoy FFB callback actually work?

This is the kill-switch for the whole bridge. The bridge plan is:

    game (DirectInput) -> vJoy virtual wheel -> [this callback] -> WGI -> real HORI motor

Everything downstream is pointless if the first arrow does not deliver usable data, so this
tool tests exactly that arrow and nothing else. It never touches the real wheel.

Two things have to be true:

  1. The FFB callback fires, and the packets DECODE -- effect type, magnitude, condition
     coefficients all arriving as sane numbers rather than garbage.
  2. The EFFECT BLOCK INDEX VARIES across concurrent effects.

(2) is the one that is easy to get wrong and fatal to discover late. vJoy 2.1.9.x has no
block-index management: every effect reports index 1, so concurrent effects are
indistinguishable. A bridge built on that looks perfect in phase B3 -- one effect at a time
-- and falls apart in B4, because real games run spring + damper + periodic simultaneously
and the bridge cannot tell which packet belongs to which effect. Hence the concurrency phase
below: three effects alive at once, and the run fails if their indices collide.

The DirectInput sender is built in. That is deliberate: it makes the result reproducible
without depending on a control-panel tab existing, and it exercises the same API a game
uses. It sends to the VIRTUAL device only -- the vJoy wheel, matched by VID 0x1234 /
PID 0xBEAD -- so no force reaches the HORI wheel from this tool at all.

Usage:
    python vjoy_ffb_spike.py             # preflight, then the scripted sender
    python vjoy_ffb_spike.py --listen    # listen only; drive it from a game or joy.cpl
    python vjoy_ffb_spike.py --device 2  # use vJoy device 2 instead of 1
"""

import argparse
import ctypes
import sys
import threading
import time
from ctypes import byref, c_int32, c_uint32

import probe_log as log

try:
    import pyvjoy
    import pyvjoy._sdk as sdk
    from pyvjoy.constants import (
        CTRL_DEVRST, CTRL_DISACT, CTRL_ENACT, CTRL_STOPALL, CTRL_DEVPAUSE, CTRL_DEVCONT,
        EFF_SOLO, EFF_START, EFF_STOP,
        HID_USAGE_CONST, HID_USAGE_DMPR, HID_USAGE_FRIC, HID_USAGE_INRT, HID_USAGE_RAMP,
        HID_USAGE_SINE, HID_USAGE_SPRNG, HID_USAGE_SQUR, HID_USAGE_STDN, HID_USAGE_STUP,
        HID_USAGE_TRNG,
        PT_BLKFRREP, PT_CONDREP, PT_CONSTREP, PT_CTRLREP, PT_EFFREP, PT_EFOPREP,
        PT_ENVREP, PT_GAINREP, PT_NEWEFREP, PT_PRIDREP, PT_RAMPREP,
        VJD_STAT_BUSY, VJD_STAT_FREE, VJD_STAT_MISS, VJD_STAT_OWN,
    )
except ImportError:
    sys.exit("pyvjoyffb is not installed. Run:\n"
             "    .\\.venv\\Scripts\\python.exe -m pip install pyvjoyffb")

import dinput_abi as di
from dinput_abi import (
    DI_FFNOMINALMAX,
    DIEB_NOTRIGGER,
    DIEFF_CARTESIAN,
    DIEFF_OBJECTOFFSETS,
    DIENUM_CONTINUE,
    DISCL_BACKGROUND,
    DISCL_EXCLUSIVE,
    DI8DEVCLASS_GAMECTRL,
    DIEDFL_ATTACHEDONLY,
    DIEDFL_FORCEFEEDBACK,
    DICONDITION,
    DICONSTANTFORCE,
    DIEFFECT,
    DIPERIODIC,
    DIRAMPFORCE,
    DirectInputError,
    GUID_ConstantForce,
    GUID_Damper,
    GUID_RampForce,
    GUID_Sine,
    GUID_Spring,
    LPDIENUMDEVICESCALLBACKW,
)

VJOY_VID = 0x1234
VJOY_PID = 0xBEAD

# Report type -> short name. Straight from the HID PID spec's report ids, which is what
# vJoy hands us; keeping the numbers visible makes an unknown packet self-documenting.
REPORT_NAMES = {
    PT_EFFREP: "SetEffect", PT_ENVREP: "SetEnvelope", PT_CONDREP: "SetCondition",
    PT_PRIDREP: "SetPeriodic", PT_CONSTREP: "SetConstant", PT_RAMPREP: "SetRamp",
    PT_EFOPREP: "EffectOp", PT_BLKFRREP: "BlockFree", PT_CTRLREP: "DeviceControl",
    PT_GAINREP: "DeviceGain", PT_NEWEFREP: "CreateNewEffect",
}
EFFECT_TYPE_NAMES = ["None", "Constant", "Ramp", "Square", "Sine", "Triangle",
                     "SawtoothUp", "SawtoothDown", "Spring", "Damper", "Inertia",
                     "Friction", "Custom"]
EFFECT_OP_NAMES = {EFF_START: "START", EFF_SOLO: "SOLO", EFF_STOP: "STOP"}
CTRL_NAMES = {CTRL_ENACT: "ENABLE_ACTUATORS", CTRL_DISACT: "DISABLE_ACTUATORS",
              CTRL_STOPALL: "STOP_ALL", CTRL_DEVRST: "DEVICE_RESET",
              CTRL_DEVPAUSE: "PAUSE", CTRL_DEVCONT: "CONTINUE"}
STATUS_NAMES = {VJD_STAT_OWN: "OWNED BY US", VJD_STAT_FREE: "FREE",
                VJD_STAT_BUSY: "BUSY (another app owns it)", VJD_STAT_MISS: "MISSING"}


def rule(title=""):
    if title:
        print("\n" + "=" * 78)
        print(title)
        print("=" * 78)
    else:
        print("-" * 78)


# ---------------------------------------------------------------------------
# Receiver: the vJoy FFB callback
# ---------------------------------------------------------------------------

class PacketSink(object):
    """
    Records every FFB packet vJoy delivers, and prints it.

    The callback runs on VJOY'S OWN THREAD, not ours. For this spike that only means
    serialising the prints, but it is the reason the real bridge must hand off over a queue
    rather than calling WinRT from here: the WGI session is bound to the pump thread, and
    calling into it from a foreign thread is exactly the kind of failure that presents as
    "force works sometimes".
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.packets = []          # (elapsed, reptype, block_index, dict-or-value)
        self.blocks_seen = set()   # every non-zero EffectBlockIndex observed
        self.block_types = {}      # block index -> effect type name, from SetEffect
        self.decode_failures = 0
        self.started = time.monotonic()
        self.quiet = False         # suppress console printing during the concurrency phase

    def __call__(self, data, reptype):
        # A ctypes callback that raises prints a traceback and returns garbage to the
        # caller. Never let one escape.
        try:
            self._handle(data, reptype)
        except Exception as exc:                                  # noqa: BLE001
            with self.lock:
                self.decode_failures += 1
            log.event("ffb.handler_error", reptype=reptype, error=repr(exc))

    def _handle(self, data, reptype):
        elapsed = time.monotonic() - self.started
        name = REPORT_NAMES.get(reptype, "Unknown(0x%02X)" % reptype)

        if isinstance(data, sdk.PacketStruct):
            fields = data.to_dict()
            block = fields.get("EffectBlockIndex", 0)
        else:
            fields = data              # control/gain/neweff decode to a plain int
            block = 0

        with self.lock:
            self.packets.append((elapsed, reptype, block, fields))
            if block:
                self.blocks_seen.add(block)
            if reptype == PT_EFFREP and block:
                et = fields.get("EffectType", 0)
                self.block_types[block] = (EFFECT_TYPE_NAMES[et]
                                           if 0 <= et < len(EFFECT_TYPE_NAMES)
                                           else "type%d" % et)

        log.event("ffb.packet", t=round(elapsed, 4), rep=name, block=block, data=fields)
        if not self.quiet:
            print("    %7.3fs  blk=%-3s %-16s %s"
                  % (elapsed, block or "-", name, self._describe(reptype, fields)))

    @staticmethod
    def _describe(reptype, f):
        """One-line human reading of a packet, so a wrong decode is obvious on sight."""
        if reptype == PT_EFFREP:
            et = f.get("EffectType", 0)
            tn = EFFECT_TYPE_NAMES[et] if 0 <= et < len(EFFECT_TYPE_NAMES) else "type%d" % et
            return ("type=%s duration=%s gain=%d dir=(%d,%d) polar=0x%02X"
                    % (tn, f["Duration"] or "INFINITE", f["Gain"],
                       f["DirX"], f["DirY"], f["Polar"]))
        if reptype == PT_CONSTREP:
            return "magnitude=%+d" % f["Magnitude"]
        if reptype == PT_RAMPREP:
            return "start=%+d end=%+d" % (f["Start"], f["End"])
        if reptype == PT_PRIDREP:
            return ("magnitude=%d offset=%+d phase=%d period=%dms"
                    % (f["Magnitude"], f["Offset"], f["Phase"], f["Period"]))
        if reptype == PT_CONDREP:
            return ("axis=%s centre=%+d coeff=%+d/%+d satur=%d/%d deadband=%d"
                    % ("Y" if f["isY"] else "X", f["CenterPointOffset"],
                       f["PosCoeff"], f["NegCoeff"],
                       f["PosSatur"], f["NegSatur"], f["DeadBand"]))
        if reptype == PT_ENVREP:
            return ("attack=%d/%dms fade=%d/%dms"
                    % (f["AttackLevel"], f["AttackTime"], f["FadeLevel"], f["FadeTime"]))
        if reptype == PT_EFOPREP:
            return "op=%s loops=%d" % (EFFECT_OP_NAMES.get(f["EffectOp"], f["EffectOp"]),
                                       f["LoopCount"])
        if reptype == PT_CTRLREP:
            return CTRL_NAMES.get(f, str(f))
        if reptype == PT_GAINREP:
            return "device gain=%d/255" % f
        if reptype == PT_NEWEFREP:
            et = f if isinstance(f, int) else 0
            return "requested type=%s" % (EFFECT_TYPE_NAMES[et]
                                          if 0 <= et < len(EFFECT_TYPE_NAMES) else et)
        if reptype == PT_BLKFRREP:
            return "freed block %s" % f
        return str(f)

    def snapshot(self):
        with self.lock:
            return len(self.packets), set(self.blocks_seen)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def preflight(rid):
    """Report everything that decides whether this can work, before touching anything."""
    rule("vJoy preflight")

    ok = True
    try:
        sdk.vJoyEnabled()
        print("  vJoy driver enabled     : yes")
    except Exception as exc:                                      # noqa: BLE001
        print("  vJoy driver enabled     : NO -- %s" % exc)
        return False

    sdk._vj.GetvJoyProductString.restype = ctypes.c_wchar_p
    sdk._vj.GetvJoyManufacturerString.restype = ctypes.c_wchar_p
    version = sdk._vj.GetvJoyVersion()
    print("  product                 : %s (%s)"
          % (sdk._vj.GetvJoyProductString(), sdk._vj.GetvJoyManufacturerString()))
    print("  driver/DLL version      : 0x%04X" % version)

    # The 2.1.9 -> 2.2.0 boundary is the whole reason this check exists. Below 2.2.0 the
    # driver has no block-index management and the bridge cannot separate concurrent
    # effects, so the run would produce a confident-looking pass that means nothing.
    if version < 0x0220:
        print("    >>> TOO OLD. Below 2.2.0 there is no FFB block index; every effect")
        print("        reports index 1 and concurrent effects are indistinguishable.")
        print("        Install BrunnerInnovation/vJoy v2.2.2.0.")
        ok = False
    else:
        print("    (>= 2.2.0, so FFB block index management is present)")

    try:
        sdk.DriverMatch()
        print("  interface DLL matches   : yes")
    except Exception as exc:                                      # noqa: BLE001
        print("  interface DLL matches   : NO -- %s" % exc)
        print("    >>> vJoyInterface.dll and the installed driver disagree. The bundled")
        print("        DLL in pyvjoyffb is Brunner 2.2.2; install that driver version.")
        ok = False

    print("  FFB capable (global)    : %s" % sdk.vJoyFfbCap())

    status = sdk.GetVJDStatus(rid)
    print()
    print("  device %d status         : %s" % (rid, STATUS_NAMES.get(status, status)))
    if status in (VJD_STAT_MISS, VJD_STAT_BUSY):
        print("    >>> Cannot use it. Configure or free device %d in vJoyConf first." % rid)
        return False

    print("  device %d FFB capable    : %s" % (rid, sdk.IsDeviceFfb(rid)))
    if not sdk.IsDeviceFfb(rid):
        print("    >>> Enable 'Effects' for this device in vJoyConf and re-run.")
        ok = False

    supported = [name for name, usage in
                 (("Constant", HID_USAGE_CONST), ("Ramp", HID_USAGE_RAMP),
                  ("Square", HID_USAGE_SQUR), ("Sine", HID_USAGE_SINE),
                  ("Triangle", HID_USAGE_TRNG), ("SawUp", HID_USAGE_STUP),
                  ("SawDown", HID_USAGE_STDN), ("Spring", HID_USAGE_SPRNG),
                  ("Damper", HID_USAGE_DMPR), ("Inertia", HID_USAGE_INRT),
                  ("Friction", HID_USAGE_FRIC))
                 if sdk.IsDeviceFfbEffect(rid, usage)]
    print("  device %d effect types   : %s" % (rid, " ".join(supported) or "NONE"))
    print("  buttons / axes          : %d buttons"
          % sdk._vj.GetVJDButtonNumber(rid))

    log.event("vjoy.preflight", version=hex(version), device=rid, ffb=sdk.IsDeviceFfb(rid),
              effects=len(supported), ok=ok)
    return ok


# ---------------------------------------------------------------------------
# Sender: a DirectInput client, i.e. what a game does
# ---------------------------------------------------------------------------

def find_vjoy_device(dinput):
    """Enumerate FFB-capable game controllers and return the vJoy one."""
    found = []

    @LPDIENUMDEVICESCALLBACKW
    def on_device(instance_ptr, _ref):
        copy = di.DIDEVICEINSTANCEW()
        ctypes.memmove(byref(copy), byref(instance_ptr.contents), ctypes.sizeof(copy))
        found.append(copy)
        return DIENUM_CONTINUE

    dinput.enum_devices(DI8DEVCLASS_GAMECTRL, on_device,
                        DIEDFL_ATTACHEDONLY | DIEDFL_FORCEFEEDBACK)

    print("  DirectInput force-feedback devices: %d" % len(found))
    match = None
    for inst in found:
        vid = inst.guidProduct.Data1 & 0xFFFF
        pid = (inst.guidProduct.Data1 >> 16) & 0xFFFF
        tag = ""
        if (vid, pid) == (VJOY_VID, VJOY_PID):
            tag = "   <-- vJoy, using this one"
            match = inst
        print("    %-40s VID 0x%04X PID 0x%04X%s"
              % (inst.tszProductName, vid, pid, tag))

    if match is None:
        print()
        print("  >>> vJoy is not among them. DirectInput only lists a device as")
        print("      force-feedback capable once 'Effects' is enabled for it in vJoyConf.")
    return match


class Sender(object):
    """Creates and starts DirectInput effects on the virtual device."""

    def __init__(self, device, gain):
        self.device = device
        self.gain = gain
        self.acquired = False
        self.effects = []
        self._fmt = None
        self._objs = None

    def open(self):
        # A console window if there is one, otherwise a hidden top-level window. Either
        # satisfies DirectInput's requirement that exclusive access have a real HWND.
        hwnd = di.ensure_hwnd()
        self._fmt, self._objs, axes = di.negotiate_axis_data_format(self.device)
        print("  data format             : %d axes" % axes)
        self.device.set_data_format(self._fmt)
        # EXCLUSIVE is mandatory for force feedback. BACKGROUND keeps it alive if the
        # console loses focus -- unlike the WGI side, DirectInput does not gate on it.
        self.device.set_cooperative_level(hwnd, DISCL_EXCLUSIVE | DISCL_BACKGROUND)
        self.device.acquire()
        self.acquired = True

    def _describe(self, duration, params):
        axes = (c_uint32 * 1)(0)        # offset 0 == first axis of our data format
        direction = (c_int32 * 1)(1)    # cartesian, +X

        eff = DIEFFECT()
        eff.dwSize = ctypes.sizeof(DIEFFECT)
        eff.dwFlags = DIEFF_OBJECTOFFSETS | DIEFF_CARTESIAN
        eff.dwDuration = int(duration * 1_000_000)
        eff.dwSamplePeriod = 0
        eff.dwGain = int(max(0.0, min(1.0, self.gain)) * DI_FFNOMINALMAX)
        eff.dwTriggerButton = DIEB_NOTRIGGER
        eff.dwTriggerRepeatInterval = 0
        eff.cAxes = 1
        eff.rgdwAxes = axes
        eff.rglDirection = direction
        eff.lpEnvelope = None
        eff.cbTypeSpecificParams = ctypes.sizeof(params)
        eff.lpvTypeSpecificParams = ctypes.cast(byref(params), ctypes.c_void_p)
        eff.dwStartDelay = 0
        eff._keepalive = (axes, direction, params)   # or the pointers dangle
        return eff

    def create(self, guid, params, duration):
        effect = self.device.create_effect(guid, self._describe(duration, params))
        self.effects.append(effect)
        return effect

    def close(self):
        for effect in self.effects:
            for method in ("stop", "release"):
                try:
                    getattr(effect, method)()
                except Exception:                                 # noqa: BLE001
                    pass
        self.effects = []
        if self.acquired:
            try:
                self.device.unacquire()
            except Exception:                                     # noqa: BLE001
                pass
            self.acquired = False


def constant(mag):
    return DICONSTANTFORCE(lMagnitude=int(mag * DI_FFNOMINALMAX))


def ramp(start, end):
    return DIRAMPFORCE(lStart=int(start * DI_FFNOMINALMAX),
                       lEnd=int(end * DI_FFNOMINALMAX))


def periodic(mag, period_ms):
    return DIPERIODIC(dwMagnitude=int(mag * DI_FFNOMINALMAX), lOffset=0, dwPhase=0,
                      dwPeriod=period_ms * 1000)


def condition(strength, deadband=0.05):
    s = int(strength * DI_FFNOMINALMAX)
    return DICONDITION(lOffset=0, lPositiveCoefficient=-s, lNegativeCoefficient=-s,
                       dwPositiveSaturation=s, dwNegativeSaturation=s,
                       lDeadBand=int(deadband * DI_FFNOMINALMAX))


# ---------------------------------------------------------------------------
# The scripted test
# ---------------------------------------------------------------------------

# (label, GUID, type-specific params, seconds to hold). One of each shape the bridge will
# have to render: the two the HORI firmware honours natively, one periodic, one condition.
SIMPLE_PHASES = [
    ("Constant force  +40%", GUID_ConstantForce, constant(0.40), 1.0),
    ("Ramp  -40% -> +40%", GUID_RampForce, ramp(-0.40, 0.40), 1.0),
    ("Sine  50%, 100 ms period", GUID_Sine, periodic(0.50, 100), 1.0),
    ("Spring  60%", GUID_Spring, condition(0.60), 1.0),
]


def run_simple(sender, sink):
    rule("Phase 1 -- one effect at a time")
    print("  Each line below is a packet the vJoy driver delivered to our callback.")
    for label, guid, params, hold in SIMPLE_PHASES:
        print()
        print("  --- %s ---" % label)
        log.note("phase: %s" % label)
        before = len(sink.packets)
        try:
            effect = sender.create(guid, params, hold)
        except DirectInputError as exc:
            print("    CreateEffect FAILED: %s" % exc)
            continue
        effect.start(1, 0)
        time.sleep(hold + 0.2)
        effect.stop()
        time.sleep(0.2)
        got = len(sink.packets) - before
        if got == 0:
            print("    >>> NO PACKETS. The effect was created but nothing reached us.")


def run_concurrent(sender, sink):
    """
    The decisive test: three effects alive at the same time.

    A real game runs a centring spring, a damper and a road-texture periodic together and
    updates them independently. If all three arrive on the same block index, the bridge has
    no way to tell whose magnitude just changed, and B4 is unbuildable on this foundation.
    """
    rule("Phase 2 -- three concurrent effects (the block-index test)")
    log.note("phase: concurrent")

    wanted = [("Spring", GUID_Spring, condition(0.50)),
              ("Damper", GUID_Damper, condition(0.35)),
              ("Sine", GUID_Sine, periodic(0.30, 120))]

    created = []
    for label, guid, params in wanted:
        before = sink.snapshot()[1]
        try:
            effect = sender.create(guid, params, 3.0)
        except DirectInputError as exc:
            print("  %-8s CreateEffect FAILED: %s" % (label, exc))
            continue
        effect.start(1, 0)
        time.sleep(0.35)                       # let the packets land before the next one
        new = sink.snapshot()[1] - before
        created.append((label, effect, new))
        print("  %-8s started -> block index %s"
              % (label, ", ".join(str(b) for b in sorted(new)) or "NONE SEEN"))

    time.sleep(1.0)
    for _label, effect, _new in created:
        try:
            effect.stop()
        except Exception:                                         # noqa: BLE001
            pass
    time.sleep(0.3)
    return created


def _encoding_notes(sink, sent_gain_fraction):
    """
    Report what the wire format actually turned out to be.

    The bridge has to convert these numbers into torque, and two of them do NOT arrive in
    the units DirectInput was given: the driver rescales gain and duration on the way
    through. Getting either wrong is a quiet factor-of-40 error rather than a crash, so the
    values are measured here from the packets this run produced rather than assumed from
    the spec, and re-measured every time the spike is run.
    """
    with sink.lock:
        effects = [f for _t, rep, _b, f in sink.packets if rep == PT_EFFREP]
    if not effects:
        return

    sent_gain = int(max(0.0, min(1.0, sent_gain_fraction)) * DI_FFNOMINALMAX)
    gains = {f["Gain"] for f in effects}
    durations = {f["Duration"] for f in effects}
    dirs = {(f["DirX"], f["DirY"], f["Polar"]) for f in effects}

    print()
    print("  Wire format, as observed this run (for the B3 decoder):")
    print("    gain      : sent %d/%d to DirectInput, arrived as %s/255"
          % (sent_gain, DI_FFNOMINALMAX, ",".join(str(g) for g in sorted(gains))))
    print("                -> rescaled to a byte. Divide by 255, not by %d."
          % DI_FFNOMINALMAX)
    print("    duration  : sent microseconds, arrived as %s"
          % ",".join(str(d) for d in sorted(durations)))
    print("                -> milliseconds; 0 means INFINITE.")
    print("    direction : cartesian +X arrived as %s"
          % ", ".join("DirX=%d DirY=%d Polar=0x%02X" % d for d in sorted(dirs)))
    print("                -> converted to polar. Resolve the angle scale in B3 by")
    print("                   sending known directions; do not assume it.")
    print("    magnitudes: constant/periodic/condition all arrived unscaled at the")
    print("                DirectInput values (+-10000 full scale).")
    log.event("b0.encoding", gain_sent=sent_gain, gain_got=sorted(gains),
              durations=sorted(durations), directions=sorted(dirs))


def verdict(sink, created, ran_sender, gain=0.0):
    rule("B0 verdict")

    total, blocks = sink.snapshot()
    print("  packets received        : %d" % total)
    print("  decode failures         : %d" % sink.decode_failures)
    print("  effect block indices    : %s"
          % (", ".join(str(b) for b in sorted(blocks)) if blocks else "none"))
    if sink.block_types:
        for block in sorted(sink.block_types):
            print("      block %-3d -> %s" % (block, sink.block_types[block]))

    passed = True

    if total == 0:
        print()
        print("  >>> FAIL: the callback never fired.")
        print("      Nothing downstream can work. Per the plan, pivot to HIDMaestro")
        print("      rather than build on this.")
        return False

    if sink.decode_failures:
        print()
        print("  >>> FAIL: %d packets could not be decoded." % sink.decode_failures)
        passed = False

    if ran_sender:
        distinct = {label: sorted(new) for label, _e, new in created if new}
        if len(created) < 2:
            print()
            print("  >>> INCONCLUSIVE: fewer than two concurrent effects were created.")
            passed = False
        else:
            all_blocks = [b for blist in distinct.values() for b in blist]
            if len(set(all_blocks)) < len(distinct):
                print()
                print("  >>> FAIL: concurrent effects share a block index %s."
                      % sorted(set(all_blocks)))
                print("      This is the vJoy 2.1.9 behaviour. Concurrent effects cannot")
                print("      be told apart, so phase B4 is not buildable on this driver.")
                passed = False
            else:
                print()
                print("  >>> Block index VARIES across concurrent effects:")
                for label in distinct:
                    print("        %-8s -> %s"
                          % (label, ", ".join(str(b) for b in distinct[label])))

    if ran_sender:
        _encoding_notes(sink, gain)

    if passed:
        print()
        print("  >>> PASS. The callback fires, packets decode, and concurrent effects")
        print("      are separable. The bridge foundation holds -- proceed to B1.")
    log.event("b0.verdict", passed=passed, packets=total, blocks=len(blocks),
              failures=sink.decode_failures)
    return passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Track B phase B0 -- vJoy FFB callback spike")
    p.add_argument("--device", type=int, default=1, help="vJoy device id (default 1)")
    p.add_argument("--listen", action="store_true",
                   help="do not send anything; just log what arrives (drive it from a game)")
    p.add_argument("--gain", type=float, default=0.5,
                   help="DirectInput effect gain 0.0-1.0 (default 0.5). Only reaches the "
                        "virtual device -- nothing is sent to the real wheel.")
    p.add_argument("--no-log", action="store_true", help="do not write a log file")
    return p.parse_args()


def main():
    args = parse_args()
    if not args.no_log:
        path = log.start(prefix="vjoy_ffb")
        if path:
            print("Logging to %s" % path)

    print(__doc__.strip().split("\n\n")[0])

    if not preflight(args.device):
        rule("B0 verdict")
        print("  >>> Preflight failed. Fix the above before the callback test means")
        print("      anything -- a silent callback would be explained by it.")
        return 1

    sink = PacketSink()
    device = None
    dinput = None
    sender = None
    created = []

    try:
        rule("Registering the FFB callback")
        device = pyvjoy.VJoyDevice(args.device)
        print("  acquired vJoy device %d" % args.device)
        device.ffb_register_callback(sink)
        print("  callback registered")

        # FfbStart is what actually opens the data flow on some driver builds. pyvjoy does
        # not wrap it, so call it directly; harmless where it is not required.
        started = sdk._vj.FfbStart(args.device)
        print("  FfbStart(%d) -> %s" % (args.device, "ok" if started else "0 (ignored)"))
        sink.started = time.monotonic()

        if args.listen:
            rule("Listening -- press Ctrl+C to stop")
            print("  Nothing is being sent. Start a game, or any DirectInput app, and")
            print("  every force-feedback packet it sends to vJoy will appear here.")
            try:
                while True:
                    time.sleep(0.2)
            except KeyboardInterrupt:
                print("\n  stopped.")
            return 0 if verdict(sink, [], ran_sender=False) else 1

        rule("Opening the virtual device as a DirectInput client")
        dinput = di.create_direct_input()
        instance = find_vjoy_device(dinput)
        if instance is None:
            return 1

        vjoy_di = dinput.create_device(instance.guidInstance)
        sender = Sender(vjoy_di, args.gain)
        sender.open()
        print("  acquired exclusively, gain %d/%d"
              % (int(args.gain * DI_FFNOMINALMAX), DI_FFNOMINALMAX))

        run_simple(sender, sink)
        created = run_concurrent(sender, sink)

        # Tear down BEFORE judging. Releasing the effects emits a further STOP and
        # BlockFree per effect and a final DEVICE_RESET; leaving that to the finally block
        # printed a dozen packets underneath the verdict and left them out of its counts.
        sender.close()
        time.sleep(0.3)
        return 0 if verdict(sink, created, ran_sender=True, gain=args.gain) else 1

    except KeyboardInterrupt:
        print("\n  interrupted.")
        return 130
    except DirectInputError as exc:
        print("\n  DirectInput error: %s" % exc)
        return 1
    finally:
        if sender is not None:
            sender.close()
        if device is not None:
            try:
                sdk._vj.FfbStop(args.device)
            except Exception:                                     # noqa: BLE001
                pass
        # VJoyDevice.__del__ relinquishes; drop it deterministically instead of at exit.
        device = None
        if not args.no_log:
            log.stop()


if __name__ == "__main__":
    sys.exit(main())
