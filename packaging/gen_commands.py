r"""
gen_commands.py -- generate COMMANDS.md from the actual source.

Flags and defaults are read out of the argparse calls and the PowerShell param() blocks,
so the reference cannot claim a flag the code does not parse. The prose around them is
written here; only the tables are derived.

This lived in a scratch folder for a while, which meant the one document claiming to be
generated was the one nobody else could regenerate. It belongs beside gen_games_table.py.

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
for f in (sorted(glob.glob('*.py')) + sorted(glob.glob('shim/*.py'))
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
for f in ['play.ps1', 'shim/build.ps1', 'packaging/build_bundle.ps1']:
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
    'Game': 'Path to the game exe. Deploys the shim beside it and launches it.',
    'Gain': 'Motor master gain. LATCHED when the shim loads the effect, so it cannot change '
            'while you drive -- leave it open and let `max_force` do the limiting.',
    'MaxForce': 'STARTING cap on commanded force. `tune.json` overrides this live.',
    'KeepDll': 'Leave the shim in the game folder on exit instead of removing it.',
    'NoBridge': 'Deploy the shim but do not start the Python bridge.',
    'BridgeOnly': 'Start the bridge and nothing else. The old no-argument behaviour.',
    'NoLaunch': 'Deploy and start the bridge, but launch the game yourself -- what you want '
                'for a Steam title.',
    'NoFfb': 'Feed the axes but never take the motor. USE THIS WHILE BINDING CONTROLS.',
    'StartTimeout': 'Seconds to wait for the game process to appear. Steam can be slow.',
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

**In the downloadable bundle**, run things through the bundled interpreter, which needs no
Python installed:

```
WH33LH4X.cmd -Game "C:\\...\\dirt4.exe"
.\\python\\python.exe vjoy_ffb_spike.py
```

**In a source checkout**, use the venv:

```
.\\play.ps1 -Game "C:\\...\\dirt4.exe"
.\\.venv\\Scripts\\python.exe vjoy_ffb_spike.py
```

Force feedback tuning is not a flag. It lives in `tune.json` and is re-read within half a
second of a save, so it changes mid-corner without restarting anything — see
[docs/tuning.md](docs/tuning.md).

---

## 1. Playing a game

### `WH33LH4X-GUI.cmd` — the window

%s Pick a game, press Start, watch three status badges. It shells out to `play.ps1` below
rather than reimplementing it, so everything true of `play.ps1` is true here too.

What it adds over the command line:

- **Steam titles are handled for you.** It passes `-NoLaunch` and asks the client to start
  the game, because Steam DRM relaunches the exe as a different process and a direct
  launch looks like the game quitting instantly.
- **Per-game notes** from `games.json` — the quirks that otherwise cost you an evening.
- **Spring, Damper and Friction** sliders, written straight to `tune.json` as you move them.
- **Status badges** read from the shim's shared section: vJoy, bridge, shim.

Takes no arguments. Runs under `pythonw.exe` so no console sits behind it, and it starts
the bridge with `-Quiet` so none appears for that either.

### `play.ps1` — start the bridge, run a game, clean up after

%s Also reachable as `WH33LH4X.cmd`, which is the same script with the PowerShell execution
policy handled — a `.ps1` that arrived inside a downloaded zip will not run on a double-click
without it.

Deploys the shim into the game folder before launch and **removes it again when the game
exits**, even on a crash or Ctrl+C. If another tool's `dinput8.dll` is already there (ReShade
uses the same filename) it is moved aside and put back afterwards.

Run it with no arguments for usage.

%s
### `WH33LH4X.cmd`

%s Takes no arguments of its own — everything is forwarded to `play.ps1` above.

---

## 2. The bridge itself

### `vjoy_bridge.py` — present the wheel to DirectInput games through vJoy

%s Normally started for you by `play.ps1`. Run it directly to debug, or to use a flag
`play.ps1` does not expose.

%s
### `tune_report.py` — say what the force feedback actually did

%s Reads a bridge session log and reports which effects the game sent, whether output ever
went negative, and the correlation between steering angle and force. That last number is the
objective test for centring: a wheel whose output never changes sign cannot centre, however it
felt.

%s
---

## 3. Checking the setup

### `vjoy_ffb_spike.py` — does the vJoy force-feedback path work?

%s **Run this first when anything is wrong.** It sends DirectInput effects to the virtual
device and logs what comes back out of vJoy's callback, verifying the whole path without a game
or the real wheel. If it does not report `PASS`, nothing built on top of vJoy will work.

%s
### `wgi_probe.py` — drive the real motor directly

%s Detection, an interactive effect menu, sweeps and calibration, all through
`Windows.Gaming.Input` with no game and no vJoy involved. Use it to answer "is the wheel itself
alive?" — and press `k` once per wheel to calibrate, which the software condition effects
require.

%s
### `shim/test_proxy.py` — is the dinput8.dll proxy transparent?

%s Drives the proxy directly and compares its device enumeration against the system DLL in the
same process, so a crippled proxy is caught here rather than looking like a game bug.

%s
### `shim/run_shim.py` — exercise the shim without a game

%s Hosts the shim outside a game so its WGI layer can be tested without launching one.

%s
---

## 4. Building

### `shim/build.ps1` — build the dinput8.dll proxy

%s Needs zig (from `tools/zig`, `$env:ZIG`, or `PATH`) and a Windows SDK for its WinRT headers.
Takes no parameters.

### `packaging/build_bundle.ps1` — assemble the downloadable bundle

%s Downloads and verifies embeddable CPython, vendors the pinned dependencies, stages the
source and the shim, and zips the result.

%s
---

## 5. Modules with no command line

Imported by the scripts above; nothing to run.

| Module | Purpose | In bundle |
|---|---|---|
| `ffb_render.py` | The force-feedback control laws, in normalised units | yes |
| `motor_sink.py` | The one place that actually touches the wheel's motor | yes |
| `live_tune.py` | `tune.json` reloading and the wheel-button tuning controls | yes |
| `wheel_profile.py` | Per-wheel calibration, saved once and reused | yes |
| `probe_log.py` | Session logging to `logs/` | yes |
| `dinput_abi.py` | ctypes transcription of the DirectInput 8 API surface | yes |
| `gameinput_abi.py` | ctypes transcription of Microsoft's GameInput API (v0 ABI) | no |
| `gip_protocol.py` | The GIP wire format for this wheel | no |

---

## 6. Diagnostics, and the dead ends kept as evidence

None of these ship in the bundle.

Everything below marked with a path lives in [`evidence/`](evidence/README.md) and
records an API that **does not work** on this wheel. Run those as modules, from the repo
root -- they import from it, so a direct path will not resolve:

```
.\.venv\Scripts\python.exe -m evidence.hid_probe
```

Read [`evidence/README.md`](evidence/README.md) first: it answers each question in a
sentence, which is usually all anyone needs. The code is there so the answers stay checkable
against a newer runtime or a different wheel.

### `test_ffb_render.py` — the control laws still do what they used to

Run before proposing a change to `ffb_render.py`. No test framework, no dependencies, no
hardware: `.\\.venv\\Scripts\\python.exe test_ffb_render.py`. Takes no flags.

### `test_shimview.py` — the GUI must not create the shared section

No hardware, no wheel, no game. Asserts the ORDER that broke a real session rather than
the parts: a viewer touching the section before any writer must not bring it into
existence, or every later writer fails with `WinError 87` and the game gets no wheel
input.

**It skips itself when a bridge is already running.** It opens a real sink, and closing
one zeroes the bridge heartbeat -- against a live session that is a force-feedback
dropout. Takes no flags.

### `evidence/probe.py` — does GameInput expose force-feedback motors? (**no**)

%s
### `evidence/dinput_probe.py` — does DirectInput expose force feedback on this wheel? (**no**)

%s
### `evidence/hid_probe.py` — does the device publish a USB PID force-feedback collection?

Reads raw HID report descriptors, and shows *why* the two answers above are no.

%s
### `evidence/wgi_background_test.py` — does WGI still work when we are not in front? (**no**)

The measurement behind the whole shim design: force output and position reading are both gated
on foreground, which is why the output stage has to live inside the game's process.

%s
### `stiction_test.py` — how much force does it take to move this wheel at all?

Measures the motor's own breakaway friction, which is where `tune.json`'s `min_force` comes
from.

%s
### `evidence/gip_trace.py` — watch what WGI sends the wheel

%s
### `evidence/gip_direct.py` — talk to the GIP driver with no WGI in the process (**dead end**)

The arming sequence was replayed byte-for-byte and still produced no torque. Do not retry this
without new information.

%s
### `evidence/gip_diff.py` — compare the two captures above

%s""" % (
    BUNDLED,
    BUNDLED, ps_table('play.ps1'), BUNDLED,
    BUNDLED, flag_table('vjoy_bridge.py'),
    BUNDLED, TUNE_REPORT_ARGS,
    BUNDLED, flag_table('vjoy_ffb_spike.py'),
    BUNDLED, flag_table('wgi_probe.py'),
    REPO, flag_table('shim/test_proxy.py'),
    REPO, flag_table('shim/run_shim.py'),
    REPO, REPO, ps_table('packaging/build_bundle.ps1'),
    flag_table('evidence/probe.py'), flag_table('evidence/dinput_probe.py'),
    flag_table('evidence/hid_probe.py'), flag_table('evidence/wgi_background_test.py'),
    flag_table('stiction_test.py'), flag_table('evidence/gip_trace.py'),
    flag_table('evidence/gip_direct.py'), flag_table('evidence/gip_diff.py'),
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
