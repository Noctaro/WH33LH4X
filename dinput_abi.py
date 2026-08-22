"""
dinput_abi.py -- ctypes transcription of the DirectInput 8 API surface needed for a
force-feedback probe.

Transcribed from the Windows SDK header

    C:\\Program Files (x86)\\Windows Kits\\10\\Include\\10.0.26100.0\\um\\dinput.h

Same approach as gameinput_abi.py: DirectInput is COM with plain vtables, so ctypes can
call it by indexing into the vtable. Every index, struct layout, constant and GUID below
was read out of that header rather than recalled.

Unlike GameInput there is only one version of this API to worry about -- DirectInput 8
has been frozen for two decades. The catch is elsewhere: playing an effect requires
exclusive access, which requires a real window handle, and requires a data format to be
set first. Both are handled in dinput_probe.py.
"""

import ctypes
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_int32,
    c_uint8,
    c_uint16,
    c_uint32,
    c_void_p,
    c_wchar,
    c_wchar_p,
    cast,
)

SOURCE_HEADER = r"C:\Program Files (x86)\Windows Kits\10\Include\10.0.26100.0\um\dinput.h"

MAX_PATH = 260
DIRECTINPUT_VERSION = 0x0800

# --- Device enumeration ----------------------------------------------------
DI8DEVCLASS_GAMECTRL = 4
DIEDFL_ALLDEVICES = 0x00000000
DIEDFL_ATTACHEDONLY = 0x00000001
DIEDFL_FORCEFEEDBACK = 0x00000100  # enumerate ONLY force-feedback capable devices

DIENUM_STOP = 0
DIENUM_CONTINUE = 1

# --- Device capability flags (DIDEVCAPS.dwFlags) ---------------------------
DIDC_ATTACHED = 0x00000001
DIDC_POLLEDDEVICE = 0x00000002
DIDC_EMULATED = 0x00000004
DIDC_POLLEDDATAFORMAT = 0x00000008
DIDC_FORCEFEEDBACK = 0x00000100  # the flag this whole exercise is about
DIDC_FFATTACK = 0x00000200
DIDC_FFFADE = 0x00000400
DIDC_SATURATION = 0x00000800
DIDC_POSNEGCOEFFICIENTS = 0x00001000
DIDC_POSNEGSATURATION = 0x00002000
DIDC_DEADBAND = 0x00004000
DIDC_STARTDELAY = 0x00008000

DIDC_NAMES = [
    ("Attached", DIDC_ATTACHED),
    ("PolledDevice", DIDC_POLLEDDEVICE),
    ("Emulated", DIDC_EMULATED),
    ("PolledDataFormat", DIDC_POLLEDDATAFORMAT),
    ("FORCEFEEDBACK", DIDC_FORCEFEEDBACK),
    ("FFAttack", DIDC_FFATTACK),
    ("FFFade", DIDC_FFFADE),
    ("Saturation", DIDC_SATURATION),
    ("PosNegCoefficients", DIDC_POSNEGCOEFFICIENTS),
    ("PosNegSaturation", DIDC_POSNEGSATURATION),
    ("DeadBand", DIDC_DEADBAND),
    ("StartDelay", DIDC_STARTDELAY),
]

# --- Effect types (DIEFFECTINFO.dwEffType, low byte) -----------------------
DIEFT_ALL = 0x00000000
DIEFT_CONSTANTFORCE = 0x00000001
DIEFT_RAMPFORCE = 0x00000002
DIEFT_PERIODIC = 0x00000003
DIEFT_CONDITION = 0x00000004
DIEFT_CUSTOMFORCE = 0x00000005
DIEFT_HARDWARE = 0x000000FF

DIEFT_TYPE_NAMES = {
    DIEFT_CONSTANTFORCE: "ConstantForce",
    DIEFT_RAMPFORCE: "RampForce",
    DIEFT_PERIODIC: "Periodic",
    DIEFT_CONDITION: "Condition",
    DIEFT_CUSTOMFORCE: "CustomForce",
}


