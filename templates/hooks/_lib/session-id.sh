# shellcheck shell=bash
# _lib/session-id.sh
# Shared helper sourced by the context hooks that key per-session state files
# off the Claude Code session_id (diff-context-inject, compact-context-reinject,
# context-size-check, post-compact).
#
# Why this exists (one concern, one home — CLAUDE.md "search before add")
# -----------------------------------------------------------------------
# Before this helper, each of the four hooks INLINED its own
# `json.loads(stdin) → get('session_id')` Python parse (Track C even added a
# second copy of that parse to two of them). That is the same logic in four
# places — a maintenance hazard and, more importantly, a security one: the
# session_id is interpolated verbatim into FILE PATHS
# (`.claude/context/CONTEXT_STATE_${SESSION_ID}.md`,
# `.claude/state/ctx_snapshot_session_${SESSION_ID}`). session_id is normally a
# trusted Claude-generated UUID, but a `/` or `..` in it would compose an
# unintended path. Centralising the parse AND the sanitise here means the
# defense-in-depth guard lives in ONE place, applied identically everywhere.
#
# Defense-in-depth: this is the first code that puts session_id into a
# user-curated CONTENT dir (.claude/context/), not just the throwaway
# .claude/state/ baselines — so path hygiene matters here (review C-1).
#
# MUST MATCH: templates/hooks/_lib/session-id.ps1 (the sanitise charset
# [A-Za-z0-9_-] and the `default` fallback must agree cross-OS).
#
# Usage (from any hook, AFTER sourcing _lib/find-python.sh so $PY is set):
#     SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#     . "$SCRIPT_DIR/_lib/find-python.sh"
#     . "$SCRIPT_DIR/_lib/session-id.sh"
#     SESSION_ID=$(vco_hook_session_id "$HOOK_STDIN")
#
# Returns (on stdout, exactly one line, never a trailing path-fragile char):
#   - sanitised session_id   when the payload carried a clean session_id
#   - "default"              when the payload carried a session_id containing
#                            any char outside [A-Za-z0-9_-] (hostile/odd id)
#   - ""  (empty)            when the payload had no session_id / was malformed
#                            / could not be parsed (no $PY available).
#
# Each caller then applies its OWN empty-handling convention on top:
#   - diff-context-inject treats empty as "default" (it always wants a key);
#   - compact-context-reinject / context-size-check gate on `[ -n ... ]`
#     (empty => skip the per-session block);
# This split is intentional and preserved — the helper unifies parse+sanitise,
# not the per-hook empty policy.
#
# This file is sourced, never executed, so it has no shebang. It is a library,
# not a hook — it is NOT registered in settings.json.template.

# vco_hook_sanitize_session_id: pure-bash path-safety guard.
# Echoes the input unchanged if it consists solely of [A-Za-z0-9_-]; otherwise
# echoes "default". An empty input echoes empty (callers decide what empty
# means). No subshell to Python — this must be cheap and dependency-free so it
# can run even when no interpreter was found.
vco_hook_sanitize_session_id() {
    local raw="$1"
    # Empty stays empty — the caller's empty-policy handles it.
    [ -n "$raw" ] || { printf '%s' ""; return 0; }
    # Any char outside the allow-list => fall back to the safe sentinel.
    case "$raw" in
        *[!a-zA-Z0-9_-]*) printf '%s' "default" ;;
        *)                printf '%s' "$raw" ;;
    esac
}

# vco_hook_session_id: parse session_id from a hook stdin JSON payload ($1),
# then sanitise it. Requires $PY (set by _lib/find-python.sh) for the JSON
# parse; with no interpreter, returns empty (caller's empty-policy applies).
# Soft-fail throughout: a malformed payload yields empty, never an error.
vco_hook_session_id() {
    local stdin_payload="$1"
    local parsed=""
    if [ -n "${PY:-}" ]; then
        parsed=$(printf '%s' "$stdin_payload" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('session_id', '') or '')
except Exception:
    print('')
" 2>/dev/null || printf '%s' "")
    fi
    vco_hook_sanitize_session_id "$parsed"
}
