"""
dinput_probe.py -- Does DirectInput expose force feedback on this wheel?

Companion to probe.py. That tool proved GameInput exposes no force-feedback motors on the
HORI wheel in either PC or Xbox mode. But HORI publishes a PC/Windows compatibility table
listing ~25 titles with working force feedback on this exact model, so a working FFB path
on Windows must exist -- and by elimination it is DirectInput.

This tool checks that directly, in two stages:

  Stage 1 (passive)  -- enumerate game controllers, ask DirectInput which of them report
                        DIDC_FORCEFEEDBACK, and list the effect types each one supports.
                        Creates nothing, acquires nothing, touches no motor.

  Stage 2 (active)   -- acquire the wheel exclusively and play a gentle constant force,
                        then a spring. Only runs if stage 1 found a force-feedback device.

IMPORTANT: put the wheel in PC mode first (hold the PROFILE button ~3 seconds). In Xbox
mode it presents as a GIP/XInput device and DirectInput will not see its force feedback.

Safety: the effect gain defaults to 35% of DirectInput's nominal maximum and the magnitude
to 30%. Every exit path stops the effect, unacquires the device and releases it. Ctrl+C is
handled the same way.

Usage:
    python dinput_probe.py                 # stage 1, then stage 2 if warranted
    python dinput_probe.py --list-only     # stage 1 only, completely passive
    python dinput_probe.py --gain 0.6      # stronger (0.0-1.0)
    python dinput_probe.py --magnitude 0.5 # stronger (0.0-1.0)
    python dinput_probe.py --duration 4    # hold each effect longer
"""

import argparse
import ctypes
import sys
import time
from ctypes import byref, c_int32, c_uint32, c_void_p

import dinput_abi as di
from dinput_abi import (
    DI_FFNOMINALMAX,
    DIDC_FORCEFEEDBACK,
    DIDC_NAMES,
    DIEB_NOTRIGGER,
    DIEFF_CARTESIAN,
    DIEFF_OBJECTOFFSETS,
    DIEFT_TYPE_NAMES,
    DIENUM_CONTINUE,
    DISCL_BACKGROUND,
    DISCL_EXCLUSIVE,
    DI8DEVCLASS_GAMECTRL,
    DIEDFL_ATTACHEDONLY,
    DIEDFL_FORCEFEEDBACK,
    KNOWN_EFFECT_GUIDS,
    DICONDITION,
    DICONSTANTFORCE,
    DIEFFECT,
    DIEFFECTINFOW,
    DIRECTINPUT_VERSION,
    DirectInputError,
    GUID_ConstantForce,
    GUID_Spring,
    LPDIENUMDEVICESCALLBACKW,
    LPDIENUMEFFECTSCALLBACKW,
    dieft_gettype,
)

HORI_VENDOR_ID = 0x0F0D


def rule(title=""):
    if title:
        print("\n" + "=" * 78)
        print(title)
        print("=" * 78)
    else:
        print("-" * 78)


def decode_caps(flags):
    hits = [name for name, bit in DIDC_NAMES if flags & bit]
    return "|".join(hits) if hits else "none"


def vid_pid_from_product_guid(guid):
    """
    For HID devices DirectInput encodes the USB ids in guidProduct's Data1 as
    (pid << 16) | vid. That is how we match the wheel without relying on its name.
    """
    return guid.Data1 & 0xFFFF, (guid.Data1 >> 16) & 0xFFFF


# ---------------------------------------------------------------------------
# Stage 1: enumeration and capability report
# ---------------------------------------------------------------------------

