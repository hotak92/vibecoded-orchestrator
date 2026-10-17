# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-stop-reconcile.ps1 — Windows sibling of subagent-stop-reconcile.sh.
# SubagentStop hook that reconciles a subagent's filesystem changes back
# into the project's KG / code-graph / credential-scan / nudge-counter
# state, plus the V52-L.2 JSONL audit row.
#
# V52-L.1 (v0.2.52): side effects, all soft-fail. See .sh sibling for the
# full design rationale. MUST MATCH the .sh sibling, in particular
# (v0.2.102 W6-C2):
#   * DETACHED (census F6/L1): the foreground parses, writes the audit row
#     and hands the payload (0600-style job file under .claude/state) to ONE
#     detached run of this script (`--job <file>`, Start-VcoDetachedProcess).
#     VCO_SUBAGENT_RECONCILE_INLINE=1 runs the job in the foreground.
#   * Step 2 routes knowledge/** + docs/**.md through route-touched-path.ps1
#     (access gate, debounce, detached runner, no timeout) -- no more
#     WaitForExit(30000) + Kill (census L1).
#   * ATTRIBUTION (census F7): an agent with its own worktree is reconciled
#     from that worktree's git state (Get-WtrecChangedFiles); otherwise the
#     shared-tree snapshot diff, recorded as unattributed_shared_tree.
#   * PARENT ALERTS (census F8): kept worktree / violation / credential hit
#     are filed under .claude/state/agent_alerts_<sid>__<agent>.jsonl.
#   * Step 5 holds the nudge's sidecar lock (census F5).

# Scrub sensitive env vars before any subprocess spawning.
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot

# v0.2.54 Track G (G-6): child spawns used a hardcoded `pwsh` (absent on
# PowerShell 5.1-only machines). $PsExe resolves pwsh -> powershell.
. (Join-Path $ScriptDir "_lib/resolve-powershell.ps1")
. (Join-Path $ScriptDir "_lib/utf8-nobom.ps1")  # the one BOM-free writer (review R5-8)
$StderrCap = Join-Path $ScriptDir "_lib/stderr-cap.ps1"
if (Test-Path $StderrCap) { . $StderrCap }

# V52-L.1: source the snapshot + credscan helpers. Optional — when
# missing, fall through to V52-L.2 logging-only behaviour.
$SnapshotHelper = Join-Path $ScriptDir "_lib/snapshot.ps1"
if (Test-Path $SnapshotHelper) { . $SnapshotHelper }
$CredScanHelper = Join-Path $ScriptDir "_lib/credscan.ps1"
if (Test-Path $CredScanHelper) { . $CredScanHelper }
# v0.2.73 HIGH-2: the canonical-root resolver + FIX-A' worktree gate — the SAME
# shared libs the per-edit code-graph hook + the Stop drain use (mirror-don't-
# fork). Optional; when absent Step-3 falls back to CONSERVATIVE enqueue-all.
$CanonHelper = Join-Path $ScriptDir "_lib/canonical-repo-root.ps1"
if (Test-Path $CanonHelper) { . $CanonHelper }
$WorktreeGateHelper = Join-Path $ScriptDir "_lib/worktree-gate.ps1"
if (Test-Path $WorktreeGateHelper) { . $WorktreeGateHelper }

$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$LogDir = Join-Path $ProjectRoot ".claude/logs"
$StateDir = Join-Path $ProjectRoot ".claude/state"
$LogFile = Join-Path $LogDir "subagent-reconciliation.jsonl"
$AlertLog = Join-Path $LogDir "credential_alerts.jsonl"
# v0.2.73 HIGH-2: the session-agnostic drain queue the Stop-hook batched drain
# consumes (replaces the orphan code-graph-queue.jsonl that nothing drained).
$CgDrainShared = Join-Path $StateDir "codegraph_drain_shared.txt"

try {
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force -ErrorAction Stop | Out-Null
    }
} catch {
    exit 0
}
try {
    if (-not (Test-Path $StateDir)) {
        New-Item -ItemType Directory -Path $StateDir -Force -ErrorAction SilentlyContinue | Out-Null
    }
} catch {}

