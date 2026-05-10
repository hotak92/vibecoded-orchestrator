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
# Audit row keeps the full payload under `payload` so future analysis
# can pick up event-specific fields (e.g. ConfigChange's `setting`,
# `old_value`, `new_value`) without re-modifying this hook.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
if [ -n "${PY:-}" ]; then
    HOOK_STDIN="$HOOK_STDIN" LOG_FILE="$LOG_FILE" "$PY" - <<'PYEOF' 2>/dev/null || true
import json, os, sys
from datetime import datetime, timezone

raw = os.environ.get("HOOK_STDIN", "")
log_file = os.environ.get("LOG_FILE", "")

try:
    payload = json.loads(raw) if raw else {}
except (json.JSONDecodeError, ValueError):
    payload = {"_parse_error": "stdin was not valid JSON", "_raw_preview": raw[:200]}

record = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "event": "config_change",
    "session_id": payload.get("session_id", ""),
    "hook_event_name": payload.get("hook_event_name", ""),
    "cwd": payload.get("cwd", ""),
    "payload": payload,
}

if log_file:
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError:
        sys.exit(0)
PYEOF
fi
