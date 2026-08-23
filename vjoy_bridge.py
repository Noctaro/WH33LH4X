"""
vjoy_bridge.py -- present the HORI wheel to DirectInput games through vJoy.

THE PROBLEM THIS SOLVES
-----------------------
Both of the wheel's USB modes are dead ends for sims:

  * PC mode   -- DirectInput sees it and it steers, but the joystick collection is
                 input-only (no HID usage page 0x0F), so there is no force feedback for any
                 driver to expose. Not fixable.
  * Xbox mode -- force feedback works, but only through Windows.Gaming.Input, and it is
                 invisible to DirectInput entirely.

RaceRoom, Automobilista 2 and Dirt 4 all read wheels through DirectInput. So this wheel has
no force feedback in any PC sim. The bridge closes that gap: vJoy presents a virtual wheel
that games can see and send effects to, and we render those effects on the real motor.

**Keep the wheel in XBOX mode.** Its invisibility to DirectInput is a feature here -- the
game can only see the vJoy device, so there is never a real device to hide.

WHAT WORKS TODAY (phase B1): the INPUT path.

    real wheel --(WGI reading)--> this bridge --(vJoy feeder API)--> game sees a wheel

Force feedback is NOT wired up yet, and cannot simply be switched on: WGI force output is
foreground-gated, so a background process like this one cannot drive the motor while a game
is in front. That is tracked separately; see motor_sink.py for the full explanation and the
interface the fix will land behind. Position READING is not gated, which is why the input
path works from here without any trickery.

Usage:
    python vjoy_bridge.py                 # feed vJoy device 1 at 100 Hz
    python vjoy_bridge.py --device 2      # a different vJoy device
    python vjoy_bridge.py --rate 250      # faster feed
    python vjoy_bridge.py --dry-run       # read and report, write nothing to vJoy
"""

import argparse
import asyncio
import queue
import sys
import time

try:
    from winrt.runtime import init_apartment
except ImportError:
    init_apartment = None

import ffb_render as render
import probe_log as log
from motor_sink import IpcMotorSink, RateLimiter, WgiMotorSink, clamp

try:
    import pyvjoy
    import pyvjoy._sdk as sdk
    from pyvjoy.constants import (
        CTRL_DEVCONT, CTRL_DEVPAUSE, CTRL_DEVRST, CTRL_DISACT, CTRL_ENACT, CTRL_STOPALL,
        EFF_STOP,
        HID_USAGE_RX, HID_USAGE_RY, HID_USAGE_X, HID_USAGE_Y, HID_USAGE_Z,
        PT_BLKFRREP, PT_CONDREP, PT_CONSTREP, PT_CTRLREP, PT_EFFREP, PT_EFOPREP,
        PT_ENVREP, PT_GAINREP, PT_NEWEFREP, PT_PRIDREP, PT_RAMPREP,
        VJD_STAT_BUSY, VJD_STAT_FREE, VJD_STAT_MISS, VJD_STAT_OWN,
    )
except ImportError:
    sys.exit("pyvjoyffb is not installed. Run:\n"
             "    .\\.venv\\Scripts\\python.exe -m pip install pyvjoyffb")

from wgi_probe import PumpThread, has_motor, report, rule, wait_for_devices

# vJoy axis range. The SDK takes 0x1..0x8000; 0x4000 is centre for a bidirectional axis.
AXIS_MIN = 0x0001
AXIS_MAX = 0x8000
AXIS_MID = 0x4000

STATUS_NAMES = {VJD_STAT_OWN: "owned by us", VJD_STAT_FREE: "free",
                VJD_STAT_BUSY: "busy (another app owns it)", VJD_STAT_MISS: "missing"}


def bidirectional(value):
    """-1.0..+1.0 -> full vJoy axis range, centred."""
    return int(round(AXIS_MID + clamp(value) * (AXIS_MAX - AXIS_MID)))


