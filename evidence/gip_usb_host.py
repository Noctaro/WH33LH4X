r"""
gip_usb_host.py -- Be the GIP host ourselves, over raw USB. Linux only.

Every route to this motor through Windows is closed: WGI output is foreground-gated by
documented design, GameInput reports no motor, DirectInput and HID PID find no collection, and
writes replayed to \\.\XboxGIP produced no torque because the driver refuses our handle. The one
untested idea left is to remove Microsoft's driver from the path entirely and speak GIP straight
to the USB endpoints, where no notion of focus exists.

Measured on the target machine (Ubuntu 24.04, kernel 6.14):

    iface 1-1:1.0   class=ff  sub=47  proto=d0   driver=xpad
       ep 0x01  Interrupt OUT  64 B  interval 4
       ep 0x81  Interrupt IN   64 B  interval 4

xpad binds and delivers steering with only its minimal init and no LED, so the device does not
demand a full handshake or any authentication before it talks.

WIRE FRAMING is not the framing our Windows capture recorded. That capture sat at the driver
interface, which prefixes an 8-byte device id and fixed-width fields. On the wire, per xone's
bus/protocol.c and [MS-GIPUSB], a message is:

    byte 0    command
    byte 1    options   -- low nibble client id; 0x10 ack, 0x20 internal,
                           0x40 chunk start, 0x80 chunk
    byte 2    sequence
    byte 3+   packet length, 7-bit varint, bit 7 = continuation
              padded with one 0x00 so the header length is EVEN
    [chunk offset varint, only when the chunk bit is set]
    payload

SAFETY: this wheel can whip itself to full lock. Force is capped, zeroed in a finally block, and
this is never run unattended.

Usage (after installing python3-usb and the udev rule):
    python3 gip_usb_host.py --probe                 # claim, read input, prove we own it
    python3 gip_usb_host.py --arm                   # send the load sequence, no force
    python3 gip_usb_host.py --force 0.30 --hold 1.5 # arm, then command force
"""

import argparse
import functools
import struct
import sys
import time

print = functools.partial(print, flush=True)  # noqa: A001 -- ssh gives us a pipe, not a tty

try:
    import usb.core
    import usb.util
except ImportError:
    sys.exit("pyusb missing -- install it with: sudo apt install python3-usb")

try:
    import gip_protocol
except ImportError:
    sys.exit("gip_protocol.py must sit beside this file")

# Windows has no system libusb; libusb_package ships one, so prefer it when present.
try:
    import libusb_package

    def _backend():
        return libusb_package.get_libusb1_backend()
except ImportError:
    def _backend():
        return None

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

# Command ids, from xone's bus/protocol.c.
GIP_CMD_ACKNOWLEDGE = 0x01
GIP_CMD_ANNOUNCE = 0x02
GIP_CMD_STATUS = 0x03
GIP_CMD_IDENTIFY = 0x04
GIP_CMD_POWER = 0x05
GIP_CMD_LED = 0x0A
GIP_CMD_HID_REPORT = 0x0B
GIP_CMD_INPUT = 0x20

# Refuse to command more than this without an explicit override; the wheel is strong.
DEFAULT_CAP = 0.35

# Which 0x0c state the heartbeat repeats. The capture holds 269 STATE_RUNNING against 2
# STATE_LOADED, but frequency is not the same question as which one keeps the effect alive --
# the run that first produced torque used STATE_LOADED. Selectable so it can be measured.
BEAT_STATE = [None]


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
        # xone sets the continuation bit on the last varint byte and appends a zero.
        head[-1] |= 0x80
        head.append(0x00)
    return bytes(head)


def decode_header(data):
    """Inverse of encode_header. Returns a dict, or None if it cannot be a GIP message."""
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


