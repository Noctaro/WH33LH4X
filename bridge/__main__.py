"""
python -m bridge: drive the wheel over raw USB and present it to games.

    python -m bridge                          # vJoy device 1, force at 250 Hz
    python -m bridge --frontend none          # no game: the tune file's spring alone
    python -m bridge --stop-file stop.request # stops when that file appears
"""

import argparse
import sys

import probe_log as log
from bridge.core import Bridge, NullFrontend
from bridge.device import RawUsbWheel
from live_tune import USER_TUNE, LiveTune, ensure_user_tune


def parse_args():
    p = argparse.ArgumentParser(prog="python -m bridge",
                                description="Drive the wheel over raw USB and present it to "
                                            "games through a virtual wheel")
    p.add_argument("--frontend", choices=("vjoy", "none"),
                   default="vjoy" if sys.platform == "win32" else "none",
                   help="what games see: 'vjoy' (default on Windows) or 'none', the tune "
                        "file's spring with no game (default elsewhere)")
    p.add_argument("--device", type=int, default=1, help="vJoy device id (default 1)")
    p.add_argument("--rate", type=float, default=250.0,
                   help="bridge loop rate in Hz (default 250, the wheel's write rate)")
    p.add_argument("--gain", type=float, default=1.0,
                   help="motor gain 0.0-1.0 after max_force (default 1.0); the raw USB "
                        "hard cap still applies")
    p.add_argument("--tune", default=USER_TUNE,
                   help="live tuning file, re-read while running (default: user-tune.json, "
                        "created from tune.json)")
    p.add_argument("--stop-file", metavar="FILE",
                   help="stop cleanly when this file appears, and delete it")
    p.add_argument("--run-seconds", type=float, default=0.0,
                   help="stop after N seconds (default: run until Ctrl+C)")
    p.add_argument("--no-ffb", action="store_true", help="input only; never command force")
    p.add_argument("--dry-run", action="store_true",
                   help="read the wheel and report; write nothing to vJoy, no force")
    p.add_argument("--trace", metavar="FILE",
                   help="write time, position and force per tick, like wheel_trace.txt")
    p.add_argument("--no-log", action="store_true")
    return p.parse_args()


def make_frontend(args):
    if args.frontend == "none":
        return NullFrontend()
    from bridge.vjoy import VJoyFrontend    # needs pyvjoyffb, so only when asked for
    return VJoyFrontend(args.device, dry_run=args.dry_run)


def write_trace(path, trace):
    with open(path, "w", encoding="ascii") as handle:
        for stamp, position, force in trace:
            handle.write("%.4f %.5f %.5f\n" % (stamp, position, force))
    print("  wrote %d trace sample(s) to %s" % (len(trace), path))


def main():
    args = parse_args()
    log_path = None if args.no_log else log.start(prefix="bridge")
    ffb = not (args.no_ffb or args.dry_run)
    device = frontend = bridge = None
    code = 0
    try:
        if log_path:
            print("  Log: %s" % log_path)
        device = RawUsbWheel(gain=args.gain)
        frontend = make_frontend(args)
        frontend.open(device.CAPABILITIES, ffb=ffb)     # a missing vJoy fails before arming
        device.open()

        if args.tune == USER_TUNE and ensure_user_tune():
            print("  created %s from tune.json" % args.tune)
        tune = LiveTune(args.tune)
        if tune.write_default():
            print("  created %s" % args.tune)
        tune.poll(0.0)

        bridge = Bridge(device, frontend, tune, rate=args.rate, ffb=ffb,
                        stop_file=args.stop_file, trace=[] if args.trace else None)
        print("  %s" % device.describe())
        for line in frontend.describe():
            print("  %s" % line)
        print("  force feedback %s" % ("on" if ffb else "off"))
        print("  Live tuning: %s, re-read every %.1f s" % (args.tune, tune.poll_seconds))
        print("  %s" % tune.summary())
        print("  Running: Ctrl+C%s to stop" % (" or the stop file" if args.stop_file else ""))
        bridge.run(args.run_seconds)
    except KeyboardInterrupt:
        print("\n  stopped.")
    except (RuntimeError, ImportError) as exc:
        print("\n  %s" % exc)
        log.event("exit.fatal", reason=str(exc))
        code = 1
    finally:
        if bridge is not None:
            bridge.close()
            if args.trace:
                write_trace(args.trace, bridge.trace)
        else:
            if device is not None:
                device.close()
            if frontend is not None:
                frontend.close()
        if log_path:
            log.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
