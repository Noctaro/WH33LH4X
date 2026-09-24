r"""
gip_arming.py -- Build the HORI wheel's GIP arming and force sequences from scratch.

WHY THIS EXISTS. Force feedback on this wheel was first achieved by replaying a USB wire
capture verbatim. That proves the protocol but is useless to anyone else: it needs someone's
pcap. This module generates the same bytes from rules, so the only captured artefact left is
186 bytes of effect table that the firmware wants uploaded before every force command.

Everything here was derived by diffing a real wire capture (USBPcap, wheel driven by
Windows.Gaming.Input) against what we sent. The capture our earlier work relied on,
`logs/wgi.txt`, was taken at the `\\.\XboxGIP` driver interface -- ABOVE the driver -- and the
driver reorders, dedupes and rewrites everything before it reaches the bus. That single fact
explains years of silent replays.

THE ARMING, as it crosses the wire:

    0a 000000                       |
    05 05                           | power
    0a 000000                       |
    0c CLEAR                        |
    26 x 0b zero-block              | zero parameter ids 0x00..0xff, ten per message
    0c CLEAR x2                     |
    0a 060000                       |
    26 x 0b zero-block              | the whole bank again
    0a 060000, every ~2 s, while heartbeating 0c CLEAR at 16 Hz
    4 x 0d table chunk              | the effect table, offsets 0x00/0x30/0x60/0x90

THE FORCE COMMAND is a SEQUENCE, not a message. Around every force block in the capture:

    0c CLEAR x2 | 0d table x4 | 0b magnitude | 0c 0x10 | 0c 0xf0 running...

Sending the 0b alone drops a magnitude into a cleared device and does nothing at all.
"""

import struct

GIP_POWER = 0x05

# The options byte matters: the capture marks POWER as INTERNAL (0x20) and leaves
# everything else at 0x00. Emitting 0x00 for POWER left the device in a state where it
# never streamed input and never moved.
OPT_NONE = 0x00
OPT_INTERNAL = 0x20
GIP_LED = 0x0A          # xone calls it LED; here it punctuates the arming
GIP_PARAM = 0x0B        # ten (u16 id, 4-byte value) slots, 60-byte body
GIP_STATE = 0x0C        # 9-byte body, byte 0 is the state
GIP_TABLE = 0x0D        # effect table upload, 53-byte body

STATE_CLEAR = 0x00
STATE_LOAD = 0x10
STATE_RUNNING = 0xF0

PARAM_UNUSED = 0xFFFF   # a slot the device ignores
SLOTS_PER_MESSAGE = 10

# The effect table, uploaded before every force command. 186 bytes across four chunks at
# offsets 0x00/0x30/0x60/0x90; the 4-byte prologue is total size (0x00ba) then the offset.
# This is firmware data we do not understand and do not need to: it is byte-identical every
# time the Windows driver sends it.
TABLE_CHUNKS = (
    "00ba000000a00050034a01d04b40d04b241124402411241400244424210621a0242040140100220222011420140120a00021421401",
    "00ba0030000222022201a0012103140201a00121032110041324140123d0140222412322210921a002214514032511140114032211",
    "00ba006000202223452322302522251323140323111404140522122322231223252310140523264514062511140620224522233025",
    "00ba009000232513221406231114071405221223222312232523102623221221222112140821221214092221a02153000000000000",
)

# Slot ids inside a force block. 0x08 is the signed magnitude -- proven by an A/B where the
# same 555 packets with 0x08 at +/-1.0 drove the wheel and at 0.0 did nothing.
FORCE_MAGNITUDE = 0x0008

# Raw slot values, copied from the wire rather than re-derived: the duration is 0x46ea6001,
# which is 30000.002 and not the 30000.0 a float literal would produce. One byte, but there is
# no reason to introduce a difference we cannot justify.
FORCE_TEMPLATE = (
    (0x0000, "00000000"),
    (0x0001, "0160ea46"),   # duration, 30000.002
    (0x0002, "01000000"),   # integer 1
    (0x0003, "000080bf"),   # direction vector
    (0x0004, "00000000"),
    (0x0005, "0000803f"),
    (0x0006, "000080bf"),
    (0x0007, "00000000"),
    (0x0008, None),         # magnitude, filled in by force_block()
    (0x0009, "0000803f"),   # gain
)


def param_block(pairs):
    """One 0x0b body: ten (id, value) slots, padded with PARAM_UNUSED."""
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
    """A complete ten-slot force block. Only slot 0x08 varies."""
    pairs = []
    for pid, value in FORCE_TEMPLATE:
        raw = struct.pack("<f", magnitude) if value is None else bytes.fromhex(value)
        pairs.append((pid, raw))
    return param_block(pairs)


def state(value):
    """A 0x0c body: the state byte, then eight zeros."""
    return bytes([value]) + b"\x00" * 8


def arming_sequence(beat_ms=31.0):
    """The full arming as (command, body, options, delay_seconds) tuples.

    The run structure is taken exactly from the wire, including the CLEAR heartbeat counts.
    Approximating those (emitting 67 beats where the capture has 88) produced a device that
    armed silently: no input stream, no torque. The counts are not decoration.

        0a 000000 | POWER 05 | 0a 000000 | CLEAR x1
        26 x zero-block | CLEAR x2 | 0a 060000
        26 x zero-block | CLEAR x26 | 0a 060000 | CLEAR x30 | 0a 060000 | CLEAR x29
        4 x table chunk
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
    """A force command: clear, re-upload the table, set the magnitude, start it.

    The table really is re-uploaded every time. The capture holds 0x0d x24 for six force
    events, which is four chunks each.
    """
    out = [(GIP_STATE, state(STATE_CLEAR), OPT_NONE, 0.004)] * 2
    for chunk in TABLE_CHUNKS:
        out.append((GIP_TABLE, bytes.fromhex(chunk), OPT_NONE, 0.004))
    out.append((GIP_PARAM, force_block(magnitude), OPT_NONE, 0.008))
    out.append((GIP_STATE, state(STATE_LOAD), OPT_NONE, 0.004))
    return out


def heartbeat():
    """What to repeat at 16 Hz between force commands."""
    return (GIP_STATE, state(STATE_RUNNING))
