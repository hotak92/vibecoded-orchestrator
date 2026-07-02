#!/usr/bin/env bash
# Pre-bash context injection hook (V52-M, v0.2.52)
# Fires BEFORE Bash tool executes — when the command is >500 chars
# (configurable via VCT_BASH_KG_THRESHOLD_CHARS), injects KG context
# using the command itself as the query.
#
# Output goes to stdout as a PreToolUse JSON envelope →
# additionalContext the LLM sees before the bash runs.
#
# Pairs with post-bash-context-record.sh via a state file in
# .claude/state/bash_task_<session>_<cmdhash>.json containing
# {task_id, start_ts_ms, query, cmd_hash}. The post-hook reads this
# file, emits the bash_outcome event with the SAME task_id (so
# offline RL training can pair retrieval → outcome), then deletes it.
#
# Constraints:
#   - Must complete in <3 seconds (timeout in settings.json)
#   - Below threshold → silent skip (no KG noise on `ls` / `cd` / `git status`)
#   - Never exit non-zero (would block the bash). Always exit 0.
#   - If searches fail or return empty → exit 0 silently

# Scrub sensitive env vars (this hook doesn't need credentials)
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ]; then
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0

# v0.2.70 Streams C+D+E: shared helpers for canonical session-id, the unified
# seen-store dedup (pre-bash injected KG BLIND before this), and the gated
# code-graph branch added below. Sourced only if present (partial-install
# tolerance); the new logic no-ops gracefully when a helper is missing.
# shellcheck source=_lib/session-id.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/session-id.sh" ] && . "$SCRIPT_DIR/_lib/session-id.sh"
# shellcheck source=_lib/seen-store.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/seen-store.sh" ] && . "$SCRIPT_DIR/_lib/seen-store.sh"
# shellcheck source=_lib/codegraph-query.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/codegraph-query.sh" ] && . "$SCRIPT_DIR/_lib/codegraph-query.sh"
# shellcheck source=_lib/command-noise-strip.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/command-noise-strip.sh" ] && . "$SCRIPT_DIR/_lib/command-noise-strip.sh"

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
_PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('tool_name', ''))
    print(d.get('session_id', ''))
    print(json.dumps(d.get('tool_input', {})))
except Exception:
    print('')
    print('')
    print('{}')
" 2>/dev/null || printf '\n\n{}\n')
TOOL_NAME=$(printf '%s' "$_PARSED" | sed -n '1p')
SESSION_ID=$(printf '%s' "$_PARSED" | sed -n '2p')
TOOL_ARGS=$(printf '%s' "$_PARSED" | sed -n '3p')

# Only fire for Bash tool
if [[ "$TOOL_NAME" != "Bash" ]]; then
    exit 0
fi

# v0.2.70 Stream E: unify session-id via the shared helper (parse+sanitise),
# matching pre-edit / pre-tool-use / post-compact. SESSION_ID_RAW preserves the
# trustworthy-vs-untrustworthy distinction for the seen-store ("" / "default" →
# inject blind, no shared bucket). SESSION_ID keeps the "default" coercion for
# the bash-task pairing file path (not cross-session-bleed sensitive).
if command -v vco_hook_session_id >/dev/null 2>&1; then
    SESSION_ID="$(vco_hook_session_id "$HOOK_STDIN")"
fi
SESSION_ID_RAW="$SESSION_ID"
[ -z "$SESSION_ID" ] && SESSION_ID="default"

# V52-M: propagate session_id to child processes (rl_kg_search.py reads
# VCT_SESSION_ID as layer-2 of its 3-layer chain). Skip the "default"
# sentinel — we'd rather have empty than a fake-key cohort.
if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "default" ]; then
    export VCT_SESSION_ID="$SESSION_ID"
fi

