"""
test_bridge_core.py: the bridge loop against a fake wheel and a fake game, no hardware.

    python test_bridge_core.py
"""

import os
import sys
import tempfile

from bridge.core import Bridge, NullFrontend
from bridge.device import Reading
from live_tune import LiveTune


class FakeClock(object):
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeWheel(object):
    """Reports a scripted position and records every force."""

    def __init__(self, position=0.0):
        self.reading = Reading(position, 0.0, 0.0, 0.0, 0.0, 0)
        self.forces = []
        self.last_force = None
        self.closed = False

    def read(self):
        return self.reading

    def set_force(self, x):
        self.forces.append(x)
        self.last_force = x
        return True

    def close(self):
        self.closed = True


class FakeMixer(object):
    def __init__(self, force):
        self._force = force

    def force(self, now, state):
        return self._force

    def running_effects(self):
        return []


class FakeDecoder(object):
    def __init__(self, force):
        self.mixer = FakeMixer(force)
        self.drained = 0
        self.received = self.dropped = self.last_dir = 0
        self.last_dir_x = 0.0

    def drain(self):
        self.drained += 1


class FakeGame(NullFrontend):
    name = "fake"

    def __init__(self, force):
        self.decoder = FakeDecoder(force)
        self.fed = 0
        self.closed = False

    def feed(self, reading):
        self.fed += 1
        return super().feed(reading)

    def close(self):
        self.closed = True


def check(name, condition, detail=""):
    print("  %s %s%s" % ("PASS" if condition else "FAIL", name,
                         "" if condition else "   <-- " + detail))
    return bool(condition)


def make(wheel, frontend, ffb=True, stop_file=None, **values):
    tune = LiveTune(os.path.join(tempfile.gettempdir(), "no-such-tune.json"))
    tune.values.update(values)
    clock = FakeClock()
    bridge = Bridge(wheel, frontend, tune, rate=250.0, ffb=ffb, stop_file=stop_file,
                    clock=clock, sleep=clock.sleep)
    return bridge, clock


def test_spring_opposes_position():
    print("\nspring alone, no game")
    wheel = FakeWheel(0.5)
    bridge, _clock = make(wheel, NullFrontend(), spring=0.3, max_force=1.0)
    bridge.run(1.0)
    summary = bridge.close()
    ok = check("ran about 250 ticks (%d)" % summary["ticks"], 249 <= summary["ticks"] <= 251)
    ok &= check("force is -position x spring (%.3f)" % wheel.forces[-2],
                abs(wheel.forces[-2] + 0.15) < 1e-9)
    ok &= check("summary counts every tick as opposed",
                summary["opposed"] == summary["counted"] == summary["ticks"])
    ok &= check("the last force is zero, then the wheel closes",
                wheel.forces[-1] == 0.0 and wheel.closed)
    return ok


def test_game_force():
    print("\ngame force through the decoder")
    wheel = FakeWheel(0.0)
    game = FakeGame(0.4)
    bridge, _clock = make(wheel, game, strength=0.5, invert=True, max_force=1.0)
    bridge.run(0.1)
    bridge.close()
    ok = check("decoder drained every tick", game.decoder.drained == bridge.ticks)
    ok &= check("strength and invert apply (%.3f)" % wheel.forces[0],
                abs(wheel.forces[0] + 0.2) < 1e-9)
    ok &= check("front-end fed and closed", game.fed == bridge.ticks and game.closed)
    return ok


def test_max_force_counted():
    print("\nmax_force ceiling")
    wheel = FakeWheel(0.0)
    bridge, _clock = make(wheel, FakeGame(0.9), max_force=0.45)
    bridge.run(0.1)
    summary = bridge.close()
    ok = check("force clamped to 0.45", abs(wheel.forces[0] - 0.45) < 1e-9)
    ok &= check("every tick counted at max_force", summary["at_max"] == summary["ticks"])
    return ok


def test_no_ffb():
    print("\ninput only")
    wheel = FakeWheel(0.5)
    game = FakeGame(0.9)
    bridge, _clock = make(wheel, game, ffb=False, spring=0.3)
    bridge.run(0.1)
    bridge.close()
    ok = check("no force is ever commanded", wheel.forces == [])
    ok &= check("input is still fed", game.fed == bridge.ticks > 0)
    return ok


def test_stop_file():
    print("\nstop file")
    path = os.path.join(tempfile.gettempdir(), "wh33lh4x-test.stop")
    with open(path, "w"):
        pass
    wheel = FakeWheel(0.2)
    bridge, clock = make(wheel, NullFrontend(), stop_file=path, spring=0.3)
    bridge.run(10.0)
    summary = bridge.close()
    ok = check("stopped by the file within a poll (%.2f s)" % clock.now, clock.now < 0.2)
    ok &= check("summary names the stop file", summary["stopped_by"] == "stop file")
    ok &= check("the file is consumed", not os.path.exists(path))
    ok &= check("motor zeroed", wheel.forces[-1] == 0.0)
    return ok


def test_tuned_centring():
    print("\ntuned centring through the loop")
    wheel = FakeWheel(0.4)
    bridge, _clock = make(wheel, NullFrontend(), centring="tuned", spring=0.25, damper=0.08,
                          max_force=1.0)
    bridge.run(0.5)
    bridge.close()
    return check("pulls back with -position x spring once still (%.4f)" % wheel.forces[-2],
                 abs(wheel.forces[-2] + 0.1) < 1e-3)


def main():
    print("bridge core checks")
    results = [test_spring_opposes_position(), test_game_force(), test_max_force_counted(),
               test_no_ffb(), test_stop_file(), test_tuned_centring()]
    print()
    if all(results):
        print("ALL CHECKS PASSED")
        return 0
    print(">>> FAILURES ABOVE.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
