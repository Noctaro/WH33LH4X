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
#   .\play.ps1 -Game ... -NoLaunch              # shim + bridge, you start it from Steam
#   .\play.ps1 -Game ... -NoFfb                 # input only -- for binding controls
#
# TUNING happens in tune.json while the game runs -- strength, max_force, min_force and the
# synthetic spring/damper are all re-read within half a second of a save. Nothing here needs
# restarting to change how the wheel feels, except -Gain.
#
#   .\play.ps1 -Game ... -KeepDll               # leave the DLL in place after exiting
#   .\play.ps1 -Game ... -StartTimeout 300      # slow Steam start / login prompt

[CmdletBinding()]
param(
    [string] $Game,
    # The motor's master gain, LATCHED when the shim loads the effect -- the one value that
    # cannot be changed while you drive. So it opens all the way and tune.json's max_force does
    # the limiting instead, live. Turning this down only removes headroom you cannot get back
    # without restarting the game.
    [double] $Gain = 1.0,
    [double] $MaxForce = 0.6,
    [switch] $KeepDll,
    [switch] $NoBridge,
    # Deploy the shim and start the bridge, but let you start the game yourself. What you want
    # for a Steam title: the client launches it the way it always does, and we still clean the
    # DLL out of the folder when you quit.
    [switch] $NoLaunch,
    # Feed the axes but never touch force feedback. FOR BINDING: a game re-initialising the
    # vJoy device mid-binding has to take the force-feedback channel, and it cannot while the
    # bridge holds it -- some games report that as the device being disconnected. Bind with
    # this, then restart without it to play.
    [switch] $NoFfb,
    # How long to wait for the game process to show up. Steam can take a while when it has to
    # start the client, update, or prompt for a login.
    [int] $StartTimeout = 120
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
$bridge = Join-Path $repo 'vjoy_bridge.py'
$builtDll = Join-Path $repo 'shim\build\dinput8.dll'

if (-not (Test-Path $python)) { Write-Error "No venv python at $python" }

$deployed = $null
$bridgeProc = $null
$procName = $null

try {
    if ($Game) {
        if (-not (Test-Path $Game)) { Write-Error "Game exe not found: $Game" }
        if (-not (Test-Path $builtDll)) { Write-Error "Shim not built. Run .\shim\build.ps1 first." }

        $gameDir = Split-Path -Parent $Game
        $deployed = Join-Path $gameDir 'dinput8.dll'
        $alreadyOurs = $false

        if (Test-Path $deployed) {
            # Two separate questions, and conflating them cost a whole test cycle: "is this the
            # CURRENT build?" and "is this OURS AT ALL?". Comparing hashes answers the first.
            # Answering the second with the same comparison means any older build of our own
            # shim looks like a stranger's DLL and is refused -- so the game silently keeps
            # running a stale shim while every rebuild appears to succeed.
            #
            # Ownership is a marker inside the binary instead: the shared-section name, which
            # only this project's shim contains. It is a wide string, hence Unicode below.
            $ours = (Get-FileHash $builtDll -Algorithm SHA256).Hash
            $there = (Get-FileHash $deployed -Algorithm SHA256).Hash
            $isOurs = $false
            try {
                $bytes = [IO.File]::ReadAllBytes($deployed)
                $isOurs = [Text.Encoding]::Unicode.GetString($bytes).Contains('WH33LH4X_bridge_v2')
            } catch {
                # Locked by a running game. Only our own shim gets loaded by a game we launched,
                # so treat a lock as evidence of ownership rather than refusing to proceed.
                $isOurs = $true
            }
            if ($ours -eq $there) {
                # Already the current shim. Usually means a game is running with it loaded,
                # which is exactly when you want to restart the bridge alone -- so this is a
                # normal state, not a conflict. Copying over it would fail anyway: Windows
                # locks a mapped DLL, and that failure is what used to abort the launcher.
                $alreadyOurs = $true
                Write-Host "shim already in place: $deployed"
            } elseif ($isOurs) {
                Write-Host "replacing an older WH33LH4X shim in $gameDir"
            } elseif (-not $KeepDll) {
                # Refuse to clobber someone else's proxy -- ReShade and friends use this same
                # filename, and silently replacing one would break their setup in a way nobody
                # would connect back to this script.
                Write-Warning "A different dinput8.dll is already in $gameDir."
                Write-Warning "Not touching it. Move it aside yourself, or pass -KeepDll to leave things alone."
                $deployed = $null
            }
        }

        if ($deployed -and -not $alreadyOurs) {
            try {
                Copy-Item $builtDll $deployed -Force
                Write-Host "shim deployed: $deployed"
            } catch {
                # In use by a running game, almost certainly. Not fatal, and not a reason to
                # refuse to start the bridge.
                Write-Warning "Could not write $deployed -- $($_.Exception.Message)"
                Write-Warning "Carrying on. If a game is already running it has the shim loaded."
                $deployed = $null
            }
        }
    }

    if (-not $NoBridge) {
        # Not $args -- that is an automatic variable in PowerShell and assigning to it is a
        # quiet way to break argument handling later in the script.
        $bridgeArgs = @($bridge, '--gain', $Gain, '--max-force', $MaxForce)
        if ($NoFfb) { $bridgeArgs += '--no-ffb' }
        $bridgeProc = Start-Process -FilePath $python -ArgumentList $bridgeArgs -PassThru
        if ($NoFfb) {
            Write-Host "bridge started (pid $($bridgeProc.Id))  INPUT ONLY -- no force feedback"
            Write-Host "  Bind your controls now, then restart without -NoFfb to play."
        } else {
            Write-Host "bridge started (pid $($bridgeProc.Id))  gain=$Gain maxforce=$MaxForce"
        }
        # The bridge no longer needs to start before the game -- readings come from the shim
        # once the game is up -- but giving it a moment means vJoy is already being fed when
        # the game enumerates devices, which makes binding an axis less fiddly.
        Start-Sleep -Seconds 2
    }

    if ($Game) {
        $procName = [IO.Path]::GetFileNameWithoutExtension($Game)
        # Waiting on a person takes longer than waiting on Steam, so -NoLaunch gets a bigger
        # default -- but an explicit -StartTimeout always wins.
        $wait = if ($NoLaunch -and -not $PSBoundParameters.ContainsKey('StartTimeout')) { 600 }
                else { $StartTimeout }

        if ($NoLaunch) {
            # Steam titles are happier started the way they normally are -- from the library,
            # so the client sets up its own environment, overlay and cloud sync exactly as it
            # always does. We still do the two things that matter: the shim is in the folder
            # before the game loads, and it comes out again when the game quits.
            Write-Host ""
            Write-Host "Shim and bridge are up. Start $procName yourself now -- Steam, a shortcut, however you like."
            Write-Host "Cleanup runs by itself when the game exits, or press Ctrl+C here."
            Write-Host ""
        } else {
            Write-Host "launching $Game"
            # -WorkingDirectory matters: without it the game inherits this script's directory
            # and looks for its own data files in the repo. No -PassThru: the handle is no use
            # to us (see below), and capturing it in a $game variable would silently coerce the
            # Process to a string, since variable names are case-insensitive and $Game is the
            # [string] parameter above.
            Start-Process -FilePath $Game -WorkingDirectory $gameDir
            Write-Host "waiting for the game to exit (Ctrl+C here is safe -- cleanup still runs)"
        }

        # WHY WE WATCH A PROCESS NAME RATHER THAN A HANDLE
        #
        # A Steam game with no steam_appid.txt next to it re-execs itself through Steam:
        # SteamAPI_RestartAppIfNecessary hands the app id to the client and the process we
        # started exits within a second, then Steam launches a fresh one. Waiting on the handle
        # we get back returns immediately, and the cleanup below then yanks the shim out of the
        # game folder just as the real process is starting -- which looks exactly like "the
        # game did not start". The replacement lives in the same folder, so the shim still
        # loads; we just have to spot it by name. The same loop serves -NoLaunch, where there
        # is no handle to wait on at all.
        $running = $false
        # Generous while we wait for it to appear -- Steam may still be starting up or asking
        # someone to log in, and under -NoLaunch a person has to go and click the thing. Short
        # once we have seen it, so a real quit cleans up promptly.
        $deadline = (Get-Date).AddSeconds($wait)
        while ((Get-Date) -lt $deadline) {
            if (@(Get-Process -Name $procName -ErrorAction SilentlyContinue).Count -gt 0) {
                if (-not $running) {
                    $running = $true
                    Write-Host "game is up ($procName)"
                }
                $deadline = (Get-Date).AddSeconds(10)
            }
            Start-Sleep -Seconds 2
        }
        if (-not $running) {
            Write-Warning "Never saw a process named '$procName' within $wait s -- cleaning up."
        }
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
            # Windows locks a DLL that is mapped into a live process, so the usual cause is a
            # game still running -- which also means the file is still doing its job and is
            # not a problem yet. Say which case this is rather than sounding an alarm that
            # reads the same either way.
            # $procName is set inside the try, so it can be unset if we failed before that.
            $stillRunning = $false
            if ($procName) {
                $stillRunning = @(Get-Process -Name $procName -ErrorAction SilentlyContinue).Count -gt 0
            }
            if ($stillRunning) {
                Write-Warning "$procName is still running, so $deployed stays for now."
                Write-Warning "Run this script again once the game exits to clear it, or delete it yourself."
            } else {
                Write-Warning "Could not remove $deployed -- delete it yourself before playing online."
            }
        }
    }
}
