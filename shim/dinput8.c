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
#include <strsafe.h>

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
static void shim_log(const char *fmt, ...)
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
    }
    return TRUE;
}
