# Third-party notices

WH33LH4X is MIT licensed (see [LICENSE](LICENSE)). It uses the components below. This file
records what each one is, who holds copyright, and under what terms — everything a
distribution has to carry.

Compiled from the installed package metadata and the upstream license files, checked
2026-08-24. Where a license text could not be found upstream, this file says so rather than
assuming.

**Not redistributed:** the vJoy kernel driver (`vJoy.sys`) is installed by the user from its
own project. Zig and Ruff are build- and development-time only. Neither is shipped.

---

## MIT-licensed components

| Component | Copyright | Source |
|---|---|---|
| vJoy / `vJoyInterface.dll` | Copyright (c) 2017 Shaul Eizikovich | [shauleiz/vJoy](https://github.com/shauleiz/vJoy) |
| vJoy 2.2.x forks (effect block index, Win11 signing) | as above, per fork | [njz3/vJoy](https://github.com/njz3/vJoy), [BrunnerInnovation/vJoy](https://github.com/BrunnerInnovation/vJoy) |
| PyWinRT (`winrt-runtime`, `winrt-Windows.*`) | Copyright (c) Microsoft Corporation. All rights reserved.<br>Copyright (c) 2021-2025 David Lechner \<david@pybricks.com\> | [pywinrt/pywinrt](https://github.com/pywinrt/pywinrt) |
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
| `typing_extensions` | PSF-2.0 | [python/typing_extensions](https://github.com/python/typing_extensions) |

Both ship their full license text in their own distributions — CPython's embeddable
distribution contains `LICENSE.txt`, and `typing_extensions` ships its license inside the
wheel. A binary release carries those files as they are, unmodified.

---

## Build- and development-time only

Not part of any distribution, listed because the build depends on them.

| Component | License | Source |
|---|---|---|
| Zig | MIT | [ziglang.org](https://ziglang.org/) |
| Ruff | MIT | [astral-sh/ruff](https://github.com/astral-sh/ruff) |
| Windows SDK WinRT headers | Microsoft SDK licence; build input only | Windows SDK |

## Trademarks

HORI and the wheel's product names are trademarks of their respective owners. No HORI code,
headers or assets are used in this project, and nothing here implies endorsement.
