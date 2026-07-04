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
# v0.2.54 Track G (G-6): child spawns used a hardcoded `pwsh` (absent on
# PowerShell 5.1-only machines). $PsExe resolves pwsh -> powershell.
. "$PSScriptRoot/_lib/resolve-powershell.ps1"

param(
    [Parameter(Position=0,Mandatory=$true)] [string]$EditedFile,
    [Parameter(Position=1)] [string]$RepoPath = "",
    [Parameter(Position=2)] [string]$ProjectName = ""
)

if (-not $RepoPath) {
    $RepoPath = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
}

# v0.2.66 (Bug 3): resolve the code-graph collection prefix for a root via
# the vct-hub resolver (code_graph_collection_prefix), else the basename of
# the root. Factored so the canonical-root re-resolution below (worktree
# dedup) reuses the SAME fallback. MUST MATCH
# templates/hooks/code-graph-incremental.sh :: _resolve_codegraph_project.
function Resolve-CodegraphProject {
    param([string]$Root)
    $name = ""
    $resolver = Join-Path $Root ".claude/scripts/vct_project_config.ps1"
    if (Test-Path $resolver) {
        try {
            $name = (& $PsExe -NoProfile -File $resolver -Project $Root -Field "code_graph_collection_prefix" 2>$null)
        } catch { $name = "" }
    }
    if (-not $name) { $name = Split-Path $Root -Leaf }
    return ("" + $name).Trim()
}

if (-not $ProjectName) { $ProjectName = Resolve-CodegraphProject -Root $RepoPath }

# v0.2.72 (P5): resolve the `.claude/` gate. For a user project `.claude/`
# is orchestrator-GENERATED tooling (noise); for the orchestrator clone it's
# first-party source. Resolution: hub resolver field
# `code_graph_index_dot_claude` (per-project bool from T-GUI-DB), else
# root-detection fallback (index only when the root has vco_lib/ + .claude/).
# CONSERVATIVE-DEFAULT: soft-fail to EXCLUDE. MUST MATCH
# templates/hooks/code-graph-incremental.sh :: _resolve_index_dot_claude
# and analyze_code_graph.py :: _looks_like_orchestrator_root.
function Resolve-IndexDotClaude {
    param([string]$Root)
    $val = ""
    $resolver = Join-Path $Root ".claude/scripts/vct_project_config.ps1"
    if (Test-Path $resolver) {
        try {
            $val = (& $PsExe -NoProfile -File $resolver -Project $Root -Field "code_graph_index_dot_claude" 2>$null)
        } catch { $val = "" }
    }
    switch (("" + $val).Trim()) {
        { $_ -in 'true','True','TRUE','1' }    { return $true }
        { $_ -in 'false','False','FALSE','0' } { return $false }
    }
    # Field absent (hub down / old hub) -> root-detection fallback.
    return ((Test-Path (Join-Path $Root "vco_lib") -PathType Container) -and
            (Test-Path (Join-Path $Root ".claude") -PathType Container))
}

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
            $extras = (& $PsExe -NoProfile -File $ExtrasResolver -Project $RepoPath -Field "code_graph_extra_paths" 2>$null)
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
        $detected = (& $PsExe -NoProfile -File $DetectPs1 $EditedFile $RepoPath 2>$null).Trim()
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

# v0.2.66 (Bug 3, part c): skip pure-scratch / transient paths.
# .claude/state/ is NEVER source (tool_backups snapshots, session scratch).
# Always skip — must match the .sh sibling's `*/.claude/state/*` case.
if ($EditedFile -match '[\\/]\.claude[\\/]state[\\/]') {
    exit 0
}

# v0.2.66 (Bug 3, part b): canonicalize a git WORKTREE edit to its MAIN repo
# root. MUST MATCH templates/hooks/code-graph-incremental.sh ::
# _canonical_repo_root (mirror cross-language logic; keep the git primitive
# identical). See the .sh sibling for the full WHY: a worktree edit otherwise
# mints a per-worktree DUPLICATE object (the absolute source root feeds the
# deterministic UUID), and those orphans accumulate across fan-out cycles into
# the Weaviate-compaction disk-write peaks. We keep $RepoPath as the on-disk
# root (so the analyzer relativizes the REAL file) but pass the canonical MAIN
# root via --canonical-source so the object converges on ONE canonical row.
# v0.2.73 (FIX-B): Get-CanonicalRepoRoot lives in the shared lib
# _lib/canonical-repo-root.ps1 so the end-of-turn batched drain
# (stop-codegraph-drain.ps1) reuses the EXACT SAME resolver — one copy, no fork.
. "$PSScriptRoot/_lib/canonical-repo-root.ps1"

