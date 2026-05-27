# PowerShell wrapper for query_code_graph.py (Windows equivalent of code-graph-query)
# Usage: .\code-graph-query.ps1 search "query"
#        .\code-graph-query.ps1 similar "module.function"
#        .\code-graph-query.ps1 structure dependencies "target"
#
# v0.2.37 (Gap 6b + 6c): backports the validate-has-weaviate-client
# pattern from the bash sibling AND sets PYTHONPATH so
# `query_code_graph.py` can import the `claude_mcp_servers` package.
# See `code-graph-query` (bash sibling) for the full rationale.

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

function Test-VenvHasCodeGraphDeps {
    param([string]$PythonExe)
    if (-not (Test-Path $PythonExe)) { return $false }
    & $PythonExe -c "import weaviate" 2>$null
    return ($LASTEXITCODE -eq 0)
}

$Candidates = @(
    $(if ($env:VCT_INSTALL_ROOT) { Join-Path $env:VCT_INSTALL_ROOT ".venv\Scripts\python.exe" }),
    $(if ($env:VCT_INSTALL_ROOT) { Join-Path $env:VCT_INSTALL_ROOT "claude_mcp_servers\.venv\Scripts\python.exe" }),
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    (Join-Path $ProjectRoot "claude_mcp_servers\.venv\Scripts\python.exe")
)

$VenvPython = $null
foreach ($cand in $Candidates) {
    if ($cand -and (Test-Path $cand) -and (Test-VenvHasCodeGraphDeps $cand)) {
        $VenvPython = $cand
        break
    }
}

if (-not $VenvPython) {
    Write-Host "ERROR: no venv with weaviate-client installed. Probed:" -ForegroundColor Red
    foreach ($cand in $Candidates) {
        if ($cand) { Write-Host "  - $cand" -ForegroundColor Red }
    }
    Write-Host "Run install.ps1 first, or set VCT_INSTALL_ROOT to point at an installed orchestrator clone." -ForegroundColor Red
    exit 1
}

# Resolve orchestrator root for PYTHONPATH. Prefer $env:VCT_ORCHESTRATOR_ROOT
# (canonical, written into .claude/env by install-bundle as of v0.2.37
# Gap 6a), fall back to $env:VCT_INSTALL_ROOT (legacy alias), then to
# script-relative paths.
$OrchRoot = ""
if ($env:VCT_ORCHESTRATOR_ROOT -and (Test-Path (Join-Path $env:VCT_ORCHESTRATOR_ROOT "claude_mcp_servers"))) {
    $OrchRoot = $env:VCT_ORCHESTRATOR_ROOT
} elseif ($env:VCT_INSTALL_ROOT -and (Test-Path (Join-Path $env:VCT_INSTALL_ROOT "claude_mcp_servers"))) {
    $OrchRoot = $env:VCT_INSTALL_ROOT
} elseif (Test-Path (Join-Path $ProjectRoot "claude_mcp_servers")) {
    $OrchRoot = $ProjectRoot
}

if ($OrchRoot) {
    $McpServersDir = Join-Path $OrchRoot "claude_mcp_servers"
    if ($env:PYTHONPATH) {
        $env:PYTHONPATH = "$McpServersDir;$env:PYTHONPATH"
    } else {
        $env:PYTHONPATH = $McpServersDir
    }
}

& $VenvPython (Join-Path $ScriptDir "query_code_graph.py") @args
exit $LASTEXITCODE