class Wheel:
    def __init__(self, verbose=True, pad=False, wait=0.0, client=0, zlp=False, dump=False,
                 reattach=True):
        self.verbose = verbose
        # Handing the kernel driver back looks polite, but a run that reattaches leaves the
        # device in the state where the NEXT run gets no torque. Every confirmed torque run
        # so far inherited a device whose previous host died without reattaching.
        self.reattach = reattach
        self.pad = pad
        self.zlp = zlp
        self.dump = [] if dump else None
        self.client = client
        self.chunk_total = 0
        self.sequence = 1
        self.detached = False
        self.dev = None
        deadline = time.time() + wait
        while True:
            self.dev = usb.core.find(idVendor=HORI_VID, idProduct=HORI_PID,
                                     backend=_backend())
            if self.dev is not None or time.time() > deadline:
                break
            time.sleep(0.05)
        if self.dev is None:
            raise SystemExit("wheel %04x:%04x not found -- plugged in and in XBOX mode?"
                             % (HORI_VID, HORI_PID))

    def log(self, message):
        if self.verbose:
            print(message, flush=True)

    def open(self):
        # Kernel-driver detach is a libusb concept Windows does not implement: there the
        # binding is done once, out of band, by pointing the device at WinUSB.
        try:
            if self.dev.is_kernel_driver_active(INTERFACE):
                self.log("  detaching kernel driver from interface %d" % INTERFACE)
                self.dev.detach_kernel_driver(INTERFACE)
                self.detached = True
        except NotImplementedError:
            pass
        usb.util.claim_interface(self.dev, INTERFACE)
        self.log("  interface %d claimed" % INTERFACE)

    def close(self):
        try:
            usb.util.release_interface(self.dev, INTERFACE)
        except Exception:
            pass
        if self.detached and not self.reattach:
            self.log("  leaving the kernel driver detached (--no-reattach)")
        elif self.detached:
            try:
                self.dev.attach_kernel_driver(INTERFACE)
                self.log("  kernel driver reattached")
            except (NotImplementedError, usb.core.USBError) as exc:
                self.log("  could not reattach the kernel driver: %s" % exc)

    def send(self, command, body, options=0x00, sequence=None):
        """One GIP message on the interrupt OUT endpoint."""
        seq = self.sequence if sequence is None else sequence
        # The options low nibble is the CLIENT ID. We have always sent 0; a device that hosts
        # several clients would silently drop actuator commands aimed at the wrong one.
        header = encode_header(command, options | (self.client & 0x0F), seq, len(body))
        packet = header + body
        if self.pad and len(packet) < PACKET:
            # Some interrupt endpoints only accept full-size packets; the force message is
            # already 64 bytes, but LED and power-on are not, so this is worth ruling out.
            packet = packet + b"\x00" * (PACKET - len(packet))
        if sequence is None:
            self.sequence = (self.sequence + 1) & 0xFF or 1  # xone never uses sequence 0
        if self.dump is not None:
            # The full packet as transmitted: header, padding and all. Recording the body alone
            # hid framing and padding differences, which is exactly what made one regression
            # unfalsifiable.
            self.dump.append(bytes(packet))
        written = self.dev.write(EP_OUT, packet, timeout=1000)
        # A transfer that is an exact multiple of wMaxPacketSize needs a zero-length packet to
        # mark its end. WinUSB leaves SHORT_PACKET_TERMINATE off by default, so a 64-byte force
        # block is never seen as complete -- which is exactly the message that never worked on
        # Windows, while every shorter one did.
        if self.zlp and len(packet) % PACKET == 0:
            try:
                self.dev.write(EP_OUT, b"", timeout=200)
            except usb.core.USBError as exc:
                self.log("  ZLP failed: %s" % exc)
        # A short write means the endpoint took less than we handed it, which would make every
        # "sent ok with no effect" result meaningless. Never assume; the count is free.
        if written != len(packet):
            self.log("  SHORT WRITE: %d of %d bytes for command 0x%02x"
                     % (written, len(packet), command))
        return written

    def power_on(self):
        """xpad's Xbox One init packet: 05 20 00 01 00.

        Detaching the kernel driver takes the device's bring-up with it -- after that it sends
        a status message and goes quiet. This is what puts it back in a talking state.
        """
        self.send(GIP_CMD_POWER, b"\x00", options=OPT_INTERNAL, sequence=0)
        self.log("  power-on sent (05 20 00 01 00)")

    def acknowledge(self, head, received=None):
        """Reply to a message whose options set OPT_ACKNOWLEDGE.

        Format from xone's gip_acknowledge_pkt: nine bytes carrying the acked command, our
        options, how much has arrived and how much is left. Without this the device stops
        after its first chunk and every later command of ours is ignored -- which is exactly
        what we measured before implementing it.
        """
        # On a CHUNK_START packet chunk_offset carries the TOTAL size, not an offset, so
        # xone's chunk_offset + packet_length would claim far more than arrived -- which told
        # the device the transfer was finished and got an empty terminating chunk back.
        # The caller passes how much it has actually assembled.
        if received is None:
            received = head["length"] if head["options"] & OPT_CHUNK_START \
                else head["chunk_offset"] + head["length"]
        remaining = 0
        if head["options"] & OPT_CHUNK and self.chunk_total:
            remaining = max(0, self.chunk_total - received)
        body = struct.pack("<BBBHHH", 0x00, head["command"], OPT_INTERNAL,
                           received & 0xFFFF, 0x0000, remaining & 0xFFFF)
        self.send(GIP_CMD_ACKNOWLEDGE, body, options=OPT_INTERNAL,
                  sequence=head["sequence"])

    def read(self, timeout=200):
        try:
            return bytes(self.dev.read(EP_IN, PACKET, timeout=timeout))
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError:
            return None


