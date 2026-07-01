# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-start-isolation-check.ps1 — Windows sibling of
# subagent-start-isolation-check.sh. SubagentStart hook (Layer 0b,
# secondary / belt) for the worktree-isolation silent-fallback safeguard.
#
# When a subagent that requested `isolation: worktree` spawns and the
# payload exposes an isolation flag AND a cwd/worktree field, assert the cwd
# is a genuinely separate worktree; on a suspected violation (cwd resolves
# to the parent checkout toplevel) inject a LOUD additionalContext block.
# Cannot block (event is non-blocking) — pure loud-warn. NO-OPs gracefully
# when isolation not requested or no cwd is exposed.
#
# Cross-OS parity: the detection logic (isolation-flag synonyms + cwd
# synonyms + the cwd==toplevel violation test) MUST match
# subagent-start-isolation-check.sh. Keep them in lockstep.
#
# Always exits 0. Silent when isolation not requested or no cwd exposed.

# Scrub sensitive env vars before any subprocess spawning.
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot

# Stderr cap so a buggy iteration cannot reproduce the 2026-05-07 GUI freeze.
$StderrCap = Join-Path $ScriptDir "_lib/stderr-cap.ps1"
if (Test-Path $StderrCap) { . $StderrCap }

# Emit-AdditionalContext helper (preferred). Inline fallback below if missing.
$EmitHelper = Join-Path $ScriptDir "_lib/emit-context.ps1"
if (Test-Path $EmitHelper) { . $EmitHelper }

$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { exit 0 }

# Parse defensively. isolation-flag synonyms + cwd synonyms mirror the .sh.
$IsoFlag = $false
$CwdField = ""
$AgentId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        function Test-Truthy($v) {
            if ($v -is [bool]) { return $v }
            if ($v -is [string]) {
                return @('worktree','true','1','yes','isolated') -contains $v.Trim().ToLowerInvariant()
            }
            return $false
        }
        # Explicit `isolation` field == "worktree".
        if ($payload.PSObject.Properties['isolation']) {
            $iv = $payload.isolation
            if ($iv -is [string] -and $iv.Trim().ToLowerInvariant() -eq 'worktree') { $IsoFlag = $true }
            elseif (Test-Truthy $iv) { $IsoFlag = $true }
        }
        if (-not $IsoFlag) {
            foreach ($f in 'isolation_mode','worktree','isolated','worktree_isolation') {
                if ($payload.PSObject.Properties[$f] -and (Test-Truthy $payload.$f)) { $IsoFlag = $true; break }
            }
        }
        foreach ($f in 'cwd','worktree_path','working_directory','working_dir','dir') {
            if ($payload.PSObject.Properties[$f] -and $payload.$f) { $CwdField = [string]$payload.$f; break }
        }
        if ($payload.PSObject.Properties['agent_id'] -and $payload.agent_id) { $AgentId = [string]$payload.agent_id }
    }
} catch {
    exit 0
}

# No-op: isolation not requested, or no cwd exposed. Don't guess.
if (-not $IsoFlag) { exit 0 }
if (-not $CwdField) { exit 0 }

# Resolve the parent checkout toplevel (relative to project root).
$Toplevel = ""
if (Get-Command git -ErrorAction SilentlyContinue) {
    try { $Toplevel = (& git -C $ProjectRoot rev-parse --show-toplevel 2>$null | Select-Object -First 1) } catch { $Toplevel = "" }
}
if (-not $Toplevel) { exit 0 }

function Norm-Path([string]$p) {
    try { return [System.IO.Path]::GetFullPath($p) } catch { return $p }
}
$CwdAbs = Norm-Path $CwdField
$ToplevelAbs = Norm-Path $Toplevel

# Violation suspected only when cwd resolves to the parent toplevel itself.
if ($CwdAbs -ne $ToplevelAbs) { exit 0 }

$Msg = "WARNING: ISOLATION VIOLATION SUSPECTED - this subagent requested ``isolation: worktree`` but its cwd ($CwdAbs) IS the parent checkout. Any ``git commit`` from here lands on the PARENT branch (the 2026-06-30 silent-fallback footgun). BEFORE any git write: create your own worktree (``git worktree add <path> -b <branch>``) and ``cd`` into it, OR abort and report. Do NOT commit from the parent checkout."

if (-not ($Msg -match '\S')) { exit 0 }

if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
    Emit-AdditionalContext $Msg 'SubagentStart'
} else {
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
