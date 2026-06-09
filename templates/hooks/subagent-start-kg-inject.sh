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
MATCHES=$("$VENV" "$RL_SCRIPT" "$QUERY" --limit 3 --hook-format 2>/dev/null \
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
