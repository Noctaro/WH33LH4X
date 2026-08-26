"""
probe.py -- Does GameInput expose real force-feedback motors on this wheel?

A standalone diagnostic. No game engine, no DirectInput, no XInput. It answers two
questions about a connected controller:

  1. Does GameInput report force-feedback MOTORS on it (forceFeedbackMotorCount >= 1)?
  2. If so, which effect types does the firmware actually honour, and can we feel torque?

Background for this specific machine: the HORI wheel (VID 0x0F0D) enumerates as a normal
DirectInput joystick but has no DirectInput force-feedback interface -- the FFB tab is
missing from joy.cpl. This tool checks whether the modern GameInput API can reach the
motors that DirectInput cannot.

The wheel has a PC/Xbox mode button, and that matters a great deal:

  * PC mode   -> PID 0x015D, enumerates as HID. GameInput's PC force-feedback path is
                 specified against the USB Physical Interface Device (PID 1.0) class. No
                 joy.cpl FFB tab is direct evidence that no PID collection is published,
                 so 0 motors here is an honest result rather than a bug.
  * Xbox mode -> PID 0x015C, enumerates as XboxComposite (GIP). GIP is GameInput's native
                 driver model and carries force feedback over the protocol instead of HID
                 PID descriptors. If real torque is reachable at all, it is reachable here.

So: run this in PC mode, then press the mode button and run it again. Press 'r' in the
menu to re-enumerate without restarting.

SAFETY
------
Torque is capped in two independent places before any effect exists: a master gain on the
motor itself (--gain, default 0.35) and the effect magnitude (--magnitude, default 0.30).
Every exit path -- normal, exception, or Ctrl+C -- stops all effects and sets every motor
gain to 0.0. Keep a light grip the first time.

Usage:
    python probe.py                     # enumerate, report, scripted test, then menu
    python probe.py --list-only         # just enumerate and dump info, touch nothing
    python probe.py --gain 0.5          # stronger master gain (0.0-1.0)
    python probe.py --vid 0x0F0D        # force device selection by vendor id
    python probe.py --oem 0x0F0D:0x015D # try EnableOemDeviceSupport before enumerating
"""

import argparse
import ctypes
import sys
import time
from ctypes import c_void_p

import evidence.gameinput_abi as abi
from evidence.gameinput_abi import (
    EFFECT_CATALOG,
    GameInputDeviceCallback,
    GameInputDeviceFamily,
    GameInputDeviceStatus,
    GameInputEnumerationKind,
    GameInputError,
    GameInputFeedbackAxes,
    GameInputFeedbackEffectState,
    GameInputForceFeedbackConditionParams,
    GameInputForceFeedbackConstantParams,
    GameInputForceFeedbackEffectKind,
    GameInputForceFeedbackEnvelope,
    GameInputForceFeedbackMagnitude,
    GameInputForceFeedbackParams,
    GameInputForceFeedbackPeriodicParams,
    GameInputForceFeedbackRampParams,
    GameInputFocusPolicy,
    GameInputKind,
    GameInputRumbleMotors,
    IGameInputDevice,
    decode_flags,
)

HORI_VENDOR_ID = 0x0F0D

# Effect kinds that use GameInputForceFeedbackConditionParams rather than an envelope.
CONDITION_KINDS = {
    GameInputForceFeedbackEffectKind.Spring,
    GameInputForceFeedbackEffectKind.Friction,
    GameInputForceFeedbackEffectKind.Damper,
    GameInputForceFeedbackEffectKind.Inertia,
}
PERIODIC_KINDS = {
    GameInputForceFeedbackEffectKind.SineWave,
    GameInputForceFeedbackEffectKind.SquareWave,
    GameInputForceFeedbackEffectKind.TriangleWave,
    GameInputForceFeedbackEffectKind.SawtoothUpWave,
    GameInputForceFeedbackEffectKind.SawtoothDownWave,
}


def rule(title=""):
    if title:
        print("\n" + "=" * 78)
        print(title)
        print("=" * 78)
    else:
        print("-" * 78)


# ---------------------------------------------------------------------------
# Step 1-3: create GameInput, set focus policy, enumerate devices
# ---------------------------------------------------------------------------

