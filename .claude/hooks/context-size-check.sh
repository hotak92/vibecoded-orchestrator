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

MAX_LINES=400
WARN_LINES=300
CONTEXT_FILE=".claude/CONTEXT_STATE.md"

# --- Functions ---
get_line_count() {
    if [ -f "$CONTEXT_FILE" ]; then
        wc -l < "$CONTEXT_FILE"
    else
        echo "0"
    fi
}

# --- Main Logic ---
LINE_COUNT=$(get_line_count)

if [ "$LINE_COUNT" -ge "$MAX_LINES" ]; then
    cat <<EOF

⚠️  CONTEXT_STATE.md Size Alert (CRITICAL)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Current size: $LINE_COUNT lines (threshold: $MAX_LINES lines)

CONTEXT_STATE.md has exceeded the recommended size. This can cause:
- Context bloat (losing track of current work)
- Catastrophic forgetting (old decisions not extracted)
- Reduced session efficiency

🔧 Recommended Action:
   Spawn doc-maintainer agent to refresh CONTEXT_STATE.md:

   "Please spawn the doc-maintainer agent to refresh CONTEXT_STATE.md"

   The agent will:
   1. Extract completed work to canonical docs (ARCHITECTURE.md, DECISIONS_LOG.md, etc.)
   2. Move historical context to knowledge graph nodes
   3. Keep only current work (<200 lines)
   4. Preserve all knowledge (no catastrophic forgetting)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF

elif [ "$LINE_COUNT" -ge "$WARN_LINES" ]; then
    cat <<EOF

ℹ️  CONTEXT_STATE.md Size Notice
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Current size: $LINE_COUNT lines (warning threshold: $WARN_LINES lines)

CONTEXT_STATE.md is approaching the recommended size limit of $MAX_LINES lines.

Consider refreshing soon with the doc-maintainer agent to:
- Extract completed work to canonical docs
- Keep CONTEXT_STATE.md focused on current work
- Prevent context bloat

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF

fi

# Exit 0 (don't block session start)
exit 0