def probe(wheel, seconds, label=""):
    """Read the interrupt IN endpoint and report which payload bytes actually vary.

    A byte that changes is not automatically an axis: the first pass here mistook a counter
    incrementing by a steady 11 per message for steering. Comparing a still window against a
    moving one is what separates the two, so this reports per-offset variability rather than
    printing a few lines to be eyeballed.
    """
    print("\n-- reading input for %g s %s --" % (seconds, label))
    kinds = {}
    payloads = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        data = wheel.read()
        if not data:
            continue
        kinds[data[0]] = kinds.get(data[0], 0) + 1
        if data[0] == GIP_CMD_INPUT and len(data) > 4:
            payloads.append(data[4:])

    print("   %d message(s); commands: %s"
          % (sum(kinds.values()),
             ", ".join("0x%02x x%d" % kv for kv in sorted(kinds.items())) or "none"))
    if not payloads:
        print("   no 0x20 INPUT messages at all")
        return kinds, {}

    width = min(len(p) for p in payloads)
    variance = {}
    for offset in range(width):
        values = {p[offset] for p in payloads}
        if len(values) > 1:
            variance[offset] = (len(values), min(values), max(values))
    print("   %d input message(s), payload %d B" % (len(payloads), width))
    if variance:
        for offset, (count, low, high) in sorted(variance.items()):
            print("      byte %2d: %3d distinct, 0x%02x..0x%02x" % (offset, count, low, high))
    else:
        print("      NO payload byte varied at all")
    return kinds, variance


