"""
The GIP wire format for this wheel: message framing, the parameter bank, and the captured
constant-force effect.

Everything here was CAPTURED from `Windows.Gaming.Input.dll` by `gip_trace.py` while it
commanded known magnitudes, not read from a specification. There is no public documentation
for this wheel's force payload -- the GIP-on-Windows writeup publishes the framing and the
gamepad rumble struct, and stops there.

This module is deliberately free of Windows API calls so both sides can use it: `gip_trace.py`
decodes what WGI emitted, `gip_direct.py` encodes the same bytes back. Keeping one definition
of the format means a correction lands in both at once.

What is VERIFIED and what is INFERRED is marked per item. The distinction matters: the force
parameter round-tripped across five magnitudes, while most of the init bank was observed
exactly once and is replayed because it worked, not because it is understood.
"""

import struct

# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

# 0..7   device id (8 bytes, assigned by the driver)
# 8      message type (u8)
# 9      flags (u8) -- 0x20 on driver announcements, 0 on everything we send
# 10..11 sequence (u16)
# 12..15 payload length (u32)
# 16..19 reserved (u32, 0 in every capture)
#
# Bytes 8..11 are NOT one u32 type. Reading them that way works for writes, where flags and
# sequence are both zero, and produces nonsense for reads: a run reported types 0x290025,
# 0x2a0025, 0x2b0025... which is a sequence counter incrementing in the high half, not six
# different message types. Whenever a "type" field looks like it is counting, it is not a type.
GIP_HEADER = struct.Struct("<8sBBHII")

GIP_TYPES = {
    0x02: "device announce",
    0x03: "status",
    0x04: "descriptor",
    0x0A: "short cmd",
    0x0B: "parameter block",
    0x0C: "state",
    0x0D: "bulk/table",
    0x20: "input report",
    0x21: "input report",
    0x25: "input report",
    0xE0: "SYSTEM FOCUS CHANGE",
}

# Parameter ids inside a 0x0b block. Only 0x0008 is established: it tracked the commanded
# magnitude exactly across +0.00 / +0.25 / +0.50 / +1.00 / -0.50. The rest are named only
# where the value is unambiguous, and left blank rather than guessed.
GIP_PARAMS = {
    0x0008: "X force (float32, -1..+1)",
    0xFFFF: "padding",
}

# This wheel in Xbox mode. Read back from the type 0x02 announce, never hard-coded as a
# device id -- see `announce_ids`.
HORI_VID = 0x0F0D
HORI_PID = 0x015C

PARAM_X_FORCE = 0x0008

# A 0x0b payload is always 60 bytes: ten (u16 id, 4-byte value) slots, unused ones filled with
# id 0xffff and a zero value. WGI pads even when it has one real parameter to send, and the
# length field says 60 either way, so the size is fixed rather than computed.
PARAM_SLOT = struct.Struct("<H4s")
PARAMS_PER_BLOCK = 10
PARAM_BLOCK_BYTES = PARAMS_PER_BLOCK * PARAM_SLOT.size  # 60
PARAM_PADDING = (0xFFFF, b"\0\0\0\0")


def decode_gip(payload):
    """
    One GIP message as (header dict, [(param_id, raw4)]).

    Returns None if it does not look like a GIP message, so a stray write from some other part
    of WGI is reported as raw hex instead of being forced into this shape.
    """
    if len(payload) < GIP_HEADER.size:
        return None
    device, mtype, flags, sequence, length, reserved = GIP_HEADER.unpack_from(payload)
    body = payload[GIP_HEADER.size:GIP_HEADER.size + length]
    pairs = None
    if mtype == 0x0B and body and len(body) % PARAM_SLOT.size == 0:
        pairs = [PARAM_SLOT.unpack_from(body, i)
                 for i in range(0, len(body), PARAM_SLOT.size)]
    return {"device": device, "type": mtype, "flags": flags, "sequence": sequence,
            "length": length, "reserved": reserved, "body": body, "pairs": pairs}


