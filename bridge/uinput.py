"""uinput as the game-facing wheel on Linux: axes and buttons in, evdev force effects out."""

import ctypes
import math
import queue
import select
import threading
import time

import ffb_render as render
import probe_log as log

# linux/input-event-codes.h and linux/input.h; the kernel ABI, so fixed.
FF_PERIODIC, FF_CONSTANT, FF_SPRING, FF_FRICTION = 0x51, 0x52, 0x53, 0x54
FF_DAMPER, FF_INERTIA, FF_RAMP = 0x55, 0x56, 0x57
FF_SQUARE, FF_TRIANGLE, FF_SINE, FF_SAW_UP, FF_SAW_DOWN = 0x58, 0x59, 0x5A, 0x5B, 0x5C
FF_GAIN, FF_AUTOCENTER = 0x60, 0x61

# evdev effect type (and periodic waveform) -> the kind names ffb_render uses.
EFFECT_KINDS = {FF_CONSTANT: "constant", FF_RAMP: "ramp", FF_SPRING: "spring",
                FF_DAMPER: "damper", FF_INERTIA: "inertia", FF_FRICTION: "friction"}
WAVEFORMS = {FF_SQUARE: "square", FF_TRIANGLE: "triangle", FF_SINE: "sine",
             FF_SAW_UP: "sawtoothup", FF_SAW_DOWN: "sawtoothdown"}
FF_CODES = sorted(EFFECT_KINDS) + [FF_PERIODIC] + sorted(WAVEFORMS) + [FF_GAIN, FF_AUTOCENTER]

LEVEL_FULL = 32767.0        # s16 levels, magnitudes, coefficients and centre
SATURATION_FULL = 65535.0   # u16 saturation and dead band; SDL2 passes them unscaled
TURN = 65536.0              # direction and phase: 0x10000 is a full circle

# vJoy's IDs, so a game set up for the Windows bridge recognises this device too; see GAMES.md.
NAME = "WH33LH4X virtual wheel"
VENDOR = 0x1234
PRODUCT = 0xBEAD
BUTTON_COUNT = 10           # RawUsbWheel.BUTTONS
STEERING_FULL = 32767
PEDAL_FULL = 65535


def ioctl_number(direction, nr, size):
    """_IOC for the uinput ('U') ioctls: direction 1 is write, 3 is read and write."""
    return (direction << 30) | (size << 16) | (ord("U") << 8) | nr


def direction_x(raw):
    """
    X multiplier of an evdev direction, -1.0 .. +1.0.

    CONSTRAINT: 0x4000 points left, which is where SDL puts every steering effect, so a
    positive level there pushes the reading negative. Measured in native DiRT 4 on
    2026-09-26: level sign matched position sign in 285 of 349 hand-steered samples.
    """
    return -math.sin(2.0 * math.pi * (raw % 0x10000) / TURN)


def envelope_fraction(level, peak):
    """An evdev envelope level is absolute; ffb_render wants a fraction of the peak."""
    return level / LEVEL_FULL / peak if peak > 0.0 else 1.0


def condition_params(block):
    """One evdev condition block in normalised units. The dead band is its full width."""
    return render.ConditionParams(
        offset=block["center"] / LEVEL_FULL,
        pos_coeff=block["right_coeff"] / LEVEL_FULL,
        neg_coeff=block["left_coeff"] / LEVEL_FULL,
        pos_saturation=block["right_saturation"] / SATURATION_FULL,
        neg_saturation=block["left_saturation"] / SATURATION_FULL,
        deadband=2.0 * block["deadband"] / SATURATION_FULL)


def effect_fields(effect):
    """Copy an evdev ff_effect into a plain dict before the upload is answered."""
    fields = {"type": effect.type, "id": effect.id, "direction": effect.direction,
              "length": effect.ff_replay.length, "delay": effect.ff_replay.delay}
    u = effect.u
    if effect.type == FF_CONSTANT:
        fields["level"] = u.ff_constant_effect.level
        fields["envelope"] = _envelope(u.ff_constant_effect.ff_envelope)
    elif effect.type == FF_RAMP:
        fields["start"] = u.ff_ramp_effect.start_level
        fields["end"] = u.ff_ramp_effect.end_level
        fields["envelope"] = _envelope(u.ff_ramp_effect.ff_envelope)
    elif effect.type == FF_PERIODIC:
        p = u.ff_periodic_effect
        fields.update(waveform=p.waveform, period=p.period, magnitude=p.magnitude,
                      offset=p.offset, phase=p.phase, envelope=_envelope(p.envelope))
    elif effect.type in EFFECT_KINDS:
        fields["conditions"] = [
            {"right_saturation": c.right_saturation, "left_saturation": c.left_saturation,
             "right_coeff": c.right_coeff, "left_coeff": c.left_coeff,
             "deadband": c.deadband, "center": c.center}
            for c in u.ff_condition_effect]
    return fields


