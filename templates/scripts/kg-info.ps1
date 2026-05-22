# PowerShell wrapper for get_node_info.py (Windows equivalent of kg-info)
# Usage: .\kg-info.ps1 info "Node Title"
#        .\kg-info.ps1 connections "Node Title"

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# Probe canonical .venv locations in priority order — see kg-search.ps1
# for rationale (OSS install root takes precedence over the legacy
# claude_mcp_servers/.venv layout).
$VenvPython = $null
foreach ($candidate in @(
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    (Join-Path $ProjectRoot "claude_mcp_servers\.venv\Scripts\python.exe")
)) {
    if (Test-Path $candidate) { $VenvPython = $candidate; break }
}

if (-not $VenvPython) {
    Write-Host "ERROR: venv not found. Probed:" -ForegroundColor Red
    Write-Host "  $ProjectRoot\.venv\Scripts\python.exe" -ForegroundColor Red
    Write-Host "  $ProjectRoot\claude_mcp_servers\.venv\Scripts\python.exe" -ForegroundColor Red
    Write-Host "Run install.ps1 first." -ForegroundColor Red
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "get_node_info.py") @args
exit $LASTEXITCODE
