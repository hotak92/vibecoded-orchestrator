# PowerShell wrapper for search_knowledge.py (Windows equivalent of kg-search)
# Usage: .\kg-search.ps1 search "query" [--type TYPE] [--tags TAGS]

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# Probe canonical .venv locations. Mirrors the .sh sibling's two-candidate
# probe: install-root/.venv (OSS layout) takes precedence over
# claude_mcp_servers/.venv (legacy private-Claude-orch layout). Without
# the OSS probe, Windows users on a fresh install hit "venv not found"
# even though install.ps1 created the venv at the install root.
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

& $VenvPython (Join-Path $ScriptDir "search_knowledge.py") @args
exit $LASTEXITCODE