def unidirectional(value):
    """0.0..1.0 -> full vJoy axis range. Pedals rest at zero, not at centre."""
    return int(round(AXIS_MIN + max(0.0, min(1.0, value)) * (AXIS_MAX - AXIS_MIN)))


# Which real-wheel control drives which vJoy axis.
#
# Mapped onto the six axes DirectInput ACTUALLY sees on vJoy -- X Y Z RX RY RZ. The vJoy SDK
# claims nine (it also advertises SL0/SL1/WHL) but DirectInput enumerates only six, measured
# in B0. Anything mapped beyond those six would be written successfully and never arrive.
#
# Pedals get their own axes rather than a combined one. Sims let you rebind axes but they
# cannot un-combine two pedals that were summed before they arrived.
AXIS_MAP = [
    # (reading attribute, vJoy usage, label, converter)
    ("wheel", HID_USAGE_X, "steering", bidirectional),
    ("throttle", HID_USAGE_Y, "throttle", unidirectional),
    ("brake", HID_USAGE_Z, "brake", unidirectional),
    ("clutch", HID_USAGE_RX, "clutch", unidirectional),
    ("handbrake", HID_USAGE_RY, "handbrake", unidirectional),
]


class WheelReader(object):
    """
    Reads the real wheel, and reports honestly when it cannot.

    POSITION READING IS FOREGROUND-GATED, exactly like force output. An earlier version of
    this docstring said the opposite, on the strength of a background sample that could not
    tell tracking from values gathered across focus transitions -- the same data showed 65%
    of background samples at exactly 0.000 against 0% while foregrounded.

    That matters more than it looks: if this returns zeros while a game is in front, vJoy's
    axes never move, the game cannot bind steering, and a game that has not bound steering
    never sends force feedback either. The whole bridge dies at the input end.

    So when a `source` is given -- the IPC sink, fed by the shim inside the game -- readings
    come from there instead, and WGI is only the fallback for running without a game.
    """

    def __init__(self, wheel, source=None):
        self.wheel = wheel
        self.source = source
        self.reads = 0
        self.failures = 0
        self.last = None
        self.from_shim = 0

    def read(self):
        if self.source is not None:
            reading = self.source.read()
            if reading is not None:
                self.reads += 1
                self.from_shim += 1
                self.last = reading
                return reading
            # Fall through to WGI rather than returning None: before the game has loaded the
            # shim there is nothing publishing, and our own window may still be in front.
        if self.wheel is None:
            self.failures += 1
            return None
        try:
            reading = self.wheel.get_current_reading()
        except Exception:
            self.failures += 1
            return None
        self.reads += 1
        self.last = reading
        return reading

    def capabilities(self):
        """What this wheel actually has, so unmapped axes can be skipped and reported."""
        caps = {}
        if self.wheel is None:
            # Readings come from the shim; we never saw the device ourselves. Unknown rather
            # than False, so nothing gets skipped on the strength of a guess.
            return {"clutch": None, "handbrake": None, "pattern_shifter": None,
                    "max_wheel_angle": None}
        for attr, probe in (("clutch", "has_clutch"),
                            ("handbrake", "has_handbrake"),
                            ("pattern_shifter", "has_pattern_shifter")):
            try:
                caps[attr] = bool(getattr(self.wheel, probe))
            except Exception:
                caps[attr] = None
        try:
            caps["max_wheel_angle"] = float(self.wheel.max_wheel_angle)
        except Exception:
            caps["max_wheel_angle"] = None
        return caps


