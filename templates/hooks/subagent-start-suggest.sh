#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-start-suggest.sh — SubagentStart hook that injects agent/skill
# suggestions into a freshly-spawned subagent's context, mirroring the
# UserPromptSubmit-side `agent-skill-keyword-suggest.sh` but for spawn-time.
#
# Why: when the parent Claude spawns a Task/Agent subagent, the spawner
# may forget to enumerate which skills/agents are relevant for the
# subagent's task. This hook scans the subagent's launch prompt for
# `keywords:` matches across `.claude/agents/*.md` and
# `.claude/skills/*/SKILL.md`, then injects a `You might want to use ...`
# block into the subagent's initial context.
#
# Agent vs skill differentiation:
# - Skills: always suggested (any subagent that runs Claude can use /skill).
# - Agents: only suggested when the subagent itself has `Agent` or `Task`
#   in its effective tool list. A subagent without those tools can't spawn
#   sub-subagents, so naming agents in its context would be noise. We
#   pass `--skills-only` to the matcher when Agent/Task is absent.
#
# Recursion: bounded by Claude Code's existing max subagent depth (4).
# No additional depth-based gating here.
#
# Matching logic: identical to the UserPromptSubmit hook — same matcher
# script (`templates/scripts/agent-skill-keyword-match.py`), same
# whole-word case-insensitive matching against curated `keywords:` lists.
# The curation on each agent/skill is the source of robustness, not any
# algorithmic transformation of the prompt (user direction 2026-05-25).
#
# Filesystem contract: same as the UserPromptSubmit sibling — globs
# `.claude/agents/*.md` and `.claude/skills/*/SKILL.md`. Disabled items
# are moved by the launcher to sibling `.disabled/` directories, so they
# naturally fall outside the glob — no DB lookup needed.
#
# Always exits 0 (never blocks subagent start). Silent when no match.

# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Stderr cap so a buggy iteration cannot reproduce the 2026-05-07 GUI freeze.
if [ -f "$SCRIPT_DIR/_lib/stderr-cap.sh" ]; then
    # shellcheck source=_lib/stderr-cap.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/stderr-cap.sh"
fi

# Resolve a Python interpreter portably.
if [ -f "$SCRIPT_DIR/_lib/find-python.sh" ]; then
    # shellcheck source=_lib/find-python.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/find-python.sh"
fi
if [ -z "${PY:-}" ]; then
    PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || command -v py 2>/dev/null || true)"
fi
[ -z "${PY:-}" ] && exit 0  # No Python interpreter → silent no-op.

# emit-context.sh is preferred (handles 10 KB cap + whitespace gate +
# JSON envelope shape). Inline fallback below if missing.
if [ -f "$SCRIPT_DIR/_lib/emit-context.sh" ]; then
    # shellcheck source=_lib/emit-context.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/emit-context.sh"
fi

# Project root resolution: CLAUDE_PROJECT_DIR is the canonical signal at
# hook fire time; fall back to PWD for ad-hoc invocations.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"

# Locate the shared matcher. Same resolution chain as the UserPromptSubmit
# wrapper — installed layout first (`.claude/scripts/`), templates fallback
# for uninstalled / orchestrator-clone testing.
MATCHER="$SCRIPT_DIR/../scripts/agent-skill-keyword-match.py"
if [ ! -f "$MATCHER" ]; then
    if [ -f "$SCRIPT_DIR/../../templates/scripts/agent-skill-keyword-match.py" ]; then
        MATCHER="$SCRIPT_DIR/../../templates/scripts/agent-skill-keyword-match.py"
    else
        exit 0  # Matcher missing → silent no-op.
    fi
fi

# SubagentStart hook input contract: JSON payload on stdin. We need:
#   - The subagent's launch prompt — emitted as `prompt`. We also tolerate
#     `task`/`description` synonyms in case the contract phrases the field
#     differently on a given Claude Code build (defensive — both should
#     never be set simultaneously; prefer `prompt` when ambiguous).
#   - The subagent's effective tool list — emitted as `tools`. Also
#     tolerate `allowed_tools` / `tool_list` synonyms.
#   - The session id — `session_id`. Empty is fine; the matcher's dedup
#     just no-ops when missing.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
[ -z "$HOOK_STDIN" ] && exit 0

# Parse the three fields in a single Python invocation. Emit them on three
# stdout lines (session_id, has_agent_tool 0/1, prompt body). Empty fields
# render as blank lines. Any JSON error → silent no-op (downstream guard
# catches the empty prompt).
PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
# Prompt: try common field names. Empty → silent no-op via downstream guard.
prompt = d.get('prompt') or d.get('task') or d.get('description') or ''
session_id = d.get('session_id') or ''
# Tools: try common field names. Accept either a list of strings or a
# space/comma-separated string. Normalise to a list, then check for the
# Agent/Task tools.
tools = d.get('tools') or d.get('allowed_tools') or d.get('tool_list') or []
if isinstance(tools, str):
    # Split on comma OR whitespace (be liberal).
    import re
    tools = [t.strip() for t in re.split(r'[\s,]+', tools) if t.strip()]
elif not isinstance(tools, list):
    tools = []
# Case-insensitive membership check for Agent / Task (both Claude Code
# names for the subagent-spawning capability).
tool_names_lower = {str(t).lower() for t in tools}
has_agent_tool = 1 if ('agent' in tool_names_lower or 'task' in tool_names_lower) else 0
# When the tools field is COMPLETELY ABSENT or empty list, default to
# allowing agent suggestions — that's the most common case in the
# wild and we'd rather over-suggest than starve the spawner of useful
# hints (user direction: 'cost of MISSING is higher than over-suggest').
if not tools:
    has_agent_tool = 1
sys.stdout.write(str(session_id) + '\n')
sys.stdout.write(str(has_agent_tool) + '\n')
sys.stdout.write(str(prompt))
" 2>/dev/null || printf '\n0\n')

SESSION_ID="$(printf '%s' "$PARSED" | sed -n '1p')"
HAS_AGENT_TOOL="$(printf '%s' "$PARSED" | sed -n '2p')"
PROMPT="$(printf '%s' "$PARSED" | tail -n +3)"

[ -z "$PROMPT" ] && exit 0

# Build the matcher argv. --session-id is always passed (empty value =
# dedup disabled). --skills-only is passed only when Agent/Task is
# absent from the subagent's tool list.
MATCHER_ARGS=( "--session-id" "$SESSION_ID" )
if [ "${HAS_AGENT_TOOL:-1}" != "1" ]; then
    MATCHER_ARGS+=( "--skills-only" )
fi

# Run the matcher. Pure-stdlib, always exits 0, prints either an empty
# string or 1-2 short bullet blocks.
MSG=$(printf '%s' "$PROMPT" | CLAUDE_PROJECT_DIR="$PROJECT_ROOT" "$PY" "$MATCHER" "${MATCHER_ARGS[@]}" 2>/dev/null || printf '')

[ -z "$MSG" ] && exit 0

# Whitespace-only guard happens inside emit_additional_context too, but
# checking here avoids the subprocess for the common no-match case.
case "$MSG" in
    *[![:space:]]*) ;;
    *) exit 0 ;;
esac

if command -v emit_additional_context >/dev/null 2>&1; then
    emit_additional_context "$MSG" "SubagentStart"
else
    # Inline fallback envelope.
    "$PY" -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SubagentStart',
        'additionalContext': sys.stdin.read(),
    }
}))
" <<< "$MSG" 2>/dev/null || true
fi

exit 0
