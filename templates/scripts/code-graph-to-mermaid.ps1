# PowerShell wrapper for `vco codegraph-diagram` — Phase 3 of the
# diagrams-integration plan. Cross-OS sibling of code-graph-to-mermaid
# (bash). Same flags, same exit codes, same JSON shape.
#
# Usage: .\code-graph-to-mermaid.ps1 <seed_symbol> [--hops N] [--scope ...]
#
# Exit codes: whatever `vco codegraph-diagram` exits; 1 = REFUSAL - no Python
# environment with the dependencies this wrapper needs (wrapper-only).
#
# v0.2.94: this wrapper carried its OWN two-layout venv probe (no $env:VCT_VENV
# tier, no .claude\env tier, no $env:VCT_INSTALL_ROOT tier, no VCO-clone
# discriminator) and printed its refusal to the HOST stream, which 2>nul / CI
# capture / stderr-scraping consumers never see. Ladder + refusal now come
# from the ONE home every KG/code-graph wrapper shares; the qualifying
# interpreter has `vco_lib` installed, so no PYTHONPATH crutch is needed or set.
#
# PARITY: this file and the bash sibling `code-graph-to-mermaid` must stay in
# lockstep (same tiers, same gate, same refusal text, same forwarding).

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# The probe a candidate interpreter must pass, verbatim as it is executed.
# `vco_lib.cli` imports `vco_lib.*` at MODULE scope; `codegraph-diagram`
# fetches its subgraph through the `weaviate` client (function-local import
# in `vco_lib.codegraph_to_mermaid.fetch_subgraph`) - without it there is no
# diagram, so its absence changes the ANSWER, not the speed.
$LadderImport = "import weaviate, vco_lib"

# The ladder lives in ONE home, shipped beside this wrapper. A missing lib is
# a broken install, not a reason to fall back to a bare interpreter.
$LadderLib = Join-Path $ScriptDir "vct_venv_ladder.ps1"
if (-not (Test-Path $LadderLib)) {
    [Console]::Error.WriteLine("code-graph-to-mermaid: ERROR - missing $LadderLib")
    [Console]::Error.WriteLine("code-graph-to-mermaid: (broken install - re-run the orchestrator install)")
    [Console]::Error.WriteLine("$([char]0x26A0)$([char]0xFE0F)  code-graph-to-mermaid did NOT run (exit 1): its shared venv ladder is missing. See the lines above.")
    exit 1
}
. $LadderLib

$VenvPython = Resolve-VctLadderPython -ScriptDir $ScriptDir `
    -ProjectRoot $ProjectRoot -ImportProbe $LadderImport

if (-not $VenvPython) {
    Write-VctLadderRefusal -Tool "code-graph-to-mermaid" -ImportProbe $LadderImport `
        -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
    [Console]::Error.WriteLine("code-graph-to-mermaid: Refusing to run with an unqualified interpreter -")
    [Console]::Error.WriteLine("code-graph-to-mermaid: a diagram that cannot reach the code graph is")
    [Console]::Error.WriteLine("code-graph-to-mermaid: not a diagram of it.")
    [Console]::Error.WriteLine("code-graph-to-mermaid: (exit 1 = did not run)")
    Write-VctLadderRefusalSummary -Tool "code-graph-to-mermaid" -ExitCode 1
    exit 1
}

& $VenvPython -m vco_lib.cli codegraph-diagram @args
exit $LASTEXITCODE