class VJoyFeeder(object):
    """Writes axis and button state into the vJoy device."""

    def __init__(self, rid, dry_run=False):
        self.rid = rid
        self.dry_run = dry_run
        self.device = None
        self.writes = 0
        self.failures = 0
        self.button_count = 0
        self.axes = []

    def open(self):
        status = sdk.GetVJDStatus(self.rid)
        if status in (VJD_STAT_MISS, VJD_STAT_BUSY):
            raise RuntimeError("vJoy device %d is %s -- configure or free it in vJoyConf."
                               % (self.rid, STATUS_NAMES.get(status, status)))

        # Which of our mapped axes this device actually has configured. Writing to an axis
        # that is not enabled fails per call; better to know once, up front, and say so.
        self.axes = [(attr, usage, label, conv) for attr, usage, label, conv in AXIS_MAP
                     if sdk._vj.GetVJDAxisExist(self.rid, usage)]
        self.button_count = int(sdk._vj.GetVJDButtonNumber(self.rid))

        if not self.dry_run:
            self.device = pyvjoy.VJoyDevice(self.rid)
        return self

    def close(self):
        if self.device is not None:
            try:
                sdk.ResetVJD(self.rid)
            except Exception:
                pass
            self.device = None

    def feed(self, reading, caps):
        """Push one reading into vJoy. Returns the values written, for display."""
        written = []
        for attr, usage, label, conv in self.axes:
            # Skip controls the wheel does not have: leaving them at rest is right, whereas
            # writing a stale or zero value would look to the game like a pedal held down.
            if caps.get(attr) is False:
                continue
            try:
                raw = getattr(reading, attr)
            except Exception:
                continue
            value = conv(raw)
            written.append((label, raw, value))
            if self.device is not None:
                try:
                    self.device.set_axis(usage, value)
                    self.writes += 1
                except Exception:
                    self.failures += 1

        if self.device is not None and self.button_count:
            try:
                self._feed_buttons(int(reading.buttons))
            except Exception:
                self.failures += 1
        return written

    def _feed_buttons(self, mask):
        """
        RacingWheelButtons is a flags enum; map set bits onto vJoy buttons 1..N.

        The mapping is positional, not semantic: bit 0 becomes button 1 and so on. Games let
        you rebind buttons, so a stable arbitrary order is worth more than a guess at which
        physical button "should" be A.
        """
        for index in range(self.button_count):
            self.device.set_button(index + 1, 1 if mask & (1 << index) else 0)


# vJoy effect-type byte -> the kind names ffb_render uses.
EFFECT_KINDS = {
    1: "constant", 2: "ramp", 3: "square", 4: "sine", 5: "triangle",
    6: "sawtoothup", 7: "sawtoothdown",
    8: "spring", 9: "damper", 10: "inertia", 11: "friction",
}


