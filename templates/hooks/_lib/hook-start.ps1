# _lib/hook-start.ps1 -- stamp the moment this hook started, for the context
# router's run deadline.
#
# One home (v0.2.105) for the stamp the v0.2.104 E-2 fix added inline to
# pre-write-context-inject only. The harness's 10 s hook timeout counts from
# the moment it spawned the WRAPPER, and the router
# (claude_mcp_servers/scripts/hook_context_router.py, _run_deadline_anchor)
# counts both its leg budget and its HARD end-to-end deadline from the
# earliest start it can trust: this stamp when present, else its own
# interpreter start. Every wrapper that spawns the router dot-sources this
# first thing, so the wrapper's own pre-spawn work is inside the deadline.
#
# Invariant culture: a "1,5" radix would be ignored by the router. The router
# ignores an implausible value (future, or more than 60 s old).
# MUST MATCH vco_mark_hook_start in _lib/hook-start.sh.
function Set-VcoHookStartedAt {
    $env:VCO_HOOK_STARTED_AT = ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0).ToString([System.Globalization.CultureInfo]::InvariantCulture)
}
