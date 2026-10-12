r"""
test_gip.py: proves the gip package still puts the same bytes on the wire.

The golden values were taken from evidence/gip_arming.py and gip_usb_host.py before they moved
into gip/, and those bytes are the ones that drove the motor on Linux and Windows. Any change
here is a bug until the wheel says otherwise. The input checks use reports recorded by
evidence/input_map.py on 2026-09-23.

No hardware, no pyusb, no test framework:

    .\.venv\Scripts\python.exe test_gip.py
"""

import hashlib
import sys

from gip import arming, report, wire

ARMING_SHA256 = "cc748028706f7b16db2c5113720751da464bd9b870ea7e294dcebc8959fd62c0"
FORCE_SHA256 = {
    0.0: "5ce4152492164a3648058c32e2c9300dcc98ef05f343dbd69681c3cc483c9515",
    0.25: "9eed93d4a60141a7c132348f6691d93cda41fb5ba800fb1d6e32f3dc884e6bc6",
    -1.0: "632d99ed1f09cdebedb405670e34485df04b54b9a136f52c500dea2786866fb9",
}
BLOCK_SHA256 = "1c54167acdefb07543234376b32c2d3634a1d8898a12c12af4b7433e61fceb1c"
HEADERS = {
    (0x0B, 0x00, 5, 60): "0b00053c",
    (0x05, 0x20, 0, 1): "05200001",
    (0x0D, 0x00, 1, 53): "0d000135",
    (0x0C, 0x00, 1, 9): "0c000109",
    (0x20, 0x00, 7, 35): "20000723",
    (0x04, 0x10, 2, 200): "041002c88100",
}
IDLE = "0000d78e0000000000000007006801ffe8000000000000000000000000000000000000"


def check(name, condition, detail=""):
    print("  %s %s%s" % ("PASS" if condition else "FAIL", name,
                         "" if condition else "   <-- " + detail))
    return bool(condition)


def digest(sequence):
    h = hashlib.sha256()
    for command, body, options, delay in sequence:
        h.update(bytes([command, options]) + body + repr(delay).encode())
    return h.hexdigest()


def payload(first_bytes):
    """IDLE with its leading bytes replaced."""
    raw = bytes.fromhex(IDLE)
    return first_bytes + raw[len(first_bytes):]


def test_arming():
    sequence = arming.arming_sequence()
    return all([
        check("arming is 150 messages", len(sequence) == 150, str(len(sequence))),
        check("arming bytes unchanged", digest(sequence) == ARMING_SHA256),
        check("heartbeat is STATE_RUNNING",
              arming.heartbeat() == (0x0C, bytes.fromhex("f00000000000000000"))),
    ])


def test_force():
    results = [check("force sequence %+.2f unchanged" % value,
                     digest(arming.force_sequence(value)) == expected)
               for value, expected in FORCE_SHA256.items()]
    block = hashlib.sha256(arming.force_block(0.25)).hexdigest()
    results.append(check("force block unchanged", block == BLOCK_SHA256))
    return all(results)


def test_framing():
    results = []
    for args, expected in HEADERS.items():
        got = wire.encode_header(*args).hex()
        results.append(check("header %s" % expected, got == expected, got))
    head = wire.decode_header(bytes.fromhex("20000723") + bytes.fromhex(IDLE))
    results.append(check("decode_header round trip",
                         (head["command"], head["sequence"], head["length"], head["header"])
                         == (0x20, 7, 35, 4) and head["payload"].hex() == IDLE))
    return all(results)


def test_report():
    idle = report.decode(bytes.fromhex(IDLE))
    held = report.decode(payload(bytes.fromhex("4000f98e")))
    pedals = report.decode(payload(bytes.fromhex("00000080ffff0080ff7f")))
    return all([
        check("idle has no buttons", idle.buttons == frozenset(), str(idle.buttons)),
        check("steering 0x8ed7 reads right of centre",
              abs(idle.steering - (0x8ED7 - 0x8000) / 32768.0) < 1e-9, str(idle.steering)),
        check("0x40 in byte 0 is X", held.buttons == {"X"}, str(held.buttons)),
        check("0x20 in byte 1 is TIP_DOWN",
              report.decode(payload(b"\x00\x20")).buttons == {"TIP_DOWN"}),
        check("0x01 in byte 1 is DPAD_UP",
              report.decode(payload(b"\x00\x01")).buttons == {"DPAD_UP"}),
        check("steering 0x8000 is centre", pedals.steering == 0.0),
        check("pedals scale 0..1",
              (pedals.throttle, round(pedals.brake, 4), round(pedals.clutch, 4))
              == (1.0, 0.5, 0.5), str(pedals)),
        check("short payload is None", report.decode(b"\x00" * 9) is None),
    ])


def main():
    print("gip wire checks")
    results = [test_arming(), test_force(), test_framing(), test_report()]
    print()
    if all(results):
        print("ALL CHECKS PASSED")
        return 0
    print(">>> FAILURES ABOVE. The bytes on the wire changed, do not ship this.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
