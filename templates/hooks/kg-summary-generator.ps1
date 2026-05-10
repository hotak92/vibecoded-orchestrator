# parity-confirmation 2026-05-10: .sh sibling now uses _lib/find-python.sh; this .ps1 already used _lib/find-python.ps1 — true parity, no asymmetric fix.
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
if ($env:CLAUDE_CODE_DISABLE_AUTO_MEMORY) { exit 0 }

# VCO-CENTRALIZED-KG: write-side delegator (PR #171 / 0.1.7).
#   Calls .claude/scripts/generate-kg-summary.py to refresh
#   knowledge/.node_formats.json — operates on the project's OWN KG only
#   (per-node summary cache, not a Weaviate collection write either).
#   No multi-source fan-out involvement. VCT_KG_ACCESS_LIST is read-side
#   only; no centralization needed here.

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

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# The legacy $env:CLAUDE_TOOL_ARG_FILE_PATH env-var fallback was removed
# 2026-05-08 — that env var is NOT populated by Claude Code, so it
# always evaluated to empty and the fallback path silently skipped to
# the MCP branch. Verified via stdin-capture diagnostic.
$Input = ""
try { $Input = [Console]::In.ReadToEnd() } catch { }

$FilePath = ""
# Single Python extractor handles both Edit/Write (tool_input.file_path)
# and MCP store_knowledge_node (tool_response.absolute_path or
# tool_input.file_path). Branches by tool_name internally.
$LibDir = Join-Path $ScriptDir "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
if ($PY) {
    $code = @'
import sys, json, re
try:
    d = json.loads(sys.stdin.read())
    tool_name = d.get('tool_name', '') or ''
    inp = d.get('tool_input', {}) or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except Exception:
            inp = {}
    # Edit/Write path — file_path lives under tool_input
    if tool_name in ('Edit', 'Write'):
        print(inp.get('file_path', '') or '')
        sys.exit(0)
    # MCP store_knowledge_node — try tool_response.absolute_path first,
    # then fall back to tool_input.file_path
    if tool_name == 'mcp__weaviate-kg__store_knowledge_node':
        resp = d.get('tool_response', '')
        if isinstance(resp, dict):
            ap = resp.get('absolute_path', '')
            if ap:
                print(ap)
                sys.exit(0)
        elif isinstance(resp, str) and resp:
            m = re.search(r'absolute_path["\s:]+([^",}]+)', resp)
            if m:
                print(m.group(1).strip())
                sys.exit(0)
        print(inp.get('file_path', '') or '')
        sys.exit(0)
    # Unknown tool — fall through to nothing
    print('')
except Exception:
    print('')
'@
    try {
        $FilePath = ($Input | & $PY -c $code 2>$null | Out-String).Trim()
    } catch { }
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
