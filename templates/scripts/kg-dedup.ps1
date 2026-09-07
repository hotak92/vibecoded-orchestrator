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

# A candidate qualifies only when BOTH `weaviate` (the upstream client) and
# `vco_lib` (which owns the reconcile logic) import from it. `kg-sync` gates
# on `weaviate_mcp` instead because ITS script needs the chunker; this one
# does not, so we name what we actually require rather than copy a stricter
# gate we cannot justify.
function Test-VenvHasDedupDeps {
    param([string]$PythonExe)
    if (-not (Test-Path $PythonExe)) { return $false }
    & $PythonExe -c "import weaviate, vco_lib" 2>$null
    return ($LASTEXITCODE -eq 0)
}

# Read `VCT_ORCHESTRATOR_ROOT` out of the project's own `.claude\env` - the
# DURABLE tier that needs no env inheritance (same line rule as
# `vco_lib/envfile.py::parse_env_lines`).
# PARITY: this block must match the bash sibling `kg-dedup`.
function Get-OrchestratorRootFromProjectEnv {
    $envFile = Join-Path $ScriptDir "..\env"
    if (-not (Test-Path $envFile)) { return $null }
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
    if ($cand -and (Test-Path $cand) -and (Test-VenvHasDedupDeps $cand)) {
        $VenvPython = $cand
        break
    }
}

# NO BARE `python` FALLBACK - a shipped component does not get a silent
# fallback (standing rule). Name every candidate and the remedy, exit 1.
# PARITY: the bash sibling refuses identically.
if (-not $VenvPython) {
    Write-Host "kg-dedup: ERROR - no Python environment with VCO's KG dependencies." -ForegroundColor Red
    Write-Host "kg-dedup: A candidate qualifies only when BOTH 'weaviate' and" -ForegroundColor Red
    Write-Host "kg-dedup: 'vco_lib' import from it. Probed, in order:" -ForegroundColor Red
    if ($Candidates.Count -eq 0) {
        Write-Host "kg-dedup:   (none - no VCT_VENV, no VCT_INSTALL_ROOT, no" -ForegroundColor Red
        Write-Host "kg-dedup:    VCT_ORCHESTRATOR_ROOT in $ScriptDir\..\env, and" -ForegroundColor Red
        Write-Host "kg-dedup:    $ProjectRoot is not a VCO orchestrator clone)" -ForegroundColor Red
    } else {
        foreach ($cand in $Candidates) { Write-Host "kg-dedup:   - $cand" -ForegroundColor Red }
    }
    Write-Host "kg-dedup: Fix by any ONE of:" -ForegroundColor Red
    Write-Host "kg-dedup:   * run this from a launcher-managed session (it exports" -ForegroundColor Red
    Write-Host "kg-dedup:     VCT_INSTALL_ROOT);" -ForegroundColor Red
    Write-Host "kg-dedup:   * `$env:VCT_VENV = 'C:\path\to\orchestrator\.venv';" -ForegroundColor Red
    Write-Host "kg-dedup:   * `$env:VCT_INSTALL_ROOT = 'C:\path\to\orchestrator';" -ForegroundColor Red
    Write-Host "kg-dedup:   * re-run the orchestrator install so this project's" -ForegroundColor Red
    Write-Host "kg-dedup:     .claude\env carries VCT_ORCHESTRATOR_ROOT." -ForegroundColor Red
    Write-Host "kg-dedup: Refusing to run with an unqualified interpreter - a" -ForegroundColor Red
    Write-Host "kg-dedup: reconcile that cannot read the collection must never" -ForegroundColor Red
    Write-Host "kg-dedup: report 'no duplicates'." -ForegroundColor Red
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
