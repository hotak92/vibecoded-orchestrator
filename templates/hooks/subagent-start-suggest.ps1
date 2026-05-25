# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-start-suggest.ps1 — Windows sibling of subagent-start-suggest.sh.
# SubagentStart hook that injects agent/skill suggestions into a freshly
# spawned subagent's context, mirroring the UserPromptSubmit equivalent.
#
# Agent vs skill differentiation:
# - Skills: always suggested.
# - Agents: only when subagent's tool list includes `Agent` or `Task`.
#
# Matching is delegated to templates/scripts/agent-skill-keyword-match.py
# (the same matcher the UserPromptSubmit hook uses). The robustness comes
# from how `keywords:` are curated in each agent/skill frontmatter — no
# algorithmic transformation of the prompt is layered on top.
#
# Always exits 0 (never blocks subagent start). Silent when no match.

# Scrub sensitive env vars before any subprocess spawning.
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot

# Stderr cap so a buggy iteration cannot reproduce the 2026-05-07 GUI freeze.
$StderrCap = Join-Path $ScriptDir "_lib/stderr-cap.ps1"
if (Test-Path $StderrCap) { . $StderrCap }

# Resolve a Python interpreter portably.
$FindPy = Join-Path $ScriptDir "_lib/find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
if (-not $PY) {
    foreach ($candidate in @('python3', 'python', 'py')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { $PY = $cmd.Source; break }
    }
}
if (-not $PY) { exit 0 }  # No Python interpreter → silent no-op.

# Emit-AdditionalContext helper (preferred). Inline fallback below if missing.
$EmitHelper = Join-Path $ScriptDir "_lib/emit-context.ps1"
if (Test-Path $EmitHelper) { . $EmitHelper }

# Project root resolution: CLAUDE_PROJECT_DIR is the canonical signal at
# hook fire time; fall back to PWD for ad-hoc invocations.
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }

# Locate the shared matcher.
$Matcher = Join-Path $ScriptDir "..\scripts\agent-skill-keyword-match.py"
if (-not (Test-Path $Matcher)) {
    $alt = Join-Path $ScriptDir "..\..\templates\scripts\agent-skill-keyword-match.py"
    if (Test-Path $alt) {
        $Matcher = $alt
    } else {
        exit 0
    }
}

# SubagentStart hook input: JSON payload on stdin. Same field-name
# defensiveness as the .sh sibling — accept `prompt` / `task` /
# `description` and `tools` / `allowed_tools` / `tool_list`.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { exit 0 }

$Prompt = ""
$SessionId = ""
$HasAgentTool = $true   # Default true: empty tool list → assume yes (user direction: over-suggest).
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        # Prompt resolution.
        if ($payload.PSObject.Properties['prompt'] -and $payload.prompt) {
            $Prompt = [string]$payload.prompt
        } elseif ($payload.PSObject.Properties['task'] -and $payload.task) {
            $Prompt = [string]$payload.task
        } elseif ($payload.PSObject.Properties['description'] -and $payload.description) {
            $Prompt = [string]$payload.description
        }
        # Session id.
        if ($payload.PSObject.Properties['session_id'] -and $payload.session_id) {
            $SessionId = [string]$payload.session_id
        }
        # Tool list resolution. Accept array OR space/comma-delimited string.
        $rawTools = $null
        foreach ($field in 'tools','allowed_tools','tool_list') {
            if ($payload.PSObject.Properties[$field] -and $null -ne $payload.$field) {
                $rawTools = $payload.$field
                break
            }
        }
        $toolList = @()
        if ($rawTools -is [string]) {
            $toolList = $rawTools -split '[\s,]+' | Where-Object { $_ }
        } elseif ($rawTools -is [System.Collections.IEnumerable] -and -not ($rawTools -is [string])) {
            $toolList = @($rawTools | ForEach-Object { [string]$_ } | Where-Object { $_ })
        }
        if ($toolList.Count -gt 0) {
            # Explicit tool list present → check for Agent / Task membership.
            $lower = $toolList | ForEach-Object { $_.ToLowerInvariant() }
            $HasAgentTool = ($lower -contains 'agent') -or ($lower -contains 'task')
        }
        # else: empty tool list → keep $HasAgentTool = $true (default).
    }
} catch {
    # Malformed JSON → silent no-op.
    exit 0
}
if (-not $Prompt) { exit 0 }

# Build the matcher argv. --session-id always; --skills-only only when
# Agent/Task absent from the subagent's tool list.
$MatcherArgs = @('--session-id', $SessionId)
if (-not $HasAgentTool) {
    $MatcherArgs += '--skills-only'
}

$prevProjectDir = $env:CLAUDE_PROJECT_DIR
$env:CLAUDE_PROJECT_DIR = $ProjectRoot
$Msg = ""
try {
    $Msg = ($Prompt | & $PY $Matcher @MatcherArgs 2>$null) -join "`n"
} catch {
    $Msg = ""
} finally {
    if ($null -eq $prevProjectDir) {
        Remove-Item Env:CLAUDE_PROJECT_DIR -ErrorAction SilentlyContinue
    } else {
        $env:CLAUDE_PROJECT_DIR = $prevProjectDir
    }
}

if (-not $Msg) { exit 0 }
if (-not ($Msg -match '\S')) { exit 0 }

if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
    Emit-AdditionalContext $Msg 'SubagentStart'
} else {
    # Inline fallback envelope.
    $envelope = [ordered]@{
        hookSpecificOutput = [ordered]@{
            hookEventName     = 'SubagentStart'
            additionalContext = $Msg
        }
    }
    $json = $envelope | ConvertTo-Json -Compress -Depth 8
    Write-Output $json
}

exit 0
