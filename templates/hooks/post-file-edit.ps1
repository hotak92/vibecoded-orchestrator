# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# VCO-CENTRALIZED-KG: write-side delegator (PR #171 / 0.1.7).
#   Calls .claude/scripts/kg-sync (writes to the project's own
#   KG_COLLECTION / DEVELOPMENT_COLLECTION) and code-graph-incremental.ps1
#   (writes to the project's own code-graph collections via
#   analyze_code_graph.py). Writes do NOT consult VCT_KG_ACCESS_LIST or
#   VCT_CODE_GRAPH_ACCESS_LIST — those env vars are read-side only
#   (fan-out search across peer KGs). This hook is correct as-is; no
#   centralization needed. See knowledge/concepts/multi-source-kg-runtime.md.

# post-file-edit.ps1 — PostToolUse hook
#
# Side-effects (background): KG / docs sync, code-graph incremental.
# LLM-visible reminders (additionalContext envelope):
#   - Code-file edits → CONTEXT_STATE / KG capture reminder
#   - CONTEXT_STATE.md significant-changes → expert-skill update prompt
#   - .claude/skills or .claude/hooks edits → workflow-test prompt
#
# Plain stdout from PostToolUse hooks is silently dropped per the
# v2.1.x contract — reminders intended for the model MUST go through
# `Emit-AdditionalContext` from `_lib/emit-context.ps1`.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

# v0.2.54 Track G (G-6): child spawns used a hardcoded `pwsh`, which does
# not exist on PowerShell 5.1-only machines - KG sync, write-gate,
# dup-detection and code-graph updates were all silently lost there.
# $PsExe resolves pwsh -> powershell fallback.
. "$PSScriptRoot/_lib/resolve-powershell.ps1"
$EmitContextLib = Join-Path $PSScriptRoot "_lib/emit-context.ps1"
if (Test-Path $EmitContextLib) { . $EmitContextLib }

# Debounce helper (2026-06-18, write-amplification fix). Coalesces rapid
# re-edits of the SAME file into one Weaviate write per quiet-window
# (VCO_KG_SYNC_DEBOUNCE_SECONDS, default 5; 0 disables). Correctness
# argument (final state always syncs) + crash-safety reasoning live in
# the helper header. Identical semantics to _lib/kg-sync-debounce.sh.
#
# Conditional source: a partial/old bundle install may lack the lib. If
# absent we define a passthrough Invoke-KgDebounceSchedule that runs the
# sync immediately in a detached process (pre-2026-06-18 behaviour), so a
# missing helper degrades to "no debounce" rather than breaking the hook.
$DebounceLib = Join-Path $PSScriptRoot "_lib/kg-sync-debounce.ps1"
if (Test-Path $DebounceLib) {
    . $DebounceLib
} else {
    function Invoke-KgDebounceSchedule {
        param([string]$ProjectRoot, [string]$FilePath, [string]$WorkingDir,
              [string]$Command, [string]$Channel = "kg")
        $psExe = if ($script:PsExe) { $script:PsExe } else { "pwsh" }
        $wdEsc = ($WorkingDir -replace "'", "''")
        $child = "if ('$wdEsc') { Set-Location -LiteralPath '$wdEsc' -ErrorAction SilentlyContinue }; try { $Command } catch { }"
        # Through the ONE guarded spawn home (_lib/resolve-powershell.ps1,
        # dot-sourced above): an unguarded `-WindowStyle Hidden` is REJECTED
        # on non-Windows PowerShell and takes the whole spawn with it.
        Start-VcoDetachedPwsh -Command $child -PowerShellExe $psExe
    }
}

# Accumulate LLM-visible reminders, emit one envelope at the end.
$LlmNudge = ""
function Add-Nudge([string]$msg) {
    if ($script:LlmNudge) {
        $script:LlmNudge = "$script:LlmNudge`n`n$msg"
    } else {
        $script:LlmNudge = $msg
    }
}

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$EditedFile = ""
# V52-L.2 Fix 2b: parse subagent identity + session_id. We don't write
# a JSONL log directly, but the kg-sync / code-graph-incremental child
# processes DO emit retrieval/sync telemetry — exporting these as env
# vars (VCT_AGENT_ID / VCT_AGENT_TYPE / VCT_SESSION_ID) lets the
# canonical emit path attribute those rows to the agent that triggered
# the write.
$AgentId = ""
$AgentType = ""
$SessionIdFromStdin = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.tool_input -and $payload.tool_input.file_path) {
        $EditedFile = [string]$payload.tool_input.file_path
    }
    if ($payload) {
        if ($payload.agent_id)   { $AgentId   = [string]$payload.agent_id }
        if ($payload.agent_type) { $AgentType = [string]$payload.agent_type }
        if ($payload.session_id) { $SessionIdFromStdin = [string]$payload.session_id }
    }
} catch {
    # Empty/malformed stdin — keep variables at defaults
}
# Export for child processes (kg-sync, code-graph-incremental.ps1, etc.)
# so their emit paths can attribute telemetry to the originating agent.
# Skip empty exports — downstream readers treat unset and empty
# identically, but unset keeps the env listing clean.
if ($AgentId)            { $Env:VCT_AGENT_ID   = $AgentId }
if ($AgentType)          { $Env:VCT_AGENT_TYPE = $AgentType }
if ($SessionIdFromStdin) { $Env:VCT_SESSION_ID = $SessionIdFromStdin }

