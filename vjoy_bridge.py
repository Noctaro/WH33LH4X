"""
vjoy_bridge.py: present the wheel to DirectInput games through vJoy.

Both of the wheel's USB modes are dead ends for sims. PC mode is seen by DirectInput but its
collection is input only, so there is no force feedback to expose. Xbox mode has force
feedback, but only through Windows.Gaming.Input, which DirectInput cannot see. Sims read
wheels through DirectInput, so the wheel has no force feedback in any of them.

This closes the gap both ways: vJoy presents a virtual wheel that games see and send effects
to, and those effects are rendered on the real motor through the shim.

    real wheel -> shim (in the game) -> this bridge -> vJoy -> game
    game -> vJoy effects -> this bridge -> ffb_render -> shim -> motor

CONSTRAINT: keep the wheel in Xbox mode. Both the readings and the force depend on it, and
its invisibility to DirectInput means a game can only see the vJoy device.

CONSTRAINT: readings come from the shim once a game is running, not from WGI here. Both are
foreground gated. See docs/hardware.md#the-foreground-owns-the-motor.

Usage:
    python vjoy_bridge.py                 # feed vJoy device 1 at 100 Hz
    python vjoy_bridge.py --device 2      # a different vJoy device
    python vjoy_bridge.py --rate 250      # faster feed
    python vjoy_bridge.py --dry-run       # read and report, write nothing to vJoy
"""

import argparse
import asyncio
import os
import sys
import time

try:
    from winrt.runtime import init_apartment
except ImportError:
    init_apartment = None

import ffb_render as render
import probe_log as log
from live_tune import ButtonTuner, LiveTune
from bridge.device import RawUsbWheel
from motor_sink import IpcMotorSink, RateLimiter, WgiMotorSink

try:
    import pyvjoy
    import pyvjoy._sdk as sdk

    from bridge.vjoy import (
        AXIS_MAP, AXIS_NAMES, EffectDecoder, VJoyFeeder, bidirectional, report_directions,
    )
except ImportError:
    sys.exit("pyvjoyffb is not installed. Run:\n"
             "    .\\.venv\\Scripts\\python.exe -m pip install pyvjoyffb")

from wgi_probe import PumpThread, has_motor, report, rule, wait_for_devices


class WheelReader(object):
    """
    Reads the real wheel, and reports honestly when it cannot.

    CONSTRAINT: position reading is foreground gated, exactly like force output. Zeros while a
    game is in front mean vJoy's axes never move, the game cannot bind steering, and a game
    that never bound steering sends no force feedback either.

    So when a source is given, the IPC sink fed by the shim inside the game, readings come
    from there. WGI is only the fallback for running without a game.
    """

    def __init__(self, wheel, source=None):
        self.wheel = wheel
        self.source = source
        self.reads = 0
        self.failures = 0
        self.last = None
        self.from_shim = 0

    def read(self):
        if self.source is not None:
            reading = self.source.read()
            if reading is not None:
                self.reads += 1
                self.from_shim += 1
                self.last = reading
                return reading
            # Fall through to WGI: before the game loads the shim nothing is publishing.
        if self.wheel is None:
            self.failures += 1
            return None
        try:
            reading = self.wheel.get_current_reading()
        except Exception:
            self.failures += 1
            return None
        self.reads += 1
        self.last = reading
        return reading

    def capabilities(self):
        """What this wheel actually has, so unmapped axes can be skipped and reported."""
        caps = {}
        if self.wheel is None and hasattr(self.source, "CAPABILITIES"):
            # The raw USB sink reads the device itself, so it knows what is physically there.
            return dict(self.source.CAPABILITIES)
        if self.wheel is None:
            # Readings come from the shim, so the device was never seen here. Unknown rather
            # than False, so nothing is skipped on a guess.
            return {"clutch": None, "handbrake": None, "pattern_shifter": None,
                    "max_wheel_angle": None}
        for attr, probe in (("clutch", "has_clutch"),
                            ("handbrake", "has_handbrake"),
                            ("pattern_shifter", "has_pattern_shifter")):
            try:
                caps[attr] = bool(getattr(self.wheel, probe))
            except Exception:
                caps[attr] = None
        try:
            caps["max_wheel_angle"] = float(self.wheel.max_wheel_angle)
        except Exception:
            caps["max_wheel_angle"] = None
        return caps


