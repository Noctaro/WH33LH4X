"""The window: pick a profile, start and stop the bridge, tune the feel, check the setup."""

import ctypes
import json
import os
import sys
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from live_tune import ensure_user_tune
from ui import profiles, service

try:
    import sv_ttk
except ImportError:
    sv_ttk = None

if sys.platform == "win32":
    from ui import setup_win as setup
else:
    from ui import setup_linux as setup

ROOT = profiles.ROOT
SETTINGS = os.path.join(ROOT, "gui_settings.json")
HERE = os.path.dirname(os.path.abspath(__file__))
ICON_ICO = os.path.join(HERE, "icon.ico")     # 16 to 192 px, from logo/icons
ICON_PNG = os.path.join(HERE, "icon.png")

POLL_MS = 250
CHECK_MS = 2000         # setup checks enumerate USB and read the registry
WRITE_DELAY_MS = 150    # sliders write the tuning file once they pause, not per pixel

# (tune key, label, low, high, hint). Spring and damper stop at 1.0: they close a loop around
# the wheel and hunt at high gain, see GAMES.md.
SLIDERS = [
    ("strength", "Strength", 0.0, 1.0, "the game's own force; 1.00 is what it sent"),
    ("max_force", "Max force", 0.0, 1.0, "ceiling on everything below"),
    ("spring", "Spring", 0.0, 1.0, "pulls back to centre"),
    ("damper", "Damper", 0.0, 1.0, "resists turning speed; steadies the spring"),
    ("friction", "Friction", 0.0, 1.0, "drag whenever the wheel moves"),
]

NUDGE_TEXT = {
    "ok": "Test force passed: the wheel turned right, then left.",
    "reversed": "The wheel turned the WRONG way. Do not drive; report this with the log.",
    "still": "The wheel did not move. Check it is not held, then try again or replug it.",
    "unclear": "The wheel moved only one way. Hands off the wheel and try again.",
    "silent": "The wheel sent no input during the test. Wait for any calibration sweep to "
              "finish, then try again.",
}


def set_icon(root):
    """The logo on the title bar and the taskbar."""
    try:
        if sys.platform == "win32":
            root.iconbitmap(default=ICON_ICO)
        else:
            root.icon = tk.PhotoImage(file=ICON_PNG)
            root.iconphoto(True, root.icon)
    except tk.TclError:
        pass


def title_bar(root, dark):
    """Match the Windows title bar to the theme; sv-ttk themes only the client area."""
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        # 20 is DWMWA_USE_IMMERSIVE_DARK_MODE.
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value),
                                                   ctypes.sizeof(value))
    except (AttributeError, OSError):
        pass


def load_setting(key, default):
    try:
        with open(SETTINGS, encoding="utf-8") as handle:
            return json.load(handle).get(key, default)
    except (OSError, ValueError, AttributeError):
        return default


