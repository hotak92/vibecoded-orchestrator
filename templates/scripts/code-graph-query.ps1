# PowerShell wrapper for query_code_graph.py (Windows equivalent of code-graph-query)
# Usage: .\code-graph-query.ps1 search "query"
#        .\code-graph-query.ps1 similar "module.function"
#        .\code-graph-query.ps1 structure dependencies "target"

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$VenvPython = Join-Path $ProjectRoot "claude_mcp_servers\.venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "ERROR: venv not found at $VenvPython" -ForegroundColor Red
    Write-Host "Run install.ps1 first." -ForegroundColor Red
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "query_code_graph.py") @args
exit $LASTEXITCODE
