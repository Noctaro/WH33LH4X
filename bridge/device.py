"""The HORI wheel over raw USB: a reader thread for input, a writer thread for force."""

import threading
import time
from collections import namedtuple

import probe_log as log
from ffb_render import clamp

# Field names match WGI's RacingWheelReading, which the vJoy axis map reads.
Reading = namedtuple("Reading", "wheel throttle brake clutch handbrake buttons")


class RawUsbWheel(object):
    """
    Owns the wheel over GIP with no Microsoft driver and no foreground gate.

    Needs WinUSB on Windows, or xone unbound on Linux; see evidence/RAW_USB.md. The effect
    loads once at zero and every change after that is a bare parameter block, with no
    periodic reload, which is felt as a step.

    CONSTRAINT: the sign passes through unchanged, so a positive force moves the reading
    positive, as under WGI. Measured in DiRT 4, see evidence/RAW_USB.md.
    """

    name = "rawusb"

    CAP = 0.35              # raw 1.0 not yet compared with WGI's 1.0; evidence/RAW_USB.md
    HEARTBEAT = 1.0 / 16.0
    WRITE_HZ = 250.0        # the interrupt endpoint's 4 ms interval
    FORCE_STEP = 0.001      # smallest magnitude change worth sending
    MAX_FAILURES = 50

    # gip.report buttons onto WGI's RacingWheelButtons order; where A sits there is unmeasured.
    BUTTONS = (("TIP_DOWN", 1 << 0), ("TIP_UP", 1 << 1),
               ("DPAD_UP", 1 << 2), ("DPAD_DOWN", 1 << 3),
               ("DPAD_LEFT", 1 << 4), ("DPAD_RIGHT", 1 << 5),
               ("A", 1 << 6), ("B", 1 << 7), ("X", 1 << 8), ("Y", 1 << 9))

    # There is no handbrake lever, so that axis stays unmapped.
    CAPABILITIES = {"clutch": True, "handbrake": False, "pattern_shifter": False,
                    "max_wheel_angle": None}

    def __init__(self, max_force=1.0, gain=1.0):
        self.max_force = clamp(abs(max_force))
        self.gain = clamp(abs(gain))
        self.healthy = True
        self.last_force = None
        self.writes = 0
        self.failures = 0
        self.blocks = 0
        self.clipped = 0
        self.ticks = 0
        self.reports = 0
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._wheel = None
        self._threads = []
        self._running = False
        self._started = None
        self._demand = 0.0
        self._reading = None

    def open(self):
        """Claim, power on, arm, load at zero, start the threads. Raises RuntimeError."""
        try:
            from gip import arming, report, wire
            from gip.host import Wheel
        except ImportError as exc:
            raise RuntimeError("raw USB needs pyusb and libusb_package: %s" % exc)
        self._arming, self._report, self._wire = arming, report, wire

        wheel = Wheel(verbose=False)    # WheelNotFound is a RuntimeError
        try:
            bound = wheel.dev.is_kernel_driver_active(0)
        except Exception:
            bound = False
        if bound:
            raise RuntimeError("xone still holds the wheel; unbind it first "
                               "(evidence/RAW_USB.md)")
        try:
            wheel.open()
        except Exception as exc:
            raise RuntimeError("could not claim the wheel over USB (bound to WinUSB?): %s"
                               % exc)
        self._wheel = wheel
        try:
            wheel.power_on()
            time.sleep(0.3)
            for command, body, options, delay in arming.arming_sequence():
                wheel.send(command, body, options=options)
                if delay:
                    time.sleep(delay)
                self._handle(wheel.read(timeout=1))
            for command, body, options, delay in arming.force_sequence(0.0):
                wheel.send(command, body, options=options)
                if delay:
                    time.sleep(delay)
        except Exception as exc:
            self._release()
            raise RuntimeError("arming the wheel failed: %s" % exc)

        if self._reading is None:
            # A still wheel sends no reports, and power-on recentres it.
            self._reading = Reading(0.0, 0.0, 0.0, 0.0, 0.0, 0)
        self.last_force = 0.0
        self._running = True
        self._started = time.monotonic()
        self._threads = [threading.Thread(target=self._read_loop, name="rawusb-read",
                                          daemon=True),
                         threading.Thread(target=self._write_loop, name="rawusb-write",
                                          daemon=True)]
        for thread in self._threads:
            thread.start()
        log.event("sink.open", sink=self.name, gain=self.gain, max_force=self.max_force,
                  cap=self.CAP, write_hz=self.WRITE_HZ)
        return self

    def _handle(self, data):
        """Acknowledge what asks for it and decode input reports into a Reading."""
        if not data:
            return
        head = self._wire.decode_header(data)
        if not head:
            return
        if head["options"] & self._wire.OPT_ACKNOWLEDGE:
            with self._send_lock:
                self._wheel.acknowledge(head)
        if head["command"] != self._wire.GIP_CMD_INPUT:
            return
        self.reports += 1
        report = self._report.decode(head["payload"])
        if self.reports == 1 or report is None:
            return      # the first report of a session is junk; see evidence/RAW_USB.md
        buttons = 0
        for name, flag in self.BUTTONS:
            if name in report.buttons:
                buttons |= flag
        self._reading = Reading(clamp(report.steering), report.throttle, report.brake,
                                report.clutch, 0.0, buttons)

    def _read_loop(self):
        wheel = self._wheel
        while self._running:
            try:
                self._handle(wheel.read(timeout=100))
            except Exception:
                time.sleep(0.01)

    def _send(self, command, body, options=0x00):
        with self._send_lock:
            self._wheel.send(command, body, options=options)

    def _write_loop(self):
        arming = self._arming
        beat_command, beat_body = arming.heartbeat()
        period = 1.0 / self.WRITE_HZ
        tick = next_beat = time.monotonic()
        sent = 0.0
        failures = 0
        while self._running:
            try:
                now = time.monotonic()
                if now >= next_beat:
                    self._send(beat_command, beat_body)
                    next_beat = now + self.HEARTBEAT
                with self._lock:
                    demand = self._demand
                if abs(demand - sent) > self.FORCE_STEP:
                    self._send(arming.GIP_PARAM, arming.force_block(demand))
                    sent = demand
                    self.blocks += 1
                self.ticks += 1
                failures = 0
            except Exception as exc:
                failures += 1
                self.failures += 1
                if failures > self.MAX_FAILURES:
                    self.healthy = False
                    self._running = False
                    log.event("sink.unhealthy", failures=self.failures, error=repr(exc))
            # Absolute schedule, resynced when behind, so lateness is not repaid in a burst.
            tick += period
            delay = tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                tick = time.monotonic()

    @property
    def achieved_hz(self):
        if not self._started:
            return 0.0
        elapsed = time.monotonic() - self._started
        return self.ticks / elapsed if elapsed > 0 else 0.0

    def set_force(self, x):
        """Hand the writer a new force, -1.0 to +1.0. Never touches USB itself."""
        if not self._running:
            return False
        scaled = clamp(x, self.max_force) * self.gain
        value = clamp(scaled, self.CAP)
        with self._lock:
            if value != scaled:
                self.clipped += 1
            self._demand = value
            self.last_force = value
            self.writes += 1
        return True

    def keepalive(self):
        """The writer beats on its own; report whether it still runs."""
        return self._running

    def read(self):
        """The latest Reading, or None before the wheel is armed or after it failed."""
        return self._reading if self._running else None

    def describe(self):
        return ("raw USB (max force %.2f, gain %.2f, hard cap %.2f, writes at %.0f Hz)%s"
                % (self.max_force, self.gain, self.CAP, self.WRITE_HZ,
                   "" if self.healthy else ", UNHEALTHY"))

    def _release(self):
        wheel, self._wheel = self._wheel, None
        if wheel is None:
            return
        try:
            for command, body, options, delay in self._arming.force_sequence(0.0):
                wheel.send(command, body, options=options)
                if delay:
                    time.sleep(delay)
        except Exception:
            pass
        try:
            wheel.close()
        except Exception:
            pass

    def close(self):
        """Stop the threads, zero the motor, release the interface. Safe to call twice."""
        self._running = False
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []
        if self._wheel is None:
            return
        achieved = self.achieved_hz
        self._release()
        log.event("sink.close", sink=self.name, writes=self.writes, failures=self.failures,
                  loop_hz=round(achieved, 1), blocks=self.blocks, clipped=self.clipped)
