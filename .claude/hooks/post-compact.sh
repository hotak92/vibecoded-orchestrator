#!/bin/bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
# post-compact.sh
# Fires on PostCompact event — after context compaction completes (manual or auto).
# Paired with pre-compact-save.sh (PreCompact) and compact-context-reinject.sh (SessionStart/compact).
#
# NOTE: PostCompact hooks fire BEFORE the next SessionStart/compact event.
# The reinject hook already handles re-injecting CONTEXT_STATE.md + snapshot.
# This hook handles side effects: KG sync, notification, logging.
#
# Payload available via stdin:
#   {"trigger": "manual|auto", "compact_summary": "...", "session_id": "..."}

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
PROJECT_NAME=$(basename "$PROJECT_DIR")

PAYLOAD=$(cat)
TRIGGER=$(echo "$PAYLOAD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('trigger','unknown'))" 2>/dev/null || echo "unknown")

# Log the compaction event
LOG_DIR="$HOME/.claude/metrics"
mkdir -p "$LOG_DIR"
echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"project\":\"$PROJECT_NAME\",\"trigger\":\"$TRIGGER\"}" >> "$LOG_DIR/compactions.jsonl"

# Desktop notification
notify-send \
    --icon=dialog-information \
    --expire-time=5000 \
    --urgency=low \
    "Context compacted — $PROJECT_NAME" \
    "Trigger: $TRIGGER. Context re-injected." 2>/dev/null || true
