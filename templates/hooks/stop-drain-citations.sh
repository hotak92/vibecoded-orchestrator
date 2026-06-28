#!/usr/bin/env bash
# Stop-hook deferred-citation drain (F-QUEUE, v0.2.70)
# Fires at turn-end (Stop). Reads session_id + transcript_path from stdin JSON
# and runs the python drain, which recovers hook-path RL citations the doomed
# in-process asyncio monitor never could (≈72% of all retrievals).
#
# ACCUMULATE-DON'T-DROP: the drain computes+writes ONLY when a pending task's
# cumulative answer window reaches the token gate; below-gate windows are left
# for the next Stop so they keep accumulating. It also TTL-sweeps abandoned
# pending files. Soft-fail throughout — telemetry must never break the turn.
#
# Constraints:
#   - Stop hooks' plain stdout is discarded by the harness; this hook only
#     writes telemetry.
#   - Always exit 0 (Stop hook must not block the turn end).
#   - Runs via the project venv (resolve-vco-venv.sh) so the rl_client modules
#     import cleanly.

unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# shellcheck source=_lib/stderr-cap.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/stderr-cap.sh" ] && . "$SCRIPT_DIR/_lib/stderr-cap.sh"

# shellcheck source=_lib/find-python.sh disable=SC1091
# $PY (a plain system interpreter) parses the stdin JSON only. It is NOT a
# valid runner for the drain: the drain imports claude_mcp_servers.rl_client.*,
# which only the VCO venv satisfies (see VENV resolution below). If no
# interpreter at all is on PATH we cannot even parse, so soft-exit.
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0

HOOK_STDIN=$(cat 2>/dev/null || echo "")

# Parse session_id + transcript_path from the Stop stdin JSON in one call.
_PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('session_id', ''))
    print(d.get('transcript_path', ''))
except Exception:
    print(''); print('')
" 2>/dev/null || printf '\n\n')

SESSION_ID=$(printf '%s' "$_PARSED" | sed -n '1p')
TRANSCRIPT_PATH=$(printf '%s' "$_PARSED" | sed -n '2p')

# Resolve the project venv python (the drain imports claude_mcp_servers.*).
# v0.2.70 FIX-2 (sibling-divergence): require the RESOLVED VCO venv on BOTH
# OSes -- no fallback to system $PY. The drain imports
# claude_mcp_servers.rl_client.*, so a bare system interpreter would ImportError
# anyway; falling back to it merely burned a subprocess for a guaranteed failure
# AND diverged from the .ps1 (which already required the resolved venv). Now
# both siblings make the SAME decision: venv resolves -> run; venv absent ->
# soft-exit 0 (telemetry recovery skipped, the turn is never blocked).
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/resolve-vco-venv.sh" ] && . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
if command -v resolve_vco_venv_python >/dev/null 2>&1; then
    resolve_vco_venv_python "$SCRIPT_DIR"
fi
VENV="${VCO_VENV_PYTHON:-}"
[ -n "$VENV" ] && [ -x "$VENV" ] || exit 0

DRAIN="$PROJECT_ROOT/claude_mcp_servers/scripts/rl_drain_citations.py"
[ -f "$DRAIN" ] || exit 0

# Run the drain. Backgrounded + waited so a slow embed never holds up the turn
# beyond the hook timeout; soft-fail on any error.
( CLAUDE_PROJECT_DIR="$PROJECT_ROOT" "$VENV" "$DRAIN" \
    --session-id "$SESSION_ID" \
    --transcript-path "$TRANSCRIPT_PATH" >/dev/null 2>&1 ) &
DRAIN_PID=$!
wait "$DRAIN_PID" 2>/dev/null || true

exit 0
