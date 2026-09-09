# PowerShell wrapper for sync_knowledge_graph.py (Windows equivalent of kg-sync)
# Usage: .\kg-sync.ps1 FILE
#        .\kg-sync.ps1 --all
#
# EXIT CODES (the ladder this wrapper shares with the script it runs;
# the script's own half is printed by `sync_knowledge_graph.py::_print_usage`):
#
#   0  clean run
#   1  the sync RAN and some nodes/docs failed
#   2  usage error, or a refused/wrong project root
#   3  the sync DID NOT RUN: no Python environment with VCO's KG
#      dependencies (wrapper-only; sync_knowledge_graph.py never emits it)
#
# v0.2.92: 3 is new. The venv refusal below used to return 1, which is the
# code the script uses for "I ran and some nodes failed" — so a caller
# could not tell "did not run" from "ran, partially failed". A check that
# cannot distinguish "could not determine" from a real result is not a
# check. PARITY: the bash sibling `kg-sync` documents and returns the
# same ladder.
#
# v0.2.37 (Gap 6b): backports the validate-has-weaviate-client pattern
# from the bash sibling. Pre-v0.2.37 this script only probed
# `$ProjectRoot\.venv` + `$ProjectRoot\claude_mcp_servers\.venv`, both
# absent in a fresh OSS install where the bundle lands in a user
# project that has its own (unrelated) `.venv`. The canonical
# venv-with-weaviate lives at `$env:VCT_INSTALL_ROOT\.venv` (launcher-
# provided) or at orch-clone-relative paths. Validate each candidate
# has `weaviate` importable before activating to avoid picking up an
# unrelated project venv.
#
# v0.2.49 Bug K: pre-fix the validator only checked `import weaviate`
# (the upstream client lib). That let unrelated project venvs through
# that had pip-installed weaviate-client themselves but lacked the
# editable `weaviate_mcp` package (installed by install.py A1,
# v0.2.38). The post-fix validator gates on BOTH imports in a single
# subprocess so candidate venvs missing `weaviate_mcp` are rejected
# (otherwise sync_knowledge_graph.py crashes at
# `from weaviate_mcp.chunking import Chunker`).

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# The probe a candidate interpreter must pass, verbatim as it is executed.
# v0.2.49 Bug K: validate BOTH `weaviate` (upstream client) AND
# `weaviate_mcp` (our editable internal module). Pre-fix only `weaviate` was
# checked, letting unrelated venvs through.
$LadderImport = "import weaviate, weaviate_mcp, vco_lib"

# v0.2.94: the ladder itself (tiers, VIRTUAL_ENV export, refusal prose) moved
# to ONE home shipped beside this wrapper - it had been copied verbatim into
# `kg-dedup.ps1`, while the bash `kg-duplicates` had no ladder at all and
# `kg-duplicates.ps1` gated on a weaker module list than its script needs.
# A missing lib is a broken install, not a reason to fall back to a bare
# interpreter.
$LadderLib = Join-Path $ScriptDir "vct_venv_ladder.ps1"
if (-not (Test-Path $LadderLib)) {
    [Console]::Error.WriteLine("kg-sync: ERROR - missing $LadderLib")
    [Console]::Error.WriteLine("kg-sync: (broken install - re-run the orchestrator install)")
    [Console]::Error.WriteLine("$([char]0x26A0)$([char]0xFE0F)  kg-sync did NOT run (exit 3): its shared venv ladder is missing. See the lines above.")
    exit 3
}
. $LadderLib

$VenvPython = Resolve-VctLadderPython -ScriptDir $ScriptDir `
    -ProjectRoot $ProjectRoot -ImportProbe $LadderImport

# v0.2.92 (W3 §4bis): NO BARE `python` FALLBACK, and the refusal names every
# candidate plus the remedy. A shipped component does not get a silent
# fallback (standing rule); the bash sibling refuses identically.
#
# EXIT 3, NOT 1: 1 is already spoken for by sync_knowledge_graph.py, which
# returns it when the sync RAN and some nodes failed. Reusing it here made
# "I never started" indistinguishable from "I finished with failures" —
# the two demand opposite responses (fix your install vs. look at the
# named failures). 3 is the next free rung above the script's 0/1/2.
if (-not $VenvPython) {
    # PARITY (v0.2.92 delivery audit m8): the refusal goes to STDERR, exactly
    # like the bash sibling's `>&2` block. Write-Host writes the HOST stream,
    # which stderr consumers (2>/dev/null, CI capture, the launcher's error
    # scraping) never see - a refused run was indistinguishable from a silent
    # one on every surface that reads error streams.
    Write-VctLadderRefusal -Tool "kg-sync" -ImportProbe $LadderImport `
        -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
    [Console]::Error.WriteLine("kg-sync: Refusing to run with an unqualified interpreter - doing so")
    [Console]::Error.WriteLine("kg-sync: fails later with a misleading ModuleNotFoundError.")
    [Console]::Error.WriteLine("kg-sync: (exit 3 = did not run; 1 would mean the sync ran and")
    [Console]::Error.WriteLine("kg-sync:  some nodes failed)")
    Write-VctLadderRefusalSummary -Tool "kg-sync" -ExitCode 3
    exit 3
}

# v0.2.89 BUG 3 (plan §1.3 B): pin the target project root via the NEW
# non-leaking env channel. `KG_BASE_DIR` is exported by every Claude Code
# session (.claude/settings.json env), so a wrapper run from a session
# whose env belongs to ANOTHER project used to inherit the foreign root
# and sync the wrong tree with false success (Windows field audit).
# `KG_SYNC_PROJECT_ROOT` is set ONLY by the launcher and by these
# wrappers, never exported by Claude sessions — so it cannot leak.
# Set-if-unset (NOT unconditional): the launcher's explicit value must
# survive the v0.2.77 orchestrator-copy wrapper fallback, where the
# ORCHESTRATOR's wrapper runs on behalf of a project and this wrapper's
# own location would be the WRONG root. Do NOT touch KG_BASE_DIR.
# PARITY: this block must match kg-sync (bash sibling — same logic, same
# rationale).
if (-not $env:KG_SYNC_PROJECT_ROOT) {
    $env:KG_SYNC_PROJECT_ROOT = "$ProjectRoot"
}

# PARITY (v0.2.92 delivery audit m8): VIRTUAL_ENV is exported like the bash
# sibling (`export VIRTUAL_ENV="$venv_path"`) - v0.2.94 moved that export into
# `Resolve-VctLadderPython`, beside the resolution that knows the venv root,
# so kg-dedup.ps1 and kg-duplicates.ps1 get it too instead of only this file.

& $VenvPython (Join-Path $ScriptDir "sync_knowledge_graph.py") @args
exit $LASTEXITCODE