def enumerate_devices(game_input):
    """
    Collect every currently-connected device.

    GameInput has no "give me a list" call. The documented way to see what is already
    connected is to register a device callback with GameInputBlockingEnumeration, which
    fires the callback once per connected device *before* RegisterDeviceCallback returns.
    """
    devices = []

    @GameInputDeviceCallback
    def on_device(token, context, device_ptr, timestamp, current_status, previous_status):
        if not device_ptr:
            return
        # The pointer we are handed is not ours to keep past this callback, so AddRef it.
        device = IGameInputDevice(c_void_p(device_ptr))
        device.add_ref()
        devices.append(device)

    token = game_input.register_device_callback(
        input_kind=GameInputKind.ANY,
        status_filter=GameInputDeviceStatus.AnyStatus,
        enumeration_kind=GameInputEnumerationKind.BlockingEnumeration,
        callback=on_device,
    )
    try:
        game_input.unregister_callback(token)
    except Exception:
        pass  # unregister is best-effort; the enumeration already happened

    return devices


# HID Generic Desktop usages worth naming; a wheel normally appears as Joystick or Gamepad.
_HID_GENERIC_DESKTOP = {
    0x0002: "Mouse",
    0x0004: "Joystick",
    0x0005: "Gamepad",
    0x0006: "Keyboard",
    0x0008: "Multi-axis Controller",
}


def describe_usage(usage):
    if usage.page == 0x0001:
        return "0x%04X/0x%04X (Generic Desktop / %s)" % (
            usage.page, usage.id, _HID_GENERIC_DESKTOP.get(usage.id, "usage 0x%04X" % usage.id))
    return "0x%04X/0x%04X" % (usage.page, usage.id)


def describe_device(device, index):
    """One row of the device table, plus the layout self-check on the first device."""
    info = device.get_device_info()

    name = ""
    if info.displayName:
        name = info.displayName.contents.text()
    if not name:
        # Not a bug: on this GameInput build displayName is NULL for plain HID devices.
        # Identify the device by VID/PID and HID usage instead.
        name = "(no display name reported)"

    family = GameInputDeviceFamily._NAMES.get(info.deviceFamily, str(info.deviceFamily))

    print("  [%d] VID 0x%04X  PID 0x%04X   %s" % (index, info.vendorId, info.productId, name))
    print("       family=%-14s  status=%s"
          % (family, decode_flags(device.get_device_status(), GameInputDeviceStatus._NAMES)))
    print("       HID usage   : %s" % describe_usage(info.usage))
    print("       input kinds : %s" % decode_flags(info.supportedInput, GameInputKind._NAMES))
    print("       FF motors=%-3d  haptic motors=%-3d  rumble=%s"
          % (info.forceFeedbackMotorCount,
             info.hapticFeedbackMotorCount,
             decode_flags(info.supportedRumbleMotors, GameInputRumbleMotors._NAMES)))
    print("       iface=%d col=%d  reports in/out/feature=%d/%d/%d  axes=%d buttons=%d switches=%d"
          % (info.interfaceNumber, info.collectionNumber,
             info.inputReportCount, info.outputReportCount, info.featureReportCount,
             info.controllerAxisCount, info.controllerButtonCount, info.controllerSwitchCount))

    if info.vendorId == HORI_VENDOR_ID:
        print("       >>> HORI device (vendor 0x0F0D) <<<")
    return info


def validate_layout(info):
    """
    The gate. GameInputDeviceInfo.infoSize is filled in by the runtime with its own
    sizeof(). If it disagrees with ours, our struct transcription is wrong and every
    field after the mismatch is garbage -- including forceFeedbackMotorCount. Stop
    rather than report a confident lie.
    """
    ours = ctypes.sizeof(abi.GameInputDeviceInfo)
    theirs = info.infoSize
    print("  struct layout check: runtime infoSize=%d, our sizeof=%d -> %s"
          % (theirs, ours, "MATCH" if theirs == ours else "MISMATCH"))
    if theirs != ours:
        print()
        print("  ABORTING. The ctypes transcription does not match this GameInput build,")
        print("  so no value read from it can be trusted. The header this was built from:")
        print("    %s" % abi.SOURCE_HEADER)
        return False
    return True


# ---------------------------------------------------------------------------
# Step 4: motor capability dump
# ---------------------------------------------------------------------------

