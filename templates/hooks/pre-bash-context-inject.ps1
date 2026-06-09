# V52-M (v0.2.52) — pre-bash KG context injection
# OS-PARITY: ports the .sh sibling. Fires BEFORE Bash tool executes
# when the command length > VCT_BASH_KG_THRESHOLD_CHARS (default 500).
# Mirrors pre-edit-context-inject.ps1's shape (stdin parse → emit
# additionalContext JSON envelope). State file in .claude/state/
# pairs this pre-event with post-bash-context-record.ps1.

foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

. "$PSScriptRoot/_lib/stderr-cap.ps1"
if (Test-Path "$PSScriptRoot/_lib/emit-context.ps1") {
    . "$PSScriptRoot/_lib/emit-context.ps1"
}

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$ToolName = ""
$ToolArgs = $null
$SessionId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name)   { $ToolName = [string]$payload.tool_name }
        if ($payload.tool_input)  { $ToolArgs = $payload.tool_input }
        if ($payload.session_id)  { $SessionId = [string]$payload.session_id }
    }
} catch { }

if ($ToolName -ne "Bash") { exit 0 }

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$LibDir = Join-Path $ScriptDir "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

if (-not $SessionId) { $SessionId = "default" }
if ($SessionId -and $SessionId -ne "default") {
    $env:VCT_SESSION_ID = $SessionId
}

# Extract the bash command from tool_input.command
$Command = ""
if ($ToolArgs -and $ToolArgs.command) { $Command = [string]$ToolArgs.command }
if (-not $Command) { exit 0 }

# === Threshold gate ===
# User-locked answer to Q6 (2026-06-09): fixed 500 chars threshold,
# with VCT_BASH_KG_THRESHOLD_CHARS env override for power users.
$Threshold = 500
if ($env:VCT_BASH_KG_THRESHOLD_CHARS) {
    try { $Threshold = [int]$env:VCT_BASH_KG_THRESHOLD_CHARS } catch { $Threshold = 500 }
}
$CmdLen = $Command.Length
if ($CmdLen -lt $Threshold) { exit 0 }

# === Compute deterministic cmd hash for state-file pairing ===
$md5 = [System.Security.Cryptography.MD5]::Create()
$bytes = [System.Text.Encoding]::UTF8.GetBytes($Command)
$CmdHash = (($md5.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "").Substring(0, 16)

# === Write pre-bash state file for post-bash to pair with ===
$StateDir = Join-Path $ProjectRoot ".claude/state"
if (-not (Test-Path $StateDir)) {
    New-Item -ItemType Directory -Path $StateDir -Force -ErrorAction SilentlyContinue | Out-Null
}
$StateFile = Join-Path $StateDir "bash_task_${SessionId}_${CmdHash}.json"

# Generate task_id; same hex8 shape as rl_kg_search.py's pre_edit_* keys.
$TaskHex = ([guid]::NewGuid().ToString("N")).Substring(0, 8)
$TaskId = "pre_bash_$TaskHex"
$StartTsMs = [int64]((Get-Date) - (Get-Date "1970-01-01Z").ToUniversalTime()).TotalMilliseconds

$state = @{
    task_id = $TaskId
    start_ts_ms = $StartTsMs
    session_id = $SessionId
    cmd_hash = $CmdHash
    cmd_len = $CmdLen
}
try {
    $state | ConvertTo-Json -Compress | Set-Content -Path $StateFile -Encoding UTF8 -NoNewline
} catch { }

# v0.2.29 GC: prune state files older than 1 day.
Get-ChildItem -File $StateDir -Filter "bash_task_*.json" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# === Resolve venv for rl_kg_search.py subprocess ===
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
$VenvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir

# === Run KG search using command as query ===
# Truncate to ~500 chars so the embedder doesn't see kilobytes of input.
$Query = if ($Command.Length -gt 500) { $Command.Substring(0, 500) } else { $Command }

$KgTmp = New-TemporaryFile
$RlScript = Join-Path $ProjectRoot "claude_mcp_servers/scripts/rl_kg_search.py"
if ($VenvPy -and (Test-Path $VenvPy) -and (Test-Path $RlScript)) {
    try {
        & $VenvPy $RlScript $Query --limit 1 --hook-format 2>$null | Select-Object -First 40 | Set-Content -Path $KgTmp.FullName
    } catch { }
}

$KgResult = ""
try { $KgResult = (Get-Content -Path $KgTmp.FullName -Raw -ErrorAction Stop) } catch { }
Remove-Item $KgTmp.FullName -Force -ErrorAction SilentlyContinue

function Emit-ContextJson([string]$ctx) {
    if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
        Emit-AdditionalContext $ctx 'PreToolUse'
    }
}

# === Only output if we found something ===
$trimmed = ($KgResult -replace '\s+', '')
if ($trimmed) {
    $firstLine = $Command -split "`n" | Select-Object -First 1
    if ($firstLine.Length -gt 80) { $firstLine = $firstLine.Substring(0, 80) }
    $out = [System.Text.StringBuilder]::new()
    [void]$out.AppendLine("[Pre-bash context for: ${firstLine}]:")
    [void]$out.AppendLine("")
    [void]$out.Append($KgResult)
    [void]$out.AppendLine("")
    Emit-ContextJson $out.ToString()
}

exit 0
