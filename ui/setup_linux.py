"""Linux setup checks: the wheel is connected, free of xone, and kept awake."""

import os

SYSFS = "/sys/bus/usb/devices"
HORI_VID = "0f0d"
XBOX_PID = "015c"


def _device_dirs():
    try:
        names = os.listdir(SYSFS)
    except OSError:
        return []
    found = []
    for name in names:
        path = os.path.join(SYSFS, name)
        try:
            with open(os.path.join(path, "idVendor")) as vid, \
                    open(os.path.join(path, "idProduct")) as pid:
                if vid.read().strip() == HORI_VID and pid.read().strip() == XBOX_PID:
                    found.append(name)
        except OSError:
            continue
    return found


def wheel_check():
    """(ok, badge, title, guidance) for the wheel on Linux."""
    devices = _device_dirs()
    if not devices:
        return (False, "not connected", "The wheel is not connected",
                "Plug the wheel in, in Xbox mode (long-press PROFILE).")
    interface = devices[0] + ":1.0"
    driver = os.path.join(SYSFS, interface, "driver")
    if os.path.islink(driver):
        name = os.path.basename(os.readlink(driver))
        return (False, "held by %s" % name, "%s holds the wheel" % name,
                "The bridge needs the wheel's interface free. Unbind it with:\n\n"
                "  echo -n '%s' | sudo tee /sys/bus/usb/drivers/%s/unbind\n\n"
                "Then wait for the calibration sweep to finish." % (interface, name))
    # CONSTRAINT: with no driver bound, Linux suspends the wheel after 2 s, and every resume
    # boots it into a calibration sweep that ignores force (measured 2026-09-26).
    control = os.path.join(SYSFS, devices[0], "power", "control")
    try:
        with open(control) as handle:
            autosuspend = handle.read().strip() == "auto"
    except OSError:
        autosuspend = False
    if autosuspend:
        return (False, "autosuspend on", "Linux suspends the wheel between uses",
                "Every time the bridge opens a suspended wheel, it boots and sweeps through a "
                "calibration that ignores force. Keep it awake until the next replug with:\n\n"
                "  echo on | sudo tee %s\n\n"
                "It sweeps once as it wakes. The installer's udev rule will make this "
                "permanent." % control)
    return (True, "OK, free", None, None)