def dump_motors(device, info):
    """Print the full GameInputForceFeedbackMotorInfo for every motor."""
    if info.forceFeedbackMotorCount == 0:
        print("  forceFeedbackMotorCount = 0")
        print()
        print("  GameInput reports NO force-feedback motors on this device.")
        if info.supportedRumbleMotors:
            print("  It does report rumble motors (%s)."
                  % decode_flags(info.supportedRumbleMotors, GameInputRumbleMotors._NAMES))
            print("  That is the signature of a rumble-only device, not true force feedback.")
        if info.outputReportCount:
            print("  It has %d raw output report(s) -- a vendor-specific feedback protocol"
                  % info.outputReportCount)
            print("  would live there. See the output report listing below.")
        else:
            print("  It also exposes no raw OUTPUT reports, so GameInput offers no channel")
            print("  to the device at all in this mode -- not even a vendor-specific one.")
        return []

    print("  forceFeedbackMotorCount = %d" % info.forceFeedbackMotorCount)
    motors = []
    for i in range(info.forceFeedbackMotorCount):
        motor = info.forceFeedbackMotorInfo[i]
        motors.append(motor)
        rule()
        print("  Motor %d" % i)
        print("    supportedAxes          : %s"
              % decode_flags(motor.supportedAxes, GameInputFeedbackAxes._NAMES))
        print("    location               : %s (id %d)"
              % (abi.GameInputLocation._NAMES.get(motor.location, motor.location),
                 motor.locationId))
        print("    maxSimultaneousEffects : %d" % motor.maxSimultaneousEffects)
        try:
            powered = device.is_force_feedback_motor_powered_on(i)
            print("    motor powered on       : %s" % powered)
        except Exception as exc:
            print("    motor powered on       : query failed (%s)" % exc)
        print("    supported effects:")
        for _kind, label, attr, _member in EFFECT_CATALOG:
            print("      %-14s : %s" % (label, "YES" if getattr(motor, attr) else "no"))
    return motors


def dump_racing_wheel_info(info):
    """
    If GameInput mapped this as a racing wheel, show what it understood. This matters for
    interpreting a zero motor count: a fully populated wheel mapping means GameInput knows
    exactly what the device is and is still declining to expose force-feedback motors.
    """
    if not info.racingWheelInfo:
        return
    w = info.racingWheelInfo.contents
    rule()
    print("  GameInput mapped this device as a RACING WHEEL:")
    print("    maxWheelAngle       : %.1f degrees" % w.maxWheelAngle)
    print("    hasClutch           : %s" % w.hasClutch)
    print("    hasHandbrake        : %s" % w.hasHandbrake)
    print("    hasPatternShifter   : %s" % w.hasPatternShifter)
    if w.hasPatternShifter:
        print("    shifter gear range  : %d .. %d"
              % (w.minPatternShifterGear, w.maxPatternShifterGear))


def dump_raw_reports(info):
    """
    Raw HID reports GameInput is willing to expose.

    OUTPUT reports matter most here: if a device has no force-feedback motors but does
    have output reports, a vendor-specific feedback protocol could still be driven
    through them. No output reports at all means GameInput offers no way to send the
    device anything.
    """
    for label, count, array in (
        ("INPUT", info.inputReportCount, info.inputReportInfo),
        ("OUTPUT", info.outputReportCount, info.outputReportInfo),
        ("FEATURE", info.featureReportCount, info.featureReportInfo),
    ):
        rule()
        if not count or not array:
            print("  Raw %s reports: none" % label)
            continue
        print("  Raw %s reports (%d):" % (label, count))
        for i in range(count):
            r = array[i]
            print("    report id=0x%02X  size=%d bytes  items=%d" % (r.id, r.size, r.itemCount))


# ---------------------------------------------------------------------------
# Effect construction
# ---------------------------------------------------------------------------

def make_envelope(duration_seconds):
    """A flat envelope: full strength immediately, hold, stop. No attack/release ramp."""
    return GameInputForceFeedbackEnvelope(
        attackDuration=0,
        sustainDuration=int(duration_seconds * 1_000_000),  # microseconds
        releaseDuration=0,
        attackGain=1.0,
        sustainGain=1.0,
        releaseGain=1.0,
        playCount=1,
        repeatDelay=0,
    )


