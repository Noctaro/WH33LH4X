# vJoy setup

On Windows, games see your wheel as a vJoy device. Set it up once. The vJoy row in the window
shows what is still missing.

## 1. Install vJoy 2.2.2.0

Install **[vJoy 2.2.2.0 from BrunnerInnovation](https://github.com/BrunnerInnovation/vJoy)**.
Use exactly this version: older ones mix up effects that play at the same time.

## 2. Set up device 1

Open **Configure vJoy**, pick device **1** and set:

| Section | Setting |
|---|---|
| **Axes** | Tick **X, Y, Z, Rx, Ry**: steering, throttle, brake, clutch, handbrake |
| **Number of Buttons** | **10** |
| **Force Feedback** | Tick **Enable Effects** and keep every effect ticked |
| Bottom of the window | Tick **Enable vJoy**, then press **Apply** |

## Things to take care of

- Use the axes **X, Y, Z, Rx, Ry**, even though the list also offers Steering, Throttle, Brake
  and Clutch.
- Tick **Enable Effects** even when steering and pedals already work. Without it the wheel has
  no force.
- Check the setup with the test below. Windows shows force feedback for every vJoy device as
  soon as one has it, so its controller settings prove nothing.

## 3. Check it

In the WH33LH4X folder, run:

```powershell
.\python\python.exe vjoy_ffb_spike.py
```

It sends test effects through vJoy, without the wheel or a game. It must print `PASS`. If it
does not, go back to step 2.
