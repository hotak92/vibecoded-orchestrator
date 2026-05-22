# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# v0.2.18 (Commit 11): surface embedding-backend failure hints to Claude.
#
# Purpose
#   When EmbeddingService.for_project() fails because no backend is reachable,
#   vco_lib/embedding_service.py writes a Claude-readable hint to
#   .claude/context/EMBEDDING_FAILURES.md (and a JSONL diagnostic to
#   $env:USERPROFILE\.claude\metrics\embedding_failures.jsonl). The MD file
#   is auto-cleared the next time construction succeeds. This SessionStart
#   hook surfaces the hint (when it exists) into the current Claude Code
#   session so the LLM has immediate context about the broken state.
#
# Idempotent — safe to run on every SessionStart even when there's nothing
# to surface. Soft-fails throughout; never blocks SessionStart.

# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# Discover install root — same anchor convention as ensure-containers.ps1.
# $env:CLAUDE_PROJECT_DIR is set by Claude Code; git toplevel is the fallback.
$InstallRoot = $env:CLAUDE_PROJECT_DIR
if (-not $InstallRoot) {
    try {
        $InstallRoot = (& git rev-parse --show-toplevel 2>$null).Trim()
    } catch {
        $InstallRoot = $null
    }
}
if (-not $InstallRoot) {
    # No project context — running outside any VCO project.
    exit 0
}

$HintFile = Join-Path $InstallRoot ".claude\context\EMBEDDING_FAILURES.md"
if (-not (Test-Path $HintFile)) {
    # No failure recorded — silent no-op (idempotent zero-output path).
    exit 0
}

# JSONL path is informational so Claude can read it; existence is not
# required here. Use USERPROFILE on Windows (the Python side uses
# Path.home() which also resolves to USERPROFILE).
$UserHome = [System.Environment]::GetFolderPath('UserProfile')
if (-not $UserHome) {
    $UserHome = $env:USERPROFILE
}
$JsonlPath = Join-Path $UserHome ".claude\metrics\embedding_failures.jsonl"

# Surface to Claude via stdout — SessionStart hook stdout is injected as
# a system-reminder. Print path + full hint body so the LLM sees both the
# pointer and the diagnostic in-context.
Write-Output ""
Write-Output "==================================================================="
Write-Output "Embedding-backend failure recorded since last successful run."
Write-Output "Claude: read this hint and (if asked) investigate the JSONL log."
Write-Output ""
Write-Output "Hint file:    $HintFile"
Write-Output "Detail log:   $JsonlPath"
Write-Output "==================================================================="
Write-Output ""
try {
    Get-Content -LiteralPath $HintFile -Raw -ErrorAction Stop | Write-Output
} catch {
    Write-Output "(hint file became unreadable between check and read)"
}
Write-Output ""
Write-Output "==================================================================="
Write-Output ""

exit 0
