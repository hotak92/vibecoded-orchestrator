# _lib/pid-live.ps1 -- "is the process that stamped this record still alive?"
#
# One home (v0.2.104) for the pid-reuse-safe liveness check, extracted from
# _lib/kg-sync-debounce.ps1 (where it was born as Test-KgDebouncePidLive,
# v0.2.103 HIGH-3) when pre-tool-use gained a second caller: its in-progress
# markers must tell a sibling hook run that is STILL running from one the
# harness killed. kg-sync-debounce.ps1 keeps its old name as a thin wrapper.
#
# v0.2.103 PID-REUSE GUARD (HIGH-3, review R3 2026-10-14). `Get-Process
# -Id` alone answers "does SOME process have this pid?" -- Windows recycles
# pids fast in a small range (and Get-Process sees every user's and
# session's processes), so a record left by a DEAD process whose pid was
# later handed to an unrelated long-lived process (a SYSTEM service after a
# reboot) used to look "alive" FOREVER.
# A pid now counts as the record's live owner only if the process ALSO
# started at/before the record's stamp (every caller stamps AFTER its owner
# process started, so a genuine owner always satisfies this; a recycled pid
# never does).
# Read-only, never kills anything. Soft-fail policy, decided on evidence:
# when the process exists but its StartTime cannot be read (access denied
# for an elevated process, process exiting) we keep the pre-guard
# semantics (treat as live) -- both callers prefer "live" when unsure.
# MUST MATCH vco_pid_live in _lib/pid-live.sh.
$script:VcoPidLiveSlackSecs = 5
# Tolerance for clock/mtime coarseness between the stamp and the StartTime
# reading; a recycled pid is seconds-to-years later than the stamp. MUST
# MATCH $VCO_PID_LIVE_SLACK_SECS in the .sh sibling.
function Test-VcoPidLive {
    param([int]$OwnerPid, [datetime]$Stamp)
    $p = Get-Process -Id $OwnerPid -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    try { $start = $p.StartTime }
    catch { return $true }   # start unreadable -> soft-fail live (see above)
    return ($start -le $Stamp.AddSeconds($script:VcoPidLiveSlackSecs))
}