def dieft_gettype(n):
    """DIEFT_GETTYPE(n) is LOBYTE(n)."""
    return n & 0xFF


# --- Cooperative level -----------------------------------------------------
DISCL_EXCLUSIVE = 0x00000001
DISCL_NONEXCLUSIVE = 0x00000002
DISCL_FOREGROUND = 0x00000004
DISCL_BACKGROUND = 0x00000008

# --- Data format -----------------------------------------------------------
DIDF_ABSAXIS = 0x00000001
DIDFT_AXIS = 0x00000003
DIDFT_ANYINSTANCE = 0x00FFFF00

# --- Effect parameters -----------------------------------------------------
DIEFF_OBJECTOFFSETS = 0x00000002
DIEFF_CARTESIAN = 0x00000010
DIEFF_POLAR = 0x00000020
DIEFF_SPHERICAL = 0x00000040

DIEP_DURATION = 0x00000001
DIEP_GAIN = 0x00000004
DIEP_AXES = 0x00000020
DIEP_DIRECTION = 0x00000040
DIEP_TYPESPECIFICPARAMS = 0x00000100
DIEP_START = 0x20000000

DIEB_NOTRIGGER = 0xFFFFFFFF
DI_INFINITE = 0xFFFFFFFF

DIES_SOLO = 0x00000001

# Effect magnitudes and gains are always in the range 0..10000 (gain) or
# -10000..10000 (signed magnitude). There are no floats anywhere in DirectInput.
DI_FFNOMINALMAX = 10000


# ---------------------------------------------------------------------------
# GUIDs
# ---------------------------------------------------------------------------

class GUID(Structure):
    _fields_ = [
        ("Data1", c_uint32),
        ("Data2", c_uint16),
        ("Data3", c_uint16),
        ("Data4", c_uint8 * 8),
    ]

    def __str__(self):
        d4 = "".join("%02X" % b for b in self.Data4)
        return "{%08X-%04X-%04X-%s-%s}" % (
            self.Data1, self.Data2, self.Data3, d4[:4], d4[4:])

    def __eq__(self, other):
        return bytes(self) == bytes(other) if isinstance(other, GUID) else NotImplemented

    def __hash__(self):
        return hash(bytes(self))


def _guid(d1, d2, d3, *rest):
    return GUID(d1, d2, d3, (c_uint8 * 8)(*rest))


IID_IDirectInput8W = _guid(0xBF798031, 0x483A, 0x4DA2,
                           0xAA, 0x99, 0x5D, 0x64, 0xED, 0x36, 0x97, 0x00)

_DI_TAIL = (0x9A, 0xD0, 0x00, 0xA0, 0xC9, 0xA0, 0x6E, 0x35)
GUID_ConstantForce = _guid(0x13541C20, 0x8E33, 0x11D0, *_DI_TAIL)
GUID_RampForce = _guid(0x13541C21, 0x8E33, 0x11D0, *_DI_TAIL)
GUID_Square = _guid(0x13541C22, 0x8E33, 0x11D0, *_DI_TAIL)
GUID_Sine = _guid(0x13541C23, 0x8E33, 0x11D0, *_DI_TAIL)
GUID_Triangle = _guid(0x13541C24, 0x8E33, 0x11D0, *_DI_TAIL)
GUID_SawtoothUp = _guid(0x13541C25, 0x8E33, 0x11D0, *_DI_TAIL)
GUID_SawtoothDown = _guid(0x13541C26, 0x8E33, 0x11D0, *_DI_TAIL)
GUID_Spring = _guid(0x13541C27, 0x8E33, 0x11D0, *_DI_TAIL)
GUID_Damper = _guid(0x13541C28, 0x8E33, 0x11D0, *_DI_TAIL)
GUID_Inertia = _guid(0x13541C29, 0x8E33, 0x11D0, *_DI_TAIL)
GUID_Friction = _guid(0x13541C2A, 0x8E33, 0x11D0, *_DI_TAIL)
GUID_CustomForce = _guid(0x13541C2B, 0x8E33, 0x11D0, *_DI_TAIL)

