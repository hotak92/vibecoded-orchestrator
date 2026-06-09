# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-stop-reconcile.ps1 — Windows sibling of subagent-stop-reconcile.sh.
# SubagentStop hook that logs subagent completion + transcript path to
# .claude/logs/subagent-reconciliation.jsonl for post-hoc forensics.
#
# V52-L.2 Fix 5 (v0.2.52, optional reconciliation hook). See the .sh
# sibling for the full rationale and schema.
#
# Side-effect only (writes JSONL). Never blocks. Always exits 0.

# Scrub sensitive env vars before any subprocess spawning.
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot

$StderrCap = Join-Path $ScriptDir "_lib/stderr-cap.ps1"
if (Test-Path $StderrCap) { . $StderrCap }

$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$LogDir = Join-Path $ProjectRoot ".claude/logs"
$LogFile = Join-Path $LogDir "subagent-reconciliation.jsonl"

if (-not (Test-Path $LogDir)) {
    try { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null } catch { exit 0 }
}

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { exit 0 }

# Parse the SubagentStop payload. Field-synonym tolerance for
# transcript_path mirrors how the official docs describe it
# (agent_transcript_path in some payloads, plain transcript_path in
# others). We emit whichever is present; both missing → empty string.
$SessionId = ""
$AgentId = ""
$AgentType = ""
$TranscriptPath = ""
$StopReason = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.session_id)              { $SessionId      = [string]$payload.session_id }
        if ($payload.agent_id)                { $AgentId        = [string]$payload.agent_id }
        if ($payload.agent_type)              { $AgentType      = [string]$payload.agent_type }
        if ($payload.agent_transcript_path)   { $TranscriptPath = [string]$payload.agent_transcript_path }
        elseif ($payload.transcript_path)         { $TranscriptPath = [string]$payload.transcript_path }
        if ($payload.finish_reason)           { $StopReason     = [string]$payload.finish_reason }
        elseif ($payload.stop_reason)             { $StopReason     = [string]$payload.stop_reason }
    }
} catch {
    exit 0
}

# Build the JSONL row via ConvertTo-Json so every field is properly
# escaped (paths with backslashes, transcript IDs with special chars).
$entry = [ordered]@{
    timestamp       = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    session_id      = $SessionId
    agent_id        = $AgentId
    agent_type      = $AgentType
    transcript_path = $TranscriptPath
    stop_reason     = $StopReason
}
try {
    $line = $entry | ConvertTo-Json -Compress -Depth 5
    Add-Content -Path $LogFile -Value $line -ErrorAction Stop
} catch { }

exit 0
