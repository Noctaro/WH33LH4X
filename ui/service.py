"""Runs the bridge and the force test as hidden child processes."""

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOP_FILE = os.path.join(ROOT, "stop.request")
LOGS = os.path.join(ROOT, "logs")
STOP_GRACE = 10.0       # arming takes ~5 s before the bridge first looks at the stop file
CREATE_NO_WINDOW = 0x08000000


def python_exe():
    """A console Python: under pythonw a child has no stdout, and the bridge's log tees it."""
    head, tail = os.path.split(sys.executable)
    if tail.lower() == "pythonw.exe":
        console = os.path.join(head, "python.exe")
        if os.path.exists(console):
            return console
    return sys.executable


def command(module, *args):
    return [python_exe(), "-m", module] + list(args)


def spawn(cmd, capture=False):
    """Start cmd with no console window; its own log is where its output goes."""
    kwargs = {"cwd": ROOT, "stdin": subprocess.DEVNULL,
              "stdout": subprocess.PIPE if capture else subprocess.DEVNULL,
              "stderr": subprocess.STDOUT if capture else subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    if capture:
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    return subprocess.Popen(cmd, **kwargs)


def newest_log(prefix="bridge_", since=0.0):
    try:
        logs = [os.path.join(LOGS, n) for n in os.listdir(LOGS)
                if n.startswith(prefix) and n.endswith(".log")]
    except OSError:
        return None
    logs = [path for path in logs if os.path.getmtime(path) >= since]
    return max(logs, key=os.path.getmtime) if logs else None


def log_reason(path):
    """The exit.fatal reason a bridge log gives, or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "exit.fatal" in line and "reason=" in line:
                    return line.split("reason=", 1)[1].strip()
    except (OSError, TypeError):
        pass
    return None


class BridgeProcess(object):
    """python -m bridge, stopped through the stop file so the motor is zeroed on every stop."""

    def __init__(self, spawner=spawn, clock=time.monotonic, stop_file=STOP_FILE):
        self.spawner = spawner
        self.clock = clock
        self.stop_file = stop_file
        self.proc = None
        self.started_wall = None
        self.stop_deadline = None
        self.killed = False

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, tune_path):
        if self.running():
            return
        _remove(self.stop_file)     # a leftover file would stop the new bridge at once
        self.started_wall = time.time()
        self.stop_deadline = None
        self.killed = False
        self.proc = self.spawner(command("bridge", "--stop-file", self.stop_file,
                                         "--tune", tune_path))

    def stop(self):
        """Ask the bridge to stop; poll() kills it if it has not gone after STOP_GRACE."""
        if not self.running():
            return
        with open(self.stop_file, "w", encoding="utf-8") as handle:
            handle.write("stop\n")
        self.stop_deadline = self.clock() + STOP_GRACE

    def poll(self):
        """None while running or idle; the exit code once, when the process has ended."""
        if self.proc is None:
            return None
        code = self.proc.poll()
        if code is None:
            if self.stop_deadline is not None and self.clock() > self.stop_deadline:
                self.proc.kill()
                self.killed = True
                self.stop_deadline = None
            return None
        self.proc = None
        self.stop_deadline = None
        _remove(self.stop_file)
        return code

    def wait_stopped(self):
        """Stop and block until gone, for closing the window."""
        if not self.running():
            return
        self.stop()
        try:
            self.proc.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        _remove(self.stop_file)

    def reason(self):
        """Why the last run ended with an error, from its log."""
        return log_reason(newest_log(since=(self.started_wall or 0.0) - 1.0))


class NudgeProcess(object):
    """python -m bridge.nudge, whose last output line is the result."""

    def __init__(self, spawner=spawn):
        self.spawner = spawner
        self.proc = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        self.proc = self.spawner(command("bridge.nudge"), capture=True)

    def poll(self):
        """None while running; (code, last line) once it has ended."""
        if self.proc is None or self.proc.poll() is None:
            return None
        output = self.proc.communicate()[0] or ""
        code, self.proc = self.proc.returncode, None
        lines = [line for line in output.splitlines() if line.strip()]
        return code, (lines[-1] if lines else "no output")


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
