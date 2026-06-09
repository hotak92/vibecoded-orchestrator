# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-start-kg-inject.ps1 — Windows sibling of subagent-start-kg-inject.sh.
# SubagentStart hook that retrieves KG context for the subagent's launch
# prompt and emits it as `additionalContext`. See the .sh sibling for
# the full rationale.
#
# V52-L.2 Fix 3 (v0.2.52). Mirrors pre-edit-context-inject.ps1's shape:
# delegates the actual search to rl_kg_search.py (canonical RL-aware
# retrieval chokepoint), wraps results in the SubagentStart JSON envelope.
#
# Constraints:
# - Must complete in <5s (timeout in settings.json bumped from 2s to 5s
#   because hybrid_search cold-path can take 1.5-2.5s).
# - Never throws / never exits non-zero (would block subagent start).
# - Silent no-op when search empty or rl_kg_search.py unavailable.

# Scrub sensitive env vars before any subprocess spawning.
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot

$StderrCap = Join-Path $ScriptDir "_lib/stderr-cap.ps1"
if (Test-Path $StderrCap) { . $StderrCap }

$EmitHelper = Join-Path $ScriptDir "_lib/emit-context.ps1"
if (Test-Path $EmitHelper) { . $EmitHelper }

# V52-L.1: source the snapshot helper. SubagentStop reconciler will
# diff against this snapshot to identify files modified by the subagent.
$SnapshotHelper = Join-Path $ScriptDir "_lib/snapshot.ps1"
if (Test-Path $SnapshotHelper) { . $SnapshotHelper }

$FindPy = Join-Path $ScriptDir "_lib/find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
if (-not $PY) {
    foreach ($candidate in @('python3', 'python', 'py')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { $PY = $cmd.Source; break }
    }
}
if (-not $PY) { exit 0 }

$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

# Parse SubagentStart payload from stdin: prompt (with synonyms),
# session_id, agent_id, agent_type. Field-synonym set matches the
# shared subagent-start-suggest.sh hook so both hooks behave
# identically on whatever wire format Claude Code emits on this build.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { exit 0 }

$Prompt = ""
$SessionId = ""
$AgentId = ""
$AgentType = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.prompt)      { $Prompt = [string]$payload.prompt }
        elseif ($payload.task)        { $Prompt = [string]$payload.task }
        elseif ($payload.description) { $Prompt = [string]$payload.description }
        if ($payload.session_id) { $SessionId = [string]$payload.session_id }
        if ($payload.agent_id)   { $AgentId   = [string]$payload.agent_id }
        if ($payload.agent_type) { $AgentType = [string]$payload.agent_type }
    }
} catch {
    # Empty/malformed stdin — keep variables at defaults
}

if (-not $Prompt) {
    # Still take a snapshot before the early exit — see .sh sibling
    # rationale. Empty prompts still produce subagents that can modify
    # files; the reconciler needs the baseline.
    if ($AgentId -and (Get-Command Take-Snapshot -ErrorAction SilentlyContinue)) {
        try { Take-Snapshot -AgentId $AgentId -ProjectRoot $ProjectRoot | Out-Null } catch {}
    }
    exit 0
}

# V52-L.1: take a filesystem snapshot BEFORE the rest of the hook
# runs. SubagentStop reconciler diffs against this snapshot to find
# files modified by the subagent. Soft-fail.
if ($AgentId -and (Get-Command Take-Snapshot -ErrorAction SilentlyContinue)) {
    try { Take-Snapshot -AgentId $AgentId -ProjectRoot $ProjectRoot | Out-Null } catch {}
}

# Export session / agent context so rl_kg_search.py's emit path
# attributes the retrieval event to this subagent. Mirrors the .sh
# sibling.
if ($SessionId) { $Env:VCT_SESSION_ID = $SessionId }
if ($AgentId)   { $Env:VCT_AGENT_ID   = $AgentId }
if ($AgentType) { $Env:VCT_AGENT_TYPE = $AgentType }

# Cap prompt to 400 chars for the query — see .sh sibling.
$Query = if ($Prompt.Length -gt 400) { $Prompt.Substring(0, 400) } else { $Prompt }

# Resolve VCO venv via the shared helper.
$ResolveVenv = Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1"
$Venv = ""
if (Test-Path $ResolveVenv) {
    . $ResolveVenv
    Resolve-VcoVenvPython -ScriptDir $ScriptDir
    if ($script:VCO_VENV_PYTHON) { $Venv = $script:VCO_VENV_PYTHON }
}

$RlScript = Join-Path $ProjectRoot "claude_mcp_servers/scripts/rl_kg_search.py"

# Bail silently if the venv didn't resolve or the script is missing.
if (-not $Venv -or -not (Test-Path $RlScript)) { exit 0 }

# Run the search with --hook-format. Limit to 3 matches to stay under
# the additionalContext cap.
$Matches = ""
try {
    $rawOut = & $Venv $RlScript $Query --limit 3 --hook-format 2>$null
    if ($rawOut) {
        # Filter out the no-results sentinel; keep the first 60 lines so
        # we don't blow past the emit-context.ps1 cap with verbose
        # bodies. Match the .sh sibling's 60-line cap.
        $lines = @($rawOut -split "`r?`n" | Where-Object { $_ -notmatch '^KG: no-results' } | Select-Object -First 60)
        $Matches = ($lines -join "`n").TrimEnd()
    }
} catch { }

# Whitespace-only / empty match: silent exit.
if (-not $Matches -or -not ($Matches -match '\S')) { exit 0 }

# Format the additionalContext block.
$HeaderLabel = if ($AgentType) { $AgentType } else { "subagent" }
$Output = "[KG context for $HeaderLabel task]:`n`n$Matches`n"

if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
    Emit-AdditionalContext $Output SubagentStart
} else {
    # Inline fallback envelope (same JSON shape).
    try {
        $envelope = @{
            hookSpecificOutput = @{
                hookEventName     = "SubagentStart"
                additionalContext = $Output
            }
        }
        $envelope | ConvertTo-Json -Compress -Depth 5 | Write-Output
    } catch { }
}

exit 0