$ScriptDir = $PSScriptRoot
# D-16 (v0.2.73): prefer CLAUDE_PROJECT_DIR (matches the .sh sibling) so
# worktree-isolated / out-of-tree sessions resolve state paths against the
# same root; fall back to the script-relative root otherwise.
if ($Env:CLAUDE_PROJECT_DIR) {
    $ProjectRoot = $Env:CLAUDE_PROJECT_DIR
} else {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}
$KnowledgeRoot = Join-Path $ProjectRoot "knowledge"
$DocsDir = Join-Path $ProjectRoot "docs"
# D-9 (v0.2.73): match on the directory WITH a trailing separator so
# sibling dirs (knowledge_base/, docs-archive/) don't sync into the KG /
# development collections. StartsWith on the bare root matched them.
$KnowledgeRootSep = $KnowledgeRoot.TrimEnd('\','/') + [System.IO.Path]::DirectorySeparatorChar
$DocsDirSep = $DocsDir.TrimEnd('\','/') + [System.IO.Path]::DirectorySeparatorChar

if (-not $EditedFile) { exit 0 }

# v0.2.49 Phase 8 (item #22): access-matrix gate for KG writes.
#
# Before kicking off any kg-sync subprocess (or upload_docs.py), check
# if this project has write access to the target Weaviate collection.
# The check is fail-open: if the hub is unreachable / the project
# isn't registered / the response is malformed, the resolver returns
# "write" + emits a WARNING + logs a dropped-write-metric row, then
# the sync proceeds. This is DELIBERATE (closed-circuit would brick
# all KG writes during launcher restart).
#
# When the gate returns "read" or "none", we SKIP the sync silently +
# the user gets the WARNING from the resolver client about the deny.
#
# Mirrors templates/hooks/post-file-edit.sh's _kg_write_allowed shell
# function. Resolver discovery: templates/scripts/vct_access_check.ps1
# (orchestrator-root) → .claude/scripts/vct_access_check.ps1
# (user-project install) — the bundle install writes that copy, so the
# two are byte-identical the moment they are written; nothing gates the
# copy afterwards (a copy the user has since edited is backed up to
# .claude/backups/bundle-adoptions/<ts>/ and replaced on the next
# bundle update). Prior text here also called this resolver
# "byte-equivalent to the bash sibling". It is not and never was: the
# .sh and .ps1 resolvers are two separate languages. What IS pinned is
# that BOTH flavours exist (a missing .ps1 sibling is a Windows
# outage) and that they stay logically equivalent — the
# `.github/workflows/hook-parity.yml` gate, which is what survived the
# PR-39 / v0.2.12 removal of `scripts/check_template_drift.py`.
# v0.2.49 SB1: emit a dropped_writes.jsonl row when the gate falls
# back to silent-allow because VCT_PROJECT_ID is missing. Mirrors
# templates/hooks/post-file-edit.sh::_kg_emit_gate_skipped_metric and
# the existing emit_metric helper in vct_access_check.ps1. Never
# throws (silent-allow contract must hold).
function Emit-KgGateSkippedMetric {
    param([string]$Collection)
    try {
        $stateDir = if ($Env:VCT_STATE_DIR) { $Env:VCT_STATE_DIR } else {
            Join-Path $Env:USERPROFILE ".vct"
        }
        $cacheDir = Join-Path $stateDir "cache"
        if (-not (Test-Path $cacheDir)) {
            New-Item -ItemType Directory -Path $cacheDir -Force -ErrorAction SilentlyContinue | Out-Null
        }
        $jsonl = Join-Path $cacheDir "dropped_writes.jsonl"
        $ts = [int][double]::Parse((Get-Date -UFormat %s))
        $row = @{
            ts          = $ts
            project_id  = ""
            collection  = $Collection
            reason      = "gate_skipped_no_project_id"
            fail_open   = $true
        } | ConvertTo-Json -Compress
        Add-Content -Path $jsonl -Value $row -Encoding utf8 -ErrorAction SilentlyContinue
    } catch {
        # Metric-emit failure must not break the silent-allow contract.
    }
}

# v0.2.49 SB1: write an UPDATE_DEFERRED.md entry directing the user to
# resolve the empty-VCT_PROJECT_ID condition (run install.py --update
# OR re-register via Launcher GUI). Per user Q1 (2026-06-08), this is
# the user-facing surface — no stderr WARNING by default.
#
# Idempotent per (session, project) via a sentinel file in
# .claude/state/. Mirrors the bash sibling's
# _kg_emit_gate_skipped_deferral exactly.
function Emit-KgGateSkippedDeferral {
    param([string]$Collection)
    $deferred = Join-Path $ProjectRoot ".claude/context/UPDATE_DEFERRED.md"
    $stateDir = Join-Path $ProjectRoot ".claude/state"
    $sessionId = if ($Env:VCT_SESSION_ID) { $Env:VCT_SESSION_ID } `
                 elseif ($Env:CLAUDE_SESSION_ID) { $Env:CLAUDE_SESSION_ID } `
                 else { [string]$PID }
    $sentinel = Join-Path $stateDir "gate_skipped_deferral_$sessionId"

    # Per-session dedup. First call writes; subsequent calls in the same
    # session are silent no-ops.
    if (Test-Path $sentinel) { return }

    try {
        if (-not (Test-Path $stateDir)) {
            New-Item -ItemType Directory -Path $stateDir -Force -ErrorAction SilentlyContinue | Out-Null
        }
        Set-Content -Path $sentinel -Value "" -ErrorAction SilentlyContinue
    } catch { return }

    try {
        $deferredDir = Split-Path $deferred -Parent
        if (-not (Test-Path $deferredDir)) {
            New-Item -ItemType Directory -Path $deferredDir -Force -ErrorAction SilentlyContinue | Out-Null
        }
    } catch { return }

    # Idempotent body marker — if a prior session wrote a row for this
    # condition_id, leave it in place.
    $marker = "## gate_skipped_no_project_id"
    if ((Test-Path $deferred) -and (Select-String -Path $deferred -SimpleMatch -Pattern $marker -Quiet -ErrorAction SilentlyContinue)) {
        return
    }

    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

    # Append-mode write. Without frontmatter the deferral parser still
    # finds the entry via "^## <cid> (sev)"; the next install.py
    # --update pass canonicalises the file with a header.
    $body = @"

$marker (warning)

**Title**: Phase-8 access-matrix gate skipped (VCT_PROJECT_ID missing from hook env)

**Detected**: The post-file-edit.ps1 hook reached Test-KgWriteAllowed with no VCT_PROJECT_ID. The Phase-8 WRITE gate cannot identify this project against the hub access matrix, so the write was permitted via the silent-allow path. Target collection: $Collection

**Why deferred**: Seeding VCT_PROJECT_ID requires an orchestrator install pass (queries launcher.db for the project UUID) or a Launcher GUI re-registration. The hook cannot self-heal.

**To apply**:
``````bash
# Option A — orchestrator-root install / update:
python install.py --update

# Option B — per-project (pre-v0.2.49 install): re-register the
# project via Launcher GUI -> Projects -> Identity tab. The
# launcher's apply_project_env pass seeds VCT_PROJECT_ID into
# the project-local .claude/env from launcher.db.
``````

**Detected at**: $ts

---
"@
    try {
        Add-Content -Path $deferred -Value $body -Encoding utf8 -ErrorAction SilentlyContinue
    } catch {
        # Silent failure: the silent-allow contract is the priority.
    }
}

# NOTE (2026-06-18 debounce): this synchronous gate is RETAINED for
# reference + sibling parity with the bash _kg_write_allowed, but the
# ACTIVE gate now runs at SYNC time inside the debounced flusher. Because
# a detached Start-Process child cannot call PowerShell functions defined
# here, the gate logic is re-expressed inline in Build-GatedSyncCommand
# (it invokes the external vct_access_check.ps1 resolver at flush time).
# The empty-project_id metric+deferral surfaces are emitted by
# Build-GatedSyncCommand directly. Keep this function for the contract it
# documents; do not assume it is on the live sync path.
#
# v0.2.92: the .sh sibling now matches this design exactly. Pre-v0.2.92,
# the .sh sibling embedded a call to its `_kg_write_allowed` bash function
# by NAME inside the eval'd command string, on the (incorrect) assumption
# that the detached flusher shares function scope with post-file-edit.sh —
# it does not (the v0.2.65 "Item 1" hardening pass made the flusher spawn
# via `setsid bash -c '...'`, a genuinely separate process that sources
# only _lib/kg-sync-debounce.sh). That silently broke the ENTIRE
# hook-triggered KG/docs auto-sync path on the bash side for ~2.5 months
# (verified via isolated repro). The fix, `_kg_build_gated_sync_cmd` in
# post-file-edit.sh, now mirrors this file's Build-GatedSyncCommand: the
# empty-project_id branch is decided synchronously at schedule time, and
# only the access-matrix decision itself is embedded as a self-contained
# POSIX snippet that calls vct_access_check.sh directly (no function-name
# dependency).
function Test-KgWriteAllowed {
    param(
        [string]$Project,
        [string]$Collection
    )
    if (-not $Project) {
        # v0.2.49 SB1: empty VCT_PROJECT_ID was a silent bypass. Per
        # user Q1 (2026-06-08), silent-allow stays the default; metric +
        # deferral are the visibility surfaces. Metric-first so the
        # JSONL row lands even if the deferral write hits a permission
        # error.
        Emit-KgGateSkippedMetric -Collection $Collection
        Emit-KgGateSkippedDeferral -Collection $Collection
        return $true   # no project context → allow (legacy path)
    }
    if (-not $Collection) { return $true } # no collection context → allow

    $resolver = $null
    $candidates = @(
        (Join-Path $ProjectRoot "templates/scripts/vct_access_check.ps1"),
        (Join-Path $ProjectRoot ".claude/scripts/vct_access_check.ps1")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $resolver = $c; break }
    }
    if (-not $resolver) {
        # Resolver not on disk → allow (pre-v0.2.49 install, or post-
        # update where the script hasn't been bundled yet). Matches the
        # bash sibling's same fallthrough.
        return $true
    }

    try {
        $level = & $PsExe -NoProfile -File $resolver $Project $Collection 2>$null
        if ($null -eq $level) { return $true }  # fail-open on null
        $level = ([string]$level).Trim()
    } catch {
        return $true  # fail-open on any invocation error
    }
    return ($level -eq 'write')
}

# Resolve project_id once for the access checks below. Same env-then-
# grep-.claude/env fallback as the bash sibling.
$VctProjectId = $Env:VCT_PROJECT_ID
if (-not $VctProjectId) {
    $envFile = Join-Path $ProjectRoot ".claude/env"
    if (Test-Path $envFile) {
        try {
            $envLines = Get-Content -LiteralPath $envFile -ErrorAction Stop
            foreach ($line in $envLines) {
                if ($line -match '^\s*VCT_PROJECT_ID\s*=\s*"?([^"]+)"?\s*$') {
                    $VctProjectId = $Matches[1].Trim()
                    break
                }
            }
        } catch { }
    }
}

# Resolve the access-matrix checker path once for the debounced (gate
# runs at SYNC time) command strings below. The detached child that
# eventually evals these commands is a SEPARATE process that does NOT
# inherit functions defined here, so the deferred command must re-run the
# gate via the EXTERNAL resolver script (vct_access_check.ps1) rather than
# calling a function by name. (The .sh sibling now does the equivalent —
# see the v0.2.92 note on Test-KgWriteAllowed above; pre-v0.2.92 it
# incorrectly assumed its detached flusher shared function scope.)
# Fall-open semantics mirror Test-KgWriteAllowed exactly: empty
# project_id OR missing resolver OR non-"write" parse failure → allow.
$AccessCheckPs1 = $null
foreach ($c in @(
    (Join-Path $ProjectRoot "templates/scripts/vct_access_check.ps1"),
    (Join-Path $ProjectRoot ".claude/scripts/vct_access_check.ps1")
)) { if (Test-Path $c) { $AccessCheckPs1 = $c; break } }

# Build a self-contained "gate THEN sync" command string for Start-Job.
# Runs the resolver at flush time; only proceeds to the sync command
# when the gate returns "write" (or when the gate cannot apply, matching
# fall-open). $SyncExpr is the PowerShell expression that performs the
# actual sync.
function Build-GatedSyncCommand {
    param([string]$Project, [string]$Collection, [string]$SyncExpr)
    # Empty VCT_PROJECT_ID: preserve the v0.2.49 SB1 user-facing
    # surfaces. Test-KgWriteAllowed emitted the dropped-write metric +
    # the UPDATE_DEFERRED.md remediation entry on this path; both are
    # idempotent (sentinel-guarded per session), so emitting them once
    # synchronously here at schedule time is equivalent — they are a
    # "your project_id is missing" notification, not a per-sync gate
    # decision. The actual write/deny gate still runs at sync time via
    # the resolver for the project-present case below.
    if (-not $Project) {
        if ($Collection) {
            Emit-KgGateSkippedMetric -Collection $Collection
            Emit-KgGateSkippedDeferral -Collection $Collection
        }
        return $SyncExpr   # fall open (legacy silent-allow)
    }
    # No collection context, or no resolver on disk → fall open (allow).
    # Identical to Test-KgWriteAllowed's early returns.
    if ((-not $Collection) -or (-not $AccessCheckPs1)) {
        return $SyncExpr
    }
    $pEsc = $Project    -replace "'", "''"
    $cEsc = $Collection -replace "'", "''"
    $rEsc = $AccessCheckPs1 -replace "'", "''"
    $psEsc = $PsExe -replace "'", "''"
    # Inline gate: invoke resolver; allow on null/error (fail-open) OR
    # exact "write"; deny otherwise.
    return @"
`$lvl = `$null
try { `$lvl = & '$psEsc' -NoProfile -File '$rEsc' '$pEsc' '$cEsc' 2>`$null } catch { `$lvl = `$null }
if ((`$null -eq `$lvl) -or ((([string]`$lvl).Trim()) -eq 'write')) { $SyncExpr }
"@
}

# 1. Knowledge graph auto-sync (background side-effect).
# D-9: require the trailing separator so knowledge_base/ etc. don't match.
if ($EditedFile.StartsWith($KnowledgeRootSep, [StringComparison]::OrdinalIgnoreCase)) {
    $relPath = $EditedFile
    if ($EditedFile.StartsWith($ProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
        $relPath = $EditedFile.Substring($ProjectRoot.Length).TrimStart('\','/')
    }

    # v0.2.49 Phase 8: gate the sync on access-matrix write permission.
    # KG_COLLECTION is the target Weaviate class for primary-KG writes.
    # 2026-06-18: debounced. The gate runs at SYNC time (inside the job's
    # command), not at schedule time, so a coalesced burst consults the
    # access matrix exactly once when the deferred sync fires. The sync
    # re-reads the file from disk → latest content lands.
    $kgSyncPs1 = Join-Path $ProjectRoot ".claude/scripts/kg-sync.ps1"
    $kgSyncSh = Join-Path $ProjectRoot ".claude/scripts/kg-sync"
    $relEsc = $relPath -replace "'", "''"
    $syncExpr = $null
    if (Test-Path $kgSyncPs1) {
        $ps1Esc = $kgSyncPs1 -replace "'", "''"
        $psEscape = $PsExe -replace "'", "''"
        $syncExpr = "& '$psEscape' -NoProfile -File '$ps1Esc' '$relEsc' *> `$null"
    } elseif ((Test-Path $kgSyncSh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
        $shEsc = $kgSyncSh -replace "'", "''"
        $syncExpr = "& bash '$shEsc' '$relEsc' *> `$null"
    }
    if ($syncExpr) {
        $kgCmd = Build-GatedSyncCommand -Project $VctProjectId -Collection $Env:KG_COLLECTION -SyncExpr $syncExpr
        Invoke-KgDebounceSchedule -ProjectRoot $ProjectRoot -FilePath $EditedFile -WorkingDir $ProjectRoot -Command $kgCmd -Channel "kg"
    }

    # Duplicate detection every 10 edits.
    $editCountFile = Join-Path $ProjectRoot ".claude/logs/.kg_edit_count"
    $logsDir = Split-Path $editCountFile -Parent
    if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }
    $count = 0
    if (Test-Path $editCountFile) {
        try { $count = [int](Get-Content $editCountFile -Raw -ErrorAction Stop).Trim() } catch { $count = 0 }
    }
    $count++
    Set-Content -Path $editCountFile -Value $count -Encoding ascii

    if (($count % 10) -eq 0) {
        # D-8 (v0.2.73): capture the summary into a report file (previously
        # the scan ran hidden and its output was discarded — inert feature).
        # The next KG-file edit surfaces + consumes the report.
        #
        # v0.2.92 (R42 / MAJOR-1): the PowerShell wrapper SHIPS now
        # (templates/scripts/kg-duplicates.ps1 -> .claude/scripts/), so it is
        # the FIRST branch — same shape the kg-sync spawn above uses. The
        # comment this replaced denied the sibling's existence and gated
        # the whole feature on `Get-Command bash`, which left every
        # native-Windows-without-bash machine with no duplicate detection at
        # all while a written, tested .ps1 sat beside it. Parity is achieved by
        # CALLING the .ps1, not by narrowing the feature; the bash wrapper
        # stays as the fallback for a project whose bundle predates the
        # sibling.
        #
        # DETACHED via Start-Process, NOT Start-Job: a job's child is torn
        # down when this hook's PowerShell host exits (measured on Linux pwsh
        # 7 — a 300 ms job never ran), and the scan is a whole-collection
        # Weaviate query that always outlives the hook, so the report was
        # never written even on machines that DID have bash. This is the same
        # reasoning _lib/kg-sync-debounce.ps1 records for its flusher; the
        # bash sibling's `( ... ) &` subshell already survives, so this also
        # restores .sh/.ps1 parity.
        $dupPs1 = Join-Path $ProjectRoot ".claude/scripts/kg-duplicates.ps1"
        $dupSh = Join-Path $ProjectRoot ".claude/scripts/kg-duplicates"
        $dupReport = Join-Path $ProjectRoot ".claude/state/kg_duplicates_report.txt"
        $dupExpr = $null
        if (Test-Path $dupPs1) {
            $psEscape = $PsExe -replace "'", "''"
            $dupEsc = $dupPs1 -replace "'", "''"
            $dupExpr = "& '$psEscape' -NoProfile -File '$dupEsc' '--threshold' '0.95' 2>&1"
        } elseif ((Test-Path $dupSh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
            $shEsc = $dupSh -replace "'", "''"
            $dupExpr = "& bash '$shEsc' '--threshold' '0.95' 2>&1"
        }
        if ($dupExpr) {
            $stateDir = Split-Path $dupReport -Parent
            if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }
            $reportEsc = $dupReport -replace "'", "''"
            $rootEsc = $ProjectRoot -replace "'", "''"
            $dupChild = @"
Set-Location -LiteralPath '$rootEsc' -ErrorAction SilentlyContinue
try {
    # ❌ kept too — the scan's failure line is what the ⚠️ "See the error
    # above." verdict points at (parity with the bash sibling's filter).
    `$out = $dupExpr | Select-String -Pattern '✅|⚠️|📊|❌' | ForEach-Object { `$_.ToString() }
    if (`$out) {
        `$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        Set-Content -Path '$reportEsc' -Value (@("# KG duplicate scan (every-10-edits, `$ts)") + `$out) -Encoding utf8
    }
} catch { }
"@
            Start-VcoDetachedPwsh -Command $dupChild -PowerShellExe $PsExe
        }
    }

    # D-8: surface a PENDING duplicate-scan report (from a prior fire)
    # through the additionalContext envelope, then consume it.
    $dupReport = Join-Path $ProjectRoot ".claude/state/kg_duplicates_report.txt"
    if (Test-Path $dupReport) {
        $dupBody = (Get-Content $dupReport -Raw -ErrorAction SilentlyContinue)
        if ($dupBody) {
            Add-Nudge "[KG duplicate scan] The periodic duplicate check found candidates worth reviewing:`n$dupBody`nRun .claude/scripts/kg-duplicates for detail, or ignore if these are intentional siblings."
        }
        Remove-Item $dupReport -Force -ErrorAction SilentlyContinue
    }
}

# 2. Docs auto-sync (background side-effect).
# v0.2.46 post-adversarial: dot-source the shared resolver instead of
# the inline VCT_INSTALL_ROOT-or-ProjectRoot fallback (the latter pointed
# at the USER's venv which doesn't have vco_lib + weaviate-client).
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
if ($EditedFile.StartsWith($DocsDirSep, [StringComparison]::OrdinalIgnoreCase) -and ($EditedFile -like "*.md")) {
    # v0.2.49 Phase 8: gate docs sync on access-matrix write permission
    # against DEVELOPMENT_COLLECTION (the docs/ target).
    # 2026-06-18: debounced (same coalesce semantics + sync-time gate as
    # the knowledge/ branch above).
    $venvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir
    $uploadScript = Join-Path $ProjectRoot ".claude/scripts/upload_docs.py"
    if ($venvPy -and (Test-Path $uploadScript)) {
        $pyEsc = $venvPy -replace "'", "''"
        $upEsc = $uploadScript -replace "'", "''"
        $efEsc = $EditedFile -replace "'", "''"
        $docsSyncExpr = "& '$pyEsc' '$upEsc' '$efEsc' *> `$null"
        $docsCmd = Build-GatedSyncCommand -Project $VctProjectId -Collection $Env:DEVELOPMENT_COLLECTION -SyncExpr $docsSyncExpr
        Invoke-KgDebounceSchedule -ProjectRoot $ProjectRoot -FilePath $EditedFile -WorkingDir $ProjectRoot -Command $docsCmd -Channel "docs"
    }
}

# 2b. Auto-index diagrams (Phase 1.5 — Mermaid + Excalidraw). Mirror of
# the .sh sibling's same-numbered branch. 60s per-file throttle, skips
# sidecar .meta.json writes (would infinite-loop), notifies vct-hub for
# UI refresh (404 silently swallowed until Phase 1.2 broadcast route).
$DiagramsDir = Join-Path $ProjectRoot ".claude/diagrams"
if ($EditedFile.StartsWith($DiagramsDir, [StringComparison]::OrdinalIgnoreCase) `
    -and ($EditedFile -notlike "*.meta.json") `
    -and (($EditedFile -like "*.mmd") -or ($EditedFile -like "*.excalidraw"))) {

    $throttleDir = Join-Path $ProjectRoot ".claude/state"
    if (-not (Test-Path $throttleDir)) {
        New-Item -ItemType Directory -Path $throttleDir -Force | Out-Null
    }

    # MD5 hash of file path → throttle key (no slashes).
    $md5 = [System.Security.Cryptography.MD5]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($EditedFile)
        $digest = $md5.ComputeHash($bytes)
        $diagramHash = -join ($digest | ForEach-Object { $_.ToString('x2') })
    } finally {
        $md5.Dispose()
    }
    $throttleFile = Join-Path $throttleDir "diagram_idx_${diagramHash}.ts"

    # TRUE Unix seconds. MUST MATCH the .sh sibling's `date +%s` — both write
    # and read the SAME `.claude/state/diagram_idx_<hash>.ts` file, so a
    # timezone-shifted value here breaks the throttle across siblings (on a
    # WSL/mixed install the .ps1 read a .sh stamp as `now - last == +offset`
    # and re-indexed on EVERY edit). `(Get-Date) - (Get-Date "1970-01-01Z")`
    # subtracts a UTC instant from a LOCAL one and is off by the UTC offset;
    # .NET subtracts raw ticks and ignores DateTimeKind, so mixing kinds never
    # errors, it just silently returns the wrong number.
    $nowTs = [long][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $lastTs = 0
    if (Test-Path $throttleFile) {
        try {
            $raw = (Get-Content $throttleFile -Raw -ErrorAction Stop).Trim()
            if ($raw -match '^[0-9]+$') { $lastTs = [int]$raw }
        } catch { $lastTs = 0 }
    }

    if (($nowTs - $lastTs) -ge 60) {
        Set-Content -Path $throttleFile -Value $nowTs -Encoding ascii -ErrorAction SilentlyContinue

        # v0.2.46 post-adversarial: shared resolver (already dot-sourced
        # above at the docs branch). Falls back to system python ONLY when
        # no VCO venv is resolvable — never to the USER's project venv.
        $diagVenv = Resolve-VcoVenvPython -ScriptDir $ScriptDir
        if (-not $diagVenv) {
            $diagVenv = (Get-Command python -ErrorAction SilentlyContinue).Source
        }

        if ($diagVenv) {
            # Build the indexer arguments. Pass --diagrams-collection
            # when DIAGRAMS_COLLECTION is set in the env (fix/a1-indexing-
            # pipeline 2026-05-25). Without this kwarg the indexer's
            # Weaviate upsert silently skips (Bug-1 of the wiring audit).
            # Older projects without DIAGRAMS_COLLECTION in env keep the
            # legacy sidecar-only behaviour automatically.
            $diagArgs = @('-m', 'vco_lib.diagram_indexer', 'index', $EditedFile)
            if ($env:DIAGRAMS_COLLECTION) {
                $diagArgs += @('--diagrams-collection', $env:DIAGRAMS_COLLECTION)
            }

            # Serialized index + snapshot in ONE detached child.
            #
            # R2 (code review 2026-05-25): previously two separate
            # Start-Process calls ran in parallel; the snapshot CLI's
            # `project_diagrams WHERE file_path=?` query returned no
            # row on first-edit-per-file because the indexer UPSERT
            # hadn't committed yet → first-version snapshot lost
            # forever. SEQUENCING IS LOAD-BEARING: index must complete
            # before snapshot runs. The two CLIs are therefore emitted
            # as consecutive statements in a single child process.
            #
            # v0.2.92 R2: that fix used Start-Job, which reintroduced the
            # loss by a different route — a job's child is torn down when
            # the host process exits, and this hook exits within
            # milliseconds while the indexer's Weaviate upsert takes
            # seconds. So on Windows NEITHER CLI completed. Identical
            # defect to the duplicate-scan branch above; same remedy, and
            # the same one home for the spawn (`Start-VcoDetachedPwsh`).
            # The .sh sibling's `( ... ) &` subshell already survives, so
            # this also restores .sh/.ps1 parity.
            $snapArgs = @(
                '-m', 'vco_lib.diagram_indexer',
                'snapshot', 'create', $EditedFile, '--quiet'
            )
            $diagPyEsc = $diagVenv -replace "'", "''"
            $diagRootEsc = $ProjectRoot -replace "'", "''"
            $idxLit = ($diagArgs | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ','
            $snapLit = ($snapArgs | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ','
            $diagChild = @"
Set-Location -LiteralPath '$diagRootEsc' -ErrorAction SilentlyContinue
`$vp = '$diagPyEsc'
`$ia = @($idxLit)
`$sa = @($snapLit)
try {
    & `$vp @ia *> `$null
    & `$vp @sa *> `$null
} catch { }
"@
            Start-VcoDetachedPwsh -Command $diagChild -PowerShellExe $PsExe
        }

        # Live UI refresh in DiagramsTab is driven by the launcher's
        # frontend file-watcher (chokidar) — NOT a hub broadcast. The
        # original Phase 1.5.A design called for /api/v1/notify/diagram-changed
        # but pub/sub from hub → frontend would need SSE/WebSocket
        # plumbing the launcher does not have today. Re-evaluate if
        # multi-machine notification ever becomes a real requirement.
    }
}

# 3. Code file changes: code graph incremental update + LLM nudge.
# v0.2.21 Step 18 (caller migration): resolve the code-graph collection
# prefix via the launcher's vct-hub first (`vct_project_config.ps1 -Field
# code_graph_collection_prefix`); fall back to the legacy env chain when
# the hub is unreachable. Mirrors the .sh sibling at the same line.
#
# v0.2.23 field switch: previously this read `code_graph_project`, which
# the hub returns as a legacy alias for `project_slug` — NOT the canonical
# Weaviate prefix. The analyzer's `_sanitize_collection_prefix` then
# re-canonicalised the slug, producing a prefix that diverged from the
# launcher's `project_codegraph_bindings.collection_prefix`. Incremental
# writes landed in zombie collections (e.g. `Orchestrator_root_Code*`)
# while consumers queried the canonical prefix and saw 0 results.
# `code_graph_collection_prefix` is the binding-row truth and the only
# correct source for the write target.
#
# Pre-v0.2.11 behaviour hardcoded "ClaudeOrchestrator" here, which
# polluted the legacy collection from every project install. Do NOT
# re-introduce a hardcoded literal in this position.
if ($EditedFile -match '\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$') {
    $bn = Split-Path $EditedFile -Leaf
    # v0.2.73 (FIX-B, MUST MATCH post-file-edit.sh): the per-EDIT code-graph
    # sync is REMOVED. It used to Invoke-KgDebounceSchedule a
    # code-graph-incremental.ps1 run on every edit; each run hit the big
    # CodeFunction collection's insert-time churn, and parallel worktrees
    # multiplied it into the measured Weaviate disk write-amplification.
    # Instead, the edited path is appended to a per-turn drain queue below and
    # drained ONCE at end-of-turn (stop-codegraph-drain.ps1) over ALL the
    # turn's files in one analyzer pass, rate-limited to once per 120s per
    # project. KG/docs debounce paths are UNCHANGED (this fix targets the CODE
    # path only, the amplifier).
    # v0.2.72 P6 + v0.2.73 FIX-B: append the edited path to BOTH the reminder
    # accumulator (drained + cleared EVERY turn by stop-codegraph-reminder.ps1)
    # AND the code-graph drain queue (drained by the rate-limited
    # stop-codegraph-drain.ps1, which persists the union across rate-limited
    # turns). Distinct files avoid a two-consumer race. Soft-fail: unkeyable
    # session / write error just skips.
    if ($SessionIdFromStdin) {
        $stateDir = Join-Path (Join-Path $ProjectRoot ".claude") "state"
        try {
            if (-not (Test-Path -LiteralPath $stateDir)) {
                New-Item -ItemType Directory -Path $stateDir -Force -ErrorAction Stop | Out-Null
            }
            $accum = Join-Path $stateDir ("edit_reminder_{0}.txt" -f $SessionIdFromStdin)
            Add-Content -LiteralPath $accum -Value $EditedFile -ErrorAction Stop
            $cgQueue = Join-Path $stateDir ("codegraph_drain_{0}.txt" -f $SessionIdFromStdin)
            Add-Content -LiteralPath $cgQueue -Value $EditedFile -ErrorAction SilentlyContinue
        } catch { }
    }
}

# 4. CONTEXT_STATE.md significant-changes → expert-skill nudge.
if ($EditedFile.EndsWith("CONTEXT_STATE.md", [StringComparison]::OrdinalIgnoreCase)) {
    $expertSkill = Join-Path $ProjectRoot ".claude/skills/project-experts/claude-orchestrator-expert.md"
    if (Test-Path $expertSkill) {
        try {
            $changes = (Select-String -Path $EditedFile -Pattern '(✅|##\s+(Status|Current Work|Next Steps|Knowledge Captured))' -ErrorAction SilentlyContinue | Measure-Object).Count
            if ($changes -gt 5) {
                Add-Nudge "[CONTEXT_STATE.md updated — expert-skill review] $changes significant markers detected.`nConsider updating .claude/skills/project-experts/claude-orchestrator-expert.md if any of:`n  - Major milestone completed (Skills system, knowledge graph, etc.)`n  - Architecture changed (MCP, agents, workflow)`n  - New scripts/commands added (kg-*, wrappers)`n  - Recent work section needs refresh"
            }
        } catch { }
    }
}

# 5. Workflow-system edits → workflow-test nudge.
$workflowChanged = $false
$skillsDir = Join-Path $ProjectRoot ".claude/skills"
$hooksDir = Join-Path $ProjectRoot ".claude/hooks"
if ($EditedFile.StartsWith($skillsDir, [StringComparison]::OrdinalIgnoreCase) -or
    $EditedFile.StartsWith($hooksDir,  [StringComparison]::OrdinalIgnoreCase)) {
    $workflowChanged = $true
}
if ($workflowChanged) {
    $bn = Split-Path $EditedFile -Leaf
    Add-Nudge "[Workflow file edited] $bn was changed.`nConsider:`n  - Test the change in actual usage before assuming it works.`n  - Update documentation if the structure changed.`n  - Run /workflow-optimizer to check for optimizations.`n  - Update skills-setup-guide.md if the setup process changed."
}

# Emit accumulated nudges as a single PostToolUse envelope.
if ($LlmNudge -and (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue)) {
    Emit-AdditionalContext $LlmNudge PostToolUse
}
exit 0
