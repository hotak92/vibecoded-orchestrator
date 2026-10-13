# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# OS-EXEMPT-PARITY: this .ps1 carries a UTF-8 BOM its .sh sibling must NOT have (PS 5.1 encoding) — the two legitimately diverge on that byte, so a BOM-only edit need not touch the .sh. Keep the LOGIC in lockstep by hand.
# subagent-start-isolation-check.ps1 — Windows sibling of
# subagent-start-isolation-check.sh. SubagentStart hook (Layer 0b,
# secondary / belt) for the worktree-isolation silent-fallback safeguard.
#
# When a subagent that requested `isolation: worktree` spawns and the
# payload exposes an isolation flag AND a cwd/worktree field, assert the cwd
# is a genuinely separate worktree; on a suspected violation (cwd resolves
# to the parent checkout toplevel) inject a LOUD additionalContext block.
# Cannot block (event is non-blocking) — pure loud-warn. NO-OPs gracefully
# when isolation not requested or no cwd is exposed.
#
# Cross-OS parity: the detection logic (isolation-flag synonyms + cwd
# synonyms + the cwd==toplevel violation test) MUST match
# subagent-start-isolation-check.sh. Keep them in lockstep.
#
# Always exits 0. Silent when isolation not requested or no cwd exposed.

# Scrub sensitive env vars before any subprocess spawning.
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot

# Stderr cap so a buggy iteration cannot reproduce the 2026-05-07 GUI freeze.
$StderrCap = Join-Path $ScriptDir "_lib/stderr-cap.ps1"
if (Test-Path $StderrCap) { . $StderrCap }

# Emit-AdditionalContext helper (preferred). Inline fallback below if missing.
$EmitHelper = Join-Path $ScriptDir "_lib/emit-context.ps1"
if (Test-Path $EmitHelper) { . $EmitHelper }

$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { exit 0 }

# Parse defensively. isolation-flag synonyms + cwd synonyms mirror the .sh.
$IsoFlag = $false
$CwdField = ""
$AgentId = ""
$AgentType = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        function Test-Truthy($v) {
            if ($v -is [bool]) { return $v }
            if ($v -is [string]) {
                return @('worktree','true','1','yes','isolated') -contains $v.Trim().ToLowerInvariant()
            }
            return $false
        }
        # Explicit `isolation` field == "worktree".
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
            if ($payload.PSObject.Properties[$f] -and $payload.$f) { $CwdField = [string]$payload.$f; break }
        }
        if ($payload.PSObject.Properties['agent_id'] -and $payload.agent_id) { $AgentId = [string]$payload.agent_id }
        if ($payload.PSObject.Properties['agent_type'] -and $payload.agent_type) { $AgentType = [string]$payload.agent_type }
    }
} catch {
    exit 0
}

function Emit-Ctx([string]$text) {
    if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
        Emit-AdditionalContext $text 'SubagentStart'
    } else {
        $envelope = [ordered]@{
            hookSpecificOutput = [ordered]@{
                hookEventName     = 'SubagentStart'
                additionalContext = $text
            }
        }
        Write-Output ($envelope | ConvertTo-Json -Compress -Depth 8)
    }
}

# -- worktree-guard notice (v0.2.102 W6-Q) - must match the .sh --------------
# worktree-guard.ps1 leaves .claude/state/worktree-guard/notice-<name>.txt
# when the agent runs in place (not a git repo) or on a snapshot base (no
# commits yet). WorktreeCreate `name` is `agent-<agent_id>`; the bare id is
# accepted too. Delivered once, then removed; independent of the isolation
# flag (the live SubagentStart payload does not carry one).
if ($AgentId -and ($AgentId -match '^[A-Za-z0-9_-][A-Za-z0-9._-]*$')) {
    $NoticeDir = Join-Path (Join-Path (Join-Path $ProjectRoot ".claude") "state") "worktree-guard"
    foreach ($nm in @("notice-agent-$AgentId.txt", "notice-$AgentId.txt")) {
        $np = Join-Path $NoticeDir $nm
        if (Test-Path -LiteralPath $np -PathType Leaf) {
            $nt = ""
            try { $nt = (Get-Content -LiteralPath $np -Raw -ErrorAction Stop) } catch { }
            try { Remove-Item -LiteralPath $np -Force -ErrorAction SilentlyContinue } catch { }
            if ($nt -and ($nt -match '\S')) {
                Emit-Ctx $nt.TrimEnd()
                exit 0
            }
        }
    }
}

