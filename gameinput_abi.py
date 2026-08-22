"""
gameinput_abi.py -- ctypes transcription of Microsoft's GameInput API (v0 ABI).

This file is deliberately boring. It is a near-mechanical translation of the C header

    C:\\Program Files (x86)\\Windows Kits\\10\\Include\\10.0.26100.0\\um\\GameInput.h

into Python ctypes declarations, so that probe.py can stay readable. Nothing here makes
decisions; it only describes what the API looks like in memory.

WHY THE v0 ABI, AND WHY THAT MATTERS
------------------------------------
GameInput ships in two incompatible flavours, and mixing them silently corrupts calls:

  * v0  -- the header above, shipped in the Windows SDK. It pairs with the inbox
           C:\\Windows\\System32\\GameInput.dll, which exports GameInputCreate directly.
  * v3  -- the header in the Microsoft.GameInput NuGet package (GAMEINPUT_API_VERSION 3,
           namespace GameInput::v3). It pairs with GameInputRedist.dll.

They differ in ways that would not crash loudly, they would just return garbage:

    GetDeviceInfo   v0: returns GameInputDeviceInfo const*  |  v3: HRESULT + out-param
    displayName     v0: GameInputString const*              |  v3: const char*
    GameInputCreate v0: a real DLL export                   |  v3: inline wrapper over
                                                                  GameInputInitialize()

We target v0 because the header and the DLL are from the same Windows build, so the
vtable layout and struct offsets are guaranteed to agree. Every offset and vtable index
below was read out of that header, not recalled from memory.

GameInput is "Nano-COM": plain C++ vtables plus a plain exported factory function. There
is no COM registration, no typelib, no CoInitialize. That is what makes it reachable from
ctypes at all -- we just index into the vtable ourselves.

On x64 Windows there is exactly one calling convention, so the C++ `this` pointer is
simply the first argument and WINFUNCTYPE/CFUNCTYPE are equivalent.
"""

import ctypes
from ctypes import (
    POINTER,
    Structure,
    Union,
    byref,
    c_bool,
    c_char_p,
    c_float,
    c_int32,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
    cast,
)
from ctypes.wintypes import DWORD, LPWSTR

# The header this file was transcribed from. probe.py prints it so there is never any
# doubt about which ABI is in play.
SOURCE_HEADER = (
    r"C:\Program Files (x86)\Windows Kits\10\Include\10.0.26100.0\um\GameInput.h"
)


# ---------------------------------------------------------------------------
# Enums (values copied verbatim from the header)
# ---------------------------------------------------------------------------

# GameInputKind. NOTE: the v0 header has no "GameInputKindAny" constant -- the docs
# mention one, but it does not exist here. This is every defined bit OR'd together, which
# is what we want for "enumerate everything". It deliberately stays inside int32 range so
# it can be passed as a C enum without sign games.
class GameInputKind:
    Unknown = 0x00000000
    RawDeviceReport = 0x00000001
    ControllerAxis = 0x00000002
    ControllerButton = 0x00000004
    ControllerSwitch = 0x00000008
    Controller = 0x0000000E
    Keyboard = 0x00000010
    Mouse = 0x00000020
    Touch = 0x00000100
    Motion = 0x00001000
    ArcadeStick = 0x00010000
    FlightStick = 0x00020000
    Gamepad = 0x00040000
    RacingWheel = 0x00080000
    UiNavigation = 0x01000000

    # Every defined bit above, OR'd. Used as "any kind" for enumeration.
    ANY = 0x010F113F

    _NAMES = [
        ("RawDeviceReport", 0x00000001),
        ("ControllerAxis", 0x00000002),
        ("ControllerButton", 0x00000004),
        ("ControllerSwitch", 0x00000008),
        ("Keyboard", 0x00000010),
        ("Mouse", 0x00000020),
        ("Touch", 0x00000100),
        ("Motion", 0x00001000),
        ("ArcadeStick", 0x00010000),
        ("FlightStick", 0x00020000),
        ("Gamepad", 0x00040000),
        ("RacingWheel", 0x00080000),
        ("UiNavigation", 0x01000000),
    ]


class GameInputEnumerationKind:
    NoEnumeration = 0
    AsyncEnumeration = 1
    BlockingEnumeration = 2  # calls back for every connected device before returning


