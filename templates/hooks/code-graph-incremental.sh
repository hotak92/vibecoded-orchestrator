#!/usr/bin/env bash
# Parity note (v0.2.54 Track G G-6): the .ps1 sibling now resolves its
# child-spawn PowerShell binary via _lib/resolve-powershell.ps1 (pwsh ->
# powershell fallback for PS 5.1-only machines). No bash-side logic
# change is needed - bash hooks never spawn PowerShell.
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# VCO-CENTRALIZED-KG: write-side delegator (PR #171 / 0.1.7).
#   Calls .claude/scripts/analyze_code_graph.py against the project's
#   own code-graph collections (auto-detected for sibling repos via
#   detect-project.sh). Writes do NOT consult VCT_CODE_GRAPH_ACCESS_LIST
#   — that env var is read-side only (fan-out across peer codegraphs).
#   No centralization needed. See knowledge/concepts/multi-source-kg-runtime.md.

# Code Graph Incremental Update Hook
# Runs incremental code graph analysis on every code file edit.
# Triggered by PostToolUse on code file edits.
#
# Multi-codebase: if edited_file is outside repo_path but under a sibling folder,
# auto-detects the sibling project name and analyzes against its collections.
# Collections are auto-created if they don't exist (handled by analyze_code_graph.py).
#
# Joern: when joern is on PATH (or VCT_JOERN_AVAILABLE=1), analyze_code_graph.py
# defaults to extracting CFG complexity + PDG data-flow vars per function. Falls
# through cleanly when joern is missing.
#
# Usage: bash code-graph-incremental.sh <edited_file> [repo_path] [project_name]
#
# Args:
#   edited_file    - The file that was edited (required)
#   repo_path      - Repository root (default: $CLAUDE_PROJECT_DIR or pwd)
#   project_name   - Weaviate collection prefix (default: basename of repo_path)
#
# Env overrides:
#   VCT_ANALYZER_SCRIPT  - path to analyze_code_graph.py
#   VCT_PYTHON           - python interpreter (default: looks for .venv first)

set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

EDITED_FILE="$1"
REPO_PATH="${2:-${CLAUDE_PROJECT_DIR:-$(pwd)}}"

# v0.2.21 Step 18 (caller migration): when the project name arg is not
# supplied, prefer the launcher's vct-hub over the basename fallback.
# post-file-edit.sh already resolves+passes $3; this branch only fires
# for direct invocations of this hook (rare, but the project_init test
# suite + ad-hoc CLI use exercise it). Falls back to basename when the
# resolver is unavailable, preserving the pre-v0.2.21 default.
#
# v0.2.23 field switch: ask the resolver for the canonical Weaviate
# collection prefix (`code_graph_collection_prefix`), NOT the legacy
# slug-alias `code_graph_project`. The slug routes writes to a derived
# zombie prefix (slug → canonical_class_prefix → e.g. `Orchestrator_root`)
# while consumers + the launcher binding row point at the canonical
# prefix (e.g. `VibeCodedOrchestrator`). See knowledge/concepts/
# multi-codebase-code-graph-detection.md for the full diagnosis.
#
# v0.2.66 (Bug 3): factored into a function so the canonical-root
# re-resolution below (worktree dedup) reuses the SAME resolver→basename
# fallback — single source of truth, no fork. MUST MATCH
# templates/hooks/code-graph-incremental.ps1 :: Resolve-CodegraphProject.
_resolve_codegraph_project() {
    # Args: <root>. Echoes the code-graph collection prefix for <root>:
    # the vct-hub resolver's `code_graph_collection_prefix` if available,
    # else the basename of <root>.
    local _root="$1"
    local _name=""
    local _resolver="$_root/.claude/scripts/vct_project_config.sh"
    if [ -x "$_resolver" ]; then
        _name=$(
            "$_resolver" "$_root" --field code_graph_collection_prefix 2>/dev/null
        ) || _name=""
    fi
    [ -z "$_name" ] && _name="$(basename "$_root")"
    printf '%s\n' "$_name"
}

