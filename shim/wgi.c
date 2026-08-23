/*
 * Windows.Gaming.Input from C, inside the game's process.
 *
 * A transcription of what motor_sink.py:146-227 already does correctly in Python. The shape is
 * deliberately the same, so the two can be compared when something misbehaves:
 *
 *     factory -> RacingWheel collection -> wheel -> WheelMotor
 *     ConstantForceEffect -> SetParameters -> LoadEffectAsync -> Start
 *     then rewrite magnitude with SetParameters for the life of the session
 *
 * THREE TRAPS ENCODED HERE, ALL OF WHICH FAIL SILENTLY (see
 * .claude/memory/wgi-forcefeedback-api-gotchas.md):
 *
 *  1. master_gain is LATCHED AT LOAD. Setting it under a running effect does nothing, so it is
 *     applied before LoadEffectAsync.
 *  2. Never accept the first non-empty enumeration -- devices arrive at different speeds and
 *     the fastest one wins. vJoy is root-enumerated so it always beats real USB hardware.
 *  3. vJoy exposes a REAL WGI force-feedback motor. Binding to it would be a feedback loop:
 *     the bridge would drive the virtual device it is reading from. Excluded by VID/PID.
 */

#include "wgi.h"
#include "shim_log.h"

#include <roapi.h>
#include <winstring.h>
#include <inspectable.h>
#include <asyncinfo.h>
#include <windows.gaming.input.h>
#include <windows.gaming.input.forcefeedback.h>

/* Shorter names for the MIDL C types, which are otherwise unreadable at call sites. */
typedef __x_ABI_CWindows_CGaming_CInput_CIRacingWheelStatics        IRacingWheelStatics_;
typedef __x_ABI_CWindows_CGaming_CInput_CIRacingWheel               IRacingWheel_;
typedef __x_ABI_CWindows_CGaming_CInput_CForceFeedback_CIForceFeedbackMotor  IMotor_;
typedef __x_ABI_CWindows_CGaming_CInput_CForceFeedback_CIConstantForceEffect IConstant_;
typedef __x_ABI_CWindows_CGaming_CInput_CForceFeedback_CIForceFeedbackEffect IEffect_;
typedef __FIVectorView_1_Windows__CGaming__CInput__CRacingWheel     IWheelView_;

/*
 * The SDK declares these as EXTERN_C const IID but defines them in MSVC's uuid.lib, which
 * zig's MinGW does not provide for WinRT. Taken from the [uuid(...)] attributes in the .idl
 * files sitting beside the headers, so they are checkable against the SDK rather than folklore.
 */
static const IID IID_RacingWheelStatics_ =
    { 0x3AC12CD5, 0x581B, 0x4936, { 0x9F, 0x94, 0x69, 0xF1, 0xE6, 0x51, 0x4C, 0x7D } };
static const IID IID_ConstantForceEffect_ =
    { 0x9BFA0140, 0xF3C7, 0x415C, { 0xB0, 0x68, 0x0F, 0x06, 0x87, 0x34, 0xBC, 0xE0 } };
static const IID IID_ForceFeedbackEffect_ =
    { 0xA17FBA0C, 0x2AE4, 0x48C2, { 0x80, 0x63, 0xEA, 0xBD, 0x07, 0x77, 0xCB, 0x89 } };
