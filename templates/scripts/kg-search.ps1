# PowerShell wrapper for search_knowledge.py (Windows equivalent of kg-search)
# Usage: .\kg-search.ps1 search "query" [--type TYPE] [--tags TAGS]

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$VenvPython = Join-Path $ProjectRoot "claude_mcp_servers\.venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "ERROR: venv not found at $VenvPython" -ForegroundColor Red
    Write-Host "Run install.ps1 first." -ForegroundColor Red
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "search_knowledge.py") @args
exit $LASTEXITCODE
