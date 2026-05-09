# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# post-compact.ps1
# Fires on PostCompact event — after context compaction completes.
# Logs the event to ~/.claude/metrics/compactions.jsonl and notifies.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$ProjectName = Split-Path $ProjectDir -Leaf

$LibDir = Join-Path $PSScriptRoot "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

# Read stdin payload (may be empty).
$Payload = ""
try { $Payload = [Console]::In.ReadToEnd() } catch { }

$Trigger = "unknown"
$SessionId = ""
if ($Payload) {
    try {
        $payloadObj = $Payload | ConvertFrom-Json -ErrorAction Stop
        if ($payloadObj) {
            if ($payloadObj.trigger)    { $Trigger = [string]$payloadObj.trigger }
            if ($payloadObj.session_id) { $SessionId = [string]$payloadObj.session_id }
        }
    } catch { }
}
if (-not $Trigger) { $Trigger = "unknown" }

# Wipe the KG/codegraph injection dedup state for this session — the LLM
# just lost the context that included those previously-injected nodes, so
# re-injecting them on subsequent edits is now correct (and helpful).
# pre-edit-context-inject.ps1 writes to .claude/state/seen_kg_titles_<id>.txt.
if ($SessionId) {
    $SeenFile = Join-Path $ProjectDir ".claude/state/seen_kg_titles_$SessionId.txt"
    if (Test-Path $SeenFile) {
        Remove-Item $SeenFile -Force -ErrorAction SilentlyContinue
    }
}

# Log compaction event under the user's home metrics dir.
$UserHome = [System.Environment]::GetFolderPath('UserProfile')
$LogDir = Join-Path $UserHome ".claude/metrics"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}
$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$line = "{""timestamp"":""$ts"",""project"":""$ProjectName"",""trigger"":""$Trigger""}"
try { Add-Content -Path (Join-Path $LogDir "compactions.jsonl") -Value $line } catch { }

# Cross-platform desktop notification.
$NotifyScript = Join-Path $ProjectDir ".claude/scripts/notify.py"
if ($PY -and (Test-Path $NotifyScript)) {
    try {
        & $PY $NotifyScript "Context compacted -- $ProjectName" "Trigger: $Trigger. Context re-injected." `
            --urgency low --icon dialog-information --expire-time 5000 2>$null | Out-Null
    } catch { }
}
exit 0
