#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# read-context-inject.sh — v0.2.101 injection redesign (PLAN-V02101 §C2).
# PostToolUse(Read) wrapper: surfaces KG + exact-symbol code-graph context
# for the file the model just Read.
#
# WHY PostToolUse (not PreToolUse): the hook payload carries the Read's
# `tool_response` content, so the router builds the exact-symbol query from
# what was actually read (vco_lib.inject_intent.file_pub_symbols) instead of
# re-reading disk; the model sees the context one event later — no loss.
# This hook REPLACES pre-tool-use.sh's Read(code) code-graph branch (one
# concern, one home): the old branch ran a semantic-by-basename query under
# a 3 s settings timeout with a 4 s inner bound — the kickoff probe
# (2026-10-05) measured the CLI cold path at 4.7-11.6 s, so the branch was
# ALWAYS killed: 0 injections, plus the timed-out empty result poisoned the
# query cache for 900 s. The Build-Anchor + reads-ledger writes stay in
# pre-tool-use.sh (they must happen PreToolUse).
#
# The wrapper is THIN by design: stdin → the ONE router
# (claude_mcp_servers/scripts/hook_context_router.py, surface `read`) →
# emit_additional_context envelope. EVERY decision — intent, query
# formation (target path/symbols, never command text), §2.1 gates,
# seen-store dedupe, per-turn budget, RL events — lives in the router and
# vco_lib/inject_intent.py. MUST MATCH read-context-inject.ps1.
#
# Constraints:
# - Registered PostToolUse(Read), timeout 10 in settings.json.template —
#   ABOVE the router's 6 s inner budget so the router, not the harness,
#   is what bounds a cold run.
# - No `if` filter (§C2): Read frequency is modest and the code-vs-docs
#   split is a content decision, not a path glob.
# - Never exit non-zero (PostToolUse cannot block, but stay disciplined).
#   Empty/failed retrieval → exit 0 silently.

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

# Resolve the VCO venv (the router needs the weaviate stack + vco_lib) and
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
# (session_id / prompt_id / transcript_path / tool_input / tool_response —
# query text never travels through argv, R31 privacy discipline).
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
INJECT_TEXT=$(printf '%s' "$HOOK_STDIN" | "$VENV" "$ROUTER" read 2>/dev/null || true)

# ONE envelope per invocation: the (optional) broken-install notice and the
# (optional) injection text share it — plain PostToolUse/PreToolUse stdout is
# discarded, so the notice must ride the same additionalContext envelope.
_VCO_OUT=""
case "$INJECT_TEXT" in
    *[![:space:]]*) _VCO_OUT="[Read context]:"$'\n'$'\n'"${INJECT_TEXT}" ;;
esac
if [ -n "$_VCO_INJECT_LIB_NOTICE" ]; then
    _VCO_OUT="${_VCO_INJECT_LIB_NOTICE}${_VCO_OUT:+$'\n'$'\n'$_VCO_OUT}"
fi
case "$_VCO_OUT" in
    *[![:space:]]*)
        if command -v emit_additional_context >/dev/null 2>&1; then
            emit_additional_context "$_VCO_OUT" PostToolUse
        fi
        ;;
esac
exit 0
