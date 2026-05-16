#!/usr/bin/env bash
# check-no-fork-bomb.sh — Defense-in-depth detector for lean-ctx fork-bombs.
#
# Background: pre-0.2.11 the BASH_ENV lean-ctx shim could trigger runaway
# fork-bombs (incidents 2026-04-30 + 2026-05-15). Root cause was the
# BASH_ENV → leanctx-bash-env.sh pattern: the shim re-exported its own
# env into every child shell, so when an interactive `lean-ctx -c …`
# wrapper invoked `bash -c …` for its inner command, that inner bash
# resourced BASH_ENV, which re-invoked lean-ctx, which spawned `bash -c`
# again, and so on. On the 2026-05-15 recurrence, `pgrep -c lean-ctx`
# reached 4512 with 24,742 PIDs in /proc and 58 GB resident in the
# app.slice cgroup before systemd-oomd killed the GDM session.
#
# Root cause fixed in 0.2.11 (PR-1 eliminated the BASH_ENV wiring from
# the orchestrator's own settings.json and templates). This hook is the
# safety net for any future regression of a similar pattern: if the
# process count of `lean-ctx` exceeds a sane threshold at SessionStart,
# nuke them all and warn loudly before they consume the host.
#
# Threshold rationale: normal steady-state is 0–2 (one for an active
# hook invocation, occasionally a background MCP server). 100 is roughly
# 50× the worst legitimate case and 45× below the lowest observed
# fork-bomb count (4512), so it sits squarely in the unambiguous-bomb
# zone without false positives on normal workloads.
#
# See knowledge/concepts/lean-ctx-shim-disabled.md for full incident
# forensics and the original proposal this hook implements.

set -u

# Scrub sensitive env vars before any subprocess spawning (defense-in-depth
# parity with every other VCO hook — enforced by tests/test_hooks_disable_guard.py).
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null

# Opt-out (debugging / release operations).
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# Cap any unbounded stderr from this hook (defense against the 2026-05-07
# GUI freeze; see _lib/stderr-cap.sh).
. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

# Threshold: > THRESHOLD lean-ctx processes is treated as a fork-bomb in
# progress. Override via LEAN_CTX_FORK_BOMB_THRESHOLD for testing.
: "${LEAN_CTX_FORK_BOMB_THRESHOLD:=100}"

# Count current user's lean-ctx processes. Cross-OS pattern:
#   - GNU pgrep (Linux) supports `-c` (count) and `-u`.
#   - BSD pgrep (macOS) supports `-x` (exact match) and `-u` but `-c`
#     means something different — so we always pipe through `wc -l` for
#     portability.
# `-x lean-ctx`  → match the process *name* exactly (avoids hitting a
#                  user shell that happens to contain "lean-ctx" in its
#                  argv).
# `-u "$USER"`   → restrict to the current user; we must not try to kill
#                  processes belonging to root or other users.
#
# If `pgrep` is missing entirely (unusual), fail open: exit 0 silently.
if ! command -v pgrep >/dev/null 2>&1; then
    exit 0
fi

LEAN_CTX_COUNT=$(pgrep -x -u "${USER:-$(id -un)}" lean-ctx 2>/dev/null | wc -l | tr -d '[:space:]')
# Defensive default if the count came back empty (e.g. wc piped nothing).
LEAN_CTX_COUNT=${LEAN_CTX_COUNT:-0}

if [ "${LEAN_CTX_COUNT}" -le "${LEAN_CTX_FORK_BOMB_THRESHOLD}" ]; then
    # Normal case: exit silently. The hook is sub-millisecond on this path.
    exit 0
fi

# --- Fork-bomb path ---
timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
hook_tag="[check-no-fork-bomb ${timestamp}]"

echo "${hook_tag} ⚠️  Fork-bomb detected: ${LEAN_CTX_COUNT} lean-ctx processes (threshold ${LEAN_CTX_FORK_BOMB_THRESHOLD})." >&2
echo "${hook_tag} Killing all lean-ctx processes for user ${USER:-$(id -un)}..." >&2
echo "${hook_tag} See knowledge/concepts/lean-ctx-shim-disabled.md for context." >&2

# `pkill -KILL -x -u $USER lean-ctx` — exact name match, current user only.
# `-KILL` is portable to BSD pkill (macOS) and GNU pkill (Linux). We
# intentionally use SIGKILL (not SIGTERM) — a fork-bomb is spawning faster
# than a graceful shutdown handler could run.
pkill -KILL -x -u "${USER:-$(id -un)}" lean-ctx 2>/dev/null || true

# Give the kernel a moment to reap the killed processes before we re-count.
sleep 1

REMAINING=$(pgrep -x -u "${USER:-$(id -un)}" lean-ctx 2>/dev/null | wc -l | tr -d '[:space:]')
REMAINING=${REMAINING:-0}

if [ "${REMAINING}" -eq 0 ]; then
    echo "${hook_tag} Kill complete. Remaining: 0." >&2
else
    echo "${hook_tag} Kill done. Remaining: ${REMAINING} (may still be reaping)." >&2
fi

# Best-effort desktop notification. Non-fatal if the dispatcher is missing
# or the desktop session can't show toasts. Prefer the cross-platform
# helper at .claude/scripts/notify.py (Linux notify-send / macOS osascript /
# Windows toast); fall back to a direct notify-send / osascript call if
# the helper is absent.
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
notify_helper="${PROJECT_DIR}/.claude/scripts/notify.py"
notify_title="VCO fork-bomb killed"
notify_body="Killed ${LEAN_CTX_COUNT} runaway lean-ctx processes (remaining: ${REMAINING})"

notified=0
if [ -f "${notify_helper}" ]; then
    # Resolve a Python interpreter portably via the existing helper.
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    # shellcheck source=_lib/find-python.sh disable=SC1091
    [ -f "${SCRIPT_DIR}/_lib/find-python.sh" ] && . "${SCRIPT_DIR}/_lib/find-python.sh"
    if [ -n "${PY:-}" ]; then
        "$PY" "${notify_helper}" "${notify_title}" "${notify_body}" \
            --urgency critical --icon dialog-error 2>/dev/null && notified=1 || true
    fi
fi
if [ "${notified}" -eq 0 ]; then
    if command -v notify-send >/dev/null 2>&1; then
        notify-send -u critical "${notify_title}" "${notify_body}" 2>/dev/null || true
    elif command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"${notify_body}\" with title \"${notify_title}\"" 2>/dev/null || true
    fi
fi

# Exit 0 unconditionally. The hook did its job (killed the bomb +
# warned); we do not want to block session start.
exit 0
