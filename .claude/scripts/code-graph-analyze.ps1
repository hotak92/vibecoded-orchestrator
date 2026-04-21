# PowerShell wrapper for analyze_code_graph.py (Windows equivalent of code-graph-analyze)
# Usage: .\code-graph-analyze.ps1 C:\path\to\repo [--project NAME] [--incremental]

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$VenvPython = Join-Path $ProjectRoot "claude_mcp_servers\.venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "ERROR: venv not found at $VenvPython" -ForegroundColor Red
    Write-Host "Run install.ps1 first." -ForegroundColor Red
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "analyze_code_graph.py") @args
exit $LASTEXITCODE
