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
# Shared session_id parse + path-safety sanitise (vco_hook_session_id). One
# implementation for all four context hooks; see _lib/session-id.sh.
# shellcheck source=_lib/session-id.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/session-id.sh" ] && . "$SCRIPT_DIR/_lib/session-id.sh"

PAYLOAD=$(cat)
# `trigger` is a label only (logged + shown in the notification) and never
# reaches a file path, so it keeps its own lightweight parse. session_id DOES
# reach file paths below (.claude/state/*_${SESSION_ID}), so it goes through
# the shared vco_hook_session_id, which parses AND sanitises it to
# [A-Za-z0-9_-] (review C-1 defense-in-depth — a hostile `/`/`..` collapses to
# the safe sentinel "default"). Must match the .ps1 sibling's
# Get-VcoHookSessionId. Empty when absent/malformed → the per-session wipes
# below are skipped (each gated on `[ -n "$SESSION_ID" ]`).
TRIGGER="unknown"
if [ -n "${PY:-}" ]; then
    TRIGGER=$(printf '%s' "$PAYLOAD" | "$PY" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('trigger', 'unknown') or 'unknown')
except Exception:
    print('unknown')
" 2>/dev/null || printf 'unknown')
fi
[ -z "$TRIGGER" ] && TRIGGER="unknown"
# Guard the call: if the helper failed to source (file absent), degrade to an
# empty session_id rather than emitting a "command not found" — empty just
# skips the per-session wipes below.
if command -v vco_hook_session_id >/dev/null 2>&1; then
    SESSION_ID=$(vco_hook_session_id "$PAYLOAD")
else
    SESSION_ID=""
fi

# Wipe the KG/codegraph injection dedup state for this session — the LLM
# just lost the context that included those previously-injected nodes, so
# re-injecting them on subsequent edits is now correct (and helpful).
# pre-edit-context-inject.sh writes to .claude/state/seen_kg_titles_<id>.txt.
if [ -n "$SESSION_ID" ]; then
    SEEN_FILE="$PROJECT_DIR/.claude/state/seen_kg_titles_${SESSION_ID}.txt"
    [ -f "$SEEN_FILE" ] && rm -f "$SEEN_FILE"
fi

# Same reasoning for pre-tool-use.sh's per-session reads file (Build Anchor
# Protocol dedup). After compaction the LLM has lost its memory of which
# files it Read pre-compaction, so the reads list is no longer meaningful
# — Build Anchor should re-require a fresh Read before the next Write/Edit.
# Note: we do NOT wipe $PROJECT_DIR/.claude/state/tool_backups/ — those
# have a different lifecycle (tool-call rollback, not dedup), and the
# pre-tool-use hook runs its own 24h GC there.
if [ -n "$SESSION_ID" ]; then
    READS_FILE="$PROJECT_DIR/.claude/state/reads_${SESSION_ID}.txt"
    [ -f "$READS_FILE" ] && rm -f "$READS_FILE" 2>/dev/null || true
fi

# v0.2.29: same reset for the agent-skill-keyword-suggest hook's
# per-session dedup file. Without this, a "you might want to use skill X"
# suggestion that was emitted before compaction would NEVER fire again
# in the session — but post-compaction the user is plausibly starting a
# fresh logical task and the suggestion may once again be relevant.
# Path: $PROJECT_DIR/.claude/state/keyword_suggest_<session_id>.txt.
# Matches what `agent-skill-keyword-match.py::_dedup_file` writes to
# (moved from $TMPDIR/claude_keyword_suggest/ to project-local state for
# the same resume-across-reboot reasoning as the ctx_snapshot block below).
if [ -n "$SESSION_ID" ]; then
    KW_SEEN="$PROJECT_DIR/.claude/state/keyword_suggest_${SESSION_ID}.txt"
    [ -f "$KW_SEEN" ] && rm -f "$KW_SEEN"
fi

# Wipe diff-context-inject's per-session snapshot + compact flag. The
# CONTEXT_STATE.md diff baseline should reset whenever the LLM's context
# resets — PostCompact is the canonical reset point. Without this, the
# first prompt after a /compact would emit a "changed sections" diff
# anchored to the pre-compact baseline, which is incoherent (the LLM no
# longer has the pre-compact view of CONTEXT_STATE.md). pre-compact-save.sh
# touches the compact flag as the cross-hook signal; this is the actual wipe.
if [ -n "$SESSION_ID" ]; then
    CTX_SNAPSHOT="$PROJECT_DIR/.claude/state/ctx_snapshot_${SESSION_ID}"
    CTX_COMPACT_FLAG="$PROJECT_DIR/.claude/state/ctx_compact_flag_${SESSION_ID}"
    [ -f "$CTX_SNAPSHOT" ] && rm -f "$CTX_SNAPSHOT"
    [ -f "$CTX_COMPACT_FLAG" ] && rm -f "$CTX_COMPACT_FLAG"
    # Track C (v0.2.65): same reset for the per-session CONTEXT_STATE diff
    # baseline. diff-context-inject.sh diffs .claude/context/CONTEXT_STATE_<id>.md
    # against this `ctx_snapshot_session_<id>` baseline; after compaction the
    # LLM lost the pre-compact view, so the baseline must reset too (else the
    # first post-compact prompt emits an incoherent diff). We reset ONLY the
    # throwaway snapshot baseline — never the per-session CONTEXT_STATE content
    # file (it may be user-curated). Sibling: post-compact.ps1.
    CTX_SNAPSHOT_SESSION="$PROJECT_DIR/.claude/state/ctx_snapshot_session_${SESSION_ID}"
    [ -f "$CTX_SNAPSHOT_SESSION" ] && rm -f "$CTX_SNAPSHOT_SESSION"
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