# The v0 focus policy is "background input is allowed by default, and you opt OUT".
# (v1+ flipped this to an opt-IN flag named GameInputEnableBackgroundInput = 0x40, which
# does NOT exist in this header. Passing 0x40 here would set
# GameInputDisableBackgroundShareButton|... nonsense instead.)
class GameInputFocusPolicy:
    Default = 0x00000000
    DisableBackgroundInput = 0x00000001
    ExclusiveForegroundInput = 0x00000002
    DisableBackgroundGuideButton = 0x00000004
    ExclusiveForegroundGuideButton = 0x00000008
    DisableBackgroundShareButton = 0x00000010
    ExclusiveForegroundShareButton = 0x00000020


class GameInputDeviceStatus:
    NoStatus = 0x00000000
    Connected = 0x00000001
    InputEnabled = 0x00000002
    OutputEnabled = 0x00000004
    RawIoEnabled = 0x00000008
    AudioCapture = 0x00000010
    AudioRender = 0x00000020
    Synchronized = 0x00000040
    Wireless = 0x00000080
    UserIdle = 0x00100000
    AnyStatus = 0x00FFFFFF

    _NAMES = [
        ("Connected", 0x00000001),
        ("InputEnabled", 0x00000002),
        ("OutputEnabled", 0x00000004),
        ("RawIoEnabled", 0x00000008),
        ("AudioCapture", 0x00000010),
        ("AudioRender", 0x00000020),
        ("Synchronized", 0x00000040),
        ("Wireless", 0x00000080),
        ("UserIdle", 0x00100000),
    ]


class GameInputDeviceFamily:
    Virtual = -1
    Aggregate = 0
    XboxOne = 1
    Xbox360 = 2
    Hid = 3
    I8042 = 4

    _NAMES = {
        -1: "Virtual",
        0: "Aggregate",
        1: "XboxOne (GIP)",
        2: "Xbox360",
        3: "HID",
        4: "I8042",
    }


class GameInputRumbleMotors:
    NoneSet = 0x00000000
    LowFrequency = 0x00000001
    HighFrequency = 0x00000002
    LeftTrigger = 0x00000004
    RightTrigger = 0x00000008

    _NAMES = [
        ("LowFrequency", 0x00000001),
        ("HighFrequency", 0x00000002),
        ("LeftTrigger", 0x00000004),
        ("RightTrigger", 0x00000008),
    ]


class GameInputFeedbackAxes:
    NoneSet = 0x00000000
    LinearX = 0x00000001
    LinearY = 0x00000002
    LinearZ = 0x00000004
    AngularX = 0x00000008  # the steering axis -- the only one HID wheels honour on PC
    AngularY = 0x00000010
    AngularZ = 0x00000020
    Normal = 0x00000040

    _NAMES = [
        ("LinearX", 0x00000001),
        ("LinearY", 0x00000002),
        ("LinearZ", 0x00000004),
        ("AngularX", 0x00000008),
        ("AngularY", 0x00000010),
        ("AngularZ", 0x00000020),
        ("Normal", 0x00000040),
    ]


class GameInputFeedbackEffectState:
    Stopped = 0
    Running = 1
    Paused = 2

    _NAMES = {0: "Stopped", 1: "Running", 2: "Paused"}


class GameInputForceFeedbackEffectKind:
    Constant = 0
    Ramp = 1
    SineWave = 2
    SquareWave = 3
    TriangleWave = 4
    SawtoothUpWave = 5
    SawtoothDownWave = 6
    Spring = 7
    Friction = 8
    Damper = 9
    Inertia = 10


class GameInputLocation:
    _NAMES = {
        -1: "Unknown",
        0: "Chassis",
        1: "Display",
        2: "Axis",
        3: "Button",
        4: "Switch",
        5: "Key",
        6: "TouchPad",
    }


def decode_flags(value, names, none_text="none"):
    """Turn a bitmask into 'FlagA|FlagB', noting any bits the header does not name."""
    hits = [name for name, bit in names if value & bit]
    known = 0
    for _, bit in names:
        known |= bit
    leftover = value & ~known
    if leftover:
        hits.append("unknown:0x%08X" % leftover)
    return "|".join(hits) if hits else none_text


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

APP_LOCAL_DEVICE_ID_SIZE = 32  # from shared/windef.h


