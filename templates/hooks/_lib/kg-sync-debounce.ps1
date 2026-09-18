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
#
# The spawn itself goes through `Start-VcoDetachedPwsh` in
# _lib/resolve-powershell.ps1 — the ONE home for the `-WindowStyle Hidden`
# capability guard. Unguarded, that parameter is REJECTED by non-Windows
# PowerShell editions and the whole spawn dies, which both loses the work and
# makes this helper untestable on a Linux/macOS host.
function Start-KgDebounceChild {
    param([string]$ChildScript)
    $psExe = Get-KgDebouncePsExe
    # Usable standalone: parent hooks dot-source resolve-powershell.ps1 before
    # this file, but a direct caller may not have.
    if (-not (Get-Command Start-VcoDetachedPwsh -ErrorAction SilentlyContinue)) {
        $lib = Join-Path $PSScriptRoot "resolve-powershell.ps1"
        if (Test-Path -LiteralPath $lib) { . $lib }
    }
    Start-VcoDetachedPwsh -Command $ChildScript -PowerShellExe $psExe
}

# ─── failure visibility (v0.2.95) ──────────────────────────────────────────
#
# WHY (field report 2026-09-14): every debounced sync ran with its
# output discarded and its failure swallowed (`catch { }` here, `|| true` in
# the POSIX sibling). A `kg-sync` exit-3 refusal — "I did NOT run, no
# interpreter with VCO's KG dependencies" — was invisible on the edit path,
# and a whole session's knowledge nodes appeared to sync and did not.
#
# The shape mirrors `embedding_failures.jsonl`: full stderr into a
# project-local log, ONE structured row per session per channel into a
# metrics stream, surfaced once by the SessionStart hook
# `session-start-retrieval-health.ps1`.
#
# MUST MATCH _lib/kg-sync-debounce.sh (same log file, same jsonl name, same
# sentinel shape, same one-row-per-session-per-channel rule).

# Bound for the stderr log; past this it is restarted rather than grown
# without limit. MUST MATCH $_KG_DEBOUNCE_LOG_MAX_BYTES in the .sh sibling.
$script:KgDebounceLogMaxBytes = 262144

# Absolute path of the stderr log for $ProjectRoot, after ensuring the
# directory exists and the file is inside its size bound. "" when unusable —
# callers then discard stderr, exactly as before this existed.
function Get-KgDebounceLogPath {
    param([string]$ProjectRoot)
    if (-not $ProjectRoot) { return "" }
    $dir = Join-Path $ProjectRoot ".claude/logs"
    try {
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) {
            New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null
        }
    } catch { return "" }
    $log = Join-Path $dir "kg-sync-hook.log"
    try {
        if (Test-Path -LiteralPath $log -PathType Leaf) {
            if ((Get-Item -LiteralPath $log).Length -gt $script:KgDebounceLogMaxBytes) {
                Set-Content -LiteralPath $log -Value "" -NoNewline -ErrorAction SilentlyContinue
            }
        }
    } catch { }
    return $log
}

# The session key the failure row is deduped by. Sanitised to the same
# [A-Za-z0-9_-] charset `_lib/session-id.ps1` enforces (it is interpolated
# into a FILE NAME); anything else collapses to "default", an absent id to
# "nosession". MUST MATCH _kg_debounce_session_key in the .sh sibling.
function Get-KgDebounceSessionKey {
    $raw = $env:VCT_SESSION_ID
    if (-not $raw) { $raw = $env:CLAUDE_SESSION_ID }
    if (-not $raw) { return "nosession" }
    if ($raw -match '^[A-Za-z0-9_-]+$') { return $raw }
    return "default"
}

