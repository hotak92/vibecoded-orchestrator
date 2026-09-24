# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# session-lock.ps1 - the per-user session-start container lock, PowerShell
# side (v0.2.97, R7a F10). ensure-containers and verify-container-ports both
# act on VCO's containers at session start and are registered `async`, so
# they ran concurrently; now each holds this lock around its "reconcile ->
# plan -> act". The lock FILE is decided by Python
# (`python -m vco_lib.service_lifecycle session-lock-path`, the one home);
# the bash hooks take the same lock through
# `vco_lib.service_lifecycle with-session-lock` (flock). A FileShare.None
# FileStream is the PowerShell form: .NET implements it with the same flock
# on Unix and a share-mode lock on Windows, its handle is not inherited by
# anything the hook starts, and it is released when the hook exits.

function Enter-VcoSessionLock {
    <#
    .SYNOPSIS
      Hold the session lock, waiting up to -WaitSeconds.
    .OUTPUTS
      @{ Held = $true; Stream = <FileStream or $null> } - held (Stream is
      $null only when no lock file could be named: the hook then runs as it
      did before the lock existed); @{ Held = $false } - still busy after
      the wait: the other hook is acting on the containers.
      Keep the returned object referenced until the hook ends.
    #>
    param([Parameter(Mandatory)][string]$RunPy, [double]$WaitSeconds = 20)
    $path = $null
    try {
        $path = ((& $RunPy -m vco_lib.service_lifecycle session-lock-path 2>$null) | Out-String).Trim()
    } catch { $path = $null }
    if (-not $path) { return @{ Held = $true; Stream = $null } }
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ($true) {
        try {
            $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            return @{ Held = $true; Stream = $stream }
        } catch {
            if ((Get-Date) -ge $deadline) { return @{ Held = $false; Stream = $null } }
            Start-Sleep -Milliseconds 200
        }
    }
}
