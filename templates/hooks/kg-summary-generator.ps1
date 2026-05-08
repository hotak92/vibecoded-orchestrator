# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
if ($env:CLAUDE_CODE_DISABLE_AUTO_MEMORY) { exit 0 }
# kg-summary-generator.ps1
# PostToolUse hook: spawn a background agent to refresh KG node summaries.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Resolve-Path (Join-Path $ScriptDir "..\..")).Path }

$venvBase = if ($env:VCT_INSTALL_ROOT) { $env:VCT_INSTALL_ROOT } else { $ProjectRoot }
$venvPy = Join-Path $venvBase "claude_mcp_servers\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    $venvPy = Join-Path $venvBase "claude_mcp_servers/.venv/bin/python"
}
$generator = Join-Path $ProjectRoot ".claude/scripts/generate-kg-summary.py"

if (-not (Test-Path $venvPy)) { exit 0 }
if (-not (Test-Path $generator)) { exit 0 }

# Read stdin payload
$Input = ""
try { $Input = [Console]::In.ReadToEnd() } catch { }

$FilePath = ""
if ($env:CLAUDE_TOOL_ARG_FILE_PATH) {
    $FilePath = $env:CLAUDE_TOOL_ARG_FILE_PATH
} elseif ($Input -match "store_knowledge_node") {
    # Extract file_path via Python.
    $LibDir = Join-Path $ScriptDir "_lib"
    $FindPy = Join-Path $LibDir "find-python.ps1"
    if (Test-Path $FindPy) { . $FindPy }
    if ($PY) {
        $code = @'
import sys, json, re
try:
    d = json.load(sys.stdin)
    resp = d.get('tool_response', '')
    if isinstance(resp, str):
        m = re.search(r'absolute_path[":\s]+([^",}]+)', resp)
        if m:
            print(m.group(1).strip()); sys.exit(0)
    inp = d.get('tool_input', {})
    if isinstance(inp, str): inp = json.loads(inp)
    fp = inp.get('file_path', '')
    if fp: print(fp)
except Exception:
    pass
'@
        try {
            $FilePath = ($Input | & $PY -c $code 2>$null | Out-String).Trim()
        } catch { }
    }
}

if (-not $FilePath -or $FilePath -notlike "*knowledge/*" -or -not ($FilePath -like "*.md")) { exit 0 }

# Resolve to absolute path.
if (-not [System.IO.Path]::IsPathRooted($FilePath)) {
    $FilePath = Join-Path $ProjectRoot $FilePath
}
if (-not (Test-Path $FilePath)) { exit 0 }

# Debounce: skip if generated for this file in the last 60 seconds.
$Tmp = if ($env:TMPDIR) { $env:TMPDIR } elseif ($env:TEMP) { $env:TEMP } else { "C:\Windows\Temp" }
$DebounceDir = Join-Path $Tmp ".kg-summary-debounce"
if (-not (Test-Path $DebounceDir)) { New-Item -ItemType Directory -Path $DebounceDir -Force | Out-Null }
$md5 = [System.Security.Cryptography.MD5]::Create()
$bytes = [System.Text.Encoding]::UTF8.GetBytes($FilePath)
$hash = ($md5.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join ""
$Stamp = Join-Path $DebounceDir $hash
if (Test-Path $Stamp) {
    $age = ((Get-Date) - (Get-Item $Stamp).LastWriteTime).TotalSeconds
    if ($age -lt 60) { exit 0 }
}
New-Item -ItemType File -Path $Stamp -Force | Out-Null

$logsDir = Join-Path $ProjectRoot ".claude/logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }
$logFile = Join-Path $logsDir "kg-summary-generator.log"

Start-Process -FilePath $venvPy -ArgumentList @($generator, $FilePath) `
    -RedirectStandardOutput $logFile -RedirectStandardError $logFile `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null

Write-Output "KG summary generation queued for $(Split-Path $FilePath -Leaf)"
exit 0
