#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# session-worktree-reap.sh — SessionEnd + SessionStart hook: the point where
# an isolated agent's UNCHANGED worktree is removed (v0.2.107 REAP-INTERIM).
# MUST MATCH session-worktree-reap.ps1.
#
# WHY
# ---
# worktree-guard creates every isolated agent's worktree, and the harness
# neither removes a hook-created worktree nor runs WorktreeRemove when a
# subagent finishes (https://code.claude.com/docs/en/hooks#worktreeremove).
# subagent-stop-reconcile used to remove an unchanged one at SubagentStop —
# but SubagentStop also fires on an INTERIM stop (the agent waits on its own
# background work or a lock and resumes later), and the payload cannot tell
# interim from final, so the worktree was removed under a paused agent and
# SendMessage could no longer resume it. SubagentStop now only marks the
# worktree a reap CANDIDATE (_lib/worktree-records.sh :: wtrec_reap). This
# hook removes candidates once no session can resume their agent:
#
#   SessionEnd   records that this session ended (wtrec_session_ended — a
#                marker file only: SessionEnd hooks get a short harness
#                budget, and a `git worktree remove` killed mid-way would
#                leave half a worktree behind).
#   SessionStart a resumed session withdraws its own end marker
#                (wtrec_session_resumed; a resume keeps the session id), then
#                wtrec_sweep removes the candidates of ENDED sessions (and of
#                sessions idle for days that never recorded an end), never
#                those of the current session, and only after re-checking
#                that each worktree is still unchanged. Each decision is a row
#                in .claude/logs/worktree-guard.jsonl.
#
# Prints NOTHING on stdout (SessionStart stdout becomes model context);
# always exits 0.

# Canonical list: _lib/scrub-env.sh; the inline unset is the fallback for a missing lib.
{ . "${BASH_SOURCE[0]%/*}/_lib/scrub-env.sh" && vct_scrub_secret_env; } 2>/dev/null ||
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/_lib/stderr-cap.sh"
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$SCRIPT_DIR/_lib/find-python.sh" 2>/dev/null || true
[ -n "${PY:-}" ] || exit 0
[ -f "$SCRIPT_DIR/_lib/worktree-records.sh" ] || exit 0
# shellcheck source=_lib/worktree-records.sh disable=SC1091
. "$SCRIPT_DIR/_lib/worktree-records.sh"
# Project root: the ONE shell rule (_lib/project-root.sh).
# shellcheck source=_lib/project-root.sh disable=SC1091
. "$SCRIPT_DIR/_lib/project-root.sh"
PROJECT_ROOT="$(vco_project_root)" || PROJECT_ROOT=""
[ -n "$PROJECT_ROOT" ] || exit 0

HOOK_STDIN="$(cat 2>/dev/null || true)"
exec 1>/dev/null

PARSED="$(printf '%s' "$HOOK_STDIN" | "$PY" -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
for k in ("hook_event_name", "session_id", "source"):
    v = d.get(k)
    sys.stdout.write((v if isinstance(v, str) else "").replace("\n", " ") + "\n")
' 2>/dev/null || true)"
EVENT="$(printf '%s\n' "$PARSED" | sed -n '1p')"
SESSION_ID="$(printf '%s\n' "$PARSED" | sed -n '2p')"
SOURCE="$(printf '%s\n' "$PARSED" | sed -n '3p')"

case "$EVENT" in
    SessionEnd)
        wtrec_session_ended "$PROJECT_ROOT" "$SESSION_ID"
        ;;
    SessionStart)
        [ "$SOURCE" = "resume" ] && wtrec_session_resumed "$PROJECT_ROOT" "$SESSION_ID"
        wtrec_sweep "$PROJECT_ROOT" "$SESSION_ID" | while IFS="$(printf '\t')" read -r dec reason path name; do
            [ -n "$dec" ] || continue
            wtrec_log_reap "$PROJECT_ROOT/.claude/logs/worktree-guard.jsonl" session-worktree-reap \
                "${name#agent-}" "$dec" "$reason" "$path"
            if [ "$dec" = "kept" ]; then
                printf 'session-worktree-reap: worktree %s KEPT (%s) - review, merge or remove it\n' \
                    "$path" "$reason" >&2
            fi
        done
        ;;
esac
exit 0
