# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Parity-touch 2026-05-11: sibling .sh dropped the stale "Stop hooks don't fire in VS Code" note. Current Claude Code docs treat Stop as universal across all surfaces (CLI / VS Code extension / Desktop) — no PS1 logic change needed.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# notify-stop.ps1
# Fires on Stop event — Claude finished responding.
# Desktop notification via .claude/scripts/notify.py (cross-platform).

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$ProjectName = Split-Path $ProjectDir -Leaf

# Resolve a Python interpreter portably. _lib/find-python.ps1 sets $PY.
$LibDir = Join-Path $PSScriptRoot "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

$NotifyScript = Join-Path $ProjectDir ".claude/scripts/notify.py"
if ($PY -and (Test-Path $NotifyScript)) {
    try {
        & $PY $NotifyScript "Claude finished -- $ProjectName" "Response ready" `
            --urgency low --icon dialog-information --expire-time 8000 2>$null | Out-Null
    } catch { }
}
exit 0