def _envelope(env):
    return {"attack_length": env.attack_length, "attack_level": env.attack_level,
            "fade_length": env.fade_length, "fade_level": env.fade_level}


class EvdevEffectDecoder(object):
    """
    Turns the effects a game uploads to the virtual wheel into ffb_render effects.

    CONSTRAINT: requests arrive on the uinput thread and are queued; only the bridge thread
    may apply them, between ticks, so effect state never changes mid-computation.
    """

    def __init__(self, mixer, clock=time.monotonic):
        self.mixer = mixer
        self.clock = clock
        self.queue = queue.Queue(maxsize=4096)
        self.received = 0
        self.dropped = 0
        self.unknown = 0
        self.dir_counts = {}
        self.kinds = {}
        self.last_dir = 0
        self.last_dir_x = 0.0

    # -- called on the uinput thread ---------------------------------------

    def post(self, what, value):
        try:
            self.queue.put_nowait((self.clock(), what, value))
            self.received += 1
        except queue.Full:
            self.dropped += 1

    # -- called on the bridge thread ---------------------------------------

    def drain(self):
        """Apply every queued request. Returns how many were applied."""
        applied = 0
        while True:
            try:
                stamp, what, value = self.queue.get_nowait()
            except queue.Empty:
                return applied
            try:
                self._apply(stamp, what, value)
            except Exception as exc:                              # noqa: BLE001
                log.event("decode.error", request=what, error=repr(exc))
            applied += 1

    def _apply(self, stamp, what, value):
        mixer = self.mixer
        if what == "upload":
            self._upload(value)
        elif what == "erase":
            mixer.free(value)
        elif what == "play":
            effect_id, count = value
            effect = mixer.effects.get(effect_id)
            if effect is None:
                self.unknown += 1
            elif count:
                effect.start(stamp, count)
            else:
                effect.stop()
        elif what == "gain":
            mixer.device_gain = max(0.0, min(1.0, value / SATURATION_FULL))
            log.event("decode.device_gain", gain=round(mixer.device_gain, 3))
        elif what == "autocenter":
            # The motor has no spring of its own to set; the tune file's spring is the one.
            log.event("decode.autocenter", value=value)

    def _upload(self, f):
        kind = EFFECT_KINDS.get(f["type"])
        if f["type"] == FF_PERIODIC:
            kind = WAVEFORMS.get(f["waveform"])
        if kind is None:
            self.unknown += 1
            log.event("decode.unsupported", type=f["type"], waveform=f.get("waveform"))
            return
        mixer = self.mixer
        effect = mixer.effects.get(f["id"])
        if effect is not None and effect.kind != kind:
            mixer.free(f["id"])
        # An update keeps a playing effect playing, as a device does; only play restarts it.
        effect = mixer.get(f["id"])
        effect.kind = kind
        effect.duration = f["length"] / 1000.0          # 0 is infinite, as in ffb_render
        effect.start_delay = f["delay"] / 1000.0
        if kind not in self.kinds:
            log.event("decode.effect", id=f["id"], kind=kind, length_ms=f["length"])
        self.kinds[kind] = self.kinds.get(kind, 0) + 1

        if kind in render.CONDITION_KINDS:
            # Conditions carry one block per axis instead of a direction.
            effect.direction = 1.0
            effect.conditions = {axis: condition_params(block)
                                 for axis, block in enumerate(f["conditions"])}
            return

        raw = f["direction"]
        effect.direction = direction_x(raw)
        self.last_dir, self.last_dir_x = raw, effect.direction
        if raw not in self.dir_counts:
            log.event("decode.direction", dir=raw, degrees=round(360.0 * raw / TURN, 1),
                      x=round(effect.direction, 3))
            self.dir_counts[raw] = 0
        self.dir_counts[raw] += 1

        if kind == "constant":
            effect.magnitude = f["level"] / LEVEL_FULL
            peak = abs(effect.magnitude)
        elif kind == "ramp":
            effect.ramp_start = f["start"] / LEVEL_FULL
            effect.ramp_end = f["end"] / LEVEL_FULL
            peak = max(abs(effect.ramp_start), abs(effect.ramp_end))
        else:
            effect.periodic_magnitude = f["magnitude"] / LEVEL_FULL
            effect.periodic_offset = f["offset"] / LEVEL_FULL
            effect.periodic_phase_deg = 360.0 * f["phase"] / TURN
            effect.periodic_period = f["period"] / 1000.0
            peak = abs(effect.periodic_magnitude)
        env = f["envelope"]
        effect.attack_level = envelope_fraction(env["attack_level"], peak)
        effect.attack_time = env["attack_length"] / 1000.0
        effect.fade_level = envelope_fraction(env["fade_level"], peak)
        effect.fade_time = env["fade_length"] / 1000.0


