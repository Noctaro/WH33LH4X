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
import os
import struct
import sys
import time

print = functools.partial(print, flush=True)  # noqa: A001 -- ssh gives us a pipe, not a tty

# The gip package sits at the repository root, or beside this file in a flat copy.
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gip import arming as gip_arming  # noqa: E402
from gip.host import Wheel, _backend  # noqa: E402,F401
from gip.report import CENTRE, steering  # noqa: E402,F401
from gip.wire import (  # noqa: E402,F401
    EP_IN,
    EP_OUT,
    GIP_CMD_ACKNOWLEDGE,
    GIP_CMD_ANNOUNCE,
    GIP_CMD_AUTHENTICATE,
    GIP_CMD_HID_REPORT,
    GIP_CMD_IDENTIFY,
    GIP_CMD_INPUT,
    GIP_CMD_LED,
    GIP_CMD_POWER,
    GIP_CMD_STATUS,
    HORI_PID,
    HORI_VID,
    INTERFACE,
    OPT_ACKNOWLEDGE,
    OPT_CHUNK,
    OPT_CHUNK_START,
    OPT_INTERNAL,
    PACKET,
    decode_header,
    encode_header,
    encode_varint,
)

try:
    import gip_protocol
except ImportError:
    sys.exit("gip_protocol.py must sit beside this file")

try:
    import ffb_render
except ImportError:
    ffb_render = None

# Refuse to command more than this without an explicit override; the wheel is strong.
DEFAULT_CAP = 0.35

