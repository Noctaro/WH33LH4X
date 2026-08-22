"""
Session logging: everything the probe printed, plus the dense data it did not print.

WHY
---
Force-feedback problems are diagnosed from timing and from signals that scroll past too
fast to read -- position samples arriving 50x a second, exactly when foreground was lost,
which effect was loaded when the wheel stopped responding. The console shows a throttled
summary because a human has to read it live; the log gets everything.

Two channels:

  * Console output is TEE'd verbatim, with an elapsed timestamp added in the file only,
    so the log reads in the same order as the session did.
  * event() writes structured lines to the file ONLY. This is where per-tick samples go --
    far too noisy for the console, exactly what is needed afterwards.

The file is flushed after every line, so a crash, a Ctrl+C or a hard kill still leaves a
complete log up to that moment.
"""

import os
import sys
import threading
import time
from datetime import datetime

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

_lock = threading.Lock()
_file = None
_start_time = None
_path = None
_saved_stdout = None
_saved_stderr = None


def _write_line(channel, text):
    """Append one already-split line to the log file. Caller holds no lock."""
    if _file is None:
        return
    stamp = time.monotonic() - _start_time
    with _lock:
        try:
            _file.write("[%9.3f] %s %s\n" % (stamp, channel, text))
            _file.flush()
        except (IOError, OSError, ValueError):
            pass


class _Tee(object):
    """Passes writes through to the real stream and mirrors whole lines to the log."""

    def __init__(self, stream, channel):
        self._stream = stream
        self._channel = channel
        self._pending = ""

    def write(self, text):
        self._stream.write(text)
        # Buffer until a newline: print() emits the text and the newline separately, and
        # splitting on fragments would scatter one console line across several log lines.
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            _write_line(self._channel, line)
        return len(text)

    def flush(self):
        self._stream.flush()

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False

    def __getattr__(self, item):
        return getattr(self._stream, item)


def start(directory=LOG_DIR):
    """Begin logging. Returns the log path, or None if it could not be opened."""
    global _file, _start_time, _path, _saved_stdout, _saved_stderr
    if _file is not None:
        return _path

    try:
        if not os.path.isdir(directory):
            os.makedirs(directory)
        _path = os.path.join(
            directory, "wgi_probe_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        _file = open(_path, "w", encoding="utf-8")
    except (IOError, OSError) as exc:
        sys.stderr.write("  (could not open log file: %s)\n" % exc)
        _file = None
        return None

    _start_time = time.monotonic()
    _file.write("# wgi_probe session log -- %s\n" % datetime.now().isoformat(timespec="seconds"))
    _file.write("# channel '|' = console output, '.' = structured event\n")
    _file.write("# python %s\n" % sys.version.replace("\n", " "))
    _file.write("# argv %r\n" % (sys.argv,))
    _file.flush()

    _saved_stdout, _saved_stderr = sys.stdout, sys.stderr
    sys.stdout = _Tee(_saved_stdout, "|")
    sys.stderr = _Tee(_saved_stderr, "!")
    return _path


def stop():
    """Restore the real streams and close the file."""
    global _file, _path, _saved_stdout, _saved_stderr
    if _saved_stdout is not None:
        sys.stdout = _saved_stdout
        _saved_stdout = None
    if _saved_stderr is not None:
        sys.stderr = _saved_stderr
        _saved_stderr = None
    if _file is not None:
        with _lock:
            try:
                _file.write("# end\n")
                _file.close()
            except (IOError, OSError, ValueError):
                pass
        _file = None
    return _path


def path():
    return _path


def event(name, **fields):
    """
    Record a structured line in the log only -- never on the console.

    Floats are formatted with a sign and three decimals, because nearly every value here
    is a signed -1..1 reading or force and the sign is usually the point.
    """
    if _file is None:
        return
    parts = []
    for key in sorted(fields):
        value = fields[key]
        if isinstance(value, float):
            parts.append("%s=%+.3f" % (key, value))
        else:
            parts.append("%s=%s" % (key, value))
    _write_line(".", "%-18s %s" % (name, " ".join(parts)))


def note(text):
    """A one-off marker in the log only."""
    _write_line(".", text)
