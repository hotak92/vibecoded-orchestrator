# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# agent-brief-kg-inject.ps1 -- Windows sibling of agent-brief-kg-inject.sh.
# PreToolUse(Agent|Task) wrapper for the v0.2.101 injection redesign
# (PLAN-V02101 section C4). See the .sh sibling for the full rationale. MUST
# MATCH it.
#
# Output contract -- DIFFERENT from the other injection wrappers: the router
# prints the COMPLETE updatedInput envelope (every original tool_input field
# echoed, only `prompt` modified) and this wrapper passes it through
# VERBATIM -- no Emit-AdditionalContext wrapping, and NEVER a
# permissionDecision (that would auto-approve the spawn).

# Scrub sensitive env vars before any subprocess spawning
# (list MUST MATCH _lib/scrub-env.ps1; enforced by the scrub parity gate).
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Resolve-Path (Join-Path $ScriptDir "..\..")).Path }

# Resolve the VCO venv + the router script (both live ONLY in the
# orchestrator root -- v0.2.100 F3 discipline; the router still runs with
# THIS project's CLAUDE_PROJECT_DIR/env).
$Venv = ""
$Router = ""
$ResolveVenv = Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1"
if (Test-Path $ResolveVenv) {
    . $ResolveVenv
    $resolvedVenv = Resolve-VcoVenvPython -ScriptDir $ScriptDir
    if ($resolvedVenv) { $Venv = $resolvedVenv }
    if (Get-Command Resolve-VcoOrchestratorScript -ErrorAction SilentlyContinue) {
        $Router = Resolve-VcoOrchestratorScript -ScriptDir $ScriptDir -RelPath "claude_mcp_servers/scripts/hook_context_router.py"
    }
}
if (-not $Venv -or -not $Router) { exit 0 }
$env:CLAUDE_PROJECT_DIR = $ProjectRoot

# stdin passthrough (see read-context-inject.ps1 for the R31 rationale).
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

# The broken-install notice is keyed per SESSION -- extract the payload's
# session_id the pattern way (no JSON parser needed at this stage). The
# extraction has ONE home: Get-VcoHookFastSessionId in _lib/session-id.ps1
# (wave-2 review nit-7 -- was an inline copy here; it lives in session-id,
# NOT inject-budget, because the notice must work when inject-budget is the
# MISSING lib). A missing session-id.ps1 leaves $VcoSid "" (accepted).
$VcoSid = ""
$FastSidLib = Join-Path $PSScriptRoot "_lib/session-id.ps1"
if (Test-Path $FastSidLib) {
    . $FastSidLib
    if (Get-Command Get-VcoHookFastSessionId -ErrorAction SilentlyContinue) {
        $VcoSid = Get-VcoHookFastSessionId -HookStdin $HookStdin
    }
}

# v0.2.101: kill-switch fast path (VCO_INJECT_PROFILE=off). The lib is on
# Get-VcoRequiredHookLibs' critical set: when it is MISSING that is a broken
# install -- report it on stderr (once per session). This hook's stdout is the
# harness updatedInput envelope, so the notice CANNOT ride an
# additionalContext here; stderr + the SessionStart probe are the channels.
# MUST MATCH the .sh sibling.
$InjectBudgetLib = Join-Path $ScriptDir "_lib/inject-budget.ps1"
if (Test-Path $InjectBudgetLib) {
    . $InjectBudgetLib
    if (Test-VcoInjectProfileOff) { exit 0 }
} else {
    $EmitHelper = Join-Path $ScriptDir "_lib/emit-context.ps1"
    if (Test-Path $EmitHelper) { . $EmitHelper }
    if (Get-Command Emit-VcoMissingHookLibNotice -ErrorAction SilentlyContinue) {
        [void](Emit-VcoMissingHookLibNotice -HooksDir $ScriptDir -ProjectRoot $ProjectRoot -SessionId $VcoSid -Lib "inject-budget")
    }
}

$Envelope = ""
if ($HookStdin) {
    try { $Envelope = ($HookStdin | & $Venv $Router 'agent' 2>$null) -join "`n" } catch { }
}

# The router's stdout IS the harness envelope (hookSpecificOutput.
# updatedInput, or nothing). Pass it through verbatim.
if ($Envelope -and ($Envelope -match '\S')) {
    Write-Output $Envelope
}
exit 0
