# shellcheck shell=bash
# _lib/hook-start.sh -- stamp the moment this hook started, for the context
# router's run deadline.
#
# One home (v0.2.105) for the stamp the v0.2.104 E-2 fix added inline to
# pre-write-context-inject only. The harness's 10 s hook timeout counts from
# the moment it spawned the WRAPPER, and the router
# (claude_mcp_servers/scripts/hook_context_router.py, _run_deadline_anchor)
# counts both its leg budget and its HARD end-to-end deadline from the
# earliest start it can trust: this stamp when present, else its own
# interpreter start. Every wrapper that spawns the router sources this first
# thing, so the wrapper's own pre-spawn work (payload parsing, venv
# resolution, cache lookups) is inside the deadline.
#
# EPOCHREALTIME is bash 5+ and uses the locale's radix character; older bash
# falls back to whole seconds (a coarser, never later, anchor). The router
# ignores an implausible value (future, or more than 60 s old), so a stale
# value inherited from an ambient environment cannot shorten a run.
# MUST MATCH Set-VcoHookStartedAt in _lib/hook-start.ps1.
vco_mark_hook_start() {
    if [ -n "${EPOCHREALTIME:-}" ]; then
        VCO_HOOK_STARTED_AT="${EPOCHREALTIME/,/.}"
    else
        VCO_HOOK_STARTED_AT="$(date +%s 2>/dev/null || true)"
    fi
    export VCO_HOOK_STARTED_AT
}
