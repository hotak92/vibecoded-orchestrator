# parity-confirmation 2026-05-10: .sh sibling now uses _lib/find-python.sh; this .ps1 already used _lib/find-python.ps1 — true parity, no asymmetric fix.
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# VCO-CENTRALIZED-KG: write-side delegator (PR #171 / 0.1.7).
#   Calls .claude/scripts/analyze_code_graph.py against the project's
#   own code-graph collections (auto-detected for sibling repos via
#   detect-project.ps1). Writes do NOT consult VCT_CODE_GRAPH_ACCESS_LIST
#   — that env var is read-side only (fan-out across peer codegraphs).
#   No centralization needed. See knowledge/concepts/multi-source-kg-runtime.md.

# code-graph-incremental.ps1
# Run incremental code graph analysis on a code file edit.
# Triggered by post-file-edit.ps1.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

param(
    [Parameter(Position=0,Mandatory=$true)] [string]$EditedFile,
    [Parameter(Position=1)] [string]$RepoPath = "",
    [Parameter(Position=2)] [string]$ProjectName = ""
)

if (-not $RepoPath) {
    $RepoPath = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
}
if (-not $ProjectName) { $ProjectName = Split-Path $RepoPath -Leaf }

$ScriptDir = $PSScriptRoot
$DefaultRepoRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$Analyzer = if ($env:VCT_ANALYZER_SCRIPT) { $env:VCT_ANALYZER_SCRIPT } else { Join-Path $DefaultRepoRoot ".claude/scripts/analyze_code_graph.py" }
# Dual-layout venv resolution (PR-25 / v0.2.12). Modern installs put the
# venv at <repo_root>\.venv (top-level); pre-v0.2.x installs had it at
# <repo_root>\claude_mcp_servers\.venv. Hardcoding the latter caused this
# hook to silently fall through to system python on modern installs.
# VCT_VENV overrides everything when set explicitly.
if ($env:VCT_VENV) {
    $Venv = $env:VCT_VENV
} elseif (Test-Path (Join-Path $DefaultRepoRoot ".venv")) {
    $Venv = Join-Path $DefaultRepoRoot ".venv"
} elseif (Test-Path (Join-Path $DefaultRepoRoot "claude_mcp_servers/.venv")) {
    $Venv = Join-Path $DefaultRepoRoot "claude_mcp_servers/.venv"
} else {
    $Venv = ""
}

# Auto-detect project (best-effort; helper may not exist on Windows).
$DetectPs1 = Join-Path $DefaultRepoRoot ".claude/scripts/detect-project.ps1"
if (Test-Path $DetectPs1) {
    try {
        $detected = (& pwsh -NoProfile -File $DetectPs1 $EditedFile $RepoPath 2>$null).Trim()
        if ($detected) {
            $ProjectName = $detected
            $RepoPath = Join-Path (Split-Path $RepoPath -Parent) $detected
        }
    } catch { }
}

# Only process code files.
if ($EditedFile -notmatch '\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$') {
    exit 0
}

# Resolve Python: prefer venv, fallback system.
$Python = $env:VCT_PYTHON
if (-not $Python) {
    $venvPy = Join-Path $Venv "Scripts\python.exe"
    if (Test-Path $venvPy) { $Python = $venvPy }
    else {
        $venvPy = Join-Path $Venv "bin/python"
        if (Test-Path $venvPy) { $Python = $venvPy }
    }
    if (-not $Python) {
        foreach ($c in @('python', 'py', 'python3')) {
            $cmd = Get-Command $c -ErrorAction SilentlyContinue
            if ($cmd) { $Python = $cmd.Source; break }
        }
    }
}

if (-not $Python -or -not (Test-Path $Analyzer)) { exit 0 }

# Run incremental analysis in background.
Start-Process -FilePath $Python -ArgumentList @($Analyzer, $RepoPath, '--project', $ProjectName, '--incremental') `
    -WorkingDirectory $RepoPath -WindowStyle Hidden | Out-Null

Write-Output "Code graph incremental update queued for $ProjectName"
exit 0
