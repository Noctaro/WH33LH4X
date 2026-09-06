r"""
gui.py: the window. Pick a game, start it, see whether the pieces are alive.

Deliberately small: choose a game, start and stop it, three status badges, per-game notes, and
the wheel feel sliders. No setup wizard, no per-game installer, no live force meter. See
docs/development.md#the-window for why tkinter, and why this shells out to play.ps1.

Run it with pythonw.exe so there is no console window:
    .venv/Scripts/pythonw.exe gui.py
"""

import ctypes
import json
import os
import time
import struct
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, ttk

from motor_sink import IpcMotorSink

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(ROOT, "gui_settings.json")
TUNE = os.path.join(ROOT, "tune.json")
# Asking play.ps1 to stop, rather than killing it. See _stop.
STOP_REQUEST = os.path.join(ROOT, "stop.request")
LOGS = os.path.join(ROOT, "logs")
GAMES = os.path.join(ROOT, "games.json")

POLL_MS = 250
# How long the bridge gets to appear after Start. Generous: WGI enumeration alone can take
# seconds when the wheel was only just plugged in.
BRIDGE_GRACE = 20.0
# How long play.ps1 gets to shut down after being asked, before it is killed. Its wait loop
# polls every 2 seconds and the bridge takes a moment to release vJoy.
STOP_GRACE = 8.0
# How many games the Recent dropdown remembers. A shortcut, not a library.
RECENT_MAX = 8

# Strength scales the force the game sends. Capped at 1.0 because that is exactly what the
# game asked for; amplification above it is expert territory, and tune.json has no limit.
STRENGTH = ("strength", "Strength", 0.0, 1.0, "how hard the game's own force is felt")

# CONSTRAINT: live_tune defaults strength to 1.0 when absent. Showing 0.0 would be a lie the
# slider then writes to the file.
TUNE_DEFAULTS = {"strength": 1.0}

# The wheel feel knobs, and the range the window allows.
#
# CONSTRAINT: capped below live_tune's 2.0 on purpose. Spring and damper close a loop around
# the wheel and hunt at too much gain, and a slider is easier to move carelessly than a config
# file is to edit. See GAMES.md. Strength closes no loop and needs no such caution.
FEEL = [
    ("spring", "Spring", 0.0, 1.0,
     "pulls back to centre, harder the further off you are"),
    ("damper", "Damper", 0.0, 1.0,
     "resists how fast you turn: this is what keeps the spring steady"),
    ("friction", "Friction", 0.0, 1.0,
     "constant drag whenever the wheel moves: weight, not centring"),
]

PD_NOTE = (
    "Strength is the volume knob. It multiplies the force the game itself sends, so 1.00 is "
    "exactly what the game asked for and anything less is quieter. Set this first, before "
    "touching anything below it.\n\n"
    "Two things sit above it that this window cannot see. max_force in tune.json caps the "
    "peak, and the wheel's own strength setting in the HORI device manager app is a gain "
    "stage of its own worth about 3.5x between its lowest and highest setting. If the whole "
    "wheel feels wrong at every position of this slider, that app is where to look.\n\n"
    "Why a spring at all: this wheel has a strong centring spring of its own, but it stays "
    "suspended for as long as the bridge holds the motor (measured 2026-08-26). Without one "
    "of these the wheel sits wherever you leave it.\n\n"
    "Spring and Damper are a pair. The spring does the centring; the damper stops it "
    "overshooting and hunting. Raise them together.\n\n"
    "If you know control theory: this is a PD controller. Spring is the proportional term "
    "(force from how far off centre the wheel is) and Damper is the derivative term (force "
    "from how fast it is moving). A P term alone, in a loop with lag, oscillates. The D "
    "term is what makes it settle. It is the same reason raising strength on its own can "
    "make the wheel hunt."
)


# --------------------------------------------------------------------------- shared section

