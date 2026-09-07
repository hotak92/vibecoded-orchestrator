#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# stop-failure-notify.sh
# Fires on StopFailure event — when a turn ends due to API error (rate limit, auth failure, etc).
# Sends urgent desktop notification and logs the failure.
#
# Payload available via stdin:
#   {"session_id":"...", "error": {"type":"...", "message":"..."}, ...}

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
PROJECT_NAME=$(basename "$PROJECT_DIR")

# Resolve a Python interpreter portably (python3 → python → py).
# See audit finding F6, 2026-04-30. _lib/find-python.sh sets $PY.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"

PAYLOAD=$(cat)

# Extract error info — only attempt if we have a Python interpreter.
ERROR_TYPE="unknown"
ERROR_MSG="No details"
SESSION_ID=""
if [ -n "${PY:-}" ]; then
    ERROR_TYPE=$(echo "$PAYLOAD" | "$PY" -c "import sys,json; d=json.load(sys.stdin); print(d.get('error',{}).get('type','unknown'))" 2>/dev/null || echo "unknown")
    ERROR_MSG=$(echo "$PAYLOAD" | "$PY" -c "import sys,json; d=json.load(sys.stdin); print(d.get('error',{}).get('message','No details')[:120])" 2>/dev/null || echo "No details")
    SESSION_ID=$(echo "$PAYLOAD" | "$PY" -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id','')[:8])" 2>/dev/null || echo "")
fi

# Log the failure. v0.2.92 W7: the metrics home moved out of ~/.claude;
# `_lib/metrics-dir.sh` is the ONE shell-side resolver (sibling:
# `_lib/metrics-dir.ps1`). A missing helper skips the log line rather than
# guessing a path — the urgent notification below still fires, which is the
# part the user actually depends on.
_SF_LIB="$(dirname "${BASH_SOURCE[0]}")/_lib/metrics-dir.sh"
if [ -f "$_SF_LIB" ]; then
    # shellcheck source=_lib/metrics-dir.sh disable=SC1091
    . "$_SF_LIB"
    LOG_DIR="$(vco_metrics_dir 2>/dev/null || printf '')"
    if [ -n "$LOG_DIR" ]; then
        echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"project\":\"$PROJECT_NAME\",\"session_id\":\"$SESSION_ID\",\"error_type\":\"$ERROR_TYPE\",\"error_message\":\"$ERROR_MSG\"}" >> "$LOG_DIR/failures.jsonl"
    fi
fi

# Cross-platform urgent desktop notification (Linux/macOS/Windows). See audit F2.
if [ -n "${PY:-}" ] && [ -f "$PROJECT_DIR/.claude/scripts/notify.py" ]; then
    "$PY" "$PROJECT_DIR/.claude/scripts/notify.py" \
        "Claude API Error — $PROJECT_NAME" "$ERROR_TYPE: $ERROR_MSG" \
        --urgency critical --icon dialog-error --expire-time 15000 \
        2>/dev/null || true
fi
