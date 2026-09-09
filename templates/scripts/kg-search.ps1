# PowerShell wrapper for search_knowledge.py (Windows equivalent of kg-search)
# Usage: .\kg-search.ps1 search "query" [--type TYPE] [--tags TAGS]
#
# Exit codes: whatever `search_knowledge.py` exits; 1 = REFUSAL - no Python
# environment with the dependency this wrapper needs (wrapper-only).
#
# v0.2.94: this wrapper carried its OWN venv ladder - a weaker one than
# `kg-sync.ps1`'s (no $env:VCT_VENV tier, no .claude\env tier, no VCO-clone
# discriminator) - and printed its refusal to the HOST stream, which
# 2>nul / CI capture / stderr-scraping consumers never see. Ladder + refusal
# now come from the ONE home every KG wrapper shares.
#
# PARITY: this file and the bash sibling `kg-search` must stay in lockstep
# (same tiers, same gate, same refusal text, same forwarding).

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# The probe a candidate interpreter must pass, verbatim as it is executed.
# `search_knowledge.py` hard-imports `weaviate` at module scope; every one of
# its `vco_lib` / `weaviate_mcp` imports is function-local and guarded, with a
# documented legacy fallback. Name what the script actually requires.
$LadderImport = "import weaviate, vco_lib"

# The ladder lives in ONE home, shipped beside this wrapper. A missing lib is
# a broken install, not a reason to fall back to a bare interpreter.
$LadderLib = Join-Path $ScriptDir "vct_venv_ladder.ps1"
if (-not (Test-Path $LadderLib)) {
    [Console]::Error.WriteLine("kg-search: ERROR - missing $LadderLib")
    [Console]::Error.WriteLine("kg-search: (broken install - re-run the orchestrator install)")
    [Console]::Error.WriteLine("$([char]0x26A0)$([char]0xFE0F)  kg-search did NOT run (exit 1): its shared venv ladder is missing. See the lines above.")
    exit 1
}
. $LadderLib

$VenvPython = Resolve-VctLadderPython -ScriptDir $ScriptDir `
    -ProjectRoot $ProjectRoot -ImportProbe $LadderImport

if (-not $VenvPython) {
    Write-VctLadderRefusal -Tool "kg-search" -ImportProbe $LadderImport `
        -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
    [Console]::Error.WriteLine("kg-search: Refusing to run with an unqualified interpreter - a")
    [Console]::Error.WriteLine("kg-search: search that cannot reach the collection must never")
    [Console]::Error.WriteLine("kg-search: report 'no results'.")
    [Console]::Error.WriteLine("kg-search: (exit 1 = did not run)")
    Write-VctLadderRefusalSummary -Tool "kg-search" -ExitCode 1
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "search_knowledge.py") @args
exit $LASTEXITCODE