SWEEP_CHOICES = [label for _a, _u, label, _c in AXIS_MAP] + ["buttons"]


def sweep_mode(feeder, target, rate, seconds):
    """
    Wiggle one vJoy control on its own, with no wheel involved. For BINDING.

    Games bind an axis by watching for movement, and they only watch while THEY have focus.
    But reading the real wheel needs OUR process to have focus. So binding a real axis is a
    catch-22: focus the game and the axis is frozen, focus us and the game is not looking.

    Synthetic motion breaks it: the game cannot tell where the movement came from, so the
    game can stay focused while binding. Nothing here touches the wheel, no detection and no
    WGI, which is why focus stops mattering.

    Bind first with this, then play with the real feeder.
    """
    print("  Sweeping %s on vJoy device %d at %.0f Hz." % (target, feeder.rid, rate))
    print("  Leave THE GAME focused and bind %s now."
          % ("a button" if target == "buttons" else "this axis"))
    print("  Ctrl+C when the binding is accepted.")
    print()

    entry = next((e for e in AXIS_MAP if e[2] == target), None)
    limiter = RateLimiter(rate)
    start = time.monotonic()
    end = start + seconds if seconds > 0 else None
    next_print = 0.0
    button = 1

    while end is None or time.monotonic() < end:
        elapsed = time.monotonic() - start
        if entry is not None:
            _attr, usage, label, conv = entry
            # A slow triangle: unambiguous direction, and it dwells at the extremes long
            # enough for a binding UI that samples occasionally to catch full deflection.
            phase = (elapsed * 0.5) % 2.0
            tri = phase if phase < 1.0 else 2.0 - phase          # 0..1..0
            raw = tri * 2.0 - 1.0 if conv is bidirectional else tri
            if feeder.device is not None:
                try:
                    feeder.device.set_axis(usage, conv(raw))
                    feeder.writes += 1
                except Exception:
                    feeder.failures += 1
            shown = raw
        else:
            # Buttons: one at a time, half a second each, so it is obvious which is which.
            index = int(elapsed * 2) % max(1, feeder.button_count)
            if feeder.device is not None:
                try:
                    feeder.device.set_button(button, 0)
                    button = index + 1
                    feeder.device.set_button(button, 1)
                    feeder.writes += 1
                except Exception:
                    feeder.failures += 1
            shown = float(index + 1)

        now = time.monotonic()
        if now >= next_print:
            print("    %s %+.2f   w=%d f=%d" % (target, shown, feeder.writes,
                                                feeder.failures), flush=True)
            next_print = now + 0.5
        limiter.wait()