class APP_LOCAL_DEVICE_ID(Structure):
    _fields_ = [("value", c_uint8 * APP_LOCAL_DEVICE_ID_SIZE)]


class GameInputUsage(Structure):
    _fields_ = [("page", c_uint16), ("id", c_uint16)]


class GameInputVersion(Structure):
    _fields_ = [
        ("major", c_uint16),
        ("minor", c_uint16),
        ("build", c_uint16),
        ("revision", c_uint16),
    ]

    def __str__(self):
        return "%d.%d.%d.%d" % (self.major, self.minor, self.build, self.revision)


class GameInputString(Structure):
    """v0 represents device strings as this struct, not as a bare char*."""

    _fields_ = [
        ("sizeInBytes", c_uint32),
        ("codePointCount", c_uint32),
        ("data", c_char_p),
    ]

    def text(self):
        if not self.data:
            return ""
        return self.data.decode("utf-8", errors="replace")


class GameInputRacingWheelInfo(Structure):
    """
    Populated only when GameInput actually maps the device as a racing wheel (i.e. when
    supportedInput includes GameInputKindRacingWheel). Useful evidence: if this is filled
    in, GameInput fully understands the hardware, so a zero motor count is a statement
    about force feedback specifically rather than a failure to recognise the device.
    """

    _fields_ = [
        ("menuButtonLabel", c_int32),
        ("viewButtonLabel", c_int32),
        ("previousGearButtonLabel", c_int32),
        ("nextGearButtonLabel", c_int32),
        ("dpadUpLabel", c_int32),
        ("dpadDownLabel", c_int32),
        ("dpadLeftLabel", c_int32),
        ("dpadRightLabel", c_int32),
        ("hasClutch", c_bool),
        ("hasHandbrake", c_bool),
        ("hasPatternShifter", c_bool),
        ("minPatternShifterGear", c_int32),
        ("maxPatternShifterGear", c_int32),
        ("maxWheelAngle", c_float),
    ]


class GameInputRawDeviceReportInfo(Structure):
    _fields_ = [
        ("kind", c_int32),
        ("id", c_uint32),
        ("size", c_uint32),
        ("itemCount", c_uint32),
        ("items", c_void_p),
    ]


class GameInputForceFeedbackMotorInfo(Structure):
    """
    NOTE: the three fields between supportedAxes and the effect flags -- location,
    locationId, maxSimultaneousEffects -- are present in this header but omitted from the
    published documentation. Transcribing from the docs alone yields a wrong layout and
    therefore wrong effect-support flags.
    """

    _fields_ = [
        ("supportedAxes", c_int32),
        ("location", c_int32),
        ("locationId", c_uint32),
        ("maxSimultaneousEffects", c_uint32),
        ("isConstantEffectSupported", c_bool),
        ("isRampEffectSupported", c_bool),
        ("isSineWaveEffectSupported", c_bool),
        ("isSquareWaveEffectSupported", c_bool),
        ("isTriangleWaveEffectSupported", c_bool),
        ("isSawtoothUpWaveEffectSupported", c_bool),
        ("isSawtoothDownWaveEffectSupported", c_bool),
        ("isSpringEffectSupported", c_bool),
        ("isFrictionEffectSupported", c_bool),
        ("isDamperEffectSupported", c_bool),
        ("isInertiaEffectSupported", c_bool),
    ]


# (kind constant, human label, attribute name on the motor info struct, union member)
EFFECT_CATALOG = [
    (GameInputForceFeedbackEffectKind.Constant, "Constant", "isConstantEffectSupported", "constant"),
    (GameInputForceFeedbackEffectKind.Ramp, "Ramp", "isRampEffectSupported", "ramp"),
    (GameInputForceFeedbackEffectKind.SineWave, "Sine wave", "isSineWaveEffectSupported", "sineWave"),
    (GameInputForceFeedbackEffectKind.SquareWave, "Square wave", "isSquareWaveEffectSupported", "squareWave"),
    (GameInputForceFeedbackEffectKind.TriangleWave, "Triangle wave", "isTriangleWaveEffectSupported", "triangleWave"),
    (GameInputForceFeedbackEffectKind.SawtoothUpWave, "Sawtooth up", "isSawtoothUpWaveEffectSupported", "sawtoothUpWave"),
    (GameInputForceFeedbackEffectKind.SawtoothDownWave, "Sawtooth down", "isSawtoothDownWaveEffectSupported", "sawtoothDownWave"),
    (GameInputForceFeedbackEffectKind.Spring, "Spring", "isSpringEffectSupported", "spring"),
    (GameInputForceFeedbackEffectKind.Friction, "Friction", "isFrictionEffectSupported", "friction"),
    (GameInputForceFeedbackEffectKind.Damper, "Damper", "isDamperEffectSupported", "damper"),
    (GameInputForceFeedbackEffectKind.Inertia, "Inertia", "isInertiaEffectSupported", "inertia"),
]


