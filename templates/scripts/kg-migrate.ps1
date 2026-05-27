# PowerShell wrapper for migrate_to_vocabulary.py (Windows equivalent of kg-migrate)
# Usage: .\kg-migrate.ps1 --check
#        .\kg-migrate.ps1 --fix
#        .\kg-migrate.ps1 --interactive
#        .\kg-migrate.ps1 --file <path>
#
# v0.2.37 (Gap 6b): Windows sibling of the bash kg-migrate (which used
# to be POSIX-only). Backports the validate-has-weaviate-client pattern
# from the bash sibling. See `kg-sync.ps1` for the full rationale.

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")

function Test-VenvHasKgDeps {
    param([string]$PythonExe)
    if (-not (Test-Path $PythonExe)) { return $false }
    & $PythonExe -c "import weaviate" 2>$null
    return ($LASTEXITCODE -eq 0)
}

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

& $VenvPython (Join-Path $ScriptDir "migrate_to_vocabulary.py") @args
exit $LASTEXITCODE
