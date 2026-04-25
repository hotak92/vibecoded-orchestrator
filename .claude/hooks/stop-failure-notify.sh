#!/bin/bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
# stop-failure-notify.sh
# Fires on StopFailure event — when a turn ends due to API error (rate limit, auth failure, etc).
# Sends urgent desktop notification and logs the failure.
#
# Payload available via stdin:
#   {"session_id":"...", "error": {"type":"...", "message":"..."}, ...}

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
PROJECT_NAME=$(basename "$PROJECT_DIR")

PAYLOAD=$(cat)

# Extract error info
ERROR_TYPE=$(echo "$PAYLOAD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error',{}).get('type','unknown'))" 2>/dev/null || echo "unknown")
ERROR_MSG=$(echo "$PAYLOAD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error',{}).get('message','No details')[:120])" 2>/dev/null || echo "No details")
SESSION_ID=$(echo "$PAYLOAD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id','')[:8])" 2>/dev/null || echo "")

# Log the failure
LOG_DIR="$HOME/.claude/metrics"
mkdir -p "$LOG_DIR"
echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"project\":\"$PROJECT_NAME\",\"session_id\":\"$SESSION_ID\",\"error_type\":\"$ERROR_TYPE\",\"error_message\":\"$ERROR_MSG\"}" >> "$LOG_DIR/failures.jsonl"

# Urgent desktop notification
notify-send \
    --icon=dialog-error \
    --expire-time=15000 \
    --urgency=critical \
    "Claude API Error — $PROJECT_NAME" \
    "$ERROR_TYPE: $ERROR_MSG" 2>/dev/null || true
