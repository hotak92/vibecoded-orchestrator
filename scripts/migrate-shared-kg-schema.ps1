# migrate-shared-kg-schema.ps1
#
# Windows equivalent of migrate-shared-kg-schema.sh. Drops + recreates
# the shared KG collection when its schema lacks
# `invertedIndexConfig.indexNullState=True`. Weaviate <=1.30 cannot add
# `indexNullState` retroactively; the only fix is a destructive recreate
# from the .md sources in knowledge/.
#
# Idempotent: no-op if indexNullState is already True, no-op if the
# shared KG doesn't exist.
#
# Soft-fail: any failure logs a warning and exits 0 so install.py
# --update can convert into a deferral.
#
# Env vars:
#   $env:WEAVIATE_URL          — defaults to http://localhost:8081
#   $env:SHARED_KG_COLLECTION  — defaults to VibecodedOrchestrator_KnowledgeGraph
#                                (renamed from VibeCodedTools_KnowledgeGraph in
#                                v0.2.12 PR-26 / Group E). Must stay in lockstep
#                                with vco_lib/project_init.py::_SHARED_KG_NAME.
#
# Requires: PowerShell 5.1+ (or PowerShell Core). The post-drop
# repopulation calls `.claude/scripts/kg-sync.ps1` if present;
# otherwise prints a hint and leaves the collection empty.

[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"

$WeaviateUrl = if ($env:WEAVIATE_URL) { $env:WEAVIATE_URL } else { "http://localhost:8081" }
$SharedKg    = if ($env:SHARED_KG_COLLECTION) { $env:SHARED_KG_COLLECTION } else { "VibecodedOrchestrator_KnowledgeGraph" }

try {
    $null = Invoke-RestMethod -Uri "$WeaviateUrl/v1/.well-known/ready" -TimeoutSec 5
} catch {
    Write-Host "[migrate-shared-kg] Weaviate not reachable at $WeaviateUrl; skipping."
    exit 0
}

# Probe current schema.
$current = $null
try {
    $schema  = Invoke-RestMethod -Uri "$WeaviateUrl/v1/schema/$SharedKg" -TimeoutSec 10
    if ($schema -and $schema.invertedIndexConfig -and $schema.invertedIndexConfig.indexNullState) {
        $current = $true
    } else {
        $current = $false
    }
} catch {
    $status = $null
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
        $status = [int]$_.Exception.Response.StatusCode
    }
    if ($status -eq 404) {
        Write-Host "[migrate-shared-kg] Shared KG '$SharedKg' does not exist; nothing to migrate."
        exit 0
    }
    Write-Host "[migrate-shared-kg] Unexpected error probing schema: $($_.Exception.Message); skipping."
    exit 0
}

if ($current -eq $true) {
    Write-Host "[migrate-shared-kg] $SharedKg already has indexNullState=true; no migration needed."
    exit 0
}

Write-Host "[migrate-shared-kg] $SharedKg indexNullState=$current; dropping + recreating ..."

try {
    Invoke-RestMethod -Uri "$WeaviateUrl/v1/schema/$SharedKg" `
                      -Method Delete `
                      -TimeoutSec 30 | Out-Null
    Write-Host "[migrate-shared-kg] Drop OK."
} catch {
    $status = $null
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
        $status = [int]$_.Exception.Response.StatusCode
    }
    if ($status -eq 404) {
        Write-Host "[migrate-shared-kg] Drop returned 404 (already absent); continuing."
    } else {
        Write-Warning "[migrate-shared-kg] Drop failed: $($_.Exception.Message); aborting migration."
        exit 0
    }
}

# Locate the kg-sync helper. On Windows, .claude/scripts/kg-sync.ps1 is
# the preferred shim; .claude/scripts/kg-sync (no extension) is the
# Unix-style fallback that Git for Windows can still execute via bash.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$candidates = @(
    (Join-Path $scriptDir "..\.claude\scripts\kg-sync.ps1"),
    (Join-Path (Get-Location) ".claude\scripts\kg-sync.ps1"),
    (Join-Path $scriptDir "..\.claude\scripts\kg-sync"),
    (Join-Path (Get-Location) ".claude\scripts\kg-sync")
)
$resyncScript = $null
foreach ($c in $candidates) {
    if (Test-Path $c) {
        $resyncScript = $c
        break
    }
}

if (-not $resyncScript) {
    Write-Host "[migrate-shared-kg] kg-sync helper not found; collection is dropped but not yet"
    Write-Host "[migrate-shared-kg] repopulated. Run '.claude/scripts/kg-sync.ps1 --all' (or the"
    Write-Host "[migrate-shared-kg] bash equivalent) manually to recreate $SharedKg."
    exit 0
}

Write-Host "[migrate-shared-kg] Resyncing via $resyncScript ..."

# Scope the env-var overrides to this invocation only.
$prevKg     = $env:KG_COLLECTION
$prevDev    = $env:DEVELOPMENT_COLLECTION
$prevShared = $env:SHARED_KG_COLLECTION
try {
    $env:KG_COLLECTION          = $SharedKg
    $env:DEVELOPMENT_COLLECTION = ""
    $env:SHARED_KG_COLLECTION   = ""
    if ($resyncScript -like "*.ps1") {
        & $resyncScript --all
    } else {
        & bash $resyncScript --all
    }
} catch {
    Write-Warning "[migrate-shared-kg] Resync exited with error: $($_.Exception.Message); collection may be empty."
} finally {
    $env:KG_COLLECTION          = $prevKg
    $env:DEVELOPMENT_COLLECTION = $prevDev
    $env:SHARED_KG_COLLECTION   = $prevShared
}

try {
    $post = Invoke-RestMethod -Uri "$WeaviateUrl/v1/schema/$SharedKg" -TimeoutSec 10
    $verify = if ($post.invertedIndexConfig -and $post.invertedIndexConfig.indexNullState) { "true" } else { "false" }
    Write-Host "[migrate-shared-kg] Done. Post-migration indexNullState=$verify."
} catch {
    Write-Host "[migrate-shared-kg] Done. Post-migration verification failed (collection may not exist yet)."
}

exit 0