def parse_args():
    p = argparse.ArgumentParser(
        description="Feed the real wheel's position into a vJoy device that games can see")
    p.add_argument("--device", type=int, default=1, help="vJoy device id (default 1)")
    p.add_argument("--sweep", choices=SWEEP_CHOICES, default=None,
                   help="BINDING HELPER: move one vJoy control synthetically, ignoring the "
                        "real wheel entirely, so a game can bind it while the GAME has "
                        "focus. Works around the catch-22 where the game only watches for "
                        "movement while focused but we can only read the wheel while WE "
                        "are. Bind with this, then play with the normal feeder.")
    p.add_argument("--sweep-seconds", type=float, default=0.0,
                   help="stop sweeping after N seconds (default: until Ctrl+C)")
    p.add_argument("--rate", type=float, default=100.0,
                   help="feed rate in Hz (default 100)")
    p.add_argument("--wait", type=float, default=30.0, help="detection timeout")
    p.add_argument("--dry-run", action="store_true",
                   help="read the wheel and report, but write nothing to vJoy")
    p.add_argument("--no-ffb", action="store_true",
                   help="input path only; never take the motor")
    p.add_argument("--sink", choices=("ipc", "wgi", "rawusb"), default="ipc",
                   help="where force goes. 'ipc' (default) publishes to the shim inside the "
                        "game, which is the only thing that works with a game running. 'wgi' "
                        "drives the motor from this process and only produces torque while "
                        "this window is in front. Diagnostics only. 'rawusb' owns the wheel "
                        "over USB with no shim and no foreground gate; needs the wheel bound "
                        "to WinUSB. See evidence/RAW_USB.md.")
    p.add_argument("--no-grab", action="store_true",
                   help="never take the foreground. Force output will be silent unless you "
                        "click the probe window yourself, but nothing steals your keyboard.")
    p.add_argument("--run-seconds", type=float, default=0.0,
                   help="stop automatically after N seconds (default: run until Ctrl+C). "
                        "Useful because an effect playing takes the foreground, which is "
                        "also where your Ctrl+C would have gone.")
    p.add_argument("--gain", type=float, default=0.5,
                   help="motor master gain 0.0-1.0 (default 0.5). Latched when the effect "
                        "is loaded, so changing it needs a reload.")
    p.add_argument("--max-force", type=float, default=0.6,
                   help="STARTING cap on commanded force 0.0-1.0 (default 0.6). Multiplies "
                        "with --gain, so the default is about 30%% of what the wheel can do. "
                        "Unlike --gain this one is live: it seeds tune.json's max_force, and "
                        "the file wins from then on.")
    p.add_argument("--tune", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "tune.json"),
                   help="live tuning file, re-read while running (default: tune.json next to "
                        "this script). Edit mid-corner; changes apply within half a second.")
    p.add_argument("--no-log", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    log_path = None if args.no_log else log.start(prefix="vjoy_bridge")

    if init_apartment is not None:
        try:
            init_apartment()
        except TypeError:
            init_apartment(0)

    rule("vJoy bridge")
    print("  Feeds the real wheel into vJoy so DirectInput games can see it, and renders")
    print("  the force those games send back onto the real motor.")
    print("  The wheel must be in Xbox mode.")
    if log_path:
        print("  Log: %s" % log_path)

    # Sweep mode never touches the wheel, so it needs no pump, no apartment and no
    # detection, and does not care who has the foreground.
    if args.sweep:
        feeder = None
        try:
            feeder = VJoyFeeder(args.device).open()
            rule("Binding helper: synthetic %s" % args.sweep)
            sweep_mode(feeder, args.sweep, args.rate, args.sweep_seconds)
            return 0
        except KeyboardInterrupt:
            print("\n  stopped.")
            return 0
        except RuntimeError as exc:
            print("\n  %s" % exc)
            return 1
        finally:
            if feeder is not None:
                feeder.close()
            if not args.no_log:
                log.stop()

    pump = PumpThread()
    pump.start()
    pump.ready.wait(timeout=5)

    feeder = None
    sink = None
    decoder = None
    loop = None
    try:
        # NOT conditional on --no-ffb. On this path the sink is also the READER's source: the
        # shim publishes wheel position through the same shared section it takes force from,
        # and reading it is the only way to see the wheel while a game holds the foreground.
        # Dropping the sink for --no-ffb silently took the input path down with it, which
        # looks exactly like "the game sees no input". Force is withheld further down, by not
        # registering the callback and never commanding anything.
        use_ipc = args.sink == "ipc" and not args.dry_run
        use_raw = args.sink == "rawusb"

        motor = label = wheel = None
        identity = {}
        if use_raw:
            # WinUSB hides the wheel from WGI entirely, so there is nothing to enumerate: the
            # sink is the reader's source and the motor both, as with the shim.
            sink = RawUsbWheel(max_force=1.0, gain=args.gain).open()
            identity = {"name": "HORI racing wheel, raw USB"}
        else:
            raw, wheels = wait_for_devices(args.wait, pump)
            if has_motor(raw, wheels):
                motor, label, wheel, identity = report(raw, wheels)
            elif not use_ipc:
                rule("RESULT")
                print("  No wheel found. Put it in Xbox mode (long-press PROFILE) and press")
                print("  one of its buttons while this runs.")
                return 1

        # Create the IPC sink BEFORE the reader, because it is also the reader's source: the
        # shim publishes wheel position through the same section it takes force from.
        if use_ipc:
            # CONSTRAINT: this limit is a guard against a bug, not the user facing ceiling.
            # tune.json's max_force is that, and must be the only one, or raising it in the
            # file does nothing against a lower clamp further down.
            sink = IpcMotorSink(max_force=1.0, gain=args.gain).open()

        if wheel is None and sink is None:
            rule("RESULT")
            print("  Found a device via %s but no RacingWheel to read position from." % label)
            return 1
        if wheel is None and use_raw:
            print("  Readings and force both go over raw USB.")
        elif wheel is None:
            # Not a failure with the IPC sink. Enumerating needs this window in front, so a
            # bridge started after the game never sees the wheel and does not need to: the
            # shim supplies both readings and force.
            print("  No wheel enumerated here. Readings will come from the shim.")
            print("  (Normal when the game is already running; it owns the foreground.)")

        reader = WheelReader(wheel, source=sink)
        caps = reader.capabilities()

        rule("Real wheel")
        print("  %s" % (identity.get("name") or "(unnamed)"))
        print("  max wheel angle : %s"
              % ("%.0f degrees" % caps["max_wheel_angle"] if caps["max_wheel_angle"]
                 else "unknown"))
        print("  clutch / handbrake / shifter : %s / %s / %s"
              % (caps["clutch"], caps["handbrake"], caps["pattern_shifter"]))

        feeder = VJoyFeeder(args.device, dry_run=args.dry_run).open()

        rule("vJoy device %d" % args.device)
        print("  axes mapped : %s"
              % ", ".join("%s->%s" % (lbl, nm) for _a, _u, lbl, _c in feeder.axes
                          for nm in [AXIS_NAMES[_u]]))
        print("  buttons     : %d" % feeder.button_count)
        skipped = [lbl for attr, _u, lbl, _c in AXIS_MAP if caps.get(attr) is False]
        if skipped:
            print("  not present on this wheel, left at rest: %s" % ", ".join(skipped))
        if args.dry_run:
            print("  Dry run: nothing is being written to vJoy.")

        # --- force feedback: render what the game sends onto the real motor ---
        if not args.no_ffb and not args.dry_run and (motor is not None or sink is not None):
            loop = None
            if sink is None:
                # 'wgi' sink: drive the motor from this process. Only produces torque while
                # OUR window is in front, so this is for diagnostics, not for playing.
                loop = asyncio.new_event_loop()
                sink = WgiMotorSink(motor, loop, max_force=1.0, gain=args.gain)
                sink.open()
            decoder = EffectDecoder(render.EffectMixer())
            device = feeder.device if feeder.device is not None else pyvjoy.VJoyDevice(
                args.device)
            device.ffb_register_callback(decoder.on_packet)
            sdk._vj.FfbStart(args.device)
            rule("Force feedback ACTIVE")
            print("  %s" % sink.describe())
            print("  Effects a DirectInput client sends to vJoy are rendered here and")
            print("  played on the real wheel. Everything is computed in software and")
            print("  summed into one command. See ffb_render.EffectMixer.")
            print()
            if use_raw:
                print("  Force goes straight to the wheel over USB. No shim, and no window")
                print("  has to be in front.")
                print()
            elif args.sink == "ipc":
                print("  Force goes to the shim inside the game, which calls WGI from there.")
                print("  This process never takes the foreground. The game keeps it, which")
                print("  is exactly what makes the motor reachable. Copy")
                print("  shim\\build\\dinput8.dll next to the game exe if you have not yet;")
                print("  until the shim reports 'driving' above, nothing reaches the wheel.")
                print()
            else:
                print("  !! WGI force output is foreground-gated, so while an effect is")
                print("     PLAYING this process takes the foreground and your keystrokes")
                print("     including Ctrl+C, go to the probe window, not here. It")
                print("     releases focus as soon as nothing is playing. Use --run-seconds")
                print("     for a self-terminating run, or --no-grab to keep your keyboard.")
                print("     That gate is why the shim exists; use --sink ipc with a game.")
        elif args.no_ffb:
            print("  Force feedback disabled (--no-ffb).")
        elif args.dry_run:
            print("  Force feedback off in dry-run mode.")
        elif motor is None:
            print("  No force-feedback motor found; input only.")

        tune = LiveTune(args.tune)
        tune.values["max_force"] = args.max_force
        if tune.write_default():
            print("  created %s" % args.tune)
        tune.poll(0.0)

        rule("Running: press Ctrl+C to stop")
        print("  Open joy.cpl and watch the vJoy device's axes track the real wheel.")
        print()
        print("  Live tuning: %s. Edit it while driving, changes apply within %.1fs."
              % (args.tune, tune.poll_seconds))
        print("  %s" % tune.summary())
        # Two multiplications sit between a full-scale effect and the motor, and only one of
        # them can be changed without a restart. Stating the product avoids an afternoon spent
        # tuning against a ceiling nobody remembered was there.
        print("  Peak torque = max_force %.2f x --gain %.2f = %.2f of the motor%s"
              % (tune["max_force"], args.gain, tune["max_force"] * args.gain,
                 "   (--gain is latched at load; restart to change it)"))
        print()

        limiter = RateLimiter(args.rate)
        tuner = ButtonTuner(tune)
        state = render.WheelState()
        raw_force = 0.0                 # stays 0 on the input-only path, which has no sink
        last_buttons = 0
        next_print = 0.0
        last_foreground = 0.0
        # The probe window earns its place only until the shim takes over. See below.
        probe_hidden = False
        stop_at = time.monotonic() + args.run_seconds if args.run_seconds > 0 else None
        while stop_at is None or time.monotonic() < stop_at:
            now = time.monotonic()
            # CONSTRAINT: stamp before anything that can skip this tick. A lapsed heartbeat
            # makes the shim release the motor, and re-claiming it repeatedly leaves it silent
            # while still reporting a running effect. Readings pause routinely, in menus and
            # loading screens.
            if sink is not None:
                sink.keepalive()
            reading = reader.read()
            if reading is not None:
                written = feeder.feed(reading, caps)
                state.update(reading.wheel, now)

                # Buttons only arrive over the IPC sink, so this is inert on other paths.
                buttons = getattr(reading, "buttons", 0)
                if buttons != last_buttons:
                    # Printing every new bitfield is the button discovery tool: press one
                    # and read off which bit moved.
                    bits = [str(i + 1) for i in range(32) if buttons & (1 << i)]
                    print("    buttons 0x%08X  bits %s"
                          % (buttons, ",".join(bits) if bits else "-"), flush=True)
                    log.event("wheel.buttons", raw=buttons, bits=",".join(bits))
                    last_buttons = buttons
                note = tuner.update(buttons)
                if note:
                    print("    tune: %s   [%s]" % (note, tune.summary()), flush=True)
                    log.event("tune.button", change=note, **tune.values)

                # `decoder`, not `sink`: with --no-ffb the sink exists so the wheel can still
                # be READ, but nothing decodes effects and nothing may command force.
                if decoder is not None:
                    # Apply the game's packets BEFORE computing, so a force command always
                    # reflects the most recent thing the game asked for rather than lagging
                    # it by a tick.
                    decoder.drain()
                    raw_force = decoder.mixer.force(now, state)
                    changed = tune.poll(now)
                    # Direction is decoded per packet, and DiRT 4 re-sends it every tick, so
                    # setting this makes a mode change audible in the wheel almost at once.
                    render.DIRECTION_MODE = tune["dir_mode"]
                    if changed:
                        # Worth a line each time: when someone reports how a change felt, this
                        # is the record of what the change actually was.
                        print("    tune: %s" % tune.summary(), flush=True)
                        log.event("tune.changed", keys=",".join(changed), **tune.values)
                    force = tune.apply(raw_force, state)
                    sink.set_force(force)

                    # CONSTRAINT: take the foreground only while an effect is playing, and
                    # only on the wgi sink. Grabbing unconditionally steals every keystroke
                    # including the Ctrl+C meant to stop it, and on the ipc sink it takes
                    # focus from the game the shim needs to be in front.
                    if (args.sink == "wgi" and not args.no_grab
                            and decoder.mixer.running_effects()
                            and now - last_foreground > 0.1):
                        pump.ensure_foreground()
                        last_foreground = now

                    # Once the shim is live nothing here touches WGI again, so the probe
                    # window is a second window for no reason. Hidden rather than destroyed,
                    # which keeps the message pump and avoids a teardown path.
                    if (args.sink == "ipc" and not probe_hidden and sink is not None
                            and sink.shim_state()[0]):
                        pump.hide()
                        probe_hidden = True

                if now >= next_print:
                    parts = " ".join("%s %+.2f" % (lbl, rawv) for lbl, rawv, _v in written)
                    extra = ""
                    if decoder is not None:
                        running = decoder.mixer.running_effects()
                        # Both numbers, always: "game asked for X, wheel got Y" is the whole
                        # diagnostic. One number alone cannot tell a game sending nothing from
                        # a tuning value eating everything.
                        extra = ("  game %+.2f -> out %+.2f  fx=%d  pkt=%d"
                                 % (raw_force, sink.last_force or 0.0, len(running),
                                    decoder.received))
                        if decoder.dropped:
                            extra += " DROPPED=%d" % decoder.dropped
                    # Write/failure counts belong on the live line, not only in the exit
                    # summary: a vJoy axis that is not enabled fails per call and silently,
                    # and a Ctrl+C or a hard kill never reaches the summary.
                    print("    %-52s %5.1f Hz w=%d f=%d%s"
                          % (parts, limiter.achieved_hz, feeder.writes, feeder.failures,
                             extra), flush=True)
                    # `game` and `pos` are what make a session log analysable after the fact:
                    # the sign relationship between wheel position and the game's force IS
                    # self-aligning torque, and it is the only objective test of whether
                    # centring works. See tune_report.py.
                    log.event("bridge.tick", hz=round(limiter.achieved_hz, 1),
                              writes=feeder.writes, failures=feeder.failures,
                              force=(sink.last_force or 0.0) if sink else 0.0,
                              game=raw_force if sink else 0.0,
                              pos=state.position, vel=state.velocity,
                              dirx=decoder.last_dir if decoder else 0,
                              dirmul=decoder.last_dir_x if decoder else 0.0,
                              effects=len(decoder.mixer.running_effects()) if decoder else 0,
                              **{lbl: rawv for lbl, rawv, _v in written})
                    next_print = now + 0.5
            limiter.wait()

    except KeyboardInterrupt:
        print("\n  stopped.")
        return 0
    except RuntimeError as exc:
        print("\n  %s" % exc)
        # The console this was printed to is hidden when the GUI is the one launching, so the
        # reason has to survive somewhere the window can read it back. One event, one field,
        # so reading it is a split rather than a parser.
        log.event("exit.fatal", reason=str(exc))
        return 1
    finally:
        # Order matters: silence the motor before anything else is torn down, so an error
        # on the way out cannot leave the wheel holding a force.
        if sink is not None:
            # Only when force was being commanded. With --no-ffb the sink is open purely to
            # read, and a single zero write would stamp the heartbeat, making the shim take
            # the motor for half a second on the way out.
            if decoder is not None:
                try:
                    sink.set_force(0.0)
                except Exception:
                    pass
            sink.close()
            print("  motor released: %d writes, %d failures" % (sink.writes, sink.failures))
        if decoder is not None:
            report_directions(decoder)
        if feeder is not None:
            print("  vJoy writes %d, failures %d" % (feeder.writes, feeder.failures))
            feeder.close()
        if loop is not None:
            loop.close()
        pump.stop()
        if not args.no_log:
            log.stop()


if __name__ == "__main__":
    sys.exit(main())
