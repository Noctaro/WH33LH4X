r"""
gui.py -- the window. Pick a game, start it, see whether the pieces are alive.

SCOPE, DELIBERATELY SMALL
-------------------------
This is the minimum that makes the tool usable without reading COMMANDS.md: choose a game,
start and stop it, three status badges, what is known about that particular game, and the
wheel-feel knobs. There is no setup wizard, no per-game installer, no live force meter. Those
were planned and cut -- they can be added once something here proves inadequate, which is a
better reason to build them than a plan written before anyone used it.

WHY TKINTER AND NOT A WEB UI
----------------------------
The alternative was Edge in app mode talking to a local HTTP server. That costs zero download,
but it opens a listening socket -- in a tool whose central problem is convincing Windows and a
suspicious user that an unsigned dinput8.dll is not malware. It also means two processes, two
languages and a lifecycle where closing the window does not stop the server.

tkinter is not in the embeddable runtime, so the bundle vendors tcl/tk: measured at +2.5 MB
zipped, and the three binaries (tcl86t.dll, tk86t.dll, _tkinter.pyd) are signed by the Python
Software Foundation. One process, one language, no port. It looks dated. That was accepted.

WHY THIS SHELLS OUT TO play.ps1 INSTEAD OF LAUNCHING THE GAME ITSELF
--------------------------------------------------------------------
play.ps1 deploys the shim, backs up a foreign dinput8.dll, starts the bridge, watches for the
game process and cleans up on every exit path including Ctrl+C. It is 315 lines and it is
verified on hardware. Porting it to Python is planned -- one launcher is better than two -- but
a rewrite whose only test is "play DiRT 4 and see" should not also be the thing carrying a new
UI. So: the GUI collects arguments, play.ps1 keeps doing the job it already does, and it gets
retired once its replacement has driven a real session.

RUN IT WITH pythonw.exe so there is no console window:
    .\.venv\Scripts\pythonw.exe gui.py
"""

import json
import mmap
import os
import struct
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, ttk

from motor_sink import IpcMotorSink

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(ROOT, "gui_settings.json")
TUNE = os.path.join(ROOT, "tune.json")
GAMES = os.path.join(ROOT, "games.json")

POLL_MS = 250

# The wheel-feel knobs, and the range the GUI allows.
#
# live_tune's own button-tuner permits up to 2.0. This caps lower ON PURPOSE. Spring and damper
# close a loop around a wheel strong enough to whip itself to full lock, through a game whose
# force already reflects wheel position from tens of milliseconds ago -- GAMES.md records that
# too much gain makes it hunt, and at worst sweep lock to lock. A slider is easier to move
# carelessly than a config file is to edit, so the slider gets the smaller range.
FEEL = [
    ("spring", "Spring", 0.0, 1.0,
     "pulls back to centre, harder the further off you are"),
    ("damper", "Damper", 0.0, 1.0,
     "resists how fast you turn -- this is what keeps the spring steady"),
    ("friction", "Friction", 0.0, 1.0,
     "constant drag whenever the wheel moves: weight, not centring"),
]

PD_NOTE = (
    "Why a spring at all: this wheel has a strong centring spring of its own, but it stays "
    "suspended for as long as the bridge holds the motor (measured 2026-08-26). Without one "
    "of these the wheel sits wherever you leave it.\n\n"
    "Spring and Damper are a pair. The spring does the centring; the damper stops it "
    "overshooting and hunting. Raise them together.\n\n"
    "If you know control theory: this is a PD controller. Spring is the proportional term "
    "(force from HOW FAR off centre the wheel is) and Damper is the derivative term (force "
    "from HOW FAST it is moving). A P term alone, in a loop with lag, oscillates -- the D "
    "term is what makes it settle. It is the same reason raising strength on its own can "
    "make the wheel hunt."
)


# --------------------------------------------------------------------------- shared section

