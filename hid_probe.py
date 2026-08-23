"""
hid_probe.py -- Does this device publish a USB PID force-feedback collection?

The decisive low-level check behind the other two probes.

DirectInput can only offer force feedback on a HID device if that device describes its
motors using the USB "Physical Interface Device" (PID) class -- HID usage page 0x0F --
in its report descriptor, or if a vendor driver supplies a force-feedback driver object.
GameInput's PC force-feedback path is specified against exactly the same USB PID spec.

So if usage page 0x0F is absent from every collection, then:

  * DirectInput reporting no DIDC_FORCEFEEDBACK is correct, not a bug
  * GameInput reporting forceFeedbackMotorCount = 0 is correct, not a bug
  * both APIs are behaving properly and the gap is in what the wheel advertises

This walks every HID top-level collection the device exposes (a composite device like this
wheel has several) and reports each one's usage page, report sizes, and the usage pages
actually used by its Output and Feature report items.

Usage:
    python hid_probe.py               # all HID collections for VID 0x0F0D
    python hid_probe.py --vid 0x046D  # a different vendor
    python hid_probe.py --all         # every HID device on the system
"""

import argparse
import ctypes
import sys
from ctypes import POINTER, Structure, byref, c_uint16, c_uint32, c_void_p, sizeof
from ctypes.wintypes import BOOL, DWORD, HANDLE, LPCWSTR, ULONG, USHORT

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010
INVALID_HANDLE_VALUE = c_void_p(-1).value
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

HIDP_STATUS_SUCCESS = 0x00110000

# The one we are hunting for.
HID_USAGE_PAGE_PID = 0x0F

USAGE_PAGE_NAMES = {
    0x01: "Generic Desktop",
    0x02: "Simulation Controls",
    0x06: "Generic Device Controls",
    0x07: "Keyboard/Keypad",
    0x08: "LEDs",
    0x09: "Button",
    0x0F: "PHYSICAL INTERFACE DEVICE (force feedback)",
    0x0C: "Consumer",
}


class GUID(Structure):
    _fields_ = [("Data1", c_uint32), ("Data2", c_uint16),
                ("Data3", c_uint16), ("Data4", ctypes.c_uint8 * 8)]


