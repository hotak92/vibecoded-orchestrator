# _lib/session-id.ps1
# Shared helper dot-sourced by the .ps1 context hooks that key per-session
# state files off the Claude Code session_id (diff-context-inject,
# compact-context-reinject, context-size-check, post-compact).
#
# Why this exists (one concern, one home — CLAUDE.md "search before add")
# -----------------------------------------------------------------------
# Before this helper, each .ps1 hook INLINED its own ConvertFrom-Json →
# session_id parse. That is the same logic in four places, and — more
# importantly — a security one: the session_id is interpolated verbatim into
# FILE PATHS (CONTEXT_STATE_$SessionId.md, ctx_snapshot_session_$SessionId).
# session_id is normally a trusted Claude-generated UUID, but a `/` or `..` in
# it would compose an unintended path. Centralising the parse AND the sanitise
# here means the defense-in-depth guard lives in ONE place, applied identically
# everywhere.
#
# Defense-in-depth: this is the first code that puts session_id into a
# user-curated CONTENT dir (.claude/context/), not just the throwaway
# .claude/state/ baselines — so path hygiene matters here (review C-1).
#
# MUST MATCH: templates/hooks/_lib/session-id.sh (the sanitise charset
# [A-Za-z0-9_-] and the `default` fallback must agree cross-OS).
#
# Usage (from any .ps1 hook):
#     $LibDir = Join-Path $PSScriptRoot "_lib"
#     . (Join-Path $LibDir "session-id.ps1")
#     $SessionId = Get-VcoHookSessionId -Stdin $HookStdin
#
# Returns:
#   - sanitised session_id   when the payload carried a clean session_id
#   - "default"              when the payload carried a session_id containing
#                            any char outside [A-Za-z0-9_-] (hostile/odd id)
#   - ""  (empty)            when the payload had no session_id / was malformed.
#
# Each caller applies its OWN empty-handling convention on top (diff-context-
# inject maps empty -> "default"; the SessionStart hooks gate on a non-empty
# value). The helper unifies parse+sanitise, not the per-hook empty policy.
#
# This file is dot-sourced, never executed. It's a library, not a hook —
# it is NOT registered in settings.json.windows.template.

# Sanitise-Session: pure path-safety guard. Returns the input unchanged if it
# consists solely of [A-Za-z0-9_-]; otherwise returns "default". Empty stays
# empty (caller's empty-policy decides what empty means).
function Get-VcoSanitizedSessionId {
    param([string]$Raw)
    if (-not $Raw) { return "" }
    if ($Raw -match '[^A-Za-z0-9_-]') { return "default" }
    return $Raw
}

# Get-VcoHookSessionId: parse session_id from a hook stdin JSON payload, then
# sanitise it. Soft-fail throughout: a malformed/empty payload yields empty.
function Get-VcoHookSessionId {
    param([string]$Stdin)
    $parsed = ""
    if ($Stdin) {
        try {
            $payload = $Stdin | ConvertFrom-Json -ErrorAction Stop
            if ($payload -and $payload.session_id) {
                $parsed = [string]$payload.session_id
            }
        } catch {
            # Empty/malformed stdin — leave $parsed empty.
        }
    }
    return Get-VcoSanitizedSessionId -Raw $parsed
}