# Extract the bash command from tool_input.command
COMMAND=$(printf '%s' "$TOOL_ARGS" | "$PY" -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(d.get('command', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

[ -z "$COMMAND" ] && exit 0

# === v0.2.70 Stream C Surface 2: gated code-graph injection on Bash ===
# Runs on EVERY Bash call BEFORE the (KG-only) 500-char threshold gate, because
# a short `grep -rn "migrate_collections"` should surface codegraph even though
# it's well under 500 chars. The gate (codegraph_bash_gate) is pure-bash and
# short-circuits with ZERO subprocess for routine ls/cd/git/cat/etc — the
# steady-state cost on a non-code command is one regex chain (~us). Only when the
# command genuinely navigates code do we spawn the (timeout-bounded) helper.
# Output is deduped through the SAME shared seen-store as pre-edit/pre-tool-use.
if command -v codegraph_bash_gate >/dev/null 2>&1 && codegraph_bash_gate "$COMMAND"; then
    _CG_SYM="$COMMAND"
    if command -v codegraph_extract_symbol >/dev/null 2>&1; then
        _CG_SYM="$(codegraph_extract_symbol "$COMMAND")"
    fi
    # v0.2.72 P2: the extracted symbol doubles as the --anchor (5th arg) so the
    # CLI's shared pipeline biases the rerank toward code call-linked to it.
    _CG_RAW="$(codegraph_query_block "$_CG_SYM" "" 2 "" "$_CG_SYM" 2>/dev/null || true)"
    if [ -n "$_CG_RAW" ]; then
        _CGB_INJECT=""
        _CGB_READS=""
        if command -v vco_seen_store_path >/dev/null 2>&1; then
            _CGB_INJECT="$(vco_seen_store_path inject "$SESSION_ID_RAW" "$PROJECT_ROOT")"
            _CGB_READS="$(vco_seen_store_path reads "$SESSION_ID_RAW" "$PROJECT_ROOT")"
        fi
        if command -v vco_filter_seen_blocks >/dev/null 2>&1; then
            _CG_RAW="$(vco_filter_seen_blocks "$_CG_RAW" "$_CGB_INJECT" "$_CGB_READS")"
        fi
        case "$_CG_RAW" in
            *[![:space:]]*)
                if command -v emit_additional_context >/dev/null 2>&1; then
                    emit_additional_context "[Code-graph context for symbol: ${_CG_SYM}]:"$'\n'$'\n'"$_CG_RAW" PreToolUse
                fi
                ;;
        esac
    fi
fi

# === Threshold gate ===
# User-locked answer to Q6 (2026-06-09): fixed 500 chars threshold,
# with VCT_BASH_KG_THRESHOLD_CHARS env override for power users who
# want to tune the noise/value ratio for their workflow.
THRESHOLD="${VCT_BASH_KG_THRESHOLD_CHARS:-500}"
CMD_LEN=${#COMMAND}
if [ "$CMD_LEN" -lt "$THRESHOLD" ]; then
    exit 0
fi

# === Compute deterministic cmd hash for state-file pairing ===
# md5 of the command — stable enough that post-bash can re-derive it
# from the same stdin and find our state file. Python hashlib for
# portability (md5sum is GNU-only).
CMD_HASH=$(printf '%s' "$COMMAND" | "$PY" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest()[:16])" 2>/dev/null)
if [ -z "$CMD_HASH" ]; then
    # Fallback: sanitized prefix (no slashes); degrades to weaker pairing
    CMD_HASH=$(printf '%s' "$COMMAND" | tr '/' '_' | tr -cd '[:alnum:]_' | head -c 32)
fi

# === Write pre-bash state file for post-bash to pair with ===
STATE_DIR="$PROJECT_ROOT/.claude/state"
mkdir -p "$STATE_DIR" 2>/dev/null || true
STATE_FILE="$STATE_DIR/bash_task_${SESSION_ID}_${CMD_HASH}.json"

# Generate task_id; same hex8 shape as rl_kg_search.py's pre_edit_* keys.
TASK_ID="pre_bash_$("$PY" -c "import uuid; print(uuid.uuid4().hex[:8])" 2>/dev/null)"
[ "$TASK_ID" = "pre_bash_" ] && TASK_ID="pre_bash_${CMD_HASH:0:8}"  # fallback

START_TS_MS=$("$PY" -c "import time; print(int(time.time()*1000))" 2>/dev/null || echo 0)

# Use Python to write JSON (avoid shell-escaping issues with command containing quotes/newlines)
"$PY" -c "
import json, sys, os
state = {
    'task_id': '$TASK_ID',
    'start_ts_ms': $START_TS_MS,
    'session_id': '$SESSION_ID',
    'cmd_hash': '$CMD_HASH',
    'cmd_len': $CMD_LEN,
}
try:
    with open('$STATE_FILE', 'w') as f:
        json.dump(state, f)
except Exception:
    pass
" 2>/dev/null || true

# v0.2.29 GC: prune state files older than 1 day. bash sessions are
# short; stale state files are evidence of a post-hook that never
# fired (crashes, kills, hung commands) — safe to drop.
find "$STATE_DIR" -maxdepth 1 -type f -name "bash_task_*.json" -mtime +1 -delete 2>/dev/null || true

# === Resolve venv for rl_kg_search.py subprocess ===
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"

# === Run KG search using command as query ===
# Truncate the query to ~500 chars so the embedding model isn't fed
# multi-kilobyte input (qwen3-embedding:0.6b needs num_ctx=8192 but
# longer queries dilute the semantic signal). The pre-edit hook caps
# new_string at 200 chars for the same reason; bash commands tend to
# have richer structure so we allow more headroom.
#
# v0.2.70 Stream D-3 (command-noise strip): the KG query is built from the raw
# bash command, which is mostly flags and paths. The noise-strip logic lives in
# the shared _lib/command-noise-strip.sh (ONE bash home — no inline copy here,
# none in the test). vco_strip_command_noise drops flags, paths (keeping a code-
# file basename), shell operators + bare cwd dots, so a bare `cd /some/dir` or
# `ls -la` yields little query signal instead of injecting directory-keyword KG.
# Falls back to the raw (capped) command if the strip leaves nothing OR the
# helper is missing (partial install).
QUERY_RAW=$(printf '%s' "$COMMAND" | head -c 500)
if command -v vco_strip_command_noise >/dev/null 2>&1; then
    QUERY="$(vco_strip_command_noise "$QUERY_RAW")"
else
    QUERY="$QUERY_RAW"
fi
# Strip fallback: if noise-removal emptied the query, use the raw command so a
# genuinely identifier-only long command still searches.
[ -z "$QUERY" ] && QUERY="$QUERY_RAW"

# === F-LOG (v0.2.70): emit the pre_bash pairing event ===
# The pre_bash event_type was declared in outcome_emit.OUTCOME_EVENT_TYPES but
# NEVER written — only the state file above was, so the offline trainer logged
# 0 pre_bash rows and (pre_bash, bash_outcome) training pairs were
# unconstructable. Emit it now with the SAME task_id post-bash will reuse, so
# the pair is JOINable by task_id. The query snippet is passed via env (not
# string-interpolated into the python source) so a command containing quotes/
# newlines can't break the emit. Soft-fail; backgrounded so it never delays the
# user's bash command.
if [ -n "$VENV" ] && [ -f "$VENV" ]; then
    ( VCT_PREBASH_QUERY=$(printf '%s' "$COMMAND" | head -c 120) \
      VCT_PREBASH_TASK_ID="$TASK_ID" \
      VCT_PREBASH_CMD_LEN="$CMD_LEN" \
      VCT_PREBASH_TS_MS="$START_TS_MS" \
      VCT_PREBASH_SESSION="$SESSION_ID" \
      "$VENV" -c "
import os
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
        },
        session_id=os.environ.get('VCT_PREBASH_SESSION', ''),
        project_id=project_id,
    )
except Exception:
    pass
" >/dev/null 2>&1 ) &
fi

KG_TMP=$(mktemp)
if [ -n "$VENV" ] && [ -f "$PROJECT_ROOT/claude_mcp_servers/scripts/rl_kg_search.py" ]; then
    ("$VENV" "$PROJECT_ROOT/claude_mcp_servers/scripts/rl_kg_search.py" "$QUERY" --limit 1 --hook-format 2>/dev/null \
        | head -40 > "$KG_TMP") &
    KG_PID=$!
    wait "$KG_PID" 2>/dev/null || true
fi

KG_RESULT=$(cat "$KG_TMP" 2>/dev/null || true)
rm -f "$KG_TMP"

# === v0.2.70 Stream E: dedup the KG result through the shared seen-store ===
# Before this, pre-bash injected KG BLIND — re-providing nodes pre-edit had
# already shown (and vice-versa). Now both injectors consult the SAME
# seen_inject_<sid>.txt (per-chunk KG keys) + reads ledger. Untrustworthy
# session id → helper returns empty store path → inject blind (no shared bucket).
if command -v vco_filter_seen_blocks >/dev/null 2>&1; then
    _PB_INJECT=""
    _PB_READS=""
    if command -v vco_seen_store_path >/dev/null 2>&1; then
        _PB_INJECT="$(vco_seen_store_path inject "$SESSION_ID_RAW" "$PROJECT_ROOT")"
        _PB_READS="$(vco_seen_store_path reads "$SESSION_ID_RAW" "$PROJECT_ROOT")"
    fi
    KG_RESULT="$(vco_filter_seen_blocks "$KG_RESULT" "$_PB_INJECT" "$_PB_READS")"
fi

# === Helper: emit context as PreToolUse JSON envelope ===
_emit_context_json() {
    if command -v emit_additional_context >/dev/null 2>&1; then
        emit_additional_context "$1" PreToolUse
    fi
}

# === Only output if we found something ===
case "$KG_RESULT" in
    *[![:space:]]*)
        # First line of the command for the header (truncate long pipelines)
        FIRST_LINE=$(printf '%s' "$COMMAND" | head -1 | head -c 80)
        OUTPUT="[Pre-bash context for: ${FIRST_LINE}]:"$'\n'$'\n'"${KG_RESULT}"$'\n'
        _emit_context_json "$OUTPUT"
        ;;
    *)
        # Empty KG result — still keep the state file (post-bash can
        # emit a degraded-mode bash_outcome) but don't inject anything.
        ;;
esac

exit 0
