# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# vct_retrieval_tuning_get.ps1 — PowerShell sibling of
# vct_retrieval_tuning_get.sh. Reads global retrieval tuning values
# (KG tier thresholds + codegraph injection floor) from the launcher
# hub's /api/v1/projects/<id>/config resolver, falling back to reading
# <vct_root_dir>/retrieval-tuning.toml directly if the hub is down.
#
# v0.2.22 Item #13 (2026-05-20). PowerShell 7+ required (Cross-Platform
# Test Matrix per Dev Constraint #1 — Windows PS7, macOS pwsh, Linux
# pwsh all map to the same code path).
#
# Usage:
#   vct_retrieval_tuning_get.ps1 -ProjectFolder <path> [-Field NAME]
#
# Exit codes (mirror the bash sibling):
#   0  success
#   1  hub unreachable AND file fallback failed
#   2  project not registered
#   3  service misconfigured
#   4  field not found
#   64 usage error
#
# Hub discovery is delegated to vct_project_config.ps1 (the sibling
# resolver client) so port + token + 401-retry + soft-fail logic lives
# in ONE place across the bash and PS surfaces.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$ProjectFolder,

    [Parameter(Mandatory = $false)]
    [string]$Field
)

$ErrorActionPreference = 'Stop'

# Defaults from knowledge/concepts/score-driven-retrieval-tiers.md.
# Pinned here AND in the bash + Rust + Svelte siblings — drift caught
# by the round-trip integration test.
$DefaultTuning = [ordered]@{
    code_graph_score_floor = 0.35
    kg_tier_min            = 0.42
    kg_tier_single_chunk   = 0.55
    kg_tier_three_chunks   = 0.65
    kg_tier_full           = 0.75
}

function Write-VctWarn([string]$Message) {
    [Console]::Error.WriteLine("[vct-retrieval-tuning] $Message")
}

function Resolve-VctStateDir {
    if ($env:VCT_STATE_DIR) {
        return $env:VCT_STATE_DIR
    }
    if ($IsWindows) {
        return Join-Path $env:USERPROFILE '.vct'
    }
    return Join-Path $env:HOME '.vct'
}

function Read-TomlFallback {
    param([string]$FieldName)

    $tomlPath = Join-Path (Resolve-VctStateDir) 'retrieval-tuning.toml'
    $tuning = [ordered]@{}
    foreach ($k in $DefaultTuning.Keys) {
        $tuning[$k] = $DefaultTuning[$k]
    }

    if (Test-Path -LiteralPath $tomlPath) {
        # Hand-rolled TOML reader — the spec subset we need is trivial
        # (5 lines of `name = number`), and PowerShell has no built-in
        # TOML parser. Each line:  ^name = floatlike$.
        try {
            foreach ($line in Get-Content -LiteralPath $tomlPath -ErrorAction Stop) {
                $trimmed = $line.Trim()
                if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
                $eq = $trimmed.IndexOf('=')
                if ($eq -lt 1) { continue }
                $name = $trimmed.Substring(0, $eq).Trim()
                $rawValue = $trimmed.Substring($eq + 1).Trim()
                if ($tuning.Contains($name)) {
                    [double]$parsed = 0
                    if ([double]::TryParse(
                        $rawValue,
                        [System.Globalization.NumberStyles]::Float,
                        [System.Globalization.CultureInfo]::InvariantCulture,
                        [ref]$parsed)) {
                        $tuning[$name] = $parsed
                    }
                }
            }
        } catch {
            Write-VctWarn "could not read $tomlPath ($_); using defaults"
            $tuning = [ordered]@{}
            foreach ($k in $DefaultTuning.Keys) {
                $tuning[$k] = $DefaultTuning[$k]
            }
        }
    }

    if ($FieldName) {
        if ($tuning.Contains($FieldName)) {
            Write-Output $tuning[$FieldName]
            return 0
        }
        return 4
    }
    Write-Output ($tuning | ConvertTo-Json -Compress)
    return 0
}

# ── Hub-first attempt via the resolver client sibling ──────────────────
$thisDir = Split-Path -Parent $PSCommandPath
$resolverClient = Join-Path $thisDir 'vct_project_config.ps1'

$hubBody = $null
$hubRc = 1
if (Test-Path -LiteralPath $resolverClient) {
    try {
        # The PS resolver client prints the requested field's JSON
        # representation on stdout. We always ask for the nested
        # retrieval_tuning object then extract the leaf field locally
        # (matches the bash sibling's strategy).
        $hubBody = & pwsh -NoProfile -File $resolverClient `
            -ProjectFolder $ProjectFolder `
            -Field 'retrieval_tuning' 2>$null
        $hubRc = $LASTEXITCODE
    } catch {
        $hubBody = $null
        $hubRc = 1
    }
} else {
    Write-VctWarn "resolver client not found at $resolverClient; using file fallback"
    $hubRc = 1
}

if ($hubRc -eq 0 -and $hubBody) {
    try {
        $obj = $hubBody | ConvertFrom-Json -ErrorAction Stop
    } catch {
        Write-VctWarn "could not parse hub response as JSON; using file fallback"
        $hubRc = 1
    }
    if ($hubRc -eq 0) {
        if ($Field) {
            if ($obj.PSObject.Properties.Name -contains $Field) {
                Write-Output ($obj.$Field)
                exit 0
            }
            Write-VctWarn "field $Field not in retrieval_tuning envelope (hub mode)"
            exit 4
        }
        Write-Output ($obj | ConvertTo-Json -Compress)
        exit 0
    }
}

# Propagate hub-side hard errors (2 / 3 / 4) directly; 1 (hub unreachable)
# triggers the file fallback below.
switch ($hubRc) {
    2 { exit 2 }
    3 { exit 3 }
    4 { exit 4 }
}

Write-VctWarn "hub unreachable; reading <vct_root_dir>/retrieval-tuning.toml directly"
$rc = Read-TomlFallback -FieldName $Field
exit $rc
