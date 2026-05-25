# PowerShell wrapper for `vco codegraph-diagram` — Phase 3 of the
# diagrams-integration plan. Cross-OS sibling of code-graph-to-mermaid
# (bash). Same flags, same exit codes, same JSON shape.
#
# Usage: .\code-graph-to-mermaid.ps1 <seed_symbol> [--hops N] [--scope ...]

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# Probe canonical .venv locations in priority order — see code-graph-query.ps1
# for the rationale (OSS install root precedes the legacy layout).
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

# PYTHONPATH so `python -m vco_lib.cli` resolves regardless of CWD.
$env:PYTHONPATH = "$ProjectRoot;$($env:PYTHONPATH)"

& $VenvPython -m vco_lib.cli codegraph-diagram @args
exit $LASTEXITCODE
