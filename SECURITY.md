# Security and trust

## What this tool does to your system

- **It replaces the wheel's driver with WinUSB**, once, when you bind it with Zadig. That is what
  lets it talk to the wheel directly. While WinUSB is bound, Xbox games and the HORI app no
  longer see the wheel. *Restore Microsoft driver* in the window removes the WinUSB driver
  package again; it only ever removes a package Zadig installed.
- **It runs readable Python.** The bridge and the window are plain source, run by Microsoft's
  signed `python.exe`. Open any file in the download and read it.
- **It touches no game.** Nothing is copied into a game folder or loaded into a game process.
  Games see a vJoy virtual wheel, the same as any other vJoy feeder.

## Why nothing is signed

**That is a decision rather than an oversight.** Certificates cost money this project does not
spend, and since 2024 even an EV certificate no longer buys past SmartScreen, so it would
purchase very little.

What you get instead:

- Releases are built by **GitHub Actions from public source**, not on somebody's desktop.
- Each release carries **SHA-256 checksums** (`SHA256SUMS.txt`) and a **GitHub artifact
  attestation**, so you can confirm the zip you downloaded is the one CI built from a specific
  commit:

  ```powershell
  gh attestation verify WH33LH4X-windows-x64.zip --repo Noctaro/WH33LH4X
  ```

  Attestations are a public repository feature, so they exist only for public releases.

- The bundled `python.exe` is **Microsoft's own**, taken unmodified from the official
  embeddable distribution. It is signed by Microsoft even though our code is not.

- You can **build the whole thing yourself** and compare. See
  [docs/development.md](docs/development.md).

## If a scanner flags it

That is a false positive, and the response that actually fixes it for everyone is Microsoft's
portal at [microsoft.com/wdsi/filesubmission](https://www.microsoft.com/en-us/wdsi/filesubmission)
under *Software developer, false positive*. Telling us is useful too, but Microsoft is who can
clear it.

## Anti-cheat

Nothing is loaded into the game, which removes the main reason an anti-cheat objects. It has
still not been tried with a game that runs kernel anti-cheat such as EasyAntiCheat or BattlEye,
and vJoy itself is a kernel driver. Treat those games as untested. See [GAMES.md](GAMES.md).

## Reporting a vulnerability

This is a hobby project with no security team and no bounty. If you find something, open a
GitHub issue. If you would rather not do that in public, say so in an issue with no detail and
we will find another channel.
