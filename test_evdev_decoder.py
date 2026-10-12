"""
test_evdev_decoder.py: evdev force effects into ffb_render, no hardware and no evdev needed.

    python test_evdev_decoder.py
"""

import sys

import ffb_render as render
from bridge.uinput import (
    FF_CONSTANT, FF_DAMPER, FF_PERIODIC, FF_RAMP, FF_SINE, FF_SPRING,
    EvdevEffectDecoder, direction_x, ioctl_number,
)

LEFT, RIGHT = 0x4000, 0xC000
NO_ENVELOPE = {"attack_length": 0, "attack_level": 0, "fade_length": 0, "fade_level": 0}


class FakeClock(object):
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def check(name, condition, detail=""):
    print("  %s %s%s" % ("PASS" if condition else "FAIL", name,
                         "" if condition else "   <-- " + detail))
    return bool(condition)


def decoder():
    clock = FakeClock()
    return EvdevEffectDecoder(render.EffectMixer(), clock=clock), clock


def constant(effect_id=0, level=16384, direction=LEFT, length=0, envelope=NO_ENVELOPE):
    return {"type": FF_CONSTANT, "id": effect_id, "direction": direction, "length": length,
            "delay": 0, "level": level, "envelope": envelope}


def condition(kind, coeff=32767, saturation=65535, deadband=0, center=0):
    block = {"right_saturation": saturation, "left_saturation": saturation,
             "right_coeff": coeff, "left_coeff": coeff, "deadband": deadband,
             "center": center}
    return {"type": kind, "id": 1, "direction": 0, "length": 0, "delay": 0,
            "conditions": [block, dict(block)]}


def force(dec, clock, position=0.0, velocity=0.0):
    dec.drain()
    state = render.WheelState()
    state.position, state.velocity = position, velocity
    return dec.mixer.force(clock.now, state)


def test_constant_direction():
    print("\nconstant force and direction")
    dec, clock = decoder()
    dec.post("upload", constant(level=16384, direction=LEFT))
    dec.post("play", (0, 1))
    left = force(dec, clock)
    dec.post("upload", constant(level=16384, direction=RIGHT))
    right = force(dec, clock)
    ok = check("0x4000 pushes the reading negative (%.3f)" % left, abs(left + 0.5) < 1e-4)
    ok &= check("0xC000 pushes it positive (%.3f)" % right, abs(right - 0.5) < 1e-4)
    ok &= check("an update keeps the effect playing", dec.mixer.effects[0].running)
    ok &= check("direction_x(0x4000) is -1", abs(direction_x(LEFT) + 1.0) < 1e-12)
    return ok


def test_play_stop_erase():
    print("\nplay, stop and erase")
    dec, clock = decoder()
    dec.post("upload", constant())
    ok = check("uploaded but not played gives no force", force(dec, clock) == 0.0)
    dec.post("play", (0, 1))
    ok &= check("played gives force", force(dec, clock) != 0.0)
    dec.post("play", (0, 0))
    ok &= check("count 0 stops it", force(dec, clock) == 0.0)
    dec.post("erase", 0)
    dec.post("play", (0, 1))
    ok &= check("erase forgets it", force(dec, clock) == 0.0 and 0 not in dec.mixer.effects)
    ok &= check("play of an erased id is counted", dec.unknown == 1)
    return ok


def test_length():
    print("\nreplay length")
    dec, clock = decoder()
    dec.post("upload", constant(length=100))
    dec.post("play", (0, 1))
    ok = check("plays within its length", force(dec, clock) != 0.0)
    clock.now = 0.2
    ok &= check("stops after 100 ms", force(dec, clock) == 0.0)
    dec, clock = decoder()
    dec.post("upload", constant(length=0))
    dec.post("play", (0, 1))
    clock.now = 3600.0
    ok &= check("length 0 plays forever", force(dec, clock) != 0.0)
    return ok


def test_gain():
    print("\ndevice gain")
    dec, clock = decoder()
    dec.post("upload", constant(level=32767))
    dec.post("play", (0, 1))
    dec.post("gain", 0x8000)
    value = force(dec, clock)
    return check("0x8000 halves the force (%.3f)" % value, abs(value + 0.5) < 1e-3)


