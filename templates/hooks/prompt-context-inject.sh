#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# prompt-context-inject.sh — v0.2.102 W6-F: the prompt-time leg of the
# injection redesign, and the mechanism behind the shipped promise
# "On every prompt (UserPromptSubmit hook): Searches the Knowledge Graph …
# Injects matches" (README.md, docs/GETTING_STARTED.md — UNWIRED until this
# hook: no UserPromptSubmit registration queried anything).
#
# The wrapper is THIN by design: stdin → the ONE router
# (claude_mcp_servers/scripts/hook_context_router.py, surface `prompt`) →
# emit_additional_context envelope. EVERY decision — the query rule
# (vco_lib.inject_intent.prompt_query: the user's prompt text,
# whitespace-collapsed and capped; trivially short prompts, slash commands
# and bang passthroughs skip), the §2.1 `prompt` gate (0.725 floor,
# titles-only below 0.85), the seen-store dedupe, the per-injection cap,
# the RL `prompt_kg_search` task type — lives in the router and
# vco_lib/inject_intent.py. MUST MATCH prompt-context-inject.ps1.
#
# Constraints:
# - Registered UserPromptSubmit, timeout 10 in settings.json.*.template —
#   ABOVE the router's 6 s inner budget so the router, not the harness, is
#   what bounds a cold run (the fail-open-to-silence discipline of every
#   injection surface; a prompt must never be blocked by retrieval).
# - The surface runs a KG leg only: §2.1 issues no semantic code-graph
#   queries from injection surfaces, and a free-text prompt yields no
#   exact-lookup symbol.
# - Never exit non-zero. Empty/failed retrieval → exit 0 silently.
# - prompt_id IS a common hook-input field and is normally PRESENT on
#   UserPromptSubmit (it names the submitted prompt), so the router's
#   per-turn budget applies per turn here; it may be absent on the first
#   turn / older harnesses, in which case the budget fails OPEN for that
#   turn (documented in the router). The per-injection soft cap bounds the
#   block either way.

# Scrub sensitive env vars before any subprocess spawning
# (list MUST MATCH _lib/scrub-env.sh; enforced by the scrub parity gate).
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# v0.2.105: the hook's start, for the router's run deadline AND its hard
# end-to-end deadline (the harness timeout counts from HERE).
# One home: _lib/hook-start.sh (MUST MATCH the .ps1 sibling's call).
# shellcheck source=_lib/hook-start.sh disable=SC1091
[ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/hook-start.sh" ] && . "$(dirname "${BASH_SOURCE[0]}")/_lib/hook-start.sh" && vco_mark_hook_start

# v0.2.102 W6-F fix-round: per-surface kill switch (VCO_PROMPT_INJECT=off)
# disables ONLY this prompt surface — the per-prompt python spawn (router +
# KG search) is the one injection surface whose latency sits on the critical path
# of EVERY prompt, so it gets its own opt-out beside the global
# VCO_INJECT_PROFILE=off (checked in _lib/inject-budget.sh below, which
# disables every surface). Checked here BEFORE anything is spawned; the
# router re-checks it (the enforcement home). MUST MATCH the .ps1 sibling.
case "$(printf '%s' "${VCO_PROMPT_INJECT:-}" | tr '[:upper:]' '[:lower:]')" in
    off) exit 0 ;;
esac

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ]; then
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Resolve the VCO venv (the router needs the venv's packages + vco_lib) and
# the router script — both live ONLY in the orchestrator root, never under
# the user's project (v0.2.100 F3 discipline). The router still runs with
# THIS project's CLAUDE_PROJECT_DIR/env, so the calling project's KG +
# shared + granted collections apply.
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"
[ -n "$VENV" ] || exit 0
resolve_vco_orchestrator_script "$SCRIPT_DIR" "claude_mcp_servers/scripts/hook_context_router.py"
ROUTER="${VCO_ORCHESTRATOR_SCRIPT:-}"
[ -n "$ROUTER" ] || exit 0
export CLAUDE_PROJECT_DIR="$PROJECT_ROOT"

# stdin passthrough: the router re-parses the full hook payload itself
# (session_id / transcript_path / prompt — query text never travels through
# argv, R31 privacy discipline).
HOOK_STDIN=$(cat 2>/dev/null || echo "")

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

# v0.2.101: kill-switch fast path (VCO_INJECT_PROFILE=off) — the router
# re-checks it, but checking here avoids the spawn entirely. The lib is on
# vco_required_hook_libs' critical set: when it is MISSING that is a broken
# install — report it loudly (once per session, both channels) and keep
# running (the router owns the real enforcement).
_VCO_INJECT_LIB_NOTICE=""
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/inject-budget.sh" ]; then
    # shellcheck source=_lib/inject-budget.sh disable=SC1091
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/inject-budget.sh"
    vco_inject_profile_off && exit 0
elif command -v vco_report_missing_hook_lib >/dev/null 2>&1; then
    vco_report_missing_hook_lib "$SCRIPT_DIR" "$PROJECT_ROOT" "$_VCO_SID" inject-budget
    _VCO_INJECT_LIB_NOTICE="${VCO_MISSING_LIB_NOTICE:-}"
fi
INJECT_TEXT=$(printf '%s' "$HOOK_STDIN" | "$VENV" "$ROUTER" prompt 2>/dev/null || true)

# ONE envelope per invocation: the (optional) broken-install notice and the
# (optional) injection text share it — plain UserPromptSubmit stdout without
# the envelope still reaches the conversation, but the envelope is the
# documented shape every other surface uses.
_VCO_OUT=""
case "$INJECT_TEXT" in
    *[![:space:]]*) _VCO_OUT="[KG context for this prompt]:"$'\n'$'\n'"${INJECT_TEXT}" ;;
esac
if [ -n "$_VCO_INJECT_LIB_NOTICE" ]; then
    _VCO_OUT="${_VCO_INJECT_LIB_NOTICE}${_VCO_OUT:+$'\n'$'\n'$_VCO_OUT}"
fi
case "$_VCO_OUT" in
    *[![:space:]]*)
        if command -v emit_additional_context >/dev/null 2>&1; then
            emit_additional_context "$_VCO_OUT" UserPromptSubmit
        fi
        ;;
esac
exit 0
