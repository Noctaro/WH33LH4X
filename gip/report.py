"""The wheel's input report (GIP 0x20), as mapped by evidence/input_map.py on 2026-09-23."""

import struct
from collections import namedtuple

CENTRE = 0x8000

# (byte, bit) per control. The wheel has no handbrake; left face reports as A and right face
# as TIP_DOWN, with no bits of their own.
BUTTONS = {
    "A": (0, 0x10), "B": (0, 0x20), "X": (0, 0x40), "Y": (0, 0x80),
    "DPAD_UP": (1, 0x01), "DPAD_DOWN": (1, 0x02),
    "DPAD_LEFT": (1, 0x04), "DPAD_RIGHT": (1, 0x08),
    "TIP_UP": (1, 0x10), "TIP_DOWN": (1, 0x20),
}

Report = namedtuple("Report", "steering throttle brake clutch buttons")


def steering(payload):
    """Raw steering, u16 LE at bytes 2-3 with centre 0x8000; None if the payload is short."""
    if len(payload) < 4:
        return None
    return struct.unpack_from("<H", payload, 2)[0]


def decode(payload):
    """
    A Report: steering -1..1 (positive right, locks at about +/-180 degrees), pedals 0..1,
    and the names of the pressed buttons. None if the payload is short.
    """
    if len(payload) < 10:
        return None
    steer, throttle, brake, clutch = struct.unpack_from("<HHHH", payload, 2)
    buttons = frozenset(name for name, (byte, bit) in BUTTONS.items() if payload[byte] & bit)
    return Report((steer - CENTRE) / 32768.0, throttle / 65535.0, brake / 65535.0,
                  clutch / 65535.0, buttons)
