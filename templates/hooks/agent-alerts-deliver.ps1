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
# vco_lib/agent_alerts.py. Cost when nothing is pending: one directory
# listing, no interpreter. Always exits 0. Alerts pending with no resolvable
# VCO venv (a broken install) are said out loud, never skipped in silence.
#
# MUST MATCH agent-alerts-deliver.sh.

foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot

$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}
$StateDir = Join-Path $ProjectRoot ".claude/state"

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { exit 0 }

# Fast path: nothing pending -> no interpreter start.
$pending = @(Get-ChildItem -LiteralPath $StateDir -Filter "agent_alerts_*.jsonl" -File -ErrorAction SilentlyContinue)
if ($pending.Count -eq 0) { exit 0 }

# The stderr cap compiles a C# writer (Add-Type, ~165 ms measured): taken
# only once there is something to say, so the per-tool-call no-pending path
# above stays at bare interpreter startup (the matcher is `*`).
$StderrCap = Join-Path $ScriptDir "_lib/stderr-cap.ps1"
if (Test-Path $StderrCap) { . $StderrCap }

# vco_lib is needed, so the VCO venv (a bare system python cannot import it).
$VenvLib = Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1"
$VenvPy = $null
if (Test-Path $VenvLib) {
    . $VenvLib
    $VenvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir
}
if (-not $VenvPy -or -not (Test-Path $VenvPy)) {
    $adMsg = "[Subagent alerts] Alerts your subagents hit are pending in $StateDir/agent_alerts_*.jsonl but cannot be read: the VCO venv is not resolvable (a broken VCO install - repair it with the orchestrator update). Read those files directly."
    [Console]::Error.WriteLine("agent-alerts-deliver: $adMsg")
    $adEvent = if ($HookStdin -match '"UserPromptSubmit"') { "UserPromptSubmit" } else { "PostToolUse" }
    $emitLib = Join-Path $ScriptDir "_lib/emit-context.ps1"
    if (Test-Path $emitLib) { . $emitLib }
    if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
        Emit-AdditionalContext $adMsg $adEvent
    }
    exit 0
}

try {
    $out = ($HookStdin | & $VenvPy -m vco_lib.agent_alerts deliver --project-root $ProjectRoot 2>$null) -join "`n"
    if ($out -and $out.Trim()) { Write-Output $out }
} catch { }
exit 0