class GameInputForceFeedbackEnvelope(Structure):
    _fields_ = [
        ("attackDuration", c_uint64),  # microseconds
        ("sustainDuration", c_uint64),  # microseconds; UINT64_MAX == run forever
        ("releaseDuration", c_uint64),  # microseconds
        ("attackGain", c_float),
        ("sustainGain", c_float),
        ("releaseGain", c_float),
        ("playCount", c_uint32),
        ("repeatDelay", c_uint64),  # microseconds between repeats
    ]


class GameInputForceFeedbackMagnitude(Structure):
    _fields_ = [
        ("linearX", c_float),
        ("linearY", c_float),
        ("linearZ", c_float),
        ("angularX", c_float),
        ("angularY", c_float),
        ("angularZ", c_float),
        ("normal", c_float),
    ]


class GameInputForceFeedbackConditionParams(Structure):
    _fields_ = [
        ("magnitude", GameInputForceFeedbackMagnitude),
        ("positiveCoefficient", c_float),
        ("negativeCoefficient", c_float),
        ("maxPositiveMagnitude", c_float),
        ("maxNegativeMagnitude", c_float),
        ("deadZone", c_float),
        ("bias", c_float),
    ]


class GameInputForceFeedbackConstantParams(Structure):
    _fields_ = [
        ("envelope", GameInputForceFeedbackEnvelope),
        ("magnitude", GameInputForceFeedbackMagnitude),
    ]


class GameInputForceFeedbackPeriodicParams(Structure):
    _fields_ = [
        ("envelope", GameInputForceFeedbackEnvelope),
        ("magnitude", GameInputForceFeedbackMagnitude),
        ("frequency", c_float),
        ("phase", c_float),
        ("bias", c_float),
    ]


class GameInputForceFeedbackRampParams(Structure):
    _fields_ = [
        ("envelope", GameInputForceFeedbackEnvelope),
        ("startMagnitude", GameInputForceFeedbackMagnitude),
        ("endMagnitude", GameInputForceFeedbackMagnitude),
    ]


class _ForceFeedbackParamsUnion(Union):
    _fields_ = [
        ("constant", GameInputForceFeedbackConstantParams),
        ("ramp", GameInputForceFeedbackRampParams),
        ("sineWave", GameInputForceFeedbackPeriodicParams),
        ("squareWave", GameInputForceFeedbackPeriodicParams),
        ("triangleWave", GameInputForceFeedbackPeriodicParams),
        ("sawtoothUpWave", GameInputForceFeedbackPeriodicParams),
        ("sawtoothDownWave", GameInputForceFeedbackPeriodicParams),
        ("spring", GameInputForceFeedbackConditionParams),
        ("friction", GameInputForceFeedbackConditionParams),
        ("damper", GameInputForceFeedbackConditionParams),
        ("inertia", GameInputForceFeedbackConditionParams),
    ]


class GameInputForceFeedbackParams(Structure):
    _fields_ = [("kind", c_int32), ("data", _ForceFeedbackParamsUnion)]


class GameInputRumbleParams(Structure):
    _fields_ = [
        ("lowFrequency", c_float),
        ("highFrequency", c_float),
        ("leftTrigger", c_float),
        ("rightTrigger", c_float),
    ]


