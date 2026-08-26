"""
live_tune.py -- tuning values that can change while a game is running.

WHY THIS EXISTS
---------------
Force feedback is judged by feel, and feel cannot be judged from a changelog. Every value in
this project used to be a CLI argument baked into an object at startup, so trying a different
strength meant: quit the game, restart the bridge, relaunch the game, drive back to the corner
that felt wrong. Two minutes per adjustment, and by the time you arrived you were comparing
against a memory rather than against the previous lap.

So these live in a JSON file that the bridge re-reads while it runs. Someone can be mid-corner
when a value changes and feel the difference immediately, which is the only way to tell 0.55
from 0.65 -- the difference is real and no amount of reasoning will find it.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not use the IPC gain field. The shim reads that exactly once, when it loads the WGI
effect, because WGI latches gain at load time (see the note in motor_sink.WgiMotorSink).
Writing
it mid-session looks like it works and changes nothing -- which is very likely why raising the
in-game strength slider appeared to do nothing. Live strength therefore SCALES THE FORCE VALUE
itself, before it is ever handed to a sink.

It also never raises. A game is running; a typo in a JSON file must not take the wheel down.
Anything unparseable leaves the previous values in place and is reported once.
"""

import json
import os

import ffb_render as render

# Every tunable, with the value that means "unchanged from how the game sent it". Anything not
# in here is ignored, so a stray key in the file is a typo rather than a silent new setting.
DEFAULTS = {
    "strength": 1.0,        # multiplies the game's force. 1.0 = what the game asked for
    "invert": False,        # flip the game's force direction (see the note in apply())
    # How to read the game's direction field: "sin" for a true polar angle, "span" for a
    # steering axis encoded linearly across part of the circle. DiRT 4 needs "span". Games
    # differ, and the wrong one costs the sign entirely -- see ffb_render.direction_x.
    "dir_mode": "sin",
    "max_force": 0.6,       # ceiling; the wheel is geared strongly enough that 1.0 is a lot
    "min_force": 0.0,       # floor on non-zero output, to beat the motor's own stiction
    "spring": 0.0,          # synthetic centring, from real wheel position. 0 = off
    "damper": 0.0,          # synthetic damping, from real wheel velocity. 0 = off
    "friction": 0.0,        # synthetic drag whenever the wheel moves at all. 0 = off

    # Wheel buttons that adjust tuning while driving, as 1-based bit numbers; 0 = unassigned.
    # A game holds the foreground and every keystroke with it, so the wheel is the only input
    # device that can reach us mid-corner. Run the bridge and press buttons to find the bits --
    # each new bitfield value is printed.
    "btn_down": 0.0,        # step the tuned parameter down
    "btn_up": 0.0,          # step it up
    "btn_next": 0.0,        # cycle which parameter is being tuned
}

# What each button step changes, in order, with its increment and range. Strength first
# because it is the one anybody actually wants mid-corner.
STEPS = [
    ("strength", 0.05, 0.0, 3.0),
    ("max_force", 0.05, 0.0, 1.0),
    ("min_force", 0.01, 0.0, 0.30),
    ("spring", 0.05, 0.0, 2.0),
    ("damper", 0.05, 0.0, 2.0),
    ("friction", 0.05, 0.0, 1.0),
]

# Below this, a force is "nothing" and must stay nothing. Without it, min_force would turn the
# silence between effects into a permanent buzz against the end stops.
SILENCE = 0.005


def clamp(value, limit=1.0):
    if value != value:                      # NaN, which a hand-edited file can produce
        return 0.0
    return max(-limit, min(limit, value))