# -- Lane reservation binding (v0.2.102 W6-AA) - must match the .sh ---------
# worktree-guard's launch record carries the reservation id, target repo,
# branch and base when the worktree came from a lane reservation
# (vco_lib/lane_reservation.py). Tell the agent, on every SubagentStart.
$WtrecLaneHelper = Join-Path $ScriptDir "_lib/worktree-records.ps1"
if ($AgentId -and (Test-Path -LiteralPath $WtrecLaneHelper)) {
    . $WtrecLaneHelper
    $laneFile = Find-WtrecRecord $ProjectRoot $AgentId
    if ($laneFile) {
        $laneRec = Read-WtrecRecord $laneFile
        if ($laneRec -and [string]$laneRec.reservation_id) {
            Emit-Ctx "VCO lane reservation $([string]$laneRec.reservation_id): your worktree is $([string]$laneRec.path) (repo $([string]$laneRec.repo), branch $([string]$laneRec.branch), base $([string]$laneRec.base_sha)). If your brief names a different reservation id, STOP and report. Only Edit/Write are held to this worktree by the harness: keep every Bash command (cd, redirects, git) inside it too."
            exit 0
        }
    }
}

# -- Skipped-isolation detection (v0.2.102 W6-Q follow-up) - must match .sh --
# A definition (found by agent_type = frontmatter `name`, in the project's or
# the user's .claude/agents) that declares `isolation: worktree`, with NO
# worktree-guard record for this agent id while the guard is registered,
# means the harness started the agent without a worktree (e.g. an
# agent-teams teammate). Unknown definition / plugin-scoped type => no-op.
# A record that outlived its reaped worktree still counts (a resumed agent):
# Remove-WtrecStale keeps records until idle past max(30, cleanupPeriodDays)
# + 30 days, and this lookup (Find-WtrecRecord) restarts the idle clock.
# The decision lives in _lib/worktree-records.ps1 :: Test-WtrecIsolationSkipped,
# shared with subagent-stop-reconcile's post-hoc alert (Layer 3b).
$WtrecHelper = Join-Path $ScriptDir "_lib/worktree-records.ps1"
if ($AgentId -and (Test-Path -LiteralPath $WtrecHelper)) {
    . $WtrecHelper
    if (Test-WtrecIsolationSkipped $ProjectRoot $AgentId $AgentType) {
        Emit-Ctx "ISOLATION SKIPPED - the definition of ``$AgentType`` declares ``isolation: worktree``, but no isolated worktree was created for this launch: you are running in the parent's working directory and every edit lands in the shared checkout (this happens, for example, when the agent is started as an agent-teams teammate). BEFORE any edit or git write: stop and report this to the caller, or work only on what the brief explicitly allows in the shared tree. Do NOT commit from the parent checkout."
        exit 0
    }
}

# No-op: isolation not requested, or no cwd exposed. Don't guess.
if (-not $IsoFlag) { exit 0 }
if (-not $CwdField) { exit 0 }

# Resolve the parent checkout toplevel (relative to project root).
$Toplevel = ""
if (Get-Command git -ErrorAction SilentlyContinue) {
    try { $Toplevel = (& git -C $ProjectRoot rev-parse --show-toplevel 2>$null | Select-Object -First 1) } catch { $Toplevel = "" }
}
if (-not $Toplevel) { exit 0 }

function Norm-Path([string]$p) {
    try { return [System.IO.Path]::GetFullPath($p) } catch { return $p }
}
$CwdAbs = Norm-Path $CwdField
$ToplevelAbs = Norm-Path $Toplevel

# Violation suspected only when cwd resolves to the parent toplevel itself.
if ($CwdAbs -ne $ToplevelAbs) { exit 0 }

$Msg = "WARNING: ISOLATION VIOLATION SUSPECTED - this subagent requested ``isolation: worktree`` but its cwd ($CwdAbs) IS the parent checkout. Any ``git commit`` from here lands on the PARENT branch (the 2026-06-30 silent-fallback footgun). BEFORE any git write: create your own worktree (``git worktree add <path> -b <branch>``) and ``cd`` into it, OR abort and report. Do NOT commit from the parent checkout."

if (-not ($Msg -match '\S')) { exit 0 }

Emit-Ctx $Msg

exit 0
