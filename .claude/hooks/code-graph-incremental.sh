#!/bin/bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
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

set -e

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

EDITED_FILE="$1"
REPO_PATH="${2:-${CLAUDE_PROJECT_DIR:-$(pwd)}}"
PROJECT_NAME="${3:-$(basename "$REPO_PATH")}"

# Resolve analyzer script + python from project layout (no hardcoded paths).
# Hook lives at <repo>/.claude/hooks/, so repo root is two parents up.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ANALYZER="${VCT_ANALYZER_SCRIPT:-$DEFAULT_REPO_ROOT/.claude/scripts/analyze_code_graph.py}"
VENV="${VCT_VENV:-$DEFAULT_REPO_ROOT/claude_mcp_servers/.venv}"

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

# Resolve python: prefer venv, fall back to system python3
PYTHON="${VCT_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x "$VENV/bin/python" ]; then
        PYTHON="$VENV/bin/python"
    else
        PYTHON="$(command -v python3 || true)"
    fi
fi

if [ -z "$PYTHON" ] || [ ! -f "$ANALYZER" ]; then
    # Silent fallback — orchestrator works fine without code graph
    exit 0
fi

# Run incremental analysis in background (no debounce — keep code graph fresh).
# --cfg/--pdg default to ON inside analyze_code_graph.py when joern is present;
# silent fallback when absent. To disable, set VCT_JOERN_AVAILABLE=0.
(
    cd "$REPO_PATH"
    "$PYTHON" "$ANALYZER" \
        "$REPO_PATH" \
        --project "$PROJECT_NAME" \
        --incremental \
        2>&1 | tail -5
) &

echo "📊 Code graph incremental update queued for $PROJECT_NAME"
