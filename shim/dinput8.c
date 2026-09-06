/*
 * WH33LH4X shim, phase D1 -- a do-nothing dinput8.dll proxy.
 *
 * WHY THIS EXISTS
 *
 * Windows.Gaming.Input gates BOTH force output and position reading on the calling process
 * being in the foreground. That gate lives in the GIP driver stack, kernel-side, so no
 * user-mode trick reaches it: there is no focus API imported by Windows.Gaming.Input.dll to
 * hook, GameInput reports zero force-feedback motors for this wheel, and a game will not bind
 * an axis unless it is focused. The only way to talk to the motor while a game is focused is
 * to be *inside the game's process*.
 *
 * A game loads dinput8.dll from its own directory before the system one (that is the standard
 * DLL search order for a non-KnownDLL), so dropping this file next to the exe gets us loaded.
 * We forward every export to the real system DLL and are otherwise invisible.
 *
 * THIS PHASE DOES NOTHING ELSE ON PURPOSE. The bar for D1 is: a game starts normally with
 * this present and still sees its controllers. Device work lands in D2 -- if the proxy is not
 * provably transparent first, every later bug is ambiguous between "our shim is wrong" and
 * "our forwarding is wrong".
 *
 * TWO TRAPS, BOTH LOAD-BEARING
 *
 * 1. We must NOT LoadLibrary from DllMain. The loader lock is held during DllMain and calling
 *    LoadLibrary there is documented as deadlock-prone -- and it is the kind of deadlock that
 *    shows up on someone else's machine, in one game, at startup, with no message. So the real
 *    DLL is resolved lazily on the first exported call, where no lock is held.
 *
 * 2. We must resolve the real DLL by ABSOLUTE SYSTEM PATH, never by the name "dinput8.dll".
 *    A bare name search starts in the application directory, which is where *we* live, so we
 *    would load ourselves and recurse until the stack dies.
 */

#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <strsafe.h>

#include "ipc.h"
#include "shim_log.h"
#include "wgi.h"

/*
 * Exported signatures use `const void *` where the SDK says REFIID / REFCLSID. Those are
 * `const GUID * const` in C, so this is ABI-identical, and it keeps the proxy free of
 * dinput.h -- we never inspect the values, only pass them along.
 */
typedef HRESULT(WINAPI *pfn_DirectInput8Create)(void *, DWORD, const void *, void **, void *);
typedef HRESULT(WINAPI *pfn_DllGetClassObject)(const void *, const void *, void **);
typedef HRESULT(WINAPI *pfn_NoArgs)(void);

static HMODULE               g_real;
static pfn_DirectInput8Create g_DirectInput8Create;
static pfn_DllGetClassObject  g_DllGetClassObject;
static pfn_NoArgs             g_DllCanUnloadNow;
static pfn_NoArgs             g_DllRegisterServer;
static pfn_NoArgs             g_DllUnregisterServer;

static INIT_ONCE g_once = INIT_ONCE_STATIC_INIT;

/* ------------------------------------------------------------------ logging */

/*
 * Log to %TEMP%, not next to the DLL: a game's own directory is often under Program Files and
 * not writable by a non-elevated process, and a proxy that crashes or silently fails to log
 * because of that would be maddening to diagnose. Append-and-close per line is slower than
 * holding a handle, but this logs a handful of lines at startup and nothing in the hot path,
 * and it means an aborted process still leaves a readable file.
 */
void shim_log(const char *fmt, ...)
{
    wchar_t path[MAX_PATH];
    DWORD   n = GetTempPathW(MAX_PATH, path);
    if (n == 0 || n > MAX_PATH - 24)
        return;
    StringCchCatW(path, MAX_PATH, L"wh33lh4x_shim.log");

    HANDLE f = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE)
        return;

    SYSTEMTIME t;
    GetLocalTime(&t);

    /*
     * strsafe rather than the _s CRT functions: mingw-w64 only declares the latter when
     * MINGW_HAS_SECURE_API happens to be defined, so they are a portability coin-flip.
     * StringCch* always truncates and always NUL-terminates, and returns
     * STRSAFE_E_INSUFFICIENT_BUFFER on truncation, which for a log line is fine.
     */
    char line[1024];
    StringCchPrintfA(line, ARRAYSIZE(line), "%02d:%02d:%02d.%03d [%lu] ",
                     t.wHour, t.wMinute, t.wSecond, t.wMilliseconds, GetCurrentProcessId());

    size_t head = 0;
    StringCchLengthA(line, ARRAYSIZE(line), &head);

    va_list ap;
    va_start(ap, fmt);
    StringCchVPrintfA(line + head, ARRAYSIZE(line) - head - 2, fmt, ap);
    va_end(ap);

    size_t len = 0;
    StringCchLengthA(line, ARRAYSIZE(line), &len);
    line[len++] = '\r';
    line[len++] = '\n';

    DWORD written = 0;
    WriteFile(f, line, (DWORD)len, &written, NULL);
    CloseHandle(f);
}

