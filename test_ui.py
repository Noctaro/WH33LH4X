"""
test_ui.py: profiles, the bridge process, the force test verdict, Linux setup; no window.

    python test_ui.py
"""

import json
import os
import shutil
import sys
import tempfile

from bridge.nudge import verdict
from live_tune import DEFAULTS
from ui import profiles, service, setup_linux


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


def fake_wheel(folder, control="on", node=True):
    """A sysfs tree and a device node folder holding one HORI wheel on bus 1, device 9."""
    sysfs, nodes = os.path.join(folder, "sys"), os.path.join(folder, "dev")
    os.makedirs(os.path.join(sysfs, "1-1", "power"))
    os.makedirs(os.path.join(nodes, "001"))
    for name, text in (("idVendor", "0f0d"), ("idProduct", "015c"), ("busnum", "1"),
                       ("devnum", "9"), (os.path.join("power", "control"), control)):
        with open(os.path.join(sysfs, "1-1", name), "w") as handle:
            handle.write(text + "\n")
    if node:
        with open(os.path.join(nodes, "001", "009"), "w"):
            pass
    return sysfs, nodes


def test_linux_setup():
    print("\nLinux setup checks")
    folder = tempfile.mkdtemp()
    try:
        empty = os.path.join(folder, "empty")
        os.makedirs(empty)
        ok = check("no wheel is not connected",
                   setup_linux.wheel_check(empty, empty)[1] == "not connected")

        sysfs, nodes = fake_wheel(os.path.join(folder, "a"))
        ok &= check("a free, awake, reachable wheel is OK",
                    setup_linux.wheel_check(sysfs, nodes)[0])
        if sys.platform == "win32":
            print("  SKIP a bound driver (Windows allows no ':' in a file name)")
        else:
            os.makedirs(os.path.join(sysfs, "1-1:1.0"))
            os.symlink(os.path.join(folder, "xone-wired"),
                       os.path.join(sysfs, "1-1:1.0", "driver"))
            result = setup_linux.wheel_check(sysfs, nodes)
            ok &= check("a bound driver is OK and named",
                        result[0] and "xone-wired" in result[1])

        sysfs, nodes = fake_wheel(os.path.join(folder, "b"), control="auto")
        result = setup_linux.wheel_check(sysfs, nodes)
        ok &= check("autosuspend is reported, with the setup script",
                    result[1] == "autosuspend on" and "setup.sh" in result[3])

        sysfs, nodes = fake_wheel(os.path.join(folder, "c"), node=False)
        result = setup_linux.wheel_check(sysfs, nodes)
        ok &= check("an unreachable device node is no access",
                    result[1] == "no access" and "setup.sh" in result[3])

        ok &= check("the setup script exists where the guidance says",
                    os.path.isfile(setup_linux.SCRIPT))
        node = os.path.join(folder, "uinput")
        ok &= check("a missing uinput node is not loaded",
                    setup_linux.uinput_check(node, evdev=True)[1] == "not loaded")
        with open(node, "w"):
            pass
        ok &= check("a writable uinput node is OK",
                    setup_linux.uinput_check(node, evdev=True)[0])
        ok &= check("python-evdev missing is reported",
                    setup_linux.uinput_check(node, evdev=False)[1] == "python-evdev missing")
        return ok
    finally:
        shutil.rmtree(folder)


def main():
    print("ui checks")
    results = [test_builtin_profiles(), test_apply_and_save(), test_bridge_process(),
               test_nudge_verdict(), test_linux_setup()]
    print()
    if all(results):
        print("ALL CHECKS PASSED")
        return 0
    print(">>> FAILURES ABOVE.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
