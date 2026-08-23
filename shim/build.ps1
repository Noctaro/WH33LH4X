# Build the WH33LH4X dinput8.dll proxy.
#
# Uses the portable zig toolchain in tools/zig -- zig ships its own clang, MinGW headers and
# import libraries, so this needs no Visual Studio and no Windows SDK. There is no cl.exe on
# this machine, which is what ruled out MSVC in the first place.
#
# 64-bit only, deliberately. Exporting DirectInput8Create from a 32-bit build needs a .def
# file, because __stdcall exports get decorated as _DirectInput8Create@20 there and a game
# looks for the undecorated name. x64 has one calling convention and no decoration, so
# __declspec(dllexport) is enough. Add the .def when a 32-bit game actually needs it -- not
# before, since an untested build artifact is worse than no build artifact.

$ErrorActionPreference = 'Stop'

$shimDir = $PSScriptRoot
$repo    = Split-Path -Parent $shimDir
$zig     = Join-Path $repo 'tools\zig\zig.exe'
$outDir  = Join-Path $shimDir 'build'

if (-not (Test-Path $zig)) {
    Write-Error "zig not found at $zig -- see the toolchain note in the plan (extract a portable zig into tools/zig)."
}

if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }

$out = Join-Path $outDir 'dinput8.dll'
$src = Join-Path $shimDir 'dinput8.c'
$def = Join-Path $shimDir 'dinput8.def'

Write-Host "zig:    $((& $zig version))"
Write-Host "source: $src"
Write-Host "output: $out"

& $zig cc `
    -shared `
    -target x86_64-windows-gnu `
    -O2 `
    -Wall -Wextra `
    -o $out `
    $src `
    $def `
    -lkernel32

if ($LASTEXITCODE -ne 0) { Write-Error "zig cc failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "built: $out  ($((Get-Item $out).Length) bytes)"
Write-Host "verify with: .\.venv\Scripts\python.exe shim\test_proxy.py"
