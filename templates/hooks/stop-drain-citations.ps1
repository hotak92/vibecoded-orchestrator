# Parity: kept in step with stop-drain-citations.sh (the v0.2.92 PowerShell-only
# detached-spawn hardening, Start-VcoDetachedProcess, is Windows-specific; every
# other behaviour must match the .sh sibling).
# Stop-hook deferred-citation drain (F-QUEUE, v0.2.70)
# OS-PARITY: ports the .sh sibling. Fires at turn-end (Stop). Reads
# session_id + transcript_path from stdin JSON and runs the python drain,
# which recovers hook-path RL citations the in-process monitor never could.
#
# ACCUMULATE-DON'T-DROP: the drain computes+writes ONLY when a pending task's
# cumulative answer window reaches the token gate; below-gate windows are left
# for the next Stop. Soft-fail throughout; always exit 0.
#
# v0.2.102 W6-C2 (census F12): ALSO registered on SubagentStop (async). With
# agent_id + agent_transcript_path in the payload it drains ONLY that agent's
# staged files against the agent's own transcript (--agent-id); no agent
# transcript -> nothing drained. MUST MATCH the .sh sibling.
#
# W6-C2 fix round (review R4c-8): ALSO routed by the ONE async PostToolUse
# dispatcher (post-tool-use-async ROUTE_TABLE) for Agent and Task. In the
# MAIN chat it re-tags the citations staged for the Agent call's brief with
# tool_response.agentId (--adopt-brief-tool-use-id, matched by tool_use_id)
# instead of draining; --agent-finished for a foreground agent (it has
# finished: stamped with its transcript), tag-only for a background launch.
# MUST MATCH the .sh sibling.
#
# v0.2.102 W6-RL2 (owner ruling R7-4): ALSO registered on SessionEnd. No
# scoring deadline: a main-chat citation below the gate waits for its chat to
# END. On SessionEnd the drain runs with --session-end (records the end first,
# then scores the session's files with the final floors; nothing unscored is
# deleted).
# This hook only parses stdin and spawns the DETACHED drain, so the short
# SessionEnd budget never cuts the drain off. MUST MATCH the .sh sibling.

foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

$SessionId = ""
$TranscriptPath = ""
$AgentId = ""
$AgentTranscript = ""
$HookEvent = ""
$ToolName = ""
$ToolUseId = ""
$RespAgent = ""
$AgentFinished = $true
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.session_id) { $SessionId = [string]$payload.session_id }
        if ($payload.transcript_path) { $TranscriptPath = [string]$payload.transcript_path }
        if ($payload.agent_id) { $AgentId = [string]$payload.agent_id }
        if ($payload.agent_transcript_path) { $AgentTranscript = [string]$payload.agent_transcript_path }
        if ($payload.hook_event_name) { $HookEvent = [string]$payload.hook_event_name }
        if ($payload.tool_name) { $ToolName = [string]$payload.tool_name }
        if ($payload.tool_use_id) { $ToolUseId = [string]$payload.tool_use_id }
        $resp = $payload.tool_response
        if ($resp -and ($resp -isnot [string])) {
            if ($resp.agentId) { $RespAgent = [string]$resp.agentId }
            if ($resp.isAsync -or ([string]$resp.status -eq "async_launched")) { $AgentFinished = $false }
        }
    }
} catch { }

# PostToolUse:Agent (R4c-8): re-tag the brief's citations - main chat only,
# and only when the payload names both the call and the agent.
$Adopt = $false
if ($HookEvent -eq "PostToolUse") {
    if ($AgentId) { exit 0 }
    if (@("Agent", "Task") -notcontains $ToolName) { exit 0 }
    if (-not $ToolUseId -or -not $RespAgent) { exit 0 }
    $Adopt = $true
}

