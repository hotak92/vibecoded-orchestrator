# PowerShell wrapper for get_node_info.py (Windows equivalent of kg-info)
# Usage: .\kg-info.ps1 info "Node Title"
#        .\kg-info.ps1 connections "Node Title"

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$VenvPython = Join-Path $ProjectRoot "claude_mcp_servers\.venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "ERROR: venv not found at $VenvPython" -ForegroundColor Red
    Write-Host "Run install.ps1 first." -ForegroundColor Red
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "get_node_info.py") @args
exit $LASTEXITCODE