PROJECT_NAME="${3:-}"
[ -z "$PROJECT_NAME" ] && PROJECT_NAME="$(_resolve_codegraph_project "$REPO_PATH")"

# Resolve analyzer script + python from project layout (no hardcoded paths).
# Hook lives at <repo>/.claude/hooks/, so repo root is two parents up.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ANALYZER="${VCT_ANALYZER_SCRIPT:-$DEFAULT_REPO_ROOT/.claude/scripts/analyze_code_graph.py}"
# v0.2.46 post-adversarial: source shared resolver. Previous inline logic
# derived $VENV from $DEFAULT_REPO_ROOT = $SCRIPT_DIR/../.. — which in a
# user-project install is the USER's project root, not VCO's clone. The
# resulting VENV would point at the user's project venv (no weaviate-
# client + no vco_lib). Shared helper consults $VCT_INSTALL_ROOT (canonical)
# first and only falls back to clone-relative when the 2-up path looks
# like a real VCO clone (has install.py + first-install.sh).
# Resolves to a python INTERPRETER directly; we expose it via $VENV here
# for back-compat with the existing $VENV/bin/python expansion below
# (which still works because $VENV-as-interpreter still has a valid
# parent dir, just unused).
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
# VCO_VENV_PYTHON is the interpreter path. Set VENV to its grandparent
# (the venv DIR) so the existing "$VENV/bin/python" expansion below still
# locates the same interpreter. Empty → downstream falls back to system
# PATH via _lib/find-python.sh, same graceful-degrade behaviour as before.
if [ -n "${VCO_VENV_PYTHON:-}" ]; then
    VENV="$(cd "$(dirname "$VCO_VENV_PYTHON")/.." 2>/dev/null && pwd)"
else
    VENV=""
fi

