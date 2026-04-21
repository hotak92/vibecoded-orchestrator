# PowerShell wrapper for sync_knowledge_graph.py (Windows equivalent of kg-sync)
# Usage: .\kg-sync.ps1 FILE
#        .\kg-sync.ps1 --all

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$VenvPython = Join-Path $ProjectRoot "claude_mcp_servers\.venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "ERROR: venv not found at $VenvPython" -ForegroundColor Red
    Write-Host "Run install.ps1 first." -ForegroundColor Red
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "sync_knowledge_graph.py") @args
exit $LASTEXITCODE