# --job <file>: the DETACHED half. The payload rides a job file, never argv.
$JobFile = ""
if ($args.Count -ge 2 -and [string]$args[0] -eq "--job" -and $args[1]) { $JobFile = [string]$args[1] }
$HookStdin = ""
if ($JobFile) {
    # Only a job file this hook writes is read (and then deleted): a plain
    # file named subagent-reconcile-job.*.json directly in $StateDir. Any
    # other path (a manual run, a diagnostic) is refused (review R4c-9).
    $jobOk = $false
    try {
        $jobFull = [System.IO.Path]::GetFullPath($JobFile)
        $stateFull = [System.IO.Path]::GetFullPath($StateDir).TrimEnd('\', '/')
        $jobName = [System.IO.Path]::GetFileName($jobFull)
        if (([System.IO.Path]::GetDirectoryName($jobFull).TrimEnd('\', '/') -eq $stateFull) -and
            ($jobName -like 'subagent-reconcile-job.*.json')) {
            $jobItem = Get-Item -LiteralPath $jobFull -Force -ErrorAction Stop
            if (-not $jobItem.PSIsContainer -and -not ($jobItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                $jobOk = $true
                $JobFile = $jobFull
            }
        }
    } catch { $jobOk = $false }
    if (-not $jobOk) {
        [Console]::Error.WriteLine("subagent-stop-reconcile: --job accepts only $StateDir/subagent-reconcile-job.*.json; ignored: $JobFile")
        exit 0
    }
    try { $HookStdin = [System.IO.File]::ReadAllText($JobFile) } catch { $HookStdin = "" }
    try { Remove-Item -LiteralPath $JobFile -Force -ErrorAction SilentlyContinue } catch { }
} else {
    try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
}
if (-not $HookStdin) { exit 0 }

# Parse the SubagentStop payload. Field-synonym tolerance for
# transcript_path mirrors the .sh sibling.
#
# v0.2.71 Track T-WT (Layer 3b): also parse the isolation flag + the
# subagent's cwd/worktree path when exposed. Drives the post-hoc
# worktree-violation alert below. Both optional — absent ⇒ no-op.
$SessionId = ""
$AgentId = ""
$AgentType = ""
$TranscriptPath = ""
$StopReason = ""
$IsoFlag = $false
$IsoCwd = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.session_id)              { $SessionId      = [string]$payload.session_id }
        if ($payload.agent_id)                { $AgentId        = [string]$payload.agent_id }
        if ($payload.agent_type)              { $AgentType      = [string]$payload.agent_type }
        if ($payload.agent_transcript_path)   { $TranscriptPath = [string]$payload.agent_transcript_path }
        elseif ($payload.transcript_path)     { $TranscriptPath = [string]$payload.transcript_path }
        if ($payload.finish_reason)           { $StopReason     = [string]$payload.finish_reason }
        elseif ($payload.stop_reason)         { $StopReason     = [string]$payload.stop_reason }

        # Isolation flag + cwd (synonym-tolerant; mirror the .sh sibling).
        function Test-Truthy($v) {
            if ($v -is [bool]) { return $v }
            if ($v -is [string]) {
                return @('worktree','true','1','yes','isolated') -contains $v.Trim().ToLowerInvariant()
            }
            return $false
        }
        if ($payload.PSObject.Properties['isolation']) {
            $iv = $payload.isolation
            if ($iv -is [string] -and $iv.Trim().ToLowerInvariant() -eq 'worktree') { $IsoFlag = $true }
            elseif (Test-Truthy $iv) { $IsoFlag = $true }
        }
        if (-not $IsoFlag) {
            foreach ($f in 'isolation_mode','worktree','isolated','worktree_isolation') {
                if ($payload.PSObject.Properties[$f] -and (Test-Truthy $payload.$f)) { $IsoFlag = $true; break }
            }
        }
        foreach ($f in 'cwd','worktree_path','working_directory','working_dir','dir') {
            if ($payload.PSObject.Properties[$f] -and $payload.$f) { $IsoCwd = [string]$payload.$f; break }
        }
    }
} catch {
    exit 0
}

# Step 1 (V52-L.2 preserved): emit the audit row -- FOREGROUND only.
$entry = [ordered]@{
    timestamp       = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    session_id      = $SessionId
    agent_id        = $AgentId
    agent_type      = $AgentType
    transcript_path = $TranscriptPath
    stop_reason     = $StopReason
}
if (-not $JobFile) {
    try {
        $line = $entry | ConvertTo-Json -Compress -Depth 5
        Add-Content -Path $LogFile -Value $line -ErrorAction Stop
    } catch { }
}


# ----------------------------------------------------------------------
# DETACH (v0.2.102 W6-C2, census F6/L1) - must match the .sh sibling.
# ----------------------------------------------------------------------
$inlineRaw = [string]$env:VCO_SUBAGENT_RECONCILE_INLINE
$Inline = @("1", "true", "yes", "on") -contains $inlineRaw.Trim().ToLowerInvariant()
if (-not $JobFile -and -not $Inline) {
    if (-not $AgentId) { exit 0 }
    # A job file whose job never started is garbage after a day.
    Get-ChildItem -LiteralPath $StateDir -Filter "subagent-reconcile-job.*.json" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    $safeJobId = [regex]::Replace($AgentId, '[^a-zA-Z0-9_-]', '_')
    if ($safeJobId.Length -gt 64) { $safeJobId = $safeJobId.Substring(0, 64) }
    $job = Join-Path $StateDir ("subagent-reconcile-job.{0}.{1}.json" -f $safeJobId, $PID)
    # The job's own output goes to a per-job log, never the hook's stdout /
    # stderr: a child that inherits those keeps the harness's pipe open until
    # the job ends (measured: the foreground "returned" only after the whole
    # job). Per job, so concurrent jobs never share a file; swept after a
    # week. Must match the .sh sibling.
    $jobLogDir = Join-Path $LogDir "subagent-reconcile-jobs"
    $redirect = @{}
    try {
        if (-not (Test-Path -LiteralPath $jobLogDir)) {
            New-Item -ItemType Directory -Force -Path $jobLogDir -ErrorAction Stop | Out-Null
        }
        Get-ChildItem -LiteralPath $jobLogDir -Filter "job.*" -File -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } |
            Remove-Item -Force -ErrorAction SilentlyContinue
        $redirect["RedirectStandardOutput"] = Join-Path $jobLogDir ("job.{0}.{1}.out" -f $safeJobId, $PID)
        $redirect["RedirectStandardError"] = Join-Path $jobLogDir ("job.{0}.{1}.err" -f $safeJobId, $PID)
    } catch { }
    try {
        Set-VcoUtf8Content -Path $job -Value $HookStdin -NoNewline
        $env:CLAUDE_PROJECT_DIR = $ProjectRoot
        Start-VcoDetachedProcess -FilePath $PsExe -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath, "--job", $job) @redirect | Out-Null
    } catch { }
    exit 0
}