# CONSTRAINT: watching a shared section must never create one. See ShimView.
FILE_MAP_READ = 0x0004
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenFileMappingW.restype = ctypes.c_void_p
_k32.OpenFileMappingW.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p)
_k32.MapViewOfFile.restype = ctypes.c_void_p
_k32.MapViewOfFile.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                               ctypes.c_ulong, ctypes.c_size_t)
_k32.UnmapViewOfFile.argtypes = (ctypes.c_void_p,)
_k32.CloseHandle.argtypes = (ctypes.c_void_p,)
_k32.GetTickCount64.restype = ctypes.c_ulonglong


class ShimView(object):
    """
    Read-only view of the shim's shared section. It observes; it owns nothing.

    CONSTRAINT: must not create the section. OpenFileMappingW fails when there is nothing to
    open; mmap(-1) invents one, as PAGE_READONLY, and every later writer then fails with
    WinError 87. See docs/development.md#the-window.

    CONSTRAINT: must not use IpcMotorSink. Its open() stamps the bridge heartbeat, which makes
    the shim hold the motor for a bridge that is not running. The struct layout is still taken
    from IpcMotorSink so the wire format has one definition, matched to shim/ipc.h.
    """

    def read(self):
        """(shim_alive, shim_state, bridge_alive). All False or None when nothing runs."""
        blob = self._snapshot()
        if blob is None:
            return (False, IpcMotorSink.STATE_NONE, False)
        try:
            magic, _version = struct.unpack_from("<II", blob, 0)
            if magic != IpcMotorSink._MAGIC:
                return (False, IpcMotorSink.STATE_NONE, False)
            bridge_tick, shim_tick, state = struct.unpack_from("<QQI", blob, 16)
        except Exception:
            return (False, IpcMotorSink.STATE_NONE, False)

        now = _k32.GetTickCount64()
        fresh = IpcMotorSink._STALE_MS
        return (bool(shim_tick) and (now - shim_tick) < fresh,
                state,
                bool(bridge_tick) and (now - bridge_tick) < fresh)

    @staticmethod
    def _snapshot():
        """A copy of the section's bytes, or None when nobody has published one."""
        size = IpcMotorSink._STRUCT.size
        handle = _k32.OpenFileMappingW(FILE_MAP_READ, 0, IpcMotorSink._NAME)
        if not handle:
            return None
        view = None
        try:
            view = _k32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, size)
            if not view:
                return None
            return ctypes.string_at(view, size)
        except Exception:
            return None
        finally:
            if view:
                _k32.UnmapViewOfFile(view)
            _k32.CloseHandle(handle)

    def close(self):
        """Nothing is held open between reads, so there is nothing to release."""


def bundle_version():
    """The bundle's version, or None in a repo checkout, where nothing writes the file."""
    try:
        with open(os.path.join(ROOT, "VERSION.txt"), encoding="utf-8") as fh:
            return fh.readline().strip() or None
    except OSError:
        return None


