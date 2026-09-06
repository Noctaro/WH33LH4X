# Security and trust

## What this tool does to your system

**It copies an unsigned `dinput8.dll` into your game's folder before the game starts, and
removes it when the game exits.**

That is the same mechanism malware uses to get its code running inside another program. It is
worth saying so plainly rather than hoping you do not notice, because if your antivirus
objects, it is not being stupid.

What differs is consent and visibility, not technique:

- **The DLL is built from source in this repo**, `shim/*.c`, by one PowerShell script. Nothing
  about it is hidden from you.
- **It is only there while you play.** `play.ps1` deploys it before launch and removes it on
  exit, including after a crash or Ctrl+C. If another tool's `dinput8.dll` is already present,
  and ReShade uses the same filename, it is moved aside and put back afterwards.
- **Everything else ships as readable Python.** Open any file in the download and read it.

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

**Do not use this with a game that has kernel anti-cheat** such as EasyAntiCheat or BattlEye.
A proxy DLL in the game folder is exactly the shape of thing they are built to stop, and being
honest about your intent is not a defence they accept. See [GAMES.md](GAMES.md).

## Reporting a vulnerability

This is a hobby project with no security team and no bounty. If you find something, open a
GitHub issue. If you would rather not do that in public, say so in an issue with no detail and
we will find another channel.
