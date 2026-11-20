# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# session-worktree-reap.ps1 -- SessionEnd + SessionStart hook: the point where
# an isolated agent's UNCHANGED worktree is removed (v0.2.107 REAP-INTERIM).
# MUST MATCH session-worktree-reap.sh, which carries the full WHY:
# SubagentStop also fires on an INTERIM stop the payload cannot tell from a
# final one, so subagent-stop-reconcile only marks an unchanged worktree a
# reap candidate, and this hook removes candidates once no session can resume
# their agent.
#   SessionEnd   records that this session ended (Set-WtrecSessionEnded; a
#                marker only -- the harness budget is short).
#   SessionStart a resumed session withdraws its own end marker, then
#                Invoke-WtrecSweep removes the candidates of ENDED sessions
#                (and of sessions idle for days that never recorded an end),
#                never those of the current session, after re-checking that
#                each worktree is still unchanged. Each decision is logged to
#                .claude/logs/worktree-guard.jsonl.
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
$EventName = ""; $SessionId = ""; $Source = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -is [System.Management.Automation.PSCustomObject]) {
        if ($payload.hook_event_name -is [string]) { $EventName = $payload.hook_event_name }
        if ($payload.session_id -is [string]) { $SessionId = $payload.session_id }
        if ($payload.source -is [string]) { $Source = $payload.source }
    }
} catch { }

try {
    if ($EventName -ceq "SessionEnd") {
        Set-WtrecSessionEnded $ProjectRoot $SessionId
    } elseif ($EventName -ceq "SessionStart") {
        if ($Source -ceq "resume") { Clear-WtrecSessionEnded $ProjectRoot $SessionId }
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
