# kg-sync-debounce.ps1 — coalesce rapid re-edits of the same file into one
# Weaviate-write per quiet-window. PowerShell 7 sibling of
# _lib/kg-sync-debounce.sh; identical semantics.
#
# WHY THIS EXISTS (write-amplification, 2026-06-18)
# -------------------------------------------------
# post-file-edit.ps1 fired an IMMEDIATE background sync (kg-sync /
# upload_docs.py / code-graph-incremental) on EVERY edit. An agent that
# edits the same file 5x in a minute therefore produced 5 Weaviate
# upserts of (mostly) the same object — a primary driver of write
# amplification. The Weaviate-side tuning landed in d43acf1f; this is
# the complementary app-layer fix: emit fewer, coalesced syncs.
#
# CORRECTNESS ARGUMENT (the final state ALWAYS syncs)
# ---------------------------------------------------
# Debounce = COALESCE rapid repeats, never DROP a sync.
#   * First edit of a file atomically claims a per-file lock directory
#     under <ProjectRoot>/.claude/state/kg_sync_pending/<key>.lock
#     (New-Item -ItemType Directory is the atomic claim) and starts ONE
#     background flusher job: sleep N, then run the real sync.
#   * The flusher removes the lock at the START of the sync. So:
#       - A re-edit DURING the sleep window finds the lock present ->
#         no-op; the pending flusher syncs the file. The sync command
#         re-reads the file FROM DISK at run time -> latest content.
#       - A re-edit AFTER the lock cleared schedules a FRESH flusher ->
#         the post-window edit also syncs. No edit is ever lost.
#   * A file edited once then left alone syncs exactly once, N seconds
#     later. Bounded delay, never "never".
#
# CRASH-SAFETY / NO-ORPHANS
# -------------------------
# The flusher is a DETACHED process (Start-Process pwsh -EncodedCommand,
# NOT Start-Job): a short-lived sleep + one sync, not a daemon, so no
# zombie pool. Start-Process is used precisely BECAUSE Start-Job ties the
# child to the parent runspace — a PostToolUse hook exits within
# milliseconds, which would kill a job still in its sleep window and
# leave the file un-synced. The detached process survives the hook exit,
# mirroring the POSIX sibling's reparent-to-init background subshell. If
# a flusher still dies mid-sleep (hard kill) its lock is left behind;
# every call first runs Invoke-KgDebounceReapStale, which deletes any
# lock older than N+GRACE seconds and runs its recorded sync NOW. The
# next edit to ANY debounced file recovers ALL abandoned pending syncs,
# so nothing is left permanently un-synced.
#
# TUNING
# ------
#   VCO_KG_SYNC_DEBOUNCE_SECONDS  quiet-window in seconds (default 5).
#                                 0 disables debounce (every edit syncs
#                                 immediately — pre-2026-06-18 behaviour).

# Resolve the PowerShell binary to relaunch detached children with. The
# parent hook dot-sources _lib/resolve-powershell.ps1 which sets $PsExe
# (pwsh → powershell 5.1 fallback); fall back to "pwsh" only if unset so
# this helper is usable standalone.
function Get-KgDebouncePsExe {
    if ($script:PsExe)              { return $script:PsExe }
    if (Get-Variable -Name PsExe -Scope Global -ErrorAction SilentlyContinue) { return $global:PsExe }
    return "pwsh"
}

# Launch a sync command as a DETACHED OS process (not Start-Job). WHY:
# Start-Job children are tied to the parent runspace lifetime — a
# PostToolUse hook process exits within milliseconds, which would kill a
# job still in its Start-Sleep window and leave the file un-synced
# (the reaper would recover it only on the NEXT edit). Start-Process
# spawns an independent process that survives the hook's exit, matching
# the POSIX sibling's reparent-to-init background subshell. This is the
# same -WindowStyle Hidden detach the rest of post-file-edit.ps1 already
# uses for its syncs.
#   -SleepSeconds  seconds to wait before syncing (0 = immediate)
#   -LockPath      lock dir to remove before syncing ("" = none)
#   -WorkingDir    cwd for the sync
#   -Command       sync expression to Invoke-Expression
function Start-KgDebounceDetached {
    param(
        [int]$SleepSeconds,
        [string]$LockPath,
        [string]$WorkingDir,
        [string]$Command
    )
    $psExe = Get-KgDebouncePsExe
    $wdEsc   = ($WorkingDir -replace "'", "''")
    $lockEsc = ($LockPath   -replace "'", "''")
    # The child script: sleep, drop the lock (so subsequent edits re-arm),
    # cd, then run the sync. $Command is already a fully self-contained
    # PowerShell expression (built by the caller with values escaped).
    $childScript = @"
Start-Sleep -Seconds $SleepSeconds
if ('$lockEsc') { Remove-Item -LiteralPath '$lockEsc' -Recurse -Force -ErrorAction SilentlyContinue }
if ('$wdEsc') { Set-Location -LiteralPath '$wdEsc' -ErrorAction SilentlyContinue }
try { $Command } catch { }
"@
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($childScript)
    $encoded = [Convert]::ToBase64String($bytes)
    Start-Process -FilePath $psExe `
        -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) `
        -WindowStyle Hidden -ErrorAction SilentlyContinue | Out-Null
}

function Get-KgDebounceWindow {
    $n = $env:VCO_KG_SYNC_DEBOUNCE_SECONDS
    if (-not $n) { return 5 }
    if ($n -notmatch '^[0-9]+$') { return 5 }  # non-numeric → safe default
    return [int]$n
}

