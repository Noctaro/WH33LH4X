"""
test_ui.py: profiles, the bridge process handling and the force test verdict, no window.

    python test_ui.py
"""

import json
import os
import shutil
import sys
import tempfile

from bridge.nudge import verdict
from live_tune import DEFAULTS
from ui import profiles, service


def check(name, condition, detail=""):
    print("  %s %s%s" % ("PASS" if condition else "FAIL", name,
                         "" if condition else "   <-- " + detail))
    return bool(condition)


def test_builtin_profiles():
    print("\nbuilt-in profiles")
    loaded = profiles.load_all()
    names = [p.name for p in loaded]
    ok = check("Default comes first (%s)" % names, names[:1] == ["Default"])
    ok &= check("DiRT 4 and RC car exist", "DiRT 4" in names and "RC car" in names)
    for profile in loaded:
        unknown = set(profile.values) - set(profiles.KEYS)
        ok &= check("%s sets only known keys" % profile.name, not unknown, str(unknown))
    rc = next(p for p in loaded if p.name == "RC car")
    ok &= check("RC car is the tuned Linux spring",
                rc.values["centring"] == "tuned" and rc.values["spring"] == 0.25
                and rc.values["damper"] == 0.08)
    return ok


def test_apply_and_save():
    print("\napply, compare, save")
    folder = tempfile.mkdtemp()
    try:
        tune = os.path.join(folder, "tune.json")
        with open(tune, "w", encoding="utf-8") as handle:
            json.dump({"btn_up": 3.0, "spring": 0.7, "centring": "tuned"}, handle)
        profile = profiles.Profile("Test", os.path.join(folder, "test.json"),
                                   {"strength": 0.5, "invert": True})
        profiles.write_tune(profile.complete(), tune)
        written = profiles.read_tune(tune)
        ok = check("unset keys fall back to defaults",
                   written["spring"] == DEFAULTS["spring"] and written["centring"] == "linear")
        ok &= check("button assignments survive a switch", written.get("btn_up") == 3.0)
        values = profiles.current_values(tune)
        ok &= check("profile matches what it wrote", profile.matches(values))
        values["strength"] = 0.6
        ok &= check("a change shows as unsaved", not profile.matches(values))
        profile.save(values)
        ok &= check("save writes the new value",
                    profiles.load_all(folder)[0].values["strength"] == 0.6)
        created = profiles.create("My Game", values, folder)
        ok &= check("create writes a slug file",
                    os.path.basename(created.path) == "my_game.json")
        try:
            profiles.create("my game", values, folder)
            ok &= check("create refuses a taken name", False)
        except ValueError:
            ok &= check("create refuses a taken name", True)
        return ok
    finally:
        shutil.rmtree(folder)


class FakeProc(object):
    def __init__(self):
        self.code = None
        self.killed = False

    def poll(self):
        return self.code

    def kill(self):
        self.killed = True
        self.code = -9


def test_bridge_process():
    print("\nbridge process")
    folder = tempfile.mkdtemp()
    try:
        stop = os.path.join(folder, "stop.request")
        with open(stop, "w"):
            pass
        spawned = []
        now = [0.0]

        def spawner(cmd):
            spawned.append(cmd)
            return FakeProc()

        bridge = service.BridgeProcess(spawner=spawner, clock=lambda: now[0], stop_file=stop)
        bridge.start("tune.json")
        ok = check("a leftover stop file is removed before start", not os.path.exists(stop))
        ok &= check("runs python -m bridge with the stop file",
                    spawned[0][1:4] == ["-m", "bridge", "--stop-file"] and stop in spawned[0])
        proc = bridge.proc
        bridge.stop()
        ok &= check("stop writes the stop file", os.path.exists(stop))
        now[0] = service.STOP_GRACE - 1.0
        ok &= check("waits while within the grace", bridge.poll() is None and not proc.killed)
        proc.code = 0
        ok &= check("reports a clean exit once", bridge.poll() == 0 and bridge.poll() is None)
        ok &= check("the stop file is gone afterwards", not os.path.exists(stop))

        bridge.start("tune.json")
        proc = bridge.proc
        bridge.stop()
        now[0] += service.STOP_GRACE + 1.0
        bridge.poll()
        ok &= check("killed after the grace", proc.killed and bridge.killed)
        return ok
    finally:
        shutil.rmtree(folder)


def test_nudge_verdict():
    print("\nforce test verdict")
    ok = check("right then left is ok", verdict(0.08, -0.07) == "ok")
    ok &= check("left then right is reversed", verdict(-0.08, 0.07) == "reversed")
    ok &= check("no movement is still", verdict(0.001, -0.004) == "still")
    ok &= check("one side only is unclear", verdict(0.08, 0.0) == "unclear")
    return ok


def main():
    print("ui checks")
    results = [test_builtin_profiles(), test_apply_and_save(), test_bridge_process(),
               test_nudge_verdict()]
    print()
    if all(results):
        print("ALL CHECKS PASSED")
        return 0
    print(">>> FAILURES ABOVE.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
