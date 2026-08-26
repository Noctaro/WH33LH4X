"""
Host the shim outside a game, so its WGI layer can be exercised without launching one.

WHY THIS IS NOT JUST test_proxy.py

`test_proxy.py` checks the proxy is transparent and exits in under a second -- which kills the
worker thread long before it has finished enumerating. And a bare console process is the wrong
shape anyway: Windows.Gaming.Input populates its device lists in response to arrival
notifications dispatched through a message loop, and only for a foregrounded process. A console
has no window of its own (the console window belongs to conhost.exe), so enumeration stays
empty forever and it looks exactly like absent hardware.

So this does what a game does: opens a real window, foregrounds it, pumps its messages, and
stays alive. Reusing `PumpThread` from wgi_probe.py rather than writing a second one, because
that class already encodes the traps -- SW_SHOW rather than SW_SHOWNOACTIVATE, and reporting
honestly when Windows refuses to give us the foreground.

This is a diagnostic harness. Passing here is necessary, not sufficient: the real bar is the
same code running inside a game.

Usage:
    .\\.venv\\Scripts\\python.exe shim\\run_shim.py            # 15 s, enumeration only
    .\\.venv\\Scripts\\python.exe shim\\run_shim.py --seconds 30
"""

import argparse
import ctypes
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wgi_probe import PumpThread, rule  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY = os.path.join(REPO, "shim", "build", "dinput8.dll")
LOG = os.path.join(tempfile.gettempdir(), "wh33lh4x_shim.log")
CFG = os.path.join(tempfile.gettempdir(), "wh33lh4x.cfg")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=15.0,
                    help="how long to keep the host alive (default 15)")
    ap.add_argument("--keep-log", action="store_true",
                    help="do not clear the shim log first")
    ap.add_argument("--winrt-assist", action="store_true",
                    help="also subscribe from Python, to test whether the C needs to at all")
    ap.add_argument("--drive", action="store_true",
                    help="act as the bridge too: publish a force sweep over the section")
    args = ap.parse_args()

    if not os.path.exists(PROXY):
        print("Proxy not built. Run:  .\\shim\\build.ps1")
        return 2

    selftest = "selftest=1" in open(CFG).read() if os.path.exists(CFG) else False
    rule("Hosting the shim")
    print("  proxy:    %s" % PROXY)
    print("  config:   %s  (selftest=%s)" % (CFG, "1" if selftest else "0"))
    if selftest:
        print()
        print("  SELF-TEST IS ON -- the shim will take the motor and drive it.")
        print("  HANDS ON THE WHEEL.")
    print()

    if not args.keep_log and os.path.exists(LOG):
        try:
            os.remove(LOG)
        except OSError:
            pass

    pump = PumpThread()
    pump.start()
    pump.ready.wait(5.0)

    if args.winrt_assist:
        # Diagnostic, not a feature. If the C's polling only finds the wheel once PYTHON has
        # subscribed in the same process, then device arrival really is gated on a
        # subscription and the C one has to be made to work. If the wheel shows up either
        # way, the C's failing add_RacingWheelAdded is a red herring and the real obstacle is
        # somewhere else. Either answer is worth more than another guess at the delegate.
        from wgi_probe import wait_for_devices
        print("  --winrt-assist: subscribing from Python first ...")
        wait_for_devices(10.0, pump)
        print("  Python enumeration done; the C worker polls next.")

    # Loading the DLL is not enough -- the worker starts on the first DirectInput8Create,
    # deliberately, because starting threads from DllMain risks the loader lock.
    proxy = ctypes.WinDLL(PROXY)
    import dinput_abi as di
    dinput = di.create_direct_input(PROXY)
    print("  DirectInput8Create through the proxy: ok (worker started)")
    print("  holding the window foreground for %.0f s ..." % args.seconds)

    sink = None
    if args.drive:
        # Exercise the full IPC path without a game: this process publishes force exactly the
        # way vjoy_bridge.py will, and the shim's worker -- in this same process, but reaching
        # the motor through WGI -- follows it. If the wheel moves here, the only thing left
        # between this and a game is which process the shim is loaded into.
        from motor_sink import IpcMotorSink
        sink = IpcMotorSink(max_force=1.0, gain=1.0).open()
        print("  --drive: publishing a force sweep over the shared section")
        print("  HANDS ON THE WHEEL.")

    steps = [(0.0, "rest"), (0.35, "right 35%"), (1.0, "right FULL"),
             (-0.35, "left 35%"), (-1.0, "left FULL"), (0.0, "rest")]
    deadline = time.time() + args.seconds
    step_len = args.seconds / len(steps) if args.drive else args.seconds
    index = -1
    while time.time() < deadline:
        pump.ensure_foreground()
        if sink is not None:
            elapsed = args.seconds - (deadline - time.time())
            which = min(int(elapsed / step_len), len(steps) - 1)
            if which != index:
                index = which
                print("    %-11s x = %+.2f      %s"
                      % (steps[which][1], steps[which][0], sink.describe()))
            sink.set_force(steps[which][0])
            time.sleep(0.01)          # ~100 Hz, the rate the real bridge runs at
        else:
            time.sleep(0.25)

    if sink is not None:
        sink.close()
    dinput.release()
    del proxy
    pump.stop()

    rule("Shim log")
    if os.path.exists(LOG):
        with open(LOG, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                print("  " + line.rstrip())
    else:
        print("  no log written at %s" % LOG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
