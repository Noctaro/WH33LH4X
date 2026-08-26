"""
wgi_probe.py -- Drive this wheel's force-feedback motor through Windows.Gaming.Input.

THIS IS THE ONE THAT WORKS.

Companion to probe.py (GameInput), dinput_probe.py (DirectInput 8) and hid_probe.py (raw
HID descriptors). Those three found nothing, for a good reason: DirectInput and GameInput
both route PC force feedback through the USB PID HID class, and hid_probe.py proved this
wheel publishes no PID collection at all. Windows.Gaming.Input is a different stack -- its
ForceFeedbackMotor comes from the GIP driver for Xbox-licensed devices -- and it reaches
the motor that the other two cannot see.

Confirmed on a Hori Force Feedback Racing Wheel DLX (VID 0x0F0D, PID 0x015C, Xbox mode):

    force_feedback_motors : 1
    is_enabled            : True
    supported_axes        : X
    RacingWheel.wheel_motor present, max_wheel_angle 900, clutch + handbrake

TWO THINGS THAT MAKE THIS WORK
------------------------------
1. A MESSAGE PUMP. Windows.Gaming.Input delivers device-arrival notifications through the
   Win32 message loop. A console process has none -- the console window belongs to
   conhost.exe, not to Python -- so the device lists stay permanently empty and it looks
   like the wheel is not there. This tool creates its own window on a background thread
   and pumps it. That single detail is the difference between "no devices" and success.

2. XBOX MODE. The wheel must be in Xbox mode (PID 0x015C). In PC mode it is a plain HID
   device with no force-feedback interface anywhere.

ABOUT THAT "LOOSE" FEELING
--------------------------
At idle this wheel applies its own firmware auto-centering -- the strong spring you feel
normally. The moment an effect takes the motor, that firmware behaviour is SUSPENDED and
your effect owns the wheel. So a gentle effect feels *looser* than the resting wheel, not
stronger. That is what taking control feels like. Use 'r' in the menu to release the motor
and let the firmware spring come back, so you can feel the difference on demand.

INTENSITY
---------
Two limiters multiply together:

    master gain (motor level, 0.0-1.0)  x  magnitude (effect level, 0.0-1.0)

Defaults are 0.35 x 0.30, i.e. about 10% of what the wheel can produce -- deliberately
gentle for a first run. Both are adjustable live from the menu ('g' and 'm').

Usage:
    python wgi_probe.py                 # detect, report, then interactive menu
    python wgi_probe.py --list-only     # detect and report only, play nothing
    python wgi_probe.py --gain 0.7 --magnitude 0.6
    python wgi_probe.py --duration 0    # effects hold until you press Enter
"""

import argparse
import asyncio
import ctypes
import sys
import threading
import time
from ctypes import POINTER, Structure, byref, c_long, c_size_t, c_uint32, c_void_p
from datetime import timedelta

try:
    from winrt.runtime import init_apartment
except ImportError:
    init_apartment = None

import winrt.windows.gaming.input as gi
import winrt.windows.gaming.input.forcefeedback as ff
from winrt.windows.foundation.numerics import Vector3

import probe_log as log
import wheel_profile as wp

HORI_VENDOR_ID = 0x0F0D

# vJoy's own virtual device -- root\VID_1234&PID_BEAD, i.e. VENDOR_N_ID / PRODUCT_N_ID from
# vJoy's public.h. It shows up under whatever product name it was configured with ("Forza
# EmuWheel" on this machine), reports 0 force-feedback motors, and being a root-enumerated
# software device it enumerates FASTER than the real wheel does over USB. Recognised here so
# it can be named in the report and never mistaken for the hardware.
VJOY_VENDOR_ID = 0x1234
VJOY_PRODUCT_ID = 0xBEAD

# How long to keep listening after the FIRST device shows up.
#
# This exists because of a measured race: the vJoy virtual device enumerated at t=0.047s and
# the real HORI wheel at t=0.094s, but detection returned at t=0.078s -- so the tool reported
# "no force-feedback motor" while the wheel was plugged in the whole time. Returning on the
# first non-empty poll is simply wrong when more than one device exists.
DEVICE_SETTLE_SECONDS = 2.0


def is_vjoy(vendor_id, product_id):
    return vendor_id == VJOY_VENDOR_ID and product_id == VJOY_PRODUCT_ID


def _is_vjoy_controller(controller):
    """is_vjoy() for a live RawGameController. Fails closed: a device we cannot identify
    is treated as real, so a read error can never make us discard the actual wheel."""
    try:
        return is_vjoy(controller.hardware_vendor_id, controller.hardware_product_id)
    except Exception:
        return False


AXIS_NAMES = [("X", ff.ForceFeedbackEffectAxes.X),
              ("Y", ff.ForceFeedbackEffectAxes.Y),
              ("Z", ff.ForceFeedbackEffectAxes.Z)]

LOAD_RESULT_NAMES = {
    ff.ForceFeedbackLoadEffectResult.SUCCEEDED: "Succeeded",
    ff.ForceFeedbackLoadEffectResult.EFFECT_STORAGE_FULL: "EffectStorageFull",
    ff.ForceFeedbackLoadEffectResult.EFFECT_NOT_SUPPORTED: "EffectNotSupported",
}

STATE_NAMES = {
    ff.ForceFeedbackEffectState.STOPPED: "Stopped",
    ff.ForceFeedbackEffectState.RUNNING: "Running",
    ff.ForceFeedbackEffectState.PAUSED: "Paused",
    ff.ForceFeedbackEffectState.FAULTED: "FAULTED",
}


def rule(title=""):
    if title:
        print("\n" + "=" * 78)
        print(title)
        print("=" * 78)
    else:
        print("-" * 78)


def decode_axes(axes):
    hits = [n for n, bit in AXIS_NAMES if int(axes) & int(bit)]
    return "|".join(hits) if hits else "none (0x%X)" % int(axes)


# ---------------------------------------------------------------------------
# Win32 message pump, on its own thread
# ---------------------------------------------------------------------------
#
# The window must be created on the same thread that pumps it, and that pumping must keep
# happening while the main thread sits in input(). Hence a dedicated daemon thread.

user32 = ctypes.WinDLL("user32", use_last_error=True)

WS_OVERLAPPEDWINDOW = 0x00CF0000
SW_SHOW = 5
SW_HIDE = 0
CW_USEDEFAULT = -2147483648
PM_REMOVE = 0x0001


class POINT(Structure):
    _fields_ = [("x", c_long), ("y", c_long)]


class MSG(Structure):
    _fields_ = [("hwnd", c_void_p), ("message", c_uint32), ("wParam", c_size_t),
                ("lParam", ctypes.c_ssize_t), ("time", c_uint32), ("pt", POINT)]


