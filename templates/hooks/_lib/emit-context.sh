# shellcheck shell=bash
# emit-context.sh — shared helper for hooks that inject LLM-visible context.
#
# Plain stdout from PreToolUse hooks is silently discarded by Claude Code's
# hook runner — only `hookSpecificOutput.additionalContext` reaches the LLM
# (system reminder wrapper). UserPromptSubmit and SessionStart accept plain
# stdout, but for those hooks this helper is still useful as a unified emit
# point with the same whitespace-only-content guard.
#
# This helper wraps a string in the JSON envelope and emits it, but ONLY
# when the content has visible (non-whitespace) characters. Reason: the
# framework still surfaces a system-reminder block to the LLM when
# additionalContext is whitespace-only. Hooks that build context from
# optional sections (e.g. dedup pipelines) can produce strings of just
# `\n` or spaces when every section is suppressed. Without this guard,
# the LLM sees an empty `[Pre-edit context for ...]:` reminder with no
# body — user-visible noise plus prompt-cache misses.
#
# Args:
#   $1 — context string to emit
#   $2 — (optional) hook event name; defaults to PreToolUse. Use
#        UserPromptSubmit for prompt-submit hooks.
#
# Behaviour:
#   - Empty or whitespace-only content → return 0 silently.
#   - $PY (find-python.sh) missing AND no python3 on PATH → return 0.
#   - 10k char cap matches the documented Claude Code contract.
#   - Always returns 0 (never blocks the calling hook).
#
# OS support: pure POSIX bash + python3. Works on Linux, macOS, Git Bash
# on Windows (provided $PY resolution from find-python.sh has run).

emit_additional_context() {
    local ctx="$1"
    local event_name="${2:-PreToolUse}"

    [ -z "$ctx" ] && return 0

    # Whitespace-only → treat as empty. Saves a Python subprocess too.
    case "$ctx" in
        *[![:space:]]*) ;;
        *) return 0 ;;
    esac

    # Prefer $PY (set by find-python.sh) for cross-OS portability;
    # fall back to python3 on POSIX-only callers.
    local py="${PY:-}"
    if [ -z "$py" ] && command -v python3 >/dev/null 2>&1; then
        py="python3"
    fi
    [ -z "$py" ] && return 0

    local truncated
    truncated=$(printf '%s' "$ctx" | head -c 10000)

    EVENT="$event_name" "$py" -c "
import json, os, sys
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': os.environ.get('EVENT', 'PreToolUse'),
        'permissionDecision': 'allow',
        'additionalContext': sys.stdin.read(),
    }
}))
" <<< "$truncated" 2>/dev/null || true
}
