r"""
gen_commands.py: generate COMMANDS.md from the actual source.

Flags and defaults are read out of the argparse calls and the PowerShell param() blocks,
so the reference cannot claim a flag the code does not parse. The prose around them is
written here; only the tables are derived.

Usage, from anywhere:
    .\.venv\Scripts\python.exe packaging\gen_commands.py
    .\.venv\Scripts\python.exe packaging\gen_commands.py --check   # CI: fail if stale
"""
import argparse
import ast
import glob
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)   # every glob below, and the output path, are repo-root relative

_args = argparse.ArgumentParser(description="Generate COMMANDS.md from the source.")
_args.add_argument("--check", action="store_true",
                   help="do not write; exit 1 if COMMANDS.md is out of date")
ARGS = _args.parse_args()

BS = chr(92)

# ---- extract argparse flags -------------------------------------------------------------
flags = {}
for f in (sorted(glob.glob('*.py')) + sorted(glob.glob('bridge/*.py'))
          + sorted(glob.glob('evidence/*.py'))):
    tree = ast.parse(open(f, encoding='utf-8').read())
    rows = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'add_argument'):
            names = [a.value for a in node.args if isinstance(a, ast.Constant)]
            kw = {k.arg: k.value for k in node.keywords}

            def const(k, kw=kw):
                v = kw.get(k)
                return v.value if isinstance(v, ast.Constant) else None

            choices = kw.get('choices')
            ch = None
            if isinstance(choices, (ast.List, ast.Tuple)):
                ch = [e.value for e in choices.elts if isinstance(e, ast.Constant)]
            rows.append({
                'names': names,
                'help': ' '.join((const('help') or '').split()),
                'default': const('default'),
                'choices': ch,
                'is_flag': const('action') in ('store_true', 'store_false'),
            })
    if rows:
        flags[f.replace(BS, '/')] = rows

# ---- extract PowerShell param() blocks ---------------------------------------------------
ps_params = {}
for f in ['packaging/build_bundle.ps1']:
    try:
        src = io.open(f, encoding='utf-8').read()
    except OSError:
        continue
    m = re.search(r'^param\((.*?)^\)', src, re.S | re.M)
    rows = []
    if m:
        for line in m.group(1).splitlines():
            pm = re.match(r'\s*\[(\w+)\]\s*\$(\w+)\s*(?:=\s*(.+?))?\s*,?\s*$', line)
            if pm:
                rows.append({'type': pm.group(1), 'name': pm.group(2),
                             'default': (pm.group(3) or '').strip()})
    ps_params[f] = rows


def flag_table(path):
    rows = flags.get(path, [])
    if not rows:
        return '_No flags._\n'
    out = ['| Flag | Default | What it does |', '|---|---|---|']
    for r in rows:
        name = ' '.join('`%s`' % n for n in r['names'])
        if r['choices']:
            name += ' ' + '/'.join('`%s`' % c for c in r['choices'])
        if r['is_flag'] or r['default'] is None:
            dflt = '—'
        else:
            dflt = '`%s`' % r['default']
        help_text = r['help'].replace('|', '\\|') or '—'
        out.append('| %s | %s | %s |' % (name, dflt, help_text))
    return '\n'.join(out) + '\n'


# Condensed from the explanatory comments already sitting above each param in the source.
PS_HELP = {
    'OutDir': 'Where to build. Defaults to `dist/`.',
    'CacheDir': 'Where the embeddable Python zip is cached between runs.',
    'SkipZip': 'Leave the staged folder, skip creating the archive.',
}


def ps_table(path):
    rows = ps_params.get(path, [])
    if not rows:
        return '_No parameters._\n'
    out = ['| Parameter | Type | Default | What it does |', '|---|---|---|---|']
    for r in rows:
        out.append('| `-%s` | %s | %s | %s |' % (
            r['name'], r['type'],
            '`%s`' % r['default'] if r['default'] else '—',
            PS_HELP.get(r['name'], '—')))
    return '\n'.join(out) + '\n'


BUNDLED = 'Ships in the downloadable bundle.'
REPO = 'Repo only — not in the bundle.'

TUNE_REPORT_ARGS = (
    '| Argument | Default | What it does |' + chr(10) +
    '|---|---|---|' + chr(10) +
    '| _(positional)_ | newest log in `logs/` | Path to the bridge session log to analyse |'
    + chr(10))

