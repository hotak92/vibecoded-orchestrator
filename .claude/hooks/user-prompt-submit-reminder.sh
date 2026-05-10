#!/usr/bin/env bash
# OS-EXEMPT-PARITY: bash-side-only fix — switched bare `python3` to `"$PY"` via _lib/find-python.sh. The .ps1 sibling parses session_id from the stdin JSON via ConvertFrom-Json (no Python invocation) and is already cross-OS-correct.
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# Context preservation reminder - triggers on output volume (words as proxy for tokens)
set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# Resolve Python portably — bare `python3` is missing on Windows.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"

# Hook input contract (v2.1.x): JSON on stdin, NOT $CLAUDE_TOOL_NAME or
# $CLAUDE_SESSION_ID env vars. Reading those env vars under `set -u` aborts
# the hook with "unbound variable" before the staleness check ever runs —
# that was a long-standing latent bug (confirmed empirically 2026-05-08).
# We drain stdin once, parse session_id from the JSON payload, and use
# `${VAR:-}` defaults so the script can run on any event without aborting.
# session_id is the canonical per-conversation key; falling back to
# "default" only on malformed JSON keeps concurrent sessions from sharing
# state files (PR #176 cross-OS sweep — see knowledge/concepts/
# hook-session-id-stdin-pattern.md).
HOOK_STDIN=$(cat 2>/dev/null || echo "")
if [ -n "${PY:-}" ]; then
    SESSION_ID=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('session_id', '') or 'default')
except Exception:
    print('default')
" 2>/dev/null || echo "default")
else
    SESSION_ID="default"
fi
[ -z "$SESSION_ID" ] && SESSION_ID="default"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
PROJECT_NAME=$(basename "$PROJECT_DIR")
CONTEXT_FILE="$PROJECT_DIR/.claude/CONTEXT_STATE.md"
SESSION_LOG="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/claude-session-${PROJECT_NAME}-${SESSION_ID}"

# Initialize session tracking
if [ ! -f "$SESSION_LOG" ]; then
    echo "0" > "$SESSION_LOG"  # Total words this session
fi

# Estimate output volume. UserPromptSubmit hooks don't see tool_name, so per-
# tool accounting is moot under v2.1.x. Kept as a defensive default in case
# a future runtime ever populates the env var; today it always evaluates 0.
WORDS_THIS_TURN=0
if [ -n "${CLAUDE_TOOL_NAME:-}" ]; then
    case "$CLAUDE_TOOL_NAME" in
        Read|Write|Edit) WORDS_THIS_TURN=1000 ;;
        Bash) WORDS_THIS_TURN=500 ;;
        Task) WORDS_THIS_TURN=2000 ;;
        *) WORDS_THIS_TURN=200 ;;
    esac
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
    MARKER="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/claude-ctx-warn-${PROJECT_NAME}-${SESSION_ID}"
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

# CONTEXT_STATE staleness — counter-based, one-shot per ~120K-word window
# since the LAST nudge or last actual edit (whichever is more recent).
#
# Replaces the prior time-based 30-min staleness check that fired 4-5x
# during long deep-work sessions. The new condition is "enough work has
# accumulated that a CONTEXT_STATE refresh is genuinely useful," not
# "the file is old."
#
# The staleness marker file is keyed by both PROJECT_NAME and SESSION_ID
# so concurrent Claude Code sessions on the same project don't stomp on
# each other's counter (the same concurrency fix as PR #176 applied to
# 11 other hooks — see knowledge/concepts/hook-session-id-stdin-pattern.md).
STALENESS_MARKER="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/claude-ctx-staleness-${PROJECT_NAME}-${SESSION_ID}"
LAST_FIRE_WORDS=0
if [ -f "$STALENESS_MARKER" ]; then
    LAST_FIRE_WORDS=$(cat "$STALENESS_MARKER" 2>/dev/null || echo 0)
    if [ -f "$CONTEXT_FILE" ]; then
        CTX_MTIME=$(stat -c %Y "$CONTEXT_FILE" 2>/dev/null || stat -f %m "$CONTEXT_FILE" 2>/dev/null || echo 0)
        MARKER_MTIME=$(stat -c %Y "$STALENESS_MARKER" 2>/dev/null || stat -f %m "$STALENESS_MARKER" 2>/dev/null || echo 0)
        if [ "$CTX_MTIME" -gt "$MARKER_MTIME" ]; then
            echo "$TOTAL_WORDS" > "$STALENESS_MARKER"
            LAST_FIRE_WORDS="$TOTAL_WORDS"
        fi
    fi
fi
DELTA=$(( TOTAL_WORDS - LAST_FIRE_WORDS ))
if [ "$DELTA" -ge 120000 ]; then
    echo ""
    echo "💾 ~${DELTA} words of work since last CONTEXT_STATE update."
    echo "   Append a 1-2 line progress note now (what shipped + what's next),"
    echo "   before context grows further or /compact lands."
    echo ""
    echo "$TOTAL_WORDS" > "$STALENESS_MARKER"
fi

exit 0
