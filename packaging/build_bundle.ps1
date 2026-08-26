# Assemble the downloadable WH33LH4X bundle.
#
# WHY EMBEDDABLE PYTHON RATHER THAN A FROZEN EXE
#
# This tool's job is copying an unsigned dinput8.dll into a game folder, which is already the
# behavioural signature of a DLL-hijack injector. PyInstaller would wrap that in an
# extract-and-run bootloader -- the same pattern packers use -- and hand antivirus a second
# reason to object. Embeddable CPython ships a python.exe SIGNED BY MICROSOFT, keeps our source
# readable so a suspicious user can check what it does, and lets someone edit a line and send
# back a log. None of that survives a frozen bundle.
#
# THE ONE MECHANISM THAT MAKES THIS WORK
#
# The embeddable distribution deliberately ignores site-packages. Vendored packages become
# importable only by rewriting python\pythonXYZ._pth, which lists sys.path relative to itself.
# Everything else here is copying files.
#
# WHY THE VENDOR STEP IS A SINGLE PIP INVOCATION
#
# winrt is a PEP 420 namespace package at every level -- no __init__.py in winrt\,
# winrt\windows\ or winrt\windows\gaming\ -- and seven separate wheels contribute into that one
# tree. Installed in one resolved pass they merge. Installed in separate --target passes, pip
# overwrites rather than merges and the tree comes out missing namespaces. Do not split it up.
#
# Usage:
#   .\packaging\build_bundle.ps1                     # build into dist\
#   .\packaging\build_bundle.ps1 -OutDir C:\somewhere
#   .\packaging\build_bundle.ps1 -SkipZip            # leave the folder, skip the archive

[CmdletBinding()]
param(
    [string] $OutDir,
    [string] $CacheDir,
    [switch] $SkipZip
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
if (-not $OutDir)   { $OutDir   = Join-Path $repo 'dist' }
if (-not $CacheDir) { $CacheDir = Join-Path $repo 'dist\.cache' }

# Pinned deliberately. The vendored wheels are cp311 win_amd64 builds, so the runtime that
# loads them has to be 3.11 too -- a mismatch here surfaces as an ImportError on a .pyd, which
# is a confusing way to learn about a version bump.
$pyVersion = '3.11.9'
$pyTag     = 'python311'
$pyUrl     = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip"
$pyMd5     = '6d9aa08531d48fcc261ba667e2df17c4'

$staging = Join-Path $OutDir 'WH33LH4X'

# Runtime modules, then the diagnostics a user actually needs. The dead ends now live in
# evidence\ and stiction_test.py stays in the repo root -- shipping either would put negative
# results and a hardware measurement tool in a user's folder. This is an ALLOWLIST: a new file
# in the repo does not reach the bundle until it is named here.
$sourceFiles = @(
    'vjoy_bridge.py', 'ffb_render.py', 'probe_log.py', 'live_tune.py',
    'motor_sink.py', 'wgi_probe.py', 'wheel_profile.py',
    'vjoy_ffb_spike.py', 'dinput_abi.py', 'tune_report.py',
    'play.ps1', 'tune.json', 'requirements.txt',
    'README.md', 'GAMES.md', 'COMMANDS.md', 'LICENSE', 'THIRD-PARTY-NOTICES.md'
)

Write-Host "repo:    $repo"
Write-Host "out:     $staging"
Write-Host "python:  $pyVersion embeddable"

# --- clean --------------------------------------------------------------------------------
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging -Force | Out-Null
New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null

# --- embeddable runtime -------------------------------------------------------------------
$pyZip = Join-Path $CacheDir "python-$pyVersion-embed-amd64.zip"
if (-not (Test-Path $pyZip)) {
    Write-Host "downloading $pyUrl"
    Invoke-WebRequest -Uri $pyUrl -OutFile $pyZip -UseBasicParsing
} else {
    Write-Host "using cached $pyZip"
}

# python.org publishes MD5 for these, so MD5 is what can be checked against upstream. It is an
# integrity check against a truncated or swapped download, not a security boundary -- the real
# provenance claim for this bundle is the CI attestation.
$gotMd5 = (Get-FileHash $pyZip -Algorithm MD5).Hash.ToLower()
if ($gotMd5 -ne $pyMd5) {
    Write-Error "Embeddable Python MD5 mismatch. Expected $pyMd5, got $gotMd5. Delete $pyZip and retry."
}
Write-Host "md5 ok:  $gotMd5"

$pyDir = Join-Path $staging 'python'
Expand-Archive -Path $pyZip -DestinationPath $pyDir -Force

# THE line that makes vendored packages importable. Paths are relative to the ._pth file, so
# '..' is the bundle root (our .py files) and '..\lib' the vendored dependencies.
$pthPath = Join-Path $pyDir "$pyTag._pth"
if (-not (Test-Path $pthPath)) {
    Write-Error "No $pyTag._pth in the embeddable distribution -- its layout changed upstream."
}
$pthLines = @("$pyTag.zip", '.', '..', '..\lib')
Set-Content -Path $pthPath -Value $pthLines -Encoding ASCII
Write-Host "patched: $pyTag._pth"

# --- vendored dependencies ------------------------------------------------------------------
# ONE invocation, resolving the whole pinned set together. See the namespace-package note above.
$libDir = Join-Path $staging 'lib'
$req    = Join-Path $repo 'requirements.txt'
Write-Host "vendoring dependencies into lib\ ..."
& python -m pip install --target $libDir --requirement $req --no-compile --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Write-Error "pip install --target failed with exit code $LASTEXITCODE" }