def identify(wheel, seconds, ack=True):
    """Ask the device to describe itself, acknowledging chunks as they arrive.

    The device answers 0x04 with options 0xf0 -- ACKNOWLEDGE | INTERNAL | CHUNK_START | CHUNK --
    so it is asking to be acknowledged and sending a chunked transfer. Reassembling the whole
    449-byte descriptor is the proof that our acknowledgements are correct, because without
    them the device stops after the first chunk.
    """
    print("\n-- 0x04 IDENTIFY, acks %s, listening %g s --"
          % ("ON" if ack else "OFF", seconds))
    wheel.chunk_total = 0
    wheel.send(GIP_CMD_IDENTIFY, b"", options=OPT_INTERNAL)

    kinds = {}
    assembled = bytearray()
    chunks = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        data = wheel.read()
        if not data:
            continue
        head = decode_header(data)
        if not head:
            continue
        kinds[head["command"]] = kinds.get(head["command"], 0) + 1
        if head["command"] != GIP_CMD_INPUT:
            print("   rx cmd=0x%02x opt=0x%02x seq=%3d len=%4d chunk_off=%4d hdr=%d"
                  % (head["command"], head["options"], head["sequence"],
                     head["length"], head["chunk_offset"], head["header"]))

        if head["command"] == GIP_CMD_IDENTIFY:
            if head["options"] & OPT_CHUNK_START:
                wheel.chunk_total = head["chunk_offset"]
                assembled = bytearray()
                chunks = 0
            assembled += head["payload"]
            chunks += 1

        if ack and head["options"] & OPT_ACKNOWLEDGE:
            wheel.acknowledge(head, received=len(assembled))

    print("   commands seen: %s"
          % (", ".join("0x%02x x%d" % kv for kv in sorted(kinds.items())) or "none"))
    if assembled:
        print("   descriptor: %d chunk(s), %d of %d bytes assembled"
              % (chunks, len(assembled), wheel.chunk_total))
        print("   head: %s" % assembled[:28].hex(" "))
        if wheel.chunk_total and len(assembled) >= wheel.chunk_total:
            print("   *** COMPLETE DESCRIPTOR -- acknowledgements are working ***")
    else:
        print("   no 0x04 reply")
    return bytes(assembled)


def pump(wheel, seconds, quiet=True, heartbeat=False):
    """Read and acknowledge for a while.

    A real host keeps answering the device. Without this the conversation stalls after the
    first reply that asks to be acknowledged, and every later command is ignored -- which is
    what made the LED and the motor look dead.
    """
    kinds = {}
    deadline = time.time() + seconds
    # WGI never stops talking: the capture holds 269 type 0x0c STATE_RUNNING heartbeats against
    # 6 force blocks, with 0x0a interleaved every ~2 s. STATE_LOADED appears only twice and is a
    # one-off transition ending the arming sequence, not the heartbeat.
    next_beat = time.time()
    next_short = time.time()
    while time.time() < deadline:
        now = time.time()
        if heartbeat and now >= next_beat:
            wheel.send(gip_protocol_state(), BEAT_STATE[0])
            next_beat = now + 1.0 / 16.0
        if heartbeat and now >= next_short:
            wheel.send(GIP_CMD_LED, gip_protocol.SHORT_CMD)
            next_short = now + 2.0

        data = wheel.read(timeout=20)
        if not data:
            continue
        head = decode_header(data)
        if not head:
            continue
        kinds[head["command"]] = kinds.get(head["command"], 0) + 1
        if head["options"] & OPT_ACKNOWLEDGE:
            wheel.acknowledge(head, received=head["length"])
        if not quiet and head["command"] != GIP_CMD_INPUT:
            print("   rx cmd=0x%02x opt=0x%02x len=%d"
                  % (head["command"], head["options"], head["length"]))
    return kinds


def gip_protocol_state():
    """Message type carrying effect state; 0x0c, declared max length 9, body is 9 bytes."""
    return 0x0C


def replay(wheel, path, until, gap_ms, beat_ms):
    """Send a captured WGI write stream verbatim, reframed for the wire.

    Our hand-written load_sequence() reproduced only the first 36 messages of an arming that
    actually runs to ~107: WGI uploads the table twice, zeroes the parameter bank twice, and
    ends on a 0x0c 20 state that load_sequence never sends. Replaying the capture removes the
    guesswork that three hand-modelled attempts did not.
    """
    import gip_protocol as gp
    with open(path, encoding="ascii") as handle:
        raw = [bytes.fromhex(line.strip()) for line in handle if line.strip()]
    messages = []
    for blob in raw:
        decoded = gp.decode_gip(blob)
        if decoded:
            messages.append((decoded["type"], decoded["body"]))
    if until:
        messages = messages[:until]

    counts = {}
    print("\n-- replaying %d captured message(s) from %s --" % (len(messages), path))
    for mtype, body in messages:
        wheel.send(mtype, body)
        counts[mtype] = counts.get(mtype, 0) + 1
        if gap_ms:
            time.sleep(gap_ms / 1000.0)
        wheel.read(timeout=1)
    print("   sent: %s" % ", ".join("0x%02x x%d" % kv for kv in sorted(counts.items())))
    return messages


