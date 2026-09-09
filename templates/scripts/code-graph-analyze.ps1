# PowerShell wrapper for analyze_code_graph.py (Windows equivalent of code-graph-analyze)
# Usage: .\code-graph-analyze.ps1 C:\path\to\repo [--project NAME] [--incremental]
#
# Exit codes: whatever `analyze_code_graph.py` exits; 1 = REFUSAL - no Python
# environment with the dependencies this wrapper needs (wrapper-only).
#
# RESILIENT INTERPRETER DISCOVERY (RT-4). The tiers live in ONE home
# (`vct_venv_ladder.ps1`, v0.2.94) shared with the bash sibling and every
# other KG/code-graph wrapper - a mirror is what let them drift:
#   1. $env:VCT_VENV              -- explicit override, venv dir OR the
#                                    interpreter itself (RT-4's remedy)
#   2. $env:VCT_INSTALL_ROOT\.venv                     -- launcher (canonical)
#   3. $env:VCT_INSTALL_ROOT\claude_mcp_servers\.venv  -- legacy launcher path
#   4. VCT_ORCHESTRATOR_ROOT from the project's .claude\env  -- durable tier
#   5. <ProjectRoot>\.venv / \claude_mcp_servers\.venv -- clone-relative, GATED
#
# Bug history:
#   - 2026-04-28 (407076a): added VCT_INSTALL_ROOT fallbacks so a
#     browse-registered project (no own venv) resolves the launcher venv.
#   - 2026-05-07: script-relative-FIRST activated the user's OWN project
#     venv (no weaviate-client) -> reorder canonical-first + validate.
#   - 2026-06-27 (RT-4): an installed shim hardcoded the removed legacy
#     `.../Claude/claude_mcp_servers/.venv/...` and exited 127. This
#     revision adds the missing $env:VCT_VENV tier + a dep validation gate
#     so a stale default is always recoverable.
#   - v0.2.94: the tiers moved to the shared ladder (which also gained the
#     durable .claude\env tier and the VCO-clone discriminator), the gate now
#     names `vco_lib` too - `analyze_code_graph.py` imports
#     `vco_lib.embedding_service` at module scope - and the refusal moved off
#     the HOST stream onto stderr, where 2>nul / CI capture can see it.
#
# PARITY: this file and the bash sibling `code-graph-analyze` must stay in
# lockstep (same tiers, same gate, same refusal text, same forwarding).

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# The probe a candidate interpreter must pass, verbatim as it is executed.
# `analyze_code_graph.py` imports `vco_lib.embedding_service` at MODULE scope
# (unguarded, v0.2.75+) and talks to Weaviate through the client. Gating on
# `weaviate` alone let a venv through that then died at import time.
$LadderImport = "import weaviate, weaviate_mcp, vco_lib"

# The ladder lives in ONE home, shipped beside this wrapper. A missing lib is
# a broken install, not a reason to fall back to a bare interpreter.
$LadderLib = Join-Path $ScriptDir "vct_venv_ladder.ps1"
if (-not (Test-Path $LadderLib)) {
    [Console]::Error.WriteLine("code-graph-analyze: ERROR - missing $LadderLib")
    [Console]::Error.WriteLine("code-graph-analyze: (broken install - re-run the orchestrator install)")
    [Console]::Error.WriteLine("$([char]0x26A0)$([char]0xFE0F)  code-graph-analyze did NOT run (exit 1): its shared venv ladder is missing. See the lines above.")
    exit 1
}
. $LadderLib

$VenvPython = Resolve-VctLadderPython -ScriptDir $ScriptDir `
    -ProjectRoot $ProjectRoot -ImportProbe $LadderImport

if (-not $VenvPython) {
    Write-VctLadderRefusal -Tool "code-graph-analyze" -ImportProbe $LadderImport `
        -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
    [Console]::Error.WriteLine("code-graph-analyze: Refusing to run with an unqualified interpreter -")
    [Console]::Error.WriteLine("code-graph-analyze: an analysis that cannot write the collection must")
    [Console]::Error.WriteLine("code-graph-analyze: never look like a build that produced nothing.")
    [Console]::Error.WriteLine("code-graph-analyze: (exit 1 = did not run)")
    Write-VctLadderRefusalSummary -Tool "code-graph-analyze" -ExitCode 1
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "analyze_code_graph.py") @args
exit $LASTEXITCODE