def make_params(kind, magnitude, duration_seconds):
    """
    Build GameInputForceFeedbackParams for one effect kind.

    Magnitude always goes on angularX. Microsoft's docs are explicit that on PC, HID
    devices restrict force feedback to the AngularX axis regardless of what other axes
    the hardware advertises -- putting force on any other axis is the most likely way to
    get a silent no-op on a wheel.
    """
    params = GameInputForceFeedbackParams()
    params.kind = kind

    envelope = make_envelope(duration_seconds)
    mag = GameInputForceFeedbackMagnitude(angularX=magnitude)

    if kind in CONDITION_KINDS:
        # Condition effects respond to wheel POSITION rather than playing a waveform.
        # Negative coefficients resist the player's motion; positive values would make
        # the wheel accelerate INTO the turn, which is the opposite of a centering spring.
        condition = GameInputForceFeedbackConditionParams(
            magnitude=mag,
            positiveCoefficient=-1.0,
            negativeCoefficient=-1.0,
            maxPositiveMagnitude=magnitude,
            maxNegativeMagnitude=magnitude,
            deadZone=0.05,  # small slack around centre before force engages
            bias=0.0,       # 0.0 == the wheel's natural centre
        )
        for k, _label, _attr, member in EFFECT_CATALOG:
            if k == kind:
                setattr(params.data, member, condition)
        return params

    if kind in PERIODIC_KINDS:
        periodic = GameInputForceFeedbackPeriodicParams(
            envelope=envelope, magnitude=mag, frequency=2.0, phase=0.0, bias=0.0
        )
        for k, _label, _attr, member in EFFECT_CATALOG:
            if k == kind:
                setattr(params.data, member, periodic)
        return params

    if kind == GameInputForceFeedbackEffectKind.Ramp:
        params.data.ramp = GameInputForceFeedbackRampParams(
            envelope=envelope,
            startMagnitude=GameInputForceFeedbackMagnitude(angularX=0.0),
            endMagnitude=mag,
        )
        return params

    # Constant
    params.data.constant = GameInputForceFeedbackConstantParams(
        envelope=envelope, magnitude=mag
    )
    return params


# ---------------------------------------------------------------------------
# Running one effect
# ---------------------------------------------------------------------------