def cold_listen(wheel, seconds):
    """Capture the connection sequence from the first packet, acknowledging everything.

    We have never seen a 0x02 ANNOUNCE because the device always announced to xpad before we
    detached it. Unbinding xpad and watching from plug-in is the only way to see what a real
    host is answering during bring-up, which is the one phase still unimplemented.
    """
    print("\n-- cold listen, %g s, acking everything --" % seconds)
    kinds = {}
    deadline = time.time() + seconds
    while time.time() < deadline:
        data = wheel.read(timeout=50)
        if not data:
            continue
        head = decode_header(data)
        if not head:
            continue
        kinds[head["command"]] = kinds.get(head["command"], 0) + 1
        if head["command"] != GIP_CMD_INPUT:
            print("   rx cmd=0x%02x opt=0x%02x seq=%3d len=%4d chunk=%4d | %s"
                  % (head["command"], head["options"], head["sequence"], head["length"],
                     head["chunk_offset"], head["payload"][:20].hex(" ")))
        if head["options"] & OPT_ACKNOWLEDGE:
            wheel.acknowledge(head, received=head["length"])
    print("   commands seen: %s"
          % (", ".join("0x%02x x%d" % kv for kv in sorted(kinds.items())) or "none"))
    return kinds


def await_announce(wheel, seconds):
    """Wait for the device to introduce itself with 0x02, acknowledging anything that asks.

    On Linux a kernel driver had always brought the device up before we detached it, so we
    never saw an announce and a thin bring-up sufficed. Under WinUSB nothing precedes us: the
    device announces to us because we are genuinely its first host, which means it sits at a
    different point in its state machine and expects to be initialised in order.
    """
    print("\n-- waiting up to %g s for a 0x02 ANNOUNCE --" % seconds)
    deadline = time.time() + seconds
    while time.time() < deadline:
        data = wheel.read(timeout=100)
        if not data:
            continue
        head = decode_header(data)
        if not head:
            continue
        if head["options"] & OPT_ACKNOWLEDGE:
            wheel.acknowledge(head, received=head["length"])
        if head["command"] == GIP_CMD_ANNOUNCE:
            body = head["payload"]
            print("   ANNOUNCE: device %s  VID %04X PID %04X"
                  % (body[:8].hex(), int.from_bytes(body[8:10], "little"),
                     int.from_bytes(body[10:12], "little")))
            return head
    print("   no announce seen (device was probably brought up before we arrived)")
    return None


def handshake(wheel, seconds=3.0, announce_wait=0.0):
    """Bring the device up: announce first when it offers one, then power, then identify."""
    if announce_wait:
        await_announce(wheel, announce_wait)
    wheel.power_on()
    time.sleep(0.3)
    descriptor = identify(wheel, seconds, ack=True)
    return descriptor


def set_led(wheel, mode, brightness):
    """GIP 0x0a, three bytes: {unknown, mode, brightness}.

    The device declares a max length of 3 for 0x0a, which matches xone's LED packet exactly.
    That also reinterprets our own capture: the messages `gip_protocol` calls CMD_RESET
    (0x0a 00 00 00) and SHORT_CMD (0x0a 06 00 00) are LED commands with mode 0 -- LED off.
    A visible response here proves the device acts on our commands, not just our reads.
    """
    wheel.send(GIP_CMD_LED, bytes((0x00, mode, brightness)))
    print("   LED command sent: mode=%d brightness=%d" % (mode, brightness))


def arm(wheel):
    """Replay the load sequence WGI performs before force means anything."""
    print("\n-- arming --")
    sent = 0
    for mtype, body in gip_protocol.load_sequence():
        wheel.send(mtype, body)
        sent += 1
        time.sleep(0.004)
    print("   %d message(s) sent" % sent)
    return sent


