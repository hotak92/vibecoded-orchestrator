# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# worktree-gate.sh — FIX-A' (v0.2.73): decide whether a code-graph edit that
# lives in a git LINKED WORKTREE should be SKIPPED (ephemeral/unregistered) or
# INDEXED (its canonical root is a registered launcher project).
#
# WHY THIS EXISTS (disk write-amplification, 2026-07-03)
# -----------------------------------------------------
# Parallel subagents editing in throwaway `isolation: worktree` checkouts fire
# the code-graph incremental hook on every edit; each sync hits the big
# CodeFunction collection's insert-time HNSW churn. Multiply by N concurrent
# worktrees and the write volume explodes (measured 857 GB/8h vs a 5 GB
# dataset). The clean fix (maintainer directive: "only track main and not
# track worktrees at all") is to SKIP indexing entirely for edits in an
# EPHEMERAL worktree — the main session's sync (or an explicit reanalyze)
# covers the merged result once work lands on main.
#
# THE CORRECTNESS HOLE THIS GATE CLOSES (plan-review R1)
# -----------------------------------------------------
# "Skip ALL worktree edits" is WRONG for a user whose PRIMARY checkout is
# itself a linked worktree (bare-repo + worktrees layout). For them EVERY edit
# is in a worktree → every edit skipped → their code graph is NEVER built,
# silently. So the gate is NOT "is this a worktree?" but "is this an
# EPHEMERAL / non-registered worktree?": skip ONLY when the edit is in a
# worktree AND its canonical main root is NOT a registered launcher project.
# A worktree whose canonical root IS a registered project still indexes (under
# the canonical prefix — code-graph-incremental.sh:314-316 already guarantees
# the prefix converges via --canonical-source).
#
# CONSERVATIVE-DEFAULT (never drop a legit index)
# -----------------------------------------------
# The registered-project probe (`vct_project_config.sh resolve-project`)
# returns exit 0 = registered, exit 2 = definitively NOT registered
# (404/400), exit 1 = hub unreachable / uncertain. We SKIP only on the
# definitive exit 2. On exit 0 (registered) OR exit 1 (uncertain / launcher
# off) we INDEX — a worktree edit that reaches the analyzer is already deduped
# onto the canonical object via --canonical-source, so indexing-when-unsure is
# safe and cheap; skipping-when-unsure could silently lose a real project's
# graph. Bias = index, never skip on doubt.
#
# NOT a submodule guard: a submodule has git-dir == git-common-dir (classifies
# as MAIN → indexes). That is fine; this gate only concerns linked worktrees.
#
# MUST MATCH templates/hooks/_lib/worktree-gate.ps1 :: Test-EphemeralWorktreeEdit
# (mirror cross-language logic; keep the git primitive + probe semantics
# identical).

# Return 0 (TRUE — SKIP the edit) when the edit is in an EPHEMERAL/unregistered
# worktree; return 1 (FALSE — INDEX the edit) otherwise.
#
#   $1 = edited_file (absolute path)
#   $2 = repo_path (the on-disk relativization root the hook resolved)
#   $3 = canon_root (the canonical MAIN repo root, from _canonical_repo_root)
#
# The caller passes $3 so we reuse the hook's ALREADY-computed canonical root
# (mirror-don't-fork: no second git-common-dir resolution). When $3 is empty
# or equals $2 the edit is NOT in a linked worktree → never skip.
_worktree_gate_should_skip() {
    _wg_file="$1"
    _wg_repo="$2"
    _wg_canon="$3"

    # Not a worktree edit (main checkout, non-git, or unresolved canonical
    # root) → INDEX. Only a canonical root that DIFFERS from the on-disk root
    # signals a linked worktree.
    [ -z "$_wg_canon" ] && return 1
    # v0.2.73 Stage-1 PLATFORM MEDIUM-2: `_wg_canon` is symlink-resolved
    # (`_canonical_repo_root` ends in `pwd -P`), but `_wg_repo` is the raw
    # hook-resolved path (may be `$(pwd)` or CLAUDE_PROJECT_DIR, NOT physical).
    # On macOS (/tmp vs /private/tmp) or a symlinked checkout, a MAIN-tree edit
    # would then have `_wg_canon != _wg_repo` → mis-classified as a worktree →
    # SILENTLY SKIPPED (the real project stops indexing). Normalize `_wg_repo`
    # to the SAME physical form before comparing so like is compared with like.
    _wg_repo_norm="$(cd "$_wg_repo" 2>/dev/null && pwd -P)" || _wg_repo_norm="$_wg_repo"
    [ "$_wg_canon" = "$_wg_repo_norm" ] && return 1

    # Worktree edit: probe whether the canonical MAIN root is a registered
    # launcher project. The resolver lives under the CANONICAL root's
    # .claude/ (a linked worktree shares the main checkout's tooling only if
    # it has its own .claude/; the canonical root always does). Probe with the
    # resolver found under the canonical root, falling back to the repo root's
    # resolver so an ephemeral worktree (which has its own .claude/scripts/)
    # can still run the query against the canonical folder.
    _wg_resolver="$_wg_canon/.claude/scripts/vct_project_config.sh"
    if [ ! -x "$_wg_resolver" ]; then
        _wg_resolver="$_wg_repo/.claude/scripts/vct_project_config.sh"
    fi
    if [ ! -x "$_wg_resolver" ]; then
        # No resolver at all → cannot positively confirm "unregistered" →
        # CONSERVATIVE: INDEX (never skip on uncertainty).
        return 1
    fi

    # resolve-project: exit 0 = registered, exit 2 = NOT registered,
    # exit 1 = hub unreachable / uncertain. Skip ONLY on the definitive
    # exit 2. Discard stdout (the project id / warnings) — we only need the
    # exit code.
    "$_wg_resolver" resolve-project "$_wg_canon" >/dev/null 2>&1
    _wg_rc=$?
    if [ "$_wg_rc" -eq 2 ]; then
        return 0   # definitively unregistered worktree → SKIP
    fi
    # exit 0 (registered) OR exit 1 (uncertain / launcher off) → INDEX.
    return 1
}
