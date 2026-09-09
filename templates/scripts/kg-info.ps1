# PowerShell wrapper for get_node_info.py (Windows equivalent of kg-info)
# Usage: .\kg-info.ps1 info "Node Title"
#        .\kg-info.ps1 connections "Node Title"
#
# Exit codes: whatever `get_node_info.py` exits; 1 = REFUSAL - no Python
# environment with the dependency this wrapper needs (wrapper-only).
#
# v0.2.94: this wrapper carried its OWN venv ladder - a weaker one than
# `kg-sync.ps1`'s (no $env:VCT_VENV tier, no .claude\env tier, no VCO-clone
# discriminator) - and printed its refusal to the HOST stream, which
# 2>nul / CI capture / stderr-scraping consumers never see. Ladder + refusal
# now come from the ONE home every KG wrapper shares.
#
# PARITY: this file and the bash sibling `kg-info` must stay in lockstep
# (same tiers, same gate, same refusal text, same forwarding).

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# The probe a candidate interpreter must pass, verbatim as it is executed.
# `get_node_info.py` hard-imports `weaviate` at module scope; its `vco_lib` /
# `weaviate_mcp` imports are function-local and guarded.
$LadderImport = "import weaviate, vco_lib"

# The ladder lives in ONE home, shipped beside this wrapper. A missing lib is
# a broken install, not a reason to fall back to a bare interpreter.
$LadderLib = Join-Path $ScriptDir "vct_venv_ladder.ps1"
if (-not (Test-Path $LadderLib)) {
    [Console]::Error.WriteLine("kg-info: ERROR - missing $LadderLib")
    [Console]::Error.WriteLine("kg-info: (broken install - re-run the orchestrator install)")
    [Console]::Error.WriteLine("$([char]0x26A0)$([char]0xFE0F)  kg-info did NOT run (exit 1): its shared venv ladder is missing. See the lines above.")
    exit 1
}
. $LadderLib

$VenvPython = Resolve-VctLadderPython -ScriptDir $ScriptDir `
    -ProjectRoot $ProjectRoot -ImportProbe $LadderImport

if (-not $VenvPython) {
    Write-VctLadderRefusal -Tool "kg-info" -ImportProbe $LadderImport `
        -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
    [Console]::Error.WriteLine("kg-info: Refusing to run with an unqualified interpreter - a")
    [Console]::Error.WriteLine("kg-info: lookup that cannot reach the collection must never")
    [Console]::Error.WriteLine("kg-info: report 'node not found'.")
    [Console]::Error.WriteLine("kg-info: (exit 1 = did not run)")
    Write-VctLadderRefusalSummary -Tool "kg-info" -ExitCode 1
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "get_node_info.py") @args
exit $LASTEXITCODE