/* ------------------------------------------------------------- real dll load */

static BOOL CALLBACK init_real(PINIT_ONCE once, PVOID param, PVOID *ctx)
{
    (void)once;
    (void)param;
    (void)ctx;

    wchar_t path[MAX_PATH];
    UINT    n = GetSystemDirectoryW(path, MAX_PATH);
    if (n == 0 || n > MAX_PATH - 16) {
        shim_log("FATAL GetSystemDirectoryW failed (%u), err=%lu", n, GetLastError());
        return TRUE; /* leave g_real NULL; the exports below degrade to E_FAIL */
    }
    StringCchCatW(path, MAX_PATH, L"\\dinput8.dll");

    g_real = LoadLibraryW(path);
    if (!g_real) {
        shim_log("FATAL LoadLibraryW(%ls) failed, err=%lu", path, GetLastError());
        return TRUE;
    }

    g_DirectInput8Create =
        (pfn_DirectInput8Create)(void *)GetProcAddress(g_real, "DirectInput8Create");
    g_DllGetClassObject =
        (pfn_DllGetClassObject)(void *)GetProcAddress(g_real, "DllGetClassObject");
    g_DllCanUnloadNow = (pfn_NoArgs)(void *)GetProcAddress(g_real, "DllCanUnloadNow");
    g_DllRegisterServer = (pfn_NoArgs)(void *)GetProcAddress(g_real, "DllRegisterServer");
    g_DllUnregisterServer = (pfn_NoArgs)(void *)GetProcAddress(g_real, "DllUnregisterServer");

    shim_log("loaded %ls  create=%p getclass=%p canunload=%p reg=%p unreg=%p",
             path, (void *)g_DirectInput8Create, (void *)g_DllGetClassObject,
             (void *)g_DllCanUnloadNow, (void *)g_DllRegisterServer,
             (void *)g_DllUnregisterServer);
    return TRUE;
}

static void ensure_real(void)
{
    InitOnceExecuteOnce(&g_once, init_real, NULL, NULL);
}

/* --------------------------------------------------------------------- config */

/*
 * %TEMP%\wh33lh4x.cfg, `key=value` per line. One key:
 *
 *   selftest=1   load an effect and oscillate the motor, so the result can be felt by hand
 *
 * OFF unless asked for. With it off the shim still enumerates and logs what it found, which
 * claims nothing; with it on the shim takes the motor, and a motor taken and released badly
 * stays dead until the wheel is replugged.
 */

/*
 * The self-test sweeps both sides at two strengths rather than holding one level.
 *
 * 0.35 is already above the 0.30 the diagnostic menu has always been felt at, but a single
 * fixed magnitude leaves "too weak to notice" and "no force at all" looking identical -- and
 * those call for completely different next steps. Going to full torque on each side removes
 * that ambiguity: if any force is reaching the motor, 1.00 cannot be missed.
 */
#define SELFTEST_HOLD_MS   1500
#define SELFTEST_TICK_MS   62      /* ~16 Hz, fine for a hand-judged sweep */

/* ~100 Hz, matching the rate the bridge rewrites force at. Faster would burn the game's CPU
 * for nothing; slower would smear the detail the renderer works to produce. */
#define BRIDGE_TICK_MS     10

static BOOL  g_cfg_selftest;
static volatile LONG g_stop;
static HANDLE g_worker;

static void read_config(void)
{
    wchar_t path[MAX_PATH];
    char    buf[1024];
    DWORD   got = 0;
    HANDLE  f;
    const char *p;

    DWORD n = GetTempPathW(MAX_PATH, path);
    if (n == 0 || n > MAX_PATH - 20)
        return;
    StringCchCatW(path, MAX_PATH, L"wh33lh4x.cfg");

    f = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                    OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) {
        shim_log("config: none at %ls -- self-test off, discovery only", path);
        return;
    }
    if (ReadFile(f, buf, sizeof(buf) - 1, &got, NULL)) {
        buf[got] = '\0';
        p = strstr(buf, "selftest=");
        if (p)
            g_cfg_selftest = (p[9] == '1');
    }
    CloseHandle(f);
    shim_log("config: selftest=%d", g_cfg_selftest ? 1 : 0);
}

/* --------------------------------------------------------------------- worker */

static BOOL stopping(void)
{
    return InterlockedCompareExchange(&g_stop, 0, 0) != 0;
}

