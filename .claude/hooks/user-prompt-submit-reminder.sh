#!/bin/bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
# Context preservation reminder - triggers on output volume (words as proxy for tokens)
set -e

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
PROJECT_NAME=$(basename "$PROJECT_DIR")
CONTEXT_FILE="$PROJECT_DIR/.claude/CONTEXT_STATE.md"
SESSION_LOG="/tmp/claude-session-$PROJECT_NAME"

# Initialize session tracking
if [ ! -f "$SESSION_LOG" ]; then
    echo "0" > "$SESSION_LOG"  # Total words this session
fi

# Estimate output volume based on tool usage
# Only count if substantial work happened (file operations, tool calls)
WORDS_THIS_TURN=0

# Check if any files were modified/created (PostToolUse context)
if [ -n "$CLAUDE_TOOL_NAME" ]; then
    case "$CLAUDE_TOOL_NAME" in
        Read|Write|Edit) WORDS_THIS_TURN=1000 ;;  # File operations = substantial
        Bash) WORDS_THIS_TURN=500 ;;              # Commands = moderate
        Task) WORDS_THIS_TURN=2000 ;;             # Agent spawns = heavy
        *) WORDS_THIS_TURN=200 ;;                 # Other tools = light
    esac
else
    # UserPromptSubmit without tool context - check if user message is long
    # Assume short exchanges don't need counting
    WORDS_THIS_TURN=0
fi

# Only increment if substantial work
if [ "$WORDS_THIS_TURN" -gt 0 ]; then
    TOTAL_WORDS=$(cat "$SESSION_LOG")
    TOTAL_WORDS=$((TOTAL_WORDS + WORDS_THIS_TURN))
    echo "$TOTAL_WORDS" > "$SESSION_LOG"
else
    TOTAL_WORDS=$(cat "$SESSION_LOG")
fi

# Trigger at 200K words (~300K tokens, about 30% of 1M context on Opus)
if [ "$TOTAL_WORDS" -ge 200000 ]; then
    MARKER="/tmp/claude-ctx-warn-$PROJECT_NAME"
    [ -f "$MARKER" ] && exit 0

    echo ""
    echo "💾 Session checkpoint (~300K tokens used)"
    echo "   • /refresh-context - Update KG if topic shifted"
    echo "   • Update CONTEXT_STATE.md with findings"
    echo "   • Consider new session before context compression"
    echo ""

    touch "$MARKER"
    exit 0
fi

# Also check CONTEXT_STATE.md staleness (every ~40K words)
if [ $((TOTAL_WORDS % 40000)) -lt 1500 ] && [ -f "$CONTEXT_FILE" ]; then
    MODIFIED=$(stat -c %Y "$CONTEXT_FILE" 2>/dev/null || stat -f %m "$CONTEXT_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    AGE_MIN=$(( (NOW - MODIFIED) / 60 ))

    if [ "$AGE_MIN" -gt 30 ]; then
        echo ""
        echo "💾 Update CONTEXT_STATE.md (last update: ${AGE_MIN}min ago)"
        echo ""
    fi
fi

exit 0
