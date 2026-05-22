# PowerShell wrapper for query_code_graph.py (Windows equivalent of code-graph-query)
# Usage: .\code-graph-query.ps1 search "query"
#        .\code-graph-query.ps1 similar "module.function"
#        .\code-graph-query.ps1 structure dependencies "target"

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

& $VenvPython (Join-Path $ScriptDir "query_code_graph.py") @args
exit $LASTEXITCODE
