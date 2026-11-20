# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# session-worktree-reap.ps1 -- SessionStart hook: the point where an isolated
# agent's worktree is removed (v0.2.107 REAP-INTERIM; owner rulings
# 2026-11-20). MUST MATCH session-worktree-reap.sh, which carries the full
# WHY: SubagentStop also fires on an INTERIM stop the payload cannot tell from
# a final one, so subagent-stop-reconcile only marks a worktree, and this
# hook's Invoke-WtrecSweep removes an UNCHANGED candidate idle for 7 days and
# a KEPT worktree whose commits are merged into the main checkout's current
# branch and that is clean (with the branch VCO created for it), never one of
# the starting session, after re-checking every condition; any doubt keeps
# it. Each decision is logged to .claude/logs/worktree-guard.jsonl.
# Prints NOTHING on stdout (SessionStart stdout becomes model context);
# always exits 0.

# Canonical list: _lib/scrub-env.ps1; the inline list is the fallback for a missing lib.
if ([System.IO.File]::Exists("$PSScriptRoot/_lib/scrub-env.ps1")) { . "$PSScriptRoot/_lib/scrub-env.ps1"; Invoke-VctScrubSecretEnv } else {
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
. "$PSScriptRoot/_lib/hook-stdin.ps1"  # UTF-8 payload read + UTF-8 output (before stderr-cap)
. "$PSScriptRoot/_lib/stderr-cap.ps1"

$WtrecHelper = Join-Path $PSScriptRoot "_lib/worktree-records.ps1"
if (-not (Test-Path -LiteralPath $WtrecHelper)) { exit 0 }
. $WtrecHelper
# Project root: the ONE rule (_lib/project-root.ps1).
. (Join-Path $PSScriptRoot "_lib/project-root.ps1")
$ProjectRoot = Get-VcoProjectRoot
if (-not $ProjectRoot) { exit 0 }

$HookStdin = ""
try { $HookStdin = Read-VcoHookStdin } catch { }
$EventName = ""; $SessionId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -is [System.Management.Automation.PSCustomObject]) {
        if ($payload.hook_event_name -is [string]) { $EventName = $payload.hook_event_name }
        if ($payload.session_id -is [string]) { $SessionId = $payload.session_id }
    }
} catch { }

try {
    if ($EventName -ceq "SessionStart") {
        $log = Join-Path $ProjectRoot ".claude/logs/worktree-guard.jsonl"
        foreach ($r in @(Invoke-WtrecSweep $ProjectRoot $SessionId)) {
            $agent = [string]$r.Name
            if ($agent.StartsWith("agent-")) { $agent = $agent.Substring(6) }
            Write-WtrecReapLog $log "session-worktree-reap" $agent ([string]$r.Decision) ([string]$r.Reason) ([string]$r.Path)
            if ($r.Decision -eq "kept") {
                [Console]::Error.WriteLine("session-worktree-reap: worktree $($r.Path) KEPT ($($r.Reason)) - review, merge or remove it")
            }
        }
    }
} catch { }
exit 0