function Get-KgDebounceDir {
    param([string]$ProjectRoot)
    return (Join-Path $ProjectRoot ".claude/state/kg_sync_pending")
}

# Hash a file path → slash-free lock key (MD5 hex), matching the bash
# sibling's md5-via-Python key and the diagram-throttle MD5 pattern.
function Get-KgDebounceKey {
    param([string]$Path)
    $md5 = [System.Security.Cryptography.MD5]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Path)
        $digest = $md5.ComputeHash($bytes)
        return (-join ($digest | ForEach-Object { $_.ToString('x2') }))
    } finally {
        $md5.Dispose()
    }
}

# Reap abandoned locks (flusher died mid-sleep): delete any lock older
# than window+GRACE and run its recorded sync NOW (background), so no
# file is left permanently un-synced.
function Invoke-KgDebounceReapStale {
    param([string]$ProjectRoot)
    $dir = Get-KgDebounceDir -ProjectRoot $ProjectRoot
    if (-not (Test-Path $dir)) { return }
    $window = Get-KgDebounceWindow
    $grace = $window + 30   # window + 30s jitter budget
    $now = Get-Date

    $locks = @(Get-ChildItem -LiteralPath $dir -Directory -Filter '*.lock' -ErrorAction SilentlyContinue)
    foreach ($lock in $locks) {
        $age = ($now - $lock.LastWriteTime).TotalSeconds
        if ($age -ge $grace) {
            $cmdFile = Join-Path $lock.FullName "cmd"
            $wd = $null; $cmd = $null
            if (Test-Path $cmdFile) {
                try {
                    $line = (Get-Content -LiteralPath $cmdFile -TotalCount 1 -ErrorAction Stop)
                    $parts = $line -split "`t", 2
                    if ($parts.Count -ge 2) { $wd = $parts[0]; $cmd = $parts[1] }
                } catch { }
            }
            # Remove the lock FIRST so a concurrent reaper can't double-run.
            Remove-Item -LiteralPath $lock.FullName -Recurse -Force -ErrorAction SilentlyContinue
            if ($cmd) {
                # Run detached + immediately (lock already removed, no
                # sleep) so reaping never blocks the live edit and the
                # recovered sync survives this hook's exit.
                Start-KgDebounceDetached -SleepSeconds 0 -LockPath "" -WorkingDir $wd -Command $cmd
            }
        }
    }
}

# Schedule a debounced sync for one file.
#   -ProjectRoot  project root (state dir lives under .claude/state/)
#   -FilePath     edited file path (used to derive the lock key)
#   -WorkingDir   directory the sync command must run in
#   -Command      the sync command string (Invoke-Expression'd at flush time)
#   -Channel      sync-type tag (e.g. "kg" / "docs" / "code") — namespaces
#                 the lock so the SAME file routed to two different sync
#                 targets gets two independent debounce locks instead of
#                 one clobbering the other.
#
# If a flush is already pending for this (file, channel) → no-op (the
# pending flush picks up the latest content). Otherwise atomically claim
# the lock and start a single sleep-N-then-sync background job. When
# window==0 runs the sync immediately in background (debounce disabled).
function Invoke-KgDebounceSchedule {
    param(
        [string]$ProjectRoot,
        [string]$FilePath,
        [string]$WorkingDir,
        [string]$Command,
        [string]$Channel = "kg"
    )
    $window = Get-KgDebounceWindow

    # Debounce disabled → preserve legacy "sync immediately" behaviour.
    if ($window -eq 0) {
        Start-KgDebounceDetached -SleepSeconds 0 -LockPath "" -WorkingDir $WorkingDir -Command $Command
        return
    }

    # Recover orphaned pending syncs before scheduling a new one.
    Invoke-KgDebounceReapStale -ProjectRoot $ProjectRoot

    $dir = Get-KgDebounceDir -ProjectRoot $ProjectRoot
    try {
        if (-not (Test-Path $dir)) {
            New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null
        }
    } catch {
        # Can't create state dir → fail OPEN to legacy path so a
        # permission problem never silently drops the sync.
        Start-KgDebounceDetached -SleepSeconds 0 -LockPath "" -WorkingDir $WorkingDir -Command $Command
        return
    }

    $key = "${Channel}_" + (Get-KgDebounceKey -Path $FilePath)
    $lock = Join-Path $dir "$key.lock"

    # Atomic claim: New-Item -ItemType Directory throws if it already
    # exists, so exactly one racer wins; the loser is a coalesced no-op.
    $claimed = $false
    try {
        New-Item -ItemType Directory -Path $lock -ErrorAction Stop | Out-Null
        $claimed = $true
    } catch {
        $claimed = $false  # lock already held → flush pending → no-op
    }

    if ($claimed) {
        # Record the command so a reaper can recover it if we die.
        try {
            Set-Content -LiteralPath (Join-Path $lock "cmd") `
                -Value ("{0}`t{1}" -f $WorkingDir, $Command) `
                -Encoding utf8 -ErrorAction SilentlyContinue
        } catch { }
        # Single flusher: a DETACHED process that sleeps the quiet-window,
        # removes the lock (so subsequent edits re-arm), then runs the
        # sync (re-reads the file fresh → latest content). Detached so it
        # survives this hook's near-immediate exit (a Start-Job would be
        # killed with the parent runspace before the sleep elapsed).
        Start-KgDebounceDetached -SleepSeconds $window -LockPath $lock -WorkingDir $WorkingDir -Command $Command
    }
}