# SubagentStop (W6-C2 F12): the answer source is the AGENT's transcript.
if ($AgentId) {
    if (-not $AgentTranscript) { exit 0 }
    $TranscriptPath = $AgentTranscript
}
# SessionEnd (W6-RL2): the main chat has ended -> final floors.
$SessionEnd = ($HookEvent -eq "SessionEnd") -and (-not $AgentId)

# Resolve the project venv python (the drain imports claude_mcp_servers.*).
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
# The ONE guarded detached-spawn home (Start-VcoDetachedProcess).
. (Join-Path $ScriptDir "_lib/resolve-powershell.ps1")
$VenvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir

# v0.2.100 F3: the drain ships ONLY in the orchestrator root - locate it there
# (same roots as the venv). It drains THIS project's pending dir: the
# CLAUDE_PROJECT_DIR pin below keeps the identity. MUST MATCH the .sh sibling.
$Drain = Resolve-VcoOrchestratorScript -ScriptDir $ScriptDir -RelPath "claude_mcp_servers/scripts/rl_drain_citations.py"
if (-not $Drain -or -not (Test-Path $Drain)) { exit 0 }
if (-not $VenvPy -or -not (Test-Path $VenvPy)) { exit 0 }

# v0.2.76 P5 (hook-latency): DETACH the drain via Start-Process so its
# answer-window embed COMPUTE + telemetry write NEVER block the Stop return.
# Previously this ran the drain SYNCHRONOUSLY (`& $VenvPy ... *> $null`), so
# the Stop hook blocked for the whole drain. The drain is fire-and-forget by
# design: no consumer reads its result within this Stop event, and the RL
# call-sequence is assigned + frozen UPSTREAM in the MCP subprocess
# (rl_state.next_rl_call_seq) at retrieval time -- the drain only READS the
# staged seq to locate the transcript position, so detaching cannot reorder
# staged-seq vs monitor-seq. MUST MATCH the .sh sibling's setsid/nohup detach.
#
# Errors stay OBSERVABLE: redirect the detached process's stdout/stderr to a
# per-run log under .claude/logs/ (overwritten each Stop, so bounded) rather
# than $null. rl_drain_citations soft-fails internally and prints a
# "soft-fail (...)" line on any exception, which lands in the log.
$DrainLog = Join-Path $ProjectRoot ".claude/logs/rl_drain_citations.log"
if ($AgentId) { $DrainLog = Join-Path $ProjectRoot ".claude/logs/rl_drain_citations.agent.log" }
if ($SessionEnd) { $DrainLog = Join-Path $ProjectRoot ".claude/logs/rl_drain_citations.end.log" }
try { New-Item -ItemType Directory -Force -Path (Split-Path $DrainLog) -ErrorAction SilentlyContinue | Out-Null } catch { }
# --agent-id only when set: Start-Process drops an empty element, which would
# leave the flag without its value.
$DrainArgs = @($Drain, "--session-id", $SessionId, "--transcript-path", $TranscriptPath)
if ($Adopt) {
    $DrainLog = Join-Path $ProjectRoot ".claude/logs/rl_drain_citations.adopt.log"
    $DrainArgs += @("--agent-id", $RespAgent, "--adopt-brief-tool-use-id", $ToolUseId)
    if ($AgentFinished) { $DrainArgs += @("--agent-finished") }
} elseif ($AgentId) {
    $DrainArgs += @("--agent-id", $AgentId)
} elseif ($SessionEnd) {
    $DrainArgs += @("--session-end")
}
try {
    $env:CLAUDE_PROJECT_DIR = $ProjectRoot
    # Through the ONE guarded spawn home (_lib/resolve-powershell.ps1): an
    # unguarded `-WindowStyle Hidden` is REJECTED on non-Windows PowerShell
    # editions and takes the whole detached drain with it.
    Start-VcoDetachedProcess -FilePath $VenvPy `
        -ArgumentList $DrainArgs `
        -RedirectStandardOutput $DrainLog `
        -RedirectStandardError "$DrainLog.err"
} catch { }

exit 0
