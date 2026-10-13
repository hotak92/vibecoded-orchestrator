#!/usr/bin/env bash
# Parity: kept in step with post-edit-outcome.ps1 (both siblings' Unix-epoch
# stamps are pinned by tests/test_v0292_ps1_unix_epoch_parity.py; every
# behaviour must match the post-edit-outcome.ps1 sibling).
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
#   pairs adjacent same-(session,file) entries). v0.2.102: rows also carry
#   `agent_id` ("" = main chat) — a subagent's session_id is the parent's,
#   so the join must include it to keep parallel lanes' rows apart.
#
#   Pairing is by that time window, not by an exact task_id handoff:
#   unlike pre-bash/post-bash, the pre-edit/pre-write context router
#   serves many edits from its query cache and warm replay without
#   minting a retrieval task_id, so there is no id to hand over for
#   those edits. The window join covers every edit the same way.
#
# Compile success-within-N-seconds (v0.2.102 W6-PCHK; the ONE home is
# vco_lib/edit_post_check.py). The PostToolUse checkers write a status
# record per check to .claude/state/last_compile_status_<pathkey>_
# <agentkey>.json: <pathkey> hashes the ABSOLUTE path, so same-named
# files in different directories never collide, and <agentkey> keeps a
# subagent's check apart from the main chat's. The only shipped checker
# is py-compile-check (Write of a *.py file); there is no ruff, cargo or
# tsc PostToolUse checker. When a checker covers the edited file, this
# hook writes the row as a pending record and starts a DETACHED poller
# that waits up to VCT_EDIT_POST_CHECK_SECS (default 20) for a status
# newer than the edit, then emits the row once with post_check filled
# (ok / fail / no_signal on timeout). A file no checker covers is
# emitted at once with post_check no_signal, no wait. The row is never
# dropped: a poller that cannot start or fails leaves post_check None,
# and a poller that dies outright is swept by the next run of this hook.
# Soft-fail throughout.

unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
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
    print(d.get('agent_id', '') or '')
except Exception:
    print(''); print(''); print(''); print(0); print(0); print('')
" 2>/dev/null || printf '\n\n\n0\n0\n\n')

TOOL_NAME=$(printf '%s' "$_PARSED" | sed -n '1p')
SESSION_ID=$(printf '%s' "$_PARSED" | sed -n '2p')
FILE_PATH=$(printf '%s' "$_PARSED" | sed -n '3p')
OLD_LEN=$(printf '%s' "$_PARSED" | sed -n '4p')
NEW_LEN=$(printf '%s' "$_PARSED" | sed -n '5p')
# v0.2.102 W6-C2 (census F13): the subagent that made the edit. Subagent tool
# events carry agent_id while session_id stays the PARENT's, so the offline
# join on (session_id, file_path, ts) mixed the parent's rows with every
# lane's that edited the same file. Sanitised by the ONE agent-id rule
# (_lib/session-id.sh): hex ids pass, anything else -> "default"; empty = the
# main chat. Interpolated below only after that allow-list.
AGENT_ID_RAW=$(printf '%s' "$_PARSED" | sed -n '6p')
AGENT_ID=""
if [ -n "$AGENT_ID_RAW" ] && [ -f "$SCRIPT_DIR/_lib/session-id.sh" ]; then
    # shellcheck source=_lib/session-id.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/session-id.sh"
    AGENT_ID="$(vco_hook_sanitize_agent_id "$AGENT_ID_RAW")"
fi

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
    # v0.2.102 W6-PCHK: the fields travel as key=value argv items, never
    # interpolated into the Python source (a quote in a file path used to
    # break the snippet). The row itself, the poller and the sweep live in
    # vco_lib/edit_post_check.py (MUST MATCH the .ps1 sibling's call).
    "$VENV" -c "
import os, sys
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
project_id = None
try:
    from vco_lib.project_config import resolve_for_project
    cfg = resolve_for_project(os.environ.get('CLAUDE_PROJECT_DIR', r'''$PROJECT_ROOT'''))
    project_id = cfg.get('project_id') if isinstance(cfg, dict) else None
except Exception:
    pass
try:
    from claude_mcp_servers.rl_client.outcome_emit import emit_outcome_event
    from vco_lib.edit_post_check import record_edit_outcome_from_argv
except Exception as exc:
    sys.stderr.write('post-edit-outcome: edit_outcome not recorded, VCO install is broken: %r\n' % (exc,))
else:
    try:
        record_edit_outcome_from_argv(sys.argv[1:], project_root=_vco_project_root, emit=emit_outcome_event, project_id=project_id)
    except Exception as exc:
        sys.stderr.write('post-edit-outcome: edit_outcome failed: %r\n' % (exc,))
" "tool=$TOOL_NAME" "file=$FILE_PATH" "diff=$DIFF_SIZE" \
      "existed=$FILE_EXISTED_BEFORE" "ts=$NOW_TS_MS" "agent=$AGENT_ID" \
      "session=$SESSION_ID" 2>/dev/null || true
fi

exit 0
