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

# Resolve the file path depending on which tool triggered us
FILE_PATH=""

# Read hook input from stdin
INPUT=$(cat)

if [ -n "$CLAUDE_TOOL_ARG_FILE_PATH" ]; then
    # Edit/Write tool — file path passed directly
    FILE_PATH="$CLAUDE_TOOL_ARG_FILE_PATH"
elif echo "$INPUT" | grep -q "store_knowledge_node"; then
    # MCP store_knowledge_node — extract file_path from tool response
    FILE_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    # Try tool_response first (contains absolute_path)
    resp = d.get('tool_response', '')
    if isinstance(resp, str):
        import re
        m = re.search(r'absolute_path[\":\s]+([^\",}]+)', resp)
        if m: print(m.group(1).strip()); sys.exit(0)
    # Try tool_input
    inp = d.get('tool_input', {})
    if isinstance(inp, str): inp = json.loads(inp)
    fp = inp.get('file_path', '')
    if fp: print(fp)
except Exception:
    pass
" 2>/dev/null)
fi

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