$CanonRoot = Get-CanonicalRepoRoot -File $EditedFile
if (-not $CanonRoot) {
    # No git main root resolvable. Two sub-cases (must match the .sh sibling):
    #   (1) under the system temp dir -> throwaway scratch -> NEVER index.
    #   (2) a legitimate NON-GIT project -> no worktree to dedup -> fall back
    #       to the on-disk $RepoPath as canonical source (preserve indexing).
    $tmp = [System.IO.Path]::GetTempPath().TrimEnd('/', '\')
    $edFull = try { [System.IO.Path]::GetFullPath($EditedFile) } catch { $EditedFile }
    if ($edFull.StartsWith($tmp, [System.StringComparison]::OrdinalIgnoreCase) -or
        $EditedFile -match '^([\\/]tmp[\\/]|[\\/]var[\\/](folders|tmp)[\\/])') {
        [Console]::Error.WriteLine("code-graph: transient scratch path $EditedFile - skipping")
        exit 0
    }
    $CanonRoot = $RepoPath  # non-git project: no worktree to dedup
}
# $RepoPath stays the on-disk root; $CanonRoot is passed via --canonical-source.

# v0.2.66 (Bug 3, CONCERN-1): the object UUID is keyed on
# {project}::{project_source}::{file_path_rel}::{full_name}. We canonicalize
# project_source (--canonical-source) + file_path_rel, but $ProjectName was
# resolved against the WORKTREE $RepoPath above. For an ephemeral
# `isolation: worktree` worktree (not a registered project) it falls back to
# the worktree basename, which DIFFERS per worktree -> distinct project ->
# distinct UUID -> duplicates STILL accumulate. Fix: for a worktree edit
# (canonical root != on-disk root) that is NOT the extras case, re-resolve
# $ProjectName against the CANONICAL MAIN root so a worktree edit and a
# main-checkout edit converge on the SAME project -> SAME UUID. Extras keep
# their parent $ProjectName. MUST MATCH the .sh sibling.
if (-not $ExtrasMatched -and $CanonRoot -ne $RepoPath) {
    $ProjectName = Resolve-CodegraphProject -Root $CanonRoot
}

# ── v0.2.73 FIX-A': skip indexing for EPHEMERAL/unregistered worktree edits ──
# Parallel-subagent worktree edits drive the Weaviate disk write-amplification;
# skip them UNLESS the worktree's canonical main root is a registered launcher
# project (else a bare-repo/worktree-PRIMARY user loses all indexing). Extras
# keep their parent project, so this never fires for the extras case.
# CONSERVATIVE: skip ONLY on a definitive "not registered" probe.
# MUST MATCH the .sh sibling.
if (-not $ExtrasMatched) {
    $wtGate = Join-Path $PSScriptRoot "_lib/worktree-gate.ps1"
    if (Test-Path -LiteralPath $wtGate) {
        . $wtGate
        if (Test-EphemeralWorktreeEdit -EditedFile $EditedFile -RepoPath $RepoPath -CanonRoot $CanonRoot) {
            [Console]::Error.WriteLine("code-graph: skipped worktree edit (ephemeral/unregistered) $EditedFile")
            exit 0
        }
    }
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

# Run single-file analysis in background.
# v0.2.66 (Bug 3): switched from `$RepoPath --incremental` to
# `$RepoPath --only-file $EditedFile` (mirrors the .sh sibling). The old
# `--incremental` ran `git diff --name-only HEAD~1 HEAD` and re-analyzed
# EVERY file in the previous commit (dozens in an active cycle), which both:
#   1. amplified writes — re-parsing + re-hashing all HEAD~1..HEAD files on
#      every single edit drove the multi-hundred-MiB/s disk peaks; and
#   2. was WRONG — the file the user just edited is uncommitted (working
#      tree), so it was never in HEAD~1..HEAD at all. The per-edit sync
#      re-churned the PREVIOUS commit's files and never indexed the edit.
#
# --only-file passes the actual edited file. $RepoPath stays the on-disk
# relativization root (collections key on repo-relative paths). The analyzer
# routes the single file through the SAME per-file (_get_existing_module,
# keyed on path+hash) and per-object (_dedup_insert content_hash) skip paths,
# so an unchanged or trivially-edited file writes ~0 objects. No repo-wide
# prune happens, so this can't re-introduce the V52-O.7 prune-deletes-other-
# rows regression.
# --canonical-source $CanonRoot dedups git-worktree edits onto the
# main-checkout object (Bug 3 part b — mirrors the .sh sibling).
#
# v0.2.72 (P7): the per-object skip also honors the embedding-revision gate.
# When an edited file contains a Function/Class whose stored embed_revision is
# behind CODEGRAPH_EMBED_REVISION (a row still embedded under the pre-P3
# pre-chunking scheme), the analyzer FORCES its re-embed even if the body is
# byte-identical — so a revision mismatch counts as "stale" here and the edited
# file's stale rows self-heal on this incremental run. Whole-project stale rows
# in unedited files are re-embedded by the background resync (install.py
# --update -> vco_lib.codegraph_resync), not per-edit. Mirrors the .sh sibling.
#
# v0.2.72 (P5): resolve + append the `.claude/` gate flag (mirrors the .sh
# sibling's $_DOT_CLAUDE_FLAG). Resolved against $CanonRoot so a worktree edit
# uses the canonical main root's setting.
$IndexDotClaude = Resolve-IndexDotClaude -Root $CanonRoot
$DotClaudeFlag = if ($IndexDotClaude) { '--index-dot-claude' } else { '--no-index-dot-claude' }
$analyzerArgs = @($Analyzer, $RepoPath, '--project', $ProjectName, '--only-file', $EditedFile, '--canonical-source', $CanonRoot, $DotClaudeFlag)
Start-Process -FilePath $Python -ArgumentList $analyzerArgs `
    -WorkingDirectory $RepoPath -WindowStyle Hidden | Out-Null

Write-Output "Code graph incremental update queued for $ProjectName"
exit 0
