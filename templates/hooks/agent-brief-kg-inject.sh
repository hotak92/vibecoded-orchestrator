#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# agent-brief-kg-inject.sh — v0.2.101 injection redesign (PLAN-V02101 §C4).
# PreToolUse(Agent|Task) wrapper: enriches the subagent brief with the KG
# nodes matching the brief's TASK section, via the router's `agent` surface.
#
# This is the SubagentStart REPLACEMENT (owner-approved design): the
# SubagentStart payload carries only agent_id + agent_type — no prompt — so
# the old subagent-start-kg-inject query on prompt|task|description could
# never fire (broken by design; its KG half was retired in the same
# redesign — see that hook's header). The parent-side PreToolUse payload
# DOES carry tool_input.prompt, and PreToolUse `updatedInput` is the
# documented surface for modifying a tool call's input.
#
# Output contract — DIFFERENT from the other injection wrappers: the router
# prints the COMPLETE updatedInput envelope (it round-trips every original
# tool_input field and mutates only `prompt`), and this wrapper passes it
# through VERBATIM. It must NOT be wrapped in emit_additional_context
# (double envelope) and must NEVER add permissionDecision "allow" (that would
# auto-approve the spawn).
#
# v0.2.105 — PRE-FLIGHT VENDOR-KEY PROBE, run FIRST: when the launched agent's
# definition pins a gateway model (`model: claude-gw/<id>`), `python -m
# vco_lib.gateway_preflight` asks the running gateway, by NAME only, whether
# it can serve that model (`GET /vendor-keys`). On a DEFINITE "cannot" (no key
# for the vendor, or an id the router refuses) it prints a PreToolUse
# envelope with permissionDecision "deny" and a reason, this wrapper passes it
# through and stops — no KG leg for a launch that will not happen. Anything
# uncertain (gateway down, transient keychain, not a gateway agent) prints
# nothing and the launch proceeds as before. Kill switch:
# VCO_GATEWAY_PREFLIGHT=0. MUST MATCH agent-brief-kg-inject.ps1.
#
# Constraints:
# - Registered PreToolUse(Agent|Task), timeout 10 in settings.json.template
#   (§C4: above the router's 6 s inner budget; the measured manual run was
#   12 s cold, ~ms warm via the query cache).
# - Never exit non-zero (would block the dispatch). Empty/failed retrieval
#   → no output at all → the Agent input is untouched.

# Scrub sensitive env vars before any subprocess spawning
# (list MUST MATCH _lib/scrub-env.sh; enforced by the scrub parity gate).
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# v0.2.105: the hook's start, for the router's run deadline AND its hard
# end-to-end deadline (the harness timeout counts from HERE).
# One home: _lib/hook-start.sh (MUST MATCH the .ps1 sibling's call).
# shellcheck source=_lib/hook-start.sh disable=SC1091
[ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/hook-start.sh" ] && . "$(dirname "${BASH_SOURCE[0]}")/_lib/hook-start.sh" && vco_mark_hook_start

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"
[ -n "$VENV" ] || exit 0
export CLAUDE_PROJECT_DIR="$PROJECT_ROOT"

# stdin passthrough (see read-context-inject.sh for the R31 rationale).
HOOK_STDIN=$(cat 2>/dev/null || echo "")

# v0.2.105 pre-flight (see the header). Its stdout is EITHER nothing OR the
# complete deny envelope (vco_lib.gateway_preflight's contract), so any output
# is passed through verbatim and ends the hook. Its stderr is left alone: a
# broken install is reported there, loudly, and the launch is not refused.
PREFLIGHT=$(printf '%s' "$HOOK_STDIN" | "$VENV" -m vco_lib.gateway_preflight || true)
case "$PREFLIGHT" in
    *[![:space:]]*) printf '%s\n' "$PREFLIGHT"; exit 0 ;;
esac

resolve_vco_orchestrator_script "$SCRIPT_DIR" "claude_mcp_servers/scripts/hook_context_router.py"
ROUTER="${VCO_ORCHESTRATOR_SCRIPT:-}"
[ -n "$ROUTER" ] || exit 0

# The broken-install notice is keyed per SESSION (the same sentinel shape
# _lib/kg-sync-debounce.sh uses). The pattern-way extraction (no JSON parser
# at this stage) has ONE home: vco_hook_fast_session_id in _lib/session-id.sh
# (wave-2 review nit-7 — was an inline copy here). It lives in session-id,
# NOT in inject-budget, because the notice below must work precisely when
# inject-budget is the MISSING lib. A missing session-id.sh leaves _VCO_SID
# empty and the notice sanitizer accepts that.
_VCO_SID=""
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/session-id.sh" ]; then
    # shellcheck source=_lib/session-id.sh disable=SC1091
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/session-id.sh"
    # v0.2.102 W6-C2: keyed PER AGENT (<sid>__<agent_id> for a subagent's
    # call) so a subagent's notice cannot consume the parent's sentinel.
    if command -v vco_hook_fast_agent_key >/dev/null 2>&1; then
        _VCO_SID="$(vco_hook_fast_agent_key "$HOOK_STDIN")"
    elif command -v vco_hook_fast_session_id >/dev/null 2>&1; then
        _VCO_SID="$(vco_hook_fast_session_id "$HOOK_STDIN")"
    fi
fi

# v0.2.101: kill-switch fast path (VCO_INJECT_PROFILE=off). The lib is on
# vco_required_hook_libs' critical set: when it is MISSING that is a broken
# install — report it on stderr (once per session). This hook's stdout is the
# harness updatedInput envelope, so the notice CANNOT ride an
# additionalContext here; stderr + the SessionStart probe are the channels.
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/inject-budget.sh" ]; then
    # shellcheck source=_lib/inject-budget.sh disable=SC1091
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/inject-budget.sh"
    vco_inject_profile_off && exit 0
else
    if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ]; then
        # shellcheck source=_lib/emit-context.sh disable=SC1091
        . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
    fi
    command -v vco_report_missing_hook_lib >/dev/null 2>&1 \
        && vco_report_missing_hook_lib "$SCRIPT_DIR" "$PROJECT_ROOT" "$_VCO_SID" inject-budget
fi
ENVELOPE=$(printf '%s' "$HOOK_STDIN" | "$VENV" "$ROUTER" agent 2>/dev/null || true)

# The router's stdout IS the harness envelope (hookSpecificOutput.
# updatedInput, or nothing). Pass it through verbatim — no additional
# wrapping, no permissionDecision (the only decision this hook ever emits is
# the pre-flight's "deny", above).
case "$ENVELOPE" in
    *[![:space:]]*) printf '%s\n' "$ENVELOPE" ;;
esac
exit 0
