#!/usr/bin/env bash
# OS-EXEMPT-PARITY: bash-side-only fix — switched bare `python3` to `"$PY"` via _lib/find-python.sh. The .ps1 sibling parses the audit-event JSON via ConvertFrom-Json (no Python invocation) and is already cross-OS-correct.
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# config-change-audit.sh
# Fires on ConfigChange event — logs all settings changes for audit trail.
# Background: does NOT write to stdout (not injected into context).

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# Resolve Python portably — bare `python3` is missing on Windows.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
LOG_FILE="$PROJECT_DIR/.claude/logs/config_changes.jsonl"

mkdir -p "$(dirname "$LOG_FILE")"

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# $CLAUDE_TOOL_NAME / $CLAUDE_TOOL_ARGS env vars are EMPTY — verified
# empirically 2026-05-08 via stdin-capture diagnostic. Without this,
# every config-change audit entry was {"tool":"unknown","args":{}}.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
if [ -n "${PY:-}" ]; then
    TOOL_NAME=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('tool_name', '') or 'unknown')
except Exception:
    print('unknown')
" 2>/dev/null || echo "unknown")
    TOOL_ARGS=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(json.dumps(d.get('tool_input', {})))
except Exception:
    print('{}')
" 2>/dev/null || echo "{}")
else
    TOOL_NAME="unknown"
    TOOL_ARGS="{}"
fi

echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"config_change\",\"tool\":\"${TOOL_NAME}\",\"args\":${TOOL_ARGS}}" >> "$LOG_FILE" 2>/dev/null || true
