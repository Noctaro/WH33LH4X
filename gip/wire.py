"""
GIP message framing on the USB wire, per xone's bus/protocol.c and [MS-GIPUSB].

    byte 0    command
    byte 1    options: low nibble client id; 0x10 ack, 0x20 internal, 0x40 chunk start,
              0x80 chunk
    byte 2    sequence
    byte 3+   payload length, 7-bit varint, padded with one 0x00 so the header length is even
    [chunk offset varint, only when the chunk bit is set]
    payload
"""

HORI_VID = 0x0F0D
HORI_PID = 0x015C
INTERFACE = 0
EP_OUT = 0x01
EP_IN = 0x81
PACKET = 64

OPT_ACKNOWLEDGE = 0x10
OPT_INTERNAL = 0x20
OPT_CHUNK_START = 0x40
OPT_CHUNK = 0x80

GIP_CMD_ACKNOWLEDGE = 0x01
GIP_CMD_ANNOUNCE = 0x02
GIP_CMD_STATUS = 0x03
GIP_CMD_IDENTIFY = 0x04
GIP_CMD_POWER = 0x05
GIP_CMD_AUTHENTICATE = 0x06
GIP_CMD_LED = 0x0A
GIP_CMD_HID_REPORT = 0x0B
GIP_CMD_INPUT = 0x20


def encode_varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def encode_header(command, options, sequence, length):
    """Three fixed bytes plus a varint length, padded so the header length is even."""
    head = bytearray((command, options, sequence))
    head += encode_varint(length)
    if len(head) % 2:
        # As xone: continuation bit on the last varint byte, then a zero.
        head[-1] |= 0x80
        head.append(0x00)
    return bytes(head)


def decode_header(data):
    """Inverse of encode_header, as a dict with the payload sliced out; None if too short."""
    if len(data) < 4:
        return None
    command, options, sequence = data[0], data[1], data[2]
    index = 3
    length = 0
    shift = 0
    while index < len(data):
        byte = data[index]
        length |= (byte & 0x7F) << shift
        index += 1
        shift += 7
        if not byte & 0x80:
            break
    chunk_offset = 0
    if options & OPT_CHUNK:
        shift = 0
        while index < len(data):
            byte = data[index]
            chunk_offset |= (byte & 0x7F) << shift
            index += 1
            shift += 7
            if not byte & 0x80:
                break
    return {"command": command, "options": options, "sequence": sequence,
            "length": length, "chunk_offset": chunk_offset, "header": index,
            "payload": data[index:index + length]}
