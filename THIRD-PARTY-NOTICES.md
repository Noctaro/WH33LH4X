# Third-party notices

WH33LH4X is MIT licensed (see [LICENSE](LICENSE)). It uses the components below. This file
records what each one is, who holds copyright, and under what terms — everything a
distribution has to carry.

Compiled from the installed package metadata and the upstream license files, checked
2026-08-24 and updated 2026-09-26 for the raw USB bundle. Where a license text could not be
found upstream, this file says so rather than assuming.

**Not redistributed:** the vJoy kernel driver (`vJoy.sys`) is installed by the user from its
own project, and so is Zadig, which installs the WinUSB driver. Ruff is development-time only.
None of them is shipped.

## What the downloadable bundle actually contains

Source alone carries no third-party binaries. The bundle built by
`packaging/build_bundle.ps1` does, so this is the list that matters for redistribution:

| Binary | Origin | Terms |
|---|---|---|
| `python\*` (embeddable CPython 3.11.9) | python.org | PSF; ships its own `LICENSE.txt` |
| `lib\libusb_package\libusb-1.0.dll` | libusb 1.0.30, unmodified, inside the `libusb-package` wheel | **LGPL-2.1-or-later**; text in `LICENSES\libusb-LGPL-2.1.txt`, see below |
| `lib\pyvjoy\lib\x64\vJoyInterface.dll` (and `x86`) | inside the `pyvjoyffb` wheel | MIT, (c) 2017 Shaul Eizikovich |
| `python\_tkinter.pyd`, `python\tcl86t.dll`, `python\tk86t.dll`, `lib\tkinter\*`, `lib\tcl8.6\*`, `lib\tk8.6\*` | CPython 3.11.9 **full Windows install** | PSF for the Python parts; Tcl/Tk is BSD-style, text in `python\LICENSE.txt` |

**libusb is the one LGPL component**, and the only one with an obligation beyond passing on a
notice. The bundle carries the DLL exactly as the `libusb-package` wheel ships it, built from
libusb's tagged release [v1.0.30](https://github.com/libusb/libusb/releases/tag/v1.0.30), which
is where its source is. It is loaded at runtime from its own file, so it can be replaced with
any compatible build of `libusb-1.0.dll` by overwriting that file. The licence text is
`LICENSES/libusb-LGPL-2.1.txt`, copied verbatim from that tag, because the wheel does not carry
it.

**Tcl/Tk is the one row that does not come from a downloaded artifact.** The embeddable
distribution ships no tkinter at all, so `packaging/build_bundle.ps1` copies those six paths out
of the build machine's full CPython install — the same 3.11.9 release, which the build enforces
rather than assumes. The licence obligation is already met by a file the bundle carries anyway:
CPython's `LICENSE.txt` reproduces the original Tcl/Tk terms in its incorporated-software
section. Worth stating explicitly, because "it is covered by the Python licence that is already
in the folder" is true here but is not a safe reflex in general.

---

## MIT-licensed components

| Component | Copyright | Source |
|---|---|---|
| vJoy / `vJoyInterface.dll` | Copyright (c) 2017 Shaul Eizikovich | [shauleiz/vJoy](https://github.com/shauleiz/vJoy) |
| vJoy 2.2.x forks (effect block index, Win11 signing) | as above, per fork | [njz3/vJoy](https://github.com/njz3/vJoy), [BrunnerInnovation/vJoy](https://github.com/BrunnerInnovation/vJoy) |
| sv-ttk (Sun Valley ttk theme) | Copyright (c) rdbende | [rdbende/Sun-Valley-ttk-theme](https://github.com/rdbende/Sun-Valley-ttk-theme) |
| pyvjoyffb | Yannick Richter — **see the note below** | [Ultrawipf/pyvjoy](https://github.com/Ultrawipf/pyvjoy) |

The MIT License text, which applies to each of the above:

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Open question: pyvjoyffb carries no license text

`pyvjoyffb` 0.4 declares `License :: OSI Approved :: MIT License` as a PyPI trove classifier,
and that is the **only** licensing statement covering the code we use. Traced 2026-08-24:

```
tidzo/pyvjoy            no license file, no licence statement in the README.
                        Last push 2021-09-17, 16 open issues, not archived.
  |
  +-- willwade/pyvjoy   forked 2021-09-17, MIT added the next day
  |                     ("Copyright (c) 2021 Will Wade"). A SIBLING fork.
  |
  +-- Ultrawipf/pyvjoy  forked 2024-10-13 from tidzo, not from willwade.
                        GitHub reports license: null. Published as pyvjoyffb.
```

Will Wade's MIT file does not reach us — `pyvjoyffb` descends from `tidzo`, not from his fork.
And it does not resolve the root either: a fork author can license their own contributions but
cannot relicense the code they inherited, and `tidzo` never licensed the original.

So the position is: the whole lineage clearly *intends* MIT, and no copyright notice exists to
carry. Using the package is fine. **Redistributing it inside a binary release is the part that
needs resolving**, because MIT's one obligation is to pass along a notice that was never
written.

Three ways out, in the order they should be tried:

1. **Ask upstream.** A `LICENSE` file is a one-file pull request — willwade's fork is the
   working template. Ultrawipf is active (last push 2025-03); tidzo has been dormant since
   2021, so the root may not be reachable.
2. **Do not bundle it.** Install `pyvjoyffb` from PyPI on first run rather than vendoring it.
   Nothing is redistributed, so the obligation never arises. Costs an offline install.
3. **Bind `vJoyInterface.dll` directly.** That DLL is properly MIT with a real copyright line
   (Shaul Eizikovich, 2017), it already does the HID PID decoding through its own `Ffb_h_*`
   exports, and this repo uses roughly a dozen of its entry points. `dinput_abi.py` and
   `gameinput_abi.py` are the same kind of hand-transcribed binding. This removes the only
   unclear dependency and the `pyvjoyffb==0.4` pin hazard at the same time.

---

## PSF-licensed components

| Component | License | Source |
|---|---|---|
| CPython | PSF License Agreement | [python.org](https://www.python.org/) |

CPython's embeddable distribution contains `LICENSE.txt`, and a binary release carries it as it
is, unmodified.

---

## Other permissive components

Each ships its full licence text inside its own `.dist-info` folder in `lib\`, and the bundle
carries those folders unmodified.

| Component | License | Copyright | Source |
|---|---|---|---|
| pyusb | BSD-3-Clause | Copyright 2009–2017 Wander Lairson Costa; Copyright 2009–2021 PyUSB contributors | [pyusb/pyusb](https://github.com/pyusb/pyusb) |
| libusb-package (the Python wrapper around the DLL) | Apache-2.0 | its authors, per the wheel | [pyocd/libusb-package](https://github.com/pyocd/libusb-package) |
| importlib_resources | Apache-2.0 | its authors, per the wheel | [python/importlib_resources](https://github.com/python/importlib_resources) |

---

## Build- and development-time only

Not part of any distribution, listed because the build depends on them.

| Component | License | Source |
|---|---|---|
| Ruff | MIT | [astral-sh/ruff](https://github.com/astral-sh/ruff) |

## Trademarks

HORI and the wheel's product names are trademarks of their respective owners. No HORI code,
headers or assets are used in this project, and nothing here implies endorsement.
