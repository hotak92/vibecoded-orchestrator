#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-start-kg-inject.sh — SubagentStart hook: V52-L.1 filesystem
# snapshot ONLY (v0.2.101 injection redesign, PLAN-V02101 §C5).
#
# WHAT THIS HOOK DOES: take a filesystem snapshot at subagent start so the
# SubagentStop reconciler (subagent-stop-reconcile.sh) can diff against it
# and identify files the subagent modified. The SubagentStop hook depends
# on this snapshot — do not remove it.
#
# WHAT IT NO LONGER DOES (v0.2.101): the KG-injection half that queried
# rl_kg_search.py on the payload's prompt|task|description field was
# RETIRED — superseded by the parent-side PreToolUse(Agent|Task) hook
# agent-brief-kg-inject.{sh,ps1}. The SubagentStart payload carries ONLY
# agent_id + agent_type (official hooks docs, re-verified 2026-10-04), so
# the old query could never fire: broken by design, not by configuration.
# The KG context a subagent needs now arrives inside its own first prompt
# (the brief), keyed on the brief's TASK section — see
# claude_mcp_servers/scripts/hook_context_router.py (surface `agent`).
#
# The FILENAME is deliberately kept (the §C5 fallback): renaming a shipped
# hook strands every installed copy that other tests and the reconciler's
# docs reference by name; the bundle update would delete the old file and
# create the new one, but the rename buys nothing that the honest header
# above does not already buy.
#
# Constraints:
# - Never exit non-zero (would block subagent start). Always exit 0.
# - The snapshot is synchronous (a backgrounded snapshot would race the
#   subagent's own first edits); soft-fail when the helper is missing
#   (partial install) — the reconciler degrades to logging-only.

# Scrub sensitive env vars before any subprocess spawning
# (list MUST MATCH _lib/scrub-env.sh; enforced by the scrub parity gate).
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

# V52-L.1: source the snapshot helper. Optional — when the helper is
# missing (partial install), the SubagentStop reconciler degrades to
# logging-only.
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/snapshot.sh" ]; then
    # shellcheck source=_lib/snapshot.sh disable=SC1091
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/snapshot.sh"
fi

# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Parse the SubagentStart payload: agent identity. The payload carries
# session_id + agent_id + agent_type ONLY (no prompt/task text — that is
# why the KG half this hook used to have could never fire).
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
agent_id = d.get('agent_id') or ''
sys.stdout.write(str(agent_id))
" 2>/dev/null || printf '')

AGENT_ID="$PARSED"

# Take the filesystem snapshot BEFORE anything else. Soft-fail: if the
# snapshot helper is missing or take_snapshot returns non-zero, the
# SubagentStop reconciler falls back to logging-only mode.
if [ -n "$AGENT_ID" ] && command -v take_snapshot >/dev/null 2>&1; then
    # Run in a subshell so any state leakage / `set -e` from sourced
    # helpers cannot escape into the rest of the hook. Synchronous (not
    # backgrounded) so the snapshot cannot race the subagent's first
    # edits. MUST MATCH the .ps1 sibling.
    (take_snapshot "$AGENT_ID" "$PROJECT_ROOT" >/dev/null 2>&1) || true
fi

exit 0
