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
# Audit row keeps the full payload under `payload` so future analysis
# can pick up event-specific fields (e.g. ConfigChange's `setting`,
# `old_value`, `new_value`) without re-modifying this hook. Mirrors
# the .sh sibling's payload-capture schema.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

# Re-parse stdin as a structured PSCustomObject; emit the original
# payload as a value under `payload`. ConvertFrom-Json gives us the
# whole tree natively — no need to extract individual fields except
# for the top-level envelope columns the audit doc reads quickly.
$payload = $null
$payloadJson = "{}"
$parseError = $null
try {
    if ($HookStdin) {
        $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
        # Re-serialise the parsed object so log lines are normalised
        # (whitespace-stripped, deterministic key order from PS).
        $payloadJson = ($payload | ConvertTo-Json -Compress -Depth 8)
    }
} catch {
    $parseError = $_.Exception.Message
    $preview = if ($HookStdin.Length -gt 200) { $HookStdin.Substring(0, 200) } else { $HookStdin }
    $payloadJson = (@{ _parse_error = $parseError; _raw_preview = $preview } | ConvertTo-Json -Compress)
}

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$LogFile = Join-Path $ProjectDir ".claude/logs/config_changes.jsonl"

$LogDir = Split-Path $LogFile -Parent
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# Build the audit record as a hashtable then ConvertTo-Json so escaping
# stays correct for unicode / quotes / nested objects. `payload` is
# embedded as the parsed object so it rehydrates cleanly on read.
$record = @{
    timestamp        = (Get-Date).ToUniversalTime().ToString("o")
    event            = "config_change"
    session_id       = if ($payload -and $payload.session_id)      { [string]$payload.session_id      } else { "" }
    hook_event_name  = if ($payload -and $payload.hook_event_name) { [string]$payload.hook_event_name } else { "" }
    cwd              = if ($payload -and $payload.cwd)             { [string]$payload.cwd             } else { "" }
    payload          = $payload
}
$line = $record | ConvertTo-Json -Compress -Depth 8

try {
    Add-Content -Path $LogFile -Value $line -ErrorAction Stop
} catch { }
exit 0
