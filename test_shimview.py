r"""
test_shimview.py -- the GUI must WATCH the shared section, never create it.

WHY THIS TEST EXISTS

gui.ShimView originally attached with `mmap.mmap(-1, size, tagname=NAME, access=ACCESS_READ)`.
With fileno -1 that CREATES the mapping when none exists, and creates it PAGE_READONLY. Once
the GUI became the thing that starts the bridge, the GUI always got there first -- so the
section existed, read-only, before any writer opened it. Both writers then failed:

    vjoy_bridge : OSError [WinError 87] out of mmap.mmap, one second after starting
    shim        : ipc: MapViewOfFile failed, err=87   (logged from inside the game)

The game ran with no wheel input at all, and the status line still said "Bridge running".

Nothing caught it because both halves were individually correct: the viewer read the right
struct at the right name, and the bridge opened the same name the same way it always had. The
bug lived entirely in WHO TOUCHED IT FIRST, which no single-component test can see.

So this test asserts the ORDER that failed, not the parts.

NEVER RUN THIS DURING A LIVE SESSION -- and it now refuses to. Step 3 opens a real
IpcMotorSink against the real section name, and `IpcMotorSink.close()` deliberately zeroes the
force and the bridge heartbeat so the shim drops the motor at once. Against a running bridge
that is a force-feedback dropout mid-corner. The bridge re-stamps within one loop so it
recovers, but a test has no business touching a live session, so it checks first and bails.

No hardware, no wheel, no game. Run it from the repo root:
    .\.venv\Scripts\python.exe test_shimview.py
"""

import sys

import gui
from motor_sink import IpcMotorSink

PASS, FAIL = "  PASS ", "  FAIL "
failures = []


def check(name, got, want):
    ok = got == want
    print("%s%s" % (PASS if ok else FAIL, name), end="")
    print("" if ok else "   (got %r, wanted %r)" % (got, want))
    if not ok:
        failures.append(name)


def main():
    print("ShimView must observe, not create\n")

    # A live bridge owns this section, and step 3 would zero its heartbeat on close. Refuse
    # rather than interfere -- and rather than report failures that only mean "something else
    # is running", which is what the first two checks degrade into in that case.
    if gui.ShimView._snapshot() is not None:
        print("  SKIPPED -- the shared section already exists, so a bridge or a game is")
        print("  running. Stop it and run this again: the test opens a real sink, and")
        print("  closing one zeroes the bridge heartbeat.")
        return 0

    view = gui.ShimView()

    # 1. With nothing running, reading is safe and reports nothing.
    check("reads as 'nothing running' when no bridge exists",
          view.read(), (False, IpcMotorSink.STATE_NONE, False))

    # 2. THE ACTUAL BUG. Reading must not bring the section into existence -- if it does, it
    #    exists read-only and every writer afterwards fails with WinError 87.
    check("reading did not create the section",
          gui.ShimView._snapshot() is None, True)

    # 3. The order that broke it: viewer first, then the bridge. This raised before the fix.
    try:
        sink = IpcMotorSink(max_force=1.0, gain=1.0).open()
        opened = True
    except OSError as exc:
        opened = False
        print("        bridge open raised: %s" % exc)
    check("bridge can still open AFTER the viewer has read", opened, True)
    if not opened:
        return 1

    try:
        # 4. And the viewer sees the bridge it did not break.
        check("viewer reports the live bridge", view.read()[2], True)
    finally:
        sink.close()

    # 5. A closed bridge goes back to reporting nothing, rather than the viewer keeping a dead
    #    section alive because it held a handle open between reads.
    check("bridge reads as gone once closed", view.read()[2], False)

    print("")
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
