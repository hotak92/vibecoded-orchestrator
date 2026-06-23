#!/usr/bin/env bash
# Context Size Check Hook Template
#
# Purpose: Monitor CONTEXT_STATE.md size and trigger doc-maintainer agent when threshold exceeded
# Hook Event: SessionStart (checks once per session)
# Location: Copy to project's .claude/hooks/ and enable in settings.json
#
# Configuration:
# - MAX_LINES: Trigger threshold (default: 200 lines)
# - WARN_LINES: Warning threshold (default: 150 lines)

set -euo pipefail

# Scrub sensitive env vars (this hook doesn't need credentials)
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# --- Configuration ---
. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# Resolve Python portably for the session_id stdin parse below (python3 →
# python → py). Same fallback chain as diff-context-inject.sh. Sourced under
# `set -e`, so guard with `|| true` — a missing helper must not abort the hook.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh" 2>/dev/null || true

MAX_LINES=400
WARN_LINES=300
CONTEXT_FILE=".claude/CONTEXT_STATE.md"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

# Track C (v0.2.65): parse session_id from the SessionStart stdin payload so
# we can also size-check this session's own CONTEXT_STATE file. Same parse as
# diff-context-inject.sh / post-compact.sh. Under `set -e` we MUST guard every
# substitution with `|| true` / `|| echo` so a malformed payload can't abort.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
SESSION_ID=""
if [ -n "${PY:-}" ]; then
    SESSION_ID=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('session_id', '') or '')
except Exception:
    print('')
" 2>/dev/null || echo "")
fi

# --- Functions ---
get_line_count() {
    local f="$1"
    if [ -f "$f" ]; then
        wc -l < "$f"
    else
        echo "0"
    fi
}

# check_size_thresholds: emit a size alert/notice for $1 (file path) against
# the shared MAX_LINES/WARN_LINES thresholds. $2 is the display label used in
# the message. Reused for both the shared CONTEXT_STATE.md and the Track C
# per-session file — one threshold implementation, two callers.
check_size_thresholds() {
    local file="$1"
    local label="$2"
    local line_count
    line_count=$(get_line_count "$file")

    if [ "$line_count" -ge "$MAX_LINES" ]; then
        cat <<EOF

⚠️  ${label} Size Alert (CRITICAL)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Current size: $line_count lines (threshold: $MAX_LINES lines)

${label} has exceeded the recommended size. This can cause:
- Context bloat (losing track of current work)
- Catastrophic forgetting (old decisions not extracted)
- Reduced session efficiency

🔧 Recommended Action:
   Spawn doc-maintainer agent to refresh ${label}:

   "Please spawn the doc-maintainer agent to refresh ${label}"

   The agent will:
   1. Extract completed work to canonical docs (ARCHITECTURE.md, DECISIONS_LOG.md, etc.)
   2. Move historical context to knowledge graph nodes
   3. Keep only current work (<200 lines)
   4. Preserve all knowledge (no catastrophic forgetting)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF

    elif [ "$line_count" -ge "$WARN_LINES" ]; then
        cat <<EOF

ℹ️  ${label} Size Notice
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Current size: $line_count lines (warning threshold: $WARN_LINES lines)

${label} is approaching the recommended size limit of $MAX_LINES lines.

Consider refreshing soon with the doc-maintainer agent to:
- Extract completed work to canonical docs
- Keep ${label} focused on current work
- Prevent context bloat

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF

    fi
}

# --- Main Logic ---
# 1. The shared CONTEXT_STATE.md rollup (original behaviour).
check_size_thresholds "$CONTEXT_FILE" "CONTEXT_STATE.md"

# 2. Track C: this session's own CONTEXT_STATE file, IF it exists. Gated on a
# resolved session_id AND file existence — single-session projects pay nothing.
if [ -n "$SESSION_ID" ]; then
    SESSION_CONTEXT_FILE="$PROJECT_DIR/.claude/context/CONTEXT_STATE_${SESSION_ID}.md"
    if [ -f "$SESSION_CONTEXT_FILE" ]; then
        check_size_thresholds "$SESSION_CONTEXT_FILE" "CONTEXT_STATE_${SESSION_ID}.md"
    fi
fi

# Exit 0 (don't block session start)
exit 0
