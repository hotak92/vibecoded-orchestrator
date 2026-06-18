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
# every call first runs Invoke-KgDebounceReapStale, which recovers any
# lock older than N+GRACE seconds and runs its recorded sync NOW. The
# next edit to ANY debounced file recovers ALL abandoned pending syncs,
# so nothing is left permanently un-synced.
#
# EXACTLY-ONCE UNDER CONCURRENCY (the atomic-claim invariant, 2026-06-18)
# ----------------------------------------------------------------------
# Both the normal flusher-completion path AND the reaper recover a lock
# through the SAME atomic step: rename the lock dir aside to
# "<lock>.claimed.<pid>" via Move-Item (atomic on NTFS same-dir; throws
# if the source is already gone). For a given source path EXACTLY ONE
# caller's Move-Item succeeds; every other caller catches the throw and
# no-ops. So for each scheduled sync there is exactly one winner that
# runs the cmd, regardless of how many processes (a live flusher + one or
# more concurrent reapers) race to recover the lock. This closes two
# races the old "read cmd, then Remove-Item lock" order left open:
#   * Two concurrent reapers (two parallel agents editing different files
#     fire two schedule calls → two reap passes) could both read the
#     persisted cmd of the SAME orphan before either removed it, then
#     both run it → 2 redundant upserts. Now both attempt the same
#     Move-Item; one wins, the other throws → 1 upsert.
#   * A LIVE flusher whose Start-Sleep stretched past window+GRACE
#     (suspend/resume, heavy load, clock jump) still holds its lock, so
#     the reaper treats it as orphaned and recovers it while the
#     still-alive flusher also runs its cmd → 2 upserts. Now the
#     flusher's own completion goes through the SAME Move-Item claim:
#     whichever of {the woken flusher, the reaper} wins runs the cmd; the
#     loser throws and no-ops → 1 upsert.
# NOTE on $PID: unlike the POSIX sibling (where a backgrounded subshell's
# $$ is the PARENT shell, defeating a liveness check), each detached child
# here is a SEPARATE OS process via Start-Process, so its $PID is its own
# real pid — the ".claimed.<pid>" stamp is liveness-accurate without any
# re-stamp dance.
#
# RESIDUAL: claimed-by-dead-pid / stranded-reaping
#   A process can die between the successful Move-Item and the
#   Invoke-Expression. The reaper therefore ALSO sweeps stale
#   ".claimed.<pid>" dirs: if the owning <pid> is no longer alive
#   (Get-Process -Id fails) the claim is dead → re-claim and run. A
#   still-alive <pid> is an in-flight claim → left untouched. The
#   intermediate ".reaping.<token>" dir (used only to win the race before
#   re-stamping to the real pid) is likewise swept by age if stranded.
#   The grace clock is RESTARTED (LastWriteTime refreshed) at each claim
#   transition so an in-flight claim is never re-tripped as stranded.
#   (PID-reuse is a theoretical false-"alive": worst case a dead claim
#   waits until the reused pid exits, then recovers — never dropped, only
#   delayed, matching the eventually-consistent contract.)
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

# Internal: launch an EncodedCommand child as a DETACHED OS process (not
# Start-Job). WHY Start-Process: Start-Job children are tied to the parent
# runspace lifetime — a PostToolUse hook exits within milliseconds, which
# would kill a job still in its Start-Sleep window and leave the file
# un-synced. Start-Process spawns an independent process that survives the
# hook's exit, matching the POSIX sibling's reparent-to-init subshell.
function Start-KgDebounceChild {
    param([string]$ChildScript)
    $psExe = Get-KgDebouncePsExe
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($ChildScript)
    $encoded = [Convert]::ToBase64String($bytes)
    Start-Process -FilePath $psExe `
        -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) `
        -WindowStyle Hidden -ErrorAction SilentlyContinue | Out-Null
}