# v0.2.47 (extras): BEFORE the sibling detection, check the current
# project's `code_graph_extra_paths`. If the edited file is under any of
# them, re-point REPO_PATH to that extra and KEEP the current
# PROJECT_NAME (extras index into the SAME per-project collections; they
# don't get their own prefix). This makes edits under e.g. a sibling
# `vibecoded-orchestrator/` checkout show up in the current project's
# codegraph without the sibling becoming a launcher project. See:
# knowledge/concepts/project-extra-codegraph-paths-2026-06-05.md
#
# Order of checks below (matches the spec, first match wins):
#   1. Edit under current $REPO_PATH (no override needed — existing)
#   2. NEW: Edit under any enabled extra of the CURRENT project
#   3. Edit under a sibling project folder (existing detect-project.sh)
#   4. None match — sibling detection returns "" → no-op
#
# Backwards-compatible: pre-v0.2.47 hubs lack the field → resolver exits
# 4 → EXTRAS_LIST stays empty → the loop is a no-op → existing logic
# runs unchanged. The resolver script is the same one we already called
# above for PROJECT_NAME; if it isn't executable we silently skip extras.
EXTRAS_MATCHED=0
# Check (1) first: don't even query extras for the trivial own-repo case.
# This avoids one hub round-trip per Edit in the common case and matches
# the spec's "first match wins" ordering.
case "$EDITED_FILE" in
    "$REPO_PATH"/*)
        : ;;  # under current repo — no remap; skip both extras + sibling
    *)
        _EXTRAS_RESOLVER="$REPO_PATH/.claude/scripts/vct_project_config.sh"
        if [ -x "$_EXTRAS_RESOLVER" ]; then
            # Resolver emits one path per line (enabled rows only). Exit 4
            # ("field not found") is the pre-v0.2.47-hub case — silent no-op.
            # We capture stdout-only; stderr (warnings) goes to the user.
            EXTRAS_LIST=$(
                "$_EXTRAS_RESOLVER" "$REPO_PATH" --field code_graph_extra_paths 2>/dev/null
            ) || EXTRAS_LIST=""
            if [ -n "$EXTRAS_LIST" ]; then
                # Iterate paths line-by-line; first prefix-match wins.
                # Strip any trailing slash so prefix-match is unambiguous.
                while IFS= read -r _EXTRA_PATH; do
                    [ -z "$_EXTRA_PATH" ] && continue
                    _EXTRA_PATH="${_EXTRA_PATH%/}"
                    case "$EDITED_FILE" in
                        "$_EXTRA_PATH"/*)
                            REPO_PATH="$_EXTRA_PATH"
                            EXTRAS_MATCHED=1
                            break
                            ;;
                    esac
                done <<< "$EXTRAS_LIST"
            fi
        fi
        ;;
esac

# Auto-detect project from file path (multi-codebase support) — silent if helper absent.
# v0.2.47: skip sibling detection when an extras path already claimed the file.
DETECT_HELPER="$DEFAULT_REPO_ROOT/.claude/scripts/detect-project.sh"
if [ "$EXTRAS_MATCHED" -eq 0 ] && [ -f "$DETECT_HELPER" ]; then
    # shellcheck source=/dev/null
    source "$DETECT_HELPER"
    DETECTED=$(detect_project_for_file "$EDITED_FILE" "$REPO_PATH" 2>/dev/null || true)
    if [[ -n "$DETECTED" ]]; then
        PROJECT_NAME="$DETECTED"
        REPO_PATH="$(dirname "$REPO_PATH")/$DETECTED"
    fi
fi

# Only process code files
if [[ ! "$EDITED_FILE" =~ \.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$ ]]; then
    exit 0
fi

# ── v0.2.66 (Bug 3, part c): skip pure-scratch / transient paths ───────────
# .claude/state/ is NEVER source (tool_backups snapshots, session scratch);
# indexing it pollutes the persistent collection with throwaway objects we
# found 97 of in the field. Always skip. Conservative: a non-source path
# under state/ must never reach the analyzer.
case "$EDITED_FILE" in
    */.claude/state/*)
        # Soft no-op — never index transient state snapshots.
        exit 0
        ;;
esac

# ── v0.2.66 (Bug 3, part b): canonicalize a git WORKTREE edit to its MAIN
# repo root ────────────────────────────────────────────────────────────────
# THE LEAK: the analyzer keys each object's file_path on
# `relative_to(source_root)` AND mixes the absolute source root into the
# object's deterministic UUID (V52-O.3 `project_source`). When an edited
# file lives in a git LINKED WORKTREE (or under a code_graph_extra_paths
# entry that is itself a worktree/clone), the hook set REPO_PATH = the
# worktree root, so the SAME logical file produced a DISTINCT object per
# worktree — a full duplicate set that the per-edit hook never prunes
# (--prune-stale was removed in v0.2.52). Every parallel-agent fan-out
# cycle therefore left thousands of orphan CodeFunction objects keyed under
# deleted worktree roots; the accumulated bloat is what drives Weaviate's
# compaction / tombstone-cleanup into the intermittent disk-write peaks.
#
# WHY NOT just the relative path: a file's path RELATIVE to its worktree
# root already equals its path relative to the main root (e.g.
# `templates/foo.py` either way) — so the `file_path` property does NOT
# diverge. What diverges is the object's deterministic UUID + `project_source`
# property, which mix in the ABSOLUTE source root (V52-O.3): worktree-abs-path
# != main-abs-path → distinct UUID → duplicate object.
#
# FIX: keep REPO_PATH as the worktree root (so the analyzer relativizes the
# REAL on-disk file correctly) but pass the worktree's CANONICAL MAIN repo
# root via `--canonical-source`. The analyzer stamps THAT as `project_source`
# and mixes it into the UUID, so a worktree edit and a main-checkout edit of
# the same logical file converge on the ONE canonical object (latest on-disk
# content wins). The content_hash + _get_existing_module skip paths still
# apply (unchanged file → no write). Parallel subagents on DISJOINT worktrees
# edit different files → no contention; same-file edits across worktrees
# last-writer-wins on ONE canonical object (acceptable, far better than
# duplicates) — deliberately NO locking.
#
# CONSERVATIVE-DEFAULT: if we cannot positively resolve a canonical main
# root for the edited file (no git, scratch /tmp path with no real repo),
# SKIP indexing rather than index under a transient root.
#
# MUST MATCH templates/hooks/code-graph-incremental.ps1 :: Get-CanonicalRepoRoot
# (mirror cross-language logic; keep the git primitive identical).
# v0.2.73 (FIX-B): `_canonical_repo_root` lives in the shared lib
# _lib/canonical-repo-root.sh so the end-of-turn batched drain
# (stop-codegraph-drain.sh) reuses the EXACT SAME resolver — one copy, no fork.
# shellcheck source=_lib/canonical-repo-root.sh disable=SC1091
. "$SCRIPT_DIR/_lib/canonical-repo-root.sh"

_CANON_ROOT="$(_canonical_repo_root "$EDITED_FILE")" || _CANON_ROOT=""
if [ -z "$_CANON_ROOT" ]; then
    # No git main root resolvable for the edited file. Two sub-cases:
    #   (1) the path is under the system temp dir → throwaway scratch
    #       (e.g. an agent worktree that lost its git metadata, or a /tmp
    #       fixture). NEVER index it — conservative no-op (part c).
    #   (2) a legitimate NON-GIT project on disk (no .git anywhere). There
    #       is no worktree to dedup, so canonicalization is moot; fall back
    #       to the on-disk REPO_PATH as the canonical source — preserving
    #       pre-v0.2.66 per-edit indexing for non-git codebases.
    case "$EDITED_FILE" in
        "${TMPDIR:-/tmp}"/*|/tmp/*|/var/folders/*|/var/tmp/*)
            echo "ℹ️  code-graph: transient scratch path $EDITED_FILE — skipping" >&2
            exit 0
            ;;
        *)
            _CANON_ROOT="$REPO_PATH"  # non-git project: no worktree to dedup
            ;;
    esac
fi
# REPO_PATH stays the on-disk root (worktree root for a worktree edit) so the
# analyzer relativizes the REAL file. $_CANON_ROOT is passed separately via
# --canonical-source to stamp the canonical project_source / UUID seed.

# v0.2.66 (Bug 3, CONCERN-1): the deterministic object UUID is keyed on
# {project}::{project_source}::{file_path_rel}::{full_name}. We canonicalize
# project_source (--canonical-source) and file_path_rel (REPO_PATH-relative,
# identical across worktrees) — but PROJECT_NAME was resolved against the
# WORKTREE REPO_PATH above. For a Claude-Code `isolation: worktree` ephemeral
# worktree (NOT a registered launcher project) the hub resolver misses and
# PROJECT_NAME falls back to `basename "$REPO_PATH"` = the worktree basename,
# which DIFFERS per worktree → distinct `project` → distinct UUID → the very
# duplicates this fix targets STILL accumulate. Fix: when the edit is in a
# worktree (canonical root != on-disk root) AND we are NOT in the extras case
# (extras deliberately keep the parent project), re-resolve PROJECT_NAME
# against the CANONICAL MAIN root so a worktree edit and a main-checkout edit
# produce the SAME project → SAME UUID. The basename fallback inside the
# resolver helper then uses basename of $_CANON_ROOT, not the worktree.
# Extras keep their parent PROJECT_NAME (project_source + file_path_rel still
# dedup an extra-that-is-a-worktree). MUST MATCH the .ps1 sibling.
if [ "$EXTRAS_MATCHED" -eq 0 ] && [ "$_CANON_ROOT" != "$REPO_PATH" ]; then
    PROJECT_NAME="$(_resolve_codegraph_project "$_CANON_ROOT")"
fi

# ── v0.2.73 FIX-A': skip indexing for EPHEMERAL/unregistered worktree edits ──
# Parallel subagents editing in throwaway `isolation: worktree` checkouts fire
# this hook on every edit; each sync hits the big CodeFunction collection's
# insert-time churn, and N concurrent worktrees multiply it into the measured
# disk write-amplification. Maintainer directive: "only track main and not
# track worktrees at all." BUT (R1 correctness hole) a user whose PRIMARY
# checkout IS a linked worktree (bare-repo layout) must NOT lose indexing — so
# we skip ONLY when the edit is in a worktree AND its canonical main root is
# NOT a registered launcher project. Extras deliberately keep their parent
# project (an extra-that-is-a-worktree still dedups via project_source +
# file_path_rel), so this gate never fires for the extras case.
# CONSERVATIVE: the shared helper skips ONLY on a definitive "not registered"
# probe result; on registered OR hub-uncertain it returns "index" (never drop
# a legit index on doubt). MUST MATCH the .ps1 sibling.
if [ "$EXTRAS_MATCHED" -eq 0 ]; then
    # shellcheck source=_lib/worktree-gate.sh disable=SC1091
    [ -f "$SCRIPT_DIR/_lib/worktree-gate.sh" ] && . "$SCRIPT_DIR/_lib/worktree-gate.sh"
    if command -v _worktree_gate_should_skip >/dev/null 2>&1 \
        && _worktree_gate_should_skip "$EDITED_FILE" "$REPO_PATH" "$_CANON_ROOT"; then
        # Detectable log line so the (deliberate) staleness is observable.
        echo "ℹ️  code-graph: skipped worktree edit (ephemeral/unregistered) $EDITED_FILE" >&2
        exit 0
    fi
fi

# Resolve python: prefer venv, fall back to system python (cross-OS).
# Bare `python3` is missing on Windows (only python.exe / py exist).
# Try POSIX venv layout first, then Windows venv layout, then system PATH
# via _lib/find-python.sh (python3 → python → py).
PYTHON="${VCT_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    SCRIPT_DIR_CGI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -x "$VENV/bin/python" ]; then
        PYTHON="$VENV/bin/python"
    elif [ -x "$VENV/Scripts/python.exe" ]; then
        PYTHON="$VENV/Scripts/python.exe"
    else
        # shellcheck source=_lib/find-python.sh disable=SC1091
        [ -f "$SCRIPT_DIR_CGI/_lib/find-python.sh" ] && . "$SCRIPT_DIR_CGI/_lib/find-python.sh"
        PYTHON="${PY:-}"
    fi
fi

if [ -z "$PYTHON" ] || [ ! -f "$ANALYZER" ]; then
    # Silent fallback — orchestrator works fine without code graph
    exit 0
fi

# ── v0.2.72 (P5): resolve the `.claude/` gate ──────────────────────────────
# For a user project `.claude/` is orchestrator-GENERATED tooling (noise);
# for the orchestrator clone it's first-party source. Resolution order:
#   1. hub resolver field `code_graph_index_dot_claude` (per-project bool,
#      written by T-GUI-DB) — authoritative when the launcher is running.
#   2. root-detection fallback (hub down / pre-T-GUI-DB hub): index only
#      when $_CANON_ROOT looks like the orchestrator clone (has vco_lib/ +
#      .claude/). CONSERVATIVE-DEFAULT: soft-fail to EXCLUDE (--no-index-
#      dot-claude) so a user project never re-injects generated tooling.
# MUST MATCH templates/hooks/code-graph-incremental.ps1 :: Resolve-IndexDotClaude
# and analyze_code_graph.py :: _looks_like_orchestrator_root (cross-language).
_resolve_index_dot_claude() {
    # Args: <root>. Echoes "1" (index) or "0" (exclude).
    local _root="$1"
    local _val=""
    local _resolver="$_root/.claude/scripts/vct_project_config.sh"
    if [ -x "$_resolver" ]; then
        _val=$(
            "$_resolver" "$_root" --field code_graph_index_dot_claude 2>/dev/null
        ) || _val=""
    fi
    case "$_val" in
        true|True|TRUE|1)  printf '1\n'; return 0 ;;
        false|False|FALSE|0) printf '0\n'; return 0 ;;
    esac
    # Field absent (hub down / old hub) → root-detection fallback.
    if [ -d "$_root/vco_lib" ] && [ -d "$_root/.claude" ]; then
        printf '1\n'
    else
        printf '0\n'
    fi
}
_INDEX_DOT_CLAUDE="$(_resolve_index_dot_claude "$_CANON_ROOT")"
if [ "$_INDEX_DOT_CLAUDE" = "1" ]; then
    _DOT_CLAUDE_FLAG="--index-dot-claude"
else
    _DOT_CLAUDE_FLAG="--no-index-dot-claude"
fi

# Run single-file analysis in background (no internal debounce — the
# per-file coalescing is handled by post-file-edit.sh's debounce layer).
# --cfg/--pdg default to ON inside analyze_code_graph.py when joern is
# present; silent fallback when absent. To disable, set VCT_JOERN_AVAILABLE=0.
# In single-file mode the analyzer scopes Joern's CPG build to the one
# edited file too, so this stays cheap per-edit.
#
# v0.2.66 (Bug 3): switched from `"$REPO_PATH" --incremental` to
# `"$REPO_PATH" --only-file "$EDITED_FILE"`. The old `--incremental` ran
# `git diff --name-only HEAD~1 HEAD` and re-analyzed EVERY file in the
# previous commit (dozens in an active cycle), which both:
#   1. amplified writes — re-parsing + re-hashing all HEAD~1..HEAD files
#      on every single edit drove the multi-hundred-MiB/s disk peaks; and
#   2. was WRONG — the file the user just edited is uncommitted (working
#      tree), so it was never in HEAD~1..HEAD at all. The per-edit sync
#      re-churned the PREVIOUS commit's files and never indexed the edit.
#
# --only-file passes the actual edited file. `$REPO_PATH` stays the
# relativization root (collections key on repo-relative paths). The
# analyzer routes the single file through the SAME per-file
# (`_get_existing_module`, keyed on path+hash) and per-object
# (`_dedup_insert` content_hash) skip paths, so an unchanged or trivially-
# edited file writes ~0 objects — "just the hashes" make it a near-no-op.
#
# v0.2.72 (P7): the per-object skip also honors the embedding-revision gate.
# When an edited file contains a Function/Class whose stored `embed_revision`
# is behind CODEGRAPH_EMBED_REVISION (a row still embedded under the pre-P3
# pre-chunking scheme), the analyzer FORCES its re-embed even if the body is
# byte-identical — so a revision mismatch counts as "stale" here and the
# edited file's stale rows self-heal on this incremental run. Whole-project
# stale rows in unedited files are re-embedded by the background resync
# (install.py --update → vco_lib.codegraph_resync), not per-edit.
# No repo-wide prune happens (single-file mode never deletes other files'
# rows), so this also can't re-introduce the V52-O.7 prune-deletes-other-
# rows regression.
(
    cd "$REPO_PATH"
    "$PYTHON" "$ANALYZER" \
        "$REPO_PATH" \
        --project "$PROJECT_NAME" \
        --only-file "$EDITED_FILE" \
        --canonical-source "$_CANON_ROOT" \
        "$_DOT_CLAUDE_FLAG" \
        2>&1 | tail -5
) &

echo "📊 Code graph incremental update queued for $PROJECT_NAME"
