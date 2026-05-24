#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# pre-diagram-path-validation.sh — PreToolUse guard for Phase 1.5
# diagrams integration.
#
# Defense-in-depth: the wrapper MCP (Phase 1.2) ALSO validates the
# scoped-path rule, but this hook catches direct Write/Edit tool calls
# that bypass the MCP entirely (e.g. Claude calling `Write` instead of
# `save_diagram`).
#
# Hook contract (Claude Code v2.1.x):
#   - Reads tool_input JSON from stdin.
#   - If the tool_input.file_path is under `.claude/diagrams/` AND
#     violates the scoped-path rule, prints the corrective message to
#     stderr and exits 2 (BLOCKS the write).
#   - Otherwise exits 0 silently (allows the write).
#
# Matcher (in settings.json):
#   Write(.claude/diagrams/**)|Edit(.claude/diagrams/**)
#
# Bypass: VCT_DISABLE_HOOKS=1 in the shell, or remove from settings.json.

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# Resolve Python portably — bare `python3` is missing on Windows.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # No Python available — fail-open silently

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args are EMPTY; do NOT rely on $CLAUDE_TOOL_NAME etc.
HOOK_STDIN=$(cat 2>/dev/null || echo "")

# Parse tool_input.file_path in a single Python call.
FILE_PATH=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    ti = d.get('tool_input', {}) or {}
    # Edit and Write both use 'file_path' as the key per the v2.1.x
    # tool_input contract. Some legacy tools used 'path' — accept either.
    print(ti.get('file_path') or ti.get('path') or '')
except Exception:
    print('')
" 2>/dev/null || echo "")

# No path → nothing to validate; allow the call.
[ -z "$FILE_PATH" ] && exit 0

# Only validate paths under .claude/diagrams/. The matcher in
# settings.json already filters by glob; this is a belt-and-suspenders
# check in case the matcher fires on a sibling pattern.
case "$FILE_PATH" in
    *.claude/diagrams/*) ;;
    *) exit 0 ;;
esac

# Resolve venv-Python so we can `import vco_lib.diagram_paths` even
# when system Python lacks the orchestrator clone on sys.path. Mirrors
# the resolution chain in pre-edit-context-inject.sh (PR-25 / v0.2.12).
_VENV_BASE="${VCT_INSTALL_ROOT:-$PROJECT_ROOT}"
VENV=""
if [ -n "${VCT_VENV:-}" ] && [ -x "$VCT_VENV/bin/python" ]; then
    VENV="$VCT_VENV/bin/python"
elif [ -n "${VCT_VENV:-}" ] && [ -x "$VCT_VENV/Scripts/python.exe" ]; then
    VENV="$VCT_VENV/Scripts/python.exe"
fi
if [ -z "$VENV" ]; then
    for _cand in \
        "$_VENV_BASE/.venv/bin/python" \
        "$_VENV_BASE/.venv/Scripts/python.exe" \
        "$_VENV_BASE/claude_mcp_servers/.venv/bin/python" \
        "$_VENV_BASE/claude_mcp_servers/.venv/Scripts/python.exe"; do
        if [ -x "$_cand" ]; then
            VENV="$_cand"
            break
        fi
    done
fi
[ -z "$VENV" ] && VENV="$PY"

# Validate via the canonical CLI. The CLI prints the corrective
# message on stderr and exits 2 on violation — exactly the contract
# the Claude Code PreToolUse hook spec expects to block a write.
#
# We DELIBERATELY do NOT add `|| true` here: the validator's exit code
# IS the hook's exit code. Exit 0 → allow, exit 2 → block, other
# non-zero → block (fail-closed for safety; an environment so broken
# that `python -m vco_lib.diagram_paths` crashes should not silently
# allow potentially-bad writes).
"$VENV" -m vco_lib.diagram_paths validate --kind auto "$FILE_PATH"
RC=$?

if [ "$RC" -eq 0 ]; then
    exit 0
fi

# Non-zero from the validator: surface it as a blocked PreToolUse.
# stderr already contains the validator's corrective message
# (printed by the CLI itself); we just need to set the exit code.
exit 2