def encode_gip(device_id, mtype, body, flags=0, sequence=0):
    """
    Frame one message for writing.

    `sequence` defaults to 0 because every captured write used 0: 353 writes deduplicated to
    three distinct byte strings per magnitude, which could not happen if the field were
    counting. The driver evidently does not require clients to number their own messages.
    """
    if len(device_id) != 8:
        raise ValueError("device id must be 8 bytes, got %d" % len(device_id))
    return GIP_HEADER.pack(device_id, mtype, flags, sequence, len(body), 0) + body


def encode_params(pairs):
    """
    A 0x0b payload from (param_id, value) pairs, padded to the fixed 60 bytes.

    Values may be float (encoded as float32 LE), int (u32 LE) or 4 raw bytes -- the bank mixes
    all three, so forcing one type at this boundary would just move the conversion outward.
    """
    if len(pairs) > PARAMS_PER_BLOCK:
        raise ValueError("a parameter block holds at most %d pairs" % PARAMS_PER_BLOCK)
    slots = list(pairs) + [PARAM_PADDING] * (PARAMS_PER_BLOCK - len(pairs))
    out = b""
    for pid, value in slots:
        if isinstance(value, bytes):
            raw = value
        elif isinstance(value, float):
            raw = struct.pack("<f", value)
        else:
            raw = struct.pack("<I", value)
        if len(raw) != 4:
            raise ValueError("param 0x%04x value must be 4 bytes" % pid)
        out += PARAM_SLOT.pack(pid, raw)
    return out


def describe_pairs(pairs, indent="          "):
    lines = []
    for pid, raw in pairs:
        if pid == 0xFFFF and raw == b"\0\0\0\0":
            continue
        as_float = struct.unpack("<f", raw)[0]
        as_uint = struct.unpack("<I", raw)[0]
        note = GIP_PARAMS.get(pid, "")
        lines.append("%sparam 0x%04x = %s   float=%-14.6g uint=%-11d %s"
                     % (indent, pid, raw.hex(" "), as_float, as_uint, note))
    return lines


def announce_ids(message):
    """
    (device_id, vid, pid) from a decoded type 0x02 announce, or None.

    THE DEVICE ID IS LEARNED, NEVER HARD-CODED. The announce body is
    `[8-byte device id][u16 VID][u16 PID][...]`, and the same id also appears in the header of
    every message from that device. Matching on VID/PID is what makes this specific to the
    wheel rather than to one machine's enumeration order.
    """
    if message is None or message["type"] != 0x02 or len(message["body"]) < 12:
        return None
    body = message["body"]
    vid, pid = struct.unpack_from("<HH", body, 8)
    return body[:8], vid, pid


# ---------------------------------------------------------------------------
# The captured constant-force effect
# ---------------------------------------------------------------------------

# Uploaded in four type 0x0d messages before the effect will respond. Each carries
# `[u8 0][u16 total_length][u16 offset][48 bytes]`, offsets 0/48/96/144, so the real payload is
# 186 bytes padded to 192.
#
# WHAT THIS IS, HONESTLY: unknown. It is a table or compiled program the firmware wants before
# it will act on the parameter bank, and its internals were never decoded -- the bytes are
# replayed exactly as captured. The bytes below came from WGI loading ONE effect: a
# ConstantForceEffect on the X axis.
#
# That single effect is not a limitation here, because `ffb_render.py` already reduces every
# game effect -- springs, dampers, periodics, all of them summed -- to one constant force
# rewritten at loop rate. This is the only effect the bridge ever needs.
FFB_TABLE_CHUNK = 48
FFB_TABLE_LENGTH = 186

# The four captured chunks, in upload order, exactly as they appeared after each 5-byte
# 0x0d sub-header. Kept split so they can be diffed line-for-line against a trace log.
_FFB_TABLE_CHUNKS = (
    "a000500 34a01d04b 40d04b241124402411 2414002444242106 21a02420401401 00220222011420140120a000214214 01",
    "0222022201a0012103 140201a001210321 1004132414012 3d0140222412322 210921a00221451403 2511140114032211",
    "2022234523223025 2225132314032311 1404140522122322 2312232523101405 2326451406251114 0620224522233025",
    "2325132214062311 1407140522122322 2312232523102623 2212212221121408 212212140922 21a02153 000000000000",
)
FFB_TABLE = bytes.fromhex("".join(_FFB_TABLE_CHUNKS).replace(" ", ""))[:FFB_TABLE_LENGTH]

