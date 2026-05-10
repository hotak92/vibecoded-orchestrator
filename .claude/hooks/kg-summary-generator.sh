#!/usr/bin/env bash
# OS-EXEMPT-PARITY: bash-side-only fix — three changes brought bash up to PowerShell-native equivalents that the .ps1 sibling already had: (1) `_lib/find-python.sh` sourced + bare `python3` → `"$PY"` (Windows ships python.exe/py); (2) `md5sum` → Python hashlib (md5sum is GNU-only, absent on macOS / minimal Linux); (3) `stat -c %Y` → Python os.path.getmtime (GNU-only; macOS BSD stat needs `-f %m`). The .ps1 sibling already uses .NET MD5, .NET LastWriteTime, and Get-Command python — already cross-OS-correct.
# kg-summary-generator.sh — PostToolUse hook
# Spawns a background Haiku agent to generate/update summaries for KG nodes.
# Fires on: Edit(knowledge/**/*.md), Write(knowledge/**/*.md), mcp__weaviate-kg__store_knowledge_node
#
# Summaries stored in knowledge/.node_formats.json, consumed by:
#   - hybrid_search (descriptions detail level)
#
# Content-hash dedup: skips regeneration if node content unchanged.

# Scrub sensitive env vars
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# VCO-CENTRALIZED-KG: write-side delegator (PR #171 / 0.1.7).
#   Calls .claude/scripts/generate-kg-summary.py to refresh
#   knowledge/.node_formats.json — operates on the project's OWN KG only
#   (per-node summary cache, not a Weaviate collection write either).
#   No multi-source fan-out involvement. VCT_KG_ACCESS_LIST is read-side
#   only; no centralization needed here.

# Don't run for agent subprocesses
. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

[ -n "$CLAUDE_CODE_DISABLE_AUTO_MEMORY" ] && exit 0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Resolve a Python interpreter portably (python3 → python → py).
# Bare `python3` doesn't exist on Windows (only `python.exe`/`py`); on
# minimal Linux distros only `python` is on PATH. find-python.sh sets
# $PY to the first working candidate.
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # No Python available — silent no-op

# Cross-OS venv path: POSIX uses .venv/bin/python; Windows venvs use
# .venv/Scripts/python.exe. Try both.
INSTALL_ROOT="${VCT_INSTALL_ROOT:-$PROJECT_ROOT}"
VENV="$INSTALL_ROOT/claude_mcp_servers/.venv/bin/python"
[ -x "$VENV" ] || VENV="$INSTALL_ROOT/claude_mcp_servers/.venv/Scripts/python.exe"
GENERATOR="$PROJECT_ROOT/.claude/scripts/generate-kg-summary.py"

# Silent fallback: if venv or generator script not present, exit clean.
[ -x "$VENV" ] || exit 0
[ -f "$GENERATOR" ] || exit 0

# Resolve the file path depending on which tool triggered us.
#
# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# The legacy $CLAUDE_TOOL_ARG_FILE_PATH env-var fallback was removed
# 2026-05-08 — that env var is NOT populated by Claude Code, so it
# always evaluated to empty and the fallback path silently skipped to
# the MCP branch. Verified via stdin-capture diagnostic.
FILE_PATH=""

# Read hook input from stdin (single read; reused by both branches)
INPUT=$(cat)

# Single Python extractor handles both Edit/Write (tool_input.file_path)
# and MCP store_knowledge_node (tool_response.absolute_path or
# tool_input.file_path). Branches by tool_name internally.
FILE_PATH=$(printf '%s' "$INPUT" | "$PY" -c "
import sys, json, re
try:
    d = json.loads(sys.stdin.read())
    tool_name = d.get('tool_name', '') or ''
    inp = d.get('tool_input', {}) or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except Exception:
            inp = {}
    # Edit/Write path — file_path lives under tool_input
    if tool_name in ('Edit', 'Write'):
        print(inp.get('file_path', '') or '')
        sys.exit(0)
    # MCP store_knowledge_node — try tool_response.absolute_path first,
    # then fall back to tool_input.file_path
    if tool_name == 'mcp__weaviate-kg__store_knowledge_node':
        resp = d.get('tool_response', '')
        if isinstance(resp, dict):
            ap = resp.get('absolute_path', '')
            if ap:
                print(ap)
                sys.exit(0)
        elif isinstance(resp, str) and resp:
            m = re.search(r'absolute_path[\":\\s]+([^\",}]+)', resp)
            if m:
                print(m.group(1).strip())
                sys.exit(0)
        print(inp.get('file_path', '') or '')
        sys.exit(0)
    # Unknown tool — fall through to nothing
    print('')
except Exception:
    print('')
" 2>/dev/null || echo "")

# Validate it's a knowledge file
if [[ -z "$FILE_PATH" ]] || [[ "$FILE_PATH" != *"knowledge/"* ]] || [[ "$FILE_PATH" != *.md ]]; then
    exit 0
fi

# Resolve to absolute path
if [[ "$FILE_PATH" != /* ]]; then
    FILE_PATH="$PROJECT_ROOT/$FILE_PATH"
fi

# Verify file exists
[ -f "$FILE_PATH" ] || exit 0

# Debounce: skip if we generated for this file in the last 60 seconds
DEBOUNCE_DIR="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/.kg-summary-debounce"
mkdir -p "$DEBOUNCE_DIR"
# Portable hash: `md5sum` is GNU-only (macOS uses `md5 -q`). Python's
# hashlib works on every OS we support.
FILE_HASH=$(printf '%s' "$FILE_PATH" | "$PY" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest())" 2>/dev/null)
[ -z "$FILE_HASH" ] && FILE_HASH=$(printf '%s' "$FILE_PATH" | tr '/' '_' | tr ' ' '_' | head -c 100)
STAMP="$DEBOUNCE_DIR/$FILE_HASH"

if [ -f "$STAMP" ]; then
    # Portable mtime: `stat -c %Y` is GNU; macOS BSD stat needs `-f %m`.
    # Python avoids the divergence.
    STAMP_MTIME=$("$PY" -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$STAMP" 2>/dev/null || echo 0)
    AGE=$(( $(date +%s) - STAMP_MTIME ))
    if [ "$AGE" -lt 60 ]; then
        exit 0
    fi
fi
touch "$STAMP"

# Make sure log dir exists
mkdir -p "$PROJECT_ROOT/.claude/logs"

# Run the generator in background (non-blocking)
nohup "$VENV" "$GENERATOR" "$FILE_PATH" \
    >> "$PROJECT_ROOT/.claude/logs/kg-summary-generator.log" 2>&1 &

echo "KG summary generation queued for $(basename "$FILE_PATH")"
exit 0
