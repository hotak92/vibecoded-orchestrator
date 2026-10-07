#!/usr/bin/env bash
# Post-bash context record hook (V52-M, v0.2.52)
# Fires AFTER Bash tool executes. Emits a bash_outcome event with
# exit code + output length + duration, paired via task_id with the
# pre-bash-context-inject.sh state file.
#
# Pairing protocol (see pre-bash-context-inject.sh for the write side):
#   - Re-hash the command from this hook's stdin to derive cmd_hash; the
#     state file is per CALL when the payload names it (tool_use_id —
#     v0.2.102 W3R-3): bash_task_<session>_<cmdhash>_<tuid>.json, falling
#     back to the legacy bash_task_<session>_<cmdhash>.json for payloads /
#     state files from before W3R-3.
#   - Claim the file ATOMICALLY (mv to a per-pid claim name) BEFORE the
#     emit: two concurrent post-bash spawns for one call used to both pass
#     the existence check and both emit a bash_outcome for one task_id
#     (W3R-3's duplicate-outcome half). The loser of the mv exits silently.
#   - If absent → pre-bash didn't fire (command was MECHANICAL / pre-bash
#     crashed); skip silently.
#   - If present → emit bash_outcome with the same task_id, then delete
#     the claim ONLY once the emit succeeded OR the loss is ledgered
#     (v0.2.102 W3R-4: a pre-POST emit failure records an `outcome_unsent`
#     line via vco_lib.rl_telemetry_loss — the ONE loss-ledger home — and
#     is never a silent delete; a loss we cannot even ledger leaves the
#     claim file for the 1-day GC, data intact).
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

# Parse tool_name, session_id, tool_input.command, tool_response,
# tool_use_id in one Python call
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
    print(d.get('tool_use_id', ''))
except Exception:
    print(''); print(''); print(''); print(''); print('')
" 2>/dev/null || printf '\n\n\n\n\n')

TOOL_NAME=$(printf '%s' "$_PARSED" | sed -n '1p')
SESSION_ID=$(printf '%s' "$_PARSED" | sed -n '2p')
COMMAND=$(printf '%s' "$_PARSED" | sed -n '3p')
TR_B64=$(printf '%s' "$_PARSED" | sed -n '4p')
TOOL_USE_ID_RAW=$(printf '%s' "$_PARSED" | sed -n '5p')

[ "$TOOL_NAME" != "Bash" ] && exit 0
[ -z "$SESSION_ID" ] && SESSION_ID="default"

# Re-derive cmd_hash from the command (must match pre-bash's hash exactly)
[ -z "$COMMAND" ] && exit 0
CMD_HASH=$(printf '%s' "$COMMAND" | "$PY" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest()[:16])" 2>/dev/null)
if [ -z "$CMD_HASH" ]; then
    CMD_HASH=$(printf '%s' "$COMMAND" | tr '/' '_' | tr -cd '[:alnum:]_' | head -c 32)
fi

# W3R-3: same sanitize ladder as pre-bash + the router's bash_claim_path.
TUID=$(printf '%s' "$TOOL_USE_ID_RAW" | tr -cd 'A-Za-z0-9_-' | head -c 64)

STATE_DIR="$PROJECT_ROOT/.claude/state"
# Per-call name when the harness names the call; legacy name otherwise AND
# as the old-format fallback (a state file written by a pre-W3R-3 pre-bash
# within the 1-day GC window still pairs).
STATE_FILE="$STATE_DIR/bash_task_${SESSION_ID}_${CMD_HASH}.json"
if [ -n "$TUID" ] && [ -f "$STATE_DIR/bash_task_${SESSION_ID}_${CMD_HASH}_${TUID}.json" ]; then
    STATE_FILE="$STATE_DIR/bash_task_${SESSION_ID}_${CMD_HASH}_${TUID}.json"
fi

# If pre-bash didn't fire (MECHANICAL / crash) → no state file → skip silently.
[ ! -f "$STATE_FILE" ] && exit 0

# === W3R-3: atomic one-shot claim =====================================
# mv (rename) is atomic: of two concurrent spawns only one can move the
# file; the loser's mv fails and it exits (the winner records the one
# outcome this call gets). The claim name keeps the bash_task_*.json glob
# so the 1-day GC reclaims a crashed holder's claim.
CLAIM_FILE="${STATE_FILE%.json}.claim.$$.json"
if ! mv "$STATE_FILE" "$CLAIM_FILE" 2>/dev/null; then
    exit 0
fi

# === Decode output length + compute duration ==========================
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

END_TS_MS=$("$PY" -c "import time; print(int(time.time()*1000))" 2>/dev/null || echo 0)

