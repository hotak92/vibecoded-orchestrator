#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
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

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
PROJECT_NAME=$(basename "$PROJECT_DIR")

# Resolve a Python interpreter portably (python3 → python → py).
# See audit finding F6, 2026-04-30. _lib/find-python.sh sets $PY.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"

PAYLOAD=$(cat)
TRIGGER="unknown"
SESSION_ID=""
if [ -n "${PY:-}" ]; then
    _PARSED=$(echo "$PAYLOAD" | "$PY" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('trigger', 'unknown'))
    print(d.get('session_id', ''))
except Exception:
    print('unknown')
    print('')
" 2>/dev/null || printf 'unknown\n\n')
    TRIGGER=$(printf '%s' "$_PARSED" | sed -n '1p')
    SESSION_ID=$(printf '%s' "$_PARSED" | sed -n '2p')
fi

# Wipe the KG/codegraph injection dedup state for this session — the LLM
# just lost the context that included those previously-injected nodes, so
# re-injecting them on subsequent edits is now correct (and helpful).
# pre-edit-context-inject.sh writes to .claude/state/seen_kg_titles_<id>.txt.
if [ -n "$SESSION_ID" ]; then
    SEEN_FILE="$PROJECT_DIR/.claude/state/seen_kg_titles_${SESSION_ID}.txt"
    [ -f "$SEEN_FILE" ] && rm -f "$SEEN_FILE"
fi

# Log the compaction event
LOG_DIR="$HOME/.claude/metrics"
mkdir -p "$LOG_DIR"
echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"project\":\"$PROJECT_NAME\",\"trigger\":\"$TRIGGER\"}" >> "$LOG_DIR/compactions.jsonl"

# Cross-platform desktop notification (Linux/macOS/Windows). See audit F2.
if [ -n "${PY:-}" ] && [ -f "$PROJECT_DIR/.claude/scripts/notify.py" ]; then
    "$PY" "$PROJECT_DIR/.claude/scripts/notify.py" \
        "Context compacted — $PROJECT_NAME" "Trigger: $TRIGGER. Context re-injected." \
        --urgency low --icon dialog-information --expire-time 5000 \
        2>/dev/null || true
fi
