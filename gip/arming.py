"""
The arming and force sequences, generated from rules. See evidence/RAW_USB.md.

Verified 150 of 150 packets byte-identical to a USBPcap wire capture of WGI driving the wheel.
A force command is a sequence, not a message: a bare magnitude into a cleared device does
nothing.
"""

import struct

# POWER must carry OPT_INTERNAL: with 0x00 the wheel never streamed input (RAW_USB.md).
OPT_NONE = 0x00
OPT_INTERNAL = 0x20

GIP_POWER = 0x05
GIP_LED = 0x0A          # punctuates the arming; xone's name for the command
GIP_PARAM = 0x0B        # ten (u16 id, 4-byte value) slots, 60-byte body
GIP_STATE = 0x0C        # 9-byte body, byte 0 is the state
GIP_TABLE = 0x0D        # effect table upload, 53-byte body

STATE_CLEAR = 0x00
STATE_LOAD = 0x10
STATE_RUNNING = 0xF0

PARAM_UNUSED = 0xFFFF   # a slot the device ignores
SLOTS_PER_MESSAGE = 10

# Opaque firmware data, byte-identical in every capture: 186 bytes in four chunks at offsets
# 0x00/0x30/0x60/0x90, each prefixed with the total size (0x00ba) and its offset.
TABLE_CHUNKS = (
    "00ba000000a00050034a01d04b40d04b241124402411241400"
    "244424210621a0242040140100220222011420140120a00021421401",
    "00ba0030000222022201a0012103140201a00121032110041324"
    "140123d0140222412322210921a002214514032511140114032211",
    "00ba00600020222345232230252225132314032311140414052212"
    "2322231223252310140523264514062511140620224522233025",
    "00ba009000232513221406231114071405221223222312232523"
    "102623221221222112140821221214092221a02153000000000000",
)

# Slot 0x08 is the signed magnitude: an A/B of the same 555 packets at +/-1.0 and 0.0.
FORCE_MAGNITUDE = 0x0008

# Raw values copied from the wire; the duration is 30000.002, not the float literal 30000.0.
FORCE_TEMPLATE = (
    (0x0000, "00000000"),
    (0x0001, "0160ea46"),   # duration
    (0x0002, "01000000"),   # integer 1
    (0x0003, "000080bf"),   # direction vector
    (0x0004, "00000000"),
    (0x0005, "0000803f"),
    (0x0006, "000080bf"),
    (0x0007, "00000000"),
    (FORCE_MAGNITUDE, None),
    (0x0009, "0000803f"),   # gain
)


def param_block(pairs):
    """One 0x0b body: up to ten (id, value) slots, padded with PARAM_UNUSED."""
    body = bytearray()
    for pid, raw in pairs[:SLOTS_PER_MESSAGE]:
        body += struct.pack("<H", pid) + raw
    while len(body) < SLOTS_PER_MESSAGE * 6:
        body += struct.pack("<H", PARAM_UNUSED) + b"\x00\x00\x00\x00"
    return bytes(body)


def zero_blocks():
    """The 26 messages that zero parameter ids 0x00..0xff, ten at a time."""
    out = []
    for base in range(0, 256, SLOTS_PER_MESSAGE):
        pairs = [(pid, b"\x00\x00\x00\x00")
                 for pid in range(base, min(base + SLOTS_PER_MESSAGE, 256))]
        out.append(param_block(pairs))
    return out


def force_block(magnitude):
    """A complete force block; only the magnitude slot varies."""
    pairs = []
    for pid, value in FORCE_TEMPLATE:
        raw = struct.pack("<f", magnitude) if value is None else bytes.fromhex(value)
        pairs.append((pid, raw))
    return param_block(pairs)


def state(value):
    """A 0x0c body: the state byte, then eight zeros."""
    return bytes([value]) + b"\x00" * 8


def arming_sequence(beat_ms=31.0):
    """
    The full arming as (command, body, options, delay_seconds) tuples, as on the wire:

        0a 000000 | POWER 05 | 0a 000000 | CLEAR x1
        26 x zero-block | CLEAR x2 | 0a 060000
        26 x zero-block | CLEAR x26 | 0a 060000 | CLEAR x30 | 0a 060000 | CLEAR x29
        4 x table chunk

    CONSTRAINT: the CLEAR counts are load-bearing; 67 beats instead of 88 armed silently.
    """
    beat = beat_ms / 1000.0
    out = [(GIP_LED, b"\x00\x00\x00", OPT_NONE, 0.004),
           (GIP_POWER, b"\x05", OPT_INTERNAL, 0.004),
           (GIP_LED, b"\x00\x00\x00", OPT_NONE, 0.004)]
    out += [(GIP_STATE, state(STATE_CLEAR), OPT_NONE, beat)]

    for body in zero_blocks():
        out.append((GIP_PARAM, body, OPT_NONE, 0.008))
    out += [(GIP_STATE, state(STATE_CLEAR), OPT_NONE, beat)] * 2
    out.append((GIP_LED, b"\x06\x00\x00", OPT_NONE, 0.004))

    for body in zero_blocks():
        out.append((GIP_PARAM, body, OPT_NONE, 0.008))

    for count in (26, 30, 29):
        out += [(GIP_STATE, state(STATE_CLEAR), OPT_NONE, beat)] * count
        if count != 29:
            out.append((GIP_LED, b"\x06\x00\x00", OPT_NONE, 0.004))

    for chunk in TABLE_CHUNKS:
        out.append((GIP_TABLE, bytes.fromhex(chunk), OPT_NONE, 0.004))
    return out


def force_sequence(magnitude):
    """Load an effect: clear, upload the table, set the magnitude, start it."""
    out = [(GIP_STATE, state(STATE_CLEAR), OPT_NONE, 0.004)] * 2
    for chunk in TABLE_CHUNKS:
        out.append((GIP_TABLE, bytes.fromhex(chunk), OPT_NONE, 0.004))
    out.append((GIP_PARAM, force_block(magnitude), OPT_NONE, 0.008))
    out.append((GIP_STATE, state(STATE_LOAD), OPT_NONE, 0.004))
    return out


def heartbeat():
    """What to repeat at 16 Hz between force commands."""
    return (GIP_STATE, state(STATE_RUNNING))
