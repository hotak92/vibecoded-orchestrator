# migrate-shared-kg-schema.ps1
#
# Windows equivalent of migrate-shared-kg-schema.sh. Drops + recreates
# the shared KG collection when its schema lacks
# `invertedIndexConfig.indexNullState=True`. Weaviate <=1.30 cannot add
# `indexNullState` retroactively; the only fix is a destructive recreate
# from the .md sources in knowledge/.
#
# DATA-SAFETY CONTRACT (v0.2.54 Track D / audit P0-2) — mirrors the .sh:
#
#   1. kg-sync presence is verified BEFORE the drop (exit 4 if missing —
#      pre-fix the drop happened first, leaving the collection empty).
#   2. Cross-project shared-write probe: nodes written by OTHER projects
#      (project-relative file_path that doesn't resolve under this
#      clone, absolute paths outside it, probe failures, or >10000
#      objects) are NOT restorable by this clone's kg-sync resync. The
#      script REFUSES (exit 3) unless
#      $env:VCO_SHARED_KG_MIGRATE_CONSENT = "1".
#
# Idempotent: no-op if indexNullState is already True, no-op if the
# shared KG doesn't exist.
#
# Exit codes: 0 = no-op or success; 3 = refused (unrecoverable
# cross-project nodes / unverifiable, no consent); 4 = refused (kg-sync
# helper missing). Non-zero exits surface as
# `schema_migration_failed_shared_kg_schema` deferrals on the install.py
# path and as ok=false + stderr in the launcher's consent modal.
#
# Env vars:
#   $env:WEAVIATE_URL          — defaults to http://localhost:8081
#   $env:SHARED_KG_COLLECTION  — defaults to VibeCodedOrchestrator_KnowledgeGraph
#                                (capital-C casing since v0.2.23 B1; was
#                                lowercase-c "VibecodedOrchestrator_KnowledgeGraph"
#                                v0.2.12–v0.2.22, itself renamed from
#                                VibeCodedTools_KnowledgeGraph pre-v0.2.12).
#                                Must stay in lockstep with
#                                vco_lib/project_init.py::_SHARED_KG_NAME.
#   $env:VCO_SHARED_KG_MIGRATE_CONSENT — "1" accepts the loss of
#                                cross-project shared nodes.
#
# Requires: PowerShell 5.1+ (or PowerShell Core). The post-drop
# repopulation calls `.claude/scripts/kg-sync.ps1` if present.

[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"

$WeaviateUrl = if ($env:WEAVIATE_URL) { $env:WEAVIATE_URL } else { "http://localhost:8081" }
$SharedKg    = if ($env:SHARED_KG_COLLECTION) { $env:SHARED_KG_COLLECTION } else { "VibeCodedOrchestrator_KnowledgeGraph" }
$Consent     = ($env:VCO_SHARED_KG_MIGRATE_CONSENT -eq "1")

# Orchestrator clone root = parent of the scripts/ dir this file lives in.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$CloneRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path

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

Write-Host "[migrate-shared-kg] $SharedKg indexNullState=$current; migration needed."

# ---------------------------------------------------------------------------
# GUARD 1 (BEFORE drop): locate the kg-sync helper. Pre-v0.2.54 this lookup
# ran AFTER the DELETE — a missing helper meant "collection dropped, exit 0,
# nothing repopulates". Now: no helper -> no drop.
# ---------------------------------------------------------------------------
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
    Write-Warning "[migrate-shared-kg] REFUSED: kg-sync helper not found — dropping $SharedKg now would leave it empty with no repopulation path. Run 'python install.py --update' (which materializes .claude/scripts/) and retry."
    exit 4
}

# ---------------------------------------------------------------------------
# GUARD 2 (BEFORE drop): cross-project shared-write probe. The resync only
# restores nodes whose .md source lives under THIS clone. Conservative:
# probe failures, >10000 objects, or any unrecoverable path REFUSE unless
# consent is given.
# ---------------------------------------------------------------------------
$ProbeLimit = 10000
$probeOk = $true
$unrecoverable = New-Object System.Collections.Generic.List[string]

try {
    $aggBody = @{ query = "{ Aggregate { $SharedKg { meta { count } } } }" } | ConvertTo-Json
    $aggResp = Invoke-RestMethod -Uri "$WeaviateUrl/v1/graphql" -Method Post `
        -ContentType "application/json" -Body $aggBody -TimeoutSec 30
    $totalCount = $aggResp.data.Aggregate.$SharedKg[0].meta.count
    if ($null -eq $totalCount) { $probeOk = $false }
    elseif ([int]$totalCount -gt $ProbeLimit) { $probeOk = $false }
    elseif ([int]$totalCount -gt 0) {
        $getBody = @{ query = "{ Get { $SharedKg(limit: $ProbeLimit) { file_path } } }" } | ConvertTo-Json
        $getResp = Invoke-RestMethod -Uri "$WeaviateUrl/v1/graphql" -Method Post `
            -ContentType "application/json" -Body $getBody -TimeoutSec 60
        $paths = @($getResp.data.Get.$SharedKg | ForEach-Object { $_.file_path } |
                   Where-Object { $_ } | Sort-Object -Unique)
        foreach ($fp in $paths) {
            $restorable = $false
            if ([System.IO.Path]::IsPathRooted($fp)) {
                # Absolute path: restorable only if inside this clone AND
                # still on disk.
                $full = $fp
                if ($full.StartsWith($CloneRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
                    (Test-Path -LiteralPath $full -PathType Leaf)) {
                    $restorable = $true
                }
            } else {
                $rel = Join-Path $CloneRoot $fp
                if (Test-Path -LiteralPath $rel -PathType Leaf) {
                    $restorable = $true
                }
            }
            if (-not $restorable) { $unrecoverable.Add($fp) }
        }
    }
} catch {
    $probeOk = $false
}

if ((-not $probeOk) -or ($unrecoverable.Count -gt 0)) {
    if ($Consent) {
        if (-not $probeOk) {
            Write-Warning "[migrate-shared-kg] safety probe could not verify the collection but VCO_SHARED_KG_MIGRATE_CONSENT=1 — proceeding."
        } else {
            Write-Warning "[migrate-shared-kg] $($unrecoverable.Count) cross-project shared node(s) will be PERMANENTLY LOST (consented)."
        }
    } else {
        if (-not $probeOk) {
            Write-Warning "[migrate-shared-kg] REFUSED: could not verify that every shared node is restorable from this clone (probe window: $ProbeLimit)."
        } else {
            Write-Warning "[migrate-shared-kg] REFUSED: $($unrecoverable.Count) shared node(s) were written by OTHER projects (or have sources outside this clone) and would be PERMANENTLY LOST by drop+resync."
            $unrecoverable | Select-Object -First 10 | ForEach-Object {
                Write-Warning "[migrate-shared-kg]     - $_"
            }
        }
        Write-Warning "[migrate-shared-kg] To proceed anyway (accepting the loss): set `$env:VCO_SHARED_KG_MIGRATE_CONSENT = '1' and re-run scripts/migrate-shared-kg-schema.ps1. Better: re-run each contributing project's '.claude/scripts/kg-sync --all' AFTER the migration to restore its shared nodes."
        exit 3
    }
}

Write-Host "[migrate-shared-kg] Dropping + recreating $SharedKg ..."

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
