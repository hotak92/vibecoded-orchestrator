# PowerShell wrapper for the KG duplicate-OBJECT reconcile (Windows
# equivalent of kg-dedup). v0.2.92 WP-B2, field defect D13.
#
# Not to be confused with `kg-duplicates`, which finds semantically similar
# but DISTINCT nodes. This tool finds the same source file stored more than
# once in the Weaviate collection - the 4x / 2x duplication a field tester
# hit (115 objects over 111 distinct paths).
#
# WP-B1 fixed the cause (one canonical POSIX `file_path` per write + a
# dual-shape delete), so a node heals itself the next time it is written.
# A node nobody edits again stays duplicated forever; this is the pass that
# reconciles those.
#
# Usage:
#   .\kg-dedup.ps1                       # DRY RUN - reports only
#   .\kg-dedup.ps1 --apply               # actually delete extras
#   .\kg-dedup.ps1 --collection MyKG     # explicit collection
#
# Exit codes match `kg-sync`: 0 = clean, 1 = refusal / operational failure,
# 2 = usage error. Flags are validated in ONE home
# (`python -m vco_lib.kg_dedup`); this wrapper is a dumb forwarder.
#
# PARITY: this file and the bash sibling `kg-dedup` must stay in lockstep
# (same tiers, same refusal text, same forwarding).

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# The probe a candidate interpreter must pass, verbatim as it is executed.
# A candidate qualifies only when BOTH `weaviate` (the upstream client) and
# `vco_lib` (which owns the reconcile logic) import from it. `kg-sync` gates
# on `weaviate_mcp` instead because ITS script needs the chunker; this one
# does not, so we name what we actually require rather than copy a stricter
# gate we cannot justify.
$LadderImport = "import weaviate, vco_lib"

# v0.2.94: the ladder itself (tiers, probe, refusal prose) moved to ONE home
# shipped beside this wrapper - it had been copied verbatim into `kg-sync.ps1`
# while the bash `kg-duplicates` had no ladder at all. A missing lib is a
# broken install, not a reason to fall back to a bare interpreter.
$LadderLib = Join-Path $ScriptDir "vct_venv_ladder.ps1"
if (-not (Test-Path $LadderLib)) {
    [Console]::Error.WriteLine("kg-dedup: ERROR - missing $LadderLib")
    [Console]::Error.WriteLine("kg-dedup: (broken install - re-run the orchestrator install)")
    [Console]::Error.WriteLine("$([char]0x26A0)$([char]0xFE0F)  kg-dedup did NOT run (exit 1): its shared venv ladder is missing. See the lines above.")
    exit 1
}
. $LadderLib

$VenvPython = Resolve-VctLadderPython -ScriptDir $ScriptDir `
    -ProjectRoot $ProjectRoot -ImportProbe $LadderImport

# NO BARE `python` FALLBACK - a shipped component does not get a silent
# fallback (standing rule). Name every candidate and the remedy, exit 1.
# PARITY: the bash sibling refuses identically.
#
# v0.2.94: the refusal goes to STDERR, like the bash sibling's `>&2` block and
# like kg-duplicates.ps1. It used to go to the HOST stream (Write-Host), which
# stderr consumers - 2>nul, CI capture, the launcher's error scraping - never
# see; a refused run was indistinguishable from a silent success there.
if (-not $VenvPython) {
    Write-VctLadderRefusal -Tool "kg-dedup" -ImportProbe $LadderImport `
        -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
    [Console]::Error.WriteLine("kg-dedup: Refusing to run with an unqualified interpreter - a")
    [Console]::Error.WriteLine("kg-dedup: reconcile that cannot read the collection must never")
    [Console]::Error.WriteLine("kg-dedup: report 'no duplicates'.")
    Write-VctLadderRefusalSummary -Tool "kg-dedup" -ExitCode 1
    exit 1
}

# Pin the target project root via the non-leaking channel `KG_SYNC_PROJECT_ROOT`
# (set-if-unset), exactly as kg-sync does: `KG_BASE_DIR` is exported by every
# Claude Code session, so a wrapper run from a foreign session would otherwise
# resolve ANOTHER project's collection. Do NOT touch KG_BASE_DIR.
# PARITY: this block must match the bash sibling `kg-dedup`.
if (-not $env:KG_SYNC_PROJECT_ROOT) {
    $env:KG_SYNC_PROJECT_ROOT = "$ProjectRoot"
}

& $VenvPython -m vco_lib.kg_dedup @args
exit $LASTEXITCODE
