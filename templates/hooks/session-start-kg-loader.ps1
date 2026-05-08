# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# SessionStart Hook: Knowledge Graph Reference Provider (PowerShell port)
# Purpose: Display paths to relevant KG resources (no auto-loading)

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }

$LibDir = Join-Path $PSScriptRoot "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

# Optional RL retrieval server (Pro tier). Auto-launches if installed,
# otherwise silently no-ops — free tier ships with KG + code graph only.
$UserHome = [System.Environment]::GetFolderPath('UserProfile')
$DefaultLauncher = Join-Path $UserHome ".claude\scripts\start-rl-server.ps1"
$RlLauncher = if ($env:RL_SERVER_LAUNCHER) { $env:RL_SERVER_LAUNCHER } else { $DefaultLauncher }
if ($UserHome -and (Test-Path $RlLauncher)) {
    if (-not $env:RL_SERVER_PORT) { $env:RL_SERVER_PORT = "11439" }
    if (-not $env:RL_PROJECT_ROOT) { $env:RL_PROJECT_ROOT = $ProjectDir }
    try {
        & pwsh -NoProfile -File $RlLauncher 2>$null | Out-Null
    } catch {
        try { & powershell -NoProfile -File $RlLauncher 2>$null | Out-Null } catch { }
    }
}

@"

KG Resources Available:

   Scripts:
   - .claude/scripts/kg-search search "query" [--type TYPE] [--tags TAGS]
   - .claude/scripts/kg-info info "Node Title"
   - .claude/scripts/kg-info connections "Node Title"

   Recent Work:
   - .claude/scripts/kg-search recent --days 7

   Project Context:
   - .claude/CONTEXT_STATE.md (current task state)
   - .claude/context/plans/ (active plans - reference when needed)
   - .claude/context/plans/archive/ (completed plans)

   Tip: Use kg-search to find relevant nodes when needed.

"@

# Re-inject enabled /loop jobs from cron-jobs.json
$CronFile = Join-Path $ProjectDir ".claude/cron-jobs.json"
if ((Test-Path $CronFile) -and $PY) {
    $pyCode = @'
import sys, json
path = sys.argv[1]
try:
    with open(path) as f:
        data = json.load(f)
    active = [j for j in data.get('jobs', []) if j.get('enabled')]
    if active:
        print("Active /loop jobs (re-run these to restore recurring tasks):")
        for j in active:
            print(f"  {j['command']}")
        print("")
except Exception:
    pass
'@
    try {
        $out = & $PY -c $pyCode $CronFile 2>$null
        if ($out) { Write-Output $out }
    } catch { }
}
exit 0
