# vco.ps1 — PowerShell sibling of scripts/vco.
#
# Thin wrapper around `python -m vco_lib.cli`. See `scripts/vco` for the
# rationale; this file exists so Windows users get the same entry-point.
#
# Usage:
#   pwsh scripts/vco.ps1 verify-pins
#   pwsh scripts/vco.ps1 verify-env-projection my-project
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$VenvPy = Join-Path $RepoRoot "claude_mcp_servers\.venv\Scripts\python.exe"
if (Test-Path $VenvPy) {
    $Py = $VenvPy
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Py = (Get-Command python).Source
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $Py = (Get-Command python3).Source
} else {
    Write-Error "[vco] no python interpreter found on PATH"
    exit 127
}

$env:PYTHONPATH = if ($env:PYTHONPATH) { "$RepoRoot;$($env:PYTHONPATH)" } else { $RepoRoot }
& $Py -m vco_lib.cli @args
exit $LASTEXITCODE