class GameInputDeviceInfo(Structure):
    """
    The full 40-field device descriptor. Field order is load-bearing -- a single wrong or
    missing field shifts everything after it and silently produces nonsense.

    The first field, infoSize, is our safety net: the runtime fills it with its own
    sizeof(). If it does not equal ctypes.sizeof(GameInputDeviceInfo), the transcription
    is wrong and nothing below it can be trusted. probe.py checks this before reading
    anything else.
    """

    _fields_ = [
        ("infoSize", c_uint32),
        ("vendorId", c_uint16),
        ("productId", c_uint16),
        ("revisionNumber", c_uint16),
        ("interfaceNumber", c_uint8),
        ("collectionNumber", c_uint8),
        ("usage", GameInputUsage),
        ("hardwareVersion", GameInputVersion),
        ("firmwareVersion", GameInputVersion),
        ("deviceId", APP_LOCAL_DEVICE_ID),
        ("deviceRootId", APP_LOCAL_DEVICE_ID),
        ("deviceFamily", c_int32),
        ("capabilities", c_int32),
        ("supportedInput", c_int32),
        ("supportedRumbleMotors", c_int32),
        ("inputReportCount", c_uint32),
        ("outputReportCount", c_uint32),
        ("featureReportCount", c_uint32),
        ("controllerAxisCount", c_uint32),
        ("controllerButtonCount", c_uint32),
        ("controllerSwitchCount", c_uint32),
        ("touchPointCount", c_uint32),
        ("touchSensorCount", c_uint32),
        ("forceFeedbackMotorCount", c_uint32),
        ("hapticFeedbackMotorCount", c_uint32),
        ("deviceStringCount", c_uint32),
        ("deviceDescriptorSize", c_uint32),
        ("inputReportInfo", POINTER(GameInputRawDeviceReportInfo)),
        ("outputReportInfo", POINTER(GameInputRawDeviceReportInfo)),
        ("featureReportInfo", POINTER(GameInputRawDeviceReportInfo)),
        ("controllerAxisInfo", c_void_p),
        ("controllerButtonInfo", c_void_p),
        ("controllerSwitchInfo", c_void_p),
        ("keyboardInfo", c_void_p),
        ("mouseInfo", c_void_p),
        ("touchSensorInfo", c_void_p),
        ("motionInfo", c_void_p),
        ("arcadeStickInfo", c_void_p),
        ("flightStickInfo", c_void_p),
        ("gamepadInfo", c_void_p),
        ("racingWheelInfo", POINTER(GameInputRacingWheelInfo)),
        ("uiNavigationInfo", c_void_p),
        ("forceFeedbackMotorInfo", POINTER(GameInputForceFeedbackMotorInfo)),
        ("hapticFeedbackMotorInfo", c_void_p),
        ("displayName", POINTER(GameInputString)),
        ("deviceStrings", POINTER(GameInputString)),
        ("deviceDescriptorData", c_void_p),
        ("supportedSystemButtons", c_int32),
    ]


# Callback signature: void CALLBACK (token, context, device, timestamp, current, previous)
GameInputDeviceCallback = ctypes.WINFUNCTYPE(
    None, c_uint64, c_void_p, c_void_p, c_uint64, c_int32, c_int32
)


# ---------------------------------------------------------------------------
# HRESULT helpers
# ---------------------------------------------------------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_FORMAT_MESSAGE_FROM_SYSTEM = 0x00001000
_FORMAT_MESSAGE_IGNORE_INSERTS = 0x00000200


def hresult_message(hr):
    """Render an HRESULT as '0x80070005 (Access is denied.)' for readable errors."""
    code = hr & 0xFFFFFFFF
    buf = ctypes.create_unicode_buffer(1024)
    length = _kernel32.FormatMessageW(
        DWORD(_FORMAT_MESSAGE_FROM_SYSTEM | _FORMAT_MESSAGE_IGNORE_INSERTS),
        None,
        DWORD(code),
        DWORD(0),
        cast(buf, LPWSTR),
        DWORD(len(buf)),
        None,
    )
    text = buf.value.strip() if length else ""
    known = {
        0x80070490: "Element not found -- GameInput does not expose this device/feature.",
        0x80070032: "Not supported -- the device or driver refuses this operation.",
        0x8007000E: "Out of memory -- the motor may be out of effect slots.",
        0x80004001: "Not implemented.",
        0x80070005: "Access denied.",
        0x8007001F: "A device attached to the system is not functioning.",
    }
    if not text and code in known:
        text = known[code]
    return "0x%08X%s" % (code, (" (%s)" % text) if text else "")


