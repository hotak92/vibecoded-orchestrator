# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/agent-alerts-deliver-pending.ps1 - the PENDING path of
# agent-alerts-deliver.ps1 (v0.2.102 W6-ALERTS2), dot-sourced by that hook
# ONLY once its fast path found an alert file or claim.
#
# Why a separate file: PowerShell parses a whole script before it runs its
# first line, and agent-alerts-deliver.ps1 runs on every main-chat tool call
# (matcher *). Keeping this body out of the hook file took ~5 ms off every
# no-pending call (pwsh 7.4 on Linux, medians of 41 runs: 209 -> ~204 ms;
# PowerShell starting an empty script: ~175 ms).
# bash reads a script as it executes it, so the .sh hook needs no split and
# this file has no .sh sibling (PS1_ONLY_LIB in check_hook_parity.py and
# tests/test_v0292_sibling_parity_blind_spots.py).
#
# Expects the hook's $ScriptDir, $ProjectRoot and $StateDir. Never throws;
# the hook exits 0 after it returns. MUST MATCH agent-alerts-deliver.sh
# (everything after its fast path).

function Invoke-AgentAlertsPendingDelivery {
    param([string]$ScriptDir, [string]$ProjectRoot, [string]$StateDir)

    foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
        if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
    }

    if (-not $env:CLAUDE_PROJECT_DIR) {
        $ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
        $StateDir = Join-Path $ProjectRoot ".claude/state"
    }

    $HookStdin = ""
    try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
    if (-not $HookStdin) { return }

    # The stderr cap compiles a C# writer (Add-Type, ~165 ms measured): taken
    # only once there is something to say, never on the no-pending path.
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
        $adMsg = "[Subagent alerts] Alerts your subagents hit are pending in $StateDir/agent_alerts_*.jsonl* but cannot be read: the VCO venv is not resolvable (a broken VCO install - repair it with the orchestrator update). Read those files directly."
        [Console]::Error.WriteLine("agent-alerts-deliver: $adMsg")
        $adEvent = if ($HookStdin -match '"UserPromptSubmit"') { "UserPromptSubmit" } else { "PostToolUse" }
        $emitLib = Join-Path $ScriptDir "_lib/emit-context.ps1"
        if (Test-Path $emitLib) { . $emitLib }
        if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
            Emit-AdditionalContext $adMsg $adEvent
        }
        return
    }

    try {
        $out = ($HookStdin | & $VenvPy -m vco_lib.agent_alerts deliver --project-root $ProjectRoot 2>$null) -join "`n"
        if ($out -and $out.Trim()) { Write-Output $out }
    } catch { }
}