def report_decoder(decoder):
    """Print and log what the game sent, for the close summary."""
    print("  ffb requests %d received, %d dropped, %d unrecognised"
          % (decoder.received, decoder.dropped, decoder.unknown))
    if decoder.kinds:
        print("  effects uploaded: %s" % ", ".join(
            "%s x%d" % kv for kv in sorted(decoder.kinds.items(), key=lambda kv: -kv[1])))
    if decoder.dir_counts:
        shown = sorted(decoder.dir_counts.items(), key=lambda kv: -kv[1])[:6]
        print("  directions seen: %s" % ", ".join(
            "0x%04X (x %+.2f) x%d" % (d, direction_x(d), n) for d, n in shown))
    log.event("decode.summary", received=decoder.received, dropped=decoder.dropped,
              unknown=decoder.unknown, directions=len(decoder.dir_counts), **decoder.kinds)


class UInputFrontend(object):
    """
    A virtual wheel under /dev/uinput, plus the decoder for the effects games send it.

    CONSTRAINT: a game's effect upload blocks until it is answered, so a thread of its own
    answers at once and only queues the effect; a slow answer stalls the game's input.
    """

    name = "uinput"

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.decoder = None
        self.caps = {}
        self.ui = None
        self.writes = 0
        self.failures = 0
        self._last = {}
        self._running = False
        self._thread = None

    def open(self, caps, ffb=True):
        self.caps = dict(caps)
        if self.dry_run:
            return self
        try:
            import fcntl
            from evdev import AbsInfo, UInput, UInputError, ecodes, ff
        except ImportError as exc:
            raise ImportError("python-evdev is not installed: sudo apt install python3-evdev, "
                              "or pip install evdev") from exc
        self._ecodes, self._ff, self._ioctl = ecodes, ff, fcntl.ioctl
        steer = AbsInfo(value=0, min=-STEERING_FULL, max=STEERING_FULL, fuzz=0, flat=0,
                        resolution=0)
        pedal = AbsInfo(value=0, min=0, max=PEDAL_FULL, fuzz=0, flat=0, resolution=0)
        # Five axes in vJoy's order, so games that number axes see the same layout.
        self.axes = [("wheel", ecodes.ABS_X, "steering", STEERING_FULL, True),
                     ("throttle", ecodes.ABS_Y, "throttle", PEDAL_FULL, False),
                     ("brake", ecodes.ABS_Z, "brake", PEDAL_FULL, False),
                     ("clutch", ecodes.ABS_RX, "clutch", PEDAL_FULL, False),
                     ("handbrake", ecodes.ABS_RY, "handbrake", PEDAL_FULL, False)]
        events = {ecodes.EV_ABS: [(code, steer if centred else pedal)
                                  for _a, code, _l, _f, centred in self.axes],
                  ecodes.EV_KEY: [ecodes.BTN_TRIGGER + i for i in range(BUTTON_COUNT)]}
        if ffb:
            events[ecodes.EV_FF] = FF_CODES
        try:
            self.ui = UInput(events, name=NAME, vendor=VENDOR, product=PRODUCT, version=1,
                             bustype=ecodes.BUS_USB)
        except (UInputError, OSError) as exc:
            raise RuntimeError("cannot create the virtual wheel: %s. /dev/uinput must be "
                               "writable for this user." % exc)
        if ffb:
            upload_size = ctypes.sizeof(ff.UInputUpload)
            erase_size = ctypes.sizeof(ff.UInputErase)
            self._ioctls = {"begin_upload": ioctl_number(3, 200, upload_size),
                            "end_upload": ioctl_number(1, 201, upload_size),
                            "begin_erase": ioctl_number(3, 202, erase_size),
                            "end_erase": ioctl_number(1, 203, erase_size)}
            self.decoder = EvdevEffectDecoder(render.EffectMixer())
            self._running = True
            self._thread = threading.Thread(target=self._serve, name="uinput-ff", daemon=True)
            self._thread.start()
        log.event("frontend.open", frontend=self.name, device=self.ui.device.path, ffb=ffb)
        return self

    # -- the uinput thread -------------------------------------------------

    def _serve(self):
        ui, ecodes = self.ui, self._ecodes
        while self._running:
            try:
                if not select.select([ui.fd], [], [], 0.1)[0]:
                    continue
                for event in ui.read():
                    if event.type == ecodes.EV_UINPUT:
                        self._answer(event)
                    elif event.type == ecodes.EV_FF:
                        self._control(event)
            except BlockingIOError:
                continue
            except Exception as exc:                              # noqa: BLE001
                log.event("uinput.error", error=repr(exc))
                time.sleep(0.01)

    def _answer(self, event):
        """Answer an upload or erase at once; the bridge thread applies the effect later."""
        ff, ecodes = self._ff, self._ecodes
        if event.code == ecodes.UI_FF_UPLOAD:
            upload = self._request(ff.UInputUpload(), "begin_upload", event.value)
            fields = effect_fields(upload.effect)
            upload.retval = 0
            self._ioctl(self.ui.fd, self._ioctls["end_upload"], upload)
            self.decoder.post("upload", fields)
        elif event.code == ecodes.UI_FF_ERASE:
            erase = self._request(ff.UInputErase(), "begin_erase", event.value)
            erase.retval = 0
            self._ioctl(self.ui.fd, self._ioctls["end_erase"], erase)
            self.decoder.post("erase", erase.effect_id)

    def _request(self, struct, name, request_id):
        # python-evdev 1.7.0's begin_upload and begin_erase fill in effect_id, never
        # request_id, so the kernel rejects them and the game's upload hangs for 30 s.
        struct.request_id = request_id
        self._ioctl(self.ui.fd, self._ioctls[name], struct, True)
        return struct

    def _control(self, event):
        if event.code == FF_GAIN:
            self.decoder.post("gain", event.value)
        elif event.code == FF_AUTOCENTER:
            self.decoder.post("autocenter", event.value)
        else:
            self.decoder.post("play", (event.code, event.value))

    # -- the bridge thread -------------------------------------------------

    def feed(self, reading):
        """Push one reading into the virtual wheel. Returns (label, raw, written) per axis."""
        written = []
        if self.dry_run:
            return [("steering", reading.wheel, None)]
        ecodes, changed = self._ecodes, False
        for attr, code, label, full, centred in self.axes:
            raw = getattr(reading, attr, 0.0)
            # A control the wheel lacks stays at rest; zero would read as a pedal held down.
            if self.caps.get(attr) is False:
                raw = 0.0
            else:
                written.append((label, raw, None))
            value = int(round(render.clamp(raw) * full if centred
                              else max(0.0, min(1.0, raw)) * full))
            changed |= self._write(ecodes.EV_ABS, code, value)
        for index in range(BUTTON_COUNT):
            pressed = 1 if int(reading.buttons) & (1 << index) else 0
            changed |= self._write(ecodes.EV_KEY, ecodes.BTN_TRIGGER + index, pressed)
        if changed:
            try:
                self.ui.syn()
            except OSError:
                self.failures += 1
        return written

    def _write(self, etype, code, value):
        if self._last.get(code) == value:
            return False
        try:
            self.ui.write(etype, code, value)
            self.writes += 1
        except OSError:
            self.failures += 1
            return False
        self._last[code] = value
        return True

    def describe(self):
        if self.dry_run:
            return ["dry run: no virtual wheel is created"]
        lines = ["virtual wheel %s at %s (%04x:%04x), %d buttons"
                 % (NAME, self.ui.device.path, VENDOR, PRODUCT, BUTTON_COUNT)]
        skipped = [label for attr, _c, label, _f, _z in self.axes
                   if self.caps.get(attr) is False]
        if skipped:
            lines.append("not on this wheel, left at rest: %s" % ", ".join(skipped))
        return lines

    def close(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self.decoder is not None:
            report_decoder(self.decoder)
        if self.ui is not None:
            print("  virtual wheel writes %d, failures %d" % (self.writes, self.failures))
            self.ui.close()
            self.ui = None