/* Alternate a steady push left and right so the result is unmistakable by hand. */
static void selftest_loop(wgi_motor *m)
{
    static const float LEVELS[] = { 0.35f, 1.00f, -0.35f, -1.00f };
    static const char *NAMES[]  = { "right 35%", "right FULL", "left 35%", "left FULL" };
    int   phase = 0;
    DWORD steps = 0;

    shim_log("selftest: sweeping %s / %s / %s / %s, %d ms each -- hands on the wheel",
             NAMES[0], NAMES[1], NAMES[2], NAMES[3], SELFTEST_HOLD_MS);

    while (!stopping()) {
        DWORD until = GetTickCount() + SELFTEST_HOLD_MS;
        shim_log("selftest: %-11s x = %+.2f", NAMES[phase], (double)LEVELS[phase]);
        if (!wgi_set_force(m, LEVELS[phase])) {
            shim_log("selftest: set_force failed -- stopping");
            return;
        }
        /* SetParameters live-updates a running effect, so the magnitude just stays put --
         * there is no heartbeat to send. Sleep in slices so stopping() stays responsive. */
        while (GetTickCount() < until && !stopping())
            Sleep(SELFTEST_TICK_MS);
        phase = (phase + 1) % (int)(sizeof(LEVELS) / sizeof(LEVELS[0]));
        steps++;
    }
    shim_log("selftest: %lu step(s) completed", steps);
}

/*
 * Everything here logs and returns rather than retrying: this thread lives inside somebody
 * else's game, and a shim that spins or crashes is worse than a shim that does nothing.
 *
 * W1 (finding the wheel and its motor) is deliberately separable from W2 (commanding force):
 * if enumeration works and force does not, the log says which, and those are different
 * problems. `selftest=` gates only the force half.
 */
/*
 * Follow the bridge: apply whatever force it publishes, for as long as it is alive.
 *
 * The effect is loaded when a bridge appears and released when it goes away, rather than held
 * for the life of the game. Holding the motor while nothing is driving it is precisely the
 * state that leaves the wheel dead for every other application.
 */
static void bridge_loop(wgi_motor *m, wh_ipc *ipc)
{
    BOOL  loaded = FALSE;
    float gain = 1.0f;
    DWORD reported = 0;

    shim_log("ipc: waiting for the bridge");

    while (!stopping()) {
        wh_reading r;
        BOOL       have;

        /*
         * Publish the reading EVERY tick, whether or not a bridge is driving force. The game
         * cannot bind an axis that never moves, and it will not send force feedback for an
         * axis it has not bound -- so input has to flow before output can. Making this
         * conditional on the bridge being live would deadlock the two halves against each
         * other.
         */
        have = wgi_read(m, &r.wheel, &r.throttle, &r.brake, &r.clutch, &r.handbrake,
                        &r.buttons, &r.shifter_gear);

        if (wh_ipc_bridge_alive(ipc)) {
            if (!loaded) {
                gain = ipc->block->gain;
                if (gain <= 0.0f || gain > 1.0f)
                    gain = 1.0f;
                if (!wgi_load_effect(m, gain)) {
                    shim_log("ipc: could not load the effect -- stopping");
                    return;
                }
                loaded = TRUE;
                shim_log("ipc: bridge is live, effect loaded (gain %.2f)", (double)gain);
            }
            wgi_set_force(m, ipc->block->force);
            wh_ipc_publish(ipc, WH_STATE_ACTIVE, have ? &r : NULL);
        } else if (loaded) {
            /* The bridge stopped stamping. Zero the force and give the motor back rather
             * than leaving the last commanded value pulling forever. */
            shim_log("ipc: bridge went quiet -- releasing the motor");
            wgi_set_force(m, 0.0f);
            wgi_release_effect(m);
            loaded = FALSE;
            wh_ipc_publish(ipc, WH_STATE_MOTOR, have ? &r : NULL);
        } else {
            wh_ipc_publish(ipc, WH_STATE_MOTOR, have ? &r : NULL);
        }

        /* One line the first time a reading arrives, so a dead input path is obvious in the
         * log rather than being something you have to infer from vJoy sitting still. */
        if (have && !reported) {
            reported = 1;
            shim_log("ipc: publishing readings (wheel %+.3f)", (double)r.wheel);
        }
        Sleep(BRIDGE_TICK_MS);
    }

    if (loaded) {
        wgi_set_force(m, 0.0f);
        wgi_release_effect(m);
    }
}

static DWORD WINAPI worker_main(LPVOID param)
{
    wgi_motor *m;
    wh_ipc     ipc;
    (void)param;

    m = wgi_open(10000);
    if (!wgi_have_motor(m)) {
        shim_log("wgi: no motor -- nothing to drive");
        wgi_close(m);
        return 0;
    }

    /* selftest= is the standalone diagnostic: drive the motor with no bridge at all, so the
     * output path can be judged by hand without the rest of the pipeline running. */
    if (g_cfg_selftest) {
        if (wgi_load_effect(m, 1.0f))
            selftest_loop(m);
        wgi_set_force(m, 0.0f);
        wgi_close(m);
        shim_log("wgi: worker done (selftest)");
        return 0;
    }

    if (!wh_ipc_open(&ipc)) {
        wgi_close(m);
        return 0;
    }
    bridge_loop(m, &ipc);
    wh_ipc_publish(&ipc, WH_STATE_NONE, NULL);
    wh_ipc_close(&ipc);
    wgi_close(m);
    shim_log("wgi: worker done");
    return 0;
}

