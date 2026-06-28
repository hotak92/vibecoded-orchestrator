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

try {
    $env:CLAUDE_PROJECT_DIR = $ProjectRoot
    & $VenvPy $Drain --session-id $SessionId --transcript-path $TranscriptPath *> $null
} catch { }

exit 0
