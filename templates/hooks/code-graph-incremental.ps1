# OS-EXEMPT-PARITY: .sh sibling uses the hub resolver
#   (vct_project_config.sh --field code_graph_collection_prefix); this
#   .ps1 uses a different design path (`detect-project.ps1` helper +
#   `Split-Path -Leaf` fallback), so the v0.2.23 W1 slug-vs-prefix
#   resolver-field switch in the .sh has no corresponding change here.
#   Marking the exemption explicitly so the OS-parity CI gate accepts
#   the asymmetric modification. See knowledge/concepts/multi-codebase-code-graph-detection.md.
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

# v0.2.18 (Plan C): map the edited file's extension to the analyzer's
# canonical language ID. Mirror of the .sh sibling's case statement.
# When the extension is recognised, the hook invokes the analyzer with
# --language=$LANG + --prune-stale so the language-scoped prune runs
# (deletes only the matching-language rows the analyzer didn't visit
# this run). Empty $Lang (unrecognised extension) falls back to plain
# --incremental for backward compat.
$Lang = ""
switch -Regex ($EditedFile) {
    '\.py$'                          { $Lang = "python";     break }
    '\.(js|mjs|jsx)$'                { $Lang = "javascript"; break }
    '\.(ts|tsx)$'                    { $Lang = "typescript"; break }
    '\.go$'                          { $Lang = "go";         break }
    '\.rs$'                          { $Lang = "rust";       break }
    '\.lua$'                         { $Lang = "lua";        break }
    '\.(cpp|cc|cxx|h|hpp)$'          { $Lang = "cpp";        break }
    '\.c$'                           { $Lang = "c";          break }
    '\.cs$'                          { $Lang = "csharp";     break }
    '\.java$'                        { $Lang = "java";       break }
    '\.rb$'                          { $Lang = "ruby";       break }
    '\.proto$'                       { $Lang = "proto";      break }
    '\.(sh|bash)$'                   { $Lang = "shell";      break }
    default                          { $Lang = "" }
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
# v0.2.18 (Plan C): when extension is recognised, pass --language + --prune-stale
# so the language-scoped prune runs and stale rows for deleted files are cleaned
# up. Empty $Lang (unknown extension) falls back to plain --incremental for
# backward compat with legacy file types.
if ($Lang) {
    $args = @($Analyzer, $RepoPath, '--project', $ProjectName, '--incremental', '--language', $Lang, '--prune-stale')
} else {
    $args = @($Analyzer, $RepoPath, '--project', $ProjectName, '--incremental')
}
Start-Process -FilePath $Python -ArgumentList $args `
    -WorkingDirectory $RepoPath -WindowStyle Hidden | Out-Null

Write-Output "Code graph incremental update queued for $ProjectName"
exit 0