static BOOL CALLBACK init_worker(PINIT_ONCE once, PVOID param, PVOID *ctx)
{
    (void)once;
    (void)param;
    (void)ctx;

    read_config();
    /*
     * The thread always starts, because ENUMERATING is harmless -- it claims nothing. Only
     * loading an effect takes the motor, and that stays gated behind selftest=, because a
     * claimed-then-released motor can be left accepting effects while producing no torque,
     * which would break force feedback for every other application on the machine.
     */
    g_worker = CreateThread(NULL, 0, worker_main, NULL, 0, NULL);
    if (!g_worker)
        shim_log("gip: CreateThread failed, err=%lu", GetLastError());
    return TRUE;
}

static INIT_ONCE g_worker_once = INIT_ONCE_STATIC_INIT;

static void ensure_worker(void)
{
    InitOnceExecuteOnce(&g_worker_once, init_worker, NULL, NULL);
}

/* ----------------------------------------------------------------- forwarding */

/*
 * Every export degrades to a failure HRESULT rather than crashing if the real DLL could not be
 * resolved. A game that gets E_FAIL from DirectInput8Create will complain about input; a game
 * that jumps through a NULL pointer takes the whole process down and looks like our fault in a
 * way nobody can read from a crash dump.
 *
 * No __declspec(dllexport) here -- the export surface is declared in dinput8.def, which
 * explains why. DllGetClassObject and DllCanUnloadNow must also match the declarations
 * combaseapi.h has already made, or this will not compile.
 */

HRESULT WINAPI DirectInput8Create(void *hinst, DWORD version, const void *riid, void **out,
                                  void *outer)
{
    ensure_real();
    if (!g_DirectInput8Create) {
        shim_log("DirectInput8Create: real export missing");
        return E_FAIL;
    }
    HRESULT hr = g_DirectInput8Create(hinst, version, riid, out, outer);
    shim_log("DirectInput8Create(version=0x%04lx) -> 0x%08lx", version, (unsigned long)hr);
    /*
     * Start the motor worker here, NOT in DllMain: creating a thread that immediately opens
     * devices while the loader lock is held is the same class of hazard as LoadLibrary there.
     * A game calls this early, and only once matters -- InitOnceExecuteOnce handles the rest.
     */
    ensure_worker();
    return hr;
}

HRESULT WINAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, LPVOID *ppv)
{
    ensure_real();
    if (!g_DllGetClassObject)
        return CLASS_E_CLASSNOTAVAILABLE;
    return g_DllGetClassObject(rclsid, riid, ppv);
}

HRESULT WINAPI DllCanUnloadNow(void)
{
    ensure_real();
    /*
     * S_FALSE means "do not unload me". That is the safe answer when we cannot ask the real
     * DLL: unloading a proxy that something still holds a pointer into is unrecoverable,
     * whereas staying resident merely costs a few hundred KB.
     */
    if (!g_DllCanUnloadNow)
        return S_FALSE;
    return g_DllCanUnloadNow();
}

HRESULT WINAPI DllRegisterServer(void)
{
    ensure_real();
    if (!g_DllRegisterServer)
        return E_FAIL;
    return g_DllRegisterServer();
}

HRESULT WINAPI DllUnregisterServer(void)
{
    ensure_real();
    if (!g_DllUnregisterServer)
        return E_FAIL;
    return g_DllUnregisterServer();
}

/* --------------------------------------------------------------------- DllMain */

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)reserved;

    if (reason == DLL_PROCESS_ATTACH) {
        /* No FFB work happens per-thread, and a game spawns many threads. */
        DisableThreadLibraryCalls(inst);

        wchar_t exe[MAX_PATH];
        if (GetModuleFileNameW(NULL, exe, MAX_PATH) == 0)
            exe[0] = L'\0';
        shim_log("attach: %ls", exe);
        /* Deliberately NOT loading the real DLL here -- see trap 1 at the top of this file. */
    } else if (reason == DLL_PROCESS_DETACH) {
        /*
         * Signal the worker and do NOT wait for it. Joining a thread from DllMain deadlocks:
         * the loader lock is held here and the exiting thread needs it. The process is going
         * away regardless, and the driver drops our force when the handle closes.
         */
        InterlockedExchange(&g_stop, 1);
    }
    return TRUE;
}
