"""Windows setup checks: vJoy device 1, and whether the wheel is bound to WinUSB."""

import ctypes
import re

# The device the bridge feeds, and the axes docs/vjoy.md says to enable. HID usage codes.
VJOY_DEVICE = 1
VJOY_AXES = (("X", 0x30), ("Y", 0x31), ("Z", 0x32), ("Rx", 0x33), ("Ry", 0x34))

# CONSTRAINT: 2.2.0 is the floor. Below it concurrent effects share block index 1 and collapse
# into one, silently. See docs/vjoy.md.
VJOY_MIN_VERSION = 0x0220
VJOY_DOWNLOAD = "https://github.com/BrunnerInnovation/vJoy"
ZADIG_DOWNLOAD = "https://zadig.akeo.ie"

HORI_VID = 0x0F0D
XBOX_PID = 0x015C
PC_PID = 0x015D

ZADIG_STEPS = (
    "The bridge talks to the wheel over raw USB, which needs the WinUSB driver instead of "
    "Microsoft's Xbox driver.\n\n"
    "1. Put the wheel in Xbox mode (long-press PROFILE) and plug it in.\n"
    "2. Download Zadig from %s and run it.\n"
    "3. Options > List All Devices.\n"
    "4. Pick \"HORI FFB Racing Wheel Series X\" (USB ID 0F0D 015C).\n"
    "5. Choose WinUSB as the target driver and press Replace Driver.\n\n"
    "While WinUSB is bound, Xbox games and the HORI app no longer see the wheel. "
    "\"Restore Microsoft driver\" here undoes it." % ZADIG_DOWNLOAD)


def _vjoy_dll():
    """The raw SDK handle, with the return types needed declared. None if vJoy is absent."""
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

    CONSTRAINT: query only; acquiring would fight the bridge for the device. Checks run worst
    first and stop at the first problem.
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
        return (False, "unknown status %s" % st, "vJoy returned an unknown status",
                "GetVJDStatus(%d) returned %r. Treat the device as unusable until it reads "
                "free or owned." % (VJOY_DEVICE, st))

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


def _present(pid):
    """True or False for a wheel on the bus with this PID; None when pyusb is missing."""
    try:
        import usb.core

        from gip.host import _backend
    except ImportError:
        return None
    try:
        return usb.core.find(idVendor=HORI_VID, idProduct=pid, backend=_backend()) is not None
    except Exception:
        return None


def binding():
    """(service, inf, provider) the registry records for the Xbox-mode wheel, or Nones."""
    import winreg
    base = r"SYSTEM\CurrentControlSet\Enum\USB\VID_%04X&PID_%04X" % (HORI_VID, XBOX_PID)
    found = (None, None, None)
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return found
    index = 0
    while True:
        try:
            instance = winreg.EnumKey(key, index)
        except OSError:
            return found
        index += 1
        try:
            with winreg.OpenKey(key, instance) as inst:
                service = winreg.QueryValueEx(inst, "Service")[0]
                driver = winreg.QueryValueEx(inst, "Driver")[0]
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                "SYSTEM\\CurrentControlSet\\Control\\Class\\" + driver) as cls:
                inf = winreg.QueryValueEx(cls, "InfPath")[0]
                provider = winreg.QueryValueEx(cls, "ProviderName")[0]
        except OSError:
            continue
        # One wheel is the normal case; a WinUSB entry wins over a stale Microsoft one.
        found = (service, inf, provider)
        if service.lower() == "winusb":
            return found


def wheel_check():
    """(ok, badge, title, guidance) for the wheel: connected, in Xbox mode, on WinUSB."""
    xbox = _present(XBOX_PID)
    if xbox is None:
        return (False, "cannot check", "pyusb is missing",
                "The raw USB bridge needs pyusb and libusb-package:\n"
                "  pip install pyusb libusb-package")
    if not xbox:
        if _present(PC_PID):
            return (False, "in PC mode", "The wheel is in PC mode",
                    "Long-press PROFILE on the wheel to switch it to Xbox mode, and let it "
                    "finish its calibration sweep.")
        return (False, "not connected", "The wheel is not connected",
                "Plug the wheel in, in Xbox mode (long-press PROFILE).")
    service = binding()[0]
    if service and service.lower() == "winusb":
        return (True, "OK, WinUSB", None, None)
    return (False, "Microsoft driver", "The wheel needs the WinUSB driver", ZADIG_STEPS)


def restorable_inf():
    """The libwdi driver package bound to the wheel, e.g. oem83.inf, or None."""
    service, inf, provider = binding()
    if provider == "libwdi" and inf and re.match(r"^oem\d+\.inf$", inf, re.IGNORECASE):
        return inf
    return None


def restore_microsoft_driver():
    """
    Remove the WinUSB package so Windows rebinds its own driver. Returns (ok, message).

    CONSTRAINT: only a package whose provider is libwdi (Zadig) is ever deleted.
    """
    inf = restorable_inf()
    if inf is None:
        return False, "No Zadig driver package is bound to the wheel; nothing to undo."
    args = "/c pnputil /delete-driver %s /uninstall /force & pnputil /scan-devices" % inf
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", args, None, 0)
    if result <= 32:
        return False, "Windows did not run the restore (cancelled, or error %d)." % result
    return True, ("Removing %s and rescanning. Replug the wheel if it does not come back "
                  "as an Xbox controller within a few seconds." % inf)
