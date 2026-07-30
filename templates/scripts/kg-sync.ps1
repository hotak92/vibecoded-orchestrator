# PowerShell wrapper for sync_knowledge_graph.py (Windows equivalent of kg-sync)
# Usage: .\kg-sync.ps1 FILE
#        .\kg-sync.ps1 --all
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

# Candidate venv python locations, canonical-first.
$Candidates = @(
    $(if ($env:VCT_INSTALL_ROOT) { Join-Path $env:VCT_INSTALL_ROOT ".venv\Scripts\python.exe" }),
    $(if ($env:VCT_INSTALL_ROOT) { Join-Path $env:VCT_INSTALL_ROOT "claude_mcp_servers\.venv\Scripts\python.exe" }),
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    (Join-Path $ProjectRoot "claude_mcp_servers\.venv\Scripts\python.exe")
)

$VenvPython = $null
foreach ($cand in $Candidates) {
    if ($cand -and (Test-Path $cand) -and (Test-VenvHasKgDeps $cand)) {
        $VenvPython = $cand
        break
    }
}

if (-not $VenvPython) {
    Write-Host "ERROR: no venv with weaviate-client installed. Probed:" -ForegroundColor Red
    foreach ($cand in $Candidates) {
        if ($cand) { Write-Host "  - $cand" -ForegroundColor Red }
    }
    Write-Host "Run install.ps1 first, or set VCT_INSTALL_ROOT to point at an installed orchestrator clone." -ForegroundColor Red
    exit 1
}

# v0.2.89 BUG 3 (plan §1.3 B): pin the target project root via the NEW
# non-leaking env channel. `KG_BASE_DIR` is exported by every Claude Code
# session (.claude/settings.json env), so a wrapper run from a session
# whose env belongs to ANOTHER project used to inherit the foreign root
# and sync the wrong tree with false success (Fabio field audit).
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

& $VenvPython (Join-Path $ScriptDir "sync_knowledge_graph.py") @args
exit $LASTEXITCODE
