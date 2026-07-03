# PowerShell wrapper for analyze_code_graph.py (Windows equivalent of code-graph-analyze)
# Usage: .\code-graph-analyze.ps1 C:\path\to\repo [--project NAME] [--incremental]
#
# RESILIENT INTERPRETER DISCOVERY (RT-4). This candidate order MUST MATCH
# the .sh sibling and the canonical tiers in
# templates/hooks/_lib/resolve-vco-venv.ps1 (mirror, don't fork):
#   1. $env:VCT_VENV              -- explicit override (makes a removed
#                                    default recoverable; RT-4's remedy)
#   2. $env:VCT_INSTALL_ROOT\.venv                     -- launcher (canonical)
#   3. $env:VCT_INSTALL_ROOT\claude_mcp_servers\.venv  -- legacy launcher path
#   4. <ProjectRoot>\.venv                             -- clone-relative
#   5. <ProjectRoot>\claude_mcp_servers\.venv          -- legacy clone path
#
# Bug history:
#   - 2026-04-28 (407076a): added VCT_INSTALL_ROOT fallbacks so a
#     browse-registered project (no own venv) resolves the launcher venv.
#   - 2026-05-07: script-relative-FIRST activated the user's OWN project
#     venv (no weaviate-client) -> reorder canonical-first + validate.
#   - 2026-06-27 (RT-4): an installed shim hardcoded the removed legacy
#     `.../Claude/claude_mcp_servers/.venv/...` and exited 127. This
#     revision adds the missing $env:VCT_VENV tier + a weaviate-client
#     validation gate so a stale default is always recoverable.

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

# Candidate venv DIRECTORIES, canonical-first. $env:VCT_VENV may point at
# a venv dir OR directly at the interpreter; both are handled below.
$Candidates = @(
    $(if ($env:VCT_VENV) { $env:VCT_VENV }),                                          # explicit override (tier 1)
    $(if ($env:VCT_INSTALL_ROOT) { Join-Path $env:VCT_INSTALL_ROOT ".venv" }),        # launcher install root (canonical)
    $(if ($env:VCT_INSTALL_ROOT) { Join-Path $env:VCT_INSTALL_ROOT "claude_mcp_servers\.venv" }), # legacy launcher path
    (Join-Path $ProjectRoot ".venv"),                                                 # clone-relative
    (Join-Path $ProjectRoot "claude_mcp_servers\.venv")                               # legacy clone path
)

# Resolve the interpreter inside a candidate (venv dir OR direct binary).
function Resolve-Interp {
    param([string]$Cand)
    if (-not $Cand) { return $null }
    $winPy = Join-Path $Cand "Scripts\python.exe"
    if (Test-Path $winPy) { return $winPy }
    $nixPy = Join-Path $Cand "bin\python"
    if (Test-Path $nixPy) { return $nixPy }
    # $env:VCT_VENV given as the interpreter itself (a file, not a dir).
    if ((Test-Path $Cand) -and -not (Test-Path $Cand -PathType Container)) { return $Cand }
    return $null
}

# Validate a candidate resolves to a python that has the analyzer deps.
function Test-AnalyzerDeps {
    param([string]$Cand)
    $py = Resolve-Interp $Cand
    if (-not $py) { return $null }
    & $py -c "import weaviate" 2>$null
    if ($LASTEXITCODE -eq 0) { return $py }
    return $null
}

$VenvPython = $null
foreach ($cand in $Candidates) {
    if (-not $cand) { continue }
    $resolved = Test-AnalyzerDeps $cand
    if ($resolved) { $VenvPython = $resolved; break }
}

if (-not $VenvPython) {
    Write-Host "ERROR: no venv with analyzer deps (weaviate-client) found. Searched:" -ForegroundColor Red
    foreach ($cand in $Candidates) {
        if ($cand) { Write-Host "  - $cand" -ForegroundColor Red }
    }
    Write-Host "Run install.ps1 first, or set VCT_VENV / VCT_INSTALL_ROOT." -ForegroundColor Red
    exit 1
}

& $VenvPython (Join-Path $ScriptDir "analyze_code_graph.py") @args
exit $LASTEXITCODE