def enumerate_devices(dinput, ff_only=False):
    """Collect DIDEVICEINSTANCEW records for attached game controllers."""
    found = []

    @LPDIENUMDEVICESCALLBACKW
    def on_device(instance_ptr, _ref):
        inst = instance_ptr.contents
        # Copy out -- DirectInput reuses its buffer between callbacks.
        copy = di.DIDEVICEINSTANCEW()
        ctypes.memmove(byref(copy), byref(inst), ctypes.sizeof(copy))
        found.append(copy)
        return DIENUM_CONTINUE

    flags = DIEDFL_ATTACHEDONLY | (DIEDFL_FORCEFEEDBACK if ff_only else 0)
    dinput.enum_devices(DI8DEVCLASS_GAMECTRL, on_device, flags)
    return found


def list_effects(device):
    """Ask the device which effect types it supports."""
    effects = []

    @LPDIENUMEFFECTSCALLBACKW
    def on_effect(info_ptr, _ref):
        info = info_ptr.contents
        copy = DIEFFECTINFOW()
        ctypes.memmove(byref(copy), byref(info), ctypes.sizeof(copy))
        effects.append(copy)
        return DIENUM_CONTINUE

    try:
        device.enum_effects(on_effect)
    except DirectInputError as exc:
        print("      EnumEffects failed: %s" % exc)
    return effects


def report_device(dinput, instance, index, ff_guids):
    vid, pid = vid_pid_from_product_guid(instance.guidProduct)
    is_ff = bytes(instance.guidInstance) in ff_guids

    print("  [%d] %s" % (index, instance.tszProductName))
    if instance.tszInstanceName != instance.tszProductName:
        print("       instance name : %s" % instance.tszInstanceName)
    print("       VID 0x%04X  PID 0x%04X   HID usage %d/%d"
          % (vid, pid, instance.wUsagePage, instance.wUsage))
    print("       force feedback enumeration filter : %s"
          % ("MATCHED (DirectInput considers this an FFB device)" if is_ff else "not matched"))

    device = None
    try:
        device = dinput.create_device(instance.guidInstance)
    except DirectInputError as exc:
        print("       CreateDevice failed: %s" % exc)
        return None, None

    try:
        caps = device.get_capabilities()
    except DirectInputError as exc:
        print("       GetCapabilities failed: %s" % exc)
        device.release()
        return None, None

    has_ff = bool(caps.dwFlags & DIDC_FORCEFEEDBACK)
    print("       axes=%d  buttons=%d  POVs=%d" % (caps.dwAxes, caps.dwButtons, caps.dwPOVs))
    print("       capability flags : %s" % decode_caps(caps.dwFlags))
    print("       DIDC_FORCEFEEDBACK : %s" % ("YES" if has_ff else "no"))

    if has_ff:
        print("       FF sample period=%dus  min time resolution=%dus  driver version=0x%X"
              % (caps.dwFFSamplePeriod, caps.dwFFMinTimeResolution, caps.dwFFDriverVersion))
        effects = list_effects(device)
        if effects:
            print("       supported effects (%d):" % len(effects))
            for e in effects:
                kind = DIEFT_TYPE_NAMES.get(dieft_gettype(e.dwEffType), "type %d" % dieft_gettype(e.dwEffType))
                known = KNOWN_EFFECT_GUIDS.get(bytes(e.guid), "")
                label = known or e.tszName
                print("         %-18s  [%s]  %s" % (label, kind, e.guid))
        else:
            print("       supported effects: none reported")

    if vid == HORI_VENDOR_ID:
        print("       >>> HORI device (vendor 0x0F0D) <<<")

    return device, caps


# ---------------------------------------------------------------------------
# Stage 2: actually play an effect
# ---------------------------------------------------------------------------

