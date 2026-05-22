# PowerShell wrapper for analyze_code_graph.py (Windows equivalent of code-graph-analyze)
# Usage: .\code-graph-analyze.ps1 C:\path\to\repo [--project NAME] [--incremental]
#
# Probe candidate venvs in canonical-first order. install_root\.venv is
# where install.py creates it for OSS vco installs; claude_mcp_servers\.venv
# is the legacy path from the private Claude orchestrator setup.
#
# When the project was registered against an existing folder via the
# launcher's "Browse" flow, the project itself has no venv. The launcher
# passes its own install root as VCT_INSTALL_ROOT — that's where
# weaviate-client + the analyzer deps actually live. Mirrors the bash
# fix landed in commit 407076a (2026-04-28).

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# Candidate venv locations, in priority order.
$Candidates = @(
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),                       # OSS install root
    (Join-Path $ProjectRoot "claude_mcp_servers\.venv\Scripts\python.exe"),    # legacy Claude orch
    $(if ($env:VCT_INSTALL_ROOT) { Join-Path $env:VCT_INSTALL_ROOT ".venv\Scripts\python.exe" }),
    $(if ($env:VCT_INSTALL_ROOT) { Join-Path $env:VCT_INSTALL_ROOT "claude_mcp_servers\.venv\Scripts\python.exe" })
)

$VenvPython = $null
foreach ($cand in $Candidates) {
    if ($cand -and (Test-Path $cand)) {
        $VenvPython = $cand
        break
    }
}

if (-not $VenvPython) {
    Write-Host "ERROR: no venv found. Searched:" -ForegroundColor Red
    foreach ($cand in $Candidates) {
        if ($cand) { Write-Host "  - $cand" -ForegroundColor Red }
    }
    Write-Host "Run install.ps1 first, or set VCT_INSTALL_ROOT." -ForegroundColor Red
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "analyze_code_graph.py") @args
exit $LASTEXITCODE