def command_force(wheel, magnitude, hold, beat=False):
    # force_block is the one field in this protocol that round-tripped across five magnitudes.
    body = gip_protocol.force_block(magnitude)
    wheel.send(GIP_CMD_HID_REPORT, body)
    print("   force %+.2f  (%d byte body)" % (magnitude, len(body)))
    if hold:
        pump(wheel, hold, heartbeat=beat)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe", action="store_true", help="claim and read input only")
    parser.add_argument("--arm", action="store_true", help="send the load sequence")
    parser.add_argument("--force", type=float, help="command this magnitude after arming")
    parser.add_argument("--hold", type=float, default=1.5, help="seconds per magnitude")
    parser.add_argument("--sweep", action="store_true",
                        help="after arming, sweep +/- the --force magnitude")
    parser.add_argument("--cap", type=float, default=DEFAULT_CAP,
                        help="refuse magnitudes above this (default %.2f)" % DEFAULT_CAP)
    parser.add_argument("--seconds", type=float, default=6.0, help="probe duration")
    parser.add_argument("--led", nargs=2, type=int, metavar=("MODE", "BRIGHTNESS"),
                        help="send a 0x0a LED command and hold, e.g. --led 1 20")
    parser.add_argument("--identify", action="store_true",
                        help="send 0x04 and report whether the device answers")
    parser.add_argument("--replay", metavar="FILE",
                        help="replay a captured write stream (logs/wgi.txt) before commanding")
    parser.add_argument("--replay-until", type=int, default=107,
                        help="stop the replay before this message index (default 107, the "
                             "first force block)")
    parser.add_argument("--gap-ms", type=float, default=2.0,
                        help="delay between replayed messages")
    parser.add_argument("--client", type=int, default=0,
                        help="client id placed in the options low nibble (default 0)")
    parser.add_argument("--client-sweep", type=int, metavar="MAX",
                        help="arm and command force once per client id 0..MAX")
    parser.add_argument("--dump-sent", metavar="FILE",
                        help="write every message we transmit, in order, as type+body hex")
    parser.add_argument("--warmup", type=float, default=0.0,
                        help="read for this long after identify, before arming, so the wheel "
                             "can finish its calibration sweep")
    parser.add_argument("--no-reattach", action="store_true",
                        help="on exit, leave the kernel driver detached instead of handing "
                             "it back -- the state every confirmed torque run inherited")
    parser.add_argument("--zlp", action="store_true",
                        help="send a zero-length packet after any transfer that is an exact "
                             "multiple of the 64-byte max packet size (needed on WinUSB)")
    parser.add_argument("--announce-wait", type=float, default=0.0,
                        help="seconds to wait for a 0x02 announce before powering on")
    parser.add_argument("--beat-state", choices=("loaded", "running"), default="loaded",
                        help="0x0c body the heartbeat repeats (default loaded)")
    parser.add_argument("--heartbeat", action="store_true",
                        help="keep sending the 0x0c running-state heartbeat at 16 Hz, as WGI does")
    parser.add_argument("--cold", action="store_true",
                        help="claim immediately and log the connection sequence, sending nothing")
    parser.add_argument("--wait-device", type=float, default=0.0,
                        help="poll this many seconds for the wheel to appear")
    parser.add_argument("--no-ack", action="store_true",
                        help="do not acknowledge, to show the transfer stalls without it")
    parser.add_argument("--pad", action="store_true",
                        help="pad every OUT packet to the full 64 bytes")
    parser.add_argument("--label", default="", help="phase label for the probe output")
    parser.add_argument("--no-init", action="store_true",
                        help="skip the power-on packet, to see what the device does without it")
    args = parser.parse_args()

    if args.force is not None and abs(args.force) > args.cap:
        sys.exit("refusing %.2f: above the %.2f cap. Raise --cap deliberately if you mean it."
                 % (args.force, args.cap))

    BEAT_STATE[0] = (gip_protocol.STATE_LOADED if args.beat_state == "loaded"
                     else gip_protocol.STATE_RUNNING)
    wheel = Wheel(pad=args.pad, wait=args.wait_device, client=args.client, zlp=args.zlp,
                  dump=bool(args.dump_sent), reattach=not args.no_reattach)
    print("found %04x:%04x%s" % (HORI_VID, HORI_PID, "  [padding OUT to 64 B]" if args.pad else ""))
    wheel.open()
    try:
        if args.cold:
            cold_listen(wheel, args.seconds)
        elif not args.no_init:
            if args.replay:
                pass  # the replay path does its own bring-up above
            elif args.led or args.force is not None or args.arm \
                    or args.client_sweep is not None:
                # A real host identifies the device before commanding it; skipping that is
                # why every earlier command went unanswered.
                handshake(wheel, announce_wait=args.announce_wait)
            else:
                wheel.power_on()
                time.sleep(0.5)

        if args.identify:
            identify(wheel, args.seconds, ack=not args.no_ack)

        if args.led:
            print("\n-- LED command, WATCH THE WHEEL --")
            set_led(wheel, args.led[0], args.led[1])
            pump(wheel, args.hold)

        if args.probe or not (args.arm or args.force is not None or args.led or args.identify):
            # Never block on input here: a prompt mid-measurement stops the sampling loop, and
            # this runs over ssh with no tty. Phases are separate invocations, each started
            # once the user is ready -- pass --label to say which is which.
            probe(wheel, args.seconds, args.label)

        if (args.arm or args.force is not None) and not args.replay:
            arm(wheel)
            print("   settling")
            pump(wheel, 3.0, heartbeat=args.heartbeat)

        if args.replay:
            # Always bring the device up before replaying; --announce-wait only decides
            # whether we let it introduce itself first.
            if args.announce_wait:
                await_announce(wheel, args.announce_wait)
            wheel.power_on()
            time.sleep(0.3)
            identify(wheel, 3.0, ack=True)
            if args.warmup:
                # Let the wheel finish its calibration sweep before arming. With xpad bound the
                # device was always calibrated long before we touched it; as first host it is
                # still mid-sweep, and a wheel that has not established centre and range has
                # every reason to refuse to drive its motor.
                print("\n-- warmup: reading for %g s, let the wheel finish calibrating --"
                      % args.warmup)
                kinds = pump(wheel, args.warmup)
                print("   saw: %s"
                      % ", ".join("0x%02x x%d" % kv for kv in sorted(kinds.items())))
            replay(wheel, args.replay, args.replay_until, args.gap_ms, None)
            print("   settling with the 0x0c 20 heartbeat")
            pump(wheel, 2.0, heartbeat=True)

        if args.client_sweep is not None:
            print("\n-- CLIENT ID SWEEP, HANDS ON THE WHEEL --")
            for cid in range(args.client_sweep + 1):
                wheel.client = cid
                print("\n   === client id %d ===" % cid)
                arm(wheel)
                pump(wheel, 1.5, heartbeat=True)
                command_force(wheel, args.cap, 1.5, beat=True)
                command_force(wheel, -args.cap, 1.5, beat=True)
                command_force(wheel, 0.0, 0.3)
            wheel.client = 0

        elif args.force is not None:
            print("\n-- commanding force, HANDS ON THE WHEEL --")
            magnitudes = [args.force, -args.force, args.force] if args.sweep else [args.force]
            for magnitude in magnitudes:
                command_force(wheel, magnitude, args.hold, beat=args.heartbeat)
    finally:
        if args.dump_sent and wheel.dump is not None:
            with open(args.dump_sent, "w", encoding="ascii") as handle:
                for packet in wheel.dump:
                    handle.write("%s\n" % packet.hex())
            print("\n   wrote %d sent message(s) to %s" % (len(wheel.dump), args.dump_sent))
        try:
            command_force(wheel, 0.0, 0.0)
            print("\n   force zeroed")
        except Exception as exc:
            print("\n   could not zero force: %s" % exc)
        wheel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