class SP_DEVICE_INTERFACE_DATA(Structure):
    _fields_ = [("cbSize", DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", DWORD), ("Reserved", ctypes.c_void_p)]


class HIDD_ATTRIBUTES(Structure):
    _fields_ = [("Size", ULONG), ("VendorID", USHORT),
                ("ProductID", USHORT), ("VersionNumber", USHORT)]


class HIDP_CAPS(Structure):
    _fields_ = [
        ("Usage", USHORT),
        ("UsagePage", USHORT),
        ("InputReportByteLength", USHORT),
        ("OutputReportByteLength", USHORT),
        ("FeatureReportByteLength", USHORT),
        ("Reserved", USHORT * 17),
        ("NumberLinkCollectionNodes", USHORT),
        ("NumberInputButtonCaps", USHORT),
        ("NumberInputValueCaps", USHORT),
        ("NumberInputDataIndices", USHORT),
        ("NumberOutputButtonCaps", USHORT),
        ("NumberOutputValueCaps", USHORT),
        ("NumberOutputDataIndices", USHORT),
        ("NumberFeatureButtonCaps", USHORT),
        ("NumberFeatureValueCaps", USHORT),
        ("NumberFeatureDataIndices", USHORT),
    ]


class HIDP_RANGE(Structure):
    _fields_ = [("UsageMin", USHORT), ("UsageMax", USHORT),
                ("StringMin", USHORT), ("StringMax", USHORT),
                ("DesignatorMin", USHORT), ("DesignatorMax", USHORT),
                ("DataIndexMin", USHORT), ("DataIndexMax", USHORT)]


class HIDP_NOTRANGE(Structure):
    _fields_ = [("Usage", USHORT), ("Reserved1", USHORT),
                ("StringIndex", USHORT), ("Reserved2", USHORT),
                ("DesignatorIndex", USHORT), ("Reserved3", USHORT),
                ("DataIndex", USHORT), ("Reserved4", USHORT)]


class _RangeUnion(ctypes.Union):
    _fields_ = [("Range", HIDP_RANGE), ("NotRange", HIDP_NOTRANGE)]


class HIDP_VALUE_CAPS(Structure):
    _fields_ = [
        ("UsagePage", USHORT),
        ("ReportID", ctypes.c_uint8),
        ("IsAlias", ctypes.c_uint8),
        ("BitField", USHORT),
        ("LinkCollection", USHORT),
        ("LinkUsage", USHORT),
        ("LinkUsagePage", USHORT),
        ("IsRange", ctypes.c_uint8),
        ("IsStringRange", ctypes.c_uint8),
        ("IsDesignatorRange", ctypes.c_uint8),
        ("IsAbsolute", ctypes.c_uint8),
        ("HasNull", ctypes.c_uint8),
        ("Reserved", ctypes.c_uint8),
        ("BitSize", USHORT),
        ("ReportCount", USHORT),
        ("Reserved2", USHORT * 5),
        ("UnitsExp", ULONG),
        ("Units", ULONG),
        ("LogicalMin", ctypes.c_long),
        ("LogicalMax", ctypes.c_long),
        ("PhysicalMin", ctypes.c_long),
        ("PhysicalMax", ctypes.c_long),
        ("u", _RangeUnion),
    ]


HidP_Input, HidP_Output, HidP_Feature = 0, 1, 2

hid.HidD_GetHidGuid.restype = None
hid.HidD_GetHidGuid.argtypes = [POINTER(GUID)]
hid.HidD_GetAttributes.restype = BOOL
hid.HidD_GetAttributes.argtypes = [HANDLE, POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetPreparsedData.restype = BOOL
hid.HidD_GetPreparsedData.argtypes = [HANDLE, POINTER(c_void_p)]
hid.HidD_FreePreparsedData.restype = BOOL
hid.HidD_FreePreparsedData.argtypes = [c_void_p]
hid.HidP_GetCaps.restype = ctypes.c_long
hid.HidP_GetCaps.argtypes = [c_void_p, POINTER(HIDP_CAPS)]
hid.HidP_GetValueCaps.restype = ctypes.c_long
hid.HidP_GetValueCaps.argtypes = [ctypes.c_int, POINTER(HIDP_VALUE_CAPS),
                                  POINTER(USHORT), c_void_p]
hid.HidD_GetProductString.restype = BOOL
hid.HidD_GetProductString.argtypes = [HANDLE, c_void_p, ULONG]

kernel32.CreateFileW.restype = HANDLE
kernel32.CreateFileW.argtypes = [LPCWSTR, DWORD, DWORD, c_void_p, DWORD, DWORD, HANDLE]
kernel32.CloseHandle.argtypes = [HANDLE]

setupapi.SetupDiGetClassDevsW.restype = c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [POINTER(GUID), c_void_p, c_void_p, DWORD]
setupapi.SetupDiEnumDeviceInterfaces.restype = BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    c_void_p, c_void_p, POINTER(GUID), DWORD, POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    c_void_p, POINTER(SP_DEVICE_INTERFACE_DATA), c_void_p, DWORD, POINTER(DWORD), c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [c_void_p]


def iter_hid_paths():
    """Yield the device interface path of every present HID collection."""
    guid = GUID()
    hid.HidD_GetHidGuid(byref(guid))

    info = setupapi.SetupDiGetClassDevsW(
        byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not info or info == INVALID_HANDLE_VALUE:
        return

    try:
        index = 0
        while True:
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    info, None, byref(guid), index, byref(iface)):
                break
            index += 1

            required = DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                info, byref(iface), None, 0, byref(required), None)
            if not required.value:
                continue

            buf = ctypes.create_string_buffer(required.value)
            # cbSize is the size of the fixed header only: 8 on 64-bit, 6 on 32-bit.
            ctypes.cast(buf, POINTER(DWORD))[0] = 8 if sizeof(c_void_p) == 8 else 6
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    info, byref(iface), buf, required.value, None, None):
                continue

            yield ctypes.wstring_at(ctypes.addressof(buf) + 4)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info)


def usage_page_name(page):
    return USAGE_PAGE_NAMES.get(page, "usage page 0x%02X" % page)


def value_cap_pages(preparsed, report_type, count):
    """Distinct usage pages used by the value items of one report type."""
    if not count:
        return {}
    caps = (HIDP_VALUE_CAPS * count)()
    length = USHORT(count)
    if hid.HidP_GetValueCaps(report_type, caps, byref(length), preparsed) != HIDP_STATUS_SUCCESS:
        return {}
    pages = {}
    for i in range(length.value):
        pages.setdefault(caps[i].UsagePage, 0)
        pages[caps[i].UsagePage] += 1
    return pages