# ---- the job (detached; inline under VCO_SUBAGENT_RECONCILE_INLINE) ----
$SessionIdLib = Join-Path $ScriptDir "_lib/session-id.ps1"
if (Test-Path -LiteralPath $SessionIdLib) { . $SessionIdLib }

# File one PARENT-facing alert (census F8). One writer: _lib/agent-alerts.ps1
# -> python -m vco_lib.agent_alerts append (row format + locking live there;
# review R4c-2/R4c-3). No-op without an agent id. A failure (broken install:
# no VCO venv) is never silent: stderr plus a desktop notification carrying
# the alert. Must match the .sh.
$AgentAlertsLib = Join-Path $ScriptDir "_lib/agent-alerts.ps1"
if (Test-Path -LiteralPath $AgentAlertsLib) { . $AgentAlertsLib }
function Add-ReconcileParentAlert([string]$Kind, [string]$Message, [string]$File = "") {
    if (-not $AgentId) { return }
    if ((Get-Command Add-VcoAgentAlert -ErrorAction SilentlyContinue) -and
        (Add-VcoAgentAlert -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot -SessionId $SessionId `
            -AgentId $AgentId -AgentType $AgentType -Kind $Kind -Message $Message -File $File)) {
        return
    }
    [Console]::Error.WriteLine("subagent-stop-reconcile: alert NOT delivered to the parent: $Message")
    $notify = Join-Path $ProjectRoot ".claude/scripts/notify.py"
    $findPy = Join-Path $ScriptDir "_lib/find-python.ps1"
    if (-not $PY -and (Test-Path -LiteralPath $findPy)) { . $findPy }
    $notifyPy = $PY
    if ($notifyPy -and (Test-Path -LiteralPath $notify)) {
        try {
            & $notifyPy $notify "VCO: subagent alert NOT delivered (broken install)" $Message `
                --urgency critical --icon dialog-warning 2>$null | Out-Null
        } catch { }
    }
}

# v0.2.102 W6-R - must match the .sh sibling: the live SubagentStop payload
# carries none of the isolation fields parsed above, so derive the flag from
# worktree-guard's record + the agent's definition (_lib/worktree-records.ps1,
# shared with subagent-start-isolation-check):
#   * record with mode head|snapshot|reservation|reservation_existing =>
#     isolated: stay silent ($IsoFlag off);
#   * no record, guard registered, definition declares `isolation: worktree`
#     => skipped: $IsoFlag on, cwd treated as the parent checkout;
#   * anything else => the payload-derived value stands.
# Read BEFORE Step 1b's reap, which rewrites the record's status.
$WtrecHelper = Join-Path $ScriptDir "_lib/worktree-records.ps1"
if ($AgentId -and (Test-Path -LiteralPath $WtrecHelper)) {
    . $WtrecHelper
    try {
        $isoRecFile = Find-WtrecRecord $ProjectRoot $AgentId
        if ($isoRecFile) {
            $isoRec = Read-WtrecRecord $isoRecFile
            if ($isoRec -and (@("head", "snapshot", "reservation", "reservation_existing") -contains [string]$isoRec.mode)) {
                $IsoFlag = $false
                # W6-C2 F7: this agent worked in its OWN worktree.
                $AgentWtIsolated = $true
            }
        } elseif (Test-WtrecIsolationSkipped $ProjectRoot $AgentId $AgentType) {
            $IsoFlag = $true
            $IsoCwd = ""
        }
    } catch { }
}

