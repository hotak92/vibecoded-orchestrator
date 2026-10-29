# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# prompt-context-inject.ps1 -- Windows sibling of prompt-context-inject.sh.
# UserPromptSubmit wrapper for the prompt-time leg of the injection redesign
# (v0.2.102 W6-F): stdin -> the ONE router (surface `prompt`) ->
# Emit-AdditionalContext envelope. Every decision lives in the router and
# vco_lib/inject_intent.py (the query rule is prompt_query; the section 2.1
# `prompt` gate is a 0.725 floor, titles-only below 0.85); see the .sh
# sibling for the full rationale. MUST MATCH it.

# Scrub sensitive env vars before any subprocess spawning
# (list MUST MATCH _lib/scrub-env.ps1; enforced by the scrub parity gate).
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# v0.2.102 W6-F fix-round: per-surface kill switch (VCO_PROMPT_INJECT=off)
# disables ONLY this prompt surface (the global VCO_INJECT_PROFILE=off,
# checked in _lib/inject-budget.ps1 below, disables every surface). Checked
# here BEFORE anything is spawned; the router re-checks it (the enforcement
# home). MUST MATCH the .sh sibling.
$VcoPromptInject = "$env:VCO_PROMPT_INJECT"
if ($VcoPromptInject -and $VcoPromptInject.ToLowerInvariant() -eq "off") { exit 0 }

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Resolve-Path (Join-Path $ScriptDir "..\..")).Path }

$EmitHelper = Join-Path $ScriptDir "_lib/emit-context.ps1"
if (Test-Path $EmitHelper) { . $EmitHelper }

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

# stdin passthrough: the router re-parses the full hook payload itself
# (query text never travels through argv, R31 privacy discipline).
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
    # v0.2.102 W6-C2: keyed PER AGENT (<sid>__<agent_id> for a subagent's
    # call) so a subagent's notice cannot consume the parent's sentinel.
    # MUST MATCH the .sh sibling.
    if (Get-Command Get-VcoHookFastAgentKey -ErrorAction SilentlyContinue) {
        $VcoSid = Get-VcoHookFastAgentKey -HookStdin $HookStdin
    } elseif (Get-Command Get-VcoHookFastSessionId -ErrorAction SilentlyContinue) {
        $VcoSid = Get-VcoHookFastSessionId -HookStdin $HookStdin
    }
}

# v0.2.101: kill-switch fast path (VCO_INJECT_PROFILE=off) -- the router
# re-checks it. The lib is on Get-VcoRequiredHookLibs' critical set: when it
# is MISSING that is a broken install -- report it loudly (once per session,
# both channels) and keep running (the router owns the real enforcement).
# MUST MATCH the .sh sibling.
$script:InjectLibNotice = ""
$InjectBudgetLib = Join-Path $ScriptDir "_lib/inject-budget.ps1"
if (Test-Path $InjectBudgetLib) {
    . $InjectBudgetLib
    if (Test-VcoInjectProfileOff) { exit 0 }
} elseif (Get-Command Emit-VcoMissingHookLibNotice -ErrorAction SilentlyContinue) {
    $script:InjectLibNotice = Emit-VcoMissingHookLibNotice -HooksDir $ScriptDir -ProjectRoot $ProjectRoot -SessionId $VcoSid -Lib "inject-budget"
}

$InjectText = ""
if ($HookStdin) {
    try { $InjectText = ($HookStdin | & $Venv $Router 'prompt' 2>$null) -join "`n" } catch { }
}

# ONE envelope per invocation: the (optional) broken-install notice and the
# (optional) injection text share it -- the envelope is the documented
# additionalContext shape every other surface uses.
$Out = ""
if ($InjectText -and ($InjectText -match '\S')) { $Out = "[KG context for this prompt]:`n`n$InjectText" }
if ($script:InjectLibNotice) {
    if ($Out) { $Out = "$($script:InjectLibNotice)`n`n$Out" } else { $Out = "$($script:InjectLibNotice)" }
}
if ($Out -and ($Out -match '\S')) {
    if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
        Emit-AdditionalContext $Out 'UserPromptSubmit'
    }
}
exit 0
