#!/usr/bin/env bash
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

# Don't run for agent subprocesses
. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

[ -n "$CLAUDE_CODE_DISABLE_AUTO_MEMORY" ] && exit 0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
VENV="${VCT_INSTALL_ROOT:-$PROJECT_ROOT}/claude_mcp_servers/.venv/bin/python"
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
FILE_PATH=$(printf '%s' "$INPUT" | python3 -c "
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
FILE_HASH=$(echo "$FILE_PATH" | md5sum | cut -d' ' -f1)
STAMP="$DEBOUNCE_DIR/$FILE_HASH"

if [ -f "$STAMP" ]; then
    AGE=$(( $(date +%s) - $(stat -c %Y "$STAMP" 2>/dev/null || echo 0) ))
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
