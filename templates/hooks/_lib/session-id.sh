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

# vco_hook_fast_session_id <hook-stdin-json>
# v0.2.101 (wave-2 review nit-7): the PRE-python session-id extraction the
# router wrappers share — one home instead of three inline copies. Pattern-
# based (grep+sed over the raw payload, allow-list [A-Za-z0-9_-]) so it runs
# BEFORE any venv/JSON-parser resolution, and INDEPENDENTLY of the lib whose
# absence it may be about to report (the broken-install notice in
# emit-context.sh needs a session key even when _lib/inject-budget is the
# missing file — that dependency cycle is why this lives HERE, not there).
# A hostile or missing id yields "" — consumers sanitize for real anyway
# (vco_hook_sanitize_session_id / the router's sanitize_session_id).
# MUST MATCH session-id.ps1's Get-VcoHookFastSessionId (parity-pinned by
# tests/test_v02101_inject_gates.py).
vco_hook_fast_session_id() {
    printf '%s' "${1:-}" \
        | grep -Eo '"session_id"[[:space:]]*:[[:space:]]*"[A-Za-z0-9_-]+"' \
        | head -n 1 \
        | sed 's/.*"\([A-Za-z0-9_-]*\)"$/\1/'
}

# ─── v0.2.102 W6-C1 (F1/F2/F3/F11): the per-AGENT state key ──────────────────
# Harness fact (probe 2026-10-13): subagent tool events carry agent_id while
# session_id / prompt_id / transcript_path stay the PARENT's — so state keyed
# on session_id alone was shared by the main chat and every subagent. These
# helpers compose the key every per-agent store uses:
#   main agent (no agent_id)   → <sid>
#   subagent (agent_id set)    → <sid>__<agent_id>
# Policy (identical in all three legs — see the comments in each):
#   * the agent id is sanitised with the session allow-list MINUS "_"
#     ([A-Za-z0-9-], v0.2.102 W6-C2: keeps the composite decodable); hostile
#     → the literal "default" component (its OWN bucket, never the main
#     store, never raw bytes in a file name);
#   * an untrustworthy session id ("" / "default") passes through UNCHANGED —
#     composing "default__<agent>" would re-create the cross-session bleed
#     the seen-store's inject-blind guard exists to prevent.
# MUST MATCH: session-id.ps1 (Get-VcoSanitizedAgentId / Get-VcoAgentKeyFromIds
# / Get-VcoHookAgentKey) and vco_lib/hook_agent_key.py — parity pinned by
# tests/test_v02102_w6c1_agent_keyed_hook_state.py.

# vco_hook_sanitize_agent_id: the agent component's guard. Allow-list
# [A-Za-z0-9-] -- the session charset MINUS "_" (v0.2.102 W6-C2, review R1
# C1): with "_" allowed on both sides, ("a__b","c") and ("a","b__c") composed
# the same key. Without it the composite decodes uniquely (agent = text after
# the last "_"). Real agent ids are hex, so real keys are unchanged. Empty
# stays empty; anything else (an "_" included) collapses to "default".
vco_hook_sanitize_agent_id() {
    local raw="$1"
    [ -n "$raw" ] || { printf '%s' ""; return 0; }
    case "$raw" in
        *[!a-zA-Z0-9-]*) printf '%s' "default" ;;
        *)               printf '%s' "$raw" ;;
    esac
}

# vco_hook_agent_key_from_ids <session_id> <agent_id>
# Compose the per-agent key from an (already-sanitised, or trusted-legacy)
# session id and a RAW agent id. Both are sanitised here, so a legacy caller
# passing an unsanitised session id cannot smuggle path-hostile bytes into
# the composite either. No subshell to Python — cheap and dependency-free.
vco_hook_agent_key_from_ids() {
    local sid agent
    sid="$(vco_hook_sanitize_session_id "$1")"
    agent="$(vco_hook_sanitize_agent_id "$2")"
    if [ -z "$agent" ] || [ -z "$sid" ] || [ "$sid" = "default" ]; then
        printf '%s' "$sid"
        return 0
    fi
    printf '%s' "${sid}__${agent}"
}

# vco_hook_fast_agent_key <hook-stdin-json>
# The pattern-way (no JSON parser) per-agent key, for the injection wrappers'
# once-per-session broken-install notices: they key their sentinel BEFORE any
# interpreter runs, and the notice must work when a lib is missing. Same
# extraction discipline as vco_hook_fast_session_id; the agent value is
# sanitised by vco_hook_agent_key_from_ids (hostile -> "default").
# v0.2.102 W6-C2 (C1 residual): a subagent's notice used to consume the
# parent's session sentinel. MUST MATCH session-id.ps1 Get-VcoHookFastAgentKey.
vco_hook_fast_agent_key() {
    local sid agent
    sid="$(vco_hook_fast_session_id "${1:-}")"
    agent="$(printf '%s' "${1:-}" \
        | grep -Eo '"agent_id"[[:space:]]*:[[:space:]]*"[^"]*"' \
        | head -n 1 \
        | sed 's/.*"\([^"]*\)"$/\1/')"
    vco_hook_agent_key_from_ids "$sid" "$agent"
}

# vco_agent_alerts_file <project_root> <session_id> <agent_id>
# v0.2.102 W6-C2 (census F8): the pending-alert file a SUBAGENT's alert is
# filed in, for delivery to the PARENT (vco_lib/agent_alerts.py is the reader
# and owns the row format). Prints "" for the main chat (no agent id). An
# untrustworthy session id files under "nosession__<agent>".
# MUST MATCH vco_lib.agent_alerts.alert_file and session-id.ps1
# Get-VcoAgentAlertsFile.
vco_agent_alerts_file() {
    local proot="${1:-}" agent key
    agent="$(vco_hook_sanitize_agent_id "${3:-}")"
    if [ -z "$proot" ] || [ -z "$agent" ]; then
        printf '%s' ""
        return 0
    fi
    key="$(vco_hook_agent_key_from_ids "${2:-}" "$agent")"
    case "$key" in
        *"__$agent") ;;
        *) key="nosession__$agent" ;;
    esac
    printf '%s' "$proot/.claude/state/agent_alerts_${key}.jsonl"
}

# vco_hook_agent_key <hook-stdin-json>
# Parse session_id AND agent_id from the payload (requires $PY, like
# vco_hook_session_id), sanitise both, compose. The main-agent payload
# (absent/empty agent_id) yields exactly vco_hook_session_id's answer.
vco_hook_agent_key() {
    local stdin_payload="$1"
    local sid="" agent=""
    if [ -n "${PY:-}" ]; then
        parsed=$(printf '%s' "$stdin_payload" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('session_id', '') or '')
    print(d.get('agent_id', '') or '')
except Exception:
    print('')
    print('')
" 2>/dev/null || printf '\n\n')
        sid="$(printf '%s' "$parsed" | sed -n '1p')"
        agent="$(printf '%s' "$parsed" | sed -n '2p')"
    fi
    vco_hook_agent_key_from_ids "$sid" "$agent"
}