KNOWN_EFFECT_GUIDS = {
    bytes(GUID_ConstantForce): "Constant force",
    bytes(GUID_RampForce): "Ramp force",
    bytes(GUID_Square): "Square wave",
    bytes(GUID_Sine): "Sine wave",
    bytes(GUID_Triangle): "Triangle wave",
    bytes(GUID_SawtoothUp): "Sawtooth up",
    bytes(GUID_SawtoothDown): "Sawtooth down",
    bytes(GUID_Spring): "Spring",
    bytes(GUID_Damper): "Damper",
    bytes(GUID_Inertia): "Inertia",
    bytes(GUID_Friction): "Friction",
    bytes(GUID_CustomForce): "Custom force",
}


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

class DIDEVCAPS(Structure):
    _fields_ = [
        ("dwSize", c_uint32),
        ("dwFlags", c_uint32),
        ("dwDevType", c_uint32),
        ("dwAxes", c_uint32),
        ("dwButtons", c_uint32),
        ("dwPOVs", c_uint32),
        ("dwFFSamplePeriod", c_uint32),
        ("dwFFMinTimeResolution", c_uint32),
        ("dwFirmwareRevision", c_uint32),
        ("dwHardwareRevision", c_uint32),
        ("dwFFDriverVersion", c_uint32),
    ]


class DIDEVICEINSTANCEW(Structure):
    _fields_ = [
        ("dwSize", c_uint32),
        ("guidInstance", GUID),
        ("guidProduct", GUID),
        ("dwDevType", c_uint32),
        ("tszInstanceName", c_wchar * MAX_PATH),
        ("tszProductName", c_wchar * MAX_PATH),
        ("guidFFDriver", GUID),
        ("wUsagePage", c_uint16),
        ("wUsage", c_uint16),
    ]


class DIEFFECTINFOW(Structure):
    _fields_ = [
        ("dwSize", c_uint32),
        ("guid", GUID),
        ("dwEffType", c_uint32),
        ("dwStaticParams", c_uint32),
        ("dwDynamicParams", c_uint32),
        ("tszName", c_wchar * MAX_PATH),
    ]


class DIOBJECTDATAFORMAT(Structure):
    _fields_ = [
        ("pguid", POINTER(GUID)),
        ("dwOfs", c_uint32),
        ("dwType", c_uint32),
        ("dwFlags", c_uint32),
    ]


class DIDATAFORMAT(Structure):
    _fields_ = [
        ("dwSize", c_uint32),
        ("dwObjSize", c_uint32),
        ("dwFlags", c_uint32),
        ("dwDataSize", c_uint32),
        ("dwNumObjs", c_uint32),
        ("rgodf", POINTER(DIOBJECTDATAFORMAT)),
    ]


class DIENVELOPE(Structure):
    _fields_ = [
        ("dwSize", c_uint32),
        ("dwAttackLevel", c_uint32),
        ("dwAttackTime", c_uint32),   # microseconds
        ("dwFadeLevel", c_uint32),
        ("dwFadeTime", c_uint32),     # microseconds
    ]


class DIEFFECT(Structure):
    _fields_ = [
        ("dwSize", c_uint32),
        ("dwFlags", c_uint32),
        ("dwDuration", c_uint32),           # microseconds
        ("dwSamplePeriod", c_uint32),
        ("dwGain", c_uint32),               # 0..10000 -- our torque limiter
        ("dwTriggerButton", c_uint32),
        ("dwTriggerRepeatInterval", c_uint32),
        ("cAxes", c_uint32),
        ("rgdwAxes", POINTER(c_uint32)),
        ("rglDirection", POINTER(c_int32)),
        ("lpEnvelope", POINTER(DIENVELOPE)),
        ("cbTypeSpecificParams", c_uint32),
        ("lpvTypeSpecificParams", c_void_p),
        ("dwStartDelay", c_uint32),
    ]


class DICONSTANTFORCE(Structure):
    _fields_ = [("lMagnitude", c_int32)]  # -10000..10000