class EffectDecoder(object):
    """
    Turns vJoy's force-feedback packets into `ffb_render` effects.

    THREADING. vJoy delivers packets on ITS OWN THREAD. Nothing here may touch WinRT, and
    the render loop must not have effect state mutated underneath it mid-computation, so
    packets are pushed onto a queue and applied by the bridge thread between ticks. That is
    the one rule; everything else in this class is arithmetic.

    UNITS. The wire format is NOT what DirectInput was handed, measured in B0 by
    `vjoy_ffb_spike.py`, which re-measures it on every run:

      * gain arrives as a 0-255 BYTE, not 0..10000
      * duration arrives in MILLISECONDS, not microseconds, and 0 means infinite
      * magnitudes and condition coefficients arrive unchanged, +-10000 full scale
      * direction becomes polar; the SIGN of a force travels in the magnitude, and the
        direction vector is normalised to 8191 (90 degrees) either way

    Getting gain or duration wrong is a silent factor-of-40 error, not a crash.
    """

    def __init__(self, mixer, clock=time.monotonic):
        self.mixer = mixer
        self.clock = clock
        self.queue = queue.Queue(maxsize=4096)
        self.received = 0
        self.dropped = 0
        self.unknown = 0

    # -- called on vJoy's thread -------------------------------------------

    def on_packet(self, data, reptype):
        """vJoy callback. Does the minimum possible and gets off this thread."""
        try:
            self.queue.put_nowait((self.clock(), reptype, _snapshot(data)))
            self.received += 1
        except queue.Full:
            # Dropping is better than blocking vJoy's thread. Counted, because a bridge
            # that silently sheds a game's effect updates would feel like random FFB.
            self.dropped += 1

    # -- called on the bridge thread ---------------------------------------

    def drain(self):
        """Apply every queued packet. Returns how many were applied."""
        applied = 0
        while True:
            try:
                stamp, reptype, fields = self.queue.get_nowait()
            except queue.Empty:
                return applied
            try:
                self._apply(stamp, reptype, fields)
            except Exception as exc:                              # noqa: BLE001
                log.event("decode.error", reptype=reptype, error=repr(exc))
            applied += 1

    def _apply(self, stamp, reptype, f):
        mixer = self.mixer

        if reptype == PT_CTRLREP:
            self._control(f)
            return
        if reptype == PT_GAINREP:
            # Device gain is the game's master volume for force feedback, 0-255.
            mixer.device_gain = _byte_fraction(f)
            log.event("decode.device_gain", gain=round(mixer.device_gain, 3))
            return
        if reptype == PT_NEWEFREP:
            return                      # allocation only; the block index arrives later
        if reptype == PT_BLKFRREP:
            mixer.free(int(f))
            return

        block = f.get("EffectBlockIndex", 0) if isinstance(f, dict) else 0
        if not block:
            self.unknown += 1
            return
        effect = mixer.get(block)

        if reptype == PT_EFFREP:
            kind = EFFECT_KINDS.get(f["EffectType"])
            if kind is None:
                self.unknown += 1
                return
            effect.kind = kind
            effect.gain = f["Gain"] / 255.0                  # BYTE, not 0..10000
            effect.duration = f["Duration"] / 1000.0         # ms; 0 stays 0 = infinite
            effect.start_delay = f.get("StartDelay", 0) / 1000.0
            effect.direction = render.direction_x(f["DirX"])
        elif reptype == PT_CONSTREP:
            effect.magnitude = f["Magnitude"] / render.DI_FULL_SCALE
        elif reptype == PT_RAMPREP:
            effect.ramp_start = f["Start"] / render.DI_FULL_SCALE
            effect.ramp_end = f["End"] / render.DI_FULL_SCALE
        elif reptype == PT_PRIDREP:
            effect.periodic_magnitude = f["Magnitude"] / render.DI_FULL_SCALE
            effect.periodic_offset = f["Offset"] / render.DI_FULL_SCALE
            # Phase is assumed to be DirectInput's hundredths of a degree. Only phase 0 has
            # been observed, so this is the one conversion here that is NOT measured.
            effect.periodic_phase_deg = f["Phase"] / 100.0
            effect.periodic_period = f["Period"] / 1000.0
        elif reptype == PT_CONDREP:
            # POSITIVE COEFFICIENT RESISTS. That is the convention WGI was measured to use
            # (the firmware spring centres on +1/+1), and ffb_render.condition_force negates
            # the raw formula to match it. `dinput_probe.spring_params` asserts the opposite,
            # but that file never reached a working force-feedback device, so its comment is
            # an untested assumption rather than evidence. If a game's spring drives the
            # wheel outward instead of centring it, this is the line to flip -- and the fix
            # belongs here, at the decode boundary, not in the control law.
            axis = 1 if f["isY"] else 0
            effect.conditions[axis] = render.ConditionParams.from_di(
                offset=f["CenterPointOffset"],
                pos_coeff=f["PosCoeff"], neg_coeff=f["NegCoeff"],
                pos_saturation=f["PosSatur"], neg_saturation=f["NegSatur"],
                deadband=f["DeadBand"])
        elif reptype == PT_ENVREP:
            effect.attack_level = f["AttackLevel"] / render.DI_FULL_SCALE
            effect.attack_time = f["AttackTime"] / 1000.0
            effect.fade_level = f["FadeLevel"] / render.DI_FULL_SCALE
            effect.fade_time = f["FadeTime"] / 1000.0
        elif reptype == PT_EFOPREP:
            op = f["EffectOp"]
            if op == EFF_STOP:
                effect.stop()
            else:
                effect.start(stamp, f.get("LoopCount", 1))
            log.event("decode.effect_op", block=block, op=op, kind=effect.kind or "?")
        else:
            self.unknown += 1

    def _control(self, value):
        mixer = self.mixer
        if value == CTRL_DEVRST:
            mixer.reset()
        elif value == CTRL_STOPALL:
            mixer.stop_all()
        elif value == CTRL_DEVPAUSE:
            mixer.paused = True
        elif value == CTRL_DEVCONT:
            mixer.paused = False
        elif value == CTRL_ENACT:
            mixer.actuators_enabled = True
        elif value == CTRL_DISACT:
            mixer.actuators_enabled = False
        log.event("decode.control", control=value)


