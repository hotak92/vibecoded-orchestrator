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
# v0.2.46 post-adversarial: dot-source shared resolver. Previous inline
# logic derived $Venv from $DefaultRepoRoot = $ScriptDir/../.. — which
# in a user-project install is the USER's project root, not VCO's clone.
# The resulting $Venv would point at the user's project venv (no
# weaviate-client + no vco_lib). Shared helper consults $VCT_INSTALL_ROOT
# (canonical) first and only falls back to clone-relative when the 2-up
# path looks like a real VCO clone (has install.py + first-install.sh).
# Returns a python INTERPRETER path; we expose it via $Venv (= its
# grandparent dir) for back-compat with the downstream $Venv/bin/python
# expansion that some call sites still rely on. PR-25 / v0.2.12 dual-
# layout history preserved in the helper's docstring.
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
$VcoVenvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir
if ($VcoVenvPy) {
    $Venv = (Resolve-Path (Join-Path (Split-Path -Parent $VcoVenvPy) "..")).Path
} else {
    $Venv = ""
}

# v0.2.47 (extras) — parity with the .sh sibling. BEFORE sibling detection,
# query the current project's `code_graph_extra_paths`. If the edited file
# is under any enabled extra, set RepoPath = that extra and keep
# ProjectName (extras index into the SAME per-project collections).
# Backwards-compatible: pre-v0.2.47 hubs lack the field; the resolver
# exits 4 and the loop is a no-op.
$ExtrasMatched = $false
if ($EditedFile -notlike "$RepoPath/*" -and $EditedFile -notlike "$RepoPath\*") {
    $ExtrasResolver = Join-Path $RepoPath ".claude/scripts/vct_project_config.ps1"
    if (Test-Path $ExtrasResolver) {
        try {
            $extras = (& pwsh -NoProfile -File $ExtrasResolver -Project $RepoPath -Field "code_graph_extra_paths" 2>$null)
            if ($extras) {
                foreach ($_line in ($extras -split "`n")) {
                    $extraPath = $_line.Trim().TrimEnd('/').TrimEnd('\')
                    if (-not $extraPath) { continue }
                    if ($EditedFile -like "$extraPath/*" -or $EditedFile -like "$extraPath\*") {
                        $RepoPath = $extraPath
                        $ExtrasMatched = $true
                        break
                    }
                }
            }
        } catch { }
    }
}

# Auto-detect project (best-effort; helper may not exist on Windows).
# v0.2.47: skip when extras already claimed the edited file.
$DetectPs1 = Join-Path $DefaultRepoRoot ".claude/scripts/detect-project.ps1"
if (-not $ExtrasMatched -and (Test-Path $DetectPs1)) {
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

# v0.2.18 (Plan C) mapped the edited file's extension to the analyzer's
# canonical language ID and passed `--language $Lang --prune-stale` to
# scope the prune to "rows of this language not visited this run".
#
# v0.2.52 V52-O.7 (2026-06-09): the $Lang mapping is now UNUSED in the
# analyzer invocation below because `--prune-stale` was dropped (see audit
# a97f0d9 — `_prune_collection` iterated the WHOLE collection per run, so
# every Python edit deleted ALL other Python rows; collection went to 0
# Python rows over time). The mapping stays in place because the proper
# architectural fix queued for v0.2.53 (scope prune to the EDITED FILE
# only, not language-wide) will need this $Lang hint. Don't delete the
# block — it's load-bearing for the v0.2.53 follow-up.
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
# v0.2.52 V52-O.7 (2026-06-09): DROPPED `--prune-stale --language=$Lang`.
# v0.2.18 added them as "Plan C" intending a language-scoped prune. But
# `_prune_collection` (analyze_code_graph.py:1889) iterates the ENTIRE
# collection and deletes every row tagged with that language that wasn't
# visited THIS run. Incremental runs visit ~1 file at a time (HEAD~1..HEAD
# diff) so every Python edit deleted all OTHER Python rows. Audit a97f0d9
# (2026-06-09) confirmed: `VibeCodedOrchestrator_CodeFunction` had 5365
# rows of which **0 were Python**. This was the PRIMARY root cause of
# zero-Python-indexed. Fix: drop the flags; stale rows leak until a full
# reanalyze (V52-O.2 `scripts/v0252_codegraph_reset.sh`).
$args = @($Analyzer, $RepoPath, '--project', $ProjectName, '--incremental')
Start-Process -FilePath $Python -ArgumentList $args `
    -WorkingDirectory $RepoPath -WindowStyle Hidden | Out-Null

Write-Output "Code graph incremental update queued for $ProjectName"
exit 0
