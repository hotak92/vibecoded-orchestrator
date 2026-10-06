# shellcheck shell=bash
# _lib/inject-budget.sh
# Per-turn injection budget + kill switch for the v0.2.101 injection redesign
# (PLAN-V02101 §2.1 "Additional bounds"). Sourced by the injection wrappers
# (pre-bash / read / write / edit / grep / agent-brief context-inject); the
# ENFORCEMENT home is the router (claude_mcp_servers/scripts/hook_context_router.py
# counts chars at emit time through vco_lib/inject_intent.py's constants) —
# this lib gives the SHELL side the same answers: the kill-switch check the
# wrappers run BEFORE spawning anything, the budget-file path convention, a
# read-only "how much is left" probe, and the 1-day GC.
#
# Contract:
#   . _lib/inject-budget.sh
#   vco_inject_profile_off && exit 0            # VCO_INJECT_PROFILE=off
#   P="$(vco_inject_budget_path "$SID" "$PID_")"  # "" → fail OPEN (no budget)
#   N="$(vco_inject_budget_used "$P")"
#   vco_inject_budget_gc                        # opportunistic, 1d
#
# Fail-open discipline (mirrors the seen-store's inject-blind guard): an
# untrustworthy session_id ("" / "default" / hostile chars) or a MISSING
# prompt_id yields an EMPTY path — no enforcement, no shared bucket. An empty
# prompt_id component would collapse every turn of the session onto one
# budget file and over-suppress; better no budget than a wrong one.
#
# MUST MATCH (path shape + fail-open policy + the 6 000 / 2 500 constants'
# one home): vco_lib/inject_intent.py::budget_state_path (the Python home the
# router enforces) and _lib/inject-budget.ps1. The cross-language parity is
# pinned BEHAVIOURALLY by tests/test_v02101_inject_gates.py
# (TestInjectBudgetLib/TestInjectBudgetPs1 drive both siblings and compare
# against the Python one-home) — change one, change all three.
#
# This file is sourced, never executed — no shebang. Library, not a hook.

# --- Idempotent double-source guard ---------------------------------------
if [ -n "${_VCO_INJECT_BUDGET_SOURCED:-}" ]; then
    return 0 2>/dev/null || true
fi
_VCO_INJECT_BUDGET_SOURCED=1

# vco_inject_profile_off — true (0) when the kill switch is engaged
# (VCO_INJECT_PROFILE=off, any case). The router re-checks this itself; the
# wrapper check is the fast path that avoids the spawn entirely.
vco_inject_profile_off() {
    local v
    v="$(printf '%s' "${VCO_INJECT_PROFILE:-}" | tr '[:upper:]' '[:lower:]')"
    [ "$v" = "off" ]
}

# (The pre-python session-id extraction the router wrappers share —
# vco_hook_fast_session_id — lives in _lib/session-id.sh, NOT here: the
# broken-install notice needs it precisely when THIS lib is the missing
# file. Wave-2 review nit-7.)

# vco_inject_budget_path <session_id> <prompt_id> [project_root]
# Echo the per-turn budget counter file, or EMPTY when enforcement must fail
# open (see the header). root defaults to $PROJECT_ROOT / $CLAUDE_PROJECT_DIR.
vco_inject_budget_path() {
    local sid="$1" pid="$2"
    local root="${3:-${PROJECT_ROOT:-${CLAUDE_PROJECT_DIR:-}}}"
    case "$sid" in ''|default|*[!a-zA-Z0-9_-]*) printf '%s' ""; return 0 ;; esac
    case "$pid" in ''|*[!a-zA-Z0-9_-]*)         printf '%s' ""; return 0 ;; esac
    [ -n "$root" ] || { printf '%s' ""; return 0; }
    printf '%s' "$root/.claude/state/inject_budget_${sid}_${pid}"
}

# vco_inject_budget_used <path> — chars already emitted this turn (integer;
# 0 on a missing/unreadable/non-numeric file — soft-fail, never an error).
vco_inject_budget_used() {
    local f="$1" n="0"
    if [ -n "$f" ] && [ -f "$f" ]; then
        n="$(cat "$f" 2>/dev/null || printf '0')"
        case "$n" in ''|*[!0-9]*) n=0 ;; esac
    fi
    printf '%s' "$n"
}

# vco_inject_budget_gc [project_root] — delete budget files older than 1 day
# (BUDGET_GC_AGE_S in the Python home). Opportunistic, bounded, soft-fail.
# -mmin +1440 (GLM review nit-5): minute-granular so the shell GC matches
# the Python/ps1 siblings' exact 86 400 s age instead of find's day-rounded
# -mtime bucket. MUST MATCH inject-budget.ps1 Remove-VcoInjectBudgetStale.
vco_inject_budget_gc() {
    local root="${1:-${PROJECT_ROOT:-${CLAUDE_PROJECT_DIR:-}}}"
    [ -n "$root" ] || return 0
    local state="$root/.claude/state"
    [ -d "$state" ] || return 0
    find "$state" -maxdepth 1 -type f -name 'inject_budget_*' -mmin +1440 -delete 2>/dev/null || true
    return 0
}
