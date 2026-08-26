# Build the WH33LH4X dinput8.dll proxy.
#
# Uses zig as the C toolchain -- it ships its own clang, MinGW headers and import libraries,
# so this needs no Visual Studio. There is no cl.exe on this machine, which is what ruled out
# MSVC in the first place. A Windows SDK is still required, but only for its WinRT headers
# (see the note further down); zig supplies everything else.
#
# zig is looked up in three places, in order: the portable toolchain in tools/zig (how this
# machine builds), $env:ZIG (an explicit override), then PATH (how CI builds, where the
# toolchain is installed by an action rather than unpacked into the repo).
#
# 64-bit only, deliberately. Exporting DirectInput8Create from a 32-bit build needs a .def
# file, because __stdcall exports get decorated as _DirectInput8Create@20 there and a game
# looks for the undecorated name. x64 has one calling convention and no decoration, so
# __declspec(dllexport) is enough. Add the .def when a 32-bit game actually needs it -- not
# before, since an untested build artifact is worse than no build artifact.

$ErrorActionPreference = 'Stop'

$shimDir = $PSScriptRoot
$repo    = Split-Path -Parent $shimDir
$outDir  = Join-Path $shimDir 'build'

$portableZig = Join-Path $repo 'tools\zig\zig.exe'
if (Test-Path $portableZig) {
    $zig = $portableZig
} elseif ($env:ZIG -and (Test-Path $env:ZIG)) {
    $zig = $env:ZIG
} elseif (Get-Command zig -ErrorAction SilentlyContinue) {
    $zig = (Get-Command zig).Source
} else {
    Write-Error "zig not found. Looked in $portableZig, the ZIG environment variable, and PATH -- extract a portable zig into tools/zig, or put one on PATH."
}

if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }

$out     = Join-Path $outDir 'dinput8.dll'
$sources = @('dinput8.c', 'wgi.c', 'ipc.c') | ForEach-Object { Join-Path $shimDir $_ }
$def     = Join-Path $shimDir 'dinput8.def'

# The Windows SDK's MIDL-generated WinRT headers give us C vtable structs for
# Windows.Gaming.Input, so the shim calls WGI as ordinary COM instead of hand-writing the ABI.
# They compile under zig's clang, but they are written for MSVC, hence the two suppressions:
# MSVC-only #pragma warning directives, and #include lines whose case does not match disk.
$sdkRoot = 'C:\Program Files (x86)\Windows Kits\10\Include'
$sdkVer  = Get-ChildItem $sdkRoot -Directory -ErrorAction SilentlyContinue |
           Where-Object { Test-Path (Join-Path $_.FullName 'winrt\windows.gaming.input.forcefeedback.h') } |
           Sort-Object Name -Descending | Select-Object -First 1
if (-not $sdkVer) {
    Write-Error "No Windows SDK with winrt\windows.gaming.input.forcefeedback.h under $sdkRoot -- the shim cannot reach the motor without it."
}
$winrtInc = Join-Path $sdkVer.FullName 'winrt'

Write-Host "zig:     $((& $zig version))"
Write-Host "sdk:     $($sdkVer.Name)"
Write-Host "sources: $($sources -join ', ')"
Write-Host "output:  $out"

& $zig cc `
    -shared `
    -target x86_64-windows-gnu `
    -O2 `
    -Wall -Wextra `
    -Wno-nonportable-include-path -Wno-unknown-pragmas `
    -I $winrtInc `
    -I $shimDir `
    -o $out `
    @sources `
    $def `
    -lkernel32 -lole32 `
    -lapi-ms-win-core-winrt-l1-1-0 `
    -lapi-ms-win-core-winrt-string-l1-1-0

if ($LASTEXITCODE -ne 0) { Write-Error "zig cc failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "built: $out  ($((Get-Item $out).Length) bytes)"
Write-Host "verify with: .\.venv\Scripts\python.exe shim\test_proxy.py"
