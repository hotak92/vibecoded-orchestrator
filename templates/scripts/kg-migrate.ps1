# PowerShell wrapper for migrate_to_vocabulary.py (Windows equivalent of kg-migrate)
# Usage: .\kg-migrate.ps1 --check
#        .\kg-migrate.ps1 --fix
#        .\kg-migrate.ps1 --interactive
#        .\kg-migrate.ps1 --file <path>
#
# Exit codes: whatever `migrate_to_vocabulary.py` exits; 1 = REFUSAL - no
# Python environment with the dependencies this wrapper needs (wrapper-only).
#
# v0.2.94: this wrapper carried its OWN venv ladder - a weaker one than
# `kg-sync.ps1`'s (no $env:VCT_VENV tier, no .claude\env tier, no VCO-clone
# discriminator), gated on `weaviate` alone (NOT enough for this script) and
# printed its refusal to the HOST stream, which 2>nul / CI capture /
# stderr-scraping consumers never see. Ladder + refusal now come from the ONE
# home every KG wrapper shares.
#
# PARITY: this file and the bash sibling `kg-migrate` must stay in lockstep
# (same tiers, same gate, same refusal text, same forwarding).

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# The probe a candidate interpreter must pass, verbatim as it is executed.
# `migrate_to_vocabulary.py` imports `sync_knowledge_graph` AT MODULE SCOPE,
# and that module hard-imports `weaviate_mcp.chunking` plus several `vco_lib`
# modules. A venv with `weaviate` alone passes the old check and then dies at
# import time inside the script.
$LadderImport = "import weaviate, weaviate_mcp, vco_lib"

# The ladder lives in ONE home, shipped beside this wrapper. A missing lib is
# a broken install, not a reason to fall back to a bare interpreter.
$LadderLib = Join-Path $ScriptDir "vct_venv_ladder.ps1"
if (-not (Test-Path $LadderLib)) {
    [Console]::Error.WriteLine("kg-migrate: ERROR - missing $LadderLib")
    [Console]::Error.WriteLine("kg-migrate: (broken install - re-run the orchestrator install)")
    [Console]::Error.WriteLine("$([char]0x26A0)$([char]0xFE0F)  kg-migrate did NOT run (exit 1): its shared venv ladder is missing. See the lines above.")
    exit 1
}
. $LadderLib

$VenvPython = Resolve-VctLadderPython -ScriptDir $ScriptDir `
    -ProjectRoot $ProjectRoot -ImportProbe $LadderImport

if (-not $VenvPython) {
    Write-VctLadderRefusal -Tool "kg-migrate" -ImportProbe $LadderImport `
        -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
    [Console]::Error.WriteLine("kg-migrate: Refusing to run with an unqualified interpreter - a")
    [Console]::Error.WriteLine("kg-migrate: migration that cannot validate against the vocabulary")
    [Console]::Error.WriteLine("kg-migrate: must never report 'no issues', let alone --fix.")
    [Console]::Error.WriteLine("kg-migrate: (exit 1 = did not run)")
    Write-VctLadderRefusalSummary -Tool "kg-migrate" -ExitCode 1
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "migrate_to_vocabulary.py") @args
exit $LASTEXITCODE
