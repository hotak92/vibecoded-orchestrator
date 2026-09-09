# PowerShell wrapper for the KG duplicate-DETECTION report (Windows
# equivalent of kg-duplicates). v0.2.92 delivery audit m3 (R42: parity is
# achieved by WRITING the .ps1, never by narrowing the feature).
#
# Not to be confused with `kg-dedup`, which reconciles the same source
# file stored more than once. This one finds semantically similar but
# DISTINCT nodes (similarity + filename + title matching) and writes a
# report - it deletes nothing.
#
# Usage:
#   .\kg-duplicates.ps1                        # full scan, threshold 0.95
#   .\kg-duplicates.ps1 -threshold 0.90        # custom threshold
#   .\kg-duplicates.ps1 -output report.md      # custom report location
#
# (The script's own argparse uses `--threshold`; PowerShell freely passes
# single-dash tokens through `@args`, so both spellings reach it.)
#
# Exit codes: whatever `detect_duplicates.py` exits (0 = report written,
# 1 = the scan could not complete); 1 = REFUSAL - no Python environment with
# the dependencies this wrapper needs (wrapper-only).
#
# v0.2.94: the parenthetical used to add "the script never emits it for its
# own results", which stopped being true in v0.2.92 when a FAILED scan started
# exiting 1 rather than reporting a clean graph. A wrapper's documented exit
# ladder is part of its contract, so it is corrected rather than left standing.
#
# PARITY: this file follows the `kg-dedup` / `kg-sync` wrapper contract - the
# same venv tiers from the SAME home (`vct_venv_ladder.ps1`, v0.2.94), the same
# refusal shape on stderr, no bare-python fallback. The bash sibling
# `kg-duplicates` is the POSIX entry point and gates on the same modules.

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# The probe a candidate interpreter must pass, verbatim as it is executed.
# `weaviate` is the upstream client `detect_duplicates.py` imports at module
# level; `vco_lib` owns the named-vector slot resolution the scan needs to
# query a multi-vector collection at all (v0.2.94). Pre-v0.2.94 this gate
# named only `weaviate` - WEAKER than what the script requires, which is the
# shape of gate that lets a run die inside the script instead of refusing here.
$LadderImport = "import weaviate, vco_lib"

# The ladder lives in ONE home, shipped beside this wrapper (v0.2.94). A
# missing lib is a broken install, not a reason to fall back to a bare
# interpreter.
$LadderLib = Join-Path $ScriptDir "vct_venv_ladder.ps1"
if (-not (Test-Path $LadderLib)) {
    [Console]::Error.WriteLine("kg-duplicates: ERROR - missing $LadderLib")
    [Console]::Error.WriteLine("kg-duplicates: (broken install - re-run the orchestrator install)")
    [Console]::Error.WriteLine("$([char]0x26A0)$([char]0xFE0F)  kg-duplicates did NOT run (exit 1): its shared venv ladder is missing. See the lines above.")
    exit 1
}
. $LadderLib

$VenvPython = Resolve-VctLadderPython -ScriptDir $ScriptDir `
    -ProjectRoot $ProjectRoot -ImportProbe $LadderImport

# NO BARE `python` FALLBACK - a shipped component does not get a silent
# fallback (standing rule). Name every candidate and the remedy, exit 1.
# The refusal goes to STDERR (`>&2` in the bash siblings' shape); the host
# stream is invisible to 2>nul / CI capture / stderr-scraping consumers.
if (-not $VenvPython) {
    Write-VctLadderRefusal -Tool "kg-duplicates" -ImportProbe $LadderImport `
        -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
    [Console]::Error.WriteLine("kg-duplicates: Refusing to run with an unqualified interpreter - a")
    [Console]::Error.WriteLine("kg-duplicates: scan that cannot query the collection must never")
    [Console]::Error.WriteLine("kg-duplicates: report 'no duplicates'.")
    [Console]::Error.WriteLine("kg-duplicates: (exit 1 = did not run)")
    Write-VctLadderRefusalSummary -Tool "kg-duplicates" -ExitCode 1
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "detect_duplicates.py") @args
exit $LASTEXITCODE