class GameInputError(RuntimeError):
    """Raised when a GameInput call fails, naming the call and the HRESULT."""

    def __init__(self, call, hr):
        self.call = call
        self.hr = hr
        super().__init__("%s failed: HRESULT %s" % (call, hresult_message(hr)))


def check(call, hr):
    """Raise if an HRESULT indicates failure. `call` names the exact call site."""
    if hr < 0:
        raise GameInputError(call, hr)
    return hr


# ---------------------------------------------------------------------------
# Nano-COM plumbing: call a method by vtable index
# ---------------------------------------------------------------------------

def _vtable_call(this_ptr, index, restype, argtypes, *args):
    """
    Invoke the method at `index` in the object's vtable.

    A COM object pointer points at a pointer to its vtable, which is an array of function
    pointers. So: deref once to get the vtable, index into it, and call the result with
    `this` as the leading argument.
    """
    vtable = cast(this_ptr, POINTER(POINTER(c_void_p)))[0]
    fn_address = vtable[index]
    prototype = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return prototype(fn_address)(this_ptr, *args)


class _Unknown:
    """Shared IUnknown behaviour. AddRef is index 1, Release is index 2."""

    def __init__(self, ptr):
        self._ptr = ptr

    @property
    def ptr(self):
        return self._ptr

    def add_ref(self):
        if self._ptr:
            _vtable_call(self._ptr, 1, c_uint32, [])

    def release(self):
        if self._ptr:
            _vtable_call(self._ptr, 2, c_uint32, [])
            self._ptr = None


# ---------------------------------------------------------------------------
# IGameInput  (IID 11BE2A7E-4254-445A-9C09-FFC40F006918)
# ---------------------------------------------------------------------------

class IGameInput(_Unknown):
    # vtable indices, read straight from the header
    _GET_CURRENT_TIMESTAMP = 3
    _REGISTER_DEVICE_CALLBACK = 9
    _STOP_CALLBACK = 12
    _UNREGISTER_CALLBACK = 13
    _ENABLE_OEM_DEVICE_SUPPORT = 20
    _SET_FOCUS_POLICY = 21

    def get_current_timestamp(self):
        return _vtable_call(self._ptr, self._GET_CURRENT_TIMESTAMP, c_uint64, [])

    def register_device_callback(
        self, input_kind, status_filter, enumeration_kind, callback, device=None, context=None
    ):
        """Returns the callback token. `callback` must be a GameInputDeviceCallback."""
        token = c_uint64(0)
        hr = _vtable_call(
            self._ptr,
            self._REGISTER_DEVICE_CALLBACK,
            c_int32,
            [c_void_p, c_int32, c_int32, c_int32, c_void_p, c_void_p, POINTER(c_uint64)],
            device,
            input_kind,
            status_filter,
            enumeration_kind,
            context,
            cast(callback, c_void_p),
            byref(token),
        )
        check("IGameInput::RegisterDeviceCallback", hr)
        return token.value

    def stop_callback(self, token):
        _vtable_call(self._ptr, self._STOP_CALLBACK, None, [c_uint64], token)

    def unregister_callback(self, token, timeout_microseconds=5_000_000):
        return bool(
            _vtable_call(
                self._ptr,
                self._UNREGISTER_CALLBACK,
                c_bool,
                [c_uint64, c_uint64],
                token,
                timeout_microseconds,
            )
        )

    def enable_oem_device_support(self, vendor_id, product_id, interface_number, collection_number):
        """
        Opt into a vendor-specific device that GameInput would not otherwise surface.
        Relevant for HID devices whose feedback lives in vendor-defined collections.
        """
        hr = _vtable_call(
            self._ptr,
            self._ENABLE_OEM_DEVICE_SUPPORT,
            c_int32,
            [c_uint16, c_uint16, c_uint8, c_uint8],
            vendor_id,
            product_id,
            interface_number,
            collection_number,
        )
        check("IGameInput::EnableOemDeviceSupport", hr)

    def set_focus_policy(self, policy):
        _vtable_call(self._ptr, self._SET_FOCUS_POLICY, None, [c_int32], policy)


# ---------------------------------------------------------------------------
# IGameInputDevice  (IID 31DD86FB-4C1B-408A-868F-439B3CD47125)
# ---------------------------------------------------------------------------

