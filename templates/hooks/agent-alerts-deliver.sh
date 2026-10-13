#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# agent-alerts-deliver.sh — v0.2.102 W6-C2 (census F8): surface the alerts a
# SUBAGENT hit to its PARENT.
#
# Hook context from a subagent's tool call (post-tool-security's credential
# alert, for one) reaches only that subagent, and SubagentStop findings (the
# reconciler's kept-worktree notice, a worktree violation, a credential in a
# file no tool event saw) had no channel to the main chat at all. Writers file
# them per agent under .claude/state/agent_alerts_<sid>__<agent>.jsonl; this
# hook hands them to the parent as additionalContext:
#   * PostToolUse (matcher *) for Agent|Task naming the agent — the finished
#     foreground agent's rows (tool_response.agentId names it);
#   * any other MAIN-CHAT PostToolUse (W6-C2 fix round, review R4c-10) and
#     UserPromptSubmit — every pending row of the session (a background
#     agent's completion, a detached SubagentStop job finishing after the
#     foreground delivery), without waiting for the next prompt. A
#     subagent's own events deliver nothing (vco_lib decides).
# The reader, the row format and the locking live in ONE place:
# vco_lib/agent_alerts.py. Cost when nothing is pending — every tool call
# once the * matcher is in place — is one glob, no interpreter. Always exits
# 0. Alerts pending with no resolvable VCO venv (a broken install) are said
# out loud — stderr and the context — never skipped in silence.
#
# MUST MATCH agent-alerts-deliver.ps1.

unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
STATE_DIR="$PROJECT_ROOT/.claude/state"

HOOK_STDIN=$(cat 2>/dev/null || echo "")
[ -z "$HOOK_STDIN" ] && exit 0

# Fast path: nothing pending -> no interpreter start.
_pending=0
for _f in "$STATE_DIR"/agent_alerts_*.jsonl; do
    [ -f "$_f" ] && { _pending=1; break; }
done
[ "$_pending" = "1" ] || exit 0

# The stderr cap only once there is something to say: the per-tool-call
# no-pending path above stays a glob (matcher `*`). MUST MATCH the .ps1.
# shellcheck source=_lib/stderr-cap.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/stderr-cap.sh" ] && . "$SCRIPT_DIR/_lib/stderr-cap.sh"

# vco_lib is needed, so the VCO venv (a bare system python cannot import it).
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/resolve-vco-venv.sh" ] && . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
VCO_VENV_PYTHON=""
command -v resolve_vco_venv_python >/dev/null 2>&1 && resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"
if [ -z "$VENV" ] || [ ! -x "$VENV" ]; then
    _AD_MSG="[Subagent alerts] Alerts your subagents hit are pending in $STATE_DIR/agent_alerts_*.jsonl but cannot be read: the VCO venv is not resolvable (a broken VCO install - repair it with the orchestrator update). Read those files directly."
    echo "agent-alerts-deliver: $_AD_MSG" >&2
    _AD_EVENT="PostToolUse"
    case "$HOOK_STDIN" in *'"UserPromptSubmit"'*) _AD_EVENT="UserPromptSubmit" ;; esac
    # shellcheck source=_lib/emit-context.sh disable=SC1091
    [ -f "$SCRIPT_DIR/_lib/emit-context.sh" ] && . "$SCRIPT_DIR/_lib/emit-context.sh"
    command -v emit_additional_context >/dev/null 2>&1 && \
        emit_additional_context "$_AD_MSG" "$_AD_EVENT"
    exit 0
fi

printf '%s' "$HOOK_STDIN" | "$VENV" -m vco_lib.agent_alerts deliver \
    --project-root "$PROJECT_ROOT" 2>/dev/null || true
exit 0
