"""
Acceptance test for Track D phase D1: is the dinput8.dll proxy transparent?

The D1 bar is "a game starts normally with the proxy present and still sees its controllers".
Launching a game to find that out is slow and ambiguous -- if a game misbehaves
you learn nothing about *where*. So this drives the proxy directly and compares it against the
system DLL in the same process:

  1. all five exports resolve by their undecorated names
  2. DirectInput8Create through the proxy returns a usable IDirectInput8
  3. enumerating game controllers through the proxy yields EXACTLY the devices the system DLL
     yields -- same instances, same names, same force-feedback flags
  4. the proxy wrote its log, and recorded a real system DLL rather than falling back

Step 3 is the one that matters. A proxy that loads and returns S_OK but hands back a crippled
interface would pass a smoke test and fail in a game, and the failure would look like a game
bug. Comparing the enumeration against ground truth in the same process closes that gap.

Usage:
    .\\.venv\\Scripts\\python.exe shim\\test_proxy.py
    .\\.venv\\Scripts\\python.exe shim\\test_proxy.py --keep-log   # do not clear the log first
"""

import argparse
import ctypes
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dinput_abi as di  # noqa: E402
from dinput_abi import (  # noqa: E402
    DI8DEVCLASS_GAMECTRL,
    DIDC_FORCEFEEDBACK,
    DIEDFL_ATTACHEDONLY,
    DIENUM_CONTINUE,
    LPDIENUMDEVICESCALLBACKW,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY = os.path.join(REPO, "shim", "build", "dinput8.dll")
SYSTEM = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "dinput8.dll")
LOG = os.path.join(tempfile.gettempdir(), "wh33lh4x_shim.log")

EXPORTS = [
    "DirectInput8Create",
    "DllCanUnloadNow",
    "DllGetClassObject",
    "DllRegisterServer",
    "DllUnregisterServer",
]

_passed = 0
_failed = 0


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  PASS {label}" + (f"  ({detail})" if detail else ""))
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"  ({detail})" if detail else ""))
    return ok


def enumerate_through(dll_path):
    """Every attached game controller as (instance name, product name, has FFB)."""
    dinput = di.create_direct_input(dll_path)
    found = []

    @LPDIENUMDEVICESCALLBACKW
    def on_device(instance_ptr, _ref):
        inst = instance_ptr.contents
        copy = di.DIDEVICEINSTANCEW()
        ctypes.memmove(ctypes.byref(copy), ctypes.byref(inst), ctypes.sizeof(copy))
        found.append(copy)
        return DIENUM_CONTINUE

    dinput.enum_devices(DI8DEVCLASS_GAMECTRL, on_device, DIEDFL_ATTACHEDONLY)

    rows = []
    for inst in found:
        # Capabilities come from the device, not the instance record, so open each one. This
        # also exercises CreateDevice through the proxy, which the enumeration alone does not.
        dev = dinput.create_device(inst.guidInstance)
        try:
            caps = dev.get_capabilities()
            rows.append((inst.tszInstanceName, inst.tszProductName,
                         bool(caps.dwFlags & DIDC_FORCEFEEDBACK)))
        finally:
            dev.release()

    dinput.release()
    return sorted(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-log", action="store_true",
                    help="do not clear the shim log before running")
    args = ap.parse_args()

    print("D1 proxy transparency test")
    print(f"  proxy:  {PROXY}")
    print(f"  system: {SYSTEM}")
    print(f"  log:    {LOG}")
    print()

    if not os.path.exists(PROXY):
        print("Proxy not built. Run:  .\\shim\\build.ps1")
        return 2

    if not args.keep_log and os.path.exists(LOG):
        try:
            os.remove(LOG)
        except OSError as exc:
            print(f"  note: could not clear log ({exc}); results may include an older run")

    print("exports")
    proxy = ctypes.WinDLL(PROXY)
    for name in EXPORTS:
        try:
            addr = ctypes.cast(getattr(proxy, name), ctypes.c_void_p).value
            check(name, bool(addr), f"0x{addr:x}")
        except AttributeError:
            check(name, False, "not exported")
    print()

    print("enumeration matches the system DLL")
    try:
        via_system = enumerate_through(SYSTEM)
    except Exception as exc:
        check("system DLL enumerates", False, repr(exc))
        via_system = None
    else:
        check("system DLL enumerates", True, f"{len(via_system)} device(s)")

    try:
        via_proxy = enumerate_through(PROXY)
    except Exception as exc:
        check("proxy enumerates", False, repr(exc))
        via_proxy = None
    else:
        check("proxy enumerates", True, f"{len(via_proxy)} device(s)")

    if via_system is not None and via_proxy is not None:
        check("same devices through both", via_system == via_proxy)
        for name, product, ffb in via_proxy:
            print(f"       - {name!r} / {product!r}  ffb={ffb}")
        if via_system != via_proxy:
            print("    system:", via_system)
            print("    proxy: ", via_proxy)
    print()

    print("shim log")
    if check("log written", os.path.exists(LOG)):
        with open(LOG, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for line in text.splitlines():
            print(f"       {line}")
        check("resolved the real system DLL", "loaded " in text and "FATAL" not in text)
        check("forwarded DirectInput8Create", "DirectInput8Create(version=" in text)
    print()

    print(f"{_passed} passed, {_failed} failed")
    if _failed:
        print("\nD1 NOT met.")
        return 1
    print("\nD1 met at the API level: the proxy is transparent to DirectInput.")
    print("Remaining D1 evidence is a real game -- copy shim/build/dinput8.dll by its exe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
