"""vJoy as the game-facing wheel: axes and buttons go in, force feedback packets come out."""

import queue
import time

import ffb_render as render
import probe_log as log

try:
    import pyvjoy
    import pyvjoy._sdk as sdk
    from pyvjoy.constants import (
        CTRL_DEVCONT, CTRL_DEVPAUSE, CTRL_DEVRST, CTRL_DISACT, CTRL_ENACT, CTRL_STOPALL,
        EFF_STOP,
        HID_USAGE_RX, HID_USAGE_RY, HID_USAGE_X, HID_USAGE_Y, HID_USAGE_Z,
        PT_BLKFRREP, PT_CONDREP, PT_CONSTREP, PT_CTRLREP, PT_EFFREP, PT_EFOPREP,
        PT_ENVREP, PT_GAINREP, PT_NEWEFREP, PT_PRIDREP, PT_RAMPREP,
        VJD_STAT_BUSY, VJD_STAT_FREE, VJD_STAT_MISS, VJD_STAT_OWN,
    )
except ImportError as exc:
    raise ImportError("pyvjoyffb is not installed: pip install pyvjoyffb") from exc

# vJoy axis range. The SDK takes 0x1..0x8000; 0x4000 is centre for a bidirectional axis.
AXIS_MIN = 0x0001
AXIS_MAX = 0x8000
AXIS_MID = 0x4000

AXIS_NAMES = {0x30: "X", 0x31: "Y", 0x32: "Z", 0x33: "RX", 0x34: "RY", 0x35: "RZ"}

STATUS_NAMES = {VJD_STAT_OWN: "owned by us", VJD_STAT_FREE: "free",
                VJD_STAT_BUSY: "busy (another app owns it)", VJD_STAT_MISS: "missing"}


def bidirectional(value):
    """-1.0..+1.0 -> full vJoy axis range, centred."""
    return int(round(AXIS_MID + render.clamp(float(value)) * (AXIS_MAX - AXIS_MID)))


def unidirectional(value):
    """0.0..1.0 -> full vJoy axis range. Pedals rest at zero, not at centre."""
    return int(round(AXIS_MIN + max(0.0, min(1.0, value)) * (AXIS_MAX - AXIS_MIN)))


# Which real-wheel control drives which vJoy axis.
#
# CONSTRAINT: only X, Y, Z, RX, RY and RZ reach DirectInput; anything beyond them writes
# successfully and never arrives. Pedals get their own axes because a game cannot split two
# that were summed before they arrived.
AXIS_MAP = [
    # (reading attribute, vJoy usage, label, converter)
    ("wheel", HID_USAGE_X, "steering", bidirectional),
    ("throttle", HID_USAGE_Y, "throttle", unidirectional),
    ("brake", HID_USAGE_Z, "brake", unidirectional),
    ("clutch", HID_USAGE_RX, "clutch", unidirectional),
    ("handbrake", HID_USAGE_RY, "handbrake", unidirectional),
]


class VJoyFeeder(object):
    """Writes axis and button state into the vJoy device."""

    def __init__(self, rid, dry_run=False):
        self.rid = rid
        self.dry_run = dry_run
        self.device = None
        self.writes = 0
        self.failures = 0
        self.button_count = 0
        self.axes = []

    def open(self):
        status = sdk.GetVJDStatus(self.rid)
        if status in (VJD_STAT_MISS, VJD_STAT_BUSY):
            raise RuntimeError("vJoy device %d is %s. Configure or free it in vJoyConf."
                               % (self.rid, STATUS_NAMES.get(status, status)))

        # Writing to an axis that is not enabled fails per call, so check once up front.
        self.axes = [(attr, usage, label, conv) for attr, usage, label, conv in AXIS_MAP
                     if sdk._vj.GetVJDAxisExist(self.rid, usage)]
        self.button_count = int(sdk._vj.GetVJDButtonNumber(self.rid))

        if not self.dry_run:
            self.device = pyvjoy.VJoyDevice(self.rid)
        return self

    def close(self):
        if self.device is not None:
            try:
                sdk.ResetVJD(self.rid)
            except Exception:
                pass
            self.device = None

    def feed(self, reading, caps):
        """Push one reading into vJoy. Returns (label, raw, written) for each axis."""
        written = []
        for attr, usage, label, conv in self.axes:
            # A control the wheel lacks stays at rest; zero would read as a pedal held down.
            if caps.get(attr) is False:
                continue
            try:
                raw = getattr(reading, attr)
            except Exception:
                continue
            value = conv(raw)
            written.append((label, raw, value))
            if self.device is not None:
                try:
                    self.device.set_axis(usage, value)
                    self.writes += 1
                except Exception:
                    self.failures += 1

        if self.device is not None and self.button_count:
            try:
                self._feed_buttons(int(reading.buttons))
            except Exception:
                self.failures += 1
        return written

    def _feed_buttons(self, mask):
        """Bit 0 becomes button 1 and so on: positional, since games rebind buttons anyway."""
        for index in range(self.button_count):
            self.device.set_button(index + 1, 1 if mask & (1 << index) else 0)