# dist-info directories carry the licence metadata THIRD-PARTY-NOTICES.md refers to, so they
# stay. __pycache__ would only be stale bytecode compiled for a different interpreter path.
Get-ChildItem $libDir -Recurse -Directory -Filter '__pycache__' |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# --- our own files ----------------------------------------------------------------------------
foreach ($f in $sourceFiles) {
    $src = Join-Path $repo $f
    if (-not (Test-Path $src)) { Write-Error "Expected file missing from the repo: $f" }
    Copy-Item $src (Join-Path $staging $f)
}

# Per-game setup material. GAMES.md links to docs\dirt4\vjoy_wheel.xml, so a bundle without it
# tells the user to open a file they do not have.
$docsSrc = Join-Path $repo 'docs'
if (Test-Path $docsSrc) { Copy-Item $docsSrc (Join-Path $staging 'docs') -Recurse }

# The shim, flattened out of shim\build\ -- 'build' is a meaningless word in a shipped folder.
$shimSrc = Join-Path $repo 'shim\build\dinput8.dll'
if (-not (Test-Path $shimSrc)) { Write-Error "No shim at $shimSrc. Run .\shim\build.ps1 first." }
New-Item -ItemType Directory -Path (Join-Path $staging 'shim') -Force | Out-Null
Copy-Item $shimSrc (Join-Path $staging 'shim\dinput8.dll')

# --- launcher ------------------------------------------------------------------------------
# WHY A .cmd AT ALL: a play.ps1 that arrived inside a downloaded zip carries Mark of the Web and
# is refused by the default execution policy. -ExecutionPolicy Bypass on an explicit -File is
# what makes a double-click work at all. %~dp0 keeps the whole folder relocatable.
$cmdLines = @(
    '@echo off',
    'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0play.ps1" %*'
)
Set-Content -Path (Join-Path $staging 'WH33LH4X.cmd') -Value $cmdLines -Encoding ASCII

# --- report and zip --------------------------------------------------------------------------
$files = @(Get-ChildItem $staging -Recurse -File)
$size  = ($files | Measure-Object -Property Length -Sum).Sum
Write-Host ""
Write-Host ("staged:  {0:N1} MB across {1} files" -f ($size / 1MB), $files.Count)

if (-not $SkipZip) {
    $zip = Join-Path $OutDir 'WH33LH4X-windows-x64.zip'
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path $staging -DestinationPath $zip -CompressionLevel Optimal
    Write-Host ("zipped:  {0}  ({1:N1} MB)" -f $zip, ((Get-Item $zip).Length / 1MB))
}

Write-Host ""
Write-Host "verify the runtime with:"
Write-Host "  $staging\python\python.exe -c ""import winrt.windows.gaming.input.forcefeedback, pyvjoy, typing_extensions; print('imports OK')"""