# === W3R-4: hook-side loss ledger (the child ledger cannot run) =======
# Records `outcome_unsent` through vco_lib.rl_telemetry_loss (the ONE
# loss-ledger home) using ANY python ($PY) with the orchestrator checkout
# on PYTHONPATH. Best-effort: returns non-zero when no root resolves, and
# the caller then KEEPS the claim file (never a silent delete).
_record_outcome_loss() {
    # $1 = reason. The task_id is read from the claim file inside python.
    local _root
    for _root in "${VCT_INSTALL_ROOT:-}" "${VCT_ORCHESTRATOR_ROOT:-}"; do
        [ -n "$_root" ] || continue
        [ -d "$_root/vco_lib" ] || continue
        VCT_PB_CLAIM_FILE="$CLAIM_FILE" \
        VCT_PB_LOSS_REASON="$1" \
        PYTHONPATH="$_root${PYTHONPATH:+:$PYTHONPATH}" \
        "$PY" -c "
import json, os
task_id = ''
try:
    with open(os.environ.get('VCT_PB_CLAIM_FILE', ''), 'r') as fh:
        d = json.load(fh)
    if isinstance(d, dict):
        task_id = str(d.get('task_id', '') or '')
except Exception:
    pass
from vco_lib.rl_telemetry_loss import KIND_OUTCOME_UNSENT, record_loss
record_loss(KIND_OUTCOME_UNSENT, os.environ.get('VCT_PB_LOSS_REASON', ''),
            task_id=task_id, task_type='bash_outcome')
" 2>/dev/null && return 0
    done
    return 1
}

# === Resolve venv + emit bash_outcome event ===========================
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"

EMIT_RC=127
if [ -n "$VENV" ] && [ -f "$VENV" ]; then
    # Child contract (W3R-4): exit 0 = emit succeeded; exit 3 = the emit
    # could not run/complete pre-POST and the child already recorded an
    # `outcome_unsent` ledger line; anything else = the child itself
    # crashed (broken interpreter / traceback) — the hook ledgers below.
    VCT_PB_CLAIM_FILE="$CLAIM_FILE" "$VENV" -c "
import json, os, sys
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

def _loss(reason, task_id=''):
    # W3R-4: pre-POST emit failures are losses — record them in the ONE
    # loss ledger. Never raises into the hook.
    try:
        from vco_lib.rl_telemetry_loss import KIND_OUTCOME_UNSENT, record_loss
        record_loss(KIND_OUTCOME_UNSENT, reason, task_id=task_id,
                    task_type='bash_outcome')
    except Exception:
        pass

try:
    with open(os.environ['VCT_PB_CLAIM_FILE'], 'r') as f:
        state = json.load(f)
    if not isinstance(state, dict):
        raise ValueError('pairing state is not a JSON object')
except Exception:
    _loss('corrupt_state')
    sys.exit(3)
task_id = state.get('task_id', '')
start_ts_ms = int(state.get('start_ts_ms', 0))
end_ts_ms = $END_TS_MS
duration_ms = max(0, end_ts_ms - start_ts_ms)
if not task_id:
    _loss('missing_task_id')
    sys.exit(3)

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
except Exception:
    _loss('emit_import_failed', task_id)
    sys.exit(3)
try:
    ok = emit_outcome_event(
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
except Exception:
    # Soft-fail — telemetry must not break the user's bash flow — but the
    # loss is now VISIBLE (W3R-4), never a silent orphan.
    _loss('emit_raised', task_id)
    sys.exit(3)
if not ok:
    # False usually means the POST was attempted and hub_writer already
    # recorded hub_post_failed; it can also be a pre-POST soft-fail with no
    # line of its own. Record the pairing-side line either way — the kinds
    # count different facts (POST loss vs unsent outcome), and a possible
    # extra line is cheaper than an invisible loss.
    _loss('emit_returned_false', task_id)
    sys.exit(3)
sys.exit(0)
" 2>/dev/null
    EMIT_RC=$?
fi

# === One-shot pairing release: only after emit-success or a ledger line =
case "$EMIT_RC" in
    0|3)
        # 0 = emitted; 3 = the child ledgered the loss itself.
        rm -f "$CLAIM_FILE" 2>/dev/null || true
        ;;
    *)
        # Missing venv (127 sentinel) / crashed child: ledger from HERE,
        # then release; if even the ledger fails, KEEP the claim file —
        # deleting the pairing data with no emit and no evidence is the
        # silent loss W3R-4 exists to end (the 1-day GC reclaims it later).
        if [ "$EMIT_RC" = "127" ]; then
            _reason="venv_unavailable"
        else
            _reason="emit_child_failed"
        fi
        if _record_outcome_loss "$_reason"; then
            rm -f "$CLAIM_FILE" 2>/dev/null || true
        fi
        ;;
esac

exit 0