def _snapshot(data):
    """
    Copy a packet out of vJoy's structure before returning from the callback.

    The decoded structs belong to the callback's stack frame; queuing one and reading it a
    tick later would be reading whatever vJoy put there since.
    """
    if isinstance(data, sdk.PacketStruct):
        return data.to_dict()
    return data


def _byte_fraction(value):
    try:
        return max(0.0, min(1.0, int(value) / 255.0))
    except (TypeError, ValueError):
        return 1.0


SWEEP_CHOICES = [label for _a, _u, label, _c in AXIS_MAP] + ["buttons"]


def sweep_mode(feeder, target, rate, seconds):
    """
    Wiggle one vJoy control on its own, with no wheel involved. For BINDING.

    Games bind an axis by watching for movement, and they only watch while THEY have focus.
    But reading the real wheel needs OUR process to have focus. So binding a real axis is a
    catch-22: focus the game and the axis is frozen, focus us and the game is not looking.

    Synthetic motion breaks it. The game cannot tell where the movement came from, so with
    this running you can leave the game focused and bind normally. Nothing here touches the
    wheel -- no detection, no WGI, no reading -- which is exactly why focus stops mattering.

    Bind first with this, then play with the real feeder.
    """
    print("  Sweeping %s on vJoy device %d at %.0f Hz." % (target, feeder.rid, rate))
    print("  Leave THE GAME focused and bind %s now."
          % ("a button" if target == "buttons" else "this axis"))
    print("  Ctrl+C when the binding is accepted.")
    print()

    entry = next((e for e in AXIS_MAP if e[2] == target), None)
    limiter = RateLimiter(rate)
    start = time.monotonic()
    end = start + seconds if seconds > 0 else None
    next_print = 0.0
    button = 1

    while end is None or time.monotonic() < end:
        elapsed = time.monotonic() - start
        if entry is not None:
            _attr, usage, label, conv = entry
            # A slow triangle: unambiguous direction, and it dwells at the extremes long
            # enough for a binding UI that samples occasionally to catch full deflection.
            phase = (elapsed * 0.5) % 2.0
            tri = phase if phase < 1.0 else 2.0 - phase          # 0..1..0
            raw = tri * 2.0 - 1.0 if conv is bidirectional else tri
            if feeder.device is not None:
                try:
                    feeder.device.set_axis(usage, conv(raw))
                    feeder.writes += 1
                except Exception:
                    feeder.failures += 1
            shown = raw
        else:
            # Buttons: one at a time, half a second each, so it is obvious which is which.
            index = int(elapsed * 2) % max(1, feeder.button_count)
            if feeder.device is not None:
                try:
                    feeder.device.set_button(button, 0)
                    button = index + 1
                    feeder.device.set_button(button, 1)
                    feeder.writes += 1
                except Exception:
                    feeder.failures += 1
            shown = float(index + 1)

        now = time.monotonic()
        if now >= next_print:
            print("    %s %+.2f   w=%d f=%d" % (target, shown, feeder.writes,
                                                feeder.failures), flush=True)
            next_print = now + 0.5
        limiter.wait()


