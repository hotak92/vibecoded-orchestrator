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


# ───────────────────────────────────────────────────────────────────────────
# vco_report_missing_hook_lib <hooks_dir> <project_root> <session_id> <lib>
#
# A shipped hook library that is ABSENT is a BROKEN INSTALL, not a fallback
# case (CLAUDE.md: "loud-fail, never silent-fallback, when a shipped
# dependency is missing"). Hooks source their `_lib/*` helpers conditionally
# so a half-applied bundle cannot make the user's Edit/Write/Bash ERROR — but
# conditional-source plus `command -v … || exit 0` had become a SILENT no-op
# mode: after v0.2.95 moved ALL routing into `_lib/route-touched-path.sh`, a
# project missing that one file synced NOTHING on any write and said nothing
# about it (review MAJOR-1).
#
# This is the missing surface. It does NOT change the soft-fail contract —
# the caller still exits 0, still never blocks — it only makes the
# degradation VISIBLE, on two channels, once per session:
#
#   * stderr — for the human watching the terminal. PostToolUse stderr is
#     not shown to the model, which is exactly why the second channel exists.
#   * `$VCO_MISSING_LIB_NOTICE` — text for the CALLER to put in its own
#     `additionalContext` envelope (plain PostToolUse stdout is discarded per
#     the v2.1.x contract). The caller owns the emit so a hook still sends ONE
#     envelope per invocation; this function never writes to stdout.
#
# Once per session, keyed by `.claude/state/route_lib_missing_<session>_<lib>`
# — the same sentinel shape `_lib/kg-sync-debounce.sh` uses for its
# `kg_sync_failure_<session>_<channel>` rows, for the same reason: a hook that
# fires 200 times a session must not print 200 times. When the sentinel cannot
# be written the notice is emitted again next time rather than lost.
#
# $VCO_MISSING_LIB_NOTICE is set to "" when the condition was already
# reported, so `[ -n "$VCO_MISSING_LIB_NOTICE" ]` is the caller's test.
#
# MUST MATCH `_lib/emit-context.ps1`'s Emit-VcoMissingHookLibNotice.
VCO_MISSING_LIB_NOTICE=""

# ---------------------------------------------------------------------------
# THE SET: which `_lib/` files the write pipeline cannot work without, and
# what each one's absence actually costs.
# ---------------------------------------------------------------------------
# v0.2.95 ship-gate review MAJOR-2. Rev 1's finding — "a shipped component
# with a SILENT no-op mode" — was closed for `route-touched-path` alone, by
# hand, in the two places that happened to be in front of the fix lane. Two
# siblings of the same shape stayed silent, and the SessionStart probe built
# for the finding printed "KG write routing: OK" while one of them was gone.
# Enumerating three paths by hand is what produced that; so the set has ONE
# home, here, and three readers take it FROM here:
#
#   * `vco_report_missing_hook_lib` below — the per-lib wording of the notice
#     (the body used to be hard-wired to route-touched-path's role, which
#     would have been a FALSE sentence for either of the others);
#   * `session-start-retrieval-health.{sh,ps1}` — the SessionStart probe
#     iterates it, so a row added here is probed with no edit there;
#   * `tests/test_v0295_missing_route_lib_is_loud.py` — drives every row
#     through the real hooks, and REFUSES a row it has no driver for, so a
#     fourth lib cannot be added and silently left uncovered.
#
# Basenames, no extension: the two flavours differ only by suffix, and each
# reader appends its own. MUST MATCH `_lib/emit-context.ps1`'s
# Get-VcoRequiredHookLibs / Get-VcoHookLibRole.
vco_required_hook_libs() {
    printf '%s\n' route-touched-path bash-write-targets code-extensions
}

# vco_hook_lib_role <lib-name-with-or-without-extension> — one clause naming
# what the file is the one home FOR and what stops without it. An unknown name
# still gets a true (if general) sentence rather than silence.
vco_hook_lib_role() {
    local base="${1##*/}"
    base="${base%.sh}"
    base="${base%.ps1}"
    case "$base" in
        route-touched-path)
            printf '%s' "routing a touched path to kg-sync (knowledge/), the development collection (docs/), the diagram indexer and the code-graph queue, so every Edit, Write and CLI write is being parsed and then dropped" ;;
        bash-write-targets)
            printf '%s' "recovering the paths a shell command wrote, so no CLI write is even looked at — a redirect, a heredoc, sed -i or patch into knowledge/ or docs/ now reaches Weaviate never" ;;
        code-extensions)
            printf '%s' "deciding which touched paths are code, so nothing is being queued for the end-of-turn code-graph drain and the code graph has stopped tracking this project" ;;
        *)
            printf '%s' "part of this project's shipped hook library, and the hook that needs it has stopped doing its job" ;;
    esac
}

vco_report_missing_hook_lib() {
    local hooks_dir="${1:-}" proot="${2:-}" session="${3:-}" lib="${4:-}"
    VCO_MISSING_LIB_NOTICE=""
    [ -n "$lib" ] || return 0

    # Path-safety for the session id: it is interpolated into a FILE NAME.
    # One home for the rule — `_lib/session-id.sh` — sourced conditionally
    # because this function's whole job is surviving a damaged _lib/; its
    # absence costs the per-session KEY, not the notice (we fall back to the
    # same "default" sentinel that helper returns for a hostile id).
    local key="default"
    if [ -n "$hooks_dir" ] && [ -f "$hooks_dir/_lib/session-id.sh" ]; then
        # shellcheck source=session-id.sh disable=SC1091
        . "$hooks_dir/_lib/session-id.sh"
    fi
    if command -v vco_hook_sanitize_session_id >/dev/null 2>&1 && [ -n "$session" ]; then
        key="$(vco_hook_sanitize_session_id "$session")"
    fi
    [ -n "$key" ] || key="default"

    local notice
    notice="[VCO broken install] .claude/hooks/_lib/${lib} is MISSING.
That file is the one home for $(vco_hook_lib_role "$lib").
Restore it by updating this project's bundle:
  python -m vco_lib.project_init install-bundle --folder ${proot:-<project>} --orchestrator-root <orchestrator-root> --update
(or the launcher's per-project Settings page → \"Update bundle\")."

    # Sentinel LAST-but-one: written only once we are about to report, and
    # only after the stderr line has gone out, so a failed write repeats the
    # notice instead of silently swallowing it.
    local sentinel_dir sentinel
    if [ -n "$proot" ]; then
        sentinel_dir="$proot/.claude/state"
        sentinel="$sentinel_dir/route_lib_missing_${key}_${lib}"
        if mkdir -p "$sentinel_dir" 2>/dev/null && [ -e "$sentinel" ]; then
            return 0   # already reported this session
        fi
    fi

    printf '%s\n' "$notice" >&2
    VCO_MISSING_LIB_NOTICE="$notice"
    [ -n "${sentinel:-}" ] && { : > "$sentinel" 2>/dev/null || true; }
    return 0
}
