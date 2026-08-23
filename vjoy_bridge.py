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
import sys
import time

try:
    from winrt.runtime import init_apartment
except ImportError:
    init_apartment = None

import probe_log as log
from motor_sink import RateLimiter, clamp

try:
    import pyvjoy
    import pyvjoy._sdk as sdk
    from pyvjoy.constants import (
        HID_USAGE_RX, HID_USAGE_RY, HID_USAGE_X, HID_USAGE_Y, HID_USAGE_Z,
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

    Position reading through WGI is NOT foreground-gated -- verified 2026-08-23 over 18.6 s
    of continuous background sampling -- so this works with a game in front. Do not add
    foreground-grabbing here; it would steal focus from the game for no benefit.
    """

    def __init__(self, wheel):
        self.wheel = wheel
        self.reads = 0
        self.failures = 0
        self.last = None

    def read(self):
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
    try:
        raw, wheels = wait_for_devices(args.wait, pump)
        if not has_motor(raw, wheels):
            rule("RESULT")
            print("  No wheel found. Put it in Xbox mode (long-press PROFILE) and press one")
            print("  of its buttons while this runs.")
            return 1

        _motor, label, wheel, identity = report(raw, wheels)
        if wheel is None:
            rule("RESULT")
            print("  Found a device via %s but no RacingWheel to read position from." % label)
            return 1

        reader = WheelReader(wheel)
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

        rule("Feeding -- press Ctrl+C to stop")
        print("  Open joy.cpl and watch the vJoy device's axes track the real wheel.")
        print()

        limiter = RateLimiter(args.rate)
        next_print = 0.0
        while True:
            reading = reader.read()
            if reading is not None:
                written = feeder.feed(reading, caps)
                now = time.monotonic()
                if now >= next_print:
                    parts = " ".join("%s %+.2f" % (lbl, rawv) for lbl, rawv, _v in written)
                    # Write/failure counts belong on the live line, not only in the exit
                    # summary: a vJoy axis that is not enabled fails per call and silently,
                    # and a Ctrl+C or a hard kill never reaches the summary.
                    print("    %-58s %5.1f Hz  w=%d f=%d"
                          % (parts, limiter.achieved_hz, feeder.writes, feeder.failures),
                          flush=True)
                    log.event("bridge.tick", hz=round(limiter.achieved_hz, 1),
                              writes=feeder.writes, failures=feeder.failures,
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
        if feeder is not None:
            print("  vJoy writes %d, failures %d" % (feeder.writes, feeder.failures))
            feeder.close()
        pump.stop()
        if not args.no_log:
            log.stop()


if __name__ == "__main__":
    sys.exit(main())
