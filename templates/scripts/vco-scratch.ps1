# PowerShell wrapper for the per-project scratch directory (Windows
# equivalent of vco-scratch). v0.2.104.
#
# Agents and lanes put disposable files here instead of %TEMP%:
#   <project>\.claude\state\scratch\<session-or-lane-id>\
# It is private (owner-only ACL), git-excluded, transient to the code graph,
# and TTL-cleaned by VCO at session start (VCO_SCRATCH_TTL_HOURS, default
# 168 h, 0 = keep). Never delete recursively outside it.
#
# Usage:
#   .\vco-scratch.ps1 path [--name LANE]   # create + print the dir
#   .\vco-scratch.ps1 report [--json]      # count, size, oldest
#   .\vco-scratch.ps1 sweep [--json]       # TTL sweep now
#
# Exit codes: whatever `python -m vco_lib.scratch_dir` exits (0 ok, 1 refused,
# 2 usage); 1 = REFUSAL - no Python environment with vco_lib (wrapper-only).
# Every rule lives in ONE home (`vco_lib/scratch_dir.py`); this wrapper is a
# dumb forwarder that pins the project it is shipped in.
#
# PARITY: this file and the bash sibling `vco-scratch` must stay in lockstep
# (same tiers, same refusal text, same forwarding).

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# The probe a candidate interpreter must pass, verbatim as it is executed.
# `vco_lib.scratch_dir` needs only the standard library plus vco_lib itself.
$LadderImport = "import vco_lib"

# The ladder lives in ONE home, shipped beside this wrapper. A missing lib is
# a broken install, not a reason to fall back to a bare interpreter.
$LadderLib = Join-Path $ScriptDir "vct_venv_ladder.ps1"
if (-not (Test-Path $LadderLib)) {
    [Console]::Error.WriteLine("vco-scratch: ERROR - missing $LadderLib")
    [Console]::Error.WriteLine("vco-scratch: (broken install - re-run the orchestrator install)")
    [Console]::Error.WriteLine("$([char]0x26A0)$([char]0xFE0F)  vco-scratch did NOT run (exit 1): its shared venv ladder is missing. See the lines above.")
    exit 1
}
. $LadderLib

$VenvPython = Resolve-VctLadderPython -ScriptDir $ScriptDir `
    -ProjectRoot $ProjectRoot -ImportProbe $LadderImport

# NO BARE `python` FALLBACK - a shipped component does not get a silent
# fallback (standing rule). PARITY: the bash sibling refuses identically.
if (-not $VenvPython) {
    Write-VctLadderRefusal -Tool "vco-scratch" -ImportProbe $LadderImport `
        -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
    [Console]::Error.WriteLine("vco-scratch: Refusing to run with an unqualified interpreter - use")
    [Console]::Error.WriteLine("vco-scratch: the system temp dir for this run and report the refusal.")
    Write-VctLadderRefusalSummary -Tool "vco-scratch" -ExitCode 1
    exit 1
}

# The project this wrapper ships in (.claude\scripts\ -> project root).
$Sub = "path"
$Rest = @()
if ($args.Count -gt 0) {
    $Sub = $args[0]
    if ($args.Count -gt 1) { $Rest = $args[1..($args.Count - 1)] }
}
& $VenvPython -m vco_lib.scratch_dir $Sub --project "$ProjectRoot" @Rest
exit $LASTEXITCODE
