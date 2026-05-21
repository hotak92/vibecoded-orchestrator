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
# Dual-layout venv resolution (PR-25 / v0.2.12). Modern installs put the
# venv at <repo_root>/.venv (top-level); pre-v0.2.x installs had it at
# <repo_root>/claude_mcp_servers/.venv. Hardcoding the latter caused this
# hook to silently fall through to system python on modern installs —
# where weaviate-client isn't installed — and crash inside the analyzer.
# VCT_VENV overrides everything when set explicitly.
if [ -n "${VCT_VENV:-}" ]; then
    VENV="$VCT_VENV"
elif [ -d "$DEFAULT_REPO_ROOT/.venv" ]; then
    VENV="$DEFAULT_REPO_ROOT/.venv"
elif [ -d "$DEFAULT_REPO_ROOT/claude_mcp_servers/.venv" ]; then
    VENV="$DEFAULT_REPO_ROOT/claude_mcp_servers/.venv"
else
    # Final fallback: empty → caller falls back to system PATH lookup
    # via _lib/find-python.sh. Hook may degrade gracefully (no
    # weaviate-client) but won't hard-fail.
    VENV=""
fi

# Auto-detect project from file path (multi-codebase support) — silent if helper absent
DETECT_HELPER="$DEFAULT_REPO_ROOT/.claude/scripts/detect-project.sh"
if [ -f "$DETECT_HELPER" ]; then
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

# v0.2.18 (Plan C): map the edited file's extension to the analyzer's
# canonical language ID. Same set as analyze_code_graph.py's argparse
# `--language` choices. When the extension matches, we pass --language
# AND --prune-stale to the analyzer so the language-scoped prune runs
# (only deletes rows tagged with this language that were not visited).
# Pre-Plan-C the hook ran with neither flag, leaving deleted source-
# files' code-graph entries behind forever.
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
# v0.2.18 (Plan C): --language=$LANG + --prune-stale together make the
# language-scoped prune correct + cheap — only entries tagged with this
# language that the analyzer didn't visit this run are deleted; other
# languages are preserved. Empty LANG (unrecognised extension) falls back
# to incremental-without-prune (legacy behaviour).
(
    cd "$REPO_PATH"
    if [ -n "$LANG" ]; then
        "$PYTHON" "$ANALYZER" \
            "$REPO_PATH" \
            --project "$PROJECT_NAME" \
            --incremental \
            --language "$LANG" \
            --prune-stale \
            2>&1 | tail -5
    else
        "$PYTHON" "$ANALYZER" \
            "$REPO_PATH" \
            --project "$PROJECT_NAME" \
            --incremental \
            2>&1 | tail -5
    fi
) &

echo "📊 Code graph incremental update queued for $PROJECT_NAME"
