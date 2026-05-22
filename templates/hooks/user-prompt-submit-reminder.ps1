# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# user-prompt-submit-reminder.ps1
# Context preservation reminder triggered on output volume.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

# Hook input contract (v2.1.x): JSON on stdin, NOT $env:CLAUDE_TOOL_NAME or
# $env:CLAUDE_SESSION_ID env vars. Drain stdin once and parse session_id
# from the JSON payload — session_id is the canonical per-conversation key.
# Falling back to "default" only on malformed JSON keeps concurrent
# Claude Code sessions on the same project from sharing state files
# (PR #176 cross-OS sweep — see knowledge/concepts/
# hook-session-id-stdin-pattern.md).
# UserPromptSubmit hooks don't see tool_name in the payload either, so
# per-tool accounting is moot under v2.1.x — kept as a defensive default
# in case a future runtime ever populates the env var. Today
# $env:CLAUDE_TOOL_NAME is always empty. Verified empirically 2026-05-08
# via stdin-capture diagnostic.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$ToolNameFromStdin = ""
$SessionId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name)  { $ToolNameFromStdin = [string]$payload.tool_name }
        if ($payload.session_id) { $SessionId = [string]$payload.session_id }
    }
} catch {
    # Empty/malformed stdin — fall through to defaults
}
if (-not $SessionId) { $SessionId = "default" }

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$ProjectName = Split-Path $ProjectDir -Leaf
$ContextFile = Join-Path $ProjectDir ".claude/CONTEXT_STATE.md"
$Tmp = if ($env:TMPDIR) { $env:TMPDIR } elseif ($env:TEMP) { $env:TEMP } else { "C:\Windows\Temp" }
$SessionLog = Join-Path $Tmp "claude-session-$ProjectName-$SessionId"

if (-not (Test-Path $SessionLog)) {
    Set-Content -Path $SessionLog -Value "0" -Encoding ascii
}

$WordsThisTurn = 0
$EffectiveToolName = if ($ToolNameFromStdin) { $ToolNameFromStdin } elseif ($env:CLAUDE_TOOL_NAME) { $env:CLAUDE_TOOL_NAME } else { "" }
if ($EffectiveToolName) {
    switch ($EffectiveToolName) {
        { $_ -in 'Read', 'Write', 'Edit' } { $WordsThisTurn = 1000 }
        'Bash' { $WordsThisTurn = 500 }
        'Task' { $WordsThisTurn = 2000 }
        default { $WordsThisTurn = 200 }
    }
}

$TotalWords = 0
try { $TotalWords = [int](Get-Content $SessionLog -Raw -ErrorAction Stop).Trim() } catch { $TotalWords = 0 }

if ($WordsThisTurn -gt 0) {
    $TotalWords += $WordsThisTurn
    Set-Content -Path $SessionLog -Value $TotalWords -Encoding ascii
}

if ($TotalWords -ge 200000) {
    $Marker = Join-Path $Tmp "claude-ctx-warn-$ProjectName-$SessionId"
    if (Test-Path $Marker) { exit 0 }
    Write-Output ""
    Write-Output "Session checkpoint (~300K tokens used)"
    Write-Output "   - /refresh-context - Update KG if topic shifted"
    Write-Output "   - Update CONTEXT_STATE.md with findings"
    Write-Output "   - Consider new session before context compression"
    Write-Output ""
    New-Item -ItemType File -Path $Marker -Force | Out-Null
    exit 0
}

# CONTEXT_STATE staleness — counter-based, one-shot per ~120K-word window
# since the LAST nudge or last actual edit (whichever is more recent).
#
# Replaces the prior time-based 30-min staleness check that fired 4-5x
# during long deep-work sessions. The new condition is "enough work has
# accumulated that a CONTEXT_STATE refresh is genuinely useful," not
# "the file is old."
#
# The staleness marker file is keyed by both ProjectName and SessionId
# so concurrent Claude Code sessions on the same project don't stomp on
# each other's counter (the same concurrency fix as PR #176 applied to
# 11 other hooks — see knowledge/concepts/hook-session-id-stdin-pattern.md).
$StalenessMarker = Join-Path $Tmp "claude-ctx-staleness-$ProjectName-$SessionId"
$LastFireWords = 0
if (Test-Path $StalenessMarker) {
    try { $LastFireWords = [int](Get-Content $StalenessMarker -Raw -ErrorAction Stop).Trim() } catch { $LastFireWords = 0 }
    if (Test-Path $ContextFile) {
        $CtxMtime = (Get-Item $ContextFile -ErrorAction SilentlyContinue).LastWriteTime.ToFileTime()
        $MarkerMtime = (Get-Item $StalenessMarker -ErrorAction SilentlyContinue).LastWriteTime.ToFileTime()
        if ($CtxMtime -gt $MarkerMtime) {
            Set-Content -Path $StalenessMarker -Value $TotalWords -Encoding ascii
            $LastFireWords = $TotalWords
        }
    }
}
$Delta = $TotalWords - $LastFireWords
if ($Delta -ge 120000) {
    Write-Output ""
    Write-Output "~${Delta} words of work since last CONTEXT_STATE update."
    Write-Output "   Append a 1-2 line progress note now (what shipped + what's next),"
    Write-Output "   before context grows further or /compact lands."
    Write-Output ""
    Set-Content -Path $StalenessMarker -Value $TotalWords -Encoding ascii
}
exit 0
