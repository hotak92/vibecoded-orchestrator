#!/usr/bin/env bash
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
PROJECT_NAME="${3:-}"
if [ -z "$PROJECT_NAME" ]; then
    _RESOLVER="$REPO_PATH/.claude/scripts/vct_project_config.sh"
    if [ -x "$_RESOLVER" ]; then
        PROJECT_NAME=$(
            "$_RESOLVER" "$REPO_PATH" --field code_graph_collection_prefix 2>/dev/null
        ) || PROJECT_NAME=""
    fi
fi
[ -z "$PROJECT_NAME" ] && PROJECT_NAME="$(basename "$REPO_PATH")"

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

# v0.2.18 (Plan C) mapped the edited file's extension to the analyzer's
# canonical language ID and passed `--language $LANG --prune-stale` to
# scope the prune to "rows of this language not visited this run".
#
# v0.2.52 V52-O.7 (2026-06-09): the LANG mapping is now UNUSED in the
# analyzer invocation below because `--prune-stale` was dropped (see audit
# a97f0d9 — `_prune_collection` iterated the WHOLE collection per run, so
# every Python edit deleted ALL other Python rows; collection went to 0
# Python rows over time). The mapping stays in place because the proper
# architectural fix queued for v0.2.53 (scope prune to the EDITED FILE
# only, not language-wide) will need this LANG hint. Don't delete the
# block — it's load-bearing for the v0.2.53 follow-up.
LANG=""
case "$EDITED_FILE" in
    *.py)                                LANG="python"     ;;
    *.js|*.mjs|*.jsx)                    LANG="javascript" ;;
    *.ts|*.tsx)                          LANG="typescript" ;;
    *.go)                                LANG="go"         ;;
    *.rs)                                LANG="rust"       ;;
    *.lua)                               LANG="lua"        ;;
    *.cpp|*.cc|*.cxx|*.h|*.hpp)          LANG="cpp"        ;;
    *.c)                                 LANG="c"          ;;
    *.cs)                                LANG="csharp"     ;;
    *.java)                              LANG="java"       ;;
    *.rb)                                LANG="ruby"       ;;
    *.proto)                             LANG="proto"      ;;
    *.sh|*.bash)                         LANG="shell"      ;;
    *)                                   LANG=""           ;;
esac

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

# Run incremental analysis in background (no debounce — keep code graph fresh).
# --cfg/--pdg default to ON inside analyze_code_graph.py when joern is present;
# silent fallback when absent. To disable, set VCT_JOERN_AVAILABLE=0.
#
# v0.2.52 V52-O.7 (2026-06-09): DROPPED `--prune-stale --language=$LANG`.
# v0.2.18 added them as "Plan C" intending a language-scoped prune. But
# `_prune_collection` (analyze_code_graph.py:1889) iterates the ENTIRE
# collection and deletes every row tagged with that language that wasn't
# visited THIS run. Incremental runs visit ~1 file at a time (HEAD~1..HEAD
# diff) so every Python edit deleted all OTHER Python rows. Audit a97f0d9
# (2026-06-09) confirmed: `VibeCodedOrchestrator_CodeFunction` had 5365
# rows of which **0 were Python**, while the legacy snapshot
# `Vco_v0243_A_install_CodeFunction` still had 219 Python rows (untouched
# by incremental runs). This was the PRIMARY root cause of zero-Python-
# indexed.
#
# Fix scope: drop --prune-stale + --language; let stale rows leak until a
# full reanalyze (V52-O.2 `scripts/v0252_codegraph_reset.sh`) cleans them
# up. The proper architectural fix (scope prune to the EDITED FILE only,
# not language-wide) is queued for v0.2.53 — see V52-O.7 sub-item in
# v0.2.52 backlog.
(
    cd "$REPO_PATH"
    "$PYTHON" "$ANALYZER" \
        "$REPO_PATH" \
        --project "$PROJECT_NAME" \
        --incremental \
        2>&1 | tail -5
) &

echo "📊 Code graph incremental update queued for $PROJECT_NAME"
