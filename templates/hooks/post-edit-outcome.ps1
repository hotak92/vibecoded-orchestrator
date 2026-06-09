# V52-M (v0.2.52) — post-edit outcome event recorder
# OS-PARITY: ports the .sh sibling. Fires AFTER Edit OR Write tool
# executes. Emits an edit_outcome event with diff size + whether the
# file existed before. Pairing strategy: trainer joins on (session_id,
# file_path, ts_window) since pre-edit's task_id isn't shared via a
# state file (see .sh sibling for the rationale + future cleanup note).

foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN') {
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
$FilePath = ""
$OldLen = 0
$NewLen = 0
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name) { $ToolName = [string]$payload.tool_name }
        if ($payload.session_id) { $SessionId = [string]$payload.session_id }
        if ($payload.tool_input) {
            if ($payload.tool_input.file_path) { $FilePath = [string]$payload.tool_input.file_path }
            $oldStr = if ($payload.tool_input.old_string) { [string]$payload.tool_input.old_string } else { '' }
            $newStr = if ($payload.tool_input.new_string) { [string]$payload.tool_input.new_string } else { '' }
            $content = if ($payload.tool_input.content) { [string]$payload.tool_input.content } else { '' }
            if (-not $newStr -and $content) { $newStr = $content }
            $OldLen = $oldStr.Length
            $NewLen = $newStr.Length
        }
    }
} catch { }

if ($ToolName -ne "Edit" -and $ToolName -ne "Write") { exit 0 }
if (-not $FilePath) { exit 0 }
if (-not $SessionId) { $SessionId = "default" }

# === file_existed_before heuristic ===
# Edit always operates on existing files; Write can create or overwrite.
# For Write, query git ls-files; fail-open to 0 (new file) when git
# unavailable or path is outside any repo.
$FileExistedBefore = 1
if ($ToolName -eq "Write") {
    $FileExistedBefore = 0
    if (Get-Command git -ErrorAction SilentlyContinue) {
        try {
            $dir = Split-Path $FilePath -Parent
            $name = Split-Path $FilePath -Leaf
            git -C $dir ls-files --error-unmatch -- $name *>$null
            if ($LASTEXITCODE -eq 0) { $FileExistedBefore = 1 }
        } catch { }
    }
}

# === diff_size ===
$DiffSize = if ($ToolName -eq "Write") {
    $NewLen
} else {
    [Math]::Abs($NewLen - $OldLen)
}

$NowTsMs = [int64]((Get-Date) - (Get-Date "1970-01-01Z").ToUniversalTime()).TotalMilliseconds

# === Resolve venv + emit ===
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
$VenvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir

if ($VenvPy -and (Test-Path $VenvPy)) {
    $pyCode = @"
import os, json, sys, uuid
task_id = f'edit_outcome_{uuid.uuid4().hex[:8]}'

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
        event_type='edit_outcome',
        task_id=task_id,
        task_type='edit_outcome',
        payload={
            'tool_name': r'''$ToolName''',
            'file_path': r'''$FilePath''',
            'diff_size': $DiffSize,
            'file_existed_before': bool($FileExistedBefore),
            'ts_ms': $NowTsMs,
            'post_check': None,
        },
        session_id=r'''$SessionId''',
        project_id=project_id,
    )
except Exception:
    pass
"@
    try {
        $pyCode | & $VenvPy - 2>$null
    } catch { }
}

exit 0
