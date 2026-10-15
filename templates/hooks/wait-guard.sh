#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Wait-loop / polling guard — THIN WRAPPER (v0.2.102 lane W4-D).
# Registered FIVE times; every registration is this one script with a mode:
#
#   pretooluse       PreToolUse matchers Bash (if-filtered verb rules),
#                    PowerShell (NO if: a PowerShell loop keyword is not a
#                    command, so no if rule could match it -- the module
#                    judges loops itself; v0.2.102 review W5D-2),
#                    Read, and ScheduleWakeup|CronCreate|Monitor
#   record-expansion UserPromptExpansion, matcher loop|schedule (records a
#                    user-typed /loop or /schedule — the documented direct
#                    path for a typed slash command)
#
#   stdin → python -m vco_lib.wait_guard <mode> → deny JSON on stdout
#
# The DECISION MODULE (vco_lib/wait_guard.py) owns every check: foreground
# sleep waits, watch/tail -f/timeout+bash -c polling shapes, loops that
# wait, self-wake tools in subagents and unlocked-main sessions, and the
# Nth-identical status-check repeat detector. Since v0.2.103 a LITERAL
# fixed sleep BETWEEN other segments of one command (`start-x && sleep 3
# && curl ...`; Start-Sleep -Seconds 2 on PowerShell) is allowed — a sleep
# inside a loop (body or condition), a non-literal duration, a
# sole/first/last sleep, or a wait hidden in a subshell, command
# substitution, xargs per-item command or function-body loop is still
# denied. `cmd & wait` is allowed (not a poll). This wrapper owns nothing
# but interpreter resolution and
# stdin/stdout plumbing — it forwards the module's stdout VERBATIM (the
# deny envelope) and lets the module's stderr lines through so the denial
# is visible in the transcript/logs.
#
# Kill switches, checked BEFORE any spawn: VCT_DISABLE_HOOKS (all hooks)
# and VCO_WAIT_GUARD=off/0/false (this guard only — a USER decision). A
# missing venv/python is a silent no-op like every VCO hook; a venv that
# cannot import vco_lib fails LOUDLY on stderr (broken install) but still
# exits 0 — the guard must never break the chat.
#
# Constraints: never exit non-zero. MUST MATCH wait-guard.ps1.

# Scrub sensitive env vars (this hook doesn't need credentials)
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
case "${VCO_WAIT_GUARD:-}" in
    off|0|false|no|OFF|FALSE) exit 0 ;;
esac

set -uo pipefail
MODE="${1:-pretooluse}"
HOOK_STDIN=$(cat 2>/dev/null || echo "")
[ -z "$HOOK_STDIN" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0

# Prefer the VCO venv-Python so `import vco_lib.wait_guard` works; fall
# back to the portable interpreter.
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/resolve-vco-venv.sh" ] && . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
if command -v resolve_vco_venv_python >/dev/null 2>&1; then
    resolve_vco_venv_python "$SCRIPT_DIR"
fi
RUN_PY="${VCO_VENV_PYTHON:-$PY}"
[ -x "$RUN_PY" ] || RUN_PY="$PY"

printf '%s' "$HOOK_STDIN" | "$RUN_PY" -m vco_lib.wait_guard "$MODE" || true

exit 0