class DIRAMPFORCE(Structure):
    _fields_ = [("lStart", c_int32), ("lEnd", c_int32)]


class DIPERIODIC(Structure):
    _fields_ = [
        ("dwMagnitude", c_uint32),
        ("lOffset", c_int32),
        ("dwPhase", c_uint32),
        ("dwPeriod", c_uint32),  # microseconds
    ]


class DICONDITION(Structure):
    _fields_ = [
        ("lOffset", c_int32),
        ("lPositiveCoefficient", c_int32),
        ("lNegativeCoefficient", c_int32),
        ("dwPositiveSaturation", c_uint32),
        ("dwNegativeSaturation", c_uint32),
        ("lDeadBand", c_int32),
    ]


# Callback prototypes. PASCAL is __stdcall, i.e. WINFUNCTYPE.
LPDIENUMDEVICESCALLBACKW = ctypes.WINFUNCTYPE(c_int32, POINTER(DIDEVICEINSTANCEW), c_void_p)
LPDIENUMEFFECTSCALLBACKW = ctypes.WINFUNCTYPE(c_int32, POINTER(DIEFFECTINFOW), c_void_p)


# ---------------------------------------------------------------------------
# HRESULT helpers
# ---------------------------------------------------------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# DirectInput's own error codes; FormatMessage does not know these.
_DI_ERRORS = {
    0x80070005: "E_ACCESSDENIED / DIERR_NOTEXCLUSIVEACQUIRED",
    0x80070006: "E_HANDLE / DIERR_INVALIDPARAM",
    0x80070057: "E_INVALIDARG / DIERR_INVALIDPARAM",
    0x8007000E: "E_OUTOFMEMORY / DIERR_OUTOFMEMORY",
    0x80004001: "E_NOTIMPL / DIERR_UNSUPPORTED",
    0x80040201: "DIERR_NOTACQUIRED",
    0x80040209: "DIERR_INPUTLOST",
    0x8004020A: "DIERR_ACQUIRED (cannot change while acquired)",
    0x8004020B: "DIERR_NOTINITIALIZED",
    0x80040154: "REGDB_E_CLASSNOTREG",
    0x80040205: "DIERR_EFFECTPLAYING",
    0x80040206: "DIERR_UNPLUGGED",
    0x80040207: "DIERR_REPORTFULL",
    0x80040208: "DIERR_MAPFILEFAIL",
    0x80040300: "DIERR_DEVICEFULL / device out of effect slots",
    0x80040301: "DIERR_MOREDATA",
    0x80040302: "DIERR_NOTDOWNLOADED",
    0x80040303: "DIERR_HASEFFECTS",
    0x80040304: "DIERR_NOTEXCLUSIVEACQUIRED",
    0x80040305: "DIERR_INCOMPLETEEFFECT",
    0x80040306: "DIERR_NOTBUFFERED",
    0x80040307: "DIERR_EFFECTPLAYING",
    0x80040308: "DIERR_UNPLUGGED",
}


def hresult_message(hr):
    code = hr & 0xFFFFFFFF
    if code in _DI_ERRORS:
        return "0x%08X (%s)" % (code, _DI_ERRORS[code])
    buf = ctypes.create_unicode_buffer(512)
    n = _kernel32.FormatMessageW(0x1000 | 0x200, None, c_uint32(code), 0, buf, len(buf), None)
    text = buf.value.strip() if n else ""
    return "0x%08X%s" % (code, (" (%s)" % text) if text else "")


class DirectInputError(RuntimeError):
    def __init__(self, call, hr):
        self.call = call
        self.hr = hr
        super().__init__("%s failed: HRESULT %s" % (call, hresult_message(hr)))


def check(call, hr):
    if hr < 0:
        raise DirectInputError(call, hr)
    return hr


# ---------------------------------------------------------------------------
# COM plumbing
# ---------------------------------------------------------------------------

