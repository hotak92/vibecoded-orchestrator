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
#
# v0.2.107 RL-DRAIN (A-024): no drain is spawned while the session has no
# pending file (its own, or one staged without a session id); a spawned drain
# runs single-flight per transcript and reads only the transcript bytes
# appended since the last drain (rl_drain_citations.py). MUST MATCH the .sh.

# Canonical list: _lib/scrub-env.ps1; the inline list is the fallback for a missing lib.
if ([System.IO.File]::Exists("$PSScriptRoot/_lib/scrub-env.ps1")) { . "$PSScriptRoot/_lib/scrub-env.ps1"; Invoke-VctScrubSecretEnv } else {
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
. "$PSScriptRoot/_lib/hook-stdin.ps1"  # UTF-8 payload read + UTF-8 output (before stderr-cap)

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$HookStdin = ""
try { $HookStdin = Read-VcoHookStdin } catch { }

# RL-DRAIN (v0.2.107, A-024): no drain without a pending file. A drain only
# ever acts on staged citations, so a project whose queue is empty spawns
# nothing. The exact per-session rule follows the parse. MUST MATCH
# citation_pending.session_may_drain and the .sh sibling.
$PendingDir = Join-Path $ProjectRoot ".claude/state/rl_pending"
function Test-VcoPendingFile([string]$Pattern) {
    # Names ending in .json only: a claim (.json.draining.<pid>) is not pending.
    try {
        foreach ($f in [System.IO.Directory]::EnumerateFiles($PendingDir, $Pattern)) {
            if ($f.EndsWith(".json")) { return $true }
        }
    } catch { }
    return $false
}
function Test-VcoClaimFile([string]$Pattern) {
    # A claim: <name>.json.draining.<pid> (citation_pending.CLAIM_MARK).
    try {
        foreach ($f in [System.IO.Directory]::EnumerateFiles($PendingDir, $Pattern)) {
            if ($f.Contains(".json.draining.")) { return $true }
        }
    } catch { }
    return $false
}
# A claim passes this cheap gate too: on SessionEnd a session whose only file
# the MCP monitor holds still drains, to record its end (wave-2 check L-4).
# The per-session rule below decides.
if (-not (Test-VcoPendingFile "*.json") -and -not (Test-VcoClaimFile "*__*.json.draining.*")) { exit 0 }

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

# The session id as it names its pending files (citation_pending._safe;
# -creplace: case-sensitive, exactly the Python class). A drain of this
# session reads its own files and the ones staged without a session (the MCP /
# kg-search CLI path); an empty id drains every file. The main chat's
# SessionEnd also drains for a file of its own under a claim.
$SafeSid = $SessionId -creplace '[^A-Za-z0-9._-]', '-'
if ($SafeSid) {
    $EndClaim = ($HookEvent -eq "SessionEnd") -and (-not $AgentId) -and (Test-VcoClaimFile "$($SafeSid)__*.json.draining.*")
    if (-not (Test-VcoPendingFile "$($SafeSid)__*.json") -and -not (Test-VcoPendingFile "nosession__*.json") -and -not $EndClaim) { exit 0 }
} elseif (-not (Test-VcoPendingFile "*.json")) {
    exit 0
}

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