def parse_args():
    p = argparse.ArgumentParser(
        description="Feed the real wheel's position into a vJoy device that games can see")
    p.add_argument("--device", type=int, default=1, help="vJoy device id (default 1)")
    p.add_argument("--sweep", choices=SWEEP_CHOICES, default=None,
                   help="BINDING HELPER: move one vJoy control synthetically, ignoring the "
                        "real wheel entirely, so a game can bind it while the GAME has "
                        "focus. Works around the catch-22 where the game only watches for "
                        "movement while focused but we can only read the wheel while WE "
                        "are. Bind with this, then play with the normal feeder.")
    p.add_argument("--sweep-seconds", type=float, default=0.0,
                   help="stop sweeping after N seconds (default: until Ctrl+C)")
    p.add_argument("--rate", type=float, default=100.0,
                   help="feed rate in Hz (default 100)")
    p.add_argument("--wait", type=float, default=30.0, help="detection timeout")
    p.add_argument("--dry-run", action="store_true",
                   help="read the wheel and report, but write nothing to vJoy")
    p.add_argument("--no-ffb", action="store_true",
                   help="input path only; never take the motor")
    p.add_argument("--sink", choices=("ipc", "wgi"), default="ipc",
                   help="where force goes. 'ipc' (default) publishes to the shim inside the "
                        "game, which is the only thing that works with a game running. 'wgi' "
                        "drives the motor from this process and only produces torque while "
                        "OUR window is in front -- diagnostics only.")
    p.add_argument("--no-grab", action="store_true",
                   help="never take the foreground. Force output will be silent unless you "
                        "click the probe window yourself, but nothing steals your keyboard.")
    p.add_argument("--run-seconds", type=float, default=0.0,
                   help="stop automatically after N seconds (default: run until Ctrl+C). "
                        "Useful because an effect playing takes the foreground, which is "
                        "also where your Ctrl+C would have gone.")
    p.add_argument("--gain", type=float, default=0.5,
                   help="motor master gain 0.0-1.0 (default 0.5). Latched when the effect "
                        "is loaded, so changing it needs a reload.")
    p.add_argument("--max-force", type=float, default=0.6,
                   help="hard cap on commanded force 0.0-1.0 (default 0.6). Multiplies with "
                        "--gain, so the default is about 30%% of what the wheel can do.")
    p.add_argument("--no-log", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    log_path = None if args.no_log else log.start(prefix="vjoy_bridge")

    if init_apartment is not None:
        try:
            init_apartment()
        except TypeError:
            init_apartment(0)

    rule("vJoy bridge -- input path (phase B1)")
    print("  Feeds the real wheel's position into vJoy so DirectInput games can see it.")
    print("  Force feedback is NOT active yet -- see motor_sink.py for why.")
    print("  The wheel must be in XBOX mode.")
    if log_path:
        print("  Log: %s" % log_path)

    # Sweep mode never touches the wheel, so it needs no message pump, no WinRT apartment
    # and no detection -- and therefore does not care who has the foreground. That is the
    # entire point of it.
    if args.sweep:
        feeder = None
        try:
            feeder = VJoyFeeder(args.device).open()
            rule("Binding helper -- synthetic %s" % args.sweep)
            sweep_mode(feeder, args.sweep, args.rate, args.sweep_seconds)
            return 0
        except KeyboardInterrupt:
            print("\n  stopped.")
            return 0
        except RuntimeError as exc:
            print("\n  %s" % exc)
            return 1
        finally:
            if feeder is not None:
                feeder.close()
            if not args.no_log:
                log.stop()

    pump = PumpThread()
    pump.start()
    pump.ready.wait(timeout=5)

    feeder = None
    sink = None
    decoder = None
    loop = None
    try:
        use_ipc = args.sink == "ipc" and not args.no_ffb and not args.dry_run

        raw, wheels = wait_for_devices(args.wait, pump)
        motor = label = wheel = None
        identity = {}
        if has_motor(raw, wheels):
            motor, label, wheel, identity = report(raw, wheels)
        elif not use_ipc:
            rule("RESULT")
            print("  No wheel found. Put it in Xbox mode (long-press PROFILE) and press one")
            print("  of its buttons while this runs.")
            return 1

        # Create the IPC sink BEFORE the reader, because it is also the reader's source: the
        # shim publishes wheel position through the same section it takes force from.
        if use_ipc:
            sink = IpcMotorSink(max_force=args.max_force, gain=args.gain).open()

        if wheel is None and sink is None:
            rule("RESULT")
            print("  Found a device via %s but no RacingWheel to read position from." % label)
            return 1
        if wheel is None:
            # NOT a failure with the IPC sink. Enumerating here needs OUR window in front, so
            # a bridge started after the game never sees the wheel -- and does not need to,
            # because the shim inside the game supplies both readings and force. Requiring it
            # was what forced "start the bridge first", which is a bad thing to require of
            # anyone launching a game from Steam.
            print("  No wheel enumerated in this process -- readings will come from the shim.")
            print("  (Normal when the game is already running; it owns the foreground.)")

        reader = WheelReader(wheel, source=sink)
        caps = reader.capabilities()

        rule("Real wheel")
        print("  %s" % (identity.get("name") or "(unnamed)"))
        print("  max wheel angle : %s"
              % ("%.0f degrees" % caps["max_wheel_angle"] if caps["max_wheel_angle"]
                 else "unknown"))
        print("  clutch / handbrake / shifter : %s / %s / %s"
              % (caps["clutch"], caps["handbrake"], caps["pattern_shifter"]))

        feeder = VJoyFeeder(args.device, dry_run=args.dry_run).open()

        rule("vJoy device %d" % args.device)
        print("  axes mapped : %s"
              % ", ".join("%s->%s" % (lbl, nm) for _a, _u, lbl, _c in feeder.axes
                          for nm in [{0x30: "X", 0x31: "Y", 0x32: "Z",
                                      0x33: "RX", 0x34: "RY", 0x35: "RZ"}[_u]]))
        print("  buttons     : %d" % feeder.button_count)
        skipped = [lbl for attr, _u, lbl, _c in AXIS_MAP if caps.get(attr) is False]
        if skipped:
            print("  not present on this wheel, left at rest: %s" % ", ".join(skipped))
        if args.dry_run:
            print("  DRY RUN -- nothing is being written to vJoy.")

        # --- force feedback: render what the game sends onto the real motor ---
        if not args.no_ffb and not args.dry_run and (motor is not None or sink is not None):
            loop = None
            if sink is None:
                # 'wgi' sink: drive the motor from this process. Only produces torque while
                # OUR window is in front, so this is for diagnostics, not for playing.
                loop = asyncio.new_event_loop()
                sink = WgiMotorSink(motor, loop, max_force=args.max_force, gain=args.gain)
                sink.open()
            decoder = EffectDecoder(render.EffectMixer())
            device = feeder.device if feeder.device is not None else pyvjoy.VJoyDevice(
                args.device)
            device.ffb_register_callback(decoder.on_packet)
            sdk._vj.FfbStart(args.device)
            rule("Force feedback ACTIVE")
            print("  %s" % sink.describe())
            print("  Effects a DirectInput client sends to vJoy are rendered here and")
            print("  played on the real wheel. Everything is computed in software and")
            print("  summed into one command -- see ffb_render.EffectMixer.")
            print()
            if args.sink == "ipc":
                print("  Force goes to the shim inside the game, which calls WGI from there.")
                print("  This process never takes the foreground -- the game keeps it, which")
                print("  is exactly what makes the motor reachable. Copy")
                print("  shim\\build\\dinput8.dll next to the game exe if you have not yet;")
                print("  until the shim reports 'driving' above, nothing reaches the wheel.")
                print()
            else:
                print("  !! WGI force output is foreground-gated, so while an effect is")
                print("     PLAYING this process takes the foreground and your keystrokes")
                print("     -- including Ctrl+C -- go to the probe window, not here. It")
                print("     releases focus as soon as nothing is playing. Use --run-seconds")
                print("     for a self-terminating run, or --no-grab to keep your keyboard.")
                print("     That gate is why the shim exists; use --sink ipc with a game.")
        elif args.no_ffb:
            print("  Force feedback disabled (--no-ffb).")
        elif args.dry_run:
            print("  Force feedback off in dry-run mode.")
        elif motor is None:
            print("  No force-feedback motor found; input only.")

        rule("Running -- press Ctrl+C to stop")
        print("  Open joy.cpl and watch the vJoy device's axes track the real wheel.")
        print()

        limiter = RateLimiter(args.rate)
        state = render.WheelState()
        next_print = 0.0
        last_foreground = 0.0
        stop_at = time.monotonic() + args.run_seconds if args.run_seconds > 0 else None
        while stop_at is None or time.monotonic() < stop_at:
            now = time.monotonic()
            reading = reader.read()
            if reading is not None:
                written = feeder.feed(reading, caps)
                state.update(reading.wheel, now)

                if sink is not None:
                    # Apply the game's packets BEFORE computing, so a force command always
                    # reflects the most recent thing the game asked for rather than lagging
                    # it by a tick.
                    decoder.drain()
                    force = decoder.mixer.force(now, state)
                    sink.set_force(force)

                    # Take the foreground ONLY while an effect is actually playing, and ONLY
                    # when we are the ones driving the motor.
                    #
                    # WGI gates force output on foreground, so with the 'wgi' sink the grab is
                    # necessary -- but grabbing unconditionally makes the tool unusable: it
                    # steals every keystroke, including the Ctrl+C meant to stop it. Gating on
                    # running effects means the console keeps focus when there is no force to
                    # lose.
                    #
                    # With the 'ipc' sink the grab is not merely unnecessary, it is ACTIVELY
                    # WRONG: the shim inside the game is what talks to the motor, so stealing
                    # focus would take it away from the game that needs it -- breaking the
                    # very thing this sink exists to make work.
                    if (args.sink != "ipc" and not args.no_grab
                            and decoder.mixer.running_effects()
                            and now - last_foreground > 0.1):
                        pump.ensure_foreground()
                        last_foreground = now

                if now >= next_print:
                    parts = " ".join("%s %+.2f" % (lbl, rawv) for lbl, rawv, _v in written)
                    extra = ""
                    if sink is not None:
                        running = decoder.mixer.running_effects()
                        extra = ("  force %+.2f  fx=%d  pkt=%d"
                                 % (sink._last or 0.0, len(running), decoder.received))
                        if decoder.dropped:
                            extra += " DROPPED=%d" % decoder.dropped
                    # Write/failure counts belong on the live line, not only in the exit
                    # summary: a vJoy axis that is not enabled fails per call and silently,
                    # and a Ctrl+C or a hard kill never reaches the summary.
                    print("    %-52s %5.1f Hz w=%d f=%d%s"
                          % (parts, limiter.achieved_hz, feeder.writes, feeder.failures,
                             extra), flush=True)
                    log.event("bridge.tick", hz=round(limiter.achieved_hz, 1),
                              writes=feeder.writes, failures=feeder.failures,
                              force=(sink._last or 0.0) if sink else 0.0,
                              effects=len(decoder.mixer.running_effects()) if decoder else 0,
                              **{lbl: rawv for lbl, rawv, _v in written})
                    next_print = now + 0.5
            limiter.wait()

    except KeyboardInterrupt:
        print("\n  stopped.")
        return 0
    except RuntimeError as exc:
        print("\n  %s" % exc)
        return 1
    finally:
        # Order matters: silence the motor before anything else is torn down, so an error
        # on the way out cannot leave the wheel holding a force.
        if sink is not None:
            try:
                sink.set_force(0.0)
            except Exception:
                pass
            sink.close()
            print("  motor released -- %d writes, %d failures" % (sink.writes, sink.failures))
        if decoder is not None:
            print("  ffb packets %d received, %d dropped, %d unrecognised"
                  % (decoder.received, decoder.dropped, decoder.unknown))
        if feeder is not None:
            print("  vJoy writes %d, failures %d" % (feeder.writes, feeder.failures))
            feeder.close()
        if loop is not None:
            loop.close()
        pump.stop()
        if not args.no_log:
            log.stop()


if __name__ == "__main__":
    sys.exit(main())
