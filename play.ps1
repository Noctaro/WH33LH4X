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
    # Start the bridge and nothing else. This is what a bare run used to do; a bare run now
    # prints usage instead, so the old behaviour needs asking for by name.
    [switch] $BridgeOnly,
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
$bridge = Join-Path $repo 'vjoy_bridge.py'

# Two layouts, one script. A developer's checkout has a .venv; a downloaded bundle has an
# embeddable CPython in python\ and no venv at all. Same resolution pattern shim\build.ps1
# uses for zig, so there is one habit to learn rather than two.
$venvPython   = Join-Path $repo '.venv\Scripts\python.exe'
$bundlePython = Join-Path $repo 'python\python.exe'
if     (Test-Path $venvPython)   { $python = $venvPython }
elseif (Test-Path $bundlePython) { $python = $bundlePython }
else {
    Write-Error "No Python found. Looked for $venvPython and $bundlePython."
}

# Likewise the shim: shim\build\ is where build.ps1 writes it, shim\ is where a bundle ships
# it prebuilt.
$builtDll = @((Join-Path $repo 'shim\build\dinput8.dll'),
              (Join-Path $repo 'shim\dinput8.dll')) |
            Where-Object { Test-Path $_ } | Select-Object -First 1

if ($PSBoundParameters.Count -eq 0) {
    # A downloaded bundle is double-clicked, and silently starting a background bridge reads
    # as "nothing happened". Say what this is instead. -BridgeOnly is the old bare-run
    # behaviour for anyone who wants it.
    Write-Host @'
WH33LH4X -- force feedback for the Hori Force Feedback Racing Wheel DLX in PC sims.

The wheel only exposes force feedback through Windows.Gaming.Input, which no sim speaks. This
presents a virtual vJoy wheel to the game and renders what the game sends on the real motor.

  WH33LH4X.cmd -Game "C:\...\DiRT 4\dirt4.exe"   deploy the shim, run the game, clean up after
  WH33LH4X.cmd -Game "..." -NoLaunch             same, but you start the game from Steam
  WH33LH4X.cmd -Game "..." -NoFfb                input only -- use while binding controls
  WH33LH4X.cmd -BridgeOnly                       bridge only, launch the game yourself

In a source checkout the same switches work on .\play.ps1 directly.

Before any of that works:
  1. vJoy 2.2.2.0 installed, force feedback enabled on device 1
  2. the wheel in Xbox mode -- long-press PROFILE
  3. any per-game setup listed in GAMES.md

  GAMES.md    what works in which game, and what each one needs first
  tune.json   force feedback tuning, re-read within half a second of a save

'@
    exit 0
}

$deployed = $null
$bridgeProc = $null
$procName = $null
# Set together when we move another tool's proxy aside; used only by the finally block. Kept
# separate from $deployed, which gets nulled on failure paths -- an orphaned backup that never
# came back would be the worst outcome here.
$foreignBackup = $null
$foreignOriginal = $null

try {
    if ($Game) {
        if (-not (Test-Path $Game)) { Write-Error "Game exe not found: $Game" }
        if (-not $builtDll) {
            Write-Error "No dinput8.dll found in shim\build\ or shim\. In a checkout, run .\shim\build.ps1 first."
        }

        $gameDir = Split-Path -Parent $Game
        $deployed = Join-Path $gameDir 'dinput8.dll'
        $alreadyOurs = $false

        if (Test-Path $deployed) {
            # Hashes, not sizes. Two builds of our own shim differ in content far more often
            # than in length, and "same size" would call a stale build current.
            $ours = (Get-FileHash $builtDll -Algorithm SHA256).Hash
            $there = (Get-FileHash $deployed -Algorithm SHA256).Hash
            if ($ours -eq $there) {
                # Already the current shim. Usually means a game is running with it loaded,
                # which is exactly when you want to restart the bridge alone -- so this is a
                # normal state, not a conflict. Copying over it would fail anyway: Windows
                # locks a mapped DLL, and that failure is what used to abort the launcher.
                $alreadyOurs = $true
                Write-Host "shim already in place: $deployed"
            } elseif (-not $KeepDll) {
                # Someone else's proxy -- ReShade and friends use this same filename. Move it
                # aside rather than refusing outright: refusing left force feedback silently
                # dead with only a console warning to explain why. The finally block puts it
                # back, so their setup returns exactly as it was.
                $candidate = "$deployed.wh33lh4x-backup"
                if (Test-Path $candidate) {
                    # An earlier run died before restoring. THAT file is the real original --
                    # overwriting it with whatever is here now would destroy it for good.
                    Write-Warning "An earlier backup is still at $candidate."
                    Write-Warning "Not touching anything. Restore it yourself, or delete it if you know it is stale."
                    $deployed = $null
                } else {
                    try {
                        Move-Item $deployed $candidate -Force
                        $foreignOriginal = $deployed
                        $foreignBackup   = $candidate
                        Write-Host "moved an existing dinput8.dll aside: $candidate"
                    } catch {
                        # Locked, most likely by a game that is already running with it loaded.
                        Write-Warning "Could not move $deployed aside -- $($_.Exception.Message)"
                        Write-Warning "Leaving it alone. Force feedback will not work this session."
                        $deployed = $null
                    }
                }
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

    # Put another tool's proxy back exactly where it was. Only once ours is gone: restoring
    # over a shim that is still present would silently discard their file, which is the one
    # outcome worse than never having moved it.
    if ($foreignBackup -and (Test-Path $foreignBackup) -and -not (Test-Path $foreignOriginal)) {
        try {
            Move-Item $foreignBackup $foreignOriginal -Force
            Write-Host "restored the original dinput8.dll"
        } catch {
            Write-Warning "Could not restore $foreignBackup -- $($_.Exception.Message)"
            Write-Warning "Your original is still on disk under that name. Rename it back to dinput8.dll."
        }
    } elseif ($foreignBackup -and (Test-Path $foreignBackup)) {
        # Ours is still in place -- -KeepDll, or a game still holding it. Say so, because a
        # stray .wh33lh4x-backup with no explanation looks like a bug.
        Write-Warning "The original dinput8.dll is still parked at $foreignBackup."
        Write-Warning "It goes back automatically once our shim is removed."
    }
}
