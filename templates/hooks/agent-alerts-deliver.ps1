# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# agent-alerts-deliver.ps1 - v0.2.102 W6-C2 (census F8).
# OS-PARITY: ports the .sh sibling. Surfaces the alerts a SUBAGENT hit to its
# PARENT as additionalContext: PostToolUse (matcher *) for Agent|Task naming
# the agent delivers that finished agent's rows; any other MAIN-CHAT
# PostToolUse (W6-C2 fix round, review R4c-10) and UserPromptSubmit deliver
# every pending row of the session (background completions, late
# SubagentStop jobs). A subagent's own events deliver nothing (vco_lib
# decides). The reader, the row format and the locking live in ONE place:
# vco_lib/agent_alerts.py. Cost when nothing is pending: one .NET directory
# listing and no cmdlet, so close to the bare PowerShell start; no Python.
# Always exits 0. Alerts pending with no resolvable
# VCO venv (a broken install) are said out loud, never skipped in silence.
#
# MUST MATCH agent-alerts-deliver.sh.

if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# Fast path FIRST: nothing pending -> exit before any cmdlet runs (owner
# ruling on the v0.2.102 Fable re-check, W6-ALERTS2). This hook runs on EVERY
# main-chat tool call (matcher *), and the first use of a cmdlet such as
# Get-ChildItem / Test-Path / Join-Path autoloads its module: measured on
# pwsh 7.4 (Linux), the old fast path (env scrub via Test-Path Env:, stdin
# read, Join-Path, two Get-ChildItem) cost ~87 ms on top of the bare ~175 ms
# interpreter start; this one (plain .NET calls, string paths, the pending
# path moved to _lib/) costs ~29 ms (medians: 262 -> 204 ms). The env
# scrub and the stdin read move below the check: nothing before it starts a
# child process, and the scrub exists for the child. "Pending" includes an
# orphaned delivery claim (<file>.jsonl.delivering.<pid>, left by a delivery
# killed mid-way): only Python re-adopts it, so it must not be hidden behind
# this exit (review R5-1); the one pattern agent_alerts_*.jsonl* covers both.
# An unlistable state dir counts as nothing pending, as the .sh glob does.
# MUST MATCH the .sh.
$ScriptDir = $PSScriptRoot
$ProjectRoot = $env:CLAUDE_PROJECT_DIR
if (-not $ProjectRoot) { $ProjectRoot = $ScriptDir + '/../..' }
$StateDir = $ProjectRoot + '/.claude/state'
$hasPending = $false
try {
    if ([System.IO.Directory]::Exists($StateDir)) {
        $hasPending = [System.IO.Directory]::GetFiles($StateDir, 'agent_alerts_*.jsonl*').Length -gt 0
    }
} catch { $hasPending = $false }
if (-not $hasPending) { exit 0 }

# Something is pending: the rest lives in _lib/ so PowerShell does not parse
# it on every no-pending call (see that file). A missing lib is a broken
# install: said on stderr AND to the model (plain PostToolUse stderr never
# reaches it), never skipped in silence.
$PendingLib = $ScriptDir + '/_lib/agent-alerts-deliver-pending.ps1'
if (-not [System.IO.File]::Exists($PendingLib)) {
    $plMsg = "[Subagent alerts] Alerts your subagents hit are pending in $StateDir/agent_alerts_*.jsonl* but cannot be read: .claude/hooks/_lib/agent-alerts-deliver-pending.ps1 is missing (a broken VCO install - repair it with the orchestrator update). Read those files directly."
    [Console]::Error.WriteLine("agent-alerts-deliver: $plMsg")
    $plStdin = ""
    try { $plStdin = [Console]::In.ReadToEnd() } catch { }
    $plEvent = if ($plStdin -match '"UserPromptSubmit"') { "UserPromptSubmit" } else { "PostToolUse" }
    Write-Output (@{ hookSpecificOutput = @{ hookEventName = $plEvent; additionalContext = $plMsg } } | ConvertTo-Json -Compress -Depth 4)
    exit 0
}
. $PendingLib
Invoke-AgentAlertsPendingDelivery -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot -StateDir $StateDir
exit 0