class IGameInputDevice(_Unknown):
    _GET_DEVICE_INFO = 3
    _GET_DEVICE_STATUS = 4
    _CREATE_FORCE_FEEDBACK_EFFECT = 6
    _IS_FF_MOTOR_POWERED_ON = 7
    _SET_FF_MOTOR_GAIN = 8
    _SET_RUMBLE_STATE = 10

    def get_device_info(self):
        """v0 returns the struct pointer directly (v3 would use an HRESULT out-param)."""
        ptr = _vtable_call(
            self._ptr, self._GET_DEVICE_INFO, POINTER(GameInputDeviceInfo), []
        )
        if not ptr:
            raise GameInputError("IGameInputDevice::GetDeviceInfo", -1)
        return ptr.contents

    def get_device_status(self):
        return _vtable_call(self._ptr, self._GET_DEVICE_STATUS, c_int32, [])

    def create_force_feedback_effect(self, motor_index, params):
        effect_ptr = c_void_p()
        hr = _vtable_call(
            self._ptr,
            self._CREATE_FORCE_FEEDBACK_EFFECT,
            c_int32,
            [c_uint32, POINTER(GameInputForceFeedbackParams), POINTER(c_void_p)],
            motor_index,
            byref(params),
            byref(effect_ptr),
        )
        check("IGameInputDevice::CreateForceFeedbackEffect", hr)
        return IGameInputForceFeedbackEffect(effect_ptr)

    def is_force_feedback_motor_powered_on(self, motor_index):
        return bool(
            _vtable_call(
                self._ptr, self._IS_FF_MOTOR_POWERED_ON, c_bool, [c_uint32], motor_index
            )
        )

    def set_force_feedback_motor_gain(self, motor_index, master_gain):
        """Master gain (0.0-1.0) for the whole motor -- our primary torque limiter."""
        _vtable_call(
            self._ptr,
            self._SET_FF_MOTOR_GAIN,
            None,
            [c_uint32, c_float],
            motor_index,
            c_float(master_gain),
        )

    def set_rumble_state(self, params):
        _vtable_call(
            self._ptr,
            self._SET_RUMBLE_STATE,
            None,
            [POINTER(GameInputRumbleParams)],
            byref(params),
        )


# ---------------------------------------------------------------------------
# IGameInputForceFeedbackEffect  (IID 51BDA05E-F742-45D9-B085-9444AE48381D)
# ---------------------------------------------------------------------------

class IGameInputForceFeedbackEffect(_Unknown):
    _GET_MOTOR_INDEX = 4
    _GET_GAIN = 5
    _SET_GAIN = 6
    _SET_PARAMS = 8
    _GET_STATE = 9
    _SET_STATE = 10

    def get_motor_index(self):
        return _vtable_call(self._ptr, self._GET_MOTOR_INDEX, c_uint32, [])

    def get_gain(self):
        return _vtable_call(self._ptr, self._GET_GAIN, c_float, [])

    def set_gain(self, gain):
        _vtable_call(self._ptr, self._SET_GAIN, None, [c_float], c_float(gain))

    def set_params(self, params):
        return bool(
            _vtable_call(
                self._ptr,
                self._SET_PARAMS,
                c_bool,
                [POINTER(GameInputForceFeedbackParams)],
                byref(params),
            )
        )

    def get_state(self):
        return _vtable_call(self._ptr, self._GET_STATE, c_int32, [])

    def set_state(self, state):
        _vtable_call(self._ptr, self._SET_STATE, None, [c_int32], state)


# ---------------------------------------------------------------------------
# Entry point: GameInputCreate
# ---------------------------------------------------------------------------

def create_game_input(dll_name="GameInput.dll"):
    """
    Load GameInput.dll and call its exported GameInputCreate.

    Returns (IGameInput, path_to_dll). Raises GameInputError with the HRESULT if the
    runtime refuses, or OSError if the DLL is missing entirely.
    """
    dll = ctypes.WinDLL(dll_name)
    factory = dll.GameInputCreate
    factory.restype = c_int32
    factory.argtypes = [POINTER(c_void_p)]

    ptr = c_void_p()
    check("GameInputCreate", factory(byref(ptr)))
    if not ptr:
        raise GameInputError("GameInputCreate (returned NULL)", -1)
    return IGameInput(ptr), getattr(dll, "_name", dll_name)
