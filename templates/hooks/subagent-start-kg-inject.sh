#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-start-kg-inject.sh — SubagentStart hook that retrieves KG
# context for the subagent's launch prompt and emits it as
# `additionalContext`. Mirrors `pre-edit-context-inject.sh`'s shape: it
# delegates the actual search to `claude_mcp_servers/scripts/
# rl_kg_search.py` (the canonical RL-aware retrieval chokepoint), then
# wraps the formatted matches in the SubagentStart JSON envelope so the
# freshly-spawned subagent's initial context includes the relevant KG
# nodes.
#
# Why this hook exists:
# - V52-L.2 Fix 3 (v0.2.52). Per A5 audit, SubagentStart hooks fire when
#   the parent spawns a Task/Agent subagent. The subagent inherits the
#   parent's user-level instructions but NOT the parent's session-state
#   KG retrievals. Without a hook like this, every subagent starts cold:
#   it has to either be told explicitly which KG nodes to consult, or
#   rediscover the relevant patterns from scratch — wasting tokens and
#   often missing patterns the parent already had in-context.
# - Mirrors `subagent-start-suggest.sh` (which surfaces matching
#   AGENTS/SKILLS) but for KG concepts. The two are complementary: the
#   suggest hook says "use these tools", this hook says "here's the
#   prior art".
#
# Constraints:
# - Must complete in <5 seconds (timeout in settings.json is 5s — bumped
#   from the standard 2s for SubagentStart because hybrid_search cold-
#   path takes 1.5-2.5s and we want a comfortable margin).
# - Never exit non-zero (would block subagent start). Always exit 0.
# - When the search returns empty or rl_kg_search.py is unavailable,
#   silent no-op.
#
# VCO-CENTRALIZED-KG: read-side delegator. Calls
# claude_mcp_servers/scripts/rl_kg_search.py, which honors
# VCT_KG_ACCESS_LIST through the shared helper in
# claude_mcp_servers/weaviate_mcp/server.py. No direct Weaviate access
# from this hook.

# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ]; then
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
fi
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0

# V52-L.1: source the snapshot helper so we can capture the
# filesystem state at SubagentStart. The SubagentStop reconciler will
# diff against this snapshot to identify files the subagent modified.
# Optional — when the helper is missing (partial install), the
# SubagentStop reconciler degrades to logging-only.
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/snapshot.sh" ]; then
    # shellcheck source=_lib/snapshot.sh disable=SC1091
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/snapshot.sh"
fi

# v0.2.77 Part 9 task 5: shared TTL result-cache so spawn N>1 with the same
# prompt is served from the cached KG result (~ms) instead of re-paying the
# ~3.8 s/spawn search (1793 spawns ~= 113 min in one fleet session — audit
# 2026-07-11). Every spawn STILL receives the injection, just from cache. The
# cache TTL self-refreshes and knowledge/ edits invalidate via the post-file-edit
# KG-sync path bumping the underlying nodes (the query key is the prompt, so a
# genuinely different task still misses + queries live). Sourced only if present.
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/query-cache.sh" ]; then
    # shellcheck source=_lib/query-cache.sh disable=SC1091
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/query-cache.sh"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Parse SubagentStart payload: prompt (with synonyms), session_id, and
# agent identity. The shared subagent-start-suggest.sh hook uses the
# same field-synonym set (prompt / task / description); replicate it
# here so both hooks behave identically on whatever wire format Claude
# Code happens to emit on this build.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
[ -z "$HOOK_STDIN" ] && exit 0

PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
prompt = d.get('prompt') or d.get('task') or d.get('description') or ''
session_id = d.get('session_id') or ''
agent_id = d.get('agent_id') or ''
agent_type = d.get('agent_type') or ''
sys.stdout.write(str(session_id) + '\n')
sys.stdout.write(str(agent_id) + '\n')
sys.stdout.write(str(agent_type) + '\n')
sys.stdout.write(str(prompt))
" 2>/dev/null || printf '\n\n\n')

SESSION_ID="$(printf '%s' "$PARSED" | sed -n '1p')"
AGENT_ID="$(printf '%s' "$PARSED" | sed -n '2p')"
AGENT_TYPE="$(printf '%s' "$PARSED" | sed -n '3p')"
PROMPT="$(printf '%s' "$PARSED" | tail -n +4)"

# V52-L.1: take a filesystem snapshot BEFORE the empty-prompt
# short-circuit. The snapshot is needed for the SubagentStop reconciler
# regardless of whether we end up injecting KG context (empty prompts
# still produce subagents that can modify files). Soft-fail: if the
# snapshot helper is missing or take_snapshot returns non-zero, the
# SubagentStop reconciler will fall back to logging-only mode.
if [ -n "$AGENT_ID" ] && command -v take_snapshot >/dev/null 2>&1; then
    # Run in a subshell so any state leakage / `set -e` from sourced
    # helpers cannot escape into the rest of the hook. Backgrounding is
    # tempting (parallelize with the KG search below) but would create a
    # snapshot-vs-edit race if the subagent starts modifying files
    # before take_snapshot has finished hashing them. Synchronous wins.
    (take_snapshot "$AGENT_ID" "$PROJECT_ROOT" >/dev/null 2>&1) || true
