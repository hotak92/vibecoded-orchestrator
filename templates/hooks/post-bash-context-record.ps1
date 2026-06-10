# V52-M (v0.2.52) — post-bash outcome event recorder
# OS-PARITY: ports the .sh sibling. Fires AFTER Bash tool executes.
# Re-derives cmd_hash from stdin, reads the state file written by
# pre-bash-context-inject.ps1, emits a bash_outcome event with the
# same task_id, deletes the state file (one-shot pairing).

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

$LibDir = Join-Path $ScriptDir "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

$ToolName = ""
$SessionId = ""
$Command = ""
$ToolResponse = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name) { $ToolName = [string]$payload.tool_name }
        if ($payload.session_id) { $SessionId = [string]$payload.session_id }
        if ($payload.tool_input -and $payload.tool_input.command) {
            $Command = [string]$payload.tool_input.command
        }
        if ($payload.tool_response) {
            if ($payload.tool_response -is [string]) {
                $ToolResponse = $payload.tool_response
            } else {
                $ToolResponse = ($payload.tool_response | ConvertTo-Json -Compress -Depth 8)
            }
        }
    }
} catch { }

if ($ToolName -ne "Bash") { exit 0 }
if (-not $SessionId) { $SessionId = "default" }
if (-not $Command) { exit 0 }

# Re-derive cmd_hash from the command (must match pre-bash's hash exactly)
$md5 = [System.Security.Cryptography.MD5]::Create()
$bytes = [System.Text.Encoding]::UTF8.GetBytes($Command)
$CmdHash = (($md5.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "").Substring(0, 16)

$StateDir = Join-Path $ProjectRoot ".claude/state"
$StateFile = Join-Path $StateDir "bash_task_${SessionId}_${CmdHash}.json"

# If pre-bash didn't fire (below threshold) → no state file → skip silently.
if (-not (Test-Path $StateFile)) { exit 0 }

# === Compute output length + exit-code heuristic + read state ===
$OutputLen = $ToolResponse.Length
$ExitCode = 0
if ($ToolResponse.StartsWith('<tool_use_error>') -or
    $ToolResponse.StartsWith('Error:') -or
    ($ToolResponse.Length -gt 0 -and $ToolResponse.Substring(0, [Math]::Min(200, $ToolResponse.Length)) -match 'Command exited with')) {
    $ExitCode = 1
}

$StateJson = ""
try { $StateJson = (Get-Content -Path $StateFile -Raw -ErrorAction Stop) } catch { }
$EndTsMs = [int64]((Get-Date) - (Get-Date "1970-01-01Z").ToUniversalTime()).TotalMilliseconds

# === Resolve venv + emit bash_outcome via Python helper ===
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
$VenvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir

if ($VenvPy -and (Test-Path $VenvPy)) {
    # Use a here-string Python emit. State JSON is read inside Python
    # to avoid PowerShell-to-Python escape headaches.
    $pyCode = @"
import json, os, sys
state_path = r'''$StateFile'''
try:
    with open(state_path, 'r') as f:
        state = json.load(f)
except Exception:
    state = {}
task_id = state.get('task_id', '')
start_ts_ms = int(state.get('start_ts_ms', 0))
end_ts_ms = $EndTsMs
duration_ms = max(0, end_ts_ms - start_ts_ms)

project_id = None
try:
    from vco_lib.project_config import resolve_for_project
    cfg = resolve_for_project(os.environ.get('CLAUDE_PROJECT_DIR', r'''$ProjectRoot'''))
    project_id = cfg.get('project_id') if isinstance(cfg, dict) else None
except Exception:
    pass

try:
    from claude_mcp_servers.rl_client.outcome_emit import emit_outcome_event
    emit_outcome_event(
        event_type='bash_outcome',
        task_id=task_id,
        task_type='bash_outcome',
        payload={
            'exit_code': $ExitCode,
            'output_len': $OutputLen,
            'duration_ms': duration_ms,
            'cmd_len': int(state.get('cmd_len', 0)),
        },
        session_id=state.get('session_id', r'''$SessionId'''),
        project_id=project_id,
    )
except Exception:
    pass
"@
    try {
        $pyCode | & $VenvPy - 2>$null
    } catch { }
}

# === One-shot pairing: delete the state file ===
Remove-Item $StateFile -Force -ErrorAction SilentlyContinue

exit 0