def save_setting(key, value):
    try:
        with open(SETTINGS, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    data[key] = value
    try:
        with open(SETTINGS, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2)
    except OSError:
        pass


class App(object):
    def __init__(self, root):
        self.root = root
        self.bridge = service.BridgeProcess()
        self.nudge = service.NudgeProcess()
        self.profiles = profiles.load_all()
        ensure_user_tune()
        self.values = profiles.current_values()
        self.problems = {}
        self._write_pending = None

        root.title("WH33LH4X")
        set_icon(root)
        root.minsize(560, 600)
        if sv_ttk is not None:
            sv_ttk.set_theme(load_setting("theme", "dark"))
        main = ttk.Frame(root, padding=14)
        main.pack(fill="both", expand=True)

        self._build_profile(main)
        self._build_bridge(main)
        self._build_feel(main)
        self._build_setup(main)
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(main, textvariable=self.status, wraplength=520,
                  justify="left").pack(anchor="w", pady=(10, 0))

        self._select_initial_profile()
        self._check_setup()
        self._poll()
        if sv_ttk is not None:
            root.after(100, lambda: title_bar(root, sv_ttk.get_theme() == "dark"))
        root.protocol("WM_DELETE_WINDOW", self._quit)

    # -- layout --------------------------------------------------------------

    def _build_profile(self, parent):
        box = ttk.LabelFrame(parent, text="Profile", padding=10)
        box.pack(fill="x")
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.profile_var = tk.StringVar()
        self.profile_box = ttk.Combobox(row, textvariable=self.profile_var, state="readonly",
                                        values=[p.name for p in self.profiles], width=24)
        self.profile_box.pack(side="left")
        self.profile_box.bind("<<ComboboxSelected>>", self._profile_picked)
        ttk.Button(row, text="Save as new...", command=self._save_as).pack(side="right")
        self.save_btn = ttk.Button(row, text="Save", command=self._save, state="disabled")
        self.save_btn.pack(side="right", padx=(0, 6))
        self.dirty_var = tk.StringVar()
        ttk.Label(row, textvariable=self.dirty_var).pack(side="left", padx=(10, 0))
        self.notes_var = tk.StringVar()
        ttk.Label(box, textvariable=self.notes_var, wraplength=500,
                  justify="left").pack(anchor="w", pady=(8, 0))

    def _build_bridge(self, parent):
        box = ttk.LabelFrame(parent, text="Bridge", padding=10)
        box.pack(fill="x", pady=(10, 0))
        self.start_btn = ttk.Button(box, text="Start", command=self._start,
                                    style="Accent.TButton")
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(box, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.bridge_var = tk.StringVar(value="stopped")
        ttk.Label(box, textvariable=self.bridge_var).pack(side="left", padx=(12, 0))

    def _build_feel(self, parent):
        box = ttk.LabelFrame(parent, text="Force feedback", padding=10)
        box.pack(fill="x", pady=(10, 0))
        self.sliders = {}
        for key, label, low, high, hint in SLIDERS:
            row = ttk.Frame(box)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=10).pack(side="left")
            var = tk.DoubleVar()
            ttk.Scale(row, from_=low, to=high, variable=var, length=180,
                      command=lambda _v, k=key: self._slider_moved(k)).pack(side="left")
            shown = ttk.Label(row, width=5)
            shown.pack(side="left", padx=(8, 8))
            ttk.Label(row, text=hint).pack(side="left")
            self.sliders[key] = (var, shown)
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        self.invert_var = tk.BooleanVar()
        ttk.Checkbutton(row, text="Invert the game's force", variable=self.invert_var,
                        command=self._toggles_changed).pack(side="left")
        self.tuned_var = tk.BooleanVar()
        ttk.Checkbutton(row, text="Tuned centring", variable=self.tuned_var,
                        command=self._toggles_changed).pack(side="left", padx=(16, 0))

    def _build_setup(self, parent):
        box = ttk.LabelFrame(parent, text="Setup", padding=10)
        box.pack(fill="x", pady=(10, 0))
        self.badges = {}
        rows = [("wheel", "Wheel")]
        if sys.platform == "win32":
            rows.insert(0, ("vjoy", "vJoy"))
        for key, label in rows:
            row = ttk.Frame(box)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, width=10).pack(side="left")
            var = tk.StringVar(value="checking...")
            ttk.Label(row, textvariable=var).pack(side="left")
            fix = ttk.Button(row, text="How to fix", command=lambda k=key: self._explain(k))
            self.badges[key] = (var, fix)
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        self.nudge_btn = ttk.Button(row, text="Test force", command=self._test_force)
        self.nudge_btn.pack(side="left")
        if sys.platform == "win32":
            self.restore_btn = ttk.Button(row, text="Restore Microsoft driver",
                                          command=self._restore)
            self.restore_btn.pack(side="left", padx=(6, 0))
        else:
            self.restore_btn = None
        if sv_ttk is not None:
            self.dark_var = tk.BooleanVar(value=sv_ttk.get_theme() == "dark")
            ttk.Checkbutton(row, text="Dark", variable=self.dark_var,
                            style="Switch.TCheckbutton",
                            command=self._theme_changed).pack(side="right")

    # -- profiles ------------------------------------------------------------

    def _profile(self):
        name = self.profile_var.get()
        return next((p for p in self.profiles if p.name == name), None)

    def _select_initial_profile(self):
        """The remembered profile, else whichever the tuning file matches."""
        remembered = load_setting("profile", None)
        match = next((p for p in self.profiles if p.name == remembered), None) \
            or next((p for p in self.profiles if p.matches(self.values)), None)
        if match is None and self.profiles:
            match = self.profiles[0]
        if match is not None:
            self.profile_var.set(match.name)
            self.notes_var.set(match.notes)
        self._show_values()

    def _profile_picked(self, _event=None):
        profile = self._profile()
        previous = load_setting("profile", None)
        old = next((p for p in self.profiles if p.name == previous), None)
        if old is not None and old is not profile and not old.matches(self.values):
            if not messagebox.askyesno("Unsaved changes",
                                       "Discard the unsaved changes to %s?" % old.name):
                self.profile_var.set(old.name)
                return
        if profile is None:
            return
        self.values.update(profile.complete())
        self._write_now()
        save_setting("profile", profile.name)
        self.notes_var.set(profile.notes)
        self._show_values()
        self._say("%s applied%s." % (profile.name, ", live" if self.bridge.running() else ""))

    def _save(self):
        profile = self._profile()
        if profile is None:
            return
        profile.save(self.values)
        self._refresh_dirty()
        self._say("Saved %s." % profile.name)

    def _save_as(self):
        name = simpledialog.askstring("Save as new profile", "Name for the new profile:",
                                      parent=self.root)
        if not name:
            return
        try:
            profile = profiles.create(name, self.values)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Could not save", str(exc))
            return
        self.profiles = profiles.load_all()
        self.profile_box.configure(values=[p.name for p in self.profiles])
        self.profile_var.set(profile.name)
        self.notes_var.set("")
        save_setting("profile", profile.name)
        self._refresh_dirty()
        self._say("Saved as %s." % profile.name)

    def _refresh_dirty(self):
        profile = self._profile()
        dirty = profile is not None and not profile.matches(self.values)
        self.dirty_var.set("unsaved changes" if dirty else "")
        self.save_btn.configure(state="normal" if dirty else "disabled")

    # -- feel ----------------------------------------------------------------

    def _show_values(self):
        for key, (var, shown) in self.sliders.items():
            var.set(float(self.values.get(key, 0.0)))
            shown.configure(text="%.2f" % var.get())
        self.invert_var.set(bool(self.values.get("invert")))
        self.tuned_var.set(self.values.get("centring") == "tuned")
        self._refresh_dirty()

    def _slider_moved(self, key):
        var, shown = self.sliders[key]
        value = round(var.get(), 2)
        shown.configure(text="%.2f" % value)
        self.values[key] = value
        self._write_soon()

    def _toggles_changed(self):
        self.values["invert"] = bool(self.invert_var.get())
        self.values["centring"] = "tuned" if self.tuned_var.get() else "linear"
        self._write_now()

    def _write_soon(self):
        if self._write_pending is not None:
            self.root.after_cancel(self._write_pending)
        self._write_pending = self.root.after(WRITE_DELAY_MS, self._write_now)

    def _write_now(self):
        self._write_pending = None
        try:
            profiles.write_tune({key: self.values[key] for key in profiles.KEYS})
        except OSError as exc:
            self._say("Could not write user-tune.json: %s" % exc)
        self._refresh_dirty()

    # -- bridge --------------------------------------------------------------

    def _start(self):
        self._write_now()
        try:
            self.bridge.start(profiles.TUNE)
        except OSError as exc:
            self._say("Could not start the bridge: %s" % exc)
            return
        self.bridge_var.set("starting: arming the wheel...")
        self._say("Starting. Start your game whenever you like.")
        self._update_buttons()

    def _stop(self):
        try:
            self.bridge.stop()
        except OSError as exc:
            self._say("Could not ask the bridge to stop: %s" % exc)
            return
        self.bridge_var.set("stopping...")
        self.stop_btn.configure(state="disabled")

    def _poll(self):
        code = self.bridge.poll()
        if code is not None:
            if code == 0 and not self.bridge.killed:
                self.bridge_var.set("stopped")
                self._say("Bridge stopped, force zeroed.")
            elif self.bridge.killed:
                self.bridge_var.set("stopped (killed)")
                self._say("The bridge did not stop when asked and was killed.")
            else:
                reason = self.bridge.reason()
                self.bridge_var.set("stopped with an error")
                self._say("The bridge stopped: %s" % (reason or "see the newest log in logs"))
        elif self.bridge.running() and self.bridge_var.get().startswith("starting"):
            self.bridge_var.set("running")

        result = self.nudge.poll()
        if result is not None:
            code, line = result
            words = line.split()
            verdict = words[1] if len(words) > 1 and words[0] == "nudge" else None
            message = NUDGE_TEXT.get(verdict) or line
            self._say("%s  (%s)" % (message, line) if verdict in NUDGE_TEXT else message)
        self._update_buttons()
        self.root.after(POLL_MS, self._poll)

    def _update_buttons(self):
        busy = self.bridge.running() or self.nudge.running()
        self.start_btn.configure(state="disabled" if busy else "normal")
        stopping = self.bridge.stop_deadline is not None
        self.stop_btn.configure(state="normal" if self.bridge.running() and not stopping
                                else "disabled")
        self.nudge_btn.configure(state="disabled" if busy else "normal")
        if self.restore_btn is not None:
            self.restore_btn.configure(state="disabled" if busy else "normal")

    # -- setup ---------------------------------------------------------------

    def _check_setup(self):
        checks = {"wheel": setup.wheel_check}
        if sys.platform == "win32":
            checks["vjoy"] = setup.vjoy_check
        for key, check in checks.items():
            # The bridge holds the wheel while it runs, so its state is the better answer.
            if key == "wheel" and self.bridge.running():
                continue
            try:
                ok, badge, title, guidance = check()
            except Exception as exc:                          # noqa: BLE001
                ok, badge, title, guidance = False, "check failed", "Check failed", str(exc)
            var, fix = self.badges[key]
            var.set(badge)
            self.problems[key] = None if ok else (title, guidance)
            if ok:
                fix.pack_forget()
            else:
                fix.pack(side="right")
        self.root.after(CHECK_MS, self._check_setup)

    def _explain(self, key):
        problem = self.problems.get(key)
        if problem is None:
            return
        title, guidance = problem
        win = tk.Toplevel(self.root)
        win.title(title)
        win.minsize(460, 260)
        text = tk.Text(win, wrap="word", padx=12, pady=12, relief="flat", height=14)
        text.pack(fill="both", expand=True)
        text.insert("1.0", guidance)
        text.configure(state="disabled")

    def _test_force(self):
        if not messagebox.askokcancel(
                "Test force", "The wheel will turn a little right, then left, then centre.\n\n"
                "Take your hands off the wheel and press OK. It takes about 10 seconds."):
            return
        try:
            self.nudge.start()
        except OSError as exc:
            self._say("Could not start the test: %s" % exc)
            return
        self._say("Testing force: arming the wheel, hands off...")
        self._update_buttons()

    def _restore(self):
        if not messagebox.askyesno(
                "Restore Microsoft driver",
                "This removes the WinUSB driver from the wheel so Windows uses its own Xbox "
                "driver again. Xbox games and the HORI app will see the wheel; this bridge "
                "will not, until WinUSB is set up again with Zadig.\n\n"
                "Windows asks for administrator rights. Continue?"):
            return
        ok, message = setup.restore_microsoft_driver()
        self._say(message)

    # -- misc ----------------------------------------------------------------

    def _theme_changed(self):
        theme = "dark" if self.dark_var.get() else "light"
        sv_ttk.set_theme(theme)
        title_bar(self.root, theme == "dark")
        # The title bar repaints on the next activation.
        self.root.withdraw()
        self.root.deiconify()
        save_setting("theme", theme)

    def _say(self, text):
        self.status.set(text)

    def _quit(self):
        if self._write_pending is not None:
            self._write_now()
        self.bridge.wait_stopped()
        self.root.destroy()


def main():
    if sys.platform == "win32":
        # Its own taskbar identity; otherwise the taskbar groups it under pythonw's icon.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WH33LH4X")
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0
