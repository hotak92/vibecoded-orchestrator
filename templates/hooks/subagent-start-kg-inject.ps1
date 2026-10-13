# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-start-kg-inject.ps1 -- Windows sibling of
# subagent-start-kg-inject.sh. SubagentStart hook: V52-L.1 filesystem
# snapshot ONLY (v0.2.101 injection redesign, PLAN-V02101 section C5).
#
# The KG-injection half that queried rl_kg_search.py on the payload's
# prompt|task|description field was RETIRED -- superseded by the parent-side
# PreToolUse(Agent|Task) hook agent-brief-kg-inject.{sh,ps1} (the
# SubagentStart payload carries only agent_id + agent_type, so the old
# query could never fire). The snapshot below stays: the SubagentStop
# reconciler diffs against it. Full rationale in the .sh sibling. MUST
# MATCH it.

# Scrub sensitive env vars before any subprocess spawning.
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot

# V52-L.1: source the snapshot helper (optional -- partial install
# tolerance; the reconciler degrades to logging-only when absent).
$SnapshotHelper = Join-Path $ScriptDir "_lib/snapshot.ps1"
if (Test-Path $SnapshotHelper) { . $SnapshotHelper }

$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

# Parse the SubagentStart payload: agent identity ONLY (no prompt/task
# text -- that is why the retired KG half could never fire).
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { exit 0 }

$AgentId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.agent_id) { $AgentId = [string]$payload.agent_id }
} catch {
    # Empty/malformed stdin -- keep the default
}

# v0.2.102 W6-C2 (census L2/F7): an agent with its OWN worktree (a
# worktree-guard record with mode head|snapshot|reservation|
# reservation_existing) is reconciled from that worktree's git state: skip
# the shared-tree snapshot. MUST MATCH the .sh sibling.
$SkipSnapshot = $false
$WtrecLib = Join-Path $ScriptDir "_lib/worktree-records.ps1"
if ($AgentId -and (Test-Path -LiteralPath $WtrecLib)) {
    . $WtrecLib
    try {
        $isoRecFile = Find-WtrecRecord $ProjectRoot $AgentId
        if ($isoRecFile) {
            $isoRec = Read-WtrecRecord $isoRecFile
            if ($isoRec -and (@("head", "snapshot", "reservation", "reservation_existing") -contains [string]$isoRec.mode)) { $SkipSnapshot = $true }
        }
    } catch { }
}

# Take the filesystem snapshot. Soft-fail. MUST MATCH the .sh sibling.
if (-not $SkipSnapshot -and $AgentId -and (Get-Command Take-Snapshot -ErrorAction SilentlyContinue)) {
    try { Take-Snapshot -AgentId $AgentId -ProjectRoot $ProjectRoot | Out-Null } catch {}
}

exit 0