def _call(this_ptr, index, restype, argtypes, *args):
    vtable = cast(this_ptr, POINTER(POINTER(c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return prototype(vtable[index])(this_ptr, *args)


class _Unknown:
    def __init__(self, ptr):
        self._ptr = ptr

    @property
    def ptr(self):
        return self._ptr

    def release(self):
        if self._ptr:
            _call(self._ptr, 2, c_uint32, [])
            self._ptr = None


class IDirectInput8(_Unknown):
    """Vtable: 3 CreateDevice, 4 EnumDevices, 5 GetDeviceStatus, ..."""

    def create_device(self, guid_instance):
        out = c_void_p()
        check("IDirectInput8::CreateDevice",
              _call(self._ptr, 3, c_int32, [POINTER(GUID), POINTER(c_void_p), c_void_p],
                    byref(guid_instance), byref(out), None))
        return IDirectInputDevice8(out)

    def enum_devices(self, device_class, callback, flags):
        check("IDirectInput8::EnumDevices",
              _call(self._ptr, 4, c_int32, [c_uint32, c_void_p, c_void_p, c_uint32],
                    device_class, cast(callback, c_void_p), None, flags))


class IDirectInputDevice8(_Unknown):
    """
    Vtable indices from dinput.h:
      3 GetCapabilities   7 Acquire        8 Unacquire     11 SetDataFormat
     13 SetCooperativeLevel   15 GetDeviceInfo
     18 CreateEffect      19 EnumEffects   22 SendForceFeedbackCommand
    """

    def get_capabilities(self):
        caps = DIDEVCAPS()
        caps.dwSize = ctypes.sizeof(DIDEVCAPS)
        check("IDirectInputDevice8::GetCapabilities",
              _call(self._ptr, 3, c_int32, [POINTER(DIDEVCAPS)], byref(caps)))
        return caps

    def acquire(self):
        return check("IDirectInputDevice8::Acquire", _call(self._ptr, 7, c_int32, []))

    def unacquire(self):
        return _call(self._ptr, 8, c_int32, [])

    def set_data_format(self, data_format):
        check("IDirectInputDevice8::SetDataFormat",
              _call(self._ptr, 11, c_int32, [POINTER(DIDATAFORMAT)], byref(data_format)))

    def set_cooperative_level(self, hwnd, flags):
        check("IDirectInputDevice8::SetCooperativeLevel",
              _call(self._ptr, 13, c_int32, [c_void_p, c_uint32], hwnd, flags))

    def get_device_info(self):
        inst = DIDEVICEINSTANCEW()
        inst.dwSize = ctypes.sizeof(DIDEVICEINSTANCEW)
        check("IDirectInputDevice8::GetDeviceInfo",
              _call(self._ptr, 15, c_int32, [POINTER(DIDEVICEINSTANCEW)], byref(inst)))
        return inst

    def create_effect(self, guid, effect):
        out = c_void_p()
        check("IDirectInputDevice8::CreateEffect",
              _call(self._ptr, 18, c_int32,
                    [POINTER(GUID), POINTER(DIEFFECT), POINTER(c_void_p), c_void_p],
                    byref(guid), byref(effect), byref(out), None))
        return IDirectInputEffect(out)

    def enum_effects(self, callback, effect_type=DIEFT_ALL):
        check("IDirectInputDevice8::EnumEffects",
              _call(self._ptr, 19, c_int32, [c_void_p, c_void_p, c_uint32],
                    cast(callback, c_void_p), None, effect_type))


class IDirectInputEffect(_Unknown):
    """Vtable: 4 GetEffectGuid, 6 SetParameters, 7 Start, 8 Stop, 9 GetEffectStatus."""

    def set_parameters(self, effect, flags):
        return check("IDirectInputEffect::SetParameters",
                     _call(self._ptr, 6, c_int32, [POINTER(DIEFFECT), c_uint32],
                           byref(effect), flags))

    def start(self, iterations=1, flags=0):
        return check("IDirectInputEffect::Start",
                     _call(self._ptr, 7, c_int32, [c_uint32, c_uint32], iterations, flags))

    def stop(self):
        return _call(self._ptr, 8, c_int32, [])

    def get_status(self):
        status = c_uint32(0)
        _call(self._ptr, 9, c_int32, [POINTER(c_uint32)], byref(status))
        return status.value


def create_direct_input():
    """DirectInput8Create(hinst, 0x0800, IID_IDirectInput8W, &out, NULL)."""
    dll = ctypes.WinDLL("dinput8.dll")
    fn = dll.DirectInput8Create
    fn.restype = c_int32
    fn.argtypes = [c_void_p, c_uint32, POINTER(GUID), POINTER(c_void_p), c_void_p]

    # restype MUST be set: ctypes defaults to c_int, which silently truncates a 64-bit
    # HMODULE to 32 bits and makes DirectInput8Create return E_INVALIDARG.
    _kernel32.GetModuleHandleW.restype = c_void_p
    _kernel32.GetModuleHandleW.argtypes = [c_void_p]
    hinst = _kernel32.GetModuleHandleW(None)

    out = c_void_p()
    check("DirectInput8Create",
          fn(hinst, DIRECTINPUT_VERSION, byref(IID_IDirectInput8W), byref(out), None))
    return IDirectInput8(out)


def make_axis_data_format(axis_count=6):
    """
    Build a minimal DIDATAFORMAT of N absolute axes.

    SetDataFormat must be called before Acquire, and Acquire is required before an effect
    can be downloaded to the device -- even though this probe never reads device state.
    A NULL pguid with DIDFT_AXIS|DIDFT_ANYINSTANCE matches any axis the device has, which
    avoids having to reproduce the 164-entry c_dfDIJoystick2 table (that symbol lives in
    dinput8.lib, not in the DLL, so ctypes cannot borrow it).

    N MUST NOT EXCEED THE AXES THE DEVICE ACTUALLY HAS. Every entry has to match a real
    object or SetDataFormat fails wholesale with E_INVALIDARG -- there is no partial match.
    The default is 6 because that is the classic DIJOYSTATE set (X Y Z RX RY RZ) and the
    most any plain HID joystick is guaranteed to carry; the old default of 8 could not
    succeed on vJoy, which presents exactly 6 axes to DirectInput. Prefer
    negotiate_axis_data_format() over guessing.

    The returned tuple keeps the object array alive; if it is garbage collected the
    format's rgodf pointer dangles.
    """
    objects = (DIOBJECTDATAFORMAT * axis_count)()
    for i in range(axis_count):
        objects[i].pguid = None
        objects[i].dwOfs = i * 4
        objects[i].dwType = DIDFT_AXIS | DIDFT_ANYINSTANCE
        objects[i].dwFlags = 0

    fmt = DIDATAFORMAT()
    fmt.dwSize = ctypes.sizeof(DIDATAFORMAT)
    fmt.dwObjSize = ctypes.sizeof(DIOBJECTDATAFORMAT)
    fmt.dwFlags = DIDF_ABSAXIS
    fmt.dwDataSize = axis_count * 4
    fmt.dwNumObjs = axis_count
    fmt.rgodf = objects
    return fmt, objects


def negotiate_axis_data_format(device, max_axes=6):
    """
    Set the largest all-axis data format the device accepts, and report how many that was.

    SetDataFormat is all-or-nothing: one unmatched entry fails the whole call with
    E_INVALIDARG, which says nothing about how many axes would have worked. Since force
    feedback needs a data format only so that Acquire will succeed -- the effect itself
    binds to one axis -- the sensible move is to negotiate downward rather than hard-code a
    count and be wrong on the next device.

    Returns (fmt, objects, axis_count). Keep the first two alive for as long as the device
    is acquired; rgodf is a borrowed pointer.
    """
    last = None
    for count in range(max_axes, 0, -1):
        fmt, objects = make_axis_data_format(count)
        try:
            device.set_data_format(fmt)
            return fmt, objects, count
        except DirectInputError as exc:
            last = exc
    raise DirectInputError(
        "No all-axis data format from %d down to 1 was accepted. The device may expose no "
        "absolute axes at all. Last error: %s" % (max_axes, last))


def get_console_hwnd():
    """
    Exclusive access is required to play force-feedback effects, and exclusive access
    requires a real HWND. A console app has one: its own console window.
    """
    _kernel32.GetConsoleWindow.restype = c_void_p
    return _kernel32.GetConsoleWindow()


# A hidden window created by ensure_hwnd(). Module-level because the window class, the
# window and the WNDPROC must outlive the call: if the WNDPROC is collected, the next
# message dispatched to the window calls a freed pointer.
_hidden_window = None
_hidden_wndproc = None

_user32 = ctypes.WinDLL("user32", use_last_error=True)
WS_OVERLAPPED = 0x00000000
CW_USEDEFAULT = -0x80000000


class _WNDCLASSW(Structure):
    _fields_ = [
        ("style", c_uint32),
        ("lpfnWndProc", c_void_p),
        ("cbClsExtra", c_int32),
        ("cbWndExtra", c_int32),
        ("hInstance", c_void_p),
        ("hIcon", c_void_p),
        ("hCursor", c_void_p),
        ("hbrBackground", c_void_p),
        ("lpszMenuName", c_wchar_p),
        ("lpszClassName", c_wchar_p),
    ]


def ensure_hwnd():
    """
    Return an HWND usable for SetCooperativeLevel, creating a hidden one if needed.

    DirectInput refuses exclusive access -- and therefore all force feedback -- without a
    real top-level window. A console window normally supplies it, but there is not always
    one: launched from a service, from a GUI host, or from any tool that captures output
    rather than allocating a console, GetConsoleWindow() returns NULL and force feedback
    becomes unavailable for a reason that has nothing to do with the hardware.

    The window is never shown. It only has to exist and be top-level; combined with
    DISCL_BACKGROUND, DirectInput is satisfied and effects play regardless of focus.
    """
    global _hidden_window, _hidden_wndproc

    hwnd = get_console_hwnd()
    if hwnd:
        return hwnd
    if _hidden_window:
        return _hidden_window

    _user32.DefWindowProcW.restype = c_void_p
    _user32.DefWindowProcW.argtypes = [c_void_p, c_uint32, c_void_p, c_void_p]
    _user32.CreateWindowExW.restype = c_void_p
    _user32.CreateWindowExW.argtypes = [c_uint32, c_wchar_p, c_wchar_p, c_uint32,
                                        c_int32, c_int32, c_int32, c_int32,
                                        c_void_p, c_void_p, c_void_p, c_void_p]
    _kernel32.GetModuleHandleW.restype = c_void_p
    _kernel32.GetModuleHandleW.argtypes = [c_void_p]

    wndproc_type = ctypes.WINFUNCTYPE(c_void_p, c_void_p, c_uint32, c_void_p, c_void_p)
    _hidden_wndproc = wndproc_type(
        lambda h, msg, wp, lp: _user32.DefWindowProcW(h, msg, wp, lp))

    hinst = _kernel32.GetModuleHandleW(None)
    cls = _WNDCLASSW()
    cls.lpfnWndProc = cast(_hidden_wndproc, c_void_p)
    cls.hInstance = hinst
    cls.lpszClassName = "WH33LH4X_DInputHost"
    # A non-zero atom means registered; failure is usually "already registered" from a
    # previous call in the same process, which is fine -- CreateWindowExW will still find
    # the class. Any other failure surfaces as a NULL window below.
    _user32.RegisterClassW(byref(cls))
    cls._keepalive = _hidden_wndproc

    _hidden_window = _user32.CreateWindowExW(
        0, "WH33LH4X_DInputHost", "WH33LH4X", WS_OVERLAPPED,
        CW_USEDEFAULT, CW_USEDEFAULT, 1, 1, None, None, hinst, None)

    if not _hidden_window:
        raise DirectInputError(
            "No console window, and creating a hidden one failed (error %d). DirectInput "
            "cannot take exclusive access without a window, so force feedback is "
            "unavailable." % ctypes.get_last_error())
    return _hidden_window