class EffectSession:
    """Holds the exclusive acquisition and guarantees teardown."""

    def __init__(self, device):
        self.device = device
        self.effect = None
        self.acquired = False
        self._fmt = None
        self._objs = None

    def open(self):
        hwnd = di.get_console_hwnd()
        if not hwnd:
            raise RuntimeError(
                "No console window handle available. Force feedback needs exclusive "
                "access, which needs a real window. Run this from a normal terminal.")

        # SetDataFormat must precede Acquire, even though we never read device state.
        self._fmt, self._objs = di.make_axis_data_format()
        self.device.set_data_format(self._fmt)

        # EXCLUSIVE is mandatory for force feedback; BACKGROUND keeps it working if the
        # console loses focus mid-test.
        self.device.set_cooperative_level(hwnd, DISCL_EXCLUSIVE | DISCL_BACKGROUND)
        self.device.acquire()
        self.acquired = True

    def _base_effect(self, gain, duration, params, params_size):
        axes = (c_uint32 * 1)(0)          # offset 0 == first axis in our data format
        direction = (c_int32 * 1)(1)      # cartesian, positive direction

        effect = DIEFFECT()
        effect.dwSize = ctypes.sizeof(DIEFFECT)
        effect.dwFlags = DIEFF_OBJECTOFFSETS | DIEFF_CARTESIAN
        effect.dwDuration = int(duration * 1_000_000)
        effect.dwSamplePeriod = 0
        effect.dwGain = int(max(0.0, min(1.0, gain)) * DI_FFNOMINALMAX)
        effect.dwTriggerButton = DIEB_NOTRIGGER
        effect.dwTriggerRepeatInterval = 0
        effect.cAxes = 1
        effect.rgdwAxes = axes
        effect.rglDirection = direction
        effect.lpEnvelope = None
        effect.cbTypeSpecificParams = params_size
        effect.lpvTypeSpecificParams = ctypes.cast(byref(params), c_void_p)
        effect.dwStartDelay = 0
        # Keep the referenced buffers alive for as long as the effect struct is used.
        effect._keepalive = (axes, direction, params)
        return effect

    def run(self, label, guid, params, gain, duration):
        print()
        print("  --- %s ---" % label)
        print("  gain=%d/%d  duration=%.1fs"
              % (int(gain * DI_FFNOMINALMAX), DI_FFNOMINALMAX, duration))

        effect_desc = self._base_effect(gain, duration, params, ctypes.sizeof(params))

        try:
            self.effect = self.device.create_effect(guid, effect_desc)
        except DirectInputError as exc:
            print("  CreateEffect FAILED: %s" % exc)
            return False

        try:
            for n in (3, 2, 1):
                print("    starting in %d..." % n, flush=True)
                time.sleep(0.6)

            print("  >>> FORCE ON  -- the wheel should resist NOW <<<", flush=True)
            self.effect.start(1, 0)
            time.sleep(duration)
            self.effect.stop()
            print("  <<< FORCE OFF -- the wheel should be free again >>>", flush=True)
            return True
        finally:
            try:
                self.effect.stop()
            except Exception:
                pass
            self.effect.release()
            self.effect = None

    def close(self):
        if self.effect is not None:
            try:
                self.effect.stop()
            except Exception:
                pass
            try:
                self.effect.release()
            except Exception:
                pass
            self.effect = None
        if self.acquired:
            try:
                self.device.unacquire()
            except Exception:
                pass
            self.acquired = False


def constant_force_params(magnitude):
    """Signed magnitude, -10000..10000."""
    return DICONSTANTFORCE(lMagnitude=int(magnitude * DI_FFNOMINALMAX))


def spring_params(magnitude):
    """
    Centring spring. Negative coefficients resist the player's steering; positive values
    would drive the wheel further into the turn.
    """
    strength = int(magnitude * DI_FFNOMINALMAX)
    return DICONDITION(
        lOffset=0,                        # centre on the wheel's natural centre
        lPositiveCoefficient=-strength,
        lNegativeCoefficient=-strength,
        dwPositiveSaturation=strength,
        dwNegativeSaturation=strength,
        lDeadBand=int(0.05 * DI_FFNOMINALMAX),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="DirectInput force-feedback probe")
    p.add_argument("--gain", type=float, default=0.35, help="effect gain 0.0-1.0 (default 0.35)")
    p.add_argument("--magnitude", type=float, default=0.30,
                   help="effect magnitude 0.0-1.0 (default 0.30)")
    p.add_argument("--duration", type=float, default=2.0,
                   help="seconds to hold each effect (default 2.0)")
    p.add_argument("--vid", type=lambda s: int(s, 0), default=None, help="select by vendor id")
    p.add_argument("--list-only", action="store_true",
                   help="stage 1 only: report capabilities, create nothing")
    return p.parse_args()


