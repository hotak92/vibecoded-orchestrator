# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# config-change-audit.ps1
# Fires on ConfigChange event — logs all settings changes for audit trail.
# Background: does NOT write to stdout (not injected into context).

. "$PSScriptRoot/_lib/stderr-cap.ps1"

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# $env:CLAUDE_TOOL_NAME / $env:CLAUDE_TOOL_ARGS are EMPTY — verified
# empirically 2026-05-08 via stdin-capture diagnostic. Without this,
# every config-change audit entry was {"tool":"unknown","args":{}}.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$tool = "unknown"
$argsRaw = "{}"
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name)  { $tool = [string]$payload.tool_name }
        if ($payload.tool_input) { $argsRaw = ($payload.tool_input | ConvertTo-Json -Compress -Depth 8) }
    }
} catch {
    # Empty/malformed stdin — keep defaults
}

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$LogFile = Join-Path $ProjectDir ".claude/logs/config_changes.jsonl"

$LogDir = Split-Path $LogFile -Parent
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
# Avoid double-encoding: emit a JSON line literally so $argsRaw is embedded as-is
# (matching the .sh behavior where args is a JSON object pasted into the line).
$line = "{""timestamp"":""$ts"",""event"":""config_change"",""tool"":""$tool"",""args"":$argsRaw}"
try {
    Add-Content -Path $LogFile -Value $line -ErrorAction Stop
} catch { }
exit 0