fi

[ -z "$PROMPT" ] && exit 0

# Export session/agent context so rl_kg_search.py's emit path attributes
# the retrieval event to this subagent. Mirrors V52-J Edit 4 in
# pre-edit-context-inject.sh: VCT_SESSION_ID is layer-2 of the canonical
# 3-layer session-id resolver; VCT_AGENT_ID / VCT_AGENT_TYPE are picked
# up by the same telemetry_emit chain for v0.2.52+ agent attribution.
[ -n "$SESSION_ID" ]  && export VCT_SESSION_ID="$SESSION_ID"
[ -n "$AGENT_ID" ]    && export VCT_AGENT_ID="$AGENT_ID"
[ -n "$AGENT_TYPE" ]  && export VCT_AGENT_TYPE="$AGENT_TYPE"

# Cap prompt to 400 chars for the query — rl_kg_search.py's RL reranker
# embeds the query; a 50K-char subagent prompt would just slow the
# embedding step without improving recall. 400 chars is enough to
# capture the task description while staying well under the embedder's
# small_context bucket (≤512 tokens for arctic1).
QUERY="${PROMPT:0:400}"

# Resolve VCO venv — the rl_kg_search.py module needs weaviate-client +
# vco_lib, which live in the orchestrator's MCP venv, not the user's
# project venv. The shared resolver enforces this distinction.
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"
RL_SCRIPT="$PROJECT_ROOT/claude_mcp_servers/scripts/rl_kg_search.py"

# Bail silently if the venv didn't resolve or the script is missing —
# this hook is best-effort context injection, never blocking.
if [ -z "$VENV" ] || [ ! -f "$RL_SCRIPT" ]; then
    exit 0
fi

# Run the search with --hook-format. Each result block is prefixed with
# "KG: <title> | <type> | score=<n.nn> | <body>". Limit to 3 matches —
# more would push past the SubagentStart additionalContext cap (10 KB
# in emit-context.sh) and dilute the signal.
#
# v0.2.77 Part 9 task 5: serve from the shared TTL cache when available. The
# cache key namespaces on the "kg-subagent" surface + prompt + limit so a repeat
# spawn with the same task prompt replays the cached RAW output (~ms) instead of
# re-running the ~3.8 s search. The RAW (pre-grep/pre-head) block is cached so
# the identical post-filtering below applies to a cache hit exactly as to a live
# result — the injection is byte-identical, just faster. Falls back to the direct
# call when the cache helper is absent (partial install).
_SAKG_RAW=""
_SAKG_KEY=""
if command -v vco_query_cache_key >/dev/null 2>&1; then
    _SAKG_KEY="$(vco_query_cache_key "kg-subagent" "$QUERY" 3)"
fi
if [ -n "$_SAKG_KEY" ] && command -v vco_query_cache_get >/dev/null 2>&1 \
        && _SAKG_RAW="$(vco_query_cache_get "$_SAKG_KEY")"; then
    : # cache hit — _SAKG_RAW holds the cached RAW producer output (maybe empty)
else
    _SAKG_RAW="$("$VENV" "$RL_SCRIPT" "$QUERY" --limit 3 --hook-format 2>/dev/null || true)"
    if [ -n "$_SAKG_KEY" ] && command -v vco_query_cache_put >/dev/null 2>&1; then
        vco_query_cache_put "$_SAKG_KEY" "$_SAKG_RAW"
    fi
fi
# Apply the SAME post-filtering to cache hits and live results (identical output).
MATCHES=$(printf '%s\n' "$_SAKG_RAW" \
    | grep -v "^KG: no-results" \
    | head -60 || echo "")

# Whitespace-only / empty match: silent exit (nothing useful to inject).
case "$MATCHES" in
    *[![:space:]]*) ;;
    *) exit 0 ;;
esac

# Format the additionalContext block. Mirror the pre-edit hook's shape
# so the subagent sees a familiar `[KG context for ...]:` header it can
# parse the same way as Edit-tool retrievals.
HEADER_LABEL="${AGENT_TYPE:-subagent}"
OUTPUT="[KG context for ${HEADER_LABEL} task]:"$'\n'$'\n'"${MATCHES}"$'\n'

if command -v emit_additional_context >/dev/null 2>&1; then
    emit_additional_context "$OUTPUT" SubagentStart
else
    # Inline fallback envelope when _lib/emit-context.sh is missing
    # (partial install). Same JSON shape as emit_additional_context.
    "$PY" -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SubagentStart',
        'additionalContext': sys.stdin.read(),
    }
}))
" <<< "$OUTPUT" 2>/dev/null || true
fi

exit 0
