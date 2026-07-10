# Stop-hook deferred-citation drain (F-QUEUE, v0.2.70)
# OS-PARITY: ports the .sh sibling. Fires at turn-end (Stop). Reads
# session_id + transcript_path from stdin JSON and runs the python drain,
# which recovers hook-path RL citations the in-process monitor never could.
#
# ACCUMULATE-DON'T-DROP: the drain computes+writes ONLY when a pending task's
# cumulative answer window reaches the token gate; below-gate windows are left
# for the next Stop. Soft-fail throughout; always exit 0.

foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

$SessionId = ""
$TranscriptPath = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.session_id) { $SessionId = [string]$payload.session_id }
        if ($payload.transcript_path) { $TranscriptPath = [string]$payload.transcript_path }
    }
} catch { }

# Resolve the project venv python (the drain imports claude_mcp_servers.*).
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
$VenvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir

$Drain = Join-Path $ProjectRoot "claude_mcp_servers/scripts/rl_drain_citations.py"
if (-not (Test-Path $Drain)) { exit 0 }
if (-not $VenvPy -or -not (Test-Path $VenvPy)) { exit 0 }

# v0.2.76 P5 (hook-latency): DETACH the drain via Start-Process so its
# answer-window embed COMPUTE + telemetry write NEVER block the Stop return.
# Previously this ran the drain SYNCHRONOUSLY (`& $VenvPy ... *> $null`), so
# the Stop hook blocked for the whole drain. The drain is fire-and-forget by
# design: no consumer reads its result within this Stop event, and the RL
# call-sequence is assigned + frozen UPSTREAM in the MCP subprocess
# (rl_state.next_rl_call_seq) at retrieval time -- the drain only READS the
# staged seq to locate the transcript position, so detaching cannot reorder
# staged-seq vs monitor-seq. MUST MATCH the .sh sibling's setsid/nohup detach.
#
# Errors stay OBSERVABLE: redirect the detached process's stdout/stderr to a
# per-run log under .claude/logs/ (overwritten each Stop, so bounded) rather
# than $null. rl_drain_citations soft-fails internally and prints a
# "soft-fail (...)" line on any exception, which lands in the log.
$DrainLog = Join-Path $ProjectRoot ".claude/logs/rl_drain_citations.log"
try { New-Item -ItemType Directory -Force -Path (Split-Path $DrainLog) -ErrorAction SilentlyContinue | Out-Null } catch { }
try {
    $env:CLAUDE_PROJECT_DIR = $ProjectRoot
    Start-Process -FilePath $VenvPy `
        -ArgumentList @($Drain, "--session-id", $SessionId, "--transcript-path", $TranscriptPath) `
        -WindowStyle Hidden `
        -RedirectStandardOutput $DrainLog `
        -RedirectStandardError "$DrainLog.err" `
        -ErrorAction SilentlyContinue | Out-Null
} catch { }

exit 0
