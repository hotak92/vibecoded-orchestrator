#!/usr/bin/env bash
# Pre-bash context injection hook — THIN WRAPPER (v0.2.101 injection redesign,
# PLAN-V02101 §C1). Fires BEFORE the Bash tool executes.
#
#   stdin → hook_context_router.py bash --intent-out <state> --claim → emit envelope
#
# The ROUTER (claude_mcp_servers/scripts/hook_context_router.py) owns every
# retrieval decision: intent classification (READ / EDIT / SEARCH /
# MECHANICAL — MECHANICAL spawns no producer and injects nothing), query
# building from target paths/symbols (NEVER from command text), the §2.1
# noise gates, seen-store dedupe, the per-turn budget, the query cache and
# the RL retrieval events (via rl_kg_search --injection-profile --task-type).
#
# What this wrapper still owns (WP-D 2):
#   * the bash_task_<session>_<cmdhash>.json state file + the pre_bash
#     outcome event — now for every READ/EDIT/SEARCH-classified command
#     (UPSTREAM of the old 500-char gate: MORE events, richer labels), with
#     intent/targets/symbols added to both payloads (additive keys —
#     post-bash-context-record.sh pairing is UNCHANGED: same file name,
#     same task_id join).
#   * the emit_additional_context envelope around the router's text.
#
# One run per tool call (v0.2.101 N2): the §C1 if-group spawns this wrapper
# once per MATCHING rule, so a multi-match command (`cat x | grep y`) runs it
# twice, concurrently, for ONE call. `--claim` makes the router take an
# O_CREAT|O_EXCL claim per call before any side effect; the losing spawn's
# router exits 0 with no output and no intent file, so this wrapper injects
# nothing, writes no state and emits no event for it — silently, because it
# is a duplicate of a run already in flight, not a failure.
#
# RETIRED here (v0.2.101, owner-approved §C1): the 500-char threshold
# (VCT_BASH_KG_THRESHOLD_CHARS — classification replaces it; the knob no
# longer exists), the noise-strip query build, the inline code-graph bash
# gate branch (the whole _lib/codegraph-query lib was retired with this, its
# last caller), and the wrapper-side KG search/dedup (the router does both —
# dedupe through the SAME seen-store files, format unchanged).
#
# Kill switches: VCT_DISABLE_HOOKS (all hooks) and VCO_INJECT_PROFILE=off
# (injection surfaces only, via _lib/inject-budget.sh) — both checked BEFORE
# any spawn. A missing venv / router is a silent no-op (soft-fail: a broken
# install never blocks the user's Bash; the state file needs the router's
# classification, so it is not written in that case either).
#
# Constraints: never exit non-zero (would block the bash). Always exit 0.
# MUST MATCH pre-bash-context-inject.ps1.

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

# Canonical session-id (the router re-sanitizes from the payload itself; this
# is for the STATE FILE name, which post-bash-context-record re-derives).
# shellcheck source=_lib/session-id.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/session-id.sh" ] && . "$SCRIPT_DIR/_lib/session-id.sh"
# v0.2.101: the injection kill switch (VCO_INJECT_PROFILE=off).
# shellcheck source=_lib/inject-budget.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/inject-budget.sh" ] && . "$SCRIPT_DIR/_lib/inject-budget.sh"

if command -v vco_inject_profile_off >/dev/null 2>&1 && vco_inject_profile_off; then
    exit 0
fi

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec. The FULL
# payload is forwarded to the router untouched (it resolves session_id /
# prompt_id / transcript_path / cwd / tool_input itself — query text never
# travels through argv, R31 privacy discipline). This parse extracts only
# what the WRAPPER needs: the tool guard, the session id and the command
# (for the pairing hash). transcript_path stays a PATH the router threads to
# the producers; its contents are never read in this shell.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
_PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('tool_name', ''))
    print(d.get('session_id', ''))
    print((d.get('tool_input') or {}).get('command', ''))
except Exception:
    print('')
    print('')
    print('')