# Which 0x0c state the heartbeat repeats. The capture holds 269 STATE_RUNNING against 2
# STATE_LOADED, but frequency is not the same question as which one keeps the effect alive --
# the run that first produced torque used STATE_LOADED. Selectable so it can be measured.
BEAT_STATE = [None]


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
    stamps = []
    started = time.time()
    deadline = started + seconds
    while time.time() < deadline:
        data = wheel.read()
        if not data:
            continue
        kinds[data[0]] = kinds.get(data[0], 0) + 1
        if data[0] == GIP_CMD_INPUT and len(data) > 4:
            payloads.append(data[4:])
            stamps.append(time.time() - started)

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

    # Per-second view. The calibration routine drives the wheel to both locks by itself, so
    # while it runs the widest-varying byte sweeps its whole range with nobody touching the
    # wheel; when it finishes, that range collapses. That transition is the thing we need to
    # time, because arming a wheel that is still calibrating is the prime suspect for the
    # silent motor.
    if variance and stamps:
        axis = max(variance.items(), key=lambda kv: kv[1][0])[0]
        print("   per-second range of byte %d (the widest-varying one):" % axis)
        for second in range(int(seconds)):
            window = [p[axis] for p, t in zip(payloads, stamps)
                      if second <= t < second + 1]
            if not window:
                print("      t=%2ds   -- silent --" % second)
                continue
            low, high = min(window), max(window)
            bar = "#" * max(1, (high - low) * 40 // 255)
            print("      t=%2ds  %4d msg  0x%02x..0x%02x  %s"
                  % (second, len(window), low, high, bar))
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


def replay(wheel, path, until, gap_ms, beat_ms, ack=False, read_ms=5, drain_cap=64):
    """Send a captured WGI write stream verbatim, reframed for the wire, and LISTEN.

    Our hand-written load_sequence() reproduced only the first 36 messages of an arming that
    actually runs to ~107: WGI uploads the table twice, zeroes the parameter bank twice, and
    ends on a 0x0c 20 state that load_sequence never sends. Replaying the capture removes the
    guesswork that three hand-modelled attempts did not.

    What it did NOT remove is our deafness. This loop used to call read(timeout=1) and throw
    the result away, so across all 107 arming messages we never examined a reply and never
    acknowledged one -- while identify() and pump() both do. A 1 ms timeout is also shorter
    than the endpoint's interval, so it mostly timed out regardless, and a reply can sit behind
    several 0x20 INPUT frames because the device streams input continuously. Hence the drain.

    Acknowledging is kept behind a flag: the recipe that produces torque today does not ack
    during arming, and it has to keep working as the A/B control.
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
    replies = {}
    ack_requests = 0
    interesting = []
    print("\n-- replaying %d captured message(s) from %s (acks %s) --"
          % (len(messages), path, "ON" if ack else "OFF"))
    for index, (mtype, body) in enumerate(messages):
        wheel.send(mtype, body)
        counts[mtype] = counts.get(mtype, 0) + 1
        if gap_ms:
            time.sleep(gap_ms / 1000.0)
        # Drain rather than sample once. The cap stops a device that streams faster than we
        # read from turning this into an unbounded loop.
        for _ in range(drain_cap):
            data = wheel.read(timeout=read_ms)
            if not data:
                break
            if wheel.trace is not None:
                wheel.trace.append(("rx", time.monotonic(), bytes(data)))
            head = decode_header(data)
            if not head:
                continue
            replies[head["command"]] = replies.get(head["command"], 0) + 1
            if head["command"] != GIP_CMD_INPUT:
                # Anything that is not the input stream is the device reacting to arming --
                # the signal we have never once looked at.
                interesting.append((index, mtype, bytes(data)))
            if head["options"] & OPT_ACKNOWLEDGE:
                ack_requests += 1
                if ack:
                    wheel.acknowledge(head, received=head["length"])

    print("   sent: %s" % ", ".join("0x%02x x%d" % kv for kv in sorted(counts.items())))
    print("   device replied: %s"
          % (", ".join("0x%02x x%d" % kv for kv in sorted(replies.items())) or "NOTHING"))
    print("   messages asking to be acknowledged: %d%s"
          % (ack_requests, "" if ack else "  (IGNORED -- pass --ack-arming to answer them)"))
    if interesting:
        print("   %d non-input message(s) during arming:" % len(interesting))
        for index, mtype, data in interesting[:12]:
            print("      after msg #%d (0x%02x): %s" % (index, mtype, data[:32].hex()))
        if len(interesting) > 12:
            print("      ... and %d more" % (len(interesting) - 12))
    else:
        print("   no non-input messages during arming at all")
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


FORCE_DURATION = b"\x01\x60\xea\x46"   # 30000.0 as f32 LE -- marks a force block
FORCE_SLOT = 0x0008                    # the signed magnitude, proven by A/B


def set_force_magnitude(packet, value, sequence=None):
    """Write an exact signed magnitude into slot 0x08 of a wire force block.

    The sequence byte matters: every force block in the capture carries a fresh one
    (0x35, 0x36, 0x37...). Re-sending a template unchanged means re-sending the same
    sequence number, which the device appears to treat as a duplicate and ignore.
    """
    data = bytearray(packet)
    if sequence is not None:
        data[2] = sequence or 1          # xone never uses sequence 0
    body = data[4:]
    for index in range(0, 60, 6):
        pid, = struct.unpack_from("<H", body, index)
        if pid == FORCE_SLOT:
            struct.pack_into("<f", body, index + 2, value)
            data[4:] = body
            break
    return bytes(data)


def scale_force_block(packet, scale):
    """Rewrite slot 0x08 of a wire force block, preserving its sign.

    Slot 0x08 is the signed magnitude: replaying the capture with it at +/-1.0 pulls the wheel,
    and with it at 0.0 does nothing, everything else held identical. Scaling it is how an
    arbitrary force is commanded without rebuilding the block from scratch, which we cannot yet
    do because the other nine slots are only partly understood.
    """
    data = bytearray(packet)
    body = data[4:]
    for index in range(0, 60, 6):
        pid, = struct.unpack_from("<H", body, index)
        if pid != FORCE_SLOT:
            continue
        current, = struct.unpack_from("<f", body, index + 2)
        sign = -1.0 if current < 0 else 1.0
        struct.pack_into("<f", body, index + 2, sign * abs(scale))
        data[4:] = body
        return bytes(data), sign * abs(scale)
    return bytes(data), None


def replay_wire(wheel, path, max_gap_ms=400.0, read_ms=1, drain_cap=8, force_scale=None):
    """Replay a host->device stream captured from the REAL WIRE, with its original timing.

    WHY THIS REPLACES THE wgi.txt REPLAY (2026-09-22): `logs/wgi.txt` is what WGI wrote to the
    driver, and the driver does not pass it through. Comparing the two streams for the same
    session:

        wire:  150 arming messages   0x0a x5  0x05 x1  0x0c x88  0x0b x52  0x0d x4
        ours:  107 arming messages   0x0a x3  0x05 x0  0x0c x43  0x0b x53  0x0d x8

    The driver reorders, dedupes and rewrites: it sends a 0x05 power message we never sent at
    all, uploads the 0x0d table ONCE at the END rather than twice near the start, and expands
    sparse 0x0b updates into full ten-slot blocks. Reconstructing the arming from the driver
    side was always going to produce something the device had never been asked to accept.

    So stop reconstructing: send exactly what crossed the wire, at the cadence it crossed.
    The one thing that cannot be replayed is the 0x06 authentication, whose challenge is
    freshly random -- so if this still produces nothing, authentication is the remaining
    difference and the raw-USB route is closed.
    """
    entries = []
    with open(path, encoding="ascii") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            delta, packet = line.split(" ", 1)
            entries.append((float(delta), bytes.fromhex(packet)))

    print("\n-- replaying %d WIRE packet(s) with captured timing (gaps capped at %g ms) --"
          % (len(entries), max_gap_ms))
    counts = {}
    replies = {}
    samples = []
    events = []
    started = time.time()

    def drain_for(seconds):
        # Spend the inter-packet gap READING rather than sleeping. Sleeping through it threw
        # away the wheel's own position reports, which are the only objective measure of
        # whether a commanded force did anything.
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return
            data = wheel.read(timeout=max(1, int(left * 1000)))
            if not data:
                continue
            head = decode_header(data)
            if not head:
                continue
            replies[head["command"]] = replies.get(head["command"], 0) + 1
            if head["command"] == GIP_CMD_INPUT:
                position = steering(head["payload"])
                if position is not None:
                    samples.append((time.time() - started, position))
            elif head["options"] & OPT_ACKNOWLEDGE:
                wheel.acknowledge(head)

    for delta, packet in entries:
        gap = min(delta, max_gap_ms) / 1000.0
        if gap > 0:
            drain_for(gap)
        if force_scale is not None and packet[0] == 0x0B and FORCE_DURATION in packet:
            packet, applied = scale_force_block(packet, force_scale)
            if applied is not None:
                print("   force block scaled to %+.2f" % applied)
        if packet[0] == 0x0B and FORCE_DURATION in packet:
            body = packet[4:]
            for index in range(0, 60, 6):
                pid, = struct.unpack_from("<H", body, index)
                if pid == FORCE_SLOT:
                    value, = struct.unpack_from("<f", body, index + 2)
                    events.append((time.time() - started, value))
                    break
        wheel.send_raw(packet)
        counts[packet[0]] = counts.get(packet[0], 0) + 1

    print("   sent: %s" % ", ".join("0x%02x x%d" % kv for kv in sorted(counts.items())))
    print("   device replied: %s"
          % (", ".join("0x%02x x%d" % kv for kv in sorted(replies.items())) or "NOTHING"))

    if not samples:
        print("   no position reports -- cannot measure the effect objectively")
        return entries
    print("\n   MEASURED WHEEL MOVEMENT after each force block (hands off to read this):")
    # Deflection saturates: with nobody holding it, even a tenth of full scale eventually
    # drives the wheel to its end stop, so "how far" cannot separate magnitudes. How FAST it
    # gets there can.
    print("      commanded    peak speed       time to lock")
    for stamp, magnitude in events:
        window = [(t_, p) for t_, p in samples if stamp <= t_ < stamp + 2.5]
        if len(window) < 3:
            print("      %+6.2f       (no samples)" % magnitude)
            continue
        speed = 0.0
        for (t0, p0), (t1, p1) in zip(window, window[1:]):
            if t1 > t0:
                speed = max(speed, abs(p1 - p0) / (t1 - t0))
        locked = next((t_ - stamp for t_, p in window
                       if abs(p - CENTRE) > CENTRE * 0.9), None)
        print("      %+6.2f     %9.0f cnt/s    %s"
              % (magnitude, speed,
                 "%.2f s" % locked if locked is not None else "never"))
    return entries


def drive(wheel, stiffness, seconds, cap=DEFAULT_CAP, rate=60.0, damping=0.0,
          centre=0.0, refresh=0.5, centres=None, gains=None):
    """Arm and hold condition effects using GENERATED sequences -- no capture file.

    Same behaviour as spring(), but every byte comes from gip_arming rather than a replayed
    pcap, so this runs on any of these wheels without someone else's capture. Verified
    byte-identical to the wire capture's arming backbone (62/62) and force block.
    """
    print("\n-- arming (generated, %d packets) --"
          % len(gip_arming.arming_sequence()))
    heard = {}
    seed = None
    for command, body, options, delay in gip_arming.arming_sequence():
        wheel.send(command, body, options=options)
        if delay:
            time.sleep(delay)
        data = wheel.read(timeout=1)
        if data:
            head = decode_header(data)
            if head:
                heard[head["command"]] = heard.get(head["command"], 0) + 1
                if head["command"] == GIP_CMD_INPUT:
                    # Seed the loop with a real position. Input is event-driven, so a loop that
                    # starts blind, commands zero and therefore moves nothing never receives a
                    # first sample -- it deadlocks and looks identical to total failure.
                    seed = steering(head["payload"]) or seed
    print("   device replied during arming: %s"
          % (", ".join("0x%02x x%d" % kv for kv in sorted(heard.items())) or "NOTHING"))

    beat_cmd, beat_body = gip_arming.heartbeat()
    print("\n-- LIVE (generated): spring %.2f, damper %.2f, cap %.2f, %g s --"
          % (stiffness, damping, cap, seconds))
    state = ffb_render.WheelState()
    params = ffb_render.legacy_condition_params("spring", stiffness, offset=centre,
                                                deadband=0.02)
    damper = ffb_render.legacy_condition_params("damper", damping)

    started = time.time()
    next_beat = started
    next_force = started
    position = seed if seed is not None else CENTRE
    if seed is not None:
        state.update((position - CENTRE) / float(CENTRE), started)
        print("   seeded position %+.3f" % ((position - CENTRE) / float(CENTRE)))
    last_sent = None
    last_time = 0.0
    current_slot = -1
    marks = []
    track = []
    worst = 0.0
    loop_heard = {}
    while time.time() - started < seconds:
        now = time.time()
        data = wheel.read(timeout=5)
        if data:
            head = decode_header(data)
            if head:
                loop_heard[head["command"]] = loop_heard.get(head["command"], 0) + 1
            if head and head["command"] == GIP_CMD_INPUT:
                reading = steering(head["payload"])
                if reading is not None:
                    position = reading
                    state.update((position - CENTRE) / float(CENTRE), now)
                    track.append((now - started, state.position))
            elif head and head["options"] & OPT_ACKNOWLEDGE:
                wheel.acknowledge(head)
        if centres:
            slot = min(int((now - started) / (seconds / len(centres))), len(centres) - 1)
            if slot != current_slot:
                current_slot = slot
                centre = centres[slot]
                if gains and slot < len(gains):
                    stiffness = gains[slot]
                params = ffb_render.legacy_condition_params("spring", stiffness,
                                                            offset=centre, deadband=0.02)
                marks.append((now - started, centre, stiffness))
                print("   -> centre %+.2f  gain %+.2f" % (centre, stiffness))
                last_sent = None
        if now >= next_beat:
            wheel.send(beat_cmd, beat_body)
            next_beat = now + 1.0 / 16.0
        if now >= next_force:
            demand = ffb_render.condition_force("spring", params, state)
            if damping:
                demand += ffb_render.condition_force("damper", damper, state)
            demand = max(-cap, min(cap, demand))
            worst = max(worst, abs(demand))
            if last_sent is None or abs(demand - last_sent) > 0.02 or now - last_time > refresh:
                for command, body, options, delay in gip_arming.force_sequence(demand):
                    wheel.send(command, body, options=options)
                    if delay:
                        time.sleep(delay)
                last_sent, last_time = demand, now
            next_force = now + 1.0 / rate

    print("   largest force commanded: %.2f" % worst)
    print("   device replied during the loop: %s"
          % (", ".join("0x%02x x%d" % kv for kv in sorted(loop_heard.items())) or "NOTHING"))
    for index, (start, target, gain) in enumerate(marks):
        stop = marks[index + 1][0] if index + 1 < len(marks) else seconds
        settled = [q for t_, q in track if start + (stop - start) * 0.5 <= t_ < stop]
        if settled:
            mean = sum(settled) / len(settled)
            print("      centre %+.3f  gain %+5.2f   measured %+.3f   error %+.3f   (n=%d)"
                  % (target, gain, mean, mean - target, len(settled)))
        else:
            before = [q for t_, q in track if t_ < start]
            print("      centre %+.3f  gain %+5.2f   NO MOVEMENT, held at %+.3f"
                  % (target, gain, before[-1] if before else float("nan")))
    if not marks and track:
        settled = [q for t_, q in track if t_ > seconds * 0.6]
        if settled:
            print("   SETTLED AT %+.3f  (commanded %+.3f)"
                  % (sum(settled) / len(settled), centre))


def spring(wheel, path, stiffness, seconds, cap=DEFAULT_CAP, rate=60.0, bias=0.0,
           damping=0.0, centre=0.0, refresh=0.5, centres=None, gains=None):
    """Arm from the wire capture, then hold a CENTRING SPRING of our own.

    This is the point of the whole exercise: not replaying a recording, but closing the loop.
    The wheel reports its own position (16-bit LE at input bytes 2-3, centred on 0x8000) all
    the way through a wire session, because that session never sends the `identify` that
    silences the input stream. So we can read where the wheel is and command a force against
    it, at whatever stiffness we like -- which is a spring, and a light one is exactly what the
    factory's immovable centring spring refuses to be.

    Arming is replayed verbatim up to the first force block, because the arming is the part we
    still cannot construct from scratch. After that every force block is ours: the captured
    block reused as a template with slot 0x08 rewritten, plus the 0x0c heartbeat the session
    keeps up at ~16 Hz.
    """
    entries = []
    with open(path, encoding="ascii") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            delta, packet = line.split(" ", 1)
            entries.append((float(delta), bytes.fromhex(packet)))

    first_force = next(i for i, (_, p) in enumerate(entries)
                       if p[0] == 0x0B and FORCE_DURATION in p)
    template = entries[first_force][1]
    # The FIRST 0x0c in the session is STATE_CLEAR, which tells the wheel no effect is
    # loaded -- heartbeating that at 16 Hz kept the firmware spring alive and our force
    # inert. The steady state is 0xf0 STATE_RUNNING, 304 of the 455 on the wire.
    beat = next(p for _, p in entries if p[0] == 0x0C and p[4] == 0xF0)

    # Replay the WHOLE session, not just up to the first force block: 20 of the 24 0x0d table
    # uploads happen after it, so stopping early leaves the device half-configured -- which is
    # exactly what a 0.30 demand that moved nothing looked like.
    print("\n-- arming from the wire capture (%d packets) --" % len(entries))
    for delta, packet in entries:
        gap = min(delta, 400.0) / 1000.0
        if gap > 0:
            time.sleep(gap)
        wheel.send_raw(packet)
        wheel.read(timeout=1)

    print("\n-- LIVE CONDITION EFFECTS: spring %.2f, damper %.2f, cap %.2f, %g s --"
          % (stiffness, damping, cap, seconds))
    print("   turn the wheel; it should pull back to centre and resist fast movement")

    # Reuse the project's own tested control laws rather than a hand-rolled -k*x. ffb_render
    # imports only math, so it runs here unchanged, and it brings the deadband, saturations
    # and velocity smoothing that were fitted to this wheel.
    state = ffb_render.WheelState()
    # A force command is not one message. Around EVERY force block in the capture:
    #     0c 00 CLEAR x2 | 0d table chunk x4 | 0b magnitude | 0c 10 | 0c f0 running
    # That is why the session carries 0x0d x24 -- six force events times four chunks. Sending
    # the 0b alone, which is all we did at first, drops a magnitude into a cleared device.
    clear = next(p for _, p in entries if p[0] == 0x0C and p[4] == 0x00)
    state10 = next(p for _, p in entries if p[0] == 0x0C and p[4] == 0x10)
    table = [p for _, p in entries[:first_force] if p[0] == 0x0D][-4:]
    if len(table) != 4:
        table = [p for _, p in entries if p[0] == 0x0D][:4]

    # offset IS the spring's centre, per ffb_render's own docstring. A non-zero centre
    # turns the spring into a position hold, which can be checked against the position
    # the wheel reports instead of against anyone's impression of the force.
    spring_params = ffb_render.legacy_condition_params("spring", stiffness,
                                                      offset=centre, deadband=0.02)
    damper_params = ffb_render.legacy_condition_params("damper", damping)

    started = time.time()
    next_beat = started
    next_force = started
    position = CENTRE
    worst = 0.0
    last_sent = None
    current_slot = -1
    marks = []
    last_time = 0.0
    sequence = template[2]
    track = []
    while time.time() - started < seconds:
        now = time.time()
        data = wheel.read(timeout=5)
        if data:
            head = decode_header(data)
            if head and head["command"] == GIP_CMD_INPUT:
                reading = steering(head["payload"])
                if reading is not None:
                    position = reading
                    state.update((position - CENTRE) / float(CENTRE), now)
                    track.append((now - started, state.position))
            elif head and head["options"] & OPT_ACKNOWLEDGE:
                wheel.acknowledge(head)
        if now >= next_beat:
            wheel.send_raw(beat)
            next_beat = now + 1.0 / 16.0
        if centres:
            # Step the commanded centre through a list, dwelling on each. Commanded versus
            # measured position is the honest test of control: no hands, no impressions.
            slot = int((now - started) / (seconds / len(centres)))
            slot = min(slot, len(centres) - 1)
            if slot != current_slot:
                current_slot = slot
                centre = centres[slot]
                # A parallel gain list lets one run change ONLY the stiffness while the target
                # stays put -- which is how we ask whether the firmware spring can be
                # cancelled, rather than asking anyone how it feels.
                if gains and slot < len(gains):
                    stiffness = gains[slot]
                spring_params = ffb_render.legacy_condition_params(
                    "spring", stiffness, offset=centre, deadband=0.02)
                marks.append((now - started, centre, stiffness))
                print("   -> centre %+.2f  gain %+.2f" % (centre, stiffness))
                last_sent = None
        if now >= next_force:
            demand = bias
            if stiffness:
                demand += ffb_render.condition_force("spring", spring_params, state)
            if damping:
                demand += ffb_render.condition_force("damper", damper_params, state)
            demand = max(-cap, min(cap, demand))
            worst = max(worst, abs(demand))
            # Send ONLY when the magnitude changes, as the Windows driver does: the capture
            # holds 6 force blocks against 455 heartbeats. Each block carries a duration and a
            # start flag, so re-sending one 60 times a second plausibly restarts the effect
            # before it can do anything -- which is what a 0.30 demand that moved nothing
            # looked like.
            # On change, but also refreshed periodically: one block for a constant demand may
            # simply lapse, while 60 Hz re-issue appears to restart the effect before it runs.
            if (last_sent is None or abs(demand - last_sent) > 0.02
                    or now - last_time > refresh):
                for packet in (clear, clear, *table):
                    sequence = (sequence + 1) & 0xFF or 1
                    wheel.send_raw(bytes(packet[:2]) + bytes([sequence]) + packet[3:])
                    time.sleep(0.004)
                sequence = (sequence + 1) & 0xFF or 1
                wheel.send_raw(set_force_magnitude(template, demand, sequence))
                time.sleep(0.008)
                sequence = (sequence + 1) & 0xFF or 1
                wheel.send_raw(bytes(state10[:2]) + bytes([sequence]) + state10[3:])
                last_sent = demand
                last_time = now
            next_force = now + 1.0 / rate
    print("   largest force commanded: %.2f" % worst)
    if track and marks:
        print("\n   COMMANDED vs MEASURED position:")
        for index, (start, target, gain) in enumerate(marks):
            stop = marks[index + 1][0] if index + 1 < len(marks) else seconds
            settled = [q for t_, q in track if start + (stop - start) * 0.5 <= t_ < stop]
            if settled:
                mean = sum(settled) / len(settled)
                print("      centre %+.3f  gain %+5.2f   measured %+.3f   error %+.3f   (n=%d)"
                      % (target, gain, mean, mean - target, len(settled)))
            else:
                # Input is event-driven, so silence means the wheel did not move. That is a
                # result: with gain 0 it says the firmware spring is NOT dragging it back.
                before = [q for t_, q in track if t_ < start]
                held = before[-1] if before else float("nan")
                print("      centre %+.3f  gain %+5.2f   NO MOVEMENT, held at %+.3f"
                      % (target, gain, held))
        return
    if track:
        print("   commanded centre: %+.3f" % centre)
        for window in (0, 1, 2, 3, 4):
            low, high = window * seconds / 5.0, (window + 1) * seconds / 5.0
            chunk = [p for t_, p in track if low <= t_ < high]
            if chunk:
                print("      t=%4.1f..%4.1fs   mean position %+.3f   (n=%d)"
                      % (low, high, sum(chunk) / len(chunk), len(chunk)))
        settled = [p for t_, p in track if t_ > seconds * 0.6]
        if settled:
            print("   SETTLED AT %+.3f  (commanded %+.3f, error %+.3f)"
                  % (sum(settled) / len(settled), centre,
                     sum(settled) / len(settled) - centre))


def force_raw(wheel, path, hold, beat=True):
    """Send force blocks captured from the REAL WIRE, verbatim.

    THE BUG THIS EXISTS FOR (2026-09-22): `logs/wgi.txt` was captured at the driver interface,
    where WGI writes a SPARSE update -- one populated slot plus 0xffff padding meaning "slot
    unused". The driver expands that into a full ten-slot effect block before it reaches the
    device. We were replaying the sparse form straight at the wheel.

        WGI -> driver:  08=0.25  ---- ---- ---- ---- ---- ---- ---- ---- ----
        driver -> wire: 00=0 01=30000 02=int1 03=-1 04=0 05=1 06=-1 07=0 08=+1 09=1

    The arming blocks are byte-identical between the two; only the force message is rewritten,
    which is exactly the message that never worked. 0x08 carries the signed magnitude and flips
    between pulses, matching the LEFT/RIGHT/LEFT/RIGHT/LEFT direction test that produced real
    torque on Windows.
    """
    with open(path, encoding="ascii") as handle:
        packets = [bytes.fromhex(line.strip()) for line in handle if line.strip()]
    print("\n-- sending %d captured WIRE force block(s), HANDS ON THE WHEEL --" % len(packets))
    for index, packet in enumerate(packets):
        wheel.send_raw(packet)
        print("   wire force block #%d (%d B)" % (index, len(packet)))
        if hold:
            pump(wheel, hold, heartbeat=beat)
    return packets


def hori_probe(wheel, profile=1, offset=0, count=51, read_ms=80, drain_cap=32):
    """Ask the wheel for its PROFILE MEMORY over HORI's own config channel.

    From mbenkmann/hori_device_manager, who traced the HORI Device Manager talking to a
    Fighting Commander Octa (Wireshark on a Linux host, Windows in a QEMU VM):

        HOST>  0f 00 <seq> 3c | 04 <profile> <off_hi> <off_lo> <count> 00...
        <PAD   10 00 <seq> 3c | 05 <profile> <off_hi> <off_lo> <count> <data...>

    hori_cmd 0x04 reads profile memory, 0x05 is the reply, 0x03 writes it. Crucially this
    channel involves **no authentication** -- it is HORI's, not Microsoft's, which is why it is
    worth trying after 0x06 closed the door on commanding the motor directly.

    Our wheel's own descriptor declares exactly two command types we have never seen used:
    0x0e (max 56) and 0x10 (max 60). That is the same shape as the Octa's 0x0f/0x10 pair, so
    try both host ids and see which the wheel answers.

    Read-only: this only issues hori_cmd 0x04.
    """
    print("\n-- probing HORI's config channel (read profile %d at 0x%04x) --"
          % (profile, offset))
    found = []
    for command, body_len in ((0x0E, 56), (0x0F, 60), (0x10, 60)):
        body = bytearray(body_len)
        body[0] = 0x04                       # hori_cmd: read profile memory
        body[1] = profile
        body[2] = (offset >> 8) & 0xFF
        body[3] = offset & 0xFF
        body[4] = min(count, body_len - 5)
        wheel.send(command, bytes(body))
        time.sleep(0.05)
        replies = []
        for _ in range(drain_cap):
            data = wheel.read(timeout=read_ms)
            if not data:
                break
            head = decode_header(data)
            if not head or head["command"] == GIP_CMD_INPUT:
                continue
            replies.append(bytes(data))
        print("   host 0x%02x (%d B body) -> %s"
              % (command, body_len,
                 "no reply" if not replies else "%d repl(y/ies)" % len(replies)))
        for packet in replies[:4]:
            print("      %s" % packet[:40].hex())
            if packet[0] in (0x0F, 0x10) and len(packet) > 5 and packet[4] == 0x05:
                print("      *** HORI PROFILE MEMORY REPLY -- the channel is open ***")
                found.append((command, packet))
    return found


def replay_auth(wheel, path, read_ms=60, gap=0.05, drain_cap=64):
    """Replay a captured host-side 0x06 AUTHENTICATE exchange, verbatim.

    THE FINDING THIS EXISTS FOR (2026-09-22): a real USB wire capture shows Windows completing
    a full mutual authentication with this wheel -- the device streams an X.509 certificate
    (subject "Xbox", valid to 2043) across ~20 chunks and the host answers with its own chunked
    response -- and it finishes ONE MESSAGE before `logs/wgi.txt` begins. Our reference capture
    sat above the driver, and the driver is what authenticates, so 0x06 could never appear in
    it. That is why this was wrongly retired as a dead hypothesis.

    It explains the central symptom exactly: parameter writes are accepted (the factory spring
    drops, 0x25 goes 06 00 -> 06 02) but the motor never actuates. Configuration is
    unprivileged; actuation is not.

    WHAT THIS TESTS, and its likely answer: if the exchange is stateless, replaying the host's
    twelve packets works. A certificate exchange normally signs a fresh nonce, in which case
    this fails and raw USB is blocked by design rather than by a bug. One run settles it.
    """
    with open(path, encoding="ascii") as handle:
        packets = [bytes.fromhex(line.strip()) for line in handle if line.strip()]

    print("\n-- replaying %d captured 0x06 AUTHENTICATE packet(s) --" % len(packets))
    replies = {}
    said = []
    for index, packet in enumerate(packets):
        wheel.send_raw(packet)
        time.sleep(gap)
        for _ in range(drain_cap):
            data = wheel.read(timeout=read_ms)
            if not data:
                break
            head = decode_header(data)
            if not head:
                continue
            replies[head["command"]] = replies.get(head["command"], 0) + 1
            if head["command"] != GIP_CMD_INPUT:
                said.append((index, bytes(data)))
            # A CHUNK_START carries the TOTAL in chunk_offset, so record it before acking;
            # acknowledge() needs it to report how much is still outstanding.
            if head["options"] & OPT_CHUNK_START:
                wheel.chunk_total = head["chunk_offset"]
            if head["options"] & OPT_ACKNOWLEDGE:
                wheel.acknowledge(head)

    print("   device replied: %s"
          % (", ".join("0x%02x x%d" % kv for kv in sorted(replies.items())) or "NOTHING"))
    answered = [p for i, p in said if p and p[0] == GIP_CMD_AUTHENTICATE]
    print("   0x06 messages back from the device: %d" % len(answered))
    for index, packet in said[:14]:
        print("      after auth pkt #%d: %s" % (index, packet[:28].hex()))
    return replies


def wait_calibration(wheel, cap=30.0, quiet_needed=2.0, settle=2.0):
    """Wait for the firmware calibration sweep to finish before arming.

    Claiming the device restarts it -- measured, not assumed: on an already-settled wheel,
    claim plus power-on made it drive itself to both locks for about 8 s while reporting
    NOTHING, then resume input in a burst covering the full steering range. The old flow
    issued its first force command at roughly t=6 s, squarely inside that window, which is a
    far better explanation for the silent motor than anything we send.

    Detected rather than slept through, because the duration is a property of the wheel and we
    would only be guessing: wait for the input stream to go quiet (the sweep starting), then
    for it to come back (the sweep finished), then a settle margin. The cap is generous on
    purpose -- there is no prize for arming early.
    """
    print("\n-- waiting for the calibration sweep (cap %g s) --" % cap)
    started = time.time()
    last_input = started
    saw_quiet = False
    finished = None
    seen = []
    while time.time() - started < cap:
        data = wheel.read(timeout=50)
        now = time.time()
        if data:
            head = decode_header(data)
            if head and head["options"] & OPT_ACKNOWLEDGE:
                wheel.acknowledge(head, received=head["length"])
            if head and head["command"] == GIP_CMD_INPUT:
                if saw_quiet:
                    finished = now - started
                    break
                last_input = now
        elif not saw_quiet and now - last_input >= quiet_needed:
            saw_quiet = True
            print("   input stopped at t=%.1f s -- the wheel is calibrating"
                  % (last_input - started))

    if finished is None:
        print("   NO completion seen within %g s -- arming anyway, treat a silent motor here "
              "as unexplained" % cap)
    else:
        # Silence alone does NOT prove a sweep happened; it only proves the device stopped
        # talking. A real sweep drives to both locks, so the burst that follows covers most of
        # the range. Measure that, because a detector reporting success on a wheel the operator
        # can see standing still is worse than no detector.
        print("   input resumed at t=%.1f s" % finished)
        burst = time.time()
        while time.time() - burst < 0.5:
            data = wheel.read(timeout=50)
            if not data:
                continue
            head = decode_header(data)
            if head and head["options"] & OPT_ACKNOWLEDGE:
                wheel.acknowledge(head, received=head["length"])
            if head and head["command"] == GIP_CMD_INPUT:
                reading = steering(head["payload"])
                if reading is not None:
                    seen.append((reading - CENTRE) / float(CENTRE))
        travel = (max(seen) - min(seen)) if seen else 0.0
        if travel > 0.5:
            print("   travel %.2f of full scale -- the wheel really swept" % travel)
        else:
            print("   travel %.2f of full scale -- NO SWEEP SEEN. The device went quiet "
                  "without moving, so it was probably already calibrated. Treat this run's "
                  "starting state as unknown." % travel)
    if settle:
        pump(wheel, settle)
    return finished


def command_force(wheel, magnitude, hold, beat=False):
    """Command a force and report what the wheel actually does while it is applied.

    Reading the position back is the only way to tell a torque from a position target: under a
    steady torque a free wheel keeps travelling to the lock, under a position target it parks
    at a value proportional to what was commanded. Asking a human which of those they felt is
    not a measurement.
    """
    # force_block is the one field in this protocol that round-tripped across five magnitudes.
    body = gip_protocol.force_block(magnitude)
    wheel.send(GIP_CMD_HID_REPORT, body)
    print("   force %+.2f  (%d byte body)" % (magnitude, len(body)))
    if not hold:
        return

    started = time.time()
    next_beat = started
    next_short = started
    samples = []
    said = []
    while time.time() - started < hold:
        now = time.time()
        if beat and now >= next_beat:
            wheel.send(gip_protocol_state(), BEAT_STATE[0])
            next_beat = now + 1.0 / 16.0
        if beat and now >= next_short:
            wheel.send(GIP_CMD_LED, gip_protocol.SHORT_CMD)
            next_short = now + 2.0
        data = wheel.read(timeout=20)
        if not data:
            continue
        head = decode_header(data)
        if not head:
            continue
        if head["options"] & OPT_ACKNOWLEDGE:
            wheel.acknowledge(head, received=head["length"])
        if head["command"] == GIP_CMD_INPUT:
            position = steering(head["payload"])
            if position is not None:
                samples.append((now - started, position))
        else:
            # Anything that is not input is the device commenting on what we just asked it to
            # do. The 0x25 status byte moved 0x00 -> 0x02 during arming, so this is where an
            # "armed", "refused" or "out of range" answer would show up.
            said.append((now - started, bytes(data)))

    for stamp, packet in said[:8]:
        print("      %6.2fs  device said: %s" % (stamp, packet[:28].hex()))
    if not samples:
        print("      (the wheel reported no position at all during the hold)")
        return
    print("      t       raw   offset   position")
    bucket = 0.25
    slot = 0.0
    while slot < hold:
        window = [p for t, p in samples if slot <= t < slot + bucket]
        slot += bucket
        if not window:
            continue
        mean = sum(window) // len(window)
        offset = mean - CENTRE
        column = 24 + max(-24, min(24, offset * 24 // CENTRE))
        bar = " " * column + "|"
        print("   %6.2fs  %5d  %+6d   %s" % (slot - bucket, mean, offset, bar))


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
    parser.add_argument("--sequence", metavar="LIST",
                        help="comma-separated magnitudes to command in order, such as "
                             "0,0.1,0,-0.1,0 -- interleave zeros so the run carries "
                             "its own baseline")
    parser.add_argument("--replay-wire", metavar="FILE",
                        help="replay a host->device stream captured from the real wire, "
                             "with its original timing (logs/wire_session.txt)")
    parser.add_argument("--drive", type=float, metavar="K",
                        help="arm from GENERATED sequences (no capture file) and "
                             "hold a spring of this stiffness")
    parser.add_argument("--spring", type=float, metavar="K",
                        help="arm from --replay-wire, then hold a live centring spring "
                             "of this stiffness (try 0.15)")
    parser.add_argument("--kill-spring", metavar="FILE",
                        help="before arming, replay the old wgi.txt sequence that ends "
                             "on 0c 20 and suppresses the firmware centring spring")
    parser.add_argument("--centres", metavar="LIST",
                        help="step the spring centre through these targets, e.g. \"0,0.3,-0.3,0\"")
    parser.add_argument("--gains", metavar="LIST",
                        help="stiffness per --centres step, e.g. \"2,0,-1\"")
    parser.add_argument("--centre", type=float, default=0.0,
                        help="where the spring pulls to, -1..+1 (0 = centre)")
    parser.add_argument("--damper", type=float, default=0.0,
                        help="damper coefficient held alongside the spring")
    parser.add_argument("--bias", type=float, default=0.0,
                        help="constant force added to the spring, to test whether the "
                             "firmware spring is suppressed only while an effect is held")
    parser.add_argument("--spring-seconds", type=float, default=20.0,
                        help="how long to hold the spring (default 20)")
    parser.add_argument("--force-scale", type=float, metavar="MAG",
                        help="rewrite slot 0x08 of each replayed force block to this "
                             "magnitude, keeping its sign (0 = control run)")
    parser.add_argument("--max-gap-ms", type=float, default=400.0,
                        help="cap replayed inter-packet gaps (default 400)")
    parser.add_argument("--force-raw", metavar="FILE",
                        help="after arming, send force blocks captured from the real "
                             "wire verbatim (logs/force_wire.txt)")
    parser.add_argument("--hori-probe", action="store_true",
                        help="ask the wheel for profile memory over HORI's own config "
                             "channel -- read-only, and needs no authentication")
    parser.add_argument("--hori-offset", type=lambda v: int(v, 0), default=0,
                        help="profile memory offset for --hori-probe (default 0)")
    parser.add_argument("--replay-auth", metavar="FILE",
                        help="replay a captured host-side 0x06 AUTHENTICATE exchange "
                             "verbatim before arming (logs/auth_out.txt)")
    parser.add_argument("--skip-identify", action="store_true",
                        help="do not send 0x04 before arming -- identify silences the "
                             "input stream, so position cannot be read while armed")
    parser.add_argument("--reset", action="store_true",
                        help="USB-reset the device after claiming it, forcing a real "
                             "bring-up instead of inheriting the kernel driver's session")
    parser.add_argument("--wait-calibration", type=float, default=0.0, metavar="CAP",
                        help="before arming, wait for the firmware calibration sweep to "
                             "finish, giving up after CAP seconds (try 30)")
    parser.add_argument("--ack-arming", action="store_true",
                        help="acknowledge device messages during the replay, as identify() and "
                             "pump() already do -- the arming has always ignored them")
    parser.add_argument("--arm-read-ms", type=int, default=5,
                        help="per-read timeout while draining during the replay (default 5)")
    parser.add_argument("--trace-arming", metavar="FILE",
                        help="write both directions with timestamps for the whole run")
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
                  dump=bool(args.dump_sent), reattach=not args.no_reattach,
                  trace=bool(args.trace_arming))
    print("found %04x:%04x%s" % (HORI_VID, HORI_PID, "  [padding OUT to 64 B]" if args.pad else ""))
    wheel.open()
    try:
        if args.reset:
            wheel.reset_device()
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

        if args.probe or not (args.arm or args.force is not None or args.led or args.identify
                              or args.sequence or args.hori_probe or args.force_raw
                              or args.replay_wire or args.drive is not None):
            # Never block on input here: a prompt mid-measurement stops the sampling loop, and
            # this runs over ssh with no tty. Phases are separate invocations, each started
            # once the user is ready -- pass --label to say which is which.
            probe(wheel, args.seconds, args.label)

        if (args.arm or args.force is not None) and not args.replay:
            arm(wheel)
            print("   settling")
            pump(wheel, 3.0, heartbeat=args.heartbeat)

        if args.drive is not None:
            if ffb_render is None:
                raise SystemExit("ffb_render.py must be importable")
            if args.wait_calibration:
                wait_calibration(wheel, cap=args.wait_calibration)
            drive(wheel, args.drive, args.spring_seconds, cap=args.cap,
                  damping=args.damper, centre=args.centre,
                  centres=[float(v) for v in args.centres.split(",")] if args.centres else None,
                  gains=[float(v) for v in args.gains.split(",")] if args.gains else None)

        elif args.replay_wire and args.spring is not None:
            if args.wait_calibration:
                wait_calibration(wheel, cap=args.wait_calibration)
            if args.kill_spring:
                # The firmware's centring spring comes back with every calibration, and the
                # calibration runs on every claim. The old (malformed) arming ends on 0c 20
                # STATE_LOADED -- a state that never appears on the wire -- and that
                # persistently suppresses the spring. So kill it AFTER calibrating, then arm
                # properly and hold a spring of our own.
                print("\n-- suppressing the firmware spring (old arming, ends on 0c 20) --")
                BEAT_STATE[0] = gip_protocol.STATE_LOADED
                replay(wheel, args.kill_spring, 107, 2.0, None)
                pump(wheel, 2.0, heartbeat=True)
                BEAT_STATE[0] = gip_protocol.STATE_RUNNING
            if ffb_render is None:
                raise SystemExit('ffb_render.py must sit beside this file')
            spring(wheel, args.replay_wire, args.spring, args.spring_seconds,
                   cap=args.cap, bias=args.bias, damping=args.damper,
                   centre=args.centre,
                   centres=[float(v) for v in args.centres.split(',')]
                   if args.centres else None,
                   gains=[float(v) for v in args.gains.split(',')]
                   if args.gains else None)

        elif args.replay_wire:
            # Let the wheel finish calibrating first, so nothing in the force phase can be
            # mistaken for the firmware's own sweep. That confusion has cost this project a
            # whole day of wrong conclusions once already.
            if args.wait_calibration:
                wait_calibration(wheel, cap=args.wait_calibration)
            replay_wire(wheel, args.replay_wire, max_gap_ms=args.max_gap_ms,
                        force_scale=args.force_scale)

        if args.replay:
            # Always bring the device up before replaying; --announce-wait only decides
            # whether we let it introduce itself first.
            if args.announce_wait:
                await_announce(wheel, args.announce_wait)
            wheel.power_on()
            time.sleep(0.3)
            # Before identify, not after: identify silences the input stream entirely -- 30 s
            # of nothing, measured twice -- and input resuming is the only completion signal
            # we have for the calibration sweep.
            if args.wait_calibration:
                wait_calibration(wheel, cap=args.wait_calibration)
            if args.skip_identify:
                print("\n-- skipping identify: it silences the input stream --")
                pump(wheel, 1.0)
            else:
                identify(wheel, 3.0, ack=True)
            if args.hori_probe:
                hori_probe(wheel, offset=args.hori_offset)
            if args.replay_auth:
                replay_auth(wheel, args.replay_auth)
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
            replay(wheel, args.replay, args.replay_until, args.gap_ms, None,
                   ack=args.ack_arming, read_ms=args.arm_read_ms)
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

        elif args.force_raw:
            force_raw(wheel, args.force_raw, args.hold, beat=args.heartbeat)

        elif args.sequence:
            # Zeros interleaved with magnitudes so the run carries its own baseline: the
            # question is never "did the wheel move" but "did it move differently while a
            # force was commanded than while zero was".
            magnitudes = [float(step) for step in args.sequence.split(",")]
            over = [m for m in magnitudes if abs(m) > args.cap]
            if over:
                raise SystemExit("magnitude(s) %s exceed the --cap of %.2f"
                                 % (over, args.cap))
            print("\n-- commanding force sequence %s --" % magnitudes)
            for magnitude in magnitudes:
                command_force(wheel, magnitude, args.hold, beat=args.heartbeat)

        elif args.force is not None:
            print("\n-- commanding force, HANDS ON THE WHEEL --")
            magnitudes = [args.force, -args.force, args.force] if args.sweep else [args.force]
            for magnitude in magnitudes:
                command_force(wheel, magnitude, args.hold, beat=args.heartbeat)
    finally:
        if args.trace_arming and wheel.trace is not None:
            base = wheel.trace[0][1] if wheel.trace else 0.0
            with open(args.trace_arming, "w", encoding="ascii") as handle:
                for direction, stamp, packet in wheel.trace:
                    handle.write("%9.3f %s %s\n"
                                 % ((stamp - base) * 1000.0, direction, packet.hex()))
            print("\n   wrote %d traced event(s) to %s"
                  % (len(wheel.trace), args.trace_arming))
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
