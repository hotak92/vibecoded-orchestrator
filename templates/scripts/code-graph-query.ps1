# PowerShell wrapper for query_code_graph.py (Windows equivalent of code-graph-query)
# Usage: .\code-graph-query.ps1 search "query"
#        .\code-graph-query.ps1 similar "module.function"
#        .\code-graph-query.ps1 structure dependencies "target"
#
# Exit codes: whatever `query_code_graph.py` exits; 1 = REFUSAL - no Python
# environment with the dependencies this wrapper needs (wrapper-only).
#
# v0.2.37 (Gap 6c): sets PYTHONPATH so `query_code_graph.py` can import the
# `claude_mcp_servers` package. See `code-graph-query` (bash sibling).
#
# v0.2.94: this wrapper carried its OWN venv ladder - a weaker one than
# `kg-sync.ps1`'s (no $env:VCT_VENV tier, no .claude\env tier, no VCO-clone
# discriminator), gated on `weaviate` alone (not enough since
# `query_code_graph.py` began importing `vco_lib` at module scope) and printed
# its refusal to the HOST stream, which 2>nul / CI capture / stderr-scraping
# consumers never see. Ladder + refusal now come from the ONE home every
# KG/code-graph wrapper shares.
#
# PARITY: this file and the bash sibling `code-graph-query` must stay in
# lockstep (same tiers, same gate, same refusal text, same PYTHONPATH order,
# same forwarding).

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# The probe a candidate interpreter must pass, verbatim as it is executed.
# `query_code_graph.py` imports `vco_lib.codegraph_references` and
# `vco_lib.paths` at MODULE scope (unguarded), and talks to Weaviate through
# the client.
$LadderImport = "import weaviate, weaviate_mcp, vco_lib"

# Resolve orchestrator root for PYTHONPATH BEFORE probing. Prefer
# $env:VCT_ORCHESTRATOR_ROOT (canonical, written into .claude\env by
# install-bundle as of v0.2.37 Gap 6a), fall back to $env:VCT_INSTALL_ROOT
# (legacy alias), then to script-relative paths.
#
# v0.2.94: this block used to run AFTER the venv probe, so the probe answered
# a question the script never faces - `weaviate_mcp` is importable via this
# PYTHONPATH on installs where it is not pip-installed. PARITY: the bash
# sibling sets it in the same order.
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

# The ladder lives in ONE home, shipped beside this wrapper. A missing lib is
# a broken install, not a reason to fall back to a bare interpreter.
$LadderLib = Join-Path $ScriptDir "vct_venv_ladder.ps1"
if (-not (Test-Path $LadderLib)) {
    [Console]::Error.WriteLine("code-graph-query: ERROR - missing $LadderLib")
    [Console]::Error.WriteLine("code-graph-query: (broken install - re-run the orchestrator install)")
    [Console]::Error.WriteLine("$([char]0x26A0)$([char]0xFE0F)  code-graph-query did NOT run (exit 1): its shared venv ladder is missing. See the lines above.")
    exit 1
}
. $LadderLib

$VenvPython = Resolve-VctLadderPython -ScriptDir $ScriptDir `
    -ProjectRoot $ProjectRoot -ImportProbe $LadderImport

if (-not $VenvPython) {
    Write-VctLadderRefusal -Tool "code-graph-query" -ImportProbe $LadderImport `
        -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
    [Console]::Error.WriteLine("code-graph-query: Refusing to run with an unqualified interpreter -")
    [Console]::Error.WriteLine("code-graph-query: a query that cannot reach the code graph must")
    [Console]::Error.WriteLine("code-graph-query: never report 'no matches'.")
    [Console]::Error.WriteLine("code-graph-query: (exit 1 = did not run)")
    Write-VctLadderRefusalSummary -Tool "code-graph-query" -ExitCode 1
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "query_code_graph.py") @args
exit $LASTEXITCODE