user32.CreateWindowExW.restype = c_void_p
user32.CreateWindowExW.argtypes = [c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, c_uint32,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   c_void_p, c_void_p, c_void_p, c_void_p]
user32.DestroyWindow.argtypes = [c_void_p]
user32.ShowWindow.argtypes = [c_void_p, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [c_void_p]
user32.GetForegroundWindow.restype = c_void_p
user32.GetForegroundWindow.argtypes = []
user32.PeekMessageW.argtypes = [POINTER(MSG), c_void_p, c_uint32, c_uint32, c_uint32]
user32.TranslateMessage.argtypes = [POINTER(MSG)]
user32.DispatchMessageW.argtypes = [POINTER(MSG)]


class PumpThread(threading.Thread):
    """Owns a real window and drains its message queue for the life of the program."""

    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.ready = threading.Event()
        self.hwnd = None
        self._warned_foreground = False

    def run(self):
        self.hwnd = user32.CreateWindowExW(
            0, "STATIC", "FFB probe (leave me open)", WS_OVERLAPPEDWINDOW,
            CW_USEDEFAULT, CW_USEDEFAULT, 460, 120, None, None, None, None)
        if self.hwnd:
            # The window must be shown AND brought to the foreground. Windows.Gaming.Input
            # only populates its device lists for a foregrounded process -- showing the
            # window without activating it is not enough, enumeration stays empty.
            user32.ShowWindow(self.hwnd, SW_SHOW)
            self.ensure_foreground()
        self.ready.set()

        msg = MSG()
        while not self._stop.is_set():
            while user32.PeekMessageW(byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(byref(msg))
                user32.DispatchMessageW(byref(msg))
            time.sleep(0.005)

        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    def ensure_foreground(self):
        """
        Assert foreground, and report honestly when Windows refuses.

        SetForegroundWindow is not guaranteed: Windows blocks foreground steals from a
        process that does not currently own focus. Whether it succeeds depends on what had
        focus when the tool was launched, so the failure is intermittent by nature -- and
        because enumeration AND force output are both foreground-gated, a silent refusal
        looks exactly like absent hardware. The old code called it once and ignored the
        result, which is why detection worked "sometimes".
        """
        if not self.hwnd:
            return False
        if user32.GetForegroundWindow() == self.hwnd:
            return True
        user32.SetForegroundWindow(self.hwnd)
        ok = user32.GetForegroundWindow() == self.hwnd
        if not ok and not self._warned_foreground:
            self._warned_foreground = True
            print("  NOTE: Windows refused to foreground the probe window."
                  " Click the 'FFB probe' window once -- until then the wheel may not")
            print("        enumerate and force output will be silent.")
            log.event("foreground.refused")
        return ok

    def hide(self):
        """
        Put the window away while keeping the message pump running.

        Only safe once nothing in this process still needs Windows.Gaming.Input. Enumeration,
        force output and position reads are all foreground-gated, and a hidden window cannot
        hold the foreground -- so calling this early silently breaks all three. The bridge
        calls it only after the shim reports itself live, which is the point WGI stops being
        used in this process at all.
        """
        if self.hwnd:
            user32.ShowWindow(self.hwnd, SW_HIDE)

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class ArrivalWatch:
    """
    Subscribes to the device-arrival events.

    This is NOT optional bookkeeping, and polling the static collections alone is not
    equivalent. Windows.Gaming.Input populates `raw_game_controllers` / `racing_wheels` in
    response to arrival notifications dispatched through the message pump; a process that
    never registers a handler may simply never see them, so a poll-only loop finds the
    wheel sometimes and misses it other times with the device plugged in the whole time.
    That was the intermittent-detection bug.

    The handler objects are kept alive deliberately -- if they are garbage collected the
    subscription dies with them and the symptom returns, intermittently, which is a
    genuinely horrible thing to debug.
    """

    def __init__(self):
        self.seen = threading.Event()
        self._handlers = []
        self._tokens = []

    def _on_added(self, kind):
        def handler(sender, device):
            log.event("device.added", kind=kind)
            self.seen.set()
        return handler

    def start(self):
        subscriptions = [
            ("raw controller", gi.RawGameController, "add_raw_game_controller_added"),
            ("racing wheel", gi.RacingWheel, "add_racing_wheel_added"),
        ]
        for kind, cls, adder in subscriptions:
            try:
                handler = self._on_added(kind)
                token = getattr(cls, adder)(handler)
                # Keep BOTH alive: the delegate and the token.
                self._handlers.append(handler)
                self._tokens.append((cls, adder, token))
                log.event("device.subscribed", kind=kind)
            except Exception as exc:
                # Not fatal -- polling still runs underneath. But say so, because it
                # downgrades detection to the unreliable path.
                print("  WARNING: could not subscribe to %s arrivals: %s" % (kind, exc))
                log.event("device.subscribe_failed", kind=kind, error=str(exc))


def wait_for_devices(timeout_seconds, pump=None):
    print("  Waiting for Windows.Gaming.Input to enumerate...")
    watch = ArrivalWatch()
    watch.start()
    deadline = time.monotonic() + timeout_seconds
    last = None
    first_seen = None
    while time.monotonic() < deadline:
        # Re-assert foreground while waiting. Enumeration is foreground-gated, and the
        # initial grab at startup can be refused by Windows depending on which process
        # held focus when the tool launched -- another source of "sometimes".
        if pump is not None:
            pump.ensure_foreground()
        raw = list(gi.RawGameController.raw_game_controllers)
        wheels = list(gi.RacingWheel.racing_wheels)

        # vJoy does not start the settle clock. It is root-enumerated and always wins the
        # race, so treating it as "a device arrived" would burn the settle window on the
        # decoy and abandon the remaining timeout while the real wheel is still coming up.
        real = [c for c in raw if not _is_vjoy_controller(c)]

        if real or wheels:
            now = time.monotonic()
            if first_seen is None:
                first_seen = now
                log.event("detect.first_device", raw=len(raw), wheels=len(wheels))

            # Stop as soon as something with an actual motor is present -- that is the
            # device we came for. Otherwise keep listening: a motorless device (vJoy) may
            # simply have got here first.
            if has_motor(raw, wheels):
                print("  Detected %d raw controller(s), %d racing wheel(s)."
                      % (len(raw), len(wheels)))
                return raw, wheels

            waited = now - first_seen
            if waited >= DEVICE_SETTLE_SECONDS:
                print("  Detected %d raw controller(s), %d racing wheel(s)"
                      " -- none with a force-feedback motor." % (len(raw), len(wheels)))
                return raw, wheels
            if last != "settling":
                print("    device seen but no motor yet -- waiting %.0fs for slower"
                      " hardware..." % DEVICE_SETTLE_SECONDS, flush=True)
                last = "settling"
            time.sleep(0.05)
            continue

        remaining = int(deadline - time.monotonic())
        if remaining != last and remaining % 5 == 0:
            note = " (only the vJoy virtual device so far -- ignoring it)" if raw else ""
            print("    nothing yet... %ds left (press a wheel button)%s"
                  % (remaining, note), flush=True)
            last = remaining
        time.sleep(0.05)

    log.event("detect.timeout", raw=len(gi.RawGameController.raw_game_controllers),
              wheels=len(gi.RacingWheel.racing_wheels))
    return [], []


def has_motor(raw, wheels):
    """
    True once a device with a real force-feedback motor is present.

    vJoy MUST be excluded here, not just in report(). vJoy's virtual device does expose a
    WGI force-feedback motor, so a naive motor check is satisfied by the decoy and detection
    returns before the real wheel -- which is slower, being actual USB hardware -- has
    arrived. Measured in logs/wgi_probe_20260822_233138.log: vJoy at t=0.047 with a motor,
    detection returned at t=0.110, the racing-wheel arrival fired at t=0.094 and was thrown
    away. Same race as before the settle window was added, just re-entered through a
    different door.
    """
    for c in raw:
        try:
            if is_vjoy(c.hardware_vendor_id, c.hardware_product_id):
                continue
            if list(c.force_feedback_motors):
                log.event("detect.motor_found", kind="raw",
                          vid=hex(c.hardware_vendor_id), pid=hex(c.hardware_product_id))
                return True
        except Exception:
            pass
    for w in wheels:
        try:
            if w.wheel_motor is not None:
                log.event("detect.motor_found", kind="wheel")
                return True
        except Exception:
            pass
    return False


def report(raw, wheels):
    motor = None
    label = None
    identity = {"name": "", "key": None}

    rule("Raw game controllers")
    for i, c in enumerate(raw):
        motors = list(c.force_feedback_motors)
        virtual = is_vjoy(c.hardware_vendor_id, c.hardware_product_id)
        print("  [%d] %s" % (i, c.display_name or "(unnamed)"))
        print("       VID 0x%04X  PID 0x%04X   axes=%d buttons=%d switches=%d"
              % (c.hardware_vendor_id, c.hardware_product_id,
                 c.axis_count, c.button_count, c.switch_count))
        print("       force_feedback_motors : %d" % len(motors))
        if virtual:
            print("       (vJoy virtual device, not hardware -- ignored for FFB)")
        if c.hardware_vendor_id == HORI_VENDOR_ID:
            print("       >>> HORI device <<<")

        # Key the saved calibration to the device we actually DRIVE. Taking the first
        # controller in the list would key it to whichever device enumerated first --
        # on this machine the vJoy device, whose VID:PID is not the wheel's.
        #
        # VID:PID rather than instance id, so the profile survives replugging and
        # different USB ports; the wheel's two modes have different PIDs and so
        # calibrate separately.
        if motors and not virtual and not identity["key"]:
            identity["key"] = wp.device_key(c.hardware_vendor_id, c.hardware_product_id)
            identity["name"] = c.display_name or "(unnamed)"

        if motor is None and motors and not virtual:
            motor, label = motors[0], "RawGameController.force_feedback_motors[0]"
        print()

    rule("Racing wheels")
    if not wheels:
        print("  Nothing classified as a RacingWheel.")
    for i, w in enumerate(wheels):
        print("  [%d] max_wheel_angle=%.1f  clutch=%s  handbrake=%s  shifter=%s"
              % (i, w.max_wheel_angle, w.has_clutch, w.has_handbrake, w.has_pattern_shifter))
        wm = w.wheel_motor
        print("       wheel_motor : %s" % ("present" if wm else "None"))
        if wm is not None and motor is None:
            motor, label = wm, "RacingWheel.wheel_motor"

    if identity["key"] is None:
        # Motor came from RacingWheel, or nothing has a motor at all. Fall back to the
        # first device that is not the vJoy virtual one, so calibration is never keyed to
        # a software device.
        for c in raw:
            if not is_vjoy(c.hardware_vendor_id, c.hardware_product_id):
                identity["key"] = wp.device_key(c.hardware_vendor_id, c.hardware_product_id)
                identity["name"] = c.display_name or "(unnamed)"
                break

    return motor, label, (wheels[0] if wheels else None), identity


def describe_motor(motor):
    for attr, fmt in (("is_enabled", "%s"), ("supported_axes", None),
                      ("master_gain", "%.2f"), ("are_effects_paused", "%s")):
        try:
            value = getattr(motor, attr)
            if attr == "supported_axes":
                value = decode_axes(value)
                fmt = "%s"
            print("    %-18s : %s" % (attr, fmt % value))
        except Exception as exc:
            print("    %-18s : query failed (%s)" % (attr, exc))


# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------

def build_constant(magnitude, duration):
    e = ff.ConstantForceEffect()
    e.set_parameters(Vector3(magnitude, 0.0, 0.0), timedelta(seconds=duration))
    return e, "steady pull to one side"


def build_ramp(magnitude, duration):
    e = ff.RampForceEffect()
    # Sweep full LEFT -> through zero -> full RIGHT. Ramping 0 -> magnitude is nearly
    # imperceptible at low magnitude; crossing the direction reversal is unmistakable.
    e.set_parameters(Vector3(-magnitude, 0.0, 0.0), Vector3(magnitude, 0.0, 0.0),
                     timedelta(seconds=duration))
    return e, "sweeps from full LEFT, through zero, to full RIGHT"


def _periodic(kind, magnitude, duration, freq, label):
    e = ff.PeriodicForceEffect(kind)
    # (vector, frequency in Hz, phase 0..1 == 0..360 deg, bias -1..1, duration)
    #
    # Frequency matters enormously on a heavy wheel. At 2 Hz the motor reverses twice a
    # second, the wheel's inertia barely responds, and it degrades into a faint buzz that
    # is easy to miss. Below ~1 Hz it becomes an obvious left-right rocking motion.
    e.set_parameters(Vector3(magnitude, 0.0, 0.0), freq, 0.0, 0.0,
                     timedelta(seconds=duration))
    return e, "%s at %.2f Hz" % (label, freq)


# Sign convention for condition effects.
#
# The WGI docs say positive coefficients RESIST (a centring spring, a drag). This firmware
# does not follow one convention for all four kinds, which is the whole reason this table
# exists per effect rather than globally.
#
# MEASURED 2026-08-23, hands-off release tests logged to logs/wgi_probe_20260823_00*.log.
# The test: with the motor held the wheel parks wherever it is left, so after the hand
# comes off, any sustained motion is the effect's doing. A passive effect ends at rest; an
# inverted one drives to the end stop and is still moving seconds later.
#
#            sign A                          sign B
#   Spring   centres                         centres          -> insensitive to the flip
#   Damper   passive, resists speed          RUNAWAY to stop  -> needs A
#   Friction RUNAWAY to stop                 passive          -> needs B
#   Inertia  passive                         passive          -> never runs away either way
#
# Damper and friction need OPPOSITE signs, so no global setting can be correct for both --
# which is what the old single "c" toggle assumed. Spring centring on both signs is
# genuinely odd; the likeliest explanation is that the firmware derives spring force from
# the displacement sign internally and ignores the supplied direction vector for that kind.
# Not chased further: it works either way, which is all that is required of it.
#
#   name, direction.x, positiveCoefficient, negativeCoefficient
CONDITION_SIGNS = [
    ("A  docs      dir +X, coeff +1/+1", 1.0, 1.0, 1.0),
    ("B  flip dir  dir -X, coeff +1/+1", -1.0, 1.0, 1.0),
    ("C  flip coef dir +X, coeff -1/-1", 1.0, -1.0, -1.0),
]
# Per-effect, from the measurements above. "c" overrides every kind at once for
# experiments; None means "use the measured default for this kind".
CONDITION_SIGN_FOR = {
    ff.ConditionForceEffectKind.SPRING: 0,    # A -- centres on either, keep the documented one
    ff.ConditionForceEffectKind.DAMPER: 0,    # A -- B drives it into the end stop
    ff.ConditionForceEffectKind.INERTIA: 0,   # A -- passive on both, no reason to differ
    ff.ConditionForceEffectKind.FRICTION: 1,  # B -- A drives it into the end stop
}
CONDITION_SIGN = None

# Broken firmware effects are hidden from the menu by default -- see EFFECTS below.
SHOW_BROKEN = False


def _condition(kind, magnitude, label):
    e = ff.ConditionForceEffect(kind)
    # (direction, positiveCoefficient, negativeCoefficient,
    #  maxPositiveMagnitude, maxNegativeMagnitude, deadZone, bias)
    #
    # Sign convention here is NOT DirectInput's, and Microsoft's own C++ sample is
    # misleading: it passes -1.0 for negativeCoefficient and -0.3 for maxNegativeMagnitude,
    # but the runtime rejects negative max magnitudes outright with E_INVALIDARG, and the
    # reference text says maxNegativeMagnitude "Range is from 0 to 1.0".
    #
    # The Remarks settle the coefficients: the illustration of a normal condition effect
    # has "all coefficient values are positive", and a negative coefficient "will cause
    # the force to go negative ... effectively, reversing the direction of the force",
    # which is explicitly not recommended. So BOTH coefficients are positive for a spring
    # that resists displacement.
    #
    # coefficient = SLOPE (how fast force builds as you move away from centre)
    # max magnitude = CLAMP (both positive, 0..1)
    # Keep the slope at full and scale strength via the clamp.
    #
    # The max magnitudes stay POSITIVE in every convention -- that is not a sign choice,
    # the runtime rejects a negative one with E_INVALIDARG regardless.
    which = CONDITION_SIGN if CONDITION_SIGN is not None else CONDITION_SIGN_FOR.get(kind, 0)
    sign_name, dir_x, pos_c, neg_c = CONDITION_SIGNS[which]
    e.set_parameters(Vector3(dir_x, 0.0, 0.0),
                     pos_c, neg_c,
                     magnitude, magnitude,
                     0.025, 0.0)
    return e, "%s   [sign %s]" % (label, sign_name.split()[0])


# Three behavioural classes, because how you TEST them differs completely:
#
#   "push"      unidirectional. Pins a free wheel to the end stop. Hold it firmly.
#   "wave"      symmetric, zero average force. Will NOT pin the wheel -- a free wheel
#               visibly rocks back and forth instead. Let it move; that is the clearest
#               signal. Held rigidly these feel like faint pressure and are easy to miss.
#   "condition" reactive. No force at all unless the wheel is moving. Hold and turn.
#
# VERDICT is what this firmware was MEASURED to do, not what the API promises:
#
#   "works"     honoured properly. Direction and magnitude both land.
#   "silent"    loads Succeeded, reports Running, produces no torque whatsoever.
#   "inverted"  produces force that ADDS to your motion instead of resisting it. Worse
#               than silent: actively wrong, and it fights you.
#
# Only "works" effects are listed in the menu by default -- offering nine broken entries
# to a first-time user buries the two real ones. They all still RUN if typed by number,
# because reproducing these results is the whole point of the tool, and the numbering
# stays fixed at 1-11 so logs and notes from earlier sessions still line up.
#
# label, builder(magnitude, duration, frequency) -> (effect, description), class, verdict
EFFECTS = [
    ("Constant force", lambda m, d, f: build_constant(m, d), "push", "works"),
    ("Ramp force", lambda m, d, f: build_ramp(m, d), "push", "works"),
    ("Sine wave", lambda m, d, f: _periodic(ff.PeriodicForceEffectKind.SINE_WAVE, m, d, f,
                                            "smooth left-right rocking"), "wave", "silent"),
    ("Square wave", lambda m, d, f: _periodic(ff.PeriodicForceEffectKind.SQUARE_WAVE, m, d, f,
                                              "hard alternating jolts"), "wave", "silent"),
    ("Triangle wave", lambda m, d, f: _periodic(ff.PeriodicForceEffectKind.TRIANGLE_WAVE,
                                                m, d, f, "linear rise and fall"),
     "wave", "silent"),
    ("Sawtooth up", lambda m, d, f: _periodic(ff.PeriodicForceEffectKind.SAWTOOTH_WAVE_UP,
                                              m, d, f, "ramp up then snap back"),
     "wave", "silent"),
    ("Sawtooth down", lambda m, d, f: _periodic(ff.PeriodicForceEffectKind.SAWTOOTH_WAVE_DOWN,
                                                m, d, f, "snap up then ramp down"),
     "wave", "silent"),
    ("Spring", lambda m, d, f: _condition(ff.ConditionForceEffectKind.SPRING, m,
                                          "pulls back to centre"), "condition", "works"),
    ("Damper", lambda m, d, f: _condition(ff.ConditionForceEffectKind.DAMPER, m,
                                          "resists speed -- turn fast vs slow"),
     "condition", "works"),
    ("Inertia", lambda m, d, f: _condition(ff.ConditionForceEffectKind.INERTIA, m,
                                           "resists acceleration -- heavy to start turning"),
     "condition", "coarse"),
    ("Friction", lambda m, d, f: _condition(ff.ConditionForceEffectKind.FRICTION, m,
                                            "constant drag whenever the wheel moves"),
     "condition", "works"),
]

VERDICT_TAGS = {
    "works": "",
    "silent": "  [silent on this firmware]",
    "inverted": "  [INVERTED -- adds to your motion]",
    # Produces force, but not the force it advertises. Passive on both signs -- it never
    # runs away -- and it is definitely doing something: held against a silent effect that
    # occupies the motor identically, only this one feels notchy. But cogging is not
    # acceleration resistance, which should be a smooth weight at the start of a turn and
    # free once moving. Safe to run, not usable as inertia.
    "coarse": "  [produces force, but notchy -- not acceleration resistance]",
}

HOW_TO_FEEL = {
    "push": ["HOLD THE WHEEL FIRMLY. This pushes one direction; let go and it",
             "will spin to the end stop and pin there."],
    "wave": ["LET THE WHEEL GO (or hold very loosely) AND WATCH IT.",
             "This wave is symmetric, so its average force is zero -- it will NOT",
             "pin the wheel, it should visibly ROCK it left and right. Gripping",
             "hard masks it into a faint buzz."],
    "condition": ["HOLD AND TURN THE WHEEL. This one only reacts to movement --",
                  "it produces no force at all while the wheel is still.",
                  "KEEP HOLD OF IT: if the sign convention is wrong the force ADDS to",
                  "your movement instead of resisting it, and the wheel runs away to",
                  "the end stop. That is a reversed-sign result, not a firmware limit."],
}


class Session:
    def __init__(self, motor, loop, gain, magnitude, duration, pump=None, frequency=0.5,
                 wheel=None, device_name="", key=None):
        self.motor = motor
        self.loop = loop
        self.gain = gain
        self.magnitude = magnitude
        self.duration = duration
        self.frequency = frequency
        self.pump = pump
        self.loaded = []
        # The RacingWheel object, used only to read the wheel's POSITION. That reading is
        # what makes calibration self-measuring and software condition effects possible.
        self.wheel = wheel
        self.device_name = device_name
        self.key = key
        self.profile = wp.load_profile(key) if key else None

    def read_wheel(self):
        """Current steering position, -1.0 to 1.0, or None if unreadable."""
        if self.wheel is None:
            return None
        try:
            return self.wheel.get_current_reading().wheel
        except Exception:
            return None

    # Below this speed (reading-units/sec) the wheel counts as stationary. At rest the
    # reading dithers by a bit or two and the derivative of that noise is meaningless.
    MOVING_SPEED = 0.05

    def report_motion(self, samples, label, release_at):
        """
        Turn a position trace into evidence about what an effect did.

        The firmware's commanded force cannot be read back, so every number here comes from
        position over time. The discriminator, which needs no force sensing:

            A correct spring, damper, friction or inertia is PASSIVE. It removes energy, or
            returns the wheel toward centre. An inverted one INJECTS energy.

        That test is only valid once the hand is off. While a hand drives the wheel the
        motion is whatever the hand imposes; a steady back-and-forth accelerates half the
        time and decelerates half the time whatever the motor is doing. So the active phase
        is reported for context only, and the verdict comes from the released phase.
        """
        if len(samples) < 10:
            print("  (no usable position trace -- %d samples)" % len(samples))
            log.event("motion.none", effect=label, samples=len(samples))
            return

        active = [(t, p) for t, p in samples if t < release_at]
        freed = [(t, p) for t, p in samples if t >= release_at]

        print()
        print("  MOTION  %d samples over %.1fs (released at %.1fs)"
              % (len(samples), samples[-1][0], release_at))
        if active:
            span = max(p for _t, p in active) - min(p for _t, p in active)
            print("          active phase : travel %.3f, peak speed %.2f/s"
                  % (span, self._peak_speed(active)))

        if len(freed) < 10:
            print("          released phase too short to judge -- no verdict")
            log.event("motion.summary", effect=label, verdict="no-release-phase",
                      samples=len(samples))
            return

        # Anchor on where the hand ACTUALLY left the wheel, not on where the prompt was
        # printed. Nobody releases on zero reaction time, and the prompt can land mid-swing:
        # measured once at +0.028 while the real release was 0.4s later at +0.245, which
        # turned a textbook centring trace into "coasted". The release is the last outward
        # extreme -- after it, nothing pushes the wheel outward again unless the effect does.
        # Only the first half is searched, so there is always motion left to observe.
        whole_freed = freed
        head = freed[:max(2, len(freed) // 2)]
        anchor = max(range(len(head)), key=lambda i: abs(head[i][1]))
        freed = freed[anchor:]

        start_pos, end_pos = freed[0][1], freed[-1][1]
        drift = abs(end_pos) - abs(start_pos)
        peak = self._peak_speed(freed)
        end_speed = self._peak_speed(freed[-max(5, len(freed) // 4):])

        print("          hands off at %+.3f (t=%.1fs) -> ended %+.3f  (%+.3f toward the"
              " stop)" % (start_pos, freed[0][0], end_pos, drift))
        print("          peak speed after release %.2f/s, final %.2f/s" % (peak, end_speed))

        # Coast numbers, measured over the WHOLE released window rather than from the
        # anchor. These are what compare a dissipative effect against a silent one: run the
        # same flick under a silent effect (a periodic -- it holds the motor but produces no
        # torque here) and under the damper. Shorter coast under the damper is the damper
        # working, measured rather than felt. The anchor is deliberately not used: on a
        # flick the wheel is released while moving fast and travels further out afterwards,
        # so the furthest point is the END of the coast, not the start of it.
        coast_travel = max(p for _t, p in whole_freed) - min(p for _t, p in whole_freed)
        stopped_at = None
        for (t0, p0), (t1, p1) in zip(whole_freed, whole_freed[1:]):
            dt = t1 - t0
            if dt > 0 and abs((p1 - p0) / dt) > self.MOVING_SPEED:
                stopped_at = t1 - whole_freed[0][0]
        print("          coast: travelled %.3f, still moving %s"
              % (coast_travel,
                 "%.2fs after release" % stopped_at if stopped_at else "not at all"))

        # Withhold the verdict when the released window was clearly not hands-off.
        #
        # This firmware drives a free wheel at up to about 2 reading-units/sec at the gains
        # used here. Anything far above that is a hand, or a reading discontinuity. Measured:
        # a sine effect -- silent, incapable of moving anything -- scored "CENTRING" at a
        # peak of 20/s purely because the flick landed inside the window the tool believed
        # was unattended. A verdict from a contaminated window is worse than no verdict,
        # because it looks like evidence.
        HAND_SPEED = 5.0
        if peak > HAND_SPEED:
            print("          >>> NO VERDICT -- peak %.1f/s is far above what this firmware"
                  % peak)
            print("              can drive on its own. The wheel was still being handled")
            print("              (or the reading jumped). Hands OFF before the prompt.")
            log.event("motion.summary", effect=label, verdict="contaminated",
                      peak_speed=round(peak, 3), coast=round(coast_travel, 3))
            return

        # Interpretation, in the order the cases can be told apart.
        #
        # Runaway is decisive: nothing passive can drive a released wheel outward at
        # sustained speed. Centring is equally decisive the other way, but only a spring
        # should do it -- a damper, friction or inertia that pulls to centre is a spring
        # wearing the wrong name. Everything else is "stayed put", which is correct for the
        # three dissipative effects and indistinguishable from doing nothing at all -- the
        # released phase cannot separate a working damper from a silent one, only the
        # active-phase feel can.
        # Still moving at the end is the decisive test, and it beats drift.
        #
        # A passive effect dissipates: whatever the wheel was doing when the hand left, it
        # ends at rest. Only an effect injecting energy keeps it going indefinitely. Drift
        # was the original signal and it is unreliable -- measured on friction, the wheel
        # ran to full lock, bounced off the mechanical stop and came back across centre, so
        # the displacement was NEGATIVE and scored "centring" on a textbook runaway.
        # Hitting the stop is the same story: nothing passive reaches it from a standstill.
        # "Reached the end stop" has to mean DROVE there, not "was already parked there".
        # A wheel left against the stop at the end of the active phase sits at 0.999 for the
        # whole released window; without the travel guard that scored as a runaway on a sine
        # effect, which cannot move anything at all. Requiring real travel first also costs
        # nothing on a true runaway -- friction on sign A covered 1.127 getting there.
        hit_stop = (coast_travel > 0.25
                    and max(abs(p) for _t, p in whole_freed) >= 0.98)
        if end_speed > self.MOVING_SPEED or hit_stop:
            verdict = "runaway"
            print("          >>> RUNAWAY -- %s"
                  % ("drove itself into the end stop." if hit_stop
                     else "still moving %.2f/s when the window ended." % end_speed))
            print("              Nothing passive can do this. The sign is INVERTED.")
        elif drift < -0.05:
            verdict = "centring"
            print("          >>> CENTRING -- returned toward centre on its own.")
            print("              Correct for a spring; wrong for damper/friction/inertia.")
        elif peak < self.MOVING_SPEED:
            verdict = "held"
            print("          >>> HELD STILL -- no self-driven motion.")
            print("              Correct for damper/friction/inertia; a spring should have")
            print("              pulled back, so for a spring this means silent or dead.")
        else:
            verdict = "coasted"
            print("          >>> COASTED to a stop without returning to centre.")
            print("              Consistent with a dissipative effect, or with none at all.")

        if abs(start_pos) < 0.10:
            print("          NOTE: released near centre (%+.3f) -- a spring has almost"
                  " nothing" % start_pos)
            print("                to pull against there. Release further out to be sure.")

        log.event("motion.summary", effect=label, verdict=verdict,
                  samples=len(samples), released_at=round(release_at, 2),
                  start=round(start_pos, 3), end=round(end_pos, 3),
                  drift=round(drift, 3), peak_speed=round(peak, 3),
                  end_speed=round(end_speed, 3), coast=round(coast_travel, 3),
                  moving_for=round(stopped_at, 2) if stopped_at else 0.0)
        # Downsample to ~10 Hz so the trace is re-checkable without flooding the log.
        trace = ["%.2f:%+.3f" % (t, p) for i, (t, p) in enumerate(samples) if i % 5 == 0]
        log.event("motion.trace", effect=label, points=" ".join(trace))

    @staticmethod
    def _peak_speed(points):
        peak = 0.0
        for (t0, p0), (t1, p1) in zip(points, points[1:]):
            dt = t1 - t0
            if dt > 0:
                peak = max(peak, abs((p1 - p0) / dt))
        return peak

    def sync(self, coro):
        return self.loop.run_until_complete(coro)

    def grab_foreground(self):
        """
        Force-feedback output is gated on this process being in the FOREGROUND. Typing a
        menu command in the console drops it, which is why an effect can load and take the
        motor (the wheel goes loose) yet produce no torque. Re-assert before every effect.
        """
        if self.pump is not None:
            self.pump.ensure_foreground()

    def is_foreground(self):
        if self.pump is None or not self.pump.hwnd:
            return None
        return user32.GetForegroundWindow() == self.pump.hwnd

    def unpause(self):
        """A paused motor silently swallows every effect."""
        try:
            if self.motor.are_effects_paused:
                print("  motor reports effects PAUSED -- resuming")
                self.motor.resume_all_effects()
        except Exception as exc:
            print("  could not check/resume paused state: %s" % exc)

    def apply_gain(self):
        try:
            self.motor.master_gain = self.gain
            print("  master_gain = %.2f" % self.motor.master_gain)
        except Exception as exc:
            print("  could not set master_gain: %s" % exc)

    def effective(self):
        return self.gain * self.magnitude

    def run_effect(self, index):
        label, builder, kind, verdict = EFFECTS[index]
        # duration 0 means "a long look" -- 15s. It deliberately does NOT wait on Enter,
        # because pressing Enter needs console focus, and taking console focus kills the
        # force output we are trying to feel.
        duration = 15.0 if self.duration <= 0 else self.duration
        try:
            effect, description = builder(self.magnitude, duration, self.frequency)
        except Exception as exc:
            print("  Could not build %s: %s" % (label, exc))
            return

        print()
        rule()
        print("  %s -- %s" % (label.upper(), description))
        if verdict != "works":
            # Say so up front. Otherwise a silent wheel reads as "the tool is broken"
            # rather than "this is the documented result being reproduced".
            print("  KNOWN RESULT: %s" % VERDICT_TAGS[verdict].strip().strip("[]"))
        print("  gain %.2f x magnitude %.2f = %.0f%% of full torque"
              % (self.gain, self.magnitude, self.effective() * 100))
        for i, line in enumerate(HOW_TO_FEEL[kind]):
            print("  %s %s" % (">>" if i == 0 else "  ", line))

        # master_gain is latched at LOAD time, so it must be set before load_effect_async,
        # not after. Setting it under an already-running effect has no effect at all.
        try:
            self.motor.master_gain = self.gain
        except Exception:
            pass

        try:
            result = self.sync(self.motor.load_effect_async(effect))
        except Exception as exc:
            print("  LoadEffectAsync raised: %s" % exc)
            return

        name = LOAD_RESULT_NAMES.get(result, str(result))
        print("  LoadEffectAsync -> %s" % name)
        if result != ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
            print("  -> the firmware will not accept this effect kind.")
            return

        self.loaded.append(effect)
        try:
            self.unpause()
            self.grab_foreground()
            print("  (bringing the probe window to the foreground -- DO NOT click away,")
            print("   force output stops when this process is not in front)")

            for n in (3, 2, 1):
                print("    %d..." % n, flush=True)
                time.sleep(1.0)

            print()
            print("  >>>>>>  FORCE ON  <<<<<<", flush=True)
            effect.start()
            time.sleep(0.15)
            try:
                print("  effect state: %s" % STATE_NAMES.get(effect.state, effect.state))
            except Exception:
                pass

            # Sample the wheel while the effect holds the motor.
            #
            # Without this the log records only that the effect ran, which is why the
            # condition verdicts had to be judged by feel -- and feel is how the wrong
            # verdicts got recorded in the first place. Position over time is the only
            # evidence available: the force the firmware commands cannot be read back, but
            # a correct spring, damper, friction or inertia can only remove energy or pull
            # toward centre. None of them can make the wheel speed up on its own, so
            # "does it accelerate the hand" is answerable from position alone.
            # The hold is split into an ACTIVE phase and a RELEASED phase.
            #
            # Only the released phase carries a verdict. While a hand is driving the wheel
            # the motion is whatever the hand imposes -- a steady back-and-forth spends half
            # its ticks accelerating and half decelerating no matter what the motor does, so
            # no statistic taken over that phase can separate a working effect from an
            # inverted one. With the hand off, the wheel moves only under the effect, and
            # the question becomes trivial: passive effects cannot speed it up.
            release_at = max(1.5, duration - 3.0)
            released = False
            samples = []
            start = time.monotonic()
            end = start + duration
            next_tick = start + 1.0
            remaining = int(duration)
            while time.monotonic() < end:
                now = time.monotonic()
                pos = self.read_wheel()
                if pos is not None:
                    samples.append((now - start, pos))
                if not released and now - start >= release_at:
                    released = True
                    print()
                    print("  >>>>>>  LET GO NOW -- hands OFF until FORCE OFF  <<<<<<",
                          flush=True)
                if now >= next_tick:
                    # Keep re-asserting foreground, and show whether we actually hold it.
                    self.grab_foreground()
                    fg = self.is_foreground()
                    flag = "" if fg is None else ("  [foreground OK]" if fg
                                                  else "  [!! NOT foreground - no force !!]")
                    state = ""
                    try:
                        state = "  state=%s" % STATE_NAMES.get(effect.state, effect.state)
                    except Exception:
                        pass
                    print("    %2ds remaining%s%s" % (remaining, state, flag), flush=True)
                    remaining -= 1
                    next_tick += 1.0
                time.sleep(0.02)

            effect.stop()
            print("  >>>>>>  FORCE OFF <<<<<<")
            self.report_motion(samples, label, release_at)
            print()
            print("  (The wheel may feel LOOSE now rather than snapping back -- while we")
            print("   hold the motor the firmware auto-centering stays suspended. Use 'r'")
            print("   to release the motor and get the stock centering spring back.)")
        except KeyboardInterrupt:
            print("\n  Interrupted.")
        finally:
            try:
                effect.stop()
            except Exception:
                pass
            try:
                self.sync(self.motor.try_unload_effect_async(effect))
            except Exception:
                pass
            if effect in self.loaded:
                self.loaded.remove(effect)

    def _play_once(self, gain, magnitude, seconds, note):
        """
        One fully independent shot: set gain, build, LOAD, start, hold, stop, unload.

        Reloading per step is the whole point. master_gain is latched by the driver when
        an effect is LOADED -- changing it under a running effect does nothing, which is
        what made the original single-load sweep useless.
        """
        try:
            self.motor.master_gain = gain
        except Exception as exc:
            print("    could not set gain: %s" % exc)

        effect = ff.ConstantForceEffect()
        effect.set_parameters(Vector3(magnitude, 0.0, 0.0), timedelta(seconds=seconds + 1))

        try:
            result = self.sync(self.motor.load_effect_async(effect))
        except Exception as exc:
            print("    LoadEffectAsync raised: %s" % exc)
            return
        if result != ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
            print("    %s -> load %s" % (note, LOAD_RESULT_NAMES.get(result, str(result))))
            return

        self.loaded.append(effect)
        try:
            self.unpause()
            self.grab_foreground()
            effect.start()
            fg = self.is_foreground()
            flag = "" if fg is None else ("" if fg else "  [!! NOT foreground !!]")
            print("    %s%s" % (note, flag), flush=True)
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                self.grab_foreground()
                time.sleep(0.2)
            effect.stop()
        finally:
            try:
                effect.stop()
            except Exception:
                pass
            try:
                self.sync(self.motor.try_unload_effect_async(effect))
            except Exception:
                pass
            if effect in self.loaded:
                self.loaded.remove(effect)

    def _stepped(self, title, explanation, steps, seconds=2.5):
        print()
        rule()
        print("  %s" % title)
        print("  %s" % explanation)
        print("  Each step is a SEPARATE load -- gain is latched at load time.")
        self.grab_foreground()
        for n in (3, 2, 1):
            print("    %d..." % n, flush=True)
            time.sleep(1.0)
        try:
            for gain, magnitude, note in steps:
                self._play_once(gain, magnitude, seconds, note)
                time.sleep(0.4)  # brief gap so steps are distinguishable
            print("  >>>>>>  DONE  <<<<<<")
        except KeyboardInterrupt:
            print("\n  Interrupted.")
        finally:
            self.apply_gain()

    def sweep_gain(self):
        steps = [(s / 10.0, 1.0, "master_gain %.1f  (magnitude 1.0)" % (s / 10.0))
                 for s in range(1, 11)]
        self._stepped(
            "GAIN SWEEP -- master_gain 0.1 -> 1.0, magnitude fixed at 1.0",
            "If every step feels identical, the firmware ignores master_gain.",
            steps)

    def sweep_magnitude(self):
        steps = [(1.0, s / 10.0, "magnitude %.1f  (gain 1.0)" % (s / 10.0))
                 for s in range(1, 11)]
        self._stepped(
            "MAGNITUDE SWEEP -- effect magnitude 0.1 -> 1.0, gain fixed at 1.0",
            "If every step feels identical, the firmware ignores the effect magnitude too.",
            steps)

    def direction_test(self):
        steps = []
        for _ in range(3):
            steps.append((1.0, 1.0, "push RIGHT (+1.0)"))
            steps.append((1.0, -1.0, "push LEFT  (-1.0)"))
        self._stepped(
            "DIRECTION TEST -- alternating +1.0 / -1.0 at full strength",
            "If direction alternates, the wheel IS reading the force vector.",
            steps, seconds=2.0)

    def synth_wave(self, shape="sine"):
        """
        Software-synthesised waveform, built from a rapidly-updated CONSTANT force.

        This wheel's firmware accepts periodic effects (LoadEffectAsync -> Succeeded,
        state -> Running) but produces no torque from them. Constant force, by contrast,
        works perfectly. So instead of asking the firmware for a sine, we hold ONE constant
        force effect and rewrite its magnitude ~50x a second to trace the wave ourselves.

        This is how games drive hardware like this. If it works, every waveform becomes
        available in software and the firmware's missing periodic support stops mattering.

        The open question it answers: does SetParameters update a RUNNING effect, or is it
        latched at load time the way master_gain is?
        """
        import math

        duration = 10.0 if self.duration <= 0 else self.duration
        rate = 50.0                     # updates per second
        freq = self.frequency
        amp = self.magnitude

        effect = ff.ConstantForceEffect()
        effect.set_parameters(Vector3(0.0, 0.0, 0.0), timedelta(seconds=duration + 5))

        print()
        rule()
        print("  SOFTWARE-SYNTHESISED %s -- %.2f Hz, %.0f%% amplitude, %.0f updates/sec"
              % (shape.upper(), freq, amp * 100, rate))
        print("  One constant-force effect, magnitude rewritten in a loop to trace the wave.")
        print("  >> LET THE WHEEL GO AND WATCH IT. It should rock left and right.")

        try:
            self.motor.master_gain = self.gain
        except Exception:
            pass

        try:
            result = self.sync(self.motor.load_effect_async(effect))
        except Exception as exc:
            print("  LoadEffectAsync raised: %s" % exc)
            return
        print("  LoadEffectAsync -> %s" % LOAD_RESULT_NAMES.get(result, str(result)))
        if result != ff.ForceFeedbackLoadEffectResult.SUCCEEDED:
            return

        self.loaded.append(effect)
        updates = 0
        failures = 0
        peak = 0.0
        trough = 0.0
        try:
            self.unpause()
            self.grab_foreground()
            for n in (3, 2, 1):
                print("    %d..." % n, flush=True)
                time.sleep(1.0)

            print("  >>>>>>  FORCE ON  <<<<<<", flush=True)
            effect.start()

            start = time.monotonic()
            next_tick = start
            last_print = start
            while True:
                now = time.monotonic()
                elapsed = now - start
                if elapsed >= duration:
                    break

                phase = 2.0 * math.pi * freq * elapsed
                if shape == "sine":
                    value = math.sin(phase)
                else:  # square
                    value = 1.0 if math.sin(phase) >= 0 else -1.0

                force = amp * value
                try:
                    effect.set_parameters(Vector3(force, 0.0, 0.0),
                                          timedelta(seconds=duration + 5))
                    updates += 1
                except Exception:
                    failures += 1

                # Every tick to the log: the console can only show a sparse sample, and a
                # sparse sample of a periodic signal is exactly how you fool yourself.
                log.event("wave.tick", shape=shape, freq=freq, t=elapsed, force=force,
                          reading=self.read_wheel())
                peak = max(peak, abs(force))
                trough = min(trough, force)

                # Deliberately not a round 1.0s: at 0.5 Hz the half-period is exactly 1s,
                # so a 1s print interval samples every zero crossing and reports a wave
                # that swings +-0.30 as though it never left zero.
                if now - last_print >= 0.7:
                    self.grab_foreground()
                    fg = self.is_foreground()
                    flag = "" if fg is None else ("" if fg else "  [!! NOT foreground !!]")
                    print("    t=%4.1fs  force=%+.2f  range so far %+.2f..%+.2f  updates=%d%s"
                          % (elapsed, force, trough, peak, updates, flag), flush=True)
                    last_print = now

                next_tick += 1.0 / rate
                sleep_for = next_tick - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_tick = time.monotonic()

            effect.stop()
            print("  >>>>>>  FORCE OFF <<<<<<")
            print("  %d parameter updates sent, %d failed." % (updates, failures))
            print("  commanded force actually swung %+.2f .. %+.2f (amplitude %.2f)"
                  % (trough, peak, amp))
        except KeyboardInterrupt:
            print("\n  Interrupted.")
        finally:
            try:
                effect.stop()
            except Exception:
                pass
            try:
                self.sync(self.motor.try_unload_effect_async(effect))
            except Exception:
                pass
            if effect in self.loaded:
                self.loaded.remove(effect)

    def stop_all(self):
        try:
            self.motor.stop_all_effects()
        except Exception:
            pass
        for effect in list(self.loaded):
            try:
                effect.stop()
                self.sync(self.motor.try_unload_effect_async(effect))
            except Exception:
                pass
        self.loaded.clear()

    def reset(self):
        self.stop_all()
        try:
            ok = self.sync(self.motor.try_reset_async())
            print("  try_reset_async -> %s" % ok)
            print("  The wheel's own auto-centering should be back now.")
        except Exception as exc:
            print("  try_reset_async failed: %s" % exc)


def menu(session):
    # Module-level, because the EFFECTS builders are plain lambdas that only receive
    # (magnitude, duration, frequency) and have no session object to read a setting from.
    global CONDITION_SIGN, SHOW_BROKEN
    while True:
        rule("INTERACTIVE -- effects on demand")
        fg = session.is_foreground()
        print("  gain=%.2f  magnitude=%.2f  ->  %.0f%% of full torque      duration=%.1fs"
              % (session.gain, session.magnitude, session.effective() * 100,
                 15.0 if session.duration <= 0 else session.duration))
        if fg is False:
            print("  (probe window is not in front right now -- effects re-grab it on start)")
        if session.profile:
            print("  calibrated: force->reading %+d, live updates %s"
                  % (session.profile["force_to_reading_sign"],
                     "yes" if session.profile.get("live_update") else "no"))
        else:
            print("  NOT CALIBRATED -- press 'k'. Software effects need it.")

        tags = {"push": "(hold firmly)", "wave": "(let it go, watch it rock)",
                "condition": "(hold AND turn)"}
        print()
        print("  FIRMWARE EFFECTS")
        hidden = 0
        for i, (label, _b, kind, verdict) in enumerate(EFFECTS, start=1):
            if verdict in ("silent", "inverted") and not SHOW_BROKEN:
                hidden += 1
                continue
            print("   %2d) %-16s %s%s" % (i, label, tags[kind], VERDICT_TAGS[verdict]))
        if hidden:
            print("       (%d more the firmware ignores or inverts -- 'b' to show;"
                  " they still run if typed)" % hidden)
        print()
        print("  SOFTWARE EFFECTS  (computed here from constant force -- need calibration)")
        print("    y) sine        z) square")
        print("    1s) spring     2s) damper      3s) friction    4s) inertia")
        print()
        print("  MEASURE")
        print("    k) CALIBRATE (saves to wheel_profile.json)   i) motor info")
        print("    w) gain sweep    e) magnitude sweep    x) direction test")
        print()
        print("  SETTINGS      gain=%.2f  magnitude=%.2f  duration=%.1fs  freq=%.2fHz"
              % (session.gain, session.magnitude,
                 15.0 if session.duration <= 0 else session.duration, session.frequency))
        print("    g) gain   m) magnitude   d) duration   f) frequency")
        print("    b) show firmware effects that don't work (%s)"
              % ("shown" if SHOW_BROKEN else "hidden"))
        if SHOW_BROKEN:
            print("    c) condition sign override (%s)"
                  % ("per-effect, as measured" if CONDITION_SIGN is None
                     else "ALL forced to %s" % CONDITION_SIGNS[CONDITION_SIGN][0]))
        print()
        print("    r) release motor (restore stock centering)    s) stop all    q) quit")
        print()
        try:
            choice = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        log.event("menu.choice", choice=choice, gain=session.gain,
                  magnitude=session.magnitude, duration=session.duration,
                  frequency=session.frequency)

        if choice == "q":
            return
        if choice == "s":
            session.stop_all()
            print("  Stopped.")
            continue
        if choice == "k":
            try:
                measured = wp.calibrate(session)
            except KeyboardInterrupt:
                session.stop_all()
                print("\n  Calibration interrupted.")
                continue
            if measured is None:
                print("  Not saved.")
                continue
            session.profile = wp.save_profile(session.key, measured)
            print("  Saved to %s -- it will be reused automatically from now on."
                  % wp.PROFILE_PATH)
            for line in wp.describe_profile(session.profile):
                print(line)
            continue
        if choice.endswith("s") and choice[:-1].isdigit():
            index = int(choice[:-1]) - 1
            if 0 <= index < len(wp.SOFTWARE_CONDITIONS):
                try:
                    wp.run_software_condition(
                        session, wp.SOFTWARE_CONDITIONS[index], session.profile,
                        session.magnitude,
                        15.0 if session.duration <= 0 else session.duration)
                except KeyboardInterrupt:
                    session.stop_all()
                continue
        if choice in ("w", "e", "x", "y", "z"):
            action = {"w": session.sweep_gain,
                      "e": session.sweep_magnitude,
                      "x": session.direction_test,
                      "y": lambda: session.synth_wave("sine"),
                      "z": lambda: session.synth_wave("square")}[choice]
            try:
                action()
            except KeyboardInterrupt:
                session.stop_all()
            continue
        if choice == "b":
            SHOW_BROKEN = not SHOW_BROKEN
            continue
        if choice == "c":
            # Cycles None -> A -> B -> C -> None. None is the measured per-effect mapping
            # and is what you want; the forced modes exist to re-run the sign sweep if a
            # firmware update changes the conventions again.
            if CONDITION_SIGN is None:
                CONDITION_SIGN = 0
            elif CONDITION_SIGN + 1 < len(CONDITION_SIGNS):
                CONDITION_SIGN += 1
            else:
                CONDITION_SIGN = None
            print("    conditions now use: %s"
                  % ("per-effect, as measured" if CONDITION_SIGN is None
                     else "ALL forced to %s" % CONDITION_SIGNS[CONDITION_SIGN][0]))
            continue
        if choice == "r":
            session.reset()
            continue
        if choice == "i":
            describe_motor(session.motor)
            continue
        if choice in ("g", "m", "d", "f"):
            prompt = {"g": "master gain 0.0-1.0", "m": "magnitude 0.0-1.0",
                      "d": "duration in seconds (0 = 15s)",
                      "f": "frequency in Hz (0.1-10)"}[choice]
            try:
                value = float(input("    %s: " % prompt).strip())
            except (ValueError, EOFError, KeyboardInterrupt):
                print("    unchanged.")
                continue
            if choice == "g":
                session.gain = max(0.0, min(1.0, value))
                session.apply_gain()
            elif choice == "m":
                session.magnitude = max(0.0, min(1.0, value))
            elif choice == "f":
                session.frequency = max(0.1, min(10.0, value))
            else:
                session.duration = max(0.0, min(120.0, value))
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(EFFECTS):
            try:
                session.run_effect(int(choice) - 1)
            except KeyboardInterrupt:
                session.stop_all()
            continue
        print("  Unrecognised choice.")


def parse_args():
    p = argparse.ArgumentParser(description="Windows.Gaming.Input force-feedback probe")
    # master_gain is latched at effect-load time and is awkward to use as a live control,
    # so leave it wide open and drive intensity with the effect magnitude, which scales
    # smoothly on this hardware.
    p.add_argument("--gain", type=float, default=1.0,
                   help="master gain 0.0-1.0, set before each load (default 1.0)")
    p.add_argument("--magnitude", type=float, default=0.30,
                   help="effect magnitude 0.0-1.0 -- the intensity control (default 0.30)")
    p.add_argument("--duration", type=float, default=6.0,
                   help="seconds per effect, 0 = hold until Enter (default 6)")
    p.add_argument("--wait", type=float, default=30.0, help="detection timeout (default 30)")
    p.add_argument("--list-only", action="store_true", help="report only, play nothing")
    p.add_argument("--no-log", action="store_true",
                   help="do not write a session log under logs/")
    return p.parse_args()


def main():
    args = parse_args()
    # Started before anything else so the log captures enumeration and any early failure.
    log_path = None if args.no_log else log.start()

    if init_apartment is not None:
        try:
            init_apartment()
        except TypeError:
            init_apartment(0)

    rule("Windows.Gaming.Input force-feedback probe")
    print("  Python : %s" % sys.version.split()[0])
    print("  Wheel must be in XBOX mode (PID 0x015C).")
    if log_path:
        print("  Log    : %s" % log_path)
    print()

    pump = PumpThread()
    pump.start()
    pump.ready.wait(timeout=5)
    if not pump.hwnd:
        print("  WARNING: no message-pump window; enumeration will probably stay empty.")
    else:
        print("  Message pump running (small window opened -- just leave it alone).")

    session = None
    try:
        raw, wheels = wait_for_devices(args.wait, pump)
        if not raw and not wheels:
            rule("RESULT")
            print("  Nothing enumerated. Make sure the wheel is in Xbox mode and press a")
            print("  button on it while this runs. Try --wait 60.")
            return 1

        motor, label, wheel, identity = report(raw, wheels)

        rule("RESULT")
        if motor is None:
            print("  No force-feedback motor exposed. That would be four standard APIs")
            print("  with no route to the motors.")
            return 1

        print("  FORCE-FEEDBACK MOTOR FOUND via %s" % label)
        print()
        describe_motor(motor)

        if args.list_only:
            print("\n  --list-only given; playing nothing.")
            return 0

        loop = asyncio.new_event_loop()
        session = Session(motor, loop, max(0.0, min(1.0, args.gain)),
                          max(0.0, min(1.0, args.magnitude)), args.duration, pump,
                          wheel=wheel, device_name=identity["name"], key=identity["key"])

        rule("Calibration profile  (%s)" % (identity["key"] or "unknown device"))
        for line in wp.describe_profile(session.profile):
            print(line)
        if session.read_wheel() is None:
            print("  NOTE: no wheel position reading -- calibration is unavailable.")

        try:
            enabled = session.sync(motor.try_enable_async())
            print("\n  try_enable_async -> %s" % enabled)
        except Exception as exc:
            print("\n  try_enable_async failed: %s" % exc)
        session.apply_gain()

        print()
        print("  NOTE: while an effect holds the motor, the wheel's own auto-centering is")
        print("  suspended -- so a gentle effect feels LOOSER than the resting wheel. That")
        print("  is what control feels like. Press 'r' any time to hand the motor back.")

        menu(session)
        return 0

    except KeyboardInterrupt:
        print("\n\n  Ctrl+C.")
        return 130
    finally:
        if session is not None:
            session.stop_all()
            try:
                session.sync(session.motor.try_reset_async())
            except Exception:
                pass
            session.loop.close()
        pump.stop()
        print("\n  Effects stopped, motor released.")
        if log.path():
            print("  Session log: %s" % log.path())
        log.stop()


if __name__ == "__main__":
    sys.exit(main())