def main():
    args = parse_args()

    rule("DirectInput force-feedback probe")
    print("  ABI source header : %s" % di.SOURCE_HEADER)
    print("  DirectInput version: 0x%04X" % DIRECTINPUT_VERSION)
    print("  Python            : %s" % sys.version.split()[0])
    print()
    print("  NOTE: the wheel must be in PC mode (hold PROFILE ~3s). In Xbox mode it")
    print("        presents as a GIP/XInput device and DirectInput cannot drive its FFB.")

    try:
        dinput = di.create_direct_input()
    except (OSError, DirectInputError) as exc:
        print("\n  Could not initialise DirectInput: %s" % exc)
        return 2

    session = None
    device = None
    try:
        # Which devices does DirectInput itself consider force-feedback capable?
        ff_instances = enumerate_devices(dinput, ff_only=True)
        ff_guids = {bytes(i.guidInstance) for i in ff_instances}

        rule("Attached game controllers")
        instances = enumerate_devices(dinput, ff_only=False)
        if not instances:
            print("  No game controllers enumerated.")
            return 1

        chosen = None
        for index, instance in enumerate(instances):
            dev, caps = report_device(dinput, instance, index, ff_guids)
            print()
            if dev is None:
                continue
            vid, _pid = vid_pid_from_product_guid(instance.guidProduct)
            wanted = (vid == args.vid) if args.vid is not None else (vid == HORI_VENDOR_ID)
            if chosen is None and wanted and caps.dwFlags & DIDC_FORCEFEEDBACK:
                chosen = dev
            else:
                dev.release()

        rule("STAGE 1 RESULT")
        print("  Devices DirectInput reports as force-feedback capable: %d" % len(ff_instances))
        for i in ff_instances:
            vid, pid = vid_pid_from_product_guid(i.guidProduct)
            print("    - %s (VID 0x%04X PID 0x%04X)" % (i.tszProductName, vid, pid))

        if chosen is None:
            print()
            print("  No force-feedback capable HORI device found.")
            print("  If the wheel is in Xbox mode, switch it to PC mode and re-run.")
            return 1

        device = chosen
        if args.list_only:
            print("\n  --list-only given; stopping before creating any effect.")
            return 0

        # --- Stage 2 --------------------------------------------------------
        rule("STAGE 2 -- FORCE TEST (keep a light grip on the wheel)")
        session = EffectSession(device)
        session.open()
        print("  Acquired exclusively, data format set.")

        session.run("Constant force", GUID_ConstantForce,
                    constant_force_params(args.magnitude), args.gain, args.duration)

        print()
        print("  Next: a centring SPRING. Turn the wheel while it runs -- it should")
        print("  pull back toward centre.")
        session.run("Spring", GUID_Spring,
                    spring_params(args.magnitude), args.gain, args.duration)

        rule("DONE")
        print("  If you felt resistance in either test, DirectInput force feedback works")
        print("  on this wheel and the GameInput result was a dead end, not a dead wheel.")
        return 0

    except KeyboardInterrupt:
        print("\n\n  Ctrl+C -- stopping force feedback.")
        return 130
    except DirectInputError as exc:
        print("\n  %s" % exc)
        return 3
    except RuntimeError as exc:
        print("\n  %s" % exc)
        return 4
    finally:
        if session is not None:
            session.close()
        if device is not None:
            device.release()
        print("\n  Effects stopped, device released.")


if __name__ == "__main__":
    sys.exit(main())