# Step 1b (v0.2.102 W6-Q follow-up) - must match the .sh sibling: reap the
# agent's worktree when it is provably unchanged (the harness neither removes
# a hook-created worktree nor runs WorktreeRemove when a subagent finishes).
# Conditions live in _lib/worktree-records.ps1 :: Invoke-WtrecReap. A kept
# worktree is logged with path + reason and named on stderr. Never blocks.
$WtrecHelper = Join-Path $ScriptDir "_lib/worktree-records.ps1"
if ($AgentId -and (Test-Path -LiteralPath $WtrecHelper)) {
    . $WtrecHelper
    $reap = $null
    try { $reap = Invoke-WtrecReap $ProjectRoot $AgentId } catch { $reap = $null }
    if ($reap) {
        try {
            $row = [ordered]@{
                timestamp     = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                hook          = "subagent-stop-reconcile"
                decision      = "reap_" + $reap.Decision
                reason        = $reap.Reason
                agent_id      = $AgentId
                resolved_path = $reap.Path
            }
            Add-Content -Path (Join-Path $LogDir "worktree-guard.jsonl") -Value ($row | ConvertTo-Json -Compress) -ErrorAction Stop
        } catch { }
        if ($reap.Decision -eq "kept") {
            [Console]::Error.WriteLine("subagent-stop-reconcile: worktree of agent $AgentId KEPT at $($reap.Path) ($($reap.Reason)) - review, merge or remove it")
            # census F8: file it for the PARENT, which must review/merge/remove it.
            Add-ReconcileParentAlert "worktree_kept" "worktree KEPT at $($reap.Path) ($($reap.Reason)): review its changes, then merge or remove it" ([string]$reap.Path)
        }
    }
}

# ----------------------------------------------------------------------
# Steps 2-5 (V52-L.1): only with a usable AgentId. W6-C2 F7: the changed-
# file set comes from the agent's OWN worktree when it had one, else from
# the shared-tree snapshot diff (unattributed). Must match the .sh sibling.
# ----------------------------------------------------------------------
if (-not $AgentId) { exit 0 }

# Sanitize agent_id for snapshot filename (must match _Get-SafeAgentId
# in snapshot.ps1; we duplicate the sanitization here so the file
# existence check is bit-for-bit accurate).
$safeId = [regex]::Replace($AgentId, '[^a-zA-Z0-9_-]', '_')
if ($safeId.Length -gt 64) { $safeId = $safeId.Substring(0, 64) }
$snapFile = Join-Path $StateDir "subagent-snapshot-$safeId.json"

# Cap to MAX_DIFF_FILES (default 500).
$maxDiff = if ($env:VCT_SUBAGENT_MAX_DIFF) {
    try { [int]$env:VCT_SUBAGENT_MAX_DIFF } catch { 500 }
} else { 500 }

$Attribution = ""
$changedFiles = @()   # project-relative (shared tree) - Layer 3b lists these
$changedAbs = @()     # absolute - every step below reads these
if ($AgentWtIsolated -and (Get-Command Get-WtrecChangedFiles -ErrorAction SilentlyContinue)) {
    $Attribution = "agent_worktree"
    try { $changedAbs = @(Get-WtrecChangedFiles $ProjectRoot $AgentId | Select-Object -First $maxDiff) } catch { $changedAbs = @() }
    # A snapshot from an older SubagentStart is not this agent's evidence.
    if (Get-Command Cleanup-Snapshot -ErrorAction SilentlyContinue) {
        try { Cleanup-Snapshot -AgentId $AgentId -ProjectRoot $ProjectRoot -SnapshotDir $StateDir } catch {}
    }
} else {
    if (-not (Get-Command Diff-Snapshot -ErrorAction SilentlyContinue)) { exit 0 }
    if (-not (Test-Path -LiteralPath $snapFile -PathType Leaf)) { exit 0 }
    $Attribution = "unattributed_shared_tree"
    try {
        $diffOut = Diff-Snapshot -AgentId $AgentId -ProjectRoot $ProjectRoot -SnapshotDir $StateDir
        if ($diffOut) {
            $changedFiles = @($diffOut | Select-Object -First $maxDiff)
        }
    } catch { }
    $changedAbs = @($changedFiles | Where-Object { $_ } | ForEach-Object { Join-Path $ProjectRoot $_ })
}