# Record ONE failure row for this session+channel. Best-effort in every
# direction: an unresolvable metrics dir, an unwritable sentinel or a JSON
# error all end in "no row", never in a failed sync path.
function Write-KgDebounceFailureRow {
    param(
        [string]$ProjectRoot,
        [string]$Channel,
        [int]$ExitCode,
        [string]$LogPath
    )
    if (-not $ProjectRoot) { return }
    try {
        $stateDir = Join-Path $ProjectRoot ".claude/state"
        if (-not (Test-Path -LiteralPath $stateDir -PathType Container)) {
            New-Item -ItemType Directory -Force -Path $stateDir -ErrorAction Stop | Out-Null
        }
        $sentinel = Join-Path $stateDir ("kg_sync_failure_{0}_{1}" -f (Get-KgDebounceSessionKey), $Channel)
        if (Test-Path -LiteralPath $sentinel) { return }

        if (-not (Get-Command Get-VcoMetricsDir -ErrorAction SilentlyContinue)) {
            $mlib = Join-Path $PSScriptRoot "metrics-dir.ps1"
            if (-not (Test-Path -LiteralPath $mlib)) { return }
            . $mlib
        }
        $metricsDir = Get-VcoMetricsDir
        if (-not $metricsDir) { return }

        $tail = ""
        if ($LogPath -and (Test-Path -LiteralPath $LogPath -PathType Leaf)) {
            $lines = @(Get-Content -LiteralPath $LogPath -ErrorAction SilentlyContinue |
                       Where-Object { $_ -and $_.Trim() })
            if ($lines.Count -gt 0) {
                $tail = [string]$lines[-1]
                if ($tail.Length -gt 300) { $tail = $tail.Substring(0, 300) }
            }
        }
        $row = [ordered]@{
            ts           = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            kind         = "kg_sync_failed"
            project_root = $ProjectRoot
            channel      = $Channel
            exit         = $ExitCode
            session      = (Get-KgDebounceSessionKey)
            log          = $LogPath
            last_stderr  = $tail
        }
        $json = ($row | ConvertTo-Json -Compress -Depth 4)
        Add-Content -LiteralPath (Join-Path $metricsDir "kg_sync_failures.jsonl") `
            -Value $json -ErrorAction Stop
        # Written only AFTER the row landed, so a failed append is retried by
        # the next failing edit rather than silently marked reported.
        Set-Content -LiteralPath $sentinel -Value "" -NoNewline -ErrorAction SilentlyContinue
    } catch { }
}

# THE one runner every debounced sync goes through: cd, run with stderr
# captured to the log, record a row when it failed. Never throws.
function Invoke-KgDebounceRunCommand {
    param([string]$WorkingDir, [string]$Channel, [string]$Command)
    if (-not $Command) { return }
    if (-not $Channel) { $Channel = "kg" }
    if ($WorkingDir) { Set-Location -LiteralPath $WorkingDir -ErrorAction SilentlyContinue }
    $log = Get-KgDebounceLogPath -ProjectRoot $WorkingDir
    $code = 0
    try {
        $global:LASTEXITCODE = 0
        if ($log) {
            # stdout discarded (progress chatter nobody reads here); stderr
            # APPENDED to the log, which is the half that carries refusals.
            Invoke-Expression $Command 2>>$log | Out-Null
        } else {
            Invoke-Expression $Command 2>&1 | Out-Null
        }
        if ($null -ne $LASTEXITCODE) { $code = [int]$LASTEXITCODE }
    } catch {
        # A PowerShell-level throw (bad command, missing exe) is a failure
        # with no exit code of its own; record it as 1 and put the message in
        # the log so the notice has something to quote.
        $code = 1
        if ($log) {
            try { Add-Content -LiteralPath $log -Value ([string]$_) -ErrorAction SilentlyContinue } catch { }
        }
    }
    if ($code -ne 0) {
        Write-KgDebounceFailureRow -ProjectRoot $WorkingDir -Channel $Channel `
            -ExitCode $code -LogPath $log
    }
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
    # Absolute path of THIS lib, embedded so the detached child can dot-source
    # it (mirrors $_KG_DEBOUNCE_LIB in the POSIX sibling).
    $libEsc = ((Join-Path $PSScriptRoot "kg-sync-debounce.ps1") -replace "'", "''")
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
# v0.2.95: the run goes through the ONE runner, which captures the child's
# stderr and records a failure row. The child is a SEPARATE process, so it
# dot-sources this lib to reach the runner — the same re-source-with-fallback
# shape the POSIX sibling's flusher uses. Falling back to the bare
# Invoke-Expression keeps the sync running (failure merely stays invisible)
# when the lib is unreadable; dropping the sync would not be acceptable.
`$chan = (`$base -split '_', 2)[0]
if (-not `$chan) { `$chan = 'kg' }
`$dbLib = '$libEsc'
if (`$cmd) {
    if (`$dbLib -and (Test-Path -LiteralPath `$dbLib)) {
        try { . `$dbLib; Invoke-KgDebounceRunCommand -WorkingDir `$wd -Channel `$chan -Command `$cmd }
        catch { try { Invoke-Expression `$cmd } catch { } }
    } else {
        try { Invoke-Expression `$cmd } catch { }
    }
}
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