class ShimView(object):
    """
    READ-ONLY view of the shim's shared section.

    It does NOT use IpcMotorSink, and that is not a style preference. `IpcMotorSink.open()`
    stamps the bridge heartbeat, and the shim treats that stamp as "the bridge is alive" --
    so a GUI that opened one would make the shim hold the motor on behalf of a bridge that is
    not running. The struct layout is still taken from IpcMotorSink rather than copied, so
    there is one definition of the wire format and it stays matched to shim/ipc.h.
    """

    def __init__(self):
        self._mm = None

    def _attach(self):
        if self._mm is not None:
            return True
        try:
            self._mm = mmap.mmap(-1, IpcMotorSink._STRUCT.size, tagname=IpcMotorSink._NAME,
                                 access=mmap.ACCESS_READ)
        except Exception:
            self._mm = None
        return self._mm is not None

    def read(self):
        """(shim_alive, shim_state, bridge_alive) -- all False/None when nothing is running."""
        if not self._attach():
            return (False, IpcMotorSink.STATE_NONE, False)
        try:
            magic, _version = struct.unpack_from("<II", self._mm, 0)
            if magic != IpcMotorSink._MAGIC:
                return (False, IpcMotorSink.STATE_NONE, False)
            bridge_tick, shim_tick, state = struct.unpack_from("<QQI", self._mm, 16)
        except Exception:
            return (False, IpcMotorSink.STATE_NONE, False)

        import ctypes
        now = ctypes.windll.kernel32.GetTickCount64()
        fresh = IpcMotorSink._STALE_MS
        return (bool(shim_tick) and (now - shim_tick) < fresh,
                state,
                bool(bridge_tick) and (now - bridge_tick) < fresh)

    def close(self):
        if self._mm is not None:
            self._mm.close()
            self._mm = None


def is_steam_game(exe_path, game=None):
    """
    Does this game come up through the Steam client rather than from the exe we start?

    games.json is asked first, because a game can be a Steam title while sitting outside the
    default library. The path check is the fallback that works for titles nobody has written
    an entry for yet -- which is most of them.
    """
    if game is not None and game.get("launch"):
        return game["launch"] == "steam"
    return os.sep + "steamapps" + os.sep in exe_path.replace("/", os.sep).lower()


