# Security and trust

## What this tool does to your system

- **On Windows it replaces the wheel's driver with WinUSB**, once, when you bind it with Zadig.
  While WinUSB is bound, Xbox games and the HORI app no longer see the wheel. *Restore
  Microsoft driver* in the window removes the WinUSB package again, and only ever a package
  Zadig installed.
- **On Linux the setup script adds a udev rule and a `wh33lh4x` group**, and loads `uinput` at
  boot. The rule gives your user access to the wheel and to `/dev/uinput` and keeps the wheel
  awake. `sudo sh wh33lh4x-setup.sh --remove` undoes all of it.
- **It runs readable Python.** The bridge and the window are plain source. Open any file in the
  download and read it.
- **It touches no game.** Nothing is copied into a game folder or loaded into a game process.

## Why nothing is signed

Certificates cost money this project does not spend, and since 2024 even an EV certificate no
longer gets past SmartScreen.

What you get instead:

- Releases are built by **GitHub Actions from public source**, not on somebody's desktop.
- Each release carries **SHA-256 checksums** (`SHA256SUMS.txt`) and a **GitHub artifact
  attestation**, so you can confirm the zip you downloaded is the one CI built from a specific
  commit:

  ```powershell
  gh attestation verify WH33LH4X-windows-x64.zip --repo Noctaro/WH33LH4X
  ```

- The Linux files carry their own checksums, `SHA256SUMS-linux.txt`:

  ```
  sha256sum -c SHA256SUMS-linux.txt
  ```

- The bundled `python.exe` is **Microsoft's own**, taken unmodified from the official
  embeddable distribution. It is signed by Microsoft even though our code is not.

- You can **build the whole thing yourself** and compare. See
  [docs/development.md](docs/development.md).

## If a scanner flags it

Report it to Microsoft at
[microsoft.com/wdsi/filesubmission](https://www.microsoft.com/en-us/wdsi/filesubmission) as
*Software developer, false positive*, and open an issue here.

## Anti-cheat

Games with kernel anti-cheat, such as EasyAntiCheat or BattlEye, are untested. Nothing is
loaded into the game, but vJoy is a kernel driver.

## Reporting a vulnerability

This is a hobby project with no security team and no bounty. If you find something, open a
GitHub issue. If you would rather not do that in public, say so in an issue with no detail and
we will find another channel.