# Shared child-script fragment that ATOMICALLY CLAIMS a work dir, runs its
# recorded cmd EXACTLY ONCE, and cleans up — emitted into every detached
# child so the flusher's normal completion and the reaper's recovery use
# the identical exactly-once primitive. The child re-stamps the won dir to
# ".claimed.<its-own-$PID>" (a real, liveness-accurate pid because each
# detached child is its OWN OS process), refreshes LastWriteTime (rename
# preserves mtime; refresh restarts the grace clock so an in-flight claim
# is never re-tripped by a concurrent reaper's stranded-sweep), runs the
# cmd, then removes the claim only AFTER the cmd finishes (a crash mid-run
# leaves a recoverable ".claimed.<pid>" rather than dropping the sync).
#   $WonExpr   — a PS expression (already-escaped literal) for the won dir
#   $StateExpr — expression for the state dir
#   $BaseExpr  — expression for the lock base name (e.g. "kg_<md5>.lock")
function Get-KgDebounceRunWonFragment {
    param([string]$WonExpr, [string]$StateExpr, [string]$BaseExpr)
    return @"
`$won  = $WonExpr
`$st   = $StateExpr
`$base = $BaseExpr
`$claimed = Join-Path `$st ("{0}.claimed.{1}" -f `$base, `$PID)
try { Move-Item -LiteralPath `$won -Destination `$claimed -ErrorAction Stop }
catch { `$claimed = `$won }   # already ours; run in place
# Refresh mtime → restart the grace clock at this claim transition.
try { (Get-Item -LiteralPath `$claimed -ErrorAction Stop).LastWriteTime = Get-Date } catch { }
`$wd = `$null; `$cmd = `$null
`$cmdFile = Join-Path `$claimed 'cmd'
if (Test-Path `$cmdFile) {
    try {
        `$line = (Get-Content -LiteralPath `$cmdFile -TotalCount 1 -ErrorAction Stop)
        `$parts = `$line -split "``t", 2
        if (`$parts.Count -ge 2) { `$wd = `$parts[0]; `$cmd = `$parts[1] }
    } catch { }
}
if (`$wd) { Set-Location -LiteralPath `$wd -ErrorAction SilentlyContinue }
if (`$cmd) { try { Invoke-Expression `$cmd } catch { } }
Remove-Item -LiteralPath `$claimed -Recurse -Force -ErrorAction SilentlyContinue
"@
}

# Launch the FLUSHER: a detached child that sleeps the quiet-window, then
# atomically claims its OWN lock (Move-Item lock → ".reaping.<token>") and
# runs it via the shared run-won fragment. The completion-time Move-Item is
# the exactly-once gate: if a reaper already claimed this lock (the
# flusher's sleep stretched past grace), the Move-Item throws and the
# flusher no-ops; otherwise the flusher wins and runs the cmd once.
#   -SleepSeconds  quiet-window before claiming
#   -LockPath      the lock dir to claim
#   -StateDir      the state dir (where .reaping/.claimed land)
#   -Base          the lock base name
function Start-KgDebounceFlusher {
    param(
        [int]$SleepSeconds,
        [string]$LockPath,
        [string]$StateDir,
        [string]$Base
    )
    $lockEsc  = ($LockPath -replace "'", "''")
    $stEsc    = ($StateDir -replace "'", "''")
    $baseEsc  = ($Base     -replace "'", "''")
    $runFrag = Get-KgDebounceRunWonFragment -WonExpr '$won' -StateExpr "'$stEsc'" -BaseExpr "'$baseEsc'"
    $childScript = @"
Start-Sleep -Seconds $SleepSeconds
`$tok = "{0}.{1}" -f `$PID, (Get-Random)
`$won = '$lockEsc' + '.reaping.' + `$tok
try { Move-Item -LiteralPath '$lockEsc' -Destination `$won -ErrorAction Stop }
catch { return }   # a reaper already claimed this lock → no double-run
try { (Get-Item -LiteralPath `$won -ErrorAction Stop).LastWriteTime = Get-Date } catch { }
$runFrag
"@
    Start-KgDebounceChild -ChildScript $childScript
}

# Launch a RUNNER on an ALREADY-WON ".reaping.<token>" dir (reaper path).
# The win (Move-Item to the .reaping name) happened in the parent; this
# child just re-stamps to its real pid and runs, via the shared fragment.
#   -WonPath   the ".reaping.<token>" dir this caller already owns
#   -StateDir  state dir
#   -Base      lock base name
function Start-KgDebounceRunWon {
    param([string]$WonPath, [string]$StateDir, [string]$Base)
    $wonEsc  = ($WonPath  -replace "'", "''")
    $stEsc   = ($StateDir -replace "'", "''")
    $baseEsc = ($Base     -replace "'", "''")
    $runFrag = Get-KgDebounceRunWonFragment -WonExpr "'$wonEsc'" -StateExpr "'$stEsc'" -BaseExpr "'$baseEsc'"
    Start-KgDebounceChild -ChildScript $runFrag
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

# Run a sync command directly, detached + immediately (no lock machinery).
# Used for the debounce-disabled (window==0) and fail-open paths where there
# is no lock to coalesce against — the cmd is built by the caller.
function Start-KgDebounceImmediate {
    param([string]$WorkingDir, [string]$Command)
    $wdEsc = ($WorkingDir -replace "'", "''")
    $childScript = @"
if ('$wdEsc') { Set-Location -LiteralPath '$wdEsc' -ErrorAction SilentlyContinue }
try { $Command } catch { }
"@
    Start-KgDebounceChild -ChildScript $childScript
}

# Reap abandoned work. Three sweeps, all using the SAME atomic Move-Item
# claim so that for any one piece of pending work EXACTLY ONE process runs
# it (mirrors the POSIX sibling):
#   (A) ".lock" dirs older than window+GRACE → orphaned (flusher died
#       mid-sleep, OR its sleep stretched past grace while still alive).
#       Atomically win via Move-Item lock → ".reaping.<token>"; the winner
#       hands off to a detached runner. A concurrent reaper's Move-Item
#       throws (source gone) and a live flusher that later wakes finds its
#       lock renamed away — its completion-time Move-Item also throws, so
#       it no-ops. Exactly one run.
#   (B) ".claimed.<pid>" dirs whose owning <pid> is dead (process died
#       between claim and run). Re-claim and run. A live <pid> is an
#       in-flight claim → left untouched.
#   (C) ".reaping.<token>" dirs older than grace (a runner died between
#       winning the race-rename and re-stamping to ".claimed.<pid>").
#       Recover by age (the token carries the claimer's pid, not the
#       runner's, so it has no liveness meaning here).
# The grace clock is restarted (LastWriteTime refreshed) at each claim
# transition so an in-flight claim is never re-tripped as stranded.
function Invoke-KgDebounceReapStale {
    param([string]$ProjectRoot)
    $dir = Get-KgDebounceDir -ProjectRoot $ProjectRoot
    if (-not (Test-Path $dir)) { return }
    $window = Get-KgDebounceWindow
    $grace = $window + 30   # window + 30s jitter budget
    $now = Get-Date

    # --- Sweep A: orphaned (or stretched-live) ".lock" dirs --------------
    $locks = @(Get-ChildItem -LiteralPath $dir -Directory -Filter '*.lock' -ErrorAction SilentlyContinue)
    foreach ($lock in $locks) {
        $age = ($now - $lock.LastWriteTime).TotalSeconds
        if ($age -ge $grace) {
            $base = $lock.Name                          # "kg_<md5>.lock"
            $tok  = "{0}.{1}" -f $PID, (Get-Random)
            $won  = $lock.FullName + '.reaping.' + $tok
            # ATOMIC CLAIM: Move-Item on NTFS same-dir is atomic and throws
            # if the source is gone → exactly one racer wins. This replaces
            # the old read-cmd-then-Remove-Item order whose comment promised
            # single-recovery but did not deliver it.
            try {
                Move-Item -LiteralPath $lock.FullName -Destination $won -ErrorAction Stop
            } catch { continue }   # another process claimed it → skip
            try { (Get-Item -LiteralPath $won -ErrorAction Stop).LastWriteTime = Get-Date } catch { }
            Start-KgDebounceRunWon -WonPath $won -StateDir $dir -Base $base
        }
    }

    # --- Sweep B: dead-pid ".claimed.<pid>" residuals --------------------
    $claims = @(Get-ChildItem -LiteralPath $dir -Directory -Filter '*.claimed.*' -ErrorAction SilentlyContinue)
    foreach ($c in $claims) {
        if ($c.Name -notmatch '\.claimed\.([0-9]+)$') { continue }   # malformed → skip
        $cpid = [int]$Matches[1]
        $age = ($now - $c.LastWriteTime).TotalSeconds
        if ($age -lt $grace) { continue }   # too fresh → leave in flight
        # Owning pid still alive → in-flight claim → leave.
        if (Get-Process -Id $cpid -ErrorAction SilentlyContinue) { continue }
        $base = ($c.Name -replace '\.claimed\.[0-9]+$', '')   # strip ".claimed.<pid>"
        $tok  = "{0}.{1}" -f $PID, (Get-Random)
        $won  = (Join-Path $dir ($base + '.reaping.' + $tok))
        try { Move-Item -LiteralPath $c.FullName -Destination $won -ErrorAction Stop }
        catch { continue }   # another reaper re-claimed it → skip
        try { (Get-Item -LiteralPath $won -ErrorAction Stop).LastWriteTime = Get-Date } catch { }
        Start-KgDebounceRunWon -WonPath $won -StateDir $dir -Base $base
    }

    # --- Sweep C: stranded ".reaping.<token>" residuals ------------------
    $reaping = @(Get-ChildItem -LiteralPath $dir -Directory -Filter '*.reaping.*' -ErrorAction SilentlyContinue)
    foreach ($r in $reaping) {
        $age = ($now - $r.LastWriteTime).TotalSeconds
        if ($age -lt $grace) { continue }   # too fresh → live re-stamp in flight
        $base = ($r.Name -replace '\.reaping\..*$', '')   # strip ".reaping.<token>"
        $tok  = "{0}.{1}" -f $PID, (Get-Random)
        $won  = (Join-Path $dir ($base + '.reaping.' + $tok))
        try { Move-Item -LiteralPath $r.FullName -Destination $won -ErrorAction Stop }
        catch { continue }
        try { (Get-Item -LiteralPath $won -ErrorAction Stop).LastWriteTime = Get-Date } catch { }
        Start-KgDebounceRunWon -WonPath $won -StateDir $dir -Base $base
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
        Start-KgDebounceImmediate -WorkingDir $WorkingDir -Command $Command
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
        Start-KgDebounceImmediate -WorkingDir $WorkingDir -Command $Command
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
        $base = Split-Path -Leaf $lock   # "<chan>_<md5>.lock"
        # Single flusher: a DETACHED process that sleeps the quiet-window,
        # then claims its OWN lock through the SAME atomic Move-Item the
        # reaper uses (lock → ".reaping.<token>") and runs the sync via the
        # shared run-won fragment (re-reads the file fresh → latest
        # content). The completion-time Move-Item is the exactly-once gate:
        # normally the flusher wins and runs once; if its sleep stretched
        # past window+GRACE a reaper may have already claimed+run it, the
        # flusher's Move-Item throws and it no-ops. The unified path makes
        # the flusher's completion and the reaper's recovery the SAME
        # mechanism, so "exactly once" is structural, not comment-promised.
        # Detached so it survives this hook's near-immediate exit (a
        # Start-Job would be killed with the parent runspace before the
        # sleep elapsed).
        Start-KgDebounceFlusher -SleepSeconds $window -LockPath $lock -StateDir $dir -Base $base
    }
}
