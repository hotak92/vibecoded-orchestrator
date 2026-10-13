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

# Get-VcoHookFastSessionId <HookStdin>
# v0.2.101 (wave-2 review nit-7): the pre-python session-id extraction the
# router wrappers share -- one home instead of three inline copies. Lives
# HERE (not in inject-budget) because the broken-install notice needs a
# session key even when inject-budget.ps1 is itself the missing file.
# Hostile/missing id -> "". MUST MATCH session-id.sh vco_hook_fast_session_id.
function Get-VcoHookFastSessionId {
    param([string]$HookStdin)
    if (-not $HookStdin) { return "" }
    if ($HookStdin -match '"session_id"\s*:\s*"([A-Za-z0-9_-]+)"') { return $Matches[1] }
    return ""
}

# --- v0.2.102 W6-C1 (F1/F2/F3/F11): the per-AGENT state key ------------------
# Harness fact (probe 2026-10-13): subagent tool events carry agent_id while
# session_id / prompt_id / transcript_path stay the PARENT'S -- so state keyed
# on session_id alone was shared by the main chat and every subagent. These
# helpers compose the key every per-agent store uses:
#   main agent (no agent_id)   -> <sid>
#   subagent (agent_id set)    -> <sid>__<agent_id>
# Policy (identical in all three legs):
#   * the agent id is sanitised with the session allow-list MINUS "_"
#     ([A-Za-z0-9-], v0.2.102 W6-C2: keeps the composite decodable); hostile
#     -> the literal "default" component (its OWN bucket, never the main
#     store, never raw bytes in a file name);
#   * an untrustworthy session id ("" / "default") passes through UNCHANGED --
#     composing "default__<agent>" would re-create the cross-session bleed
#     the seen-store's inject-blind guard exists to prevent.
# MUST MATCH: session-id.sh (vco_hook_sanitize_agent_id /
# vco_hook_agent_key_from_ids / vco_hook_agent_key) and
# vco_lib/hook_agent_key.py -- parity pinned by
# tests/test_v02102_w6c1_agent_keyed_hook_state.py.

# Get-VcoSanitizedAgentId: the agent component's guard. Allow-list
# [A-Za-z0-9-] -- the session charset MINUS "_" (v0.2.102 W6-C2, review R1
# C1), so the composite key decodes uniquely. Empty stays empty; anything
# else (an "_" included) collapses to "default". MUST MATCH session-id.sh.
function Get-VcoSanitizedAgentId {
    param([string]$Raw)
    if (-not $Raw) { return "" }
    if ($Raw -cmatch '[^A-Za-z0-9-]') { return "default" }
    return $Raw
}

# Get-VcoAgentKeyFromIds <SessionId> <AgentId>
# Compose the per-agent key from a session id and a RAW agent id. Both are
# sanitised here, so a legacy caller passing an unsanitised session id cannot
# smuggle path-hostile bytes into the composite either.
function Get-VcoAgentKeyFromIds {
    param([string]$SessionId, [string]$AgentId)
    $sid = Get-VcoSanitizedSessionId -Raw $SessionId
    $agent = Get-VcoSanitizedAgentId -Raw $AgentId
    if (-not $agent -or -not $sid -or $sid -eq "default") { return $sid }
    return "${sid}__${agent}"
}

# Get-VcoHookFastAgentKey -HookStdin <HookStdin>
# The pattern-way per-agent key for the injection wrappers' broken-install
# notice sentinels (no JSON parse; must work when a lib is missing).
# v0.2.102 W6-C2 (C1 residual). MUST MATCH session-id.sh
# vco_hook_fast_agent_key.
function Get-VcoHookFastAgentKey {
    param([string]$HookStdin)
    $sid = Get-VcoHookFastSessionId -HookStdin $HookStdin
    $agent = ""
    if ($HookStdin -and ($HookStdin -match '"agent_id"\s*:\s*"([^"]*)"')) { $agent = $Matches[1] }
    return (Get-VcoAgentKeyFromIds -SessionId $sid -AgentId $agent)
}

# Get-VcoHookAgentKey -Stdin <HookStdin>
# Parse session_id AND agent_id from the payload, sanitise both, compose. A
# main-agent payload (absent/empty agent_id) yields exactly
# Get-VcoHookSessionId's answer. Soft-fail: malformed stdin -> "".
function Get-VcoHookAgentKey {
    param([string]$Stdin)
    $sid = ""
    $agent = ""
    if ($Stdin) {
        try {
            $payload = $Stdin | ConvertFrom-Json -ErrorAction Stop
            if ($payload -and $payload.session_id) { $sid = [string]$payload.session_id }
            if ($payload -and $payload.agent_id) { $agent = [string]$payload.agent_id }
        } catch {
            # Empty/malformed stdin -- leave both empty.
        }
    }
    return Get-VcoAgentKeyFromIds -SessionId $sid -AgentId $agent
}