if (-not $changedAbs -or $changedAbs.Count -eq 0) {
    # Happy path: nothing changed. Clean up snapshot + exit.
    if (Get-Command Cleanup-Snapshot -ErrorAction SilentlyContinue) {
        try { Cleanup-Snapshot -AgentId $AgentId -ProjectRoot $ProjectRoot -SnapshotDir $StateDir } catch {}
    }
    exit 0
}

# Tally + classify. Mirrors the .sh sibling's bookkeeping.
$fileCount = 0
$totalBytes = [int64]0
$kgFiles = New-Object System.Collections.Generic.List[string]    # absolute; shared tree only
$codeFiles = New-Object System.Collections.Generic.List[string]  # relative; shared tree only
$allFiles = New-Object System.Collections.Generic.List[string]   # absolute; credential scan

$codeExtRe = '\.(py|rs|ts|tsx|js|jsx|go|java|cs|c|cpp|h|hpp|rb|php|swift|kt|scala|sh|ps1|sql)$'

foreach ($abs in $changedAbs) {
    if (-not $abs) { continue }
    $fileCount++
    if (Test-Path -LiteralPath $abs -PathType Leaf) {
        try {
            $totalBytes += (Get-Item -LiteralPath $abs -ErrorAction Stop).Length
        } catch {}
    }
    $allFiles.Add($abs) | Out-Null
    # Indexing for SHARED-TREE files only: a worktree agent's files reach the
    # KG / code graph when its work is merged.
    if ($Attribution -eq "agent_worktree") { continue }
    $rel = $abs.Substring($ProjectRoot.Length).TrimStart('\', '/') -replace '\\', '/'
    if ($rel -match '^knowledge/.*\.md$' -or $rel -match '^docs/.*\.md$') {
        $kgFiles.Add($abs) | Out-Null
    }
    if ($rel -match $codeExtRe) {
        $codeFiles.Add($rel) | Out-Null
    }
}

# Reconciliation row: what was found and HOW it is attributed (census F7).
# Its own log, so the V52-L.2 audit log keeps exactly one row per stop.
$FilesLog = Join-Path $LogDir "subagent-reconcile-files.jsonl"
try {
    $recon = [ordered]@{
        timestamp     = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        event         = "reconciled"
        session_id    = $SessionId
        agent_id      = $AgentId
        agent_type    = $AgentType
        attribution   = $Attribution
        file_count    = $changedAbs.Count
        changed_files = @($changedAbs | Select-Object -First 50)
    }
    Add-Content -LiteralPath $FilesLog -Value ($recon | ConvertTo-Json -Compress -Depth 4) -ErrorAction Stop
} catch { }

# -------------------- Step 2: KG / docs sync --------------------
# Through the ONE routing home (route-touched-path.ps1): access gate,
# per-file debounce, detached runner, no timeout. The subagent identity is
# exported for the failure sentinel / row (census F15/F16).
if ($kgFiles.Count -gt 0) {
    $RouteLib = Join-Path $ScriptDir "_lib/route-touched-path.ps1"
    if (Test-Path -LiteralPath $RouteLib) {
        . $RouteLib
        $env:VCT_AGENT_ID = $AgentId
        if ($AgentType) { $env:VCT_AGENT_TYPE = $AgentType }
        if ($SessionId) { $env:VCT_SESSION_ID = $SessionId }
        Initialize-VcoRoute -HooksDir $ScriptDir -ProjectRoot $ProjectRoot
        foreach ($kf in $kgFiles) {
            try { Invoke-VcoRouteTouchedPath -Path $kf -SessionId $SessionId -AgentId $AgentId } catch { }
        }
    } else {
        # A missing routing lib is a BROKEN install, not "nothing to sync".
        try {
            $skip = [ordered]@{ event = "kg_sync_skipped"; reason = "_lib/route-touched-path.ps1 missing"; agent_id = $safeId }
            Add-Content -LiteralPath $FilesLog -Value ($skip | ConvertTo-Json -Compress) -ErrorAction Stop
        } catch { }
    }
}

# -------------------- Step 3: Code-graph drain enqueue --------------------
# MUST MATCH subagent-stop-reconcile.sh Step 3. Route modified code files into
# the SAME batched, worktree-gated path the Stop drain consumes — NOT the old
# orphan code-graph-queue.jsonl (nothing drained it — v0.2.73 HIGH-2). Apply
# the FIX-A' worktree gate (drop ephemeral/unregistered worktree edits), resolve
# + gate ONCE per distinct canonical root, and append gated-IN ABSOLUTE paths
# (newline-delimited) to the session-agnostic shared drain queue. Never run the
# analyzer synchronously; only enqueue. Soft-fail throughout.
if ($codeFiles.Count -gt 0) {
    $gateCache = @{}   # canonical root -> 'SKIP' | 'INDEX'
    $toEnqueue = New-Object System.Collections.Generic.List[string]
    $canonAvail = [bool](Get-Command Get-CanonicalRepoRoot -ErrorAction SilentlyContinue)
    $gateAvail  = [bool](Get-Command Test-EphemeralWorktreeEdit -ErrorAction SilentlyContinue)
    foreach ($rel in $codeFiles) {
        if (-not $rel) { continue }
        $abs = Join-Path $ProjectRoot $rel

        # Canonical MAIN root (empty when unresolved → gate treats as non-worktree/INDEX).
        $canon = ""
        if ($canonAvail) {
            try { $canon = Get-CanonicalRepoRoot -File $abs } catch { $canon = "" }
            if (-not $canon) { $canon = "" }
        }

        $key = $canon
        if (-not $gateCache.ContainsKey($key)) {
            $verdict = "INDEX"
            if ($gateAvail) {
                try {
                    if (Test-EphemeralWorktreeEdit -EditedFile $abs -RepoPath $ProjectRoot -CanonRoot $canon) {
                        $verdict = "SKIP"
                    }
                } catch { $verdict = "INDEX" }
            }
            $gateCache[$key] = $verdict
        }
        if ($gateCache[$key] -eq "INDEX") { [void]$toEnqueue.Add($abs) }
    }

    if ($toEnqueue.Count -gt 0) {
        try {
            $sb = New-Object System.Text.StringBuilder
            foreach ($p in $toEnqueue) { [void]$sb.AppendLine($p) }
            # No BOM (Encoding.UTF8 writes one into a NEW file, glued to the first path).
            Add-VcoUtf8Content -Path $CgDrainShared -Value $sb.ToString() -NoNewline
        } catch {}
    }
}

# -------------------- Step 4: Credential scan --------------------
if ((Get-Command Scan-FileForCredentials -ErrorAction SilentlyContinue) -and $allFiles.Count -gt 0) {
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    foreach ($abs in $allFiles) {
        if (-not (Test-Path -LiteralPath $abs -PathType Leaf)) { continue }
        try {
            $hits = @(Scan-FileForCredentials -FilePath $abs)
            if ($hits.Count -gt 0) {
                $alert = [ordered]@{
                    timestamp  = $ts
                    file       = $abs
                    patterns   = ($hits -join " ")
                    session_id = $SessionId
                    agent_id   = $AgentId
                    agent_type = $AgentType
                    source     = "subagent_stop_reconciler"
                    attribution = $Attribution
                }
                $alertLine = $alert | ConvertTo-Json -Compress -Depth 5
                Add-Content -Path $AlertLog -Value $alertLine -ErrorAction SilentlyContinue
                # census F8: the PARENT must hear about it (pattern NAMES only).
                $relShown = $abs
                if ($abs.StartsWith($ProjectRoot)) { $relShown = $abs.Substring($ProjectRoot.Length).TrimStart('\', '/') }
                Add-ReconcileParentAlert "credential" ("possible credential in {0} (patterns: {1}; found by the SubagentStop scan, {2}): review it before committing" -f $relShown, ($hits -join " "), $Attribution) $abs
            }
        } catch {}
    }
}

# -------------------- Step 5: Subagent work record --------------------
# Records subagent work on the parent session's row as subagent_work_units
# (+ subagent_count / subagent_ids), for the per-subagent KG nudge the owner
# scheduled for v0.2.103 (each subagent nudged on its own work). The
# main-chat kg-update-nudge deliberately does NOT read these fields (owner
# ruling 2026-10-13: the main-chat nudge ignores subagent work). Must match
# the .sh sibling.
# The counter file lives under the machine-wide metrics dir (not the project's
# .claude/) because nudge state is per-session, not per-project;
# kg-update-nudge.ps1 resolves the same path through the same
# `_lib/metrics-dir.ps1` helper. v0.2.92 W7 moved that home out of ~/.claude —
# both hooks moved together, which is why they still agree. Before the helper
# this hook used a THIRD home-directory convention ($env:HOME then
# $env:USERPROFILE) from its two .ps1 siblings.
$MetricsLib = Join-Path $PSScriptRoot "_lib/metrics-dir.ps1"
if ($SessionId -and $fileCount -gt 0 -and (Test-Path -LiteralPath $MetricsLib -PathType Leaf)) {
    . $MetricsLib
    $workUnits = ($fileCount * 50) + [int]([math]::Floor($totalBytes / 4))
    $nudgeDir = Get-VcoMetricsDir
    if (-not $nudgeDir) {
        # Cannot resolve/create the dir → skip step 5.
        if (Get-Command Cleanup-Snapshot -ErrorAction SilentlyContinue) {
            try { Cleanup-Snapshot -AgentId $AgentId -ProjectRoot $ProjectRoot -SnapshotDir $StateDir } catch {}
        }
        exit 0
    }
    $nudgeFile = Join-Path $nudgeDir "kg_update_tokens.jsonl"

    # Counter continuity across the W7 move — seed from the frozen archive when
    # the new file does not exist yet, exactly as kg-update-nudge.ps1 does, so a
    # subagent's work is added to the session's real baseline instead of to a
    # fresh zero. Read-only against the archive.
    if (-not (Test-Path -LiteralPath $nudgeFile -PathType Leaf)) {
        $legacyCounter = Get-VcoMetricsReadFile -Name "kg_update_tokens.jsonl"
        if ($legacyCounter -and $legacyCounter -ne $nudgeFile) {
            try { Copy-Item -LiteralPath $legacyCounter -Destination $nudgeFile -ErrorAction Stop } catch { }
        }
    }

    # W6-C2 (census F5): hold the SAME sidecar lock kg-update-nudge.ps1 takes
    # (O_CREAT|O_EXCL on kg_update_tokens.jsonl.lock, 50 x 20 ms, deleted on
    # release) for the whole read-modify-write. Lock unavailable ->
    # best-effort unlocked, like the nudge. MUST MATCH the .sh sibling.
    $nudgeLock = $nudgeFile + ".lock"
    $lockStream = $null
    for ($try = 0; $try -lt 50; $try++) {
        try {
            $lockStream = [System.IO.File]::Open($nudgeLock, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            break
        } catch [System.IO.IOException] {
            Start-Sleep -Milliseconds 20
        } catch {
            break
        }
    }
    try {
    # Read existing rows, find/upsert ours, atomic rewrite.
    $rows = @()
    if (Test-Path -LiteralPath $nudgeFile -PathType Leaf) {
        try {
            foreach ($line in (Get-Content -LiteralPath $nudgeFile -ErrorAction Stop)) {
                $line = $line.Trim()
                if (-not $line) { continue }
                try {
                    $entry = $line | ConvertFrom-Json -ErrorAction Stop
                    $rows += ,$entry
                } catch {}
            }
        } catch {}
    }

    $existingIdx = -1
    for ($i = 0; $i -lt $rows.Count; $i++) {
        if ($rows[$i].session_id -eq $SessionId) {
            $existingIdx = $i
            break
        }
    }

    if ($existingIdx -ge 0) {
        $r = $rows[$existingIdx]
        $existingWorkUnits = 0
        if ($r.PSObject.Properties["subagent_work_units"]) {
            try { $existingWorkUnits = [int64]$r.subagent_work_units } catch {}
        }
        $existingCount = 0
        if ($r.PSObject.Properties["subagent_count"]) {
            try { $existingCount = [int]$r.subagent_count } catch {}
        }
        $newRow = [ordered]@{}
        foreach ($p in $r.PSObject.Properties) { $newRow[$p.Name] = $p.Value }
        $newRow["subagent_work_units"] = $existingWorkUnits + $workUnits
        $newRow["subagent_last_at"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        $newRow["subagent_count"] = $existingCount + 1
        if ($newRow.Contains("subagent_ids") -and $newRow["subagent_ids"] -is [System.Collections.IList]) {
            $ids = [System.Collections.ArrayList]@($newRow["subagent_ids"])
            $ids.Add($AgentId) | Out-Null
            $newRow["subagent_ids"] = $ids
        } else {
            $newRow["subagent_ids"] = @($AgentId)
        }
        $rows[$existingIdx] = [PSCustomObject]$newRow
    } else {
        $newRow = [ordered]@{
            session_id          = $SessionId
            subagent_work_units = $workUnits
            subagent_last_at    = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            subagent_count      = 1
            subagent_ids        = @($AgentId)
        }
        $rows += ,[PSCustomObject]$newRow
    }

    # Deduplicate by session_id (last-wins matches the nudge hook).
    $seen = New-Object System.Collections.Generic.HashSet[string]
    $final = New-Object System.Collections.Generic.List[object]
    foreach ($r in $rows) {
        $sid = [string]$r.session_id
        if (-not $sid) { continue }
        if ($seen.Contains($sid)) { continue }
        [void]$seen.Add($sid)
        $final.Add($r)
    }

    # Atomic rewrite: temp file + rename.
    $tmp = $nudgeFile + ".tmp"
    try {
        $sb = New-Object System.Text.StringBuilder
        foreach ($r in $final) {
            $line = $r | ConvertTo-Json -Compress -Depth 5
            [void]$sb.AppendLine($line)
        }
        # No BOM: [Text.Encoding]::UTF8 writes one, and the nudge's Python reader
        # (json.loads per line, utf-8) then drops the first row.
        Set-VcoUtf8Content -Path $tmp -Value $sb.ToString() -NoNewline
        Move-Item -LiteralPath $tmp -Destination $nudgeFile -Force -ErrorAction Stop
    } catch {
        try { Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue } catch {}
    }
    } finally {
        if ($lockStream) {
            try { $lockStream.Dispose() } catch { }
            try { Remove-Item -LiteralPath $nudgeLock -Force -ErrorAction SilentlyContinue } catch { }
        }
    }
}

# -------------------- Layer 3b: worktree-violation alert --------------------
# v0.2.71 Track T-WT. Post-hoc detector mirroring subagent-stop-reconcile.sh:
# an `isolation: worktree` agent that modified files under the PARENT
# checkout (Diff-Snapshot resolves paths relative to $ProjectRoot) silently
# fell back to the shared tree. Write a `worktree_violation` row to
# .claude/logs/worktree-guard.jsonl. Non-blocking, soft-fail.
#
# Guard conditions (all must hold): isolation requested and skipped ($IsoFlag,
# derived after the payload parse from the guard record + the definition via
# Test-WtrecIsolationSkipped, or a payload flag), >0 changed files,
# and the reported cwd was NOT a separate worktree (cwd == parent toplevel OR
# cwd absent).
if ($IsoFlag -and $fileCount -gt 0) {
    $wtLog = Join-Path $LogDir "worktree-guard.jsonl"
    $toplevelWt = ""
    if (Get-Command git -ErrorAction SilentlyContinue) {
        try { $toplevelWt = (& git -C $ProjectRoot rev-parse --show-toplevel 2>$null | Select-Object -First 1) } catch { $toplevelWt = "" }
    }
    function Norm-Path([string]$p) {
        try { return [System.IO.Path]::GetFullPath($p) } catch { return $p }
    }
    # Comparison base: prefer the git toplevel, fall back to ProjectRoot when
    # not a git repo (mirror subagent-stop-reconcile.sh) so a reported
    # separate-worktree cwd still exempts.
    $cmpBase = if ($toplevelWt) { $toplevelWt } else { $ProjectRoot }
    $cwdIsParent = $true
    if ($IsoCwd -and $cmpBase) {
        $nc = Norm-Path $IsoCwd
        $nt = Norm-Path $cmpBase
        if ($nc -ne $nt) { $cwdIsParent = $false }
    }
    if ($cwdIsParent) {
        $parentTop = if ($toplevelWt) { $toplevelWt } else { $ProjectRoot }
        $reason = "isolation:worktree agent modified $fileCount file(s) under the parent checkout ($parentTop) - silent shared-tree fallback detected post-hoc"
        # Cap the file list so a tree-wide pass cannot bloat the row.
        $capped = @($changedFiles | Select-Object -First 50)
        $row = [ordered]@{
            timestamp       = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            hook            = "subagent-stop-reconcile"
            decision        = "worktree_violation"
            reason          = $reason
            session_id      = $SessionId
            agent_id        = $AgentId
            agent_type      = $AgentType
            parent_toplevel = $parentTop
            reported_cwd    = $IsoCwd
            file_count      = $fileCount
            changed_files   = $capped
        }
        try {
            $line = $row | ConvertTo-Json -Compress -Depth 6
            Add-Content -Path $wtLog -Value $line -ErrorAction SilentlyContinue
        } catch { }
        [Console]::Error.WriteLine("subagent-stop-reconcile: WORKTREE VIOLATION - $reason")
        Add-ReconcileParentAlert "worktree_violation" "WORKTREE VIOLATION: $reason"
    }
}

# Cleanup: delete the snapshot file.
if (Get-Command Cleanup-Snapshot -ErrorAction SilentlyContinue) {
    try { Cleanup-Snapshot -AgentId $AgentId -ProjectRoot $ProjectRoot -SnapshotDir $StateDir } catch {}
}

exit 0