# vJoy effect-type byte -> the kind names ffb_render uses.
EFFECT_KINDS = {
    1: "constant", 2: "ramp", 3: "square", 4: "sine", 5: "triangle",
    6: "sawtoothup", 7: "sawtoothdown",
    8: "spring", 9: "damper", 10: "inertia", 11: "friction",
}


class EffectDecoder(object):
    """
    Turns vJoy's force feedback packets into ffb_render effects.

    CONSTRAINT: packets arrive on vJoy's thread and are queued; only the bridge thread may
    apply them, between ticks, so effect state never changes mid-computation.

    CONSTRAINT: gain arrives as a 0-255 byte and duration in milliseconds with 0xFFFF meaning
    infinite; magnitudes stay at +-10000. A wrong unit is a silent factor of 40, not a crash.
    vjoy_ffb_spike.py re-measures this. Direction is a mode, see
    docs/tuning.md#how-dir_mode-reads-a-games-direction-field.
    """

    def __init__(self, mixer, clock=time.monotonic):
        self.mixer = mixer
        self.clock = clock
        self.queue = queue.Queue(maxsize=4096)
        self.received = 0
        self.dropped = 0
        self.unknown = 0

        # Distinct direction values and their counts; they arrive about 66 times a second.
        self.dir_counts = {}
        # A defining packet is logged only when it says something new.
        self.effect_seen = set()
        self.mag_min = 0.0
        self.mag_max = 0.0
        # The latest direction, for the tick line: its correlation with steering is the signal.
        self.last_dir = 0
        self.last_dir_x = 0.0

    # -- called on vJoy's thread -------------------------------------------

    def on_packet(self, data, reptype):
        """vJoy callback. Does the minimum possible and gets off this thread."""
        try:
            self.queue.put_nowait((self.clock(), reptype, _snapshot(data)))
            self.received += 1
        except queue.Full:
            # Dropping beats blocking vJoy's thread; counted because it feels like random FFB.
            self.dropped += 1

    # -- called on the bridge thread ---------------------------------------

    def drain(self):
        """Apply every queued packet. Returns how many were applied."""
        applied = 0
        while True:
            try:
                stamp, reptype, fields = self.queue.get_nowait()
            except queue.Empty:
                return applied
            try:
                self._apply(stamp, reptype, fields)
            except Exception as exc:                              # noqa: BLE001
                log.event("decode.error", reptype=reptype, error=repr(exc))
            applied += 1

    def _apply(self, stamp, reptype, f):
        mixer = self.mixer

        if reptype == PT_CTRLREP:
            self._control(f)
            return
        if reptype == PT_GAINREP:
            # Device gain is the game's master volume for force feedback, 0-255.
            mixer.device_gain = _byte_fraction(f)
            log.event("decode.device_gain", gain=round(mixer.device_gain, 3))
            return
        if reptype == PT_NEWEFREP:
            return                      # allocation only; the block index arrives later
        if reptype == PT_BLKFRREP:
            mixer.free(int(f))
            return

        block = f.get("EffectBlockIndex", 0) if isinstance(f, dict) else 0
        if not block:
            self.unknown += 1
            return
        effect = mixer.get(block)

        if reptype == PT_EFFREP:
            kind = EFFECT_KINDS.get(f["EffectType"])
            if kind is None:
                self.unknown += 1
                return
            effect.kind = kind
            effect.gain = f["Gain"] / 255.0                  # BYTE, not 0..10000
            effect.duration = render.duration_seconds(f["Duration"])
            effect.start_delay = f.get("StartDelay", 0) / 1000.0
            seen = (block, kind, f["Duration"])
            if seen not in self.effect_seen:
                self.effect_seen.add(seen)
                log.event("decode.effect", block=block, kind=kind,
                          raw_duration=f["Duration"],
                          seconds=effect.duration or "infinite",
                          gain=round(effect.gain, 3))
            dir_raw = f["DirX"]
            effect.direction = render.direction_x(dir_raw)
            self.last_dir = dir_raw
            self.last_dir_x = effect.direction
            # Two distinct values here means the game steers with the angle.
            if dir_raw not in self.dir_counts:
                log.event("decode.direction", dirx=dir_raw,
                          degrees=round(360.0 * (dir_raw % 32768) / 32768.0, 1),
                          x=round(effect.direction, 3))
                self.dir_counts[dir_raw] = 0
            self.dir_counts[dir_raw] += 1
        elif reptype == PT_CONSTREP:
            raw = f["Magnitude"]
            # A magnitude that never goes negative means the sign travels elsewhere.
            self.mag_min = min(self.mag_min, raw)
            self.mag_max = max(self.mag_max, raw)
            effect.magnitude = raw / render.DI_FULL_SCALE
        elif reptype == PT_RAMPREP:
            effect.ramp_start = f["Start"] / render.DI_FULL_SCALE
            effect.ramp_end = f["End"] / render.DI_FULL_SCALE
        elif reptype == PT_PRIDREP:
            effect.periodic_magnitude = f["Magnitude"] / render.DI_FULL_SCALE
            effect.periodic_offset = f["Offset"] / render.DI_FULL_SCALE
            # Assumed hundredths of a degree; only phase 0 has been observed.
            effect.periodic_phase_deg = f["Phase"] / 100.0
            effect.periodic_period = f["Period"] / 1000.0
        elif reptype == PT_CONDREP:
            # CONSTRAINT: a positive coefficient resists, matching the firmware spring. If a
            # game's spring pushes outward, flip it here at the decode boundary.
            axis = 1 if f["isY"] else 0
            effect.conditions[axis] = render.ConditionParams.from_di(
                offset=f["CenterPointOffset"],
                pos_coeff=f["PosCoeff"], neg_coeff=f["NegCoeff"],
                pos_saturation=f["PosSatur"], neg_saturation=f["NegSatur"],
                deadband=f["DeadBand"])
        elif reptype == PT_ENVREP:
            effect.attack_level = f["AttackLevel"] / render.DI_FULL_SCALE
            effect.attack_time = f["AttackTime"] / 1000.0
            effect.fade_level = f["FadeLevel"] / render.DI_FULL_SCALE
            effect.fade_time = f["FadeTime"] / 1000.0
        elif reptype == PT_EFOPREP:
            op = f["EffectOp"]
            if op == EFF_STOP:
                effect.stop()
            else:
                effect.start(stamp, f.get("LoopCount", 1))
            log.event("decode.effect_op", block=block, op=op, kind=effect.kind or "?")
        else:
            self.unknown += 1

    def _control(self, value):
        mixer = self.mixer
        if value == CTRL_DEVRST:
            mixer.reset()
        elif value == CTRL_STOPALL:
            mixer.stop_all()
        elif value == CTRL_DEVPAUSE:
            mixer.paused = True
        elif value == CTRL_DEVCONT:
            mixer.paused = False
        elif value == CTRL_ENACT:
            mixer.actuators_enabled = True
        elif value == CTRL_DISACT:
            mixer.actuators_enabled = False
        log.event("decode.control", control=value)


