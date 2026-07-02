#!/usr/bin/env bash
# stop-codegraph-reminder.sh — Stop hook (v0.2.72 P6)
#
# End-of-turn AGGREGATION of the "code file was just edited → update
# CONTEXT_STATE / capture KG" reminder. Previously post-file-edit.sh emitted
# this nudge on EVERY code-file Edit (~15x on a busy turn — pure repetition).
# Now post-file-edit.sh only APPENDS each edited path to a per-turn accumulator
# (.claude/state/edit_reminder_<sid>.txt); this Stop hook drains that file at
# turn-end and emits ONE aggregated reminder naming all edited files, then
# clears the accumulator so the next turn starts fresh.
#
# Contract / constraints:
#   - Stop hooks' plain stdout is discarded by the harness (v2.1.x), so the
#     reminder MUST go through emit_additional_context (JSON envelope). The
#     additionalContext surfaces into the NEXT turn — exactly the right moment
#     for a "when you resume this work, update CONTEXT_STATE" nudge.
#   - Always exit 0 — a Stop hook must never block turn end.
#   - Soft-fail throughout: a missing accumulator, unkeyable session, or parse
#     error simply emits nothing.
#   - No cross-language logic beyond the accumulator path convention, which
#     MUST MATCH stop-codegraph-reminder.ps1 + post-file-edit.{sh,ps1}:
#     .claude/state/edit_reminder_<sid>.txt.

unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# shellcheck source=_lib/stderr-cap.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/stderr-cap.sh" ] && . "$SCRIPT_DIR/_lib/stderr-cap.sh"
# shellcheck source=_lib/emit-context.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/emit-context.sh" ] && . "$SCRIPT_DIR/_lib/emit-context.sh"
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
# No interpreter → emit-context can't build the JSON envelope → soft no-op.
[ -z "${PY:-}" ] && exit 0

HOOK_STDIN=$(cat 2>/dev/null || echo "")
SESSION_ID=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    print(json.loads(sys.stdin.read()).get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

# Untrustworthy session id → no keyed accumulator to drain. (post-file-edit
# skips accumulation for the same case, so there is nothing to emit.)
[ -n "$SESSION_ID" ] || exit 0
# Reject a path-hostile session id (defence-in-depth; matches the seen-store
# sanitise policy — only [A-Za-z0-9_-] compose a real per-chat key).
case "$SESSION_ID" in
    *[!A-Za-z0-9_-]*) exit 0 ;;
esac

ACCUM="$PROJECT_ROOT/.claude/state/edit_reminder_${SESSION_ID}.txt"
[ -f "$ACCUM" ] || exit 0

# Drain: read the accumulated paths, dedup (a file re-edited across the turn
# lists once), reduce each to a basename for a compact reminder. Remove the
# accumulator AFTER reading so the next turn starts fresh (unlink even if the
# emit below no-ops — a drained turn should not carry stale paths forward).
FILES_RAW="$(cat "$ACCUM" 2>/dev/null || true)"
rm -f "$ACCUM" 2>/dev/null || true
[ -n "$FILES_RAW" ] || exit 0

# Unique basenames, preserving first-seen order, capped so a pathological turn
# can't blow the additionalContext budget (emit-context also hard-caps at 10k).
UNIQUE_NAMES="$(printf '%s\n' "$FILES_RAW" \
    | while IFS= read -r _p; do [ -n "$_p" ] && basename "$_p"; done \
    | awk '!seen[$0]++' \
    | head -40)"
[ -n "$UNIQUE_NAMES" ] || exit 0

COUNT="$(printf '%s\n' "$UNIQUE_NAMES" | grep -c . 2>/dev/null || printf '0')"
# One-line-per-file list, comma-free (basenames only).
FILE_LIST="$(printf '%s\n' "$UNIQUE_NAMES" | sed 's/^/  - /')"

REMINDER="[Code edit reminder] ${COUNT} code file(s) edited this turn:
${FILE_LIST}
When you're done with this work item:
- Update CONTEXT_STATE.md with what changed and what's next.
- Capture any non-obvious learnings as a KG node under knowledge/concepts/."

if command -v emit_additional_context >/dev/null 2>&1; then
    emit_additional_context "$REMINDER" Stop
fi

exit 0
