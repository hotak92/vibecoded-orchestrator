#!/usr/bin/env bash
# Post-bash context record hook (V52-M, v0.2.52)
# Fires AFTER Bash tool executes. Emits a bash_outcome event with
# exit code + output length + duration, paired via task_id with the
# pre-bash-context-inject.sh state file.
#
# Pairing protocol (see pre-bash-context-inject.sh for the write side):
#   - Re-hash the command from this hook's stdin to derive cmd_hash.
#   - Read .claude/state/bash_task_<session>_<cmdhash>.json.
#   - If absent → pre-bash didn't fire (command was below the 500-char
#     threshold OR pre-bash crashed); skip silently.
#   - If present → emit bash_outcome with the same task_id, then delete
#     the state file (one-shot pairing).
#
# Constraints:
#   - PostToolUse hooks' plain stdout is discarded by the harness; this
#     hook never tries to inject context. It only writes telemetry.
#   - Always exit 0 (PostToolUse can't block).
#   - Soft-fail on every subprocess / venv resolution issue.

unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0

HOOK_STDIN=$(cat 2>/dev/null || echo "")

# Parse tool_name, session_id, tool_input.command, tool_response in one Python call
_PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('tool_name', ''))
    print(d.get('session_id', ''))
    ti = d.get('tool_input', {}) or {}
    print(ti.get('command', ''))
    tr = d.get('tool_response', '')
    # tool_response can be a string or object — coerce to string for length
    if isinstance(tr, (dict, list)):
        tr = json.dumps(tr)
    elif tr is None:
        tr = ''
    else:
        tr = str(tr)
    # Newline-separator hostile to commands containing newlines; b64 wrap
    import base64
    print(base64.b64encode((tr or '').encode('utf-8','replace')).decode('ascii'))
except Exception:
    print(''); print(''); print(''); print('')
" 2>/dev/null || printf '\n\n\n\n')

TOOL_NAME=$(printf '%s' "$_PARSED" | sed -n '1p')
SESSION_ID=$(printf '%s' "$_PARSED" | sed -n '2p')
COMMAND=$(printf '%s' "$_PARSED" | sed -n '3p')
TR_B64=$(printf '%s' "$_PARSED" | sed -n '4p')

[ "$TOOL_NAME" != "Bash" ] && exit 0
[ -z "$SESSION_ID" ] && SESSION_ID="default"

# Re-derive cmd_hash from the command (must match pre-bash's hash exactly)
[ -z "$COMMAND" ] && exit 0
CMD_HASH=$(printf '%s' "$COMMAND" | "$PY" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest()[:16])" 2>/dev/null)
if [ -z "$CMD_HASH" ]; then
    CMD_HASH=$(printf '%s' "$COMMAND" | tr '/' '_' | tr -cd '[:alnum:]_' | head -c 32)
fi

STATE_DIR="$PROJECT_ROOT/.claude/state"
STATE_FILE="$STATE_DIR/bash_task_${SESSION_ID}_${CMD_HASH}.json"

# If pre-bash didn't fire (below threshold) → no state file → skip silently.
[ ! -f "$STATE_FILE" ] && exit 0

# === Decode output length + compute duration + extract task_id ===
# Output length is the byte length of tool_response (string-coerced).
# exit_code is harder — Bash tool_response doesn't expose it explicitly,
# but Claude Code wraps errors with a tool-side <error/> marker. As a
# heuristic, treat presence of an error marker as exit != 0; otherwise 0.
OUTPUT_LEN=$(printf '%s' "$TR_B64" | "$PY" -c "
import sys, base64
b = base64.b64decode(sys.stdin.read().encode('ascii'))
print(len(b))
" 2>/dev/null || echo 0)

EXIT_CODE=$(printf '%s' "$TR_B64" | "$PY" -c "
import sys, base64
b = base64.b64decode(sys.stdin.read().encode('ascii')).decode('utf-8','replace')
# Heuristic: Claude Code surfaces bash failures as 'Error:' / '<error>'
# prefixes in tool_response. Best-effort signal for offline RL — the
# trainer reads it as 'likely failed' not 'definitively failed'.
if b.startswith('<tool_use_error>') or b.startswith('Error:') or 'Command exited with' in b[:200]:
    print(1)
else:
    print(0)
" 2>/dev/null || echo 0)

# Read state file for task_id + start_ts_ms
STATE_JSON=$(cat "$STATE_FILE" 2>/dev/null || echo "{}")
END_TS_MS=$("$PY" -c "import time; print(int(time.time()*1000))" 2>/dev/null || echo 0)

# === Resolve venv + emit bash_outcome event ===
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"

if [ -n "$VENV" ] && [ -f "$VENV" ]; then
    "$VENV" -c "
import json, os, sys
state = json.loads('''$STATE_JSON''')
task_id = state.get('task_id', '')
start_ts_ms = int(state.get('start_ts_ms', 0))
end_ts_ms = $END_TS_MS
duration_ms = max(0, end_ts_ms - start_ts_ms)

# Resolve project_id + writer (soft-fail if helpers unavailable)
project_id = None
try:
    from vco_lib.project_config import resolve_for_project
    cfg = resolve_for_project(os.environ.get('CLAUDE_PROJECT_DIR', '$PROJECT_ROOT'))
    project_id = cfg.get('project_id') if isinstance(cfg, dict) else None
except Exception:
    pass

try:
    from claude_mcp_servers.rl_client.outcome_emit import emit_outcome_event
    emit_outcome_event(
        event_type='bash_outcome',
        task_id=task_id,
        task_type='bash_outcome',
        payload={
            'exit_code': int($EXIT_CODE),
            'output_len': int($OUTPUT_LEN),
            'duration_ms': duration_ms,
            'cmd_len': int(state.get('cmd_len', 0)),
        },
        session_id=state.get('session_id', '$SESSION_ID'),
        project_id=project_id,
    )
except Exception as exc:
    # Soft-fail — telemetry must not break the user's bash flow
    pass
" 2>/dev/null || true
fi

# === One-shot pairing: delete the state file ===
rm -f "$STATE_FILE" 2>/dev/null || true

exit 0