doc = []
w = doc.append

w("""# Command reference

Every runnable script in this project, what it is for, and every flag it takes. Generated from
the source, so the flags and defaults here are the ones the code actually parses.

Ordered by what you are trying to do rather than alphabetically. If you only ever read one
section, read the first.

**In the downloadable bundle**, use the two launchers, or the bundled interpreter, which needs
no Python installed:

```
WH33LH4X-GUI.cmd
.\\python\\python.exe vjoy_ffb_spike.py
```

**In a source checkout**, use the venv:

```
.\\.venv\\Scripts\\pythonw.exe -m ui
.\\.venv\\Scripts\\python.exe vjoy_ffb_spike.py
```

Force feedback tuning is not a flag. It lives in `user-tune.json`, created from `tune.json` the
first time, and is re-read within half a second of a save, so it changes mid-corner without
restarting anything — see [docs/tuning.md](docs/tuning.md).

---

## 1. Driving

### `WH33LH4X-GUI.cmd` / `python -m ui` — the window

%s Pick a profile, press Start, drive. Start and Stop run the bridge below as a hidden process,
and closing the window stops it too.

- **Profiles**: Default, DiRT 4 and RC car, chosen by hand. The sliders apply live; a profile
  file only changes on Save or Save as new.
- **Setup checks** for vJoy device 1 and the wheel's WinUSB binding, each with a How to fix.
  On Linux it checks that xone has let go of the wheel and that USB autosuspend is off.
- **Test force** pushes the wheel briefly right and left and reports whether it moved the
  right way.
- **Restore Microsoft driver** removes the WinUSB driver so Xbox games and the HORI app see the
  wheel again.

Takes no arguments. Runs under `pythonw.exe` so no console sits behind it.

### `WH33LH4X.cmd` / `python -m bridge` — the bridge without the window

%s Owns the wheel over raw USB (WinUSB on Windows, xone unbound on Linux) and presents it
through vJoy. For running headless or with flags the window does not expose; `WH33LH4X.cmd`
passes its arguments through.

%s
### `python -m bridge.nudge` — test force

%s Arms the wheel, pushes it briefly right and then left with a spring in between, and prints
`nudge ok right=+0.2 left=-0.2`, or `reversed`, `still`, `unclear` or `silent` (no input).
Hands off the wheel. The window's Test force button runs this. Takes no flags.

### `tune_report.py` — say what the force feedback actually did

%s Reads a bridge session log and reports which effects the game sent, whether output ever
went negative, and the correlation between steering angle and force. That last number is the
objective test for centring: a wheel whose output never changes sign cannot centre, however it
felt.

%s
---

## 2. Checking the setup

### `vjoy_ffb_spike.py` — does the vJoy force-feedback path work?

%s **Run this first when a game gets no force feedback.** It sends DirectInput effects to the
virtual device and logs what comes back out of vJoy's callback, verifying the whole path
without a game or the real wheel. If it does not report `PASS`, nothing built on top of vJoy
will work.

%s
---

## 3. Building

### `packaging/build_bundle.ps1` — assemble the downloadable bundle

%s Downloads and verifies embeddable CPython, vendors the pinned dependencies, stages the
source, writes the two launchers, and zips the result.

%s
---

## 4. Modules with no command line

Imported by the commands above; nothing to run.

| Module | Purpose | In bundle |
|---|---|---|
| `bridge/` | The bridge loop, the wheel over raw USB, the vJoy front-end | yes |
| `gip/` | The GIP protocol: framing, arming, USB host, input reports | yes |
| `ui/` | The window: profiles, the bridge process, setup checks | yes |
| `ffb_render.py` | The force-feedback control laws, in normalised units | yes |
| `live_tune.py` | Tuning file reloading and the wheel-button tuning controls | yes |
| `probe_log.py` | Session logging to `logs/` | yes |
| `dinput_abi.py` | ctypes transcription of the DirectInput 8 API surface | yes |
| `evidence/gameinput_abi.py` | ctypes transcription of the GameInput API (v0 ABI) | no |
| `evidence/gip_protocol.py` | The GIP wire format as captured above the driver | no |

---

## 5. Tests

None of these need hardware, and CI runs all four.

### `test_ffb_render.py` — the control laws still do what they used to

Run before proposing a change to `ffb_render.py` or `live_tune.py`. No test framework, no
dependencies: `.\\.venv\\Scripts\\python.exe test_ffb_render.py`. Takes no flags.

### `test_gip.py` — the same bytes still go on the wire

Run before proposing a change to `gip/`. Pins the arming and force bytes that drove the motor,
and the input report decoding. No pyusb needed: `.\\.venv\\Scripts\\python.exe test_gip.py`.

### `test_bridge_core.py` — the bridge loop against a fake wheel

Run before proposing a change to `bridge/`. Force sign, the stop file, and zero force before
the wheel is released: `.\\.venv\\Scripts\\python.exe test_bridge_core.py`.

### `test_ui.py` — profiles and the bridge process, without a window

Run before proposing a change to `ui/` or `profiles/`:
`.\\.venv\\Scripts\\python.exe test_ui.py`.

---

## 6. Evidence

None of these ship in the bundle. They live in [`evidence/`](evidence/README.md): the dead ends
that prove why the other APIs cannot drive this wheel, and the raw USB instruments the working
route was found with. Run the Windows ones as modules from the repo root, since they import
from it:

```
.\\.venv\\Scripts\\python.exe -m evidence.hid_probe
```

Read [`evidence/README.md`](evidence/README.md) first: it answers each question in a
sentence, which is usually all anyone needs.

### `evidence/probe.py` — does GameInput expose force-feedback motors? (**no**)

%s
### `evidence/dinput_probe.py` — does DirectInput expose force feedback on this wheel? (**no**)

%s
### `evidence/hid_probe.py` — does the device publish a USB PID force-feedback collection?

Reads raw HID report descriptors, and shows *why* the two answers above are no.

%s
### `evidence/gip_direct.py` — talk to the GIP driver with no WGI in the process (**dead end**)

The arming sequence was replayed byte-for-byte and still produced no torque. Do not retry this
without new information.

%s
### `evidence/gip_diff.py` — compare two GIP captures

%s
### `evidence/gip_wheel_driver.py` — the first raw USB driver (**Linux**)

Owns the wheel over raw USB, publishes a virtual joystick, and holds a spring and damper on
the motor. The defaults are the tuned values. [`evidence/RAW_USB.md`](evidence/RAW_USB.md)
explains how it got there.

%s
### `evidence/gip_hold.py` — drive to a position and hold it (**Linux**)

The sweep half of the Linux acceptance test: drives to each target slowly and logs where the
wheel really is.

%s
### `evidence/gip_usb_host.py` — the raw USB instrument (**Linux**)

Claiming, power-on, wire replay, force scaling and closed-loop position control. This is what
the findings were measured with.

### `evidence/usbpcap_parse.py` — read a USBPcap capture

Parses `DLT_USBPCAP` records and filters by device and endpoint. This is what showed that the
old WGI capture is not what reaches the wire, which is the discovery the raw USB route rests
on.
""" % (
    BUNDLED,
    BUNDLED, flag_table('bridge/__main__.py'),
    BUNDLED,
    BUNDLED, TUNE_REPORT_ARGS,
    BUNDLED, flag_table('vjoy_ffb_spike.py'),
    REPO, ps_table('packaging/build_bundle.ps1'),
    flag_table('evidence/probe.py'), flag_table('evidence/dinput_probe.py'),
    flag_table('evidence/hid_probe.py'),
    flag_table('evidence/gip_direct.py'), flag_table('evidence/gip_diff.py'),
    flag_table('evidence/gip_wheel_driver.py'), flag_table('evidence/gip_hold.py'),
))

new = '\n'.join(doc)
old = (io.open('COMMANDS.md', encoding='utf-8').read()
       if os.path.exists('COMMANDS.md') else None)

if ARGS.check:
    if old != new:
        print('COMMANDS.md is STALE. Run: python packaging' + BS + 'gen_commands.py')
        sys.exit(1)
    print('COMMANDS.md is up to date.')
    sys.exit(0)

io.open('COMMANDS.md', 'w', encoding='utf-8', newline='\n').write(new)

total = sum(len(v) for v in flags.values())
print('COMMANDS.md written: %d scripts with flags, %d flags total' % (len(flags), total))
