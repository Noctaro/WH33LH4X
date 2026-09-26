"""The bridge loop: wheel readings go to the front-end, game force comes back to the wheel."""

import os
import time

import ffb_render as render
import probe_log as log
from live_tune import ButtonTuner

TICK_SECONDS = 0.5      # between console lines and bridge.tick events
STOP_POLL = 0.1         # between checks for the stop file
MOVING = 0.01           # force and position below this stay out of the direction count


class NullFrontend(object):
    """No virtual device and no game: the wheel runs on the tune file alone."""

    name = "none"
    decoder = None

    def open(self, caps, ffb=True):
        return self

    def feed(self, reading):
        return [("steering", reading.wheel, None)]

    def describe(self):
        return ["no virtual device: force comes from the tune file only"]

    def close(self):
        pass


class Bridge(object):
    """
    Each tick reads the wheel, feeds the front-end, drains game effects and commands force.

    CONSTRAINT: the stop file is polled here, not by a launcher, so every stop path zeroes the
    motor before the wheel is released.
    """

    def __init__(self, device, frontend, tune, rate=250.0, ffb=True, stop_file=None,
                 trace=None, clock=time.monotonic, sleep=time.sleep):
        self.device = device
        self.frontend = frontend
        self.tune = tune
        self.rate = float(rate)
        self.ffb = ffb
        self.stop_file = stop_file
        self.trace = trace
        self.clock = clock
        self.sleep = sleep
        self.tuner = ButtonTuner(tune)
        self.state = render.WheelState()
        self.ticks = 0
        self.game = 0.0
        self.force = 0.0
        self.counted = 0
        self.opposed = 0
        self.at_max = 0
        self.stopped_by = None
        self._reading = None
        self._buttons = 0
        self._written = []
        self._started = None
        self._next_print = 0.0
        self._next_stop_poll = 0.0

    @property
    def achieved_hz(self):
        if self._started is None:
            return 0.0
        elapsed = self.clock() - self._started
        return self.ticks / elapsed if elapsed > 0 else 0.0

    def tick(self, now):
        reading = self.device.read()
        if reading is None:
            return
        fresh = reading is not self._reading
        self._reading = reading
        self.ticks += 1
        self._written = self.frontend.feed(reading)
        self.state.update(reading.wheel, now)
        self.tune.centring_law.observe(reading.wheel, now, fresh)
        self._buttons_changed(reading.buttons)

        if self.ffb:
            decoder = self.frontend.decoder
            if decoder is not None:
                # Packets first, so the force reflects the game's latest request.
                decoder.drain()
                self.game = decoder.mixer.force(now, self.state)
            changed = self.tune.poll(now)
            render.DIRECTION_MODE = self.tune["dir_mode"]
            if changed:
                print("    tune: %s" % self.tune.summary(), flush=True)
                log.event("tune.changed", keys=",".join(changed), **self.tune.values)
            self.force = self.tune.apply(self.game, self.state)
            self.device.set_force(self.force)
            self._count(self.force, self.state.position)

        if self.trace is not None:
            self.trace.append((now - self._started, self.state.position, self.force))
        if now >= self._next_print:
            self._print_tick()
            self._next_print = now + TICK_SECONDS

    def _buttons_changed(self, buttons):
        if buttons != self._buttons:
            bits = [str(i + 1) for i in range(32) if buttons & (1 << i)]
            print("    buttons 0x%08X  bits %s" % (buttons, ",".join(bits) or "-"), flush=True)
            log.event("wheel.buttons", raw=buttons, bits=",".join(bits))
            self._buttons = buttons
        note = self.tuner.update(buttons)
        if note:
            print("    tune: %s   [%s]" % (note, self.tune.summary()), flush=True)
            log.event("tune.button", change=note, **self.tune.values)

    def _count(self, force, position):
        if abs(force) >= MOVING and abs(position) >= MOVING:
            self.counted += 1
            if force * position < 0:
                self.opposed += 1
        ceiling = self.tune["max_force"]
        if force and abs(force) >= ceiling - 1e-9:
            self.at_max += 1

    def _print_tick(self):
        decoder = self.frontend.decoder
        device = self.device
        parts = " ".join("%s %+.2f" % (label, raw) for label, raw, _v in self._written)
        line = "    %-52s %5.1f Hz" % (parts, self.achieved_hz)
        if self.ffb:
            line += "  game %+.2f -> out %+.2f" % (self.game, device.last_force or 0.0)
        if decoder is not None:
            line += "  fx=%d pkt=%d" % (len(decoder.mixer.running_effects()), decoder.received)
            if decoder.dropped:
                line += " DROPPED=%d" % decoder.dropped
        print(line, flush=True)
        # game and pos together are what tune_report.py measures centring from.
        log.event("bridge.tick", hz=round(self.achieved_hz, 1),
                  writes=getattr(device, "writes", 0), failures=getattr(device, "failures", 0),
                  force=(device.last_force or 0.0) if self.ffb else 0.0,
                  game=self.game, pos=self.state.position, vel=self.state.velocity,
                  dirx=decoder.last_dir if decoder else 0,
                  dirmul=decoder.last_dir_x if decoder else 0.0,
                  effects=len(decoder.mixer.running_effects()) if decoder else 0,
                  **{label: raw for label, raw, _v in self._written})

    def stop_requested(self, now):
        if self.stop_file is None or now < self._next_stop_poll:
            return False
        self._next_stop_poll = now + STOP_POLL
        if not os.path.exists(self.stop_file):
            return False
        try:
            os.remove(self.stop_file)
        except OSError:
            pass
        return True

    def run(self, seconds=0.0):
        """Tick at the set rate until the time is up or the stop file appears."""
        period = 1.0 / self.rate
        self._started = start = next_tick = self.clock()
        while True:
            now = self.clock()
            if seconds > 0 and now - start >= seconds:
                self.stopped_by = "time"
                return
            if self.stop_requested(now):
                self.stopped_by = "stop file"
                print("  stop requested", flush=True)
                return
            self.tick(now)
            # Absolute schedule, resynced when behind, so lateness is not repaid in a burst.
            next_tick += period
            delay = next_tick - self.clock()
            if delay > 0:
                self.sleep(delay)
            else:
                next_tick = self.clock()

    def summary(self):
        device = self.device
        return {"ticks": self.ticks, "hz": round(self.achieved_hz, 1),
                "opposed": self.opposed, "counted": self.counted, "at_max": self.at_max,
                "clipped": getattr(device, "clipped", 0),
                "blocks": getattr(device, "blocks", 0),
                "failures": getattr(device, "failures", 0),
                "write_hz": round(getattr(device, "achieved_hz", 0.0), 1),
                "stopped_by": self.stopped_by or "interrupt"}

    def close(self):
        """Zero the motor first, then release the wheel and the front-end."""
        if self.ffb:
            try:
                self.device.set_force(0.0)
            except Exception:
                pass
        summary = self.summary()
        try:
            self.device.close()
        finally:
            self.frontend.close()
        print("  summary: force opposed position in %d of %d moving ticks, %.1f Hz loop, "
              "%.1f Hz writes, %d blocks, %d failures, %d at max_force, %d at the cap"
              % (summary["opposed"], summary["counted"], summary["hz"], summary["write_hz"],
                 summary["blocks"], summary["failures"], summary["at_max"],
                 summary["clipped"]))
        log.event("bridge.summary", **summary)
        return summary
