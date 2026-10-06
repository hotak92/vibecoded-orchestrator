#!/usr/bin/env bash
# Pre-write context injection hook (v0.2.101 injection redesign,
# PLAN-V02101 §C3) — THIN WRAPPER. Fires BEFORE the Write tool executes.
#
#   stdin → hook_context_router.py write → emit envelope
#
# The ROUTER owns everything retrieval-side: KG titles keyed on module name
# + sibling dir (the path topic — never the file content), the write-profile
# §2.1 floor (0.65, titles-only below 0.85), an exact code-graph def+callers
# leg ONLY when the path is a REWRITE of an existing file (a brand-new
# file's symbols are not indexed yet — querying them would return nothing
# or, worse, a same-named stranger), seen-store dedupe, the per-turn budget,
# the query cache and the RL retrieval event (pre_write_kg_search).
#
# No per-file replay cache here (unlike pre-edit): a Write is a whole-file
# event and the router's own kgi/cgi query cache already serves repeats at
# ~ms; a second wrapper cache layer would only add staleness.
#
# Registration: PreToolUse(Write), beside the Edit entry, timeout 10
# (SF-1: above the router's 6 s inner budget)
# (settings templates are owned by the Wave-2 HOOKS-READ-AGENT lane).
#
# Constraints: never exit non-zero (would block the write); always exit 0;
# missing venv/router → silent no-op; kill switches VCT_DISABLE_HOOKS and
# VCO_INJECT_PROFILE=off checked BEFORE any spawn.
# MUST MATCH pre-write-context-inject.ps1.

# Scrub sensitive env vars (this hook doesn't need credentials)
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# VCO-CENTRALIZED-KG: read-side delegator (PR #171 / 0.1.7). v0.2.101: the
#   delegate is claude_mcp_servers/scripts/hook_context_router.py (KG +
#   code-graph legs loaded in-process, both access-aware via the weaviate_mcp
#   server helpers reading VCT_KG_ACCESS_LIST / VCT_CODE_GRAPH_ACCESS_LIST).
#   This hook does NOT query Weaviate directly; env propagates by subprocess
#   inheritance. See tests/test_kg_access_list.py for the consumer contract.

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ]; then
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0

# shellcheck source=_lib/session-id.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/session-id.sh" ] && . "$SCRIPT_DIR/_lib/session-id.sh"
# v0.2.101: the injection kill switch (VCO_INJECT_PROFILE=off).
# shellcheck source=_lib/inject-budget.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/inject-budget.sh" ] && . "$SCRIPT_DIR/_lib/inject-budget.sh"

if command -v vco_inject_profile_off >/dev/null 2>&1 && vco_inject_profile_off; then
    exit 0
fi

# Hook input arrives as JSON on stdin. The FULL payload is forwarded to the
# router untouched (the Write content never travels through argv — R31
# privacy discipline); this parse extracts only the WRAPPER's fields.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
_PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('tool_name', ''))
    print(d.get('session_id', ''))
    print((d.get('tool_input') or {}).get('file_path', ''))
except Exception:
    print('')
    print('')
    print('')
" 2>/dev/null || printf '\n\n\n')
TOOL_NAME=$(printf '%s' "$_PARSED" | sed -n '1p')
SESSION_ID=$(printf '%s' "$_PARSED" | sed -n '2p')
FILE_PATH=$(printf '%s' "$_PARSED" | sed -n '3p')

# Only fire for Write tool
if [[ "$TOOL_NAME" != "Write" ]]; then
    exit 0
fi
[[ -z "$FILE_PATH" ]] && exit 0

if command -v vco_hook_session_id >/dev/null 2>&1; then
    SESSION_ID="$(vco_hook_session_id "$HOOK_STDIN")"
fi
[ -z "$SESSION_ID" ] && SESSION_ID="default"

# WP-D 1 export discipline: session attribution for the router's KG leg.
if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "default" ]; then
    export VCT_SESSION_ID="$SESSION_ID"
fi

# === Resolve venv + the router (orchestrator-root script, F3 discipline) ===
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"
if [ -z "$VENV" ] || [ ! -f "$VENV" ]; then
    exit 0
fi
resolve_vco_orchestrator_script "$SCRIPT_DIR" "claude_mcp_servers/scripts/hook_context_router.py"
ROUTER="${VCO_ORCHESTRATOR_SCRIPT:-$PROJECT_ROOT/claude_mcp_servers/scripts/hook_context_router.py}"
if [ ! -f "$ROUTER" ]; then
    exit 0
fi
export CLAUDE_PROJECT_DIR="$PROJECT_ROOT"

# === Run the router ===
INJECT=$(printf '%s' "$HOOK_STDIN" | "$VENV" "$ROUTER" write 2>/dev/null || true)

# === Only output if we found something ===
case "$INJECT" in
    *[![:space:]]*)
        if command -v emit_additional_context >/dev/null 2>&1; then
            BASENAME=$(basename "$FILE_PATH")
            emit_additional_context "[Pre-write context for ${BASENAME}]:"$'\n'$'\n'"${INJECT}" PreToolUse
        fi
        ;;
esac

exit 0