def test_conditions():
    print("\nconditions")
    dec, clock = decoder()
    dec.post("upload", condition(FF_SPRING))
    dec.post("play", (1, 1))
    spring = force(dec, clock, position=0.5)
    ok = check("full spring resists position (%.3f)" % spring, abs(spring + 0.5) < 1e-4)
    dec.post("upload", condition(FF_SPRING, saturation=0x8000))
    capped = force(dec, clock, position=0.9)
    ok &= check("saturation 0x8000 caps at half (%.3f)" % capped, abs(capped + 0.5) < 1e-3)
    dec.post("upload", condition(FF_SPRING, deadband=0x8000))
    ok &= check("a half-width dead band covers +-0.5",
                force(dec, clock, position=0.49) == 0.0)
    dec, clock = decoder()
    dec.post("upload", condition(FF_DAMPER))
    dec.post("play", (1, 1))
    damper = force(dec, clock, velocity=-0.4)
    ok &= check("damper resists velocity (%.3f)" % damper, abs(damper - 0.4) < 1e-4)
    return ok


def test_periodic_and_envelope():
    print("\nperiodic and envelope")
    dec, clock = decoder()
    dec.post("upload", {"type": FF_PERIODIC, "id": 2, "direction": RIGHT, "length": 0,
                        "delay": 0, "waveform": FF_SINE, "period": 100, "magnitude": 32767,
                        "offset": 0, "phase": 0x4000, "envelope": NO_ENVELOPE})
    dec.post("play", (2, 1))
    peak = force(dec, clock)
    ok = check("phase 0x4000 starts a sine at its peak (%.3f)" % peak, abs(peak - 1.0) < 1e-3)
    dec, clock = decoder()
    envelope = dict(NO_ENVELOPE, attack_length=1000, attack_level=0)
    dec.post("upload", constant(level=32767, direction=RIGHT, envelope=envelope))
    dec.post("play", (0, 1))
    clock.now = 0.5
    half = force(dec, clock)
    ok &= check("attack from 0 is half way at 0.5 s (%.3f)" % half, abs(half - 0.5) < 1e-3)
    dec, clock = decoder()
    dec.post("upload", {"type": FF_RAMP, "id": 3, "direction": RIGHT, "length": 1000,
                        "delay": 0, "start": 0, "end": 32767, "envelope": NO_ENVELOPE})
    dec.post("play", (3, 1))
    clock.now = 0.25
    ramp = force(dec, clock)
    ok &= check("ramp is a quarter up at 0.25 s (%.3f)" % ramp, abs(ramp - 0.25) < 1e-3)
    return ok


def test_unsupported():
    print("\nunsupported effects")
    dec, clock = decoder()
    dec.post("upload", {"type": FF_PERIODIC, "id": 4, "direction": LEFT, "length": 0,
                        "delay": 0, "waveform": 0x5D, "period": 100, "magnitude": 1,
                        "offset": 0, "phase": 0, "envelope": NO_ENVELOPE})
    force(dec, clock)
    return check("a custom waveform is counted, not played", dec.unknown == 1)


def test_ioctl_numbers():
    print("\nuinput ioctl numbers")
    # linux/uinput.h on x86_64: struct uinput_ff_upload is 104 bytes, uinput_ff_erase 12.
    return check("match the kernel's",
                 (ioctl_number(3, 200, 104), ioctl_number(1, 201, 104),
                  ioctl_number(3, 202, 12), ioctl_number(1, 203, 12))
                 == (0xC06855C8, 0x406855C9, 0xC00C55CA, 0x400C55CB))


def main():
    print("evdev decoder checks")
    results = [test_constant_direction(), test_play_stop_erase(), test_length(), test_gain(),
               test_conditions(), test_periodic_and_envelope(), test_unsupported(),
               test_ioctl_numbers()]
    print()
    if all(results):
        print("ALL CHECKS PASSED")
        return 0
    print(">>> FAILURES ABOVE.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
