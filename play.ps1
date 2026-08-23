# Start everything needed for force feedback, run a game, then clean up.
#
# WHY A LAUNCHER RATHER THAN AUTO-STARTING FROM THE SHIM
#
# The shim could spawn the bridge itself, and that would be one fewer step. It would also mean
# a DLL inside a game launching a Python interpreter, which is the shape of thing behavioural
# anti-cheat looks for -- and when it went wrong it would go wrong invisibly, in someone else's
# process, with no console to read. This does the same work where you can see it.
#
# WHAT IT DOES ABOUT THE PROXY DLL
#
# A dinput8.dll sitting in a game folder is the actual anti-cheat exposure -- BattlEye blocks
# that filename on sight in titles that use it, because input-proxy DLLs are what mods and
# overlays use. The documented mitigation is to have it present only while you are playing, so
# by default this copies it in before launch and REMOVES IT AGAIN when the game exits, even if
# the game crashes or you Ctrl+C out.
#
# The sims this targets -- Dirt 4, RaceRoom, Automobilista 2 -- have no kernel anti-cheat and
# routinely take DLL mods. Do not point this at a title with EasyAntiCheat or BattlEye.
#
# Usage:
#   .\play.ps1                                  # bridge only, launch the game yourself
#   .\play.ps1 -Game "Y:\...\DiRT 4\dirt4.exe"  # deploy, run the game, clean up after
#   .\play.ps1 -Game ... -Gain 0.8 -MaxForce 0.9
#   .\play.ps1 -Game ... -KeepDll               # leave the DLL in place after exiting

[CmdletBinding()]
param(
    [string] $Game,
    [double] $Gain = 0.5,
    [double] $MaxForce = 0.6,
    [switch] $KeepDll,
    [switch] $NoBridge
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
$bridge = Join-Path $repo 'vjoy_bridge.py'
$builtDll = Join-Path $repo 'shim\build\dinput8.dll'

if (-not (Test-Path $python)) { Write-Error "No venv python at $python" }

$deployed = $null
$bridgeProc = $null

try {
    if ($Game) {
        if (-not (Test-Path $Game)) { Write-Error "Game exe not found: $Game" }
        if (-not (Test-Path $builtDll)) { Write-Error "Shim not built. Run .\shim\build.ps1 first." }

        $gameDir = Split-Path -Parent $Game
        $deployed = Join-Path $gameDir 'dinput8.dll'
        # Refuse to clobber someone else's proxy -- ReShade and friends use this same filename,
        # and silently replacing one would break their setup in a way nobody would connect
        # back to this script.
        if ((Test-Path $deployed) -and -not $KeepDll) {
            $existing = (Get-Item $deployed).Length
            $ours = (Get-Item $builtDll).Length
            if ($existing -ne $ours) {
                Write-Warning "A different dinput8.dll is already in $gameDir."
                Write-Warning "Not touching it. Move it aside yourself, or pass -KeepDll to leave things alone."
                $deployed = $null
            }
        }
        if ($deployed) {
            Copy-Item $builtDll $deployed -Force
            Write-Host "shim deployed: $deployed"
        }
    }

    if (-not $NoBridge) {
        # Not $args -- that is an automatic variable in PowerShell and assigning to it is a
        # quiet way to break argument handling later in the script.
        $bridgeArgs = @($bridge, '--gain', $Gain, '--max-force', $MaxForce)
        $bridgeProc = Start-Process -FilePath $python -ArgumentList $bridgeArgs -PassThru
        Write-Host "bridge started (pid $($bridgeProc.Id))  gain=$Gain maxforce=$MaxForce"
        # The bridge no longer needs to start before the game -- readings come from the shim
        # once the game is up -- but giving it a moment means vJoy is already being fed when
        # the game enumerates devices, which makes binding an axis less fiddly.
        Start-Sleep -Seconds 2
    }

    if ($Game) {
        Write-Host "launching $Game"
        $game = Start-Process -FilePath $Game -PassThru
        Write-Host "waiting for the game to exit (Ctrl+C here is safe -- cleanup still runs)"
        $game.WaitForExit()
    } else {
        Write-Host ""
        Write-Host "Bridge is running. Launch the game whenever you like."
        Write-Host "Press Ctrl+C here when you are done."
        while ($true) { Start-Sleep -Seconds 3600 }
    }
}
finally {
    if ($bridgeProc -and -not $bridgeProc.HasExited) {
        Write-Host "stopping the bridge"
        try { $bridgeProc.CloseMainWindow() | Out-Null } catch {}
        Start-Sleep -Milliseconds 500
        try { if (-not $bridgeProc.HasExited) { $bridgeProc.Kill() } } catch {}
    }
    if ($deployed -and -not $KeepDll -and (Test-Path $deployed)) {
        # The whole point: the proxy exists in the game folder only while you are playing.
        try {
            Remove-Item $deployed -Force
            Write-Host "shim removed: $deployed"
        } catch {
            Write-Warning "Could not remove $deployed -- delete it yourself before playing online."
        }
    }
}