def vjoy_status():
    """(ok, text) for device 1, WITHOUT acquiring it -- acquiring would fight the bridge."""
    try:
        import pyvjoy._sdk as sdk
        from pyvjoy.constants import (
            VJD_STAT_BUSY,
            VJD_STAT_FREE,
            VJD_STAT_MISS,
            VJD_STAT_OWN,
        )
    except Exception:
        return (False, "pyvjoy not installed")
    try:
        st = sdk.GetVJDStatus(1)
    except Exception as exc:
        return (False, "query failed: %s" % exc)
    return {
        VJD_STAT_FREE: (True, "device 1 free"),
        VJD_STAT_OWN: (True, "device 1 ours"),
        VJD_STAT_BUSY: (False, "device 1 busy -- another app has it"),
        VJD_STAT_MISS: (False, "device 1 missing -- configure it in vJoyConf"),
    }.get(st, (False, "unknown status %s" % st))


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
                    "That does not mean it will not work -- it means nobody has written it "
                    "up. If you get it running, GAMES.md has a template, and the entry "
                    "belongs in games.json." % os.path.basename(exe_path))

        out = ["%s -- %s" % (game["name"], game.get("summary", ""))]
        if game.get("verified"):
            out.append("Last verified %s." % game["verified"])
        if game.get("setup_required"):
            out.append("")
            out.append("NEEDS SETUP BEFORE IT WORKS. See %s." % game.get("docs", "GAMES.md"))
        if game.get("sends_ffb") is False:
            out.append("")
            out.append("This game sends no force feedback of its own -- Spring and Damper "
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
        self.tune = self._load_tune()
        self.game = tk.StringVar(value=self._load_setting("game", ""))

        root.title("WH33LH4X")
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

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(8, 0))
        self.start_btn = ttk.Button(row2, text="Start", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(row2, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))

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

    def _build_notes(self, parent):
        box = ttk.LabelFrame(parent, text="About this game", padding=8)
        box.pack(fill="both", expand=True, pady=(10, 0))
        self.notes_text = tk.Text(box, height=10, wrap="word", relief="flat",
                                  background=parent.winfo_toplevel().cget("background"))
        self.notes_text.pack(fill="both", expand=True)
        self.notes_text.configure(state="disabled")

    def _build_feel(self, parent):
        box = ttk.LabelFrame(parent, text="Wheel feel -- adds force the game is not sending",
                             padding=8)
        box.pack(fill="x", pady=(10, 0))
        self.feel_vars = {}
        for key, label, lo, hi, hint in FEEL:
            row = ttk.Frame(box)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, width=8).pack(side="left")
            var = tk.DoubleVar(value=float(self.tune.get(key, 0.0)))
            scale = ttk.Scale(row, from_=lo, to=hi, variable=var, length=150,
                              command=lambda _v, k=key: self._feel_changed(k))
            scale.pack(side="left")
            val = ttk.Label(row, width=5)
            val.pack(side="left", padx=(6, 6))
            ttk.Label(row, text=hint, foreground="#555").pack(side="left")
            self.feel_vars[key] = (var, val)
            self._update_feel_label(key)

        self.feel_hint = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.feel_hint, foreground="#a05000",
                  wraplength=560, justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Button(box, text="What are these?",
                   command=lambda: self._info("Spring, Damper and Friction", PD_NOTE)
                   ).pack(anchor="w", pady=(4, 0))

    # -- behaviour -----------------------------------------------------------

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
        if not exe or not os.path.isfile(exe):
            self._say("Pick a game executable first.")
            return

        game = self.notes.find(exe)
        steam = is_steam_game(exe, game)
        appid = (game or {}).get("steam_appid")

        ps = os.path.join(ROOT, "play.ps1")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps,
               "-Game", exe]
        # STEAM DRM RELAUNCHES THE GAME THROUGH THE CLIENT, so the exe we start exits at once
        # and the real game appears as a different process. Launching it ourselves therefore
        # looks like the game quitting instantly and play.ps1 correctly tears everything down.
        # -NoLaunch is play.ps1's answer: deploy the shim, start the bridge, then WAIT for the
        # process to show up however it was started. GAMES.md has said so all along.
        if steam:
            cmd.append("-NoLaunch")
        try:
            # CREATE_NO_WINDOW: the point of a GUI is not to spawn a console behind it.
            self.proc = subprocess.Popen(cmd, cwd=ROOT, creationflags=0x08000000)
        except Exception as exc:
            self._say("Could not start: %s" % exc)
            return

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        if not steam:
            self._say("Started. play.ps1 deploys the shim and launches the game.")
            return
        if appid:
            # We can press the button for them: the client is what has to start the game, but
            # nothing says a person has to be the one to ask it.
            try:
                os.startfile("steam://rungameid/%d" % appid)   # noqa: S606
                self._say("Bridge running. Asked Steam to launch the game -- "
                          "play.ps1 is waiting for it.")
            except Exception as exc:
                self._say("Bridge running. Start the game from Steam now. (%s)" % exc)
        else:
            self._say("Bridge running. START THE GAME FROM STEAM NOW -- "
                      "play.ps1 waits up to 10 minutes for it.")

    def _stop(self):
        if self.proc is not None and self.proc.poll() is None:
            # terminate() lets play.ps1's finally block run, which is what removes the shim
            # from the game folder and restores any dinput8.dll it moved aside.
            self.proc.terminate()
            self._say("Stopping -- play.ps1 is cleaning up the game folder.")
        self._ended()

    def _ended(self):
        self.proc = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def _feel_changed(self, key):
        self._update_feel_label(key)
        self.tune[key] = round(self.feel_vars[key][0].get(), 3)
        self._save_tune()
        spring = self.tune.get("spring", 0.0)
        damper = self.tune.get("damper", 0.0)
        if spring >= 0.3 and damper <= 0.01:
            self.feel_hint.set("Spring without Damper tends to hunt. Try a Damper of about "
                               "half your Spring.")
        elif spring > 0.0:
            self.feel_hint.set("A spring is weakest near centre, and this wheel has spots "
                               "there it will not move at any usable force. Expect it to "
                               "stop just short -- that is the hardware, not the setting.")
        else:
            self.feel_hint.set("")

    def _update_feel_label(self, key):
        var, label = self.feel_vars[key]
        label.configure(text="%.2f" % var.get())

    def _poll(self):
        shim_alive, state, bridge_alive = self.view.read()
        ok, text = vjoy_status()
        self.badges["vjoy"].set(("OK -- " if ok else "") + text)
        self.badges["bridge"].set("running" if bridge_alive else "not running")
        self.badges["shim"].set({
            IpcMotorSink.STATE_NONE: "no motor",
            IpcMotorSink.STATE_MOTOR: "motor idle",
            IpcMotorSink.STATE_ACTIVE: "driving",
        }.get(state, "—") if shim_alive else "not in a game")

        if self.proc is not None and self.proc.poll() is not None:
            self._say("play.ps1 exited (code %s). The game folder has been cleaned up."
                      % self.proc.returncode)
            self._ended()
        self.root.after(POLL_MS, self._poll)

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
        if self.proc is not None and self.proc.poll() is None:
            self._stop()
        self.view.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