class EffectRunner:
    """Creates, runs and unconditionally cleans up force-feedback effects."""

    def __init__(self, device, motor_index, motor_gain):
        self.device = device
        self.motor_index = motor_index
        self.motor_gain = motor_gain
        self.live = []

    def arm(self):
        """Cap torque at the motor before any effect is created."""
        print("  Setting motor %d master gain to %.2f (torque limiter)."
              % (self.motor_index, self.motor_gain))
        self.device.set_force_feedback_motor_gain(self.motor_index, self.motor_gain)

    def run(self, kind, label, magnitude, duration):
        print()
        print("  --- %s effect ---" % label)
        print("  magnitude=%.2f on angularX, motor gain=%.2f, duration=%.1fs"
              % (magnitude, self.motor_gain, duration))

        params = make_params(kind, magnitude, duration)

        try:
            effect = self.device.create_force_feedback_effect(self.motor_index, params)
        except GameInputError as exc:
            print("  CreateForceFeedbackEffect FAILED: %s" % exc)
            print("  -> the firmware does not accept this effect kind.")
            return False

        self.live.append(effect)
        try:
            for n in (3, 2, 1):
                print("    starting in %d..." % n, flush=True)
                time.sleep(0.6)

            print("  >>> FORCE ON  -- the wheel should resist NOW <<<", flush=True)
            effect.set_state(GameInputFeedbackEffectState.Running)

            state = effect.get_state()
            print("      (effect state reported as %s)"
                  % GameInputFeedbackEffectState._NAMES.get(state, state))

            time.sleep(duration)

            effect.set_state(GameInputFeedbackEffectState.Stopped)
            print("  <<< FORCE OFF -- the wheel should be free again >>>", flush=True)
            return True
        finally:
            effect.set_state(GameInputFeedbackEffectState.Stopped)
            effect.release()
            if effect in self.live:
                self.live.remove(effect)

    def panic_stop(self):
        """Every exit path lands here. Never leave the motor energised."""
        for effect in list(self.live):
            try:
                effect.set_state(GameInputFeedbackEffectState.Stopped)
                effect.release()
            except Exception:
                pass
        self.live.clear()
        try:
            self.device.set_force_feedback_motor_gain(self.motor_index, 0.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------

def interactive_menu(runner, motor, magnitude, duration, reenumerate):
    supported = [(k, label) for k, label, attr, _m in EFFECT_CATALOG if getattr(motor, attr)]

    while True:
        rule("INTERACTIVE -- fire effects on demand")
        print("  motor gain=%.2f   magnitude=%.2f   duration=%.1fs" %
              (runner.motor_gain, magnitude, duration))
        print()
        for i, (_kind, label) in enumerate(supported, start=1):
            print("   %2d) %s" % (i, label))
        print()
        print("    g) set motor master gain      m) set magnitude")
        print("    d) set duration               i) re-print device info")
        print("    r) re-enumerate devices (do this after flipping the mode switch)")
        print("    s) panic stop                 q) quit")
        print()

        try:
            choice = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"

        if choice == "q":
            return "quit"
        if choice == "r":
            return "reenumerate"
        if choice == "s":
            runner.panic_stop()
            runner.arm()
            print("  Stopped everything and re-armed.")
            continue
        if choice == "i":
            return "info"
        if choice in ("g", "m", "d"):
            prompt = {"g": "motor gain 0.0-1.0", "m": "magnitude 0.0-1.0",
                      "d": "duration seconds"}[choice]
            try:
                value = float(input("    new %s: " % prompt).strip())
            except (ValueError, EOFError, KeyboardInterrupt):
                print("    unchanged.")
                continue
            if choice == "g":
                runner.motor_gain = max(0.0, min(1.0, value))
                runner.arm()
            elif choice == "m":
                magnitude = max(0.0, min(1.0, value))
            else:
                duration = max(0.1, min(30.0, value))
            continue

        if choice.isdigit() and 1 <= int(choice) <= len(supported):
            kind, label = supported[int(choice) - 1]
            try:
                runner.run(kind, label, magnitude, duration)
            except KeyboardInterrupt:
                print("\n  Interrupted -- stopping.")
                runner.panic_stop()
                runner.arm()
            continue

        print("  Unrecognised choice.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def pick_device(devices, infos, want_vid, want_pid):
    """Prefer an explicit --vid/--pid, then a HORI device, then anything with motors."""
    for device, info in zip(devices, infos):
        if want_vid is not None and info.vendorId != want_vid:
            continue
        if want_pid is not None and info.productId != want_pid:
            continue
        if want_vid is not None or want_pid is not None:
            return device, info

    if want_vid is not None or want_pid is not None:
        return None, None

    for device, info in zip(devices, infos):
        if info.vendorId == HORI_VENDOR_ID:
            return device, info
    for device, info in zip(devices, infos):
        if info.forceFeedbackMotorCount > 0:
            return device, info
    return None, None


def parse_args():
    p = argparse.ArgumentParser(description="GameInput force-feedback probe")
    p.add_argument("--vid", type=lambda s: int(s, 0), default=None,
                   help="select device by vendor id, e.g. 0x0F0D")
    p.add_argument("--pid", type=lambda s: int(s, 0), default=None,
                   help="select device by product id, e.g. 0x015D")
    p.add_argument("--gain", type=float, default=0.35,
                   help="motor master gain 0.0-1.0 (default 0.35, deliberately gentle)")
    p.add_argument("--magnitude", type=float, default=0.30,
                   help="effect magnitude 0.0-1.0 (default 0.30)")
    p.add_argument("--duration", type=float, default=2.0,
                   help="seconds to hold each effect (default 2.0)")
    p.add_argument("--motor", type=int, default=0, help="motor index (default 0)")
    p.add_argument("--list-only", action="store_true",
                   help="enumerate and report only; create no effects at all")
    p.add_argument("--no-script", action="store_true",
                   help="skip the scripted constant/spring test, go straight to the menu")
    p.add_argument("--oem", default=None, metavar="VID:PID",
                   help="call EnableOemDeviceSupport for VID:PID before enumerating")
    p.add_argument("--dll", default="GameInput.dll", metavar="NAME",
                   help="which GameInput runtime to load. Default is the inbox v0 "
                        "GameInput.dll. 'GameInputRedist.dll' is the newer v3 runtime, "
                        "which exports GameInputCreate as well -- worth a try when v0 "
                        "reports no force-feedback motors, but the v3 ABI is NOT the same "
                        "and the struct layout check may (correctly) refuse it.")
    return p.parse_args()


def main():
    args = parse_args()

    rule("GameInput force-feedback probe")
    print("  ABI source header : %s" % abi.SOURCE_HEADER)
    print("  Python            : %s" % sys.version.split()[0])

    # --- Step 1: create the GameInput singleton -----------------------------
    try:
        game_input, dll_path = abi.create_game_input(args.dll)
    except OSError as exc:
        print("\n  Could not load %s: %s" % (args.dll, exc))
        return 2
    except GameInputError as exc:
        print("\n  %s" % exc)
        return 2
    print("  GameInput runtime : %s  -- GameInputCreate OK" % dll_path)

    # --- Step 2: focus policy ----------------------------------------------
    # In this v0 ABI background input is allowed by DEFAULT and you opt out of it, so the
    # default policy is what we want for a console app that may lose focus. (v1+ inverted
    # this into an opt-in GameInputEnableBackgroundInput flag that does not exist here.)
    game_input.set_focus_policy(GameInputFocusPolicy.Default)
    print("  Focus policy      : default (v0 permits background input)")

    if args.oem:
        try:
            vid_s, pid_s = args.oem.split(":")
            vid, pid = int(vid_s, 0), int(pid_s, 0)
        except ValueError:
            print("  --oem expects VID:PID, e.g. 0x0F0D:0x015D")
            return 2
        rule("EnableOemDeviceSupport for 0x%04X:0x%04X" % (vid, pid))
        for collection in range(4):
            try:
                game_input.enable_oem_device_support(vid, pid, 0, collection)
                print("  interface 0 collection %d : OK" % collection)
            except GameInputError as exc:
                print("  interface 0 collection %d : %s" % (collection, exc))

    runner = None
    try:
        while True:
            # --- Step 3: enumerate -----------------------------------------
            rule("Connected devices")
            devices = enumerate_devices(game_input)
            if not devices:
                print("  No devices enumerated at all.")
                print("  If the wheel is plugged in, GameInput may not be surfacing it.")
                return 1

            infos = []
            for i, device in enumerate(devices):
                info = describe_device(device, i)
                infos.append(info)
                print()

            # --- Step 4: the layout gate -------------------------------------
            if not validate_layout(infos[0]):
                return 3

            # --- Step 5: pick the device under test --------------------------
            device, info = pick_device(devices, infos, args.vid, args.pid)
            if device is None:
                print("\n  No device matched the requested vid/pid.")
                return 1

            name = info.displayName.contents.text() if info.displayName else "(unnamed)"
            rule("Device under test: VID 0x%04X PID 0x%04X -- %s"
                 % (info.vendorId, info.productId, name))

            dump_racing_wheel_info(info)
            motors = dump_motors(device, info)
            dump_raw_reports(info)

            if not motors:
                rule("RESULT")
                print("  GameInput exposes no force-feedback motors on this device.")
                if info.productId == 0x015D:
                    print()
                    print("  This is PC/HID mode. Press the wheel's mode button to switch to")
                    print("  Xbox mode (PID 0x015C, GIP) and run this again -- that is the")
                    print("  path where GameInput force feedback is actually expected to work.")
                print()
                try:
                    again = input("  Re-enumerate now? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    return 0
                if again == "y":
                    for d in devices:
                        d.release()
                    continue
                return 0

            if args.list_only:
                print("\n  --list-only given; creating no effects.")
                return 0

            motor = motors[args.motor] if args.motor < len(motors) else motors[0]
            motor_index = args.motor if args.motor < len(motors) else 0

            # --- Step 6: scripted test ---------------------------------------
            runner = EffectRunner(device, motor_index, max(0.0, min(1.0, args.gain)))
            rule("FORCE FEEDBACK TEST -- keep a light grip on the wheel")
            runner.arm()

            if not args.no_script:
                if motor.isConstantEffectSupported:
                    runner.run(GameInputForceFeedbackEffectKind.Constant, "Constant force",
                               args.magnitude, args.duration)
                else:
                    print("\n  Constant effect not supported on this motor; skipping.")

                if motor.isSpringEffectSupported:
                    print()
                    print("  Next: a SPRING centred on the wheel's current centre. Turn the")
                    print("  wheel while it runs -- it should pull back toward centre.")
                    runner.run(GameInputForceFeedbackEffectKind.Spring, "Spring",
                               args.magnitude, args.duration)
                else:
                    print("\n  Spring effect not supported on this motor; skipping.")

            # --- Step 7: interactive ------------------------------------------
            action = interactive_menu(runner, motor, args.magnitude, args.duration, True)
            runner.panic_stop()
            runner = None

            if action == "reenumerate":
                for d in devices:
                    d.release()
                continue
            return 0

    except KeyboardInterrupt:
        print("\n\n  Ctrl+C -- stopping all force feedback.")
        return 130
    except GameInputError as exc:
        print("\n  %s" % exc)
        return 4
    finally:
        # Belt and braces: whatever happened, the motor must not stay energised.
        if runner is not None:
            runner.panic_stop()
        print("\n  All effects stopped, motor gain zeroed.")


if __name__ == "__main__":
    sys.exit(main())