def describe(path, want_vid, show_all):
    handle = kernel32.CreateFileW(
        path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        # Fall back to a metadata-only open; some collections refuse read/write.
        handle = kernel32.CreateFileW(
            path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        return None

    try:
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = sizeof(HIDD_ATTRIBUTES)
        if not hid.HidD_GetAttributes(handle, byref(attrs)):
            return None
        if not show_all and attrs.VendorID != want_vid:
            return None

        preparsed = c_void_p()
        if not hid.HidD_GetPreparsedData(handle, byref(preparsed)):
            return None

        try:
            caps = HIDP_CAPS()
            if hid.HidP_GetCaps(preparsed, byref(caps)) != HIDP_STATUS_SUCCESS:
                return None

            name_buf = ctypes.create_unicode_buffer(256)
            product = ""
            if hid.HidD_GetProductString(handle, name_buf, sizeof(name_buf)):
                product = name_buf.value

            out_pages = value_cap_pages(preparsed, HidP_Output, caps.NumberOutputValueCaps)
            feat_pages = value_cap_pages(preparsed, HidP_Feature, caps.NumberFeatureValueCaps)

            return {
                "path": path,
                "vid": attrs.VendorID,
                "pid": attrs.ProductID,
                "version": attrs.VersionNumber,
                "product": product,
                "caps": caps,
                "out_pages": out_pages,
                "feat_pages": feat_pages,
            }
        finally:
            hid.HidD_FreePreparsedData(preparsed)
    finally:
        kernel32.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description="HID PID force-feedback descriptor probe")
    parser.add_argument("--vid", type=lambda s: int(s, 0), default=0x0F0D,
                        help="vendor id to inspect (default 0x0F0D, HORI)")
    parser.add_argument("--all", action="store_true", help="inspect every HID device")
    args = parser.parse_args()

    print("=" * 78)
    print("HID force-feedback (USB PID) descriptor probe")
    print("=" * 78)
    if args.all:
        print("  Scanning every present HID collection")
    else:
        print("  Scanning HID collections for VID 0x%04X" % args.vid)

    results = []
    for path in iter_hid_paths():
        info = describe(path, args.vid, args.all)
        if info:
            results.append(info)

    if not results:
        print("\n  No matching HID collections found.")
        return 1

    pid_found_anywhere = False

    for info in results:
        caps = info["caps"]
        print()
        print("-" * 78)
        print("  VID 0x%04X  PID 0x%04X  rev 0x%04X   %s"
              % (info["vid"], info["pid"], info["version"], info["product"] or "(no product string)"))
        print("    path : %s" % info["path"])
        print("    top-level collection : usage page 0x%02X (%s), usage 0x%02X"
              % (caps.UsagePage, usage_page_name(caps.UsagePage), caps.Usage))
        print("    report byte lengths  : input=%d  output=%d  feature=%d"
              % (caps.InputReportByteLength, caps.OutputReportByteLength,
                 caps.FeatureReportByteLength))
        print("    value item counts    : input=%d  output=%d  feature=%d"
              % (caps.NumberInputValueCaps, caps.NumberOutputValueCaps,
                 caps.NumberFeatureValueCaps))

        for label, pages in (("output", info["out_pages"]), ("feature", info["feat_pages"])):
            if pages:
                rendered = ", ".join(
                    "0x%02X (%s) x%d" % (p, usage_page_name(p), n) for p, n in sorted(pages.items()))
                print("    %-7s item usage pages : %s" % (label, rendered))
                if HID_USAGE_PAGE_PID in pages:
                    pid_found_anywhere = True

        if caps.UsagePage == HID_USAGE_PAGE_PID:
            pid_found_anywhere = True

    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    if pid_found_anywhere:
        print("  Usage page 0x0F (Physical Interface Device) IS present.")
        print("  This device describes force feedback the way DirectInput and GameInput")
        print("  expect. If neither API offers FFB, the problem is elsewhere.")
    else:
        print("  Usage page 0x0F (Physical Interface Device) is ABSENT from every")
        print("  collection, and no collection declares itself as a PID device.")
        print()
        print("  This wheel does not describe its force-feedback motors in the standard")
        print("  USB PID way. That fully explains every result so far:")
        print("    - DirectInput reports no DIDC_FORCEFEEDBACK  -> correct")
        print("    - GameInput reports forceFeedbackMotorCount=0 -> correct")
        print("    - joy.cpl shows no Force Feedback tab         -> correct")
        print()
        print("  Both APIs are behaving properly. Torque on this wheel must be driven")
        print("  through the vendor's own protocol on its vendor-defined collections.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
