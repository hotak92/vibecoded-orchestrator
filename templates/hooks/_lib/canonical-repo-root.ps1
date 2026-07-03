# SPDX-License-Identifier: AGPL-3.0-or-later
# canonical-repo-root.ps1 — resolve a git LINKED WORKTREE edit to its MAIN repo
# root. Extracted to a shared lib in v0.2.73 FIX-B so BOTH the per-edit
# code-graph hook AND the end-of-turn batched drain use ONE copy.
#
# MUST MATCH templates/hooks/_lib/canonical-repo-root.sh :: _canonical_repo_root
# (mirror cross-language logic; keep the git primitive identical).

function Get-CanonicalRepoRoot {
    param([string]$File)
    $dir = Split-Path -Parent $File
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return "" }
    # --git-common-dir resolves a linked worktree's shared .git to the MAIN
    # repo's .git (same for the main checkout). --path-format=absolute
    # (git >= 2.31) makes the relative ".git" of a main checkout absolute;
    # older git falls back to resolving the bare value against $dir.
    $common = (& git -C $dir rev-parse --path-format=absolute --git-common-dir 2>$null)
    if (-not $common) {
        $common = (& git -C $dir rev-parse --git-common-dir 2>$null)
        if (-not $common) { return "" }
        if (-not ([System.IO.Path]::IsPathRooted($common))) {
            $common = Join-Path $dir $common
        }
    }
    $root = $common.TrimEnd('/', '\')
    if ($root.EndsWith("/.git") -or $root.EndsWith("\.git")) {
        $root = Split-Path -Parent $root
    }
    if (-not (Test-Path -PathType Container $root)) { return "" }
    try { $root = (Resolve-Path -LiteralPath $root).Path } catch { }
    return $root
}