" 2>/dev/null || printf '\n\n\n')
TOOL_NAME=$(printf '%s' "$_PARSED" | sed -n '1p')
SESSION_ID=$(printf '%s' "$_PARSED" | sed -n '2p')
COMMAND=$(printf '%s' "$_PARSED" | sed -n '3p')

# Only fire for Bash tool
if [[ "$TOOL_NAME" != "Bash" ]]; then
    exit 0
fi
[ -z "$COMMAND" ] && exit 0

# v0.2.70 Stream E session discipline (unchanged): canonical sanitised id;
# "default" coercion kept for the pairing-file path (not cross-session-bleed
# sensitive — the router owns the bleed-guarded dedupe stores).
if command -v vco_hook_session_id >/dev/null 2>&1; then
    SESSION_ID="$(vco_hook_session_id "$HOOK_STDIN")"
fi
[ -z "$SESSION_ID" ] && SESSION_ID="default"

# V52-M: propagate session_id to child processes (the router's KG leg reads
# VCT_SESSION_ID as layer-2 of the telemetry 3-layer chain). Skip the
# "default" sentinel — we'd rather have empty than a fake-key cohort.
if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "default" ]; then
    export VCT_SESSION_ID="$SESSION_ID"
fi

# === Deterministic cmd hash for state-file pairing (UNCHANGED contract) ===
# md5 of the command, first 16 hex chars — post-bash-context-record.sh
# re-derives the SAME path from its own stdin (tests/test_v52_m_prepost_hooks.py
# pins the parity). Python hashlib for portability (md5sum is GNU-only).
CMD_HASH=$(printf '%s' "$COMMAND" | "$PY" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest()[:16])" 2>/dev/null)
if [ -z "$CMD_HASH" ]; then
    # Fallback: sanitized prefix (no slashes); degrades to weaker pairing
    CMD_HASH=$(printf '%s' "$COMMAND" | tr '/' '_' | tr -cd '[:alnum:]_' | head -c 32)