def _snapshot(data):
    """Copy a packet out of vJoy's structure, which belongs to the callback's stack frame."""
    if isinstance(data, sdk.PacketStruct):
        return data.to_dict()
    return data


def _byte_fraction(value):
    try:
        return max(0.0, min(1.0, int(value) / 255.0))
    except (TypeError, ValueError):
        return 1.0


def report_directions(decoder):
    """Print and log which convention the game used to point its forces."""
    print("  ffb packets %d received, %d dropped, %d unrecognised"
          % (decoder.received, decoder.dropped, decoder.unknown))
    if not decoder.dir_counts:
        return
    shown = sorted(decoder.dir_counts.items(), key=lambda kv: -kv[1])[:6]
    print("  directions seen: %s"
          % ", ".join("%d (%.0f deg) x%d" % (d, 360.0 * (d % 32768) / 32768.0, n)
                      for d, n in shown))
    print("  raw magnitude range: %+d .. %+d" % (decoder.mag_min, decoder.mag_max))
    if len(decoder.dir_counts) > 1:
        print("  -> the game POINTS forces with the angle. The sign fix in "
              "ffb_render.direction_x is the one that matters.")
    elif decoder.mag_min < 0:
        print("  -> the game SIGNS forces in the magnitude, single direction.")
    else:
        print("  -> one direction and a never-negative magnitude: this game sent "
              "no directional force at all during this run.")
    log.event("decode.summary", directions=len(decoder.dir_counts),
              mag_min=decoder.mag_min, mag_max=decoder.mag_max)


class VJoyFrontend(object):
    """The vJoy device a game binds, plus the decoder for the effects it sends."""

    name = "vjoy"

    def __init__(self, rid=1, dry_run=False):
        self.feeder = VJoyFeeder(rid, dry_run=dry_run)
        self.decoder = None
        self.caps = {}

    def open(self, caps, ffb=True):
        self.caps = dict(caps)
        self.feeder.open()
        if ffb and self.feeder.device is not None:
            self.decoder = EffectDecoder(render.EffectMixer())
            self.feeder.device.ffb_register_callback(self.decoder.on_packet)
            sdk._vj.FfbStart(self.feeder.rid)
        return self

    def feed(self, reading):
        return self.feeder.feed(reading, self.caps)

    def describe(self):
        feeder = self.feeder
        lines = ["vJoy device %d: axes %s, %d buttons"
                 % (feeder.rid, ", ".join("%s->%s" % (label, AXIS_NAMES[usage])
                                          for _a, usage, label, _c in feeder.axes),
                    feeder.button_count)]
        skipped = [label for attr, _u, label, _c in AXIS_MAP if self.caps.get(attr) is False]
        if skipped:
            lines.append("not on this wheel, left at rest: %s" % ", ".join(skipped))
        if feeder.dry_run:
            lines.append("dry run: nothing is written to vJoy")
        return lines

    def close(self):
        if self.decoder is not None:
            report_directions(self.decoder)
        print("  vJoy writes %d, failures %d" % (self.feeder.writes, self.feeder.failures))
        self.feeder.close()