# Reaper throttle interval, in seconds. The reaper globs ALL pending work
# dirs (O(M^2) across a long-lived session), so running it on EVERY schedule
# call is wasteful under edit bursts. 30s = the grace jitter budget
# ($window + 30), so even a throttled reaper still fires within one grace
# window — an orphan is recovered with at most ~grace extra delay, inside the
# eventually-consistent contract.
# MUST MATCH _lib/kg-sync-debounce.sh ($_KG_DEBOUNCE_REAP_THROTTLE_SECONDS).
$script:KgDebounceReapThrottleSeconds = 30

# Max number of live ".lock" dirs before we stop SCHEDULING new sleeping
# flushers and instead run the sync IMMEDIATELY (detached). The common burst
# is a handful (3-10) of distinct (file,channel) pairs; 64 simultaneous
# pending locks is a pathological fan-out far above any normal agent edit
# burst, so this ceiling never trips on the common case. Above it we fall
# through to immediate-sync — NEVER dropping a sync, only halting the
# accumulation of sleeping flushers.
# MUST MATCH _lib/kg-sync-debounce.sh ($_KG_DEBOUNCE_LOCK_CEILING).
$script:KgDebounceLockCeiling = 64

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
#
# v0.2.95: routed through the same runner as the lock paths, so a failure on
# the immediate path is recorded too. A failure that is visible only when the
# debounce window happens to be non-zero is not visible.
function Start-KgDebounceImmediate {
    param([string]$WorkingDir, [string]$Command, [string]$Channel = "kg")
    $wdEsc = ($WorkingDir -replace "'", "''")
    $cmdEsc = ($Command -replace "'", "''")
    $chanEsc = ($Channel -replace "'", "''")
    $libEsc = ((Join-Path $PSScriptRoot "kg-sync-debounce.ps1") -replace "'", "''")
    $childScript = @"
`$dbLib = '$libEsc'
if (`$dbLib -and (Test-Path -LiteralPath `$dbLib)) {
    try { . `$dbLib; Invoke-KgDebounceRunCommand -WorkingDir '$wdEsc' -Channel '$chanEsc' -Command '$cmdEsc' }
    catch {
        if ('$wdEsc') { Set-Location -LiteralPath '$wdEsc' -ErrorAction SilentlyContinue }
        try { $Command } catch { }
    }
} else {
    if ('$wdEsc') { Set-Location -LiteralPath '$wdEsc' -ErrorAction SilentlyContinue }
    try { $Command } catch { }
}
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

    # Reaper throttle: skip the O(M^2) sweep if we ran it within the last
    # ~30s. Mirrors the diagram-throttle stamp-file pattern in
    # post-file-edit.sh (~line 336) and the bash sibling's `.last_reap.ts`.
    # SAFE for coalesce-NEVER-DROP: orphans are recovered by the very NEXT
    # un-throttled reap (at most ~throttle extra delay), well inside the
    # eventually-consistent contract. Best-effort: a stat/write failure falls
    # through to running the reap (fail toward MORE recovery).
    $stamp = Join-Path $dir '.last_reap.ts'
    $lastReap = 0
    if (Test-Path -LiteralPath $stamp) {
        try {
            $raw = (Get-Content -LiteralPath $stamp -TotalCount 1 -ErrorAction Stop)
            if ($raw -match '^[0-9]+$') { $lastReap = [long]$raw }
        } catch { $lastReap = 0 }
    }
    # TRUE Unix seconds. MUST MATCH the .sh sibling's `date +%s` — both
    # siblings write and read the SAME `.last_reap.ts` (see the comment just
    # above), so a timezone-shifted value here breaks the throttle across
    # them. `$now.ToUniversalTime() - [datetime]'1970-01-01T00:00:00Z'` was
    # off by one UTC offset: [datetime]'...Z' parses to a LOCAL-kind instant,
    # and .NET subtracts raw ticks ignoring DateTimeKind, so mixing kinds
    # never errors — it silently returns the wrong number. East of UTC the
    # .ps1 then read a .sh stamp as `now - last == -offset`, which is below
    # the throttle, so the reaper NEVER ran and orphaned lock dirs
    # accumulated. [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() has no kind
    # to get wrong (.NET 4.6+ / PS 5.1 on Win10+).
    $nowEpoch = [long][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    if ($lastReap -gt 0 -and (($nowEpoch - $lastReap) -lt $script:KgDebounceReapThrottleSeconds)) {
        return
    }
    # Stamp BEFORE sweeping so concurrent schedule calls during this sweep
    # also see the throttle (avoids a thundering-herd of parallel reaps).
    try { Set-Content -LiteralPath $stamp -Value ([string]$nowEpoch) -Encoding ascii -ErrorAction SilentlyContinue } catch { }

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

    # Item 3 (wd TAB/newline guard): the cmd-file line is persisted as
    # "{0}`t{1}" -f $WorkingDir, $Command and the reaper splits it on TAB +
    # reads only the FIRST line. A $WorkingDir containing a literal TAB or
    # newline would corrupt that record (TAB → wrong split; newline → cmd
    # truncated). $WorkingDir is always PROJECT_ROOT today (no control
    # chars), so this can't fire now — free defense-in-depth against a future
    # hostile path. Strip (not reject): a slightly-wrong dir still beats
    # dropping the sync (the cmd re-derives paths from the repo root anyway).
    # MIRRORS the bash sibling's `tr -d '\t\n'` guard.
    if ($WorkingDir -match "[`t`n]") {
        $WorkingDir = ($WorkingDir -replace "[`t`n]", "")
    }

    # Debounce disabled → preserve legacy "sync immediately" behaviour.
    if ($window -eq 0) {
        Start-KgDebounceImmediate -WorkingDir $WorkingDir -Command $Command -Channel $Channel
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
        Start-KgDebounceImmediate -WorkingDir $WorkingDir -Command $Command -Channel $Channel
        return
    }

    # Item 2a (flusher cap): if too many flushers are already pending (one
    # ".lock" dir each), stop ACCUMULATING sleeping flusher processes and
    # instead sync THIS edit immediately (detached) in the background.
    # CRITICAL: this NEVER drops a sync — it only swaps "schedule a sleeping
    # flusher" for "run the sync now"; the already-scheduled flushers still
    # sync their own files. MIRRORS the bash sibling's ceiling fall-through.
    $lockCount = @(Get-ChildItem -LiteralPath $dir -Directory -Filter '*.lock' -ErrorAction SilentlyContinue).Count
    if ($lockCount -ge $script:KgDebounceLockCeiling) {
        Start-KgDebounceImmediate -WorkingDir $WorkingDir -Command $Command -Channel $Channel
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