fi
CMD_LEN=${#COMMAND}

STATE_DIR="$PROJECT_ROOT/.claude/state"
mkdir -p "$STATE_DIR" 2>/dev/null || true
# The router's classification handoff (WP-D 2): written by --intent-out,
# read below, then removed. Named like the state file so the 1d GC covers it.
# Per SPAWN ($$, v0.2.101 N2): the two spawns of one multi-match call share
# session + hash, and a shared name let the winner's intent reach the loser
# (or the loser's rm delete it before the winner read it).
INTENT_FILE="$STATE_DIR/bash_intent_${SESSION_ID}_${CMD_HASH}_$$.json"

# === Resolve venv + the router (orchestrator-root script, v0.2.100 F3 discipline) ===
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"
if [ -z "$VENV" ] || [ ! -f "$VENV" ]; then
    # No VCO venv = broken/absent install: the router cannot run, and the
    # state file's intent gate depends on it — silent no-op (soft-fail).
    exit 0
fi
resolve_vco_orchestrator_script "$SCRIPT_DIR" "claude_mcp_servers/scripts/hook_context_router.py"
ROUTER="${VCO_ORCHESTRATOR_SCRIPT:-$PROJECT_ROOT/claude_mcp_servers/scripts/hook_context_router.py}"
if [ ! -f "$ROUTER" ]; then
    exit 0
fi
# Pin the CALLING project's identity for the router + producers (a no-op
# whenever the harness already set it): the script lives in the orchestrator
# root, so its own location must never be what names the project.
export CLAUDE_PROJECT_DIR="$PROJECT_ROOT"

# === Run the router (single interpreter; inner budget VCO_INJECT_BUDGET_S) ===
INJECT=$(printf '%s' "$HOOK_STDIN" | "$VENV" "$ROUTER" bash --intent-out "$INTENT_FILE" --claim 2>/dev/null || true)

# === Read the classification back (WP-D 2 gate for state + outcome) ===
_INT_PARSED=$(VCT_PB_INTENT_FILE="$INTENT_FILE" "$PY" -c "
import json, os
try:
    with open(os.environ.get('VCT_PB_INTENT_FILE', ''), encoding='utf-8') as fh:
        d = json.load(fh)
except Exception:
    d = {}
print(d.get('intent', '') or '')
print(json.dumps(d.get('targets', []) or []))
print(json.dumps(d.get('symbols', []) or []))
" 2>/dev/null || printf '\n[]\n[]')
INTENT=$(printf '%s' "$_INT_PARSED" | sed -n '1p')
TARGETS_JSON=$(printf '%s' "$_INT_PARSED" | sed -n '2p')
SYMBOLS_JSON=$(printf '%s' "$_INT_PARSED" | sed -n '3p')
rm -f "$INTENT_FILE" 2>/dev/null || true

# v0.2.29 GC (HK-4 accepted-scatter, deliberate 1d threshold — unchanged
# `-mtime +1` semantics): prune stale pairing state AND the intent handoffs.
find "$STATE_DIR" -maxdepth 1 -type f -name "bash_task_*.json" -mtime +1 -delete 2>/dev/null || true
find "$STATE_DIR" -maxdepth 1 -type f -name "bash_intent_*.json" -mtime +1 -delete 2>/dev/null || true

# === F-LOG (v0.2.70): emit the pre_bash pairing event ===
# SAME task_id post-bash-context-record reuses, so the (pre_bash,
# bash_outcome) pair stays JOINable. v0.2.101 WP-D 2: intent + extracted
# targets/symbols join the payload (richer training labels). Query snippet
# + payload travel via env, never string-interpolated into the python
# source. Soft-fail; backgrounded so it never delays the user's command.
# Kept as a function at top-level indent so the child-env block matches the
# pre-v0.2.101 shape the v0.2.94 root-handoff pin asserts.
_emit_prebash_outcome() {
    ( VCT_PREBASH_QUERY=$(printf '%s' "$COMMAND" | head -c 120) \
      VCT_PREBASH_TASK_ID="$TASK_ID" \
      VCT_PREBASH_CMD_LEN="$CMD_LEN" \
      VCT_PREBASH_TS_MS="$START_TS_MS" \
      VCT_PREBASH_SESSION="$SESSION_ID" \
      VCT_PREBASH_INTENT="$INTENT" \
      VCT_PREBASH_TARGETS="$TARGETS_JSON" \
      VCT_PREBASH_SYMBOLS="$SYMBOLS_JSON" \
      VCT_PROJECT_ROOT="$PROJECT_ROOT" \
      "$VENV" -c "
import os
# v0.2.94: pin the project root the hook already resolved for THIS child.
# A rootless embedding_service._detect_project_root() falls through to
# Path.cwd() (rule 4) and reconciles THAT directory's deferral ledger,
# rewriting its CLAUDE.md -- in a detached child, cwd is whatever the
# harness gave us, not the project. KG_BASE_DIR is rule 2 of the same
# ladder and already means the project folder path; setdefault, so an
# explicit VS Code / launcher value still wins.
_vco_project_root = r'''$PROJECT_ROOT'''
if _vco_project_root:
    os.environ.setdefault('KG_BASE_DIR', _vco_project_root)
try:
    from vco_lib.project_config import resolve_for_project
    cfg = resolve_for_project(os.environ.get('CLAUDE_PROJECT_DIR', os.environ.get('VCT_PROJECT_ROOT', '')))
    project_id = cfg.get('project_id') if isinstance(cfg, dict) else None
except Exception:
    project_id = None
def _int(name):
    try:
        return int(os.environ.get(name, '0') or '0')
    except (TypeError, ValueError):
        return 0
def _list(name):
    import json as _json
    try:
        v = _json.loads(os.environ.get(name, '[]') or '[]')
        return v if isinstance(v, list) else []
    except Exception:
        return []
try:
    from claude_mcp_servers.rl_client.outcome_emit import emit_outcome_event
    emit_outcome_event(
        event_type='pre_bash',
        task_id=os.environ.get('VCT_PREBASH_TASK_ID', ''),
        task_type='pre_bash',
        payload={
            'cmd_len': _int('VCT_PREBASH_CMD_LEN'),
            'query': os.environ.get('VCT_PREBASH_QUERY', ''),
            'ts_ms': _int('VCT_PREBASH_TS_MS'),
            'intent': os.environ.get('VCT_PREBASH_INTENT', ''),
            'targets': _list('VCT_PREBASH_TARGETS'),
            'symbols': _list('VCT_PREBASH_SYMBOLS'),
        },
        session_id=os.environ.get('VCT_PREBASH_SESSION', ''),
        project_id=project_id,
    )
except Exception:
    pass
" >/dev/null 2>&1 ) &
}

# === State file + pre_bash outcome event: READ/EDIT/SEARCH only ===
# MECHANICAL commands get NO pairing state and NO outcome event (WP-D 2:
# the intent gate replaces the 500-char gate — more events than before on
# short classified commands, none on routine noise).
case "$INTENT" in
    READ|EDIT|SEARCH)
        STATE_FILE="$STATE_DIR/bash_task_${SESSION_ID}_${CMD_HASH}.json"
        # Multi-match double spawns (wave-2 review nit-5) cannot reach here
        # twice: only the spawn that won the router's --claim gets an intent
        # (v0.2.101 N2 — the pre-N2 `find -mmin -1` guard on this file was a
        # check-then-write that two cold spawns could both pass).
        # task_id: same hex8 shape as rl_kg_search.py's pre_bash_* keys.
        TASK_ID="pre_bash_$("$PY" -c "import uuid; print(uuid.uuid4().hex[:8])" 2>/dev/null)"
        [ "$TASK_ID" = "pre_bash_" ] && TASK_ID="pre_bash_${CMD_HASH:0:8}"  # fallback
        START_TS_MS=$("$PY" -c "import time; print(int(time.time()*1000))" 2>/dev/null || echo 0)

        # JSON written by Python (env-passed fields — a command with quotes/
        # newlines can never break the emit). Same core fields as before
        # (post-bash pairing) + the WP-D 2 additions.
        VCT_PB_STATE_FILE="$STATE_FILE" \
        VCT_PB_TASK_ID="$TASK_ID" \
        VCT_PB_START_TS="$START_TS_MS" \
        VCT_PB_SESSION="$SESSION_ID" \
        VCT_PB_HASH="$CMD_HASH" \
        VCT_PB_LEN="$CMD_LEN" \
        VCT_PB_INTENT="$INTENT" \
        VCT_PB_TARGETS="$TARGETS_JSON" \
        VCT_PB_SYMBOLS="$SYMBOLS_JSON" \
        "$PY" -c "
import json, os
def _int(name):
    try:
        return int(os.environ.get(name, '0') or '0')
    except (TypeError, ValueError):
        return 0
def _list(name):
    try:
        v = json.loads(os.environ.get(name, '[]') or '[]')
        return v if isinstance(v, list) else []
    except Exception:
        return []
state = {
    'task_id': os.environ.get('VCT_PB_TASK_ID', ''),
    'start_ts_ms': _int('VCT_PB_START_TS'),
    'session_id': os.environ.get('VCT_PB_SESSION', ''),
    'cmd_hash': os.environ.get('VCT_PB_HASH', ''),
    'cmd_len': _int('VCT_PB_LEN'),
    'intent': os.environ.get('VCT_PB_INTENT', ''),
    'targets': _list('VCT_PB_TARGETS'),
    'symbols': _list('VCT_PB_SYMBOLS'),
}
try:
    with open(os.environ.get('VCT_PB_STATE_FILE', ''), 'w') as f:
        json.dump(state, f)
except Exception:
    pass
" 2>/dev/null || true

        _emit_prebash_outcome
        ;;
esac

# === Emit the router's injection text (if any) ===
case "$INJECT" in
    *[![:space:]]*)
        if command -v emit_additional_context >/dev/null 2>&1; then
            # First line of the command for the header (truncate long pipelines)
            FIRST_LINE=$(printf '%s' "$COMMAND" | head -1 | head -c 80)
            emit_additional_context "[Pre-bash context for: ${FIRST_LINE}]:"$'\n'$'\n'"${INJECT}" PreToolUse
        fi
        ;;
esac

exit 0