class LiveTune(object):
    """
    The current tuning values, reloaded from disk when the file changes.

    Polled by mtime rather than watched: a watcher means a thread and a callback mutating state
    underneath the render loop, and this needs neither. One `os.stat` twice a second is free
    next to what the loop already does.
    """

    def __init__(self, path, poll_seconds=0.5):
        self.path = path
        self.poll_seconds = poll_seconds
        self.values = dict(DEFAULTS)
        self.loads = 0
        self.errors = 0
        self.last_error = None
        self._mtime = None
        self._next_poll = 0.0

    # -- file ---------------------------------------------------------------

    def write_default(self):
        """Create the file if it is not there, so there is something to edit."""
        if os.path.exists(self.path):
            return False
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(DEFAULTS, handle, indent=2)
            handle.write("\n")
        return True

    def poll(self, now):
        """
        Reload if the file changed. Returns the keys whose values changed, or an empty list.

        A file being saved is not atomic: an editor can truncate it and write it in two steps,
        and reading it in between yields invalid JSON. That is a NORMAL event, not an error --
        it just means try again next poll, holding the values we already have.
        """
        if now < self._next_poll:
            return []
        self._next_poll = now + self.poll_seconds

        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            return []
        if mtime == self._mtime:
            return []

        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("top level is not an object")
        except (OSError, ValueError) as exc:
            # Do NOT record the mtime: a half-written file must be retried, not skipped.
            self.errors += 1
            self.last_error = str(exc)
            return []

        self._mtime = mtime
        self.loads += 1
        changed = []
        for key, default in DEFAULTS.items():
            if key not in loaded:
                continue
            value = loaded[key]
            try:
                if isinstance(default, bool):
                    value = bool(value)
                elif isinstance(default, str):
                    value = str(value).strip().lower()
                else:
                    value = float(value)
            except (TypeError, ValueError):
                continue
            if value != self.values[key]:
                self.values[key] = value
                changed.append(key)
        return changed

    # -- values -------------------------------------------------------------

    def __getitem__(self, key):
        return self.values[key]

    def save(self):
        """
        Write the current values back out.

        Button tuning would otherwise diverge from the file: someone settles on a strength by
        feel, then the next edit from outside reloads the old number and silently undoes it.
        Writing back makes the file the single record of what is set, which also means an
        in-car session survives a restart and can be read afterwards instead of recalled.
        """
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(self.values, handle, indent=2)
                handle.write("\n")
        except OSError as exc:
            self.errors += 1
            self.last_error = str(exc)
            return False
        # Adopt our own mtime so the write does not read back as an external change.
        try:
            self._mtime = os.stat(self.path).st_mtime
        except OSError:
            pass
        return True

    def set(self, key, value):
        """Change a value in memory (for in-car button tuning) without touching the file."""
        if key in self.values:
            self.values[key] = value

    def summary(self):
        return " ".join(
            "%s=%s" % (k, "on" if v is True else "off" if v is False
                       else v if isinstance(v, str) else "%.2f" % v)
            for k, v in self.values.items())

    # -- the actual work ----------------------------------------------------

    def apply(self, force, state=None):
        """
        Turn the game's force into what the motor should do.

        ORDER MATTERS AND EACH STEP IS DELIBERATE:

        1. `strength` scales the game's force. This is the knob that actually works, unlike the
           latched WGI gain.
        2. `invert` flips THE GAME'S FORCE ONLY, not the synthetic terms below. Its job is to
           correct the game's direction convention if we guessed it backwards; the synthetic
           terms are computed from measured wheel motion and are correctly signed by
           construction. Inverting those would turn the spring from something that opposes
           displacement into something that amplifies it -- positive feedback that drives the
           wheel to its end stop and holds it there.
        3. Synthetic spring and damper are ADDED. DiRT 4 sends neither -- it sends one constant
           force and nothing else -- so if the game's own centring feels thin there is nothing
           to turn up in the game. These fill that gap from real wheel position and velocity.
        4. `max_force` clamps the sum, once, at the end. Clamping per term would quietly change
           the mix rather than limiting it.
        5. `min_force` lifts a small non-zero force up to something the motor can actually
           express, leaving true silence silent.
        """
        out = clamp(force) * self.values["strength"]
        if self.values["invert"]:
            out = -out

        if state is not None:
            spring = self.values["spring"]
            damper = self.values["damper"]
            friction = self.values["friction"]
            if spring:
                out += -state.position * spring
            if damper:
                out += -state.velocity * damper
            if friction:
                # Not -sign(velocity) * gain written out again. ffb_render already holds the
                # law that was fitted to this wheel, DEAD BAND INCLUDED -- and the dead band
                # is the whole difference between drag and a buzz against a wheel at rest.
                out += render.condition_force(
                    "friction", render.legacy_condition_params("friction", friction), state)

        out = clamp(out, max(0.0, self.values["max_force"]))

        floor = self.values["min_force"]
        if floor > 0.0 and SILENCE < abs(out) < floor:
            out = floor if out > 0.0 else -floor
        return out


class ButtonTuner(object):
    """
    Adjust tuning from the wheel's own buttons, because nothing else can reach us.

    While a game runs it owns the foreground and therefore the keyboard -- that gate is the
    reason this whole project exists. The wheel is the one input device whose state we read
    directly, over the same shared section the shim publishes position through.

    ONE HONEST LIMITATION: there is no display in the car. Stepping a value gives immediate
    physical feedback -- that is the entire point -- but which parameter is selected does not,
    so cycling is a blind mode change. Hence the order in STEPS: strength is first and is what
    a single up/down pair adjusts if `btn_next` is never assigned. Every change is printed and
    logged, so what happened is recoverable afterwards even when it was not obvious then.
    """

    def __init__(self, tune):
        self.tune = tune
        self.index = 0
        self.last = 0
        self.changes = 0

    @property
    def parameter(self):
        return STEPS[self.index][0]

    def _bit(self, name):
        try:
            bit = int(self.tune.values.get(name, 0))
        except (TypeError, ValueError):
            return 0
        return bit

    def pressed(self, buttons, name):
        """True on the RISING edge only -- a held button must not repeat 100 times a second."""
        bit = self._bit(name)
        if bit <= 0:
            return False
        mask = 1 << (bit - 1)
        return bool(buttons & mask) and not bool(self.last & mask)

    def update(self, buttons):
        """
        Feed the current bitfield. Returns a description of what changed, or None.

        Nothing is applied unless a button is actually assigned, so an unconfigured wheel
        cannot adjust anything by accident -- which matters, because these are buttons the
        game is very likely using for something of its own.
        """
        if buttons == self.last:
            return None

        note = None
        if self.pressed(buttons, "btn_next"):
            self.index = (self.index + 1) % len(STEPS)
            note = "tuning %s (%.2f)" % (self.parameter, self.tune[self.parameter])
        else:
            step = 0
            if self.pressed(buttons, "btn_up"):
                step = 1
            elif self.pressed(buttons, "btn_down"):
                step = -1
            if step:
                key, increment, low, high = STEPS[self.index]
                value = max(low, min(high, self.tune[key] + step * increment))
                if value != self.tune[key]:
                    self.tune.set(key, value)
                    self.tune.save()
                    self.changes += 1
                note = "%s %.2f" % (key, value)

        self.last = buttons
        return note
