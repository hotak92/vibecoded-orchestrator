# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars (this hook doesn't need credentials)
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# compact-context-reinject.ps1
# Fires on SessionStart matcher "compact"
# Re-injects critical session context to stdout (becomes additionalContext).

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }

# 1. CONTEXT_STATE.md (uncapped)
$CtxState = Join-Path $ProjectDir ".claude/CONTEXT_STATE.md"
if (Test-Path $CtxState) {
    Write-Output "## Current Task State (re-injected after compaction)"
    Get-Content $CtxState
    Write-Output ""
}

# 2. Active plan summary (cap 30 lines)
$PlansDir = Join-Path $ProjectDir ".claude/context/plans"
if (Test-Path $PlansDir) {
    $LatestPlan = Get-ChildItem -Path $PlansDir -Filter "*.md" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '[/\\]archive[/\\]' } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($LatestPlan) {
        Write-Output "## Active Plan: $($LatestPlan.Name)"
        Get-Content $LatestPlan.FullName -TotalCount 30
        Write-Output ""
    }
}

# 3. Recent git activity (cap 8 commits)
Write-Output "## Recent Commits"
try {
    & git -C $ProjectDir log --oneline -8 2>$null
} catch { }
Write-Output ""

# 4. Pre-compaction snapshot (cap 50 lines)
$Snapshot = Join-Path $ProjectDir ".claude/context/pre-compact-snapshot.md"
if (Test-Path $Snapshot) {
    Write-Output "## Pre-Compaction Snapshot"
    Get-Content $Snapshot -TotalCount 50
    Write-Output ""
}

# 5. Pruned activity summary (cap 30 lines), then truncate
$Pruned = Join-Path $ProjectDir ".claude/context/pruned-context-summary.md"
if ((Test-Path $Pruned) -and ((Get-Item $Pruned).Length -gt 0)) {
    Get-Content $Pruned -TotalCount 30
    Write-Output ""
    # Truncate (clear) so a non-compact SessionStart doesn't re-show last
    # compact's summary as stale state.
    Set-Content -Path $Pruned -Value "" -NoNewline
}
exit 0
