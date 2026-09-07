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
# Exit codes: whatever `detect_duplicates.py` exits (0 = report written);
# 1 = REFUSAL - no Python environment with the dependencies this wrapper
# needs (wrapper-only; the script never emits it for its own results).
#
# PARITY: this file follows the `kg-dedup` / `kg-sync` wrapper contract
# (same venv tiers, same refusal shape, stderr refusal, no bare-python
# fallback). The bash sibling `kg-duplicates` is the POSIX entry point.

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# A candidate qualifies only when `weaviate` (the upstream client this
# script imports at module level) imports from it. `detect_duplicates.py`
# resolves its project root from its OWN file location (the wrapper
# invokes it by path from $ScriptDir), so no project-root pin is needed
# here - name what the script requires, nothing more.
function Test-VenvHasDeps {
    param([string]$PythonExe)
    if (-not (Test-Path $PythonExe)) { return $false }
    & $PythonExe -c "import weaviate" 2>$null
    return ($LASTEXITCODE -eq 0)
}

# Read `VCT_ORCHESTRATOR_ROOT` out of the project's own `.claude\env` - the
# DURABLE tier that needs no env inheritance (same line rule as
# `vco_lib/envfile.py::parse_env_lines`). PARITY: kg-sync / kg-dedup.
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
# happens to own a `.venv`)? Same discriminator as kg-sync / kg-dedup.
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

# Candidate venv python locations, canonical-first (same tier order as
# kg-sync / kg-dedup): explicit override, launcher install root, the
# file-backed orchestrator root, then clone-relative paths - the last
# GATED so a user project's venv can never be selected.
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
    if ($cand -and (Test-Path $cand) -and (Test-VenvHasDeps $cand)) {
        $VenvPython = $cand
        break
    }
}

# NO BARE `python` FALLBACK - a shipped component does not get a silent
# fallback (standing rule). Name every candidate and the remedy, exit 1.
# The refusal goes to STDERR (`>&2` in the bash siblings' shape); the host
# stream is invisible to 2>nul / CI capture / stderr-scraping consumers.
if (-not $VenvPython) {
    [Console]::Error.WriteLine("kg-duplicates: ERROR - no Python environment with VCO's KG dependencies.")
    [Console]::Error.WriteLine("kg-duplicates: A candidate qualifies only when 'weaviate' imports from it.")
    [Console]::Error.WriteLine("kg-duplicates: Probed, in order:")
    if ($Candidates.Count -eq 0) {
        [Console]::Error.WriteLine("kg-duplicates:   (none - no VCT_VENV, no VCT_INSTALL_ROOT, no")
        [Console]::Error.WriteLine("kg-duplicates:    VCT_ORCHESTRATOR_ROOT in $ScriptDir\..\env, and")
        [Console]::Error.WriteLine("kg-duplicates:    $ProjectRoot is not a VCO orchestrator clone)")
    } else {
        foreach ($cand in $Candidates) { [Console]::Error.WriteLine("kg-duplicates:   - $cand") }
    }
    [Console]::Error.WriteLine("kg-duplicates: Fix by any ONE of:")
    [Console]::Error.WriteLine("kg-duplicates:   * run this from a launcher-managed session (it exports")
    [Console]::Error.WriteLine("kg-duplicates:     VCT_INSTALL_ROOT);")
    [Console]::Error.WriteLine("kg-duplicates:   * `$env:VCT_VENV = 'C:\path\to\orchestrator\.venv';")
    [Console]::Error.WriteLine("kg-duplicates:   * `$env:VCT_INSTALL_ROOT = 'C:\path\to\orchestrator';")
    [Console]::Error.WriteLine("kg-duplicates:   * re-run the orchestrator install so this project's")
    [Console]::Error.WriteLine("kg-duplicates:     .claude\env carries VCT_ORCHESTRATOR_ROOT.")
    [Console]::Error.WriteLine("kg-duplicates: (exit 1 = did not run)")
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "detect_duplicates.py") @args
exit $LASTEXITCODE