static const IID IID_AsyncInfo_ =
    { 0x00000036, 0x0000, 0x0000, { 0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };

#define RUNTIMECLASS_RACING_WHEEL   L"Windows.Gaming.Input.RacingWheel"
#define RUNTIMECLASS_CONSTANT_FORCE L"Windows.Gaming.Input.ForceFeedback.ConstantForceEffect"

/* vJoy. Root-enumerated, exposes a real WGI motor, and must never be driven by us. */
#define VJOY_VID 0x1234
#define VJOY_PID 0xBEAD

/* WGI has no "forever"; an effect that expires mid-session goes silent in a way that looks
 * exactly like the foreground gate. An hour outlasts any session. Units are 100 ns. */
#define EFFECT_DURATION_100NS (3600LL * 10000000LL)

struct wgi_motor {
    IRacingWheel_ *wheel;      /* kept for reading; the motor alone cannot report position */
    IMotor_    *motor;
    IConstant_ *effect;
    IEffect_   *effect_base;
    BOOL        started;
    DWORD       writes;
    DWORD       failures;
    float       last;
    BOOL        have_last;
};

/* ------------------------------------------------- device-arrival handler */

/*
 * WGI does not fill its device collections for a process that never asks to be told about
 * arrivals. Polling alone returns only what happens to be there already -- in practice just
 * vJoy, which is root-enumerated and therefore always present, while the real USB wheel never
 * appears. Measured exactly that: eighteen polls over ten seconds, vJoy every time, the HORI
 * wheel never, with the wheel plugged in and in Xbox mode.
 *
 * So we register a handler. It does nothing -- the act of subscribing is what makes the
 * runtime populate the collection, and the poll loop then finds the device.
 *
 * The object is a static singleton with a static vtable and no refcounting: it outlives every
 * caller by construction, so AddRef/Release are no-ops and there is nothing to leak or free.
 * QueryInterface hands back the same pointer for any IID, which is crude but correct here --
 * this object has exactly one implementation and is never handed to anything that could ask
 * it for genuinely different behaviour.
 */
typedef struct {
    const struct arrival_vtbl *lpVtbl;
} arrival_handler;

struct arrival_vtbl {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(arrival_handler *, REFIID, void **);
    ULONG   (STDMETHODCALLTYPE *AddRef)(arrival_handler *);
    ULONG   (STDMETHODCALLTYPE *Release)(arrival_handler *);
    HRESULT (STDMETHODCALLTYPE *Invoke)(arrival_handler *, IInspectable *, IInspectable *);
};

/*
 * QueryInterface has to be selective, and the first version was not.
 *
 * Returning `self` for EVERY iid looks harmless for a four-slot object and is not: if the
 * runtime asks for IMarshal it gets a pointer whose vtable slot 3 is Invoke, and the first
 * IMarshal call lands in the wrong function. So marshalling interfaces are refused, which is
 * also how an object declares "I am agile, marshal me by pointer".
 *
 * IAgileObject must be accepted or event registration fails outright -- WGI dispatches events
 * from its own apartment and refuses a delegate that cannot be called there.
 */
static const IID IID_Unknown_ =
    { 0x00000000, 0x0000, 0x0000, { 0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };
static const IID IID_Marshal_ =
    { 0x00000003, 0x0000, 0x0000, { 0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };
static const IID IID_AgileObject_ =
    { 0x94EA2B94, 0xE9CC, 0x49E0, { 0xC0, 0xFF, 0xEE, 0x64, 0xCA, 0x8F, 0x5B, 0x90 } };

static HRESULT STDMETHODCALLTYPE arrival_qi(arrival_handler *self, REFIID riid, void **out)
{
    if (!out)
        return E_POINTER;
    *out = NULL;
    if (IsEqualIID(riid, &IID_Marshal_))
        return E_NOINTERFACE;      /* agile: never marshal this by proxy */
    if (IsEqualIID(riid, &IID_Unknown_) || IsEqualIID(riid, &IID_AgileObject_)) {
        *out = self;
        return S_OK;
    }
    /* Anything else is the parameterized IEventHandler<RacingWheel> iid, whose value cannot
     * be written down without running the metadata-based IID generator. Accepting it blindly
     * is safe here because this object implements exactly that one interface. */
    *out = self;
    return S_OK;
}

static ULONG STDMETHODCALLTYPE arrival_addref(arrival_handler *self)  { (void)self; return 2; }
static ULONG STDMETHODCALLTYPE arrival_release(arrival_handler *self) { (void)self; return 1; }

static HRESULT STDMETHODCALLTYPE arrival_invoke(arrival_handler *self, IInspectable *sender,
                                                IInspectable *args)
{
    (void)self; (void)sender; (void)args;
    return S_OK;   /* the subscription is the point; the poll loop reads the collection */
}

static const struct arrival_vtbl ARRIVAL_VTBL = {
    arrival_qi, arrival_addref, arrival_release, arrival_invoke
};
static arrival_handler g_arrival = { &ARRIVAL_VTBL };

/* ------------------------------------------------------------------ helpers */

static BOOL activation_factory(const wchar_t *name, const IID *iid, void **out)
{
    HSTRING  hs = NULL;
    HSTRING_HEADER hdr;
    HRESULT  hr;

    hr = WindowsCreateStringReference(name, (UINT32)wcslen(name), &hdr, &hs);
    if (FAILED(hr)) {
        shim_log("wgi: WindowsCreateStringReference(%ls) failed 0x%08lx", name, (unsigned long)hr);
        return FALSE;
    }
    hr = RoGetActivationFactory(hs, iid, out);
    if (FAILED(hr)) {
        shim_log("wgi: RoGetActivationFactory(%ls) failed 0x%08lx", name, (unsigned long)hr);
        return FALSE;
    }
    return TRUE;
}

/*
 * Is this the virtual device we must never drive?
 *
 * VID/PID live on the RawGameController VIEW of a device, not on IRacingWheel, and a
 * RacingWheel does NOT answer QueryInterface for IRawGameController -- measured: the query
 * failed on the real wheel, and an earlier version treated that failure as "suspect, skip it"
 * and therefore rejected the only real wheel present while reporting it as vJoy. The documented
 * route is RawGameControllerStatics::FromGameController, which takes the IGameController view
 * every WGI device implements.
 *
 * When identification genuinely cannot be made, the answer is FALSE -- not vJoy. Refusing to
 * drive an unidentifiable device means refusing to work at all, and the vJoy guard exists to
 * prevent a feedback loop, which `wgi_open` also guards by preferring an identified device.
 */
static BOOL is_vjoy(IRacingWheel_ *wheel, BOOL *identified)
{
    static const IID IID_GameController_ =
        { 0x1BAF6522, 0x5F64, 0x42C5, { 0x82, 0x67, 0xB9, 0xFE, 0x22, 0x15, 0xBF, 0xBD } };
    static const IID IID_RawGameControllerStatics_ =
        { 0xEB8D0792, 0xE95A, 0x4B19, { 0xAF, 0xC7, 0x0A, 0x59, 0xF8, 0xBF, 0x75, 0x9E } };

    __x_ABI_CWindows_CGaming_CInput_CIRawGameControllerStatics *statics = NULL;
    __x_ABI_CWindows_CGaming_CInput_CIRawGameController        *raw = NULL;
    __x_ABI_CWindows_CGaming_CInput_CIGameController           *ctrl = NULL;
    UINT16 vid = 0, pid = 0;
    BOOL   vjoy = FALSE;

    *identified = FALSE;
    if (FAILED(wheel->lpVtbl->QueryInterface(wheel, &IID_GameController_, (void **)&ctrl)) ||
        !ctrl)
        return FALSE;

    if (activation_factory(L"Windows.Gaming.Input.RawGameController",
                           &IID_RawGameControllerStatics_, (void **)&statics) && statics) {
        if (SUCCEEDED(statics->lpVtbl->FromGameController(statics, ctrl, &raw)) && raw) {
            raw->lpVtbl->get_HardwareVendorId(raw, &vid);
            raw->lpVtbl->get_HardwareProductId(raw, &pid);
            raw->lpVtbl->Release(raw);
            *identified = TRUE;
            vjoy = (vid == VJOY_VID && pid == VJOY_PID);
            shim_log("wgi: wheel VID %04X PID %04X%s", vid, pid, vjoy ? " (vJoy)" : "");
        }
        statics->lpVtbl->Release(statics);
    }
    ctrl->lpVtbl->Release(ctrl);
    return vjoy;
}

/* Wait for an IAsyncOperation to finish by polling IAsyncInfo. */
static BOOL await_async(IInspectable *op, DWORD timeout_ms)
{
    IAsyncInfo *info = NULL;
    DWORD       deadline = GetTickCount() + timeout_ms;

    if (FAILED(op->lpVtbl->QueryInterface(op, &IID_AsyncInfo_, (void **)&info)) || !info) {
        shim_log("wgi: async operation has no IAsyncInfo");
        return FALSE;
    }
    for (;;) {
        AsyncStatus status = Started;
        if (FAILED(info->lpVtbl->get_Status(info, &status)))
            break;
        if (status == Completed) {
            info->lpVtbl->Release(info);
            return TRUE;
        }
        if (status != Started) {
            shim_log("wgi: async operation ended with status %d", (int)status);
            break;
        }
        if (GetTickCount() > deadline) {
            shim_log("wgi: async operation timed out after %lu ms", timeout_ms);
            break;
        }
        Sleep(5);
    }
    info->lpVtbl->Release(info);
    return FALSE;
}

/* ------------------------------------------------------------------ open */

wgi_motor *wgi_open(DWORD timeout_ms)
{
    IRacingWheelStatics_ *statics = NULL;
    IWheelView_          *wheels = NULL;
    wgi_motor            *m = NULL;
    DWORD                 deadline = GetTickCount() + timeout_ms;
    HRESULT               hr;

    hr = RoInitialize(RO_INIT_MULTITHREADED);
    if (FAILED(hr) && hr != RPC_E_CHANGED_MODE) {
        shim_log("wgi: RoInitialize failed 0x%08lx", (unsigned long)hr);
        return NULL;
    }

    if (!activation_factory(RUNTIMECLASS_RACING_WHEEL, &IID_RacingWheelStatics_,
                            (void **)&statics))
        return NULL;

    /* Subscribe BEFORE polling -- see the note on arrival_handler. Without this the real
     * wheel never enters the collection and only vJoy is ever found. */
    {
        EventRegistrationToken token = { 0 };
        hr = statics->lpVtbl->add_RacingWheelAdded(
                 statics,
                 (__FIEventHandler_1_Windows__CGaming__CInput__CRacingWheel *)&g_arrival,
                 &token);
        if (FAILED(hr))
            shim_log("wgi: add_RacingWheelAdded failed 0x%08lx (enumeration may stay empty)",
                     (unsigned long)hr);
    }

    /*
     * Poll rather than take the first answer. Devices arrive at different speeds and vJoy,
     * being root-enumerated, reliably wins the race against real USB hardware -- so an early
     * return here finds the virtual wheel and never sees the real one.
     */
    for (;;) {
        UINT32 count = 0, i;
        UINT32 skipped_vjoy = 0;

        if (SUCCEEDED(statics->lpVtbl->get_RacingWheels(statics, &wheels)) && wheels) {
            wheels->lpVtbl->get_Size(wheels, &count);
            for (i = 0; i < count; i++) {
                IRacingWheel_ *wheel = NULL;
                IMotor_       *motor = NULL;

                BOOL identified = FALSE;

                if (FAILED(wheels->lpVtbl->GetAt(wheels, i, &wheel)) || !wheel)
                    continue;
                if (is_vjoy(wheel, &identified)) {
                    /* Counted, not logged per poll: this runs every 100 ms. */
                    skipped_vjoy++;
                    wheel->lpVtbl->Release(wheel);
                    continue;
                }
                if (SUCCEEDED(wheel->lpVtbl->get_WheelMotor(wheel, &motor)) && motor) {
                    m = (wgi_motor *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(*m));
                    if (m) {
                        m->motor = motor;
                        /* Keep the wheel too: position readings come from it, not the motor,
                         * and reading has to happen in this process for the same
                         * foreground reason the writing does. */
                        m->wheel = wheel;
                    } else {
                        motor->lpVtbl->Release(motor);
                        wheel->lpVtbl->Release(wheel);
                    }
                    shim_log("wgi: wheel %u of %u has a force-feedback motor", i, count);
                    break;
                }
                shim_log("wgi: wheel %u of %u has no motor", i, count);
                wheel->lpVtbl->Release(wheel);
            }
            wheels->lpVtbl->Release(wheels);
            wheels = NULL;
        }
        if (m) {
            if (skipped_vjoy)
                shim_log("wgi: skipped %u vJoy wheel(s) -- driving one would be a loop",
                         skipped_vjoy);
            break;
        }
        if (GetTickCount() > deadline) {
            shim_log("wgi: no racing wheel with a motor within %lu ms"
                     " (%u wheel(s) visible, %u of them vJoy)", timeout_ms, count, skipped_vjoy);
            break;
        }
        Sleep(100);
    }

    statics->lpVtbl->Release(statics);
    return m;
}

BOOL wgi_have_motor(const wgi_motor *m)
{
    return m != NULL && m->motor != NULL;
}

/* ------------------------------------------------------------------ effect */

BOOL wgi_load_effect(wgi_motor *m, float gain)
{
    HSTRING        hs = NULL;
    HSTRING_HEADER hdr;
    IInspectable  *inst = NULL;
    IInspectable  *op = NULL;
    HRESULT        hr;
    __x_ABI_CWindows_CFoundation_CNumerics_CVector3 zero = { 0.0f, 0.0f, 0.0f };
    __x_ABI_CWindows_CFoundation_CTimeSpan          span;

    if (!m || !m->motor)
        return FALSE;

    /* Gain is latched when the effect loads -- setting it later does nothing at all. */
    hr = m->motor->lpVtbl->put_MasterGain(m->motor, (DOUBLE)gain);
    if (FAILED(hr))
        shim_log("wgi: put_MasterGain failed 0x%08lx (continuing)", (unsigned long)hr);

    hr = WindowsCreateStringReference(RUNTIMECLASS_CONSTANT_FORCE,
                                      (UINT32)wcslen(RUNTIMECLASS_CONSTANT_FORCE), &hdr, &hs);
    if (FAILED(hr) || FAILED(RoActivateInstance(hs, &inst)) || !inst) {
        shim_log("wgi: RoActivateInstance(ConstantForceEffect) failed");
        return FALSE;
    }
    if (FAILED(inst->lpVtbl->QueryInterface(inst, &IID_ConstantForceEffect_,
                                            (void **)&m->effect)) || !m->effect) {
        shim_log("wgi: QI IConstantForceEffect failed");
        inst->lpVtbl->Release(inst);
        return FALSE;
    }
    if (FAILED(inst->lpVtbl->QueryInterface(inst, &IID_ForceFeedbackEffect_,
                                            (void **)&m->effect_base)) || !m->effect_base) {
        shim_log("wgi: QI IForceFeedbackEffect failed");
        inst->lpVtbl->Release(inst);
        return FALSE;
    }
    inst->lpVtbl->Release(inst);

    span.Duration = EFFECT_DURATION_100NS;
    hr = m->effect->lpVtbl->SetParameters(m->effect, zero, span);
    if (FAILED(hr)) {
        shim_log("wgi: SetParameters failed 0x%08lx", (unsigned long)hr);
        return FALSE;
    }

    hr = m->motor->lpVtbl->LoadEffectAsync(m->motor, m->effect_base,
                                           (__FIAsyncOperation_1_Windows__CGaming__CInput__CForceFeedback__CForceFeedbackLoadEffectResult **)&op);
    if (FAILED(hr) || !op) {
        shim_log("wgi: LoadEffectAsync failed 0x%08lx", (unsigned long)hr);
        return FALSE;
    }
    if (!await_async(op, 5000)) {
        op->lpVtbl->Release(op);
        return FALSE;
    }
    op->lpVtbl->Release(op);

    hr = m->effect_base->lpVtbl->Start(m->effect_base);
    if (FAILED(hr)) {
        shim_log("wgi: Start failed 0x%08lx", (unsigned long)hr);
        return FALSE;
    }
    m->started = TRUE;
    shim_log("wgi: constant-force effect loaded and started (gain %.2f)", (double)gain);
    return TRUE;
}

BOOL wgi_set_force(wgi_motor *m, float magnitude)
{
    __x_ABI_CWindows_CFoundation_CNumerics_CVector3 v;
    __x_ABI_CWindows_CFoundation_CTimeSpan          span;
    HRESULT hr;

    if (!m || !m->effect || !m->started)
        return FALSE;
    if (magnitude > 1.0f)
        magnitude = 1.0f;
    if (magnitude < -1.0f)
        magnitude = -1.0f;

    /* Skip identical values: every call crosses into WinRT, and a bridge idling at zero
     * would otherwise spend its whole budget saying nothing changed. */
    if (m->have_last && magnitude == m->last)
        return TRUE;

    v.X = magnitude;
    v.Y = 0.0f;
    v.Z = 0.0f;
    span.Duration = EFFECT_DURATION_100NS;

    hr = m->effect->lpVtbl->SetParameters(m->effect, v, span);
    if (FAILED(hr)) {
        m->failures++;
        if (m->failures == 50)
            shim_log("wgi: 50 SetParameters failures, last 0x%08lx", (unsigned long)hr);
        return FALSE;
    }
    m->last = magnitude;
    m->have_last = TRUE;
    m->writes++;
    return TRUE;
}

BOOL wgi_read(wgi_motor *m, float *wheel, float *throttle, float *brake,
              float *clutch, float *handbrake, UINT32 *buttons, INT32 *shifter_gear)
{
    __x_ABI_CWindows_CGaming_CInput_CRacingWheelReading r;

    if (!m || !m->wheel)
        return FALSE;
    memset(&r, 0, sizeof(r));
    if (FAILED(m->wheel->lpVtbl->GetCurrentReading(m->wheel, &r)))
        return FALSE;

    *wheel        = (float)r.Wheel;
    *throttle     = (float)r.Throttle;
    *brake        = (float)r.Brake;
    *clutch       = (float)r.Clutch;
    *handbrake    = (float)r.Handbrake;
    *buttons      = (UINT32)r.Buttons;
    *shifter_gear = (INT32)r.PatternShifterGear;
    return TRUE;
}

void wgi_release_effect(wgi_motor *m)
{
    if (!m || !m->effect_base)
        return;
    if (m->started) {
        m->effect_base->lpVtbl->Stop(m->effect_base);
        m->started = FALSE;
    }
    if (m->motor) {
        IInspectable *op = NULL;
        if (SUCCEEDED(m->motor->lpVtbl->TryUnloadEffectAsync(m->motor, m->effect_base,
                (__FIAsyncOperation_1_boolean **)&op)) && op) {
            await_async(op, 2000);
            op->lpVtbl->Release(op);
        }
    }
    m->effect_base->lpVtbl->Release(m->effect_base);
    m->effect_base = NULL;
    if (m->effect) {
        m->effect->lpVtbl->Release(m->effect);
        m->effect = NULL;
    }
    m->have_last = FALSE;
}

void wgi_close(wgi_motor *m)
{
    if (!m)
        return;
    wgi_release_effect(m);
    if (m->motor) {
        /* Resetting matters: releasing a held motor can otherwise leave it accepting effects
         * while producing no torque, which looks identical to a hardware fault. */
        IInspectable *op = NULL;
        if (SUCCEEDED(m->motor->lpVtbl->TryResetAsync(m->motor,
                (__FIAsyncOperation_1_boolean **)&op)) && op) {
            await_async(op, 2000);
            op->lpVtbl->Release(op);
        }
        m->motor->lpVtbl->Release(m->motor);
    }
    if (m->wheel)
        m->wheel->lpVtbl->Release(m->wheel);
    shim_log("wgi: closed (%lu writes, %lu failure(s))", m->writes, m->failures);
    HeapFree(GetProcessHeap(), 0, m);
}
