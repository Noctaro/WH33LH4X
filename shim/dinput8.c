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

#include "gip.h"
#include "shim_log.h"

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
 * %TEMP%\wh33lh4x.cfg, `key=value` per line. Two keys:
 *
 *   device=9735815b51cc0000   fallback device id, written by gip_trace.py
 *   selftest=1                oscillate the motor so the gate can be tested by hand
 *
 * The self-test is OFF unless asked for, so a normal game launch is unaffected by the shim
 * being present. The device id is only a fallback -- discovery is tried first, because
 * hard-coding one machine's id is not something to build on.
 */

#define SELFTEST_MAGNITUDE 0.35f   /* felt clearly; nowhere near fighting the user */
#define SELFTEST_HOLD_MS   1500
#define SELFTEST_TICK_MS   62      /* ~16 Hz, the rate WGI re-sends at */

static char  g_cfg_device[17];
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
        p = strstr(buf, "device=");
        if (p) {
            size_t i;
            p += 7;
            for (i = 0; i < 16; i++) {
                char c = p[i];
                BOOL hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
                           (c >= 'A' && c <= 'F');
                if (!hex)
                    break;
                g_cfg_device[i] = c;
            }
            if (i == 16)
                g_cfg_device[16] = '\0';
            else
                g_cfg_device[0] = '\0';
        }
        p = strstr(buf, "selftest=");
        if (p)
            g_cfg_selftest = (p[9] == '1');
    }
    CloseHandle(f);
    shim_log("config: device=%s selftest=%d",
             g_cfg_device[0] ? g_cfg_device : "(none)", g_cfg_selftest ? 1 : 0);
}

/* --------------------------------------------------------------------- worker */

static BOOL stopping(void)
{
    return InterlockedCompareExchange(&g_stop, 0, 0) != 0;
}

/*
 * Alternate a steady push left and right so the result is unmistakable by hand.
 *
 * The force is re-sent at ~16 Hz rather than set once, because that is what WGI does -- a
 * single write may well be treated as a stale command and dropped.
 */
static void selftest_loop(gip_device *dev)
{
    const float levels[2] = { SELFTEST_MAGNITUDE, -SELFTEST_MAGNITUDE };
    int   phase = 0;
    DWORD halves = 0;

    shim_log("selftest: oscillating +/-%.2f every %d ms -- hands on the wheel",
             (double)SELFTEST_MAGNITUDE, SELFTEST_HOLD_MS);

    while (!stopping()) {
        DWORD until = GetTickCount() + SELFTEST_HOLD_MS;
        while (GetTickCount() < until && !stopping()) {
            if (!gip_set_force(dev, levels[phase])) {
                shim_log("selftest: write failed, err=%lu -- stopping", dev->last_error);
                return;
            }
            Sleep(SELFTEST_TICK_MS);
        }
        phase ^= 1;
        if (++halves % 4 == 0)
            shim_log("selftest: %lu half-cycles, %lu writes, %lu error(s)",
                     halves, dev->writes, dev->write_errors);
    }
}

/*
 * Everything here logs and returns rather than retrying: this thread lives inside somebody
 * else's game, and a shim that spins or crashes is worse than a shim that does nothing.
 */
static DWORD WINAPI worker_main(LPVOID param)
{
    gip_device dev;
    (void)param;

    if (!gip_open(&dev))
        return 0;

    /* Discovery first. In-game we are foreground with a proper window, which is what the GIP
     * driver wants before it delivers input reports -- so this is the case most likely to
     * work, and trying it also tells us whether reads are usable from here at all. */
    if (!gip_discover(&dev, 2000) && g_cfg_device[0])
        gip_set_device_id_hex(&dev, g_cfg_device);

    if (!dev.have_device_id) {
        shim_log("gip: no device id (discovery failed, no config) -- nothing to drive");
        gip_close(&dev);
        return 0;
    }

    if (!gip_load_effect(&dev)) {
        gip_close(&dev);
        return 0;
    }

    if (g_cfg_selftest)
        selftest_loop(&dev);
    else
        shim_log("gip: effect ready; self-test disabled, so idling");

    gip_set_force(&dev, 0.0f);
    gip_close(&dev);
    shim_log("gip: worker done (%lu writes, %lu error(s))", dev.writes, dev.write_errors);
    return 0;
}

static BOOL CALLBACK init_worker(PINIT_ONCE once, PVOID param, PVOID *ctx)
{
    (void)once;
    (void)param;
    (void)ctx;

    read_config();
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
