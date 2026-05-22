# migrate-development-temporal-props.ps1
#
# Windows equivalent of migrate-development-temporal-props.sh. Adds the
# four canonical temporal properties (created, updated, valid_from,
# valid_until) to every existing *_Development collection in the running
# Weaviate.
#
# Idempotent: per-property presence is checked before POST.
# Soft-fail per collection: errors are logged; exit code is always 0.
#
# Env vars:
#   $env:WEAVIATE_URL  — defaults to http://localhost:8081
#
# Requires: PowerShell 5.1+ (or PowerShell Core). No curl/jq needed —
# uses Invoke-RestMethod + ConvertFrom-Json.
#
# Coordinated with: scripts/migrate-shared-kg-schema.ps1 (PR-24,
# 2026-05-16).

[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"

$WeaviateUrl = if ($env:WEAVIATE_URL) { $env:WEAVIATE_URL } else { "http://localhost:8081" }

# Probe readiness — exit clean on unreachable.
try {
    $null = Invoke-RestMethod -Uri "$WeaviateUrl/v1/.well-known/ready" -TimeoutSec 5
} catch {
    Write-Host "[migrate-dev-props] Weaviate not reachable at $WeaviateUrl; skipping."
    exit 0
}

try {
    $schema = Invoke-RestMethod -Uri "$WeaviateUrl/v1/schema" -TimeoutSec 10
} catch {
    Write-Host "[migrate-dev-props] Failed to read schema from $WeaviateUrl/v1/schema; skipping."
    exit 0
}

$devCollections = @()
if ($schema.classes) {
    foreach ($cls in $schema.classes) {
        if ($cls.class -match '_Development$') {
            $devCollections += $cls.class
        }
    }
}

if ($devCollections.Count -eq 0) {
    Write-Host "[migrate-dev-props] No *_Development collections found; nothing to migrate."
    exit 0
}

$anyChange = $false
$anyError  = $false

foreach ($coll in $devCollections) {
    try {
        $collSchema = Invoke-RestMethod -Uri "$WeaviateUrl/v1/schema/$coll" -TimeoutSec 10
    } catch {
        Write-Warning "[migrate-dev-props] $coll : failed to fetch schema; skipping."
        $anyError = $true
        continue
    }

    $existingProps = @()
    if ($collSchema.properties) {
        $existingProps = $collSchema.properties | ForEach-Object { $_.name }
    }

    foreach ($prop in @("created", "updated", "valid_from", "valid_until")) {
        if ($existingProps -contains $prop) {
            Write-Host "[migrate-dev-props] $coll.$prop already present; skip."
            continue
        }
        Write-Host "[migrate-dev-props] $coll.$prop missing; adding ..."
        $body = @{ name = $prop; dataType = @("date") } | ConvertTo-Json -Compress
        try {
            $null = Invoke-RestMethod -Uri "$WeaviateUrl/v1/schema/$coll/properties" `
                                      -Method Post `
                                      -ContentType "application/json" `
                                      -Body $body `
                                      -TimeoutSec 10
            Write-Host "[migrate-dev-props] $coll.$prop added."
            $anyChange = $true
        } catch {
            # Inspect HTTP status when possible — 422 = already present.
            $status = $null
            if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
                $status = [int]$_.Exception.Response.StatusCode
            }
            if ($status -eq 422) {
                Write-Host "[migrate-dev-props] $coll.$prop returned 422 — already present; skip."
            } else {
                Write-Warning "[migrate-dev-props] $coll.$prop add failed: $($_.Exception.Message)"
                $anyError = $true
            }
        }
    }
}

if ($anyChange) {
    Write-Host "[migrate-dev-props] Done (changes applied)."
} elseif ($anyError) {
    Write-Host "[migrate-dev-props] Done (some operations failed; see log)."
} else {
    Write-Host "[migrate-dev-props] Done (no changes needed)."
}

exit 0
