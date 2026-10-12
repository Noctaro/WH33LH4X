"""
Be the GIP host over raw USB: claim the wheel's interface and exchange messages.

Needs pyusb, and the wheel bound to WinUSB on Windows or free of xone on Linux. See
evidence/RAW_USB.md.
"""

import struct
import time

try:
    import usb.core
    import usb.util
except ImportError as exc:
    raise ImportError("pyusb missing: pip install pyusb (Linux: apt install python3-usb)") \
        from exc

from gip.wire import (
    EP_IN,
    EP_OUT,
    GIP_CMD_ACKNOWLEDGE,
    GIP_CMD_POWER,
    HORI_PID,
    HORI_VID,
    INTERFACE,
    OPT_CHUNK,
    OPT_CHUNK_START,
    OPT_INTERNAL,
    PACKET,
    encode_header,
)

# Windows has no system libusb; libusb_package ships one.
try:
    import libusb_package

    def _backend():
        return libusb_package.get_libusb1_backend()
except ImportError:
    def _backend():
        return None


class WheelNotFound(RuntimeError):
    """No HORI wheel on the bus, or it did not come back after a reset."""


def find():
    """The wheel's pyusb device, or None."""
    return usb.core.find(idVendor=HORI_VID, idProduct=HORI_PID, backend=_backend())


class Wheel:
    def __init__(self, verbose=True, pad=False, wait=0.0, client=0, zlp=False, dump=False,
                 reattach=True, trace=False):
        self.verbose = verbose
        self.trace = [] if trace else None      # (direction, time, packet) both ways
        # CONSTRAINT: a run that reattaches the kernel driver leaves the next run with no
        # torque (RAW_USB.md).
        self.reattach = reattach
        self.pad = pad
        self.zlp = zlp
        self.dump = [] if dump else None        # every packet as sent, header included
        self.client = client
        self.chunk_total = 0
        self.sequence = 1
        self.detached = False
        self.dev = None
        deadline = time.time() + wait
        while True:
            self.dev = find()
            if self.dev is not None or time.time() > deadline:
                break
            time.sleep(0.05)
        if self.dev is None:
            raise WheelNotFound("wheel %04x:%04x not found; plugged in and in Xbox mode?"
                                % (HORI_VID, HORI_PID))

    def log(self, message):
        if self.verbose:
            print(message, flush=True)

    def open(self):
        # Windows has no kernel-driver detach; the WinUSB binding is made out of band.
        try:
            if self.dev.is_kernel_driver_active(INTERFACE):
                self.log("  detaching kernel driver from interface %d" % INTERFACE)
                self.dev.detach_kernel_driver(INTERFACE)
                self.detached = True
        except NotImplementedError:
            pass
        usb.util.claim_interface(self.dev, INTERFACE)
        self.log("  interface %d claimed" % INTERFACE)

    def reset_device(self):
        """Reset and reclaim, for a genuine bring-up instead of xone's inherited session."""
        self.log("  resetting the device for a genuine bring-up")
        try:
            self.dev.reset()
        except usb.core.USBError as exc:
            self.log("  reset failed: %s" % exc)
        usb.util.dispose_resources(self.dev)
        self.detached = False
        deadline = time.time() + 15.0
        while time.time() < deadline:
            time.sleep(0.25)
            found = find()
            if found is not None:
                self.dev = found
                break
        else:
            raise WheelNotFound("the device did not come back after the reset")
        self.log("  device re-enumerated")
        self.open()

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
        """One GIP message on the interrupt OUT endpoint. Returns the bytes written."""
        seq = self.sequence if sequence is None else sequence
        # The options low nibble is the client id.
        header = encode_header(command, options | (self.client & 0x0F), seq, len(body))
        packet = header + body
        if self.pad and len(packet) < PACKET:
            packet = packet + b"\x00" * (PACKET - len(packet))
        if sequence is None:
            self.sequence = (self.sequence + 1) & 0xFF or 1  # xone never uses sequence 0
        if self.trace is not None:
            self.trace.append(("tx", time.monotonic(), bytes(packet)))
        if self.dump is not None:
            self.dump.append(bytes(packet))
        written = self.dev.write(EP_OUT, packet, timeout=1000)
        # A transfer of exactly wMaxPacketSize needs a zero-length packet to end it on WinUSB.
        if self.zlp and len(packet) % PACKET == 0:
            try:
                self.dev.write(EP_OUT, b"", timeout=200)
            except usb.core.USBError as exc:
                self.log("  ZLP failed: %s" % exc)
        if written != len(packet):
            self.log("  SHORT WRITE: %d of %d bytes for command 0x%02x"
                     % (written, len(packet), command))
        return written

    def send_raw(self, packet):
        """Write a captured packet verbatim: no reframing, no sequence of ours."""
        if self.trace is not None:
            self.trace.append(("tx", time.monotonic(), bytes(packet)))
        if self.dump is not None:
            self.dump.append(bytes(packet))
        written = self.dev.write(EP_OUT, packet, timeout=1000)
        if written != len(packet):
            self.log("  SHORT WRITE: %d of %d bytes" % (written, len(packet)))
        return written

    def power_on(self):
        """xpad's init packet 05 20 00 01 00. Without it the wheel only repeats ANNOUNCE."""
        self.send(GIP_CMD_POWER, b"\x00", options=OPT_INTERNAL, sequence=0)
        self.log("  power-on sent (05 20 00 01 00)")

    def acknowledge(self, head, received=None):
        """
        Reply to a message that set OPT_ACKNOWLEDGE, in xone's gip_acknowledge_pkt format.
        Unacknowledged, the device stops after its first chunk.
        """
        # On CHUNK_START, chunk_offset is the TOTAL size, so callers pass what has arrived.
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
        """One input packet, or None on timeout or error."""
        try:
            return bytes(self.dev.read(EP_IN, PACKET, timeout=timeout))
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError:
            return None
