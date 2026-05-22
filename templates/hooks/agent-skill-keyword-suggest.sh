#!/usr/bin/env bash
# agent-skill-keyword-suggest.sh — UserPromptSubmit hook that scans the user
# prompt for keywords declared in agents'/skills' `keywords:` frontmatter
# and injects a short suggestion as additionalContext.
#
# Filesystem contract: the hook globs `.claude/agents/*.md` and
# `.claude/skills/*/SKILL.md`. That is the entire source of truth. The
# launcher's "disable agent/skill" toggle moves the file to a SIBLING
# `.claude/agents.disabled/` / `.claude/skills.disabled/` directory, so
# disabled files naturally fall outside this glob — no DB lookup needed.
#
# Always exits 0 (never blocks a prompt). Silent when no keyword matches.

# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Stderr cap so a buggy iteration cannot reproduce the 2026-05-07 GUI freeze.
if [ -f "$SCRIPT_DIR/_lib/stderr-cap.sh" ]; then
    # shellcheck source=_lib/stderr-cap.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/stderr-cap.sh"
fi

# Resolve a Python interpreter portably — bare `python3` is missing on
# Windows (only `python.exe` / `py` exist).
if [ -f "$SCRIPT_DIR/_lib/find-python.sh" ]; then
    # shellcheck source=_lib/find-python.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/find-python.sh"
fi
# Hardening (Wave-1 integration review): if _lib/find-python.sh is missing
# (partial install, manual hook copy without the _lib/ siblings) $PY stays
# unset and the hook would silently no-op even when `python3` is on PATH.
# Fall back to a direct `command -v` probe so we degrade gracefully.
if [ -z "${PY:-}" ]; then
    PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || command -v py 2>/dev/null || true)"
fi
[ -z "${PY:-}" ] && exit 0  # No Python interpreter found anywhere → silent no-op.

# emit-context.sh is preferred (handles 10 KB cap + whitespace gate +
# JSON envelope shape). If missing, we still emit a bare JSON envelope
# inline rather than failing.
if [ -f "$SCRIPT_DIR/_lib/emit-context.sh" ]; then
    # shellcheck source=_lib/emit-context.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/emit-context.sh"
fi

# Project root resolution: CLAUDE_PROJECT_DIR is the canonical signal at
# hook fire time; fall back to PWD for ad-hoc invocations.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"

# Locate the matcher. Per the install-bundle layout, hooks live at
# `<project>/.claude/hooks/` and scripts at `<project>/.claude/scripts/`,
# so the matcher is two directories up + scripts/<name>.
MATCHER="$SCRIPT_DIR/../scripts/agent-skill-keyword-match.py"
if [ ! -f "$MATCHER" ]; then
    # Fallback: templates layout (only relevant when running uninstalled
    # against the orchestrator clone for testing).
    if [ -f "$SCRIPT_DIR/../../templates/scripts/agent-skill-keyword-match.py" ]; then
        MATCHER="$SCRIPT_DIR/../../templates/scripts/agent-skill-keyword-match.py"
    else
        exit 0  # Matcher missing → silent no-op.
    fi
fi

# Hook input contract (v2.1.x): JSON payload on stdin. We need the `prompt`
# field; session_id is not used by this hook (it's stateless).
HOOK_STDIN=$(cat 2>/dev/null || echo "")
[ -z "$HOOK_STDIN" ] && exit 0

PROMPT=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    p = d.get('prompt', '')
    if p:
        sys.stdout.write(str(p))
except Exception:
    pass
" 2>/dev/null || printf '')

[ -z "$PROMPT" ] && exit 0

# Run the matcher. It is pure-stdlib, always exits 0, prints either an
# empty string (no matches) or 1-2 short lines.
MSG=$(printf '%s' "$PROMPT" | CLAUDE_PROJECT_DIR="$PROJECT_ROOT" "$PY" "$MATCHER" 2>/dev/null || printf '')

[ -z "$MSG" ] && exit 0

# Whitespace-only guard happens inside emit_additional_context too, but
# checking here avoids the subprocess for the common no-match case.
case "$MSG" in
    *[![:space:]]*) ;;
    *) exit 0 ;;
esac

if command -v emit_additional_context >/dev/null 2>&1; then
    emit_additional_context "$MSG" "UserPromptSubmit"
else
    # Inline fallback: emit a minimal JSON envelope so the suggestion still
    # reaches the LLM even when the helper is absent.
    "$PY" -c "
import json, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'UserPromptSubmit',
        'additionalContext': sys.stdin.read(),
    }
}))
" <<< "$MSG" 2>/dev/null || true
fi

exit 0
