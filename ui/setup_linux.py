"""Linux setup checks: the wheel and /dev/uinput are reachable and the wheel is kept awake."""

import importlib.util
import os

SYSFS = "/sys/bus/usb/devices"
USB_NODES = "/dev/bus/usb"
UINPUT = "/dev/uinput"
HORI_VID = "0f0d"
XBOX_PID = "015c"

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "packaging", "linux", "setup.sh")
RUN_SETUP = ("Run the setup script once:\n\n  sudo sh %s\n\n"
             "Then log out and back in, and replug the wheel." % SCRIPT)


def _read(*parts):
    try:
        with open(os.path.join(*parts)) as handle:
            return handle.read().strip()
    except OSError:
        return None


def _device_dirs(sysfs):
    try:
        names = os.listdir(sysfs)
    except OSError:
        return []
    return [name for name in names
            if _read(sysfs, name, "idVendor") == HORI_VID
            and _read(sysfs, name, "idProduct") == XBOX_PID]


def _usable(node):
    return os.access(node, os.R_OK | os.W_OK)


def wheel_check(sysfs=SYSFS, nodes=USB_NODES):
    """(ok, badge, title, guidance) for the wheel on Linux."""
    devices = _device_dirs(sysfs)
    if not devices:
        return (False, "not connected", "The wheel is not connected",
                "Plug the wheel in, in Xbox mode (long-press PROFILE).")
    device = devices[0]
    bus, number = _read(sysfs, device, "busnum"), _read(sysfs, device, "devnum")
    if bus and number and not _usable(os.path.join(nodes, "%03d" % int(bus),
                                                   "%03d" % int(number))):
        return (False, "no access", "This user may not open the wheel", RUN_SETUP)
    # CONSTRAINT: with no driver bound, Linux suspends the wheel after 2 s, and every resume
    # boots it into a calibration sweep that ignores force (measured 2026-09-26).
    control = os.path.join(sysfs, device, "power", "control")
    if _read(control) == "auto":
        return (False, "autosuspend on", "Linux suspends the wheel between uses",
                "Every time the bridge opens a suspended wheel, it boots and sweeps through a "
                "calibration that ignores force. %s\n\n"
                "Or keep it awake until the next replug with:\n\n"
                "  echo on | sudo tee %s\n\n"
                "It sweeps once as it wakes." % (RUN_SETUP, control))
    # A bound kernel driver is no problem: the bridge detaches it when it opens the wheel.
    driver = os.path.join(sysfs, device + ":1.0", "driver")
    if os.path.islink(driver):
        return (True, "OK, %s lets go at Start" % os.path.basename(os.readlink(driver)),
                None, None)
    return (True, "OK, free", None, None)


def uinput_check(node=UINPUT, evdev=None):
    """(ok, badge, title, guidance) for the virtual wheel games see."""
    if not os.path.exists(node):
        return (False, "not loaded", "The uinput module is not loaded",
                "Games see the wheel through %s, which does not exist. %s" % (node, RUN_SETUP))
    if not _usable(node):
        return (False, "no access", "This user may not create a virtual wheel",
                "%s is not writable for this user. %s" % (node, RUN_SETUP))
    if evdev is None:
        evdev = importlib.util.find_spec("evdev") is not None
    if not evdev:
        return (False, "python-evdev missing", "python-evdev is not installed",
                "Install it with:\n\n  sudo apt install python3-evdev\n\n"
                "or pip install evdev.")
    return (True, "OK", None, None)
