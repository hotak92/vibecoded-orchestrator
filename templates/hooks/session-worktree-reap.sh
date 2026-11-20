#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# session-worktree-reap.sh — SessionStart hook: the point where an isolated
# agent's worktree is removed (v0.2.107 REAP-INTERIM; owner rulings
# 2026-11-20). MUST MATCH session-worktree-reap.ps1.
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
# worktree (a reap CANDIDATE when unchanged, `kept` when changed —
# _lib/worktree-records.sh :: wtrec_reap). At every SessionStart this hook
# runs wtrec_sweep, which removes
#   * an UNCHANGED candidate once it has been idle for 7 days (not when its
#     chat ends: a chat can be resumed later), and
#   * a KEPT worktree whose commits are merged into the main checkout's
#     current branch and that has nothing uncommitted or untracked, with the
#     branch VCO created for it (`git branch -d`),
# never a worktree of the session that is starting, and only after
# re-checking every condition; any doubt (a lock, a process working in it, a
# git failure, an unknown main branch, a record that does not match) keeps
# it. Each decision is a row in .claude/logs/worktree-guard.jsonl.
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
for k in ("hook_event_name", "session_id"):
    v = d.get(k)
    sys.stdout.write((v if isinstance(v, str) else "").replace("\n", " ") + "\n")
' 2>/dev/null || true)"
EVENT="$(printf '%s\n' "$PARSED" | sed -n '1p')"
SESSION_ID="$(printf '%s\n' "$PARSED" | sed -n '2p')"

case "$EVENT" in
    SessionStart)
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
