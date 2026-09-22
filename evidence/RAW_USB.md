# Raw USB — driving this wheel's motor without Microsoft

**The motor is driven over raw USB, with no Microsoft driver, no GIP authentication, no
foreground gate, and no capture file.** On Linux the wheel is a working device: it publishes a
virtual joystick anything can read, while we hold a centring spring of our own choosing on the
real motor.

That is the answer to the question this repo was opened for. The firmware's centring spring is
far too strong for delicate steering, and Windows only lets the foreground application touch the
motor. Both problems disappear here.

This reverses [`README.md`](README.md)'s long-standing "raw GIP is a dead end". The verdict was
correct about every attempt behind it and wrong about the conclusion drawn from them.

## Why years of replays were silent

`logs/wgi.txt` was captured at `\.\XboxGIP` — **above the driver, not on the wire.** The
`dc1-controller` driver does not pass that stream through. For one identical session it:

- reorders the arming,
- sends a `0x05` power message WGI never issues,
- uploads the effect table once where WGI asks twice,
- expands sparse force updates into full ten-slot blocks,
- and fills in sequence numbers, option bytes and timing that are simply absent above it.

So everything reconstructed from that file was a guess about what the driver would do next. A
USBPcap capture of the same session, parsed by `usbpcap_parse.py`, ended the guessing.

## A force command is a sequence, not a message

The second discovery, and the one that had wasted the most time. Around **every** force block:

```
0c .. 00    STATE_CLEAR            x2
0d .. 0000  effect table chunk 1
0d .. 0030  effect table chunk 2   the whole four-chunk table,
0d .. 0060  effect table chunk 3   re-uploaded before every force
0d .. 0090  effect table chunk 4
0b ..       the magnitude block
0c .. 10    state 0x10
0c .. f0    STATE_RUNNING resumes
```

We had been sending the `0b` alone, into a device that was still cleared. It does nothing, which
is exactly what "commanded 0.30, wheel did not move" looked like for four wrong guesses running.

Once an effect is loaded and running, a **bare `0b` block updates the magnitude.** Re-running the
full load to change a level tears the effect down and rebuilds it, which is felt as hard cogging.

## Authentication is real, and is not the gate

The wheel does speak a genuine `0x06` authentication exchange: an X.509 certificate chain
("Xbox Accessory", issuer "Saxony", valid to 2043) with a fresh random nonce per session, so a
replay cannot pass it. For a while that looked like the wall.

**It is not.** Force feedback works with no `0x06` exchange at all. Nothing here extracts or
circumvents a signing key, and nothing here needs to.

## The protocol is generated, not replayed

`gip_arming.py` builds the arming and force sequences from rules. Verified **150 of 150 packets
byte-identical** to the wire capture, headers and option bytes included.

The only captured artefact left is **186 bytes of effect table** (`TABLE_CHUNKS`), which the
firmware wants re-uploaded before each force command and which nobody needs to understand.

Two traps inside it, both self-inflicted and both expensive:

- **The option byte.** `POWER` carries `0x20` INTERNAL; everything else carries `0x00`. Sending
  `0x00` for POWER arms the device *silently*: no input stream, no torque, no error.
- **The heartbeat counts are load-bearing.** The arming's `CLEAR` runs are 1, 2, 26, 30, 29.
  Approximating them as "about two seconds of beats" also arms silently. They are not padding.

## Files

| File | What it is |
|---|---|
| `gip_arming.py` | Generates the arming and force sequences from rules. The protocol itself. |
| `gip_wheel_driver.py` | The usable driver: owns the wheel, publishes a joystick, holds the spring. |
| `gip_usb_host.py` | The instrument. Claiming, power-on, calibration waits, replay, force scaling, closed-loop position control. |
| `usbpcap_parse.py` | Parses USBPcap captures, which is how the driver-versus-wire difference was found. |

## Running it on Linux

```
sudo apt install -y python3-usb python3-evdev
sudo modprobe uinput
echo 'KERNEL=="uinput", MODE="0660", GROUP="plugdev"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload && sudo udevadm trigger
```

Then, with the wheel unbound from `xone`:

```
python3 gip_wheel_driver.py --spring 0.25 --damper 0 --cap 0.35 --refresh 0 \
        --min-force 0.05 --deadband 0.01 --release 0.05 --no-unstick
```

That produces `/dev/input/js0`, named "HORI Force Feedback Racing Wheel (raw USB)", carrying
steering, while the motor holds a light spring. Those values are the ones that felt right on the
real wheel; the defaults in the script are stiffer.

`--wait-for FILE` arms the wheel and then holds still until the file appears, so an operator can
be told to take hold at the exact moment instead of guessing when the calibration sweep ended.

## The control law, and four bugs found by measuring

The spring comes from `ffb_render` (it imports only `math`, so it runs on Linux unchanged).
Position is read back from the input stream at the same time as force is commanded, which is
everything a closed loop needs.

1. **A proportional spring parks off centre.** At a gain of 0.25 an offset of 0.08 demands 0.02,
   which is below what starts a stationary wheel, so it stops wherever the force ran out. Fixed
   with a minimum force outside the deadband.
2. **Floor only the centring force, never spring plus damper.** Flooring the sum let the damper
   win whenever the spring was small, and the damper's sign comes from a smoothed, lagging
   velocity. The floor then amplified force in the direction the wheel was already moving. That
   is positive feedback, and it was felt exactly as such: "it felt like accelerating my moves".
3. **A fixed floor outside a narrow deadband is bang-bang control.** It drives through centre,
   flips, drives back. Mitigated with settle hysteresis, and with coasting: stop pushing once the
   wheel is already travelling towards centre and let momentum finish.
4. **Never reload the effect to change a magnitude.** See above; `--refresh 0`.

Measured: static friction exceeds moving friction, so 0.05 moves a turning wheel but often will
not start a still one. Breakaway itself is below 0.01, so the floor is about starting friction,
not stiction.

## Not done

- **Coasting is implemented but untested.** The wheel still sweeps slightly around centre, and
  that is the change most likely to settle it.
- **The virtual joystick publishes steering only.** Pedals and buttons need their input offsets
  measured; guessing input layouts has already cost this project one retraction.
- **Windows is unretested against the wire-derived protocol.** The earlier WinUSB attempt used
  the bytes from `wgi.txt`, which we now know were the wrong bytes.

## Safety

The wheel is strong. Force is capped (0.35 by default) and zeroed when the driver exits. Do not
run it unattended.
