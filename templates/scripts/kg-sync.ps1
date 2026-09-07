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

function Test-VenvHasKgDeps {
    param([string]$PythonExe)
    if (-not (Test-Path $PythonExe)) { return $false }
    # v0.2.49 Bug K: validate BOTH `weaviate` (upstream client) AND
    # `weaviate_mcp` (our editable internal module). Pre-fix only
    # `weaviate` was checked, letting unrelated venvs through.
    & $PythonExe -c "import weaviate, weaviate_mcp" 2>$null
    return ($LASTEXITCODE -eq 0)
}

# v0.2.92 (W3 §4bis): read `VCT_ORCHESTRATOR_ROOT` out of the project's own
# `.claude\env`. This is the DURABLE tier — the one that works in a plain
# terminal, in CI, and from any scheduled task: it needs no env inheritance at
# all, because the value is file-backed and written by the canonical env
# projection on every install and update.
# PARITY: this block must match the bash sibling `kg-sync` (same tiers, same
# order, same refusal).
function Get-OrchestratorRootFromProjectEnv {
    $envFile = Join-Path $ScriptDir "..\env"
    if (-not (Test-Path $envFile)) { return $null }
    # First assignment wins, `export ` prefix tolerated, one quote pair
    # stripped — the same line rule `vco_lib/envfile.py::parse_env_lines`
    # applies, so the file has one meaning on every surface that reads it.
    foreach ($line in (Get-Content -LiteralPath $envFile -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*(export\s+)?VCT_ORCHESTRATOR_ROOT\s*=\s*(.*)$') {
            $val = $Matches[2].Trim()
            if ($val.Length -ge 2 -and (
                    ($val.StartsWith('"') -and $val.EndsWith('"')) -or
                    ($val.StartsWith("'") -and $val.EndsWith("'")))) {
                $val = $val.Substring(1, $val.Length - 2)
            }
            if ($val) { return $val }
            return $null
        }
    }
    return $null
}

# Is this a real VCO orchestrator clone (not the user's project that merely
# happens to own a `.venv`)? Same discriminator as
# `templates/hooks/_lib/resolve-vco-venv.ps1` and `install.py::validate_source_repo`.
# Without it, `$ProjectRoot\.venv` IS the user's project venv whenever this
# wrapper is bundled into a project.
function Test-VcoOrchestratorClone {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path $Candidate)) { return $false }
    if (-not (Test-Path (Join-Path $Candidate "install.py"))) { return $false }
    if (-not (Test-Path (Join-Path $Candidate "first-install.sh"))) { return $false }
    return $true
}

function Get-VenvPythonCandidates {
    param([string]$Root)
    return @(
        (Join-Path $Root ".venv\Scripts\python.exe"),
        (Join-Path $Root ".venv\bin\python"),
        (Join-Path $Root "claude_mcp_servers\.venv\Scripts\python.exe"),
        (Join-Path $Root "claude_mcp_servers\.venv\bin\python")
    )
}

# Candidate venv python locations, canonical-first: explicit override, then
# the launcher-provided install root, then the file-backed orchestrator root,
# then clone-relative paths — the last GATED so a user project's venv can
# never be selected.
$ProjectEnvRoot = Get-OrchestratorRootFromProjectEnv
$Candidates = @()
if ($env:VCT_VENV) {
    $Candidates += @(
        (Join-Path $env:VCT_VENV "Scripts\python.exe"),
        (Join-Path $env:VCT_VENV "bin\python")
    )
}
if ($env:VCT_INSTALL_ROOT) {
    $Candidates += Get-VenvPythonCandidates $env:VCT_INSTALL_ROOT
}
if ($ProjectEnvRoot) {
    $Candidates += Get-VenvPythonCandidates $ProjectEnvRoot
}
if (Test-VcoOrchestratorClone $ProjectRoot) {
    $Candidates += Get-VenvPythonCandidates $ProjectRoot
}

$VenvPython = $null
foreach ($cand in $Candidates) {
    if ($cand -and (Test-Path $cand) -and (Test-VenvHasKgDeps $cand)) {
        $VenvPython = $cand
        break
    }
}

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
    [Console]::Error.WriteLine("kg-sync: ERROR - no Python environment with VCO's KG dependencies.")
    [Console]::Error.WriteLine("kg-sync: A candidate qualifies only when BOTH 'weaviate' and")
    [Console]::Error.WriteLine("kg-sync: 'weaviate_mcp' import from it. Probed, in order:")
    if ($Candidates.Count -eq 0) {
        [Console]::Error.WriteLine("kg-sync:   (none - no VCT_VENV, no VCT_INSTALL_ROOT, no")
        [Console]::Error.WriteLine("kg-sync:    VCT_ORCHESTRATOR_ROOT in $ScriptDir\..\env, and")
        [Console]::Error.WriteLine("kg-sync:    $ProjectRoot is not a VCO orchestrator clone)")
    } else {
        foreach ($cand in $Candidates) { [Console]::Error.WriteLine("kg-sync:   - $cand") }
    }
    [Console]::Error.WriteLine("kg-sync: Fix by any ONE of:")
    [Console]::Error.WriteLine("kg-sync:   * run this from a launcher-managed session (it exports")
    [Console]::Error.WriteLine("kg-sync:     VCT_INSTALL_ROOT);")
    [Console]::Error.WriteLine("kg-sync:   * `$env:VCT_VENV = 'C:\path\to\orchestrator\.venv';")
    [Console]::Error.WriteLine("kg-sync:   * `$env:VCT_INSTALL_ROOT = 'C:\path\to\orchestrator';")
    [Console]::Error.WriteLine("kg-sync:   * re-run the orchestrator install so this project's")
    [Console]::Error.WriteLine("kg-sync:     .claude\env carries VCT_ORCHESTRATOR_ROOT.")
    [Console]::Error.WriteLine("kg-sync: Refusing to run with an unqualified interpreter - doing so")
    [Console]::Error.WriteLine("kg-sync: fails later with a misleading ModuleNotFoundError.")
    [Console]::Error.WriteLine("kg-sync: (exit 3 = did not run; 1 would mean the sync ran and")
    [Console]::Error.WriteLine("kg-sync:  some nodes failed)")
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

# PARITY (v0.2.92 delivery audit m8): export VIRTUAL_ENV like the bash
# sibling (`export VIRTUAL_ENV="$VENV_PATH"`). Invoking the venv python
# binary directly activates the interpreter, but subprocess libraries that
# probe $VIRTUAL_ENV need it set to the venv ROOT. Both venv layouts
# (`<root>\Scripts\python.exe`, `<root>/bin/python`) place the binary
# exactly one directory inside the root, so two Split-Path -Parent hops
# recover the root in either shape.
$venvBinDir = Split-Path -Parent $VenvPython
$env:VIRTUAL_ENV = Split-Path -Parent $venvBinDir

& $VenvPython (Join-Path $ScriptDir "sync_knowledge_graph.py") @args
exit $LASTEXITCODE
