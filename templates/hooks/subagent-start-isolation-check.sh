#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-start-isolation-check.sh — SubagentStart hook (Layer 0b,
# secondary / belt) for the worktree-isolation silent-fallback safeguard.
#
# ── Why ───────────────────────────────────────────────────────────────────
# The primary deterministic gate (worktree-guard.sh on WorktreeCreate) runs
# AT the worktree-create instant. This hook is the belt: when a subagent
# that requested `isolation: worktree` is actually spawning, and the
# SubagentStart payload exposes both an isolation flag AND a cwd/worktree
# field, we assert the cwd is a genuinely separate worktree. On a suspected
# violation (cwd resolves to the parent checkout toplevel) we inject a LOUD
# `additionalContext` block the agent sees in its first turn telling it to
# create its own worktree before any git write.
#
# SubagentStart CANNOT block (the event is non-blocking) — this is pure
# loud-warn, acceptable as a backstop. It NO-OPs gracefully when the
# isolation flag is absent or the payload doesn't expose a cwd (don't guess).
#
# ── Cross-OS parity ──────────────────────────────────────────────────────
# The detection logic (isolation-flag synonyms + cwd synonyms + the
# cwd==toplevel violation test) MUST match subagent-start-isolation-check.ps1.
# Keep them in lockstep.
#
# Always exits 0 (never blocks subagent start). Silent when isolation not
# requested or no cwd is exposed.

# Scrub sensitive env vars before any subprocess spawning.
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Stderr cap so a buggy iteration cannot reproduce the 2026-05-07 GUI freeze.
if [ -f "$SCRIPT_DIR/_lib/stderr-cap.sh" ]; then
    # shellcheck source=_lib/stderr-cap.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/stderr-cap.sh"
fi

# Resolve a Python interpreter portably.
if [ -f "$SCRIPT_DIR/_lib/find-python.sh" ]; then
    # shellcheck source=_lib/find-python.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/find-python.sh"
fi
if [ -z "${PY:-}" ]; then
    PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || command -v py 2>/dev/null || true)"
fi
[ -z "${PY:-}" ] && exit 0

# emit-context.sh handles the 10 KB cap + JSON envelope shape.
if [ -f "$SCRIPT_DIR/_lib/emit-context.sh" ]; then
    # shellcheck source=_lib/emit-context.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/emit-context.sh"
fi

PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"

HOOK_STDIN=$(cat 2>/dev/null || echo "")
[ -z "$HOOK_STDIN" ] && exit 0

# Parse defensively. Emit three lines:
#   line 1 = isolation flag normalised to 0/1 (1 = isolation:worktree requested)
#   line 2 = the subagent's cwd / worktree path (or empty)
#   line 3 = agent_id (for the warning text)
# The isolation flag synonyms: `isolation` (== "worktree"), `isolation_mode`,
# `worktree` (truthy bool/string), `isolated` (bool). The cwd synonyms:
# `cwd`, `worktree_path`, `working_directory`, `working_dir`, `dir`.
PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)

def truthy(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("worktree", "true", "1", "yes", "isolated")
    return False

iso = 0
# Explicit isolation field equals "worktree".
val = d.get("isolation")
if isinstance(val, str) and val.strip().lower() == "worktree":
    iso = 1
elif truthy(val):
    iso = 1
for f in ("isolation_mode", "worktree", "isolated", "worktree_isolation"):
    if truthy(d.get(f)):
        iso = 1
        break

cwd = (
    d.get("cwd")
    or d.get("worktree_path")
    or d.get("working_directory")
    or d.get("working_dir")
    or d.get("dir")
    or ""
)
agent_id = d.get("agent_id") or ""
sys.stdout.write(str(iso) + "\n")
sys.stdout.write(str(cwd) + "\n")
sys.stdout.write(str(agent_id))
' 2>/dev/null || printf '0\n\n')

ISO_FLAG="$(printf '%s' "$PARSED" | sed -n '1p')"
CWD_FIELD="$(printf '%s' "$PARSED" | sed -n '2p')"
AGENT_ID="$(printf '%s' "$PARSED" | sed -n '3p')"

# No-op: isolation not requested, or payload doesn't expose a cwd. Don't guess.
[ "${ISO_FLAG:-0}" = "1" ] || exit 0
[ -n "$CWD_FIELD" ] || exit 0

# Resolve the parent checkout toplevel (relative to project root, so
# monorepo / subdir layouts resolve correctly).
TOPLEVEL=""
if command -v git >/dev/null 2>&1; then
    TOPLEVEL="$(git -C "$PROJECT_ROOT" rev-parse --show-toplevel 2>/dev/null || true)"
fi
# No repo ⇒ nothing to compare against ⇒ no-op.
[ -n "$TOPLEVEL" ] || exit 0

norm_path() {
    local p="$1"
    if command -v realpath >/dev/null 2>&1; then
        realpath -m "$p" 2>/dev/null || printf '%s' "$p"
    else
        printf '%s' "$p"
    fi
}
CWD_ABS="$(norm_path "$CWD_FIELD")"
TOPLEVEL_ABS="$(norm_path "$TOPLEVEL")"

# Violation suspected only when the cwd resolves to the parent toplevel
# itself (HEAD shared). Mirror worktree-guard's single-block test.
[ "$CWD_ABS" = "$TOPLEVEL_ABS" ] || exit 0

MSG="⚠️  ISOLATION VIOLATION SUSPECTED — this subagent requested \`isolation: worktree\` but its cwd ($CWD_ABS) IS the parent checkout. Any \`git commit\` from here lands on the PARENT branch (the 2026-06-30 silent-fallback footgun). BEFORE any git write: create your own worktree (\`git worktree add <path> -b <branch>\`) and \`cd\` into it, OR abort and report. Do NOT commit from the parent checkout."

case "$MSG" in
    *[![:space:]]*) ;;
    *) exit 0 ;;
esac

if command -v emit_additional_context >/dev/null 2>&1; then
    emit_additional_context "$MSG" "SubagentStart"
else
    "$PY" -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SubagentStart',
        'additionalContext': sys.stdin.read(),
    }
}))
" <<< "$MSG" 2>/dev/null || true
fi

exit 0
