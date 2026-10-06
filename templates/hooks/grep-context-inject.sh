#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# grep-context-inject.sh — v0.2.101 injection redesign (PLAN-V02101 §C6).
# PreToolUse(Grep) wrapper: when the Grep PATTERN is an identifier, inject
# the exact symbol's definition + callers from the code graph.
#
# This hook REPLACES pre-tool-use.sh's Grep(symbol) branch (one concern,
# one home). Differences from the old branch, all owned by the router:
#   * identifier gating + symbol extraction live in vco_lib.inject_intent
#     (the Python one-home for codegraph_pattern_gate /
#     codegraph_extract_symbol);
#   * the lookup is EXACT (structure callers <symbol> --hook-format), not
#     a semantic search keyed on the pattern;
#   * NO KG leg (§2.1: the Grep surface runs the code-graph leg only).
# Glob gets NO injection surface at all (§C6 decision: a glob carries no
# query signal).
#
# The wrapper is THIN: stdin → the ONE router (surface `grep`) →
# emit_additional_context envelope. MUST MATCH grep-context-inject.ps1.
#
# Constraints:
# - Registered PreToolUse(Grep), timeout 10 in settings.json.template —
#   ABOVE the router's 8 s inner budget (SF-1: an equal timeout gets
#   cold runs harness-killed; the router must be what bounds itself).
# - No `if` filter (§C6): a pattern's shape is not a path glob.
# - Never exit non-zero (would block the Grep). Empty/failed retrieval →
#   exit 0 silently.

# Scrub sensitive env vars before any subprocess spawning
# (list MUST MATCH _lib/scrub-env.sh; enforced by the scrub parity gate).
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ]; then
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"
[ -n "$VENV" ] || exit 0
resolve_vco_orchestrator_script "$SCRIPT_DIR" "claude_mcp_servers/scripts/hook_context_router.py"
ROUTER="${VCO_ORCHESTRATOR_SCRIPT:-}"
[ -n "$ROUTER" ] || exit 0
export CLAUDE_PROJECT_DIR="$PROJECT_ROOT"

# stdin passthrough (see read-context-inject.sh for the R31 rationale).
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
    if command -v vco_hook_fast_session_id >/dev/null 2>&1; then
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
INJECT_TEXT=$(printf '%s' "$HOOK_STDIN" | "$VENV" "$ROUTER" grep 2>/dev/null || true)

# ONE envelope per invocation: the (optional) broken-install notice and the
# (optional) injection text share it — plain PreToolUse stdout is discarded,
# so the notice must ride the same additionalContext envelope.
_VCO_OUT=""
case "$INJECT_TEXT" in
    *[![:space:]]*) _VCO_OUT="[Grep context]:"$'\n'$'\n'"${INJECT_TEXT}" ;;
esac
if [ -n "$_VCO_INJECT_LIB_NOTICE" ]; then
    _VCO_OUT="${_VCO_INJECT_LIB_NOTICE}${_VCO_OUT:+$'\n'$'\n'$_VCO_OUT}"
fi
case "$_VCO_OUT" in
    *[![:space:]]*)
        if command -v emit_additional_context >/dev/null 2>&1; then
            emit_additional_context "$_VCO_OUT" PreToolUse
        fi
        ;;
esac
exit 0
