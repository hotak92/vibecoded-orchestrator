# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# canonical-repo-root.sh — resolve a git LINKED WORKTREE edit to its MAIN repo
# root (v0.2.66 Bug 3; extracted to a shared lib in v0.2.73 FIX-B so BOTH the
# per-edit code-graph hook AND the end-of-turn batched drain use ONE copy —
# mirror-don't-fork).
#
# `--git-common-dir` resolves a linked worktree's shared .git dir to the MAIN
# repo's `.git` (for the main checkout it is the same path). The main repo root
# is its dirname. `--path-format=absolute` (git >= 2.31) makes the relative
# ".git" of a main checkout absolute too; older git falls back to resolving the
# (possibly-relative) value against the file's dir.
#
# MUST MATCH templates/hooks/code-graph-incremental.ps1 :: Get-CanonicalRepoRoot
# (mirror cross-language logic; keep the git primitive identical).

# _canonical_repo_root <edited_file>
#   Echoes the canonical main repo root (absolute, symlink-resolved) on success;
#   echoes nothing + returns non-zero when no git main root is resolvable.
_canonical_repo_root() {
    local _file="$1"
    local _dir
    _dir="$(dirname "$_file")"
    command -v git >/dev/null 2>&1 || return 1
    local _common
    _common="$(git -C "$_dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || _common=""
    if [ -z "$_common" ]; then
        _common="$(git -C "$_dir" rev-parse --git-common-dir 2>/dev/null)" || return 1
        [ -z "$_common" ] && return 1
        case "$_common" in
            /*) : ;;                       # already absolute
            *)  _common="$_dir/$_common" ;; # relative (".git") → make absolute
        esac
    fi
    local _root="${_common%/}"
    _root="${_root%/.git}"
    [ -d "$_root" ] || return 1
    # NORMALIZE to an absolute, symlink-resolved path (macOS /tmp→/private,
    # older-git ".../src/../.." unnormalized values) so a worktree edit's
    # stamped root EXACTLY equals the main-checkout's normalized value.
    local _norm
    _norm="$(cd "$_root" 2>/dev/null && pwd -P)" || return 1
    [ -n "$_norm" ] || return 1
    printf '%s\n' "$_norm"
}
