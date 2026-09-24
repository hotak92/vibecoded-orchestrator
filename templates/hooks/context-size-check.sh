#!/usr/bin/env bash
# Context Size Check Hook Template
#
# Purpose: Monitor CONTEXT_STATE.md size and trigger doc-maintainer agent when threshold exceeded
# Hook Event: SessionStart (checks once per session)
# Location: Copy to project's .claude/hooks/ and enable in settings.json
#
# Configuration (v0.2.97 — the bundled session-state module's settings,
# `launcher/bundled_manifests/vct-session-state.json`):
# - CONTEXT_STATE_MAX_LINES → MAX_LINES: alert threshold (default 500 —
#   the documented CONTEXT_STATE.md "max 500"; the 250–350 working range
#   is normal, so warning earlier just nags). WARN_LINES = 60% of it.
# - MEMORY_MAX_LINES: notice when Claude Code's auto-memory MEMORY.md for
#   this project reaches it (default 200 — Claude Code loads only its
#   first 200 lines).
# Each resolves: the environment variable → the vct-hub /env (where the
# launcher's module settings land) through the shipped resolver
# `.claude/scripts/vct_secrets_resolve.sh` (hub → file store → the
# project's .env) → the default. A value outside 50..2000 or not a number
# falls back to the default.
#
# Cost (v0.2.97, review R6 F51): ONE resolver process for whichever of the
# two settings the environment does not set (`resolve-many`), with its hub
# time bounded by VCT_RESOLVE_MAX_TIME=1 — a hub port that accepts and then
# hangs costs this session start at most ~1 s (it was up to 2 × 5 s), after
# which the file store / .env / defaults answer, silently.

set -euo pipefail

# Scrub sensitive env vars (this hook doesn't need credentials)
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# --- Configuration ---
. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# Resolve Python portably for the session_id stdin parse below (python3 →
# python → py). Same fallback chain as diff-context-inject.sh. Sourced under
# `set -e`, so guard with `|| true` — a missing helper must not abort the hook.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh" 2>/dev/null || true
# Shared session_id parse + path-safety sanitise (vco_hook_session_id). One
# implementation for all four context hooks; see _lib/session-id.sh. Same
# `|| true` guard so a missing helper under `set -e` can't abort the hook.
# shellcheck source=_lib/session-id.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/session-id.sh" 2>/dev/null || true

CONTEXT_FILE=".claude/CONTEXT_STATE.md"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

# The settings the environment does not set, resolved in ONE resolver
# spawn (see "Cost" above). Output: one KEY=VALUE line per resolved key.
SETTING_KEYS="CONTEXT_STATE_MAX_LINES MEMORY_MAX_LINES"
RESOLVED_SETTINGS=""
unset_settings=""
for setting_key in $SETTING_KEYS; do
    [ -z "${!setting_key:-}" ] && unset_settings="$unset_settings $setting_key"
done
if [ -n "$unset_settings" ]; then
    resolver="$(dirname "${BASH_SOURCE[0]}")/../scripts/vct_secrets_resolve.sh"
    if [ -f "$resolver" ]; then
        # shellcheck disable=SC2086  # the key list splits on purpose
        RESOLVED_SETTINGS=$(VCT_RESOLVE_MAX_TIME=1 bash "$resolver" resolve-many "$PROJECT_DIR" $unset_settings 2>/dev/null || true)
    fi
fi

# resolve_threshold KEY DEFAULT — see "Configuration" above. MUST MATCH
# Resolve-Threshold in context-size-check.ps1.
resolve_threshold() {
    local key="$1" default="$2" value="" line
    value="${!key:-}"
    if [ -z "$value" ]; then
        while IFS= read -r line; do
            case "$line" in
                "$key="*) value="${line#*=}"; break ;;
            esac
        done <<< "$RESOLVED_SETTINGS"
    fi
    case "$value" in
        ''|*[!0-9]*) value="$default" ;;
    esac
    if [ "$value" -lt 50 ] || [ "$value" -gt 2000 ]; then
        value="$default"
    fi
    printf '%s' "$value"
}

MAX_LINES=$(resolve_threshold CONTEXT_STATE_MAX_LINES 500)
WARN_LINES=$(( MAX_LINES * 3 / 5 ))
MEMORY_MAX_LINES=$(resolve_threshold MEMORY_MAX_LINES 200)

# Track C (v0.2.65): the shared vco_hook_session_id parses session_id from the
# SessionStart stdin payload so we can also size-check this session's own
# CONTEXT_STATE file. Empty when the payload is absent/malformed → the
# per-session block below is skipped (gated on `[ -n "$SESSION_ID" ]`).
# Defense-in-depth (review C-1): the helper sanitises the id to [A-Za-z0-9_-]
# before it reaches the file path below (hostile `/`/`..` → "default"). Must
# match the .ps1 sibling's Get-VcoHookSessionId. Under `set -e` the helper
# (and any `command -v` it omits) is fully soft-failing, so the substitution
# can't abort; the `|| true` on the parse keeps that guarantee explicit.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
SESSION_ID=$(vco_hook_session_id "$HOOK_STDIN" 2>/dev/null || echo "")

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
   3. Keep only current work (the 250–350 line working range)
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

# 3. Claude Code's auto-memory MEMORY.md for this project (v0.2.97, the
# MEMORY_MAX_LINES setting). Claude Code keeps it under
# <claude home>/projects/<project path, every non-alphanumeric char → '-'>/memory/.
CLAUDE_HOME_DIR="${VCT_CLAUDE_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}"
MEMORY_SLUG=$(printf '%s' "$PROJECT_DIR" | sed 's/[^A-Za-z0-9]/-/g')
MEMORY_FILE="$CLAUDE_HOME_DIR/projects/$MEMORY_SLUG/memory/MEMORY.md"
if [ -f "$MEMORY_FILE" ]; then
    memory_lines=$(get_line_count "$MEMORY_FILE")
    if [ "$memory_lines" -ge "$MEMORY_MAX_LINES" ]; then
        cat <<EOF

ℹ️  MEMORY.md Size Notice
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Current size: $memory_lines lines (threshold: $MEMORY_MAX_LINES lines)

Claude Code loads only the first 200 lines of MEMORY.md into each session.
Keep it a one-line-per-entry index and move detail into topic files.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF
    fi
fi

# Exit 0 (don't block session start)
exit 0