# The non-zero entries of the 256-slot parameter bank, as WGI left it after loading the
# constant-force effect. Everything else in 0x0000..0x00ff was zero.
#
# Only 0x0008 is understood. The others are replayed because replaying them is what the
# firmware saw when it worked; the notes are observations, not decoded meanings.
FFB_INIT_PARAMS = {
    0x0001: 600000.0,   # matches the commanded 60 s duration at 100 us/unit -- ONE sample,
                        # so this is a hypothesis, not a decoded field.
    0x0002: 1,          # integer 1, not a float. Effect type or axis count.
    0x0003: -1.0,       # the -1/+1 pairs look like magnitude or axis clamps.
    0x0005: 1.0,
    0x0006: -1.0,
    0x0009: 1.0,
}
PARAM_BANK_SIZE = 0x0100

# Type 0x0c "state" bodies, 9 bytes. 0x00 clears, 0xf0 runs; 0x20 appears only at teardown.
STATE_CLEAR = b"\0" * 9
STATE_LOADED = bytes.fromhex("20") + b"\0" * 8
STATE_RUNNING = bytes.fromhex("f0") + b"\0" * 8

# Type 0x0a "short cmd", 3 bytes. 0x00 resets, 0x06 starts/keeps alive.
CMD_RESET = b"\0\0\0"
SHORT_CMD = bytes.fromhex("060000")


def table_chunks(table=FFB_TABLE, chunk=FFB_TABLE_CHUNK):
    """The 0x0d bodies for a table upload, in order."""
    total = len(table)
    padded = table + b"\0" * (-total % chunk)
    return [struct.pack("<BHH", 0, total, offset) + padded[offset:offset + chunk]
            for offset in range(0, len(padded), chunk)]


def zero_param_blocks(bank=PARAM_BANK_SIZE):
    """The 0x0b bodies that zero the whole bank, ten slots at a time -- 26 messages."""
    blocks = []
    for start in range(0, bank, PARAMS_PER_BLOCK):
        pairs = [(pid, 0) for pid in range(start, min(start + PARAMS_PER_BLOCK, bank))]
        blocks.append(encode_params(pairs))
    return blocks


def value_param_block(params=None):
    """
    The single 0x0b body carrying the effect's real values, ids 0x0000..0x0009.

    ONE block, not the whole bank. WGI zeroes all 256 slots BEFORE uploading the table and
    then writes only this one block after it -- see `load_sequence`.
    """
    values = dict(FFB_INIT_PARAMS if params is None else params)
    return encode_params([(pid, values.get(pid, 0)) for pid in range(PARAMS_PER_BLOCK)])


def load_sequence():
    """
    The exact ordered (message type, body) list WGI sends to arm this effect.

    THIS ORDER IS LOAD-BEARING AND WAS GOT WRONG ONCE. The first implementation uploaded the
    table, then wrote the whole 256-slot bank with values, then set state -- and the wheel
    went slack without ever producing torque: the firmware handed over the motor and then had
    no effect to run. Comparing the ordered byte streams (`gip_diff.py`) showed WGI actually:

      1. RESETS first -- 0x0a 00 00 00, then a zeroed 0x0c
      2. zeroes all 256 parameter slots (26 blocks)
      3. clears state again
      4. uploads the table
      5. writes ONE value block, ids 0x0000..0x0009
      6. sets state running, then starts

    Steps 1-3 were missing entirely, and step 5 was being sent as 26 blocks instead of one.
    """
    out = [(0x0A, CMD_RESET), (0x0C, STATE_CLEAR)]
    out += [(0x0B, body) for body in zero_param_blocks()]
    out.append((0x0C, STATE_CLEAR))
    out += [(0x0D, body) for body in table_chunks()]
    out.append((0x0B, value_param_block()))
    out.append((0x0C, STATE_RUNNING))
    out.append((0x0A, SHORT_CMD))
    return out


def force_block(magnitude):
    """
    The 0x0b body that sets X force. This is the one field that is fully verified.

    Sent ONCE per change, not repeatedly. What repeats at ~16 Hz is the 0x0c STATE_RUNNING
    heartbeat -- WGI sent 273 of those against 6 force blocks across the same run.
    """
    return encode_params([(PARAM_X_FORCE, float(magnitude))])