def last_log_line():
    """
    Why the newest bridge run gave up, or None if it did not say.

    The bridge writes one `exit.fatal` event before returning non-zero, and that is the only
    thing read here. Guessing from the last console line does not work: a failed run still
    releases the motor on the way out, so the final line is housekeeping and the diagnosis is
    several lines above it.
    """
    try:
        logs = [os.path.join(LOGS, n) for n in os.listdir(LOGS)
                if n.startswith("vjoy_bridge_") and n.endswith(".log")]
        if not logs:
            return None
        with open(max(logs, key=os.path.getmtime), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                if "exit.fatal" in line and "reason=" in line:
                    return line.split("reason=", 1)[1].strip()
    except Exception:
        pass                           # a missing reason must never break the window
    return None


def is_steam_game(exe_path, game=None):
    """
    Does this game come up through the Steam client rather than from the exe we start?

    games.json is asked first, because a game can be a Steam title while sitting outside the
    default library. The path check is the fallback for titles with no entry yet.
    """
    if game is not None and game.get("launch"):
        return game["launch"] == "steam"
    return os.sep + "steamapps" + os.sep in exe_path.replace("/", os.sep).lower()


# The device the bridge feeds, and the axes docs/vjoy.md says to enable. HID usage codes.
VJOY_DEVICE = 1
VJOY_AXES = (("X", 0x30), ("Y", 0x31), ("Z", 0x32), ("Rx", 0x33), ("Ry", 0x34))

# CONSTRAINT: 2.2.0 is the floor. Below it concurrent effects share block index 1 and collapse
# into one, silently. See docs/vjoy.md.
VJOY_MIN_VERSION = 0x0220

VJOY_DOWNLOAD = "https://github.com/BrunnerInnovation/vJoy"


def _vjoy_dll():
    """The raw SDK handle, with the return types we need declared. None if vJoy is absent."""
    try:
        import pyvjoy._sdk as sdk
    except Exception:
        return None, None
    dll = getattr(sdk, "_vj", None)
    if dll is None:
        return None, sdk
    for name, restype in (("GetvJoyVersion", ctypes.c_short),
                          ("vJoyEnabled", ctypes.c_bool),
                          ("IsDeviceFfb", ctypes.c_bool),
                          ("GetVJDAxisExist", ctypes.c_bool)):
        fn = getattr(dll, name, None)
        if fn is not None:
            fn.restype = restype
    return dll, sdk


def vjoy_version_text(raw):
    """0x0222 is version 2.2.2. Each nibble is one decimal digit, not a byte."""
    return "%d.%d.%d" % ((raw >> 8) & 0xF, (raw >> 4) & 0xF, raw & 0xF)


def vjoy_check():
    """
    (ok, badge, title, guidance) for vJoy device 1, without acquiring it.

    CONSTRAINT: query only. Acquiring would fight the bridge for the device. Checks run worst
    first and stop at the first real problem, because a missing driver makes every later
    answer meaningless.
    """
    dll, sdk = _vjoy_dll()
    if sdk is None:
        return (False, "pyvjoy not installed", "vJoy driver not found",
                "The Python side of vJoy is missing from this install, which\n"
                "should not happen in the packaged bundle. Re-extract the download.")
    if dll is None or not dll.vJoyEnabled():
        return (False, "not installed", "vJoy is not installed",
                "This bridge presents your wheel to games as a vJoy virtual\n"
                "device, so vJoy has to be installed first.\n\n"
                "Install vJoy 2.2.2.0 from:\n  %s\n\n"
                "Then open 'Configure vJoy' and enable device 1." % VJOY_DOWNLOAD)

    raw = dll.GetvJoyVersion()
    if raw and raw < VJOY_MIN_VERSION:
        return (False, "version %s is too old" % vjoy_version_text(raw),
                "vJoy %s is too old" % vjoy_version_text(raw),
                "Install 2.2.2.0 from:\n  %s\n\n"
                "Below 2.2.0 every concurrent force-feedback effect shares block\n"
                "index 1, so a game sending a spring, a damper and a road texture\n"
                "has all three collapse into one.\n\n"
                "That failure is silent. Nothing errors, one effect at a time still\n"
                "tests fine, and the only symptom is that the wheel feels wrong in a\n"
                "real game."
                % VJOY_DOWNLOAD)

    from pyvjoy.constants import (
        VJD_STAT_BUSY,
        VJD_STAT_FREE,
        VJD_STAT_MISS,
        VJD_STAT_OWN,
    )
    try:
        st = sdk.GetVJDStatus(VJOY_DEVICE)
    except Exception as exc:
        return (False, "query failed", "Could not ask vJoy about device 1", str(exc))

    if st == VJD_STAT_MISS:
        return (False, "device 1 missing",
                "vJoy is installed but device 1 is not configured",
                "Open 'Configure vJoy' from the Start menu, tick device 1, and give it:\n\n"
                "  Axes:    X, Y, Z, Rx, Ry\n"
                "  Buttons: a handful, 12 is plenty\n"
                "  Force Feedback: tick 'Enable Effects'\n\n"
                "Then press Apply. The bridge feeds device 1 specifically.")
    if st == VJD_STAT_BUSY:
        return (False, "device 1 busy",
                "Another program already owns vJoy device 1",
                "Something else is holding the device, often a bridge left running from an "
                "earlier session, or another vJoy feeder.\n\n"
                "Close it and press Stop here, then try again.")
    if st not in (VJD_STAT_FREE, VJD_STAT_OWN):
        return (False, "unknown status %s" % st, "vJoy returned a status we do not recognise",
                "GetVJDStatus(%d) returned %r. Nothing here knows what that means, so treat "
                "the device as unusable until it reads free or owned." % (VJOY_DEVICE, st))

    # The device exists. Now the two configuration mistakes that still let it exist.
    if not dll.IsDeviceFfb(VJOY_DEVICE):
        return (False, "device 1 has no force feedback",
                "Device 1 exists but force feedback is switched off",
                "In 'Configure vJoy', select device 1 and tick 'Enable Effects' under Force "
                "Feedback, then Apply.\n\n"
                "Without it steering and pedals still work, so the game looks fine, and the "
                "wheel simply never pushes back.")

    missing = [name for name, usage in VJOY_AXES
               if not dll.GetVJDAxisExist(VJOY_DEVICE, usage)]
    if missing:
        return (False, "device 1 missing %s" % ", ".join(missing),
                "Device 1 is missing axes",
                "In 'Configure vJoy', select device 1 and enable these axes: %s\n\n"
                "Missing: %s\n\n"
                "A game binds steering, throttle and brake to specific axes, so an absent one "
                "cannot be bound at all." % (", ".join(n for n, _ in VJOY_AXES),
                                             ", ".join(missing)))

    where = "ours" if st == VJD_STAT_OWN else "free"
    return (True, "OK, device 1 %s, v%s" % (where, vjoy_version_text(raw)), None, None)


# --------------------------------------------------------------------------- game notes

class GameNotes(object):
    """games.json, looked up by exe name. The unknown case matters as much as the known one."""

    def __init__(self):
        self.games = []
        try:
            with open(GAMES, encoding="utf-8") as fh:
                self.games = json.load(fh).get("games", [])
        except Exception:
            pass

    def find(self, exe_path):
        if not exe_path:
            return None
        base = os.path.basename(exe_path).lower()
        for g in self.games:
            if base in [e.lower() for e in g.get("exe") or []]:
                return g
        return None

    @staticmethod
    def describe(game, exe_path):
        if game is None:
            if not exe_path:
                return "Pick a game exe and anything known about it appears here."
            return ("No notes for %s yet.\n\n"
                    "That does not mean it will not work, only that nobody has written it "
                    "up. If you get it running, GAMES.md has a template, and the entry "
                    "belongs in games.json." % os.path.basename(exe_path))

        out = ["%s: %s" % (game["name"], game.get("summary", ""))]
        if game.get("verified"):
            out.append("Last verified %s." % game["verified"])
        if game.get("setup_required"):
            out.append("")
            out.append("Needs setup before it works. See %s." % game.get("docs", "GAMES.md"))
        if game.get("sends_ffb") is False:
            out.append("")
            out.append("This game sends no force feedback of its own. Spring and Damper "
                       "below are how you get centring here.")
        for q in game.get("quirks") or []:
            out.append("")
            out.append(" * " + q)
        return "\n".join(out)


# --------------------------------------------------------------------------- the window

class App(object):

    def __init__(self, root):
        self.root = root
        self.notes = GameNotes()
        self.view = ShimView()
        self.proc = None
        # Whether the bridge was ever seen alive, so one that dies at startup is reported.
        # play.ps1 outlives it, so its exit code says nothing about the bridge underneath.
        self.started_at = None
        self.bridge_seen = False
        self.warned_no_bridge = False
        # Set when a stop has been asked for and not yet happened. See _stop.
        self.stop_deadline = None
        self.tune = self._load_tune()
        self.game = tk.StringVar(value=self._load_setting("game", ""))

        # A plain title is the repo checkout, a version is the bundle. Which install a
        # screenshot came from stops being a question.
        version = bundle_version()
        root.title(("WH33LH4X %s" % version) if version else "WH33LH4X")
        root.minsize(620, 640)
        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)

        self._build_game(main)
        self._build_status(main)
        self._build_notes(main)
        self._build_feel(main)

        self.log = tk.StringVar(value="Ready.")
        ttk.Separator(main).pack(fill="x", pady=(8, 4))
        ttk.Label(main, textvariable=self.log, foreground="#555").pack(anchor="w")

        self._refresh_notes()
        self._poll()
        root.protocol("WM_DELETE_WINDOW", self._quit)

    # -- sections ------------------------------------------------------------

    def _build_game(self, parent):
        box = ttk.LabelFrame(parent, text="Game", padding=8)
        box.pack(fill="x")
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.game).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=self._browse).pack(side="left", padx=(6, 0))

        # Writes a path into the entry above, which stays the one thing _start reads.
        self.recent_row = ttk.Frame(box)
        ttk.Label(self.recent_row, text="Recent:").pack(side="left")
        self.recent_pick = tk.StringVar()
        self.recent_box = ttk.Combobox(self.recent_row, textvariable=self.recent_pick,
                                       state="readonly", width=40)
        self.recent_box.pack(side="left", padx=(6, 0))
        self.recent_box.bind("<<ComboboxSelected>>", self._pick_recent)

        self.buttons_row = ttk.Frame(box)
        self.buttons_row.pack(fill="x", pady=(8, 0))
        self.start_btn = ttk.Button(self.buttons_row, text="Start", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(self.buttons_row, text="Stop", command=self._stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))

        # After the buttons exist: showing the row later puts it back above them, and
        # before= needs something to be before.
        self._refresh_recent()

    def _build_status(self, parent):
        box = ttk.LabelFrame(parent, text="Status", padding=8)
        box.pack(fill="x", pady=(10, 0))
        self.badges = {}
        for key, label in (("vjoy", "vJoy"), ("bridge", "Bridge"), ("shim", "Shim")):
            row = ttk.Frame(box)
            row.pack(fill="x")
            ttk.Label(row, text=label, width=8).pack(side="left")
            var = tk.StringVar(value="—")
            ttk.Label(row, textvariable=var).pack(side="left")
            self.badges[key] = var
            if key == "vjoy":
                # Shown only when the check finds something.
                self.vjoy_fix = ttk.Button(row, text="How do I fix this?", width=18,
                                           command=self._vjoy_help)
                self.vjoy_problem = None

    def _build_notes(self, parent):
        box = ttk.LabelFrame(parent, text="About this game", padding=8)
        box.pack(fill="both", expand=True, pady=(10, 0))
        self.notes_text = tk.Text(box, height=10, wrap="word", relief="flat",
                                  background=parent.winfo_toplevel().cget("background"))
        self.notes_text.pack(fill="both", expand=True)
        self.notes_text.configure(state="disabled")

    def _build_feel(self, parent):
        box = ttk.LabelFrame(parent, text="Force feedback", padding=8)
        box.pack(fill="x", pady=(10, 0))
        self.feel_vars = {}

        # Strength is set apart: it scales the game's own force, while the three below add
        # force the game never sent.
        self._feel_row(box, STRENGTH)
        ttk.Separator(box, orient="horizontal").pack(fill="x", pady=6)
        ttk.Label(box, text="Added force the game is not sending:",
                  foreground="#555").pack(anchor="w", pady=(0, 2))
        for spec in FEEL:
            self._feel_row(box, spec)

        self.feel_hint = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.feel_hint, foreground="#a05000",
                  wraplength=560, justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Button(box, text="What are these?",
                   command=lambda: self._info("Strength, Spring, Damper and Friction", PD_NOTE)
                   ).pack(anchor="w", pady=(4, 0))

    # -- behaviour -----------------------------------------------------------

    # -- the recent list -----------------------------------------------------

    def _recent_label(self, path):
        """What to call a remembered game. Its real name when games.json knows the exe."""
        game = self.notes.find(path)
        if game:
            return game["name"]
        return os.path.basename(path)

    def _refresh_recent(self):
        """
        Rebuild the dropdown from the stored paths, and hide it when it has nothing to add.

        A dropdown holding one item is a control that cannot do anything, so it only appears
        once there is a second game to switch to.
        """
        paths = [p for p in self._load_setting("recent", []) if isinstance(p, str)]
        labels = [self._recent_label(p) for p in paths]
        # Two installs of the same game, or two unknown exes with the same filename, would
        # otherwise be one label appearing twice with no way to tell which is which.
        # Counted against a copy: renaming in place would drop the count for the entries not
        # reached yet, so the third install of a game would keep the ambiguous label.
        seen = list(labels)
        for i, label in enumerate(labels):
            if seen.count(label) > 1:
                labels[i] = "%s  (%s)" % (label, os.path.basename(os.path.dirname(paths[i])))
        self.recent_paths = dict(zip(labels, paths))
        self.recent_box.configure(values=labels)
        if len(labels) > 1:
            self.recent_row.pack(fill="x", pady=(6, 0), before=self.buttons_row)
        else:
            self.recent_row.pack_forget()

    def _pick_recent(self, _event=None):
        path = self.recent_paths.get(self.recent_pick.get())
        if path:
            self.game.set(path)
            self._save_setting("game", path)
            self._refresh_notes()

    def _remember(self, path):
        """Move a game to the front of the recent list. Called on Start, not on Browse."""
        paths = [p for p in self._load_setting("recent", []) if isinstance(p, str)]
        paths = [p for p in paths if p.lower() != path.lower()]
        self._save_setting("recent", ([path] + paths)[:RECENT_MAX])
        self._refresh_recent()

    def _browse(self):
        path = filedialog.askopenfilename(title="Pick the game executable",
                                          filetypes=[("Game executable", "*.exe")])
        if path:
            self.game.set(path)
            self._save_setting("game", path)
            self._refresh_notes()

    def _refresh_notes(self):
        game = self.notes.find(self.game.get())
        self.notes_text.configure(state="normal")
        self.notes_text.delete("1.0", "end")
        self.notes_text.insert("1.0", GameNotes.describe(game, self.game.get()))
        self.notes_text.configure(state="disabled")

    def _start(self):
        exe = self.game.get().strip()
        if not exe:
            self._say("Pick a game executable first.")
            return
        if not os.path.isfile(exe):
            # Remembered games go stale: uninstalled, moved, or on a drive that is not awake.
            # The list keeps the entry, because dropping a game because an external disk was
            # asleep would be worse than a message saying where it was.
            self._say("That file is not there any more: %s" % exe)
            return

        game = self.notes.find(exe)
        steam = is_steam_game(exe, game)
        appid = (game or {}).get("steam_appid")

        ps = os.path.join(ROOT, "play.ps1")
        # -Quiet keeps the bridge's console off screen. This window already reports bridge
        # state in its Status panel, so a console adds a second window and no information.
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps,
               "-Game", exe, "-Quiet"]
        # CONSTRAINT: Steam DRM relaunches the game through the client, so a directly started
        # exe exits at once and reads as the game quitting. -NoLaunch deploys the shim, starts
        # the bridge, then waits for the process however it was started. See GAMES.md.
        if steam:
            cmd.append("-NoLaunch")
        try:
            # CREATE_NO_WINDOW: the point of a GUI is not to spawn a console behind it.
            self.proc = subprocess.Popen(cmd, cwd=ROOT, creationflags=0x08000000)
            self.started_at = time.monotonic()
            self.bridge_seen = False
            self.warned_no_bridge = False
        except Exception as exc:
            self._say("Could not start: %s" % exc)
            return

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        # Remembered here rather than in _browse, so browsing to the wrong exe never fills the
        # list with things that were never played. RRRE64.exe against RRREWebBrowser.exe is
        # exactly that mistake.
        self._remember(exe)

        if not steam:
            self._say("Started. play.ps1 deploys the shim and launches the game.")
            return
        if appid:
            # The client has to start the game, but nobody has to be the one to ask it.
            try:
                os.startfile("steam://rungameid/%d" % appid)   # noqa: S606
                # CONSTRAINT: say what was requested, never what is true. The badges above
                # are the only things reading real state.
                self._say("Asked Steam to launch the game. play.ps1 is starting the "
                          "bridge. Watch the Bridge badge.")
            except Exception as exc:
                self._say("Could not ask Steam to launch it (%s). Start it from Steam "
                          "yourself." % exc)
        else:
            self._say("Start the game from Steam now. play.ps1 waits up to 10 minutes.")

    def _stop(self):
        """
        Ask play.ps1 to stop, and kill it only if asking does not work.

        CONSTRAINT: do not terminate() first. That is TerminateProcess on Windows, so
        PowerShell never unwinds and its finally block never runs, leaving a bridge holding
        vJoy and a dinput8.dll in the game folder. See docs/development.md#the-window.
        """
        if self.proc is None or self.proc.poll() is not None:
            self._ended()
            return
        self._request_stop()
        # Waiting here would freeze the window. _poll runs four times a second and already
        # notices the process ending, so it does the waiting.
        self.stop_deadline = time.monotonic() + STOP_GRACE
        self.stop_btn.configure(state="disabled")
        self._say("Stopping. play.ps1 is cleaning up the game folder.")

    def _request_stop(self):
        """Touch the file play.ps1 polls for. Returns whether it worked."""
        try:
            with open(STOP_REQUEST, "w", encoding="utf-8") as fh:
                fh.write("stop\n")
            return True
        except Exception:
            return False               # killing is the fallback for exactly this

    def _ended(self):
        self.proc = None
        self.stop_deadline = None
        try:
            os.remove(STOP_REQUEST)
        except OSError:
            pass
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def _feel_row(self, box, spec):
        key, label, lo, hi, hint = spec
        row = ttk.Frame(box)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text=label, width=8).pack(side="left")
        var = tk.DoubleVar(value=float(self.tune.get(key, TUNE_DEFAULTS.get(key, 0.0))))
        scale = ttk.Scale(row, from_=lo, to=hi, variable=var, length=150,
                          command=lambda _v, k=key: self._feel_changed(k))
        scale.pack(side="left")
        val = ttk.Label(row, width=5)
        val.pack(side="left", padx=(6, 6))
        ttk.Label(row, text=hint, foreground="#555").pack(side="left")
        self.feel_vars[key] = (var, val)
        self._update_feel_label(key)

    def _feel_changed(self, key):
        self._update_feel_label(key)
        self.tune[key] = round(self.feel_vars[key][0].get(), 3)
        self._save_tune()
        # The hint follows the slider just moved. A hint about Spring left standing while
        # somebody drags Strength reads as a comment on Strength.
        if key == "strength":
            self._strength_hint()
            return
        spring = self.tune.get("spring", 0.0)
        damper = self.tune.get("damper", 0.0)
        if spring >= 0.3 and damper <= 0.01:
            self.feel_hint.set("Spring without Damper tends to hunt. Try a Damper of about "
                               "half your Spring.")
        elif spring > 0.0:
            self.feel_hint.set("A spring is weakest near centre, and this wheel has spots "
                               "there it will not move at any usable force. Expect it to "
                               "stop just short. That is the hardware, not the setting.")
        else:
            self.feel_hint.set("")

    def _strength_hint(self):
        """
        Explain the two ways this slider stops behaving like a volume knob.

        max_force is not on screen, so a slider that visibly moves while the wheel does not
        change is otherwise unexplainable. It clips whenever the scaled force exceeds it, and
        a game at full lock sends 1.0, so strength above max_force buys nothing there.
        """
        strength = self.tune.get("strength", 1.0)
        ceiling = self.tune.get("max_force", 1.0)
        if strength <= 0.0:
            self.feel_hint.set("Zero switches the game's own force off. Only Spring, Damper "
                               "and Friction are left.")
        elif ceiling and strength > ceiling:
            self.feel_hint.set("max_force in tune.json caps output at %.2f, so the strongest "
                               "moments the game sends are already clipped. Raising this "
                               "further only affects the quieter ones." % ceiling)
        else:
            self.feel_hint.set("")

    def _update_feel_label(self, key):
        var, label = self.feel_vars[key]
        label.configure(text="%.2f" % var.get())

    def _poll(self):
        shim_alive, state, bridge_alive = self.view.read()
        ok, text, title, guidance = vjoy_check()
        self.badges["vjoy"].set(text)
        self.vjoy_problem = None if ok else (title, guidance)
        if ok:
            self.vjoy_fix.pack_forget()
        else:
            self.vjoy_fix.pack(side="right")
        self.badges["bridge"].set("running" if bridge_alive else "not running")
        self.badges["shim"].set({
            IpcMotorSink.STATE_NONE: "no motor",
            IpcMotorSink.STATE_MOTOR: "motor idle",
            IpcMotorSink.STATE_ACTIVE: "driving",
        }.get(state, "—") if shim_alive else "not in a game")

        if bridge_alive:
            self.bridge_seen = True

        if self.proc is not None and self.proc.poll() is not None:
            self._say("play.ps1 exited (code %s). The game folder has been cleaned up."
                      % self.proc.returncode)
            self._ended()
        elif (self.proc is not None and not self.bridge_seen and not self.warned_no_bridge
              and self.started_at is not None
              and time.monotonic() - self.started_at > BRIDGE_GRACE):
            # play.ps1 survives a bridge that died at startup, so nothing else notices, and
            # the window sits looking healthy while the game gets no wheel input.
            self.warned_no_bridge = True
            # Quote the log rather than pointing at it. The bridge always says why it gave up,
            # in a console nobody can see, in a file nobody opens. "vJoy device 1 is busy"
            # sitting on screen is the difference between five seconds and a lost test run.
            reason = last_log_line()
            if reason:
                self._say("The bridge did not start: %s" % reason)
            else:
                self._say("The bridge has not come up after %d seconds. The game will get no "
                          "wheel input. Check the newest log in logs." % int(BRIDGE_GRACE))

        if (self.stop_deadline is not None and self.proc is not None
                and time.monotonic() > self.stop_deadline):
            # Asking did not work. Kill it, and say what that leaves behind rather than
            # reporting a clean stop that did not happen.
            self.stop_deadline = None
            self.proc.terminate()
            self._say("play.ps1 did not stop when asked, so it was killed. The shim may "
                      "still be in the game folder.")
        self.root.after(POLL_MS, self._poll)

    def _vjoy_help(self):
        if self.vjoy_problem is None:
            return
        title, guidance = self.vjoy_problem
        self._info(title, guidance)

    # -- small helpers -------------------------------------------------------

    def _say(self, text):
        self.log.set(text)

    def _info(self, title, body):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.minsize(460, 240)
        txt = tk.Text(win, wrap="word", padx=10, pady=10, relief="flat")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", body)
        txt.configure(state="disabled")

    def _load_tune(self):
        try:
            with open(TUNE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_tune(self):
        # live_tune re-reads tune.json within half a second, so a slider takes effect while
        # the game is running. Write the whole file: it is the same document the user may be
        # editing by hand, and dropping keys we did not set would silently reset their tuning.
        try:
            with open(TUNE, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(self.tune, fh, indent=2)
                fh.write("\n")
        except Exception as exc:
            self._say("Could not write tune.json: %s" % exc)

    def _load_setting(self, key, default):
        try:
            with open(SETTINGS, encoding="utf-8") as fh:
                return json.load(fh).get(key, default)
        except Exception:
            return default

    def _save_setting(self, key, value):
        data = {}
        try:
            with open(SETTINGS, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            pass
        data[key] = value
        try:
            with open(SETTINGS, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(data, fh, indent=2)
        except Exception:
            pass

    def _quit(self):
        # Unlike the Stop button, there is no poll loop left to finish the job: the window is
        # going away. So this one waits, and closing takes a few seconds when a game was
        # running. That is the right trade against leaving a bridge on vJoy and a dinput8.dll
        # in somebody's game folder, which is what closing this window used to do.
        if self.proc is not None and self.proc.poll() is None:
            self._request_stop()
            try:
                self.proc.wait(timeout=STOP_GRACE)
            except Exception:
                self.proc.terminate()
            self._ended()
        self.view.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
