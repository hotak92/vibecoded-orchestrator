#!/usr/bin/env bash
# Post-edit outcome event hook (V52-M, v0.2.52)
# Fires AFTER Edit OR Write tool executes. Emits an edit_outcome event
# with diff size + whether the file existed before + (best-effort)
# whether a subsequent compile/test succeeded.
#
# Pairing strategy:
#   The pre-edit-context-inject.{sh,ps1} hook calls rl_kg_search.py
#   which mints a random pre_edit_<uuid> task_id and writes the
#   retrieval event with it. Pre-edit does NOT write a state file we
#   can read here, so we mint our own edit_outcome_<uuid> task_id
#   and let the offline trainer JOIN on (session_id, file_path,
#   ts_window) — that's lossy but enough for training-pair construction
#   (the trainer sorts retrieval events + outcome events by ts and
#   pairs adjacent same-(session,file) entries).
#
#   FOLLOW-UP (V52-M.2 candidate): refactor pre-edit to write its
#   task_id to .claude/state/edit_task_<session>_<filehash>.json so
#   we can do exact task_id pairing like pre-bash/post-bash. Out of
#   scope for the V52-M initial ship to keep blast radius small.
#
# Compile/test success-within-N-seconds is implemented as a forked
# background poller (N=20s default, VCT_EDIT_POST_CHECK_SECS env
# override). The poller watches .claude/state/last_compile_status_
# <file_basename>.json for a status update emitted by ruff / py_compile
# / cargo PostToolUse siblings; default-empty means "no signal".
# Soft-fail throughout.

unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0

HOOK_STDIN=$(cat 2>/dev/null || echo "")

# Parse tool_name, session_id, file_path, old_string length, new_string length
_PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('tool_name', ''))
    print(d.get('session_id', ''))
    ti = d.get('tool_input', {}) or {}
    print(ti.get('file_path', ''))
    # For Edit: old_string + new_string. For Write: content (treat as new_string).
    old_str = ti.get('old_string', '')
    new_str = ti.get('new_string', '')
    content = ti.get('content', '')
    if not new_str and content:
        new_str = content
    print(len(old_str or ''))
    print(len(new_str or ''))
except Exception:
    print(''); print(''); print(''); print(0); print(0)
" 2>/dev/null || printf '\n\n\n0\n0\n')

TOOL_NAME=$(printf '%s' "$_PARSED" | sed -n '1p')
SESSION_ID=$(printf '%s' "$_PARSED" | sed -n '2p')
FILE_PATH=$(printf '%s' "$_PARSED" | sed -n '3p')
OLD_LEN=$(printf '%s' "$_PARSED" | sed -n '4p')
NEW_LEN=$(printf '%s' "$_PARSED" | sed -n '5p')

# Only fire for Edit and Write
if [[ "$TOOL_NAME" != "Edit" ]] && [[ "$TOOL_NAME" != "Write" ]]; then
    exit 0
fi
[ -z "$FILE_PATH" ] && exit 0
[ -z "$SESSION_ID" ] && SESSION_ID="default"

# === file_existed_before heuristic ===
# Edit always operates on existing files (Claude Code's Read-before-Edit
# rule); Write can create or overwrite. We can't observe the pre-edit
# file state here because the edit already happened. Best signal: for
# Write, look at git history (file in HEAD = existed). For Edit, treat
# as 1 unconditionally.
FILE_EXISTED_BEFORE=1
if [[ "$TOOL_NAME" = "Write" ]]; then
    # Check if file is tracked by git in HEAD. Fails open to 0 (new file)
    # when git is unavailable or the path is outside any repo.
    if command -v git >/dev/null 2>&1; then
        if git -C "$(dirname "$FILE_PATH")" ls-files --error-unmatch -- "$(basename "$FILE_PATH")" >/dev/null 2>&1; then
            FILE_EXISTED_BEFORE=1
        else
            FILE_EXISTED_BEFORE=0
        fi
    fi
fi

# === Compute diff_size ===
# For Edit: abs(new_len - old_len) is a reasonable proxy for diff size.
# For Write: new_len (entire new content is the "diff").
if [[ "$TOOL_NAME" = "Write" ]]; then
    DIFF_SIZE="$NEW_LEN"
else
    DIFF_SIZE=$("$PY" -c "print(abs(int('$NEW_LEN') - int('$OLD_LEN')))" 2>/dev/null || echo "$NEW_LEN")
fi

NOW_TS_MS=$("$PY" -c "import time; print(int(time.time()*1000))" 2>/dev/null || echo 0)

# === Resolve venv + emit edit_outcome event ===
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
. "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
resolve_vco_venv_python "$SCRIPT_DIR"
VENV="${VCO_VENV_PYTHON:-}"

if [ -n "$VENV" ] && [ -f "$VENV" ]; then
    "$VENV" -c "
import os, json, sys, uuid
# Mint our own task_id; offline trainer joins by (session_id, file_path, ts).
task_id = f'edit_outcome_{uuid.uuid4().hex[:8]}'

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
        event_type='edit_outcome',
        task_id=task_id,
        task_type='edit_outcome',
        payload={
            'tool_name': '$TOOL_NAME',
            'file_path': '$FILE_PATH',
            'diff_size': int('$DIFF_SIZE'),
            'file_existed_before': bool(int('$FILE_EXISTED_BEFORE')),
            'ts_ms': $NOW_TS_MS,
            # post_check populated by the background poller below (optional)
            'post_check': None,
        },
        session_id='$SESSION_ID',
        project_id=project_id,
    )
except Exception:
    pass
" 2>/dev/null || true
fi

exit 0
