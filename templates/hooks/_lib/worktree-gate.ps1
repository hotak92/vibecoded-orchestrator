# SPDX-License-Identifier: AGPL-3.0-or-later
# worktree-gate.ps1 — FIX-A' (v0.2.73): decide whether a code-graph edit that
# lives in a git LINKED WORKTREE should be SKIPPED (ephemeral/unregistered) or
# INDEXED (its canonical root is a registered launcher project).
#
# MUST MATCH templates/hooks/_lib/worktree-gate.sh :: _worktree_gate_should_skip
# (mirror cross-language logic; keep the git primitive + probe semantics
# identical). See the .sh sibling for the full WHY: parallel-subagent worktree
# edits drive the Weaviate disk write-amplification, so we skip indexing them —
# but ONLY when the worktree's canonical main root is NOT a registered launcher
# project (else a bare-repo/worktree-PRIMARY user would silently lose all
# indexing). CONSERVATIVE: skip ONLY on a definitive "not registered" probe
# (exit 2); on registered (0) OR hub-uncertain (1) we INDEX — never drop a
# legit index on doubt.

# Return $true (SKIP the edit) when the edit is in an EPHEMERAL/unregistered
# worktree; return $false (INDEX the edit) otherwise.
#
#   -EditedFile : absolute path of the edited file
#   -RepoPath   : the on-disk relativization root the hook resolved
#   -CanonRoot  : the canonical MAIN repo root (from Get-CanonicalRepoRoot)
#
# The caller passes -CanonRoot so we reuse the hook's ALREADY-computed
# canonical root (mirror-don't-fork: no second git-common-dir resolution).
# When -CanonRoot is empty or equals -RepoPath the edit is NOT in a linked
# worktree -> never skip.
function Test-EphemeralWorktreeEdit {
    param(
        [string]$EditedFile,
        [string]$RepoPath,
        [string]$CanonRoot
    )

    # Not a worktree edit (main checkout, non-git, or unresolved canonical
    # root) -> INDEX.
    if (-not $CanonRoot) { return $false }
    if ($CanonRoot -eq $RepoPath) { return $false }

    # Worktree edit: probe whether the canonical MAIN root is a registered
    # launcher project. Prefer the resolver under the canonical root; fall
    # back to the repo root's resolver.
    $resolver = Join-Path $CanonRoot ".claude/scripts/vct_project_config.ps1"
    if (-not (Test-Path -LiteralPath $resolver)) {
        $resolver = Join-Path $RepoPath ".claude/scripts/vct_project_config.ps1"
    }
    if (-not (Test-Path -LiteralPath $resolver)) {
        # No resolver -> cannot positively confirm "unregistered" ->
        # CONSERVATIVE: INDEX (never skip on uncertainty).
        return $false
    }

    # resolve-project: exit 0 = registered, exit 2 = NOT registered,
    # exit 1 = hub unreachable / uncertain. Skip ONLY on the definitive
    # exit 2. Discard stdout/stderr; we only need the exit code.
    $pwsh = (Get-Process -Id $PID).Path
    if (-not $pwsh) { $pwsh = "pwsh" }
    try {
        & $pwsh -NoProfile -File $resolver -ResolveProject $CanonRoot *> $null
        $rc = $LASTEXITCODE
    } catch {
        # Probe failed to even launch -> uncertain -> INDEX.
        return $false
    }
    if ($rc -eq 2) {
        return $true   # definitively unregistered worktree -> SKIP
    }
    # exit 0 (registered) OR exit 1 (uncertain / launcher off) -> INDEX.
    return $false
}
