# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# vct_project_config.ps1 — PowerShell counterpart of
# `vct_project_config.sh`. See the .sh header for the full contract;
# this file mirrors the bash version's behaviour using PowerShell-
# native primitives (Invoke-WebRequest with -SkipHttpErrorCheck so we
# can read non-200 responses inline, named [CmdletBinding()] params).
#
# Requires PowerShell 7+ for -SkipHttpErrorCheck. PowerShell 5.1
# (Windows-bundled) is NOT supported; users on Windows should either
# install pwsh 7+ or shell out to WSL bash.
#
# Usage:
#   .\vct_project_config.ps1 -Project <folder> [-Field <name>]
#       Print the full config JSON for <folder>, or one field's value.
#   .\vct_project_config.ps1 -ResolveProject <folder>
#       Print the project_id (UUID) for <folder>.
#
# Exit codes (identical to bash):
#   0  success
#   1  hub unreachable
#   2  project not registered
#   3  service misconfigured (primary KG binding missing)
#   4  field not found
#  64  usage error

[CmdletBinding(DefaultParameterSetName = 'Config')]
param(
    [Parameter(ParameterSetName = 'Config', Mandatory = $true, Position = 0)]
    [string]$Project,

    [Parameter(ParameterSetName = 'Config', Mandatory = $false)]
    [string]$Field,

    [Parameter(ParameterSetName = 'ResolveOnly', Mandatory = $true)]
    [string]$ResolveProject
)

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# Mirrors templates/scripts/vct_project_config.sh — kept in lockstep by
# the template-drift gate. Both copies share the same logic; the hub
# owns the project-root lookup.
# VCO-REWIRE-END: orchestrator-root-resolution

$ErrorActionPreference = 'Stop'

function Write-Err {
    param([string]$Message)
    [Console]::Error.WriteLine("[vct-project-config] $Message")
}

# ── Hub port discovery ──────────────────────────────────────────────────
function Get-HubPort {
    if ($Env:VCT_HUB_PORT) {
        return [int]$Env:VCT_HUB_PORT
    }
    $stateDir = if ($Env:VCT_STATE_DIR) { $Env:VCT_STATE_DIR } else { Join-Path $HOME ".vct" }
    $portFile = Join-Path $stateDir "hub.port"
    if (Test-Path $portFile) {
        $raw = (Get-Content -Raw -Path $portFile).Trim()
        if ($raw -match '^\d+$') {
            return [int]$raw
        }
    }
    return 7700
}

# ── Hub token discovery ─────────────────────────────────────────────────
function Get-HubToken {
    if ($Env:VCT_HUB_TOKEN) {
        $t = $Env:VCT_HUB_TOKEN.Trim()
        if ($t.Length -gt 0) { return $t }
    }
    $stateDir = if ($Env:VCT_STATE_DIR) { $Env:VCT_STATE_DIR } else { Join-Path $HOME ".vct" }
    $tokenFile = Join-Path $stateDir "hub.token"
    if (Test-Path $tokenFile) {
        $raw = (Get-Content -Raw -Path $tokenFile).Trim()
        if ($raw.Length -gt 0) { return $raw }
    }
    return $null
}

# ── HTTP helper ─────────────────────────────────────────────────────────
# Returns a hashtable: @{ Status = <int>; Body = <string> } on success,
# @{ Status = 0; Body = '' } if token is missing, or $null on
# connection failure. -SkipHttpErrorCheck makes 4xx/5xx fall through as
# normal responses (so we can read the error envelope body).
function Invoke-Hub {
    param([string]$PathAndQuery)
    $port = Get-HubPort
    $token = Get-HubToken
    if ($null -eq $token) {
        return @{ Status = 0; Body = '' }
    }
    $url = "http://127.0.0.1:${port}/api/v1/${PathAndQuery}"
    $headers = @{ Authorization = "Bearer $token" }
    try {
        $resp = Invoke-WebRequest -Uri $url -Method Get -UseBasicParsing `
            -Headers $headers -TimeoutSec 5 -SkipHttpErrorCheck `
            -ErrorAction Stop
        return @{ Status = [int]$resp.StatusCode; Body = $resp.Content }
    } catch {
        # Connection refused / DNS / TLS error — no response.
        return $null
    }
}

# ── Project ID resolution ───────────────────────────────────────────────
function Test-LooksLikePath {
    param([string]$Value)
    if ($Value.StartsWith("/") -or $Value.StartsWith("./") -or $Value.StartsWith("../")) { return $true }
    if ($Value.Contains("/") -or $Value.Contains("\")) { return $true }
    # Windows drive prefix: "C:..." etc.
    if ($Value.Length -ge 2 -and $Value.Substring(1, 1) -eq ":") { return $true }
    return $false
}

function Resolve-ProjectId {
    param([string]$ArgValue)
    if (-not (Test-LooksLikePath $ArgValue)) {
        return @{ ExitCode = 0; Value = $ArgValue }
    }
    $encoded = [System.Uri]::EscapeDataString($ArgValue)
    $result = Invoke-Hub "projects/by-path?path=$encoded"
    if ($null -eq $result) {
        Write-Err "hub unreachable; is the launcher running?"
        return @{ ExitCode = 1 }
    }
    switch ($result.Status) {
        0 {
            Write-Err "hub.token missing; is the launcher running?"
            return @{ ExitCode = 1 }
        }
        401 {
            Write-Err "hub returned 401 unauthorized; the launcher may have restarted (token rotated). Try again."
            return @{ ExitCode = 1 }
        }
        200 {
            try {
                $obj = $result.Body | ConvertFrom-Json
            } catch {
                Write-Err "hub returned 200 but body is not JSON; body=$($result.Body)"
                return @{ ExitCode = 2 }
            }
            if (-not $obj.id) {
                Write-Err "hub returned 200 but no .id field; body=$($result.Body)"
                return @{ ExitCode = 2 }
            }
            return @{ ExitCode = 0; Value = $obj.id }
        }
        404 {
            Write-Err "no project registered at path: $ArgValue"
            return @{ ExitCode = 2 }
        }
        400 {
            Write-Err "hub rejected path query: $($result.Body)"
            return @{ ExitCode = 2 }
        }
        Default {
            Write-Err "hub returned status $($result.Status) for by-path lookup; body=$($result.Body)"
            return @{ ExitCode = 1 }
        }
    }
}

# ── Fetch config ────────────────────────────────────────────────────────
function Get-Config {
    param([string]$ProjectId, [string]$FieldName)
    $encodedPid = [System.Uri]::EscapeDataString($ProjectId)
    $pathAndQuery = "projects/$encodedPid/config"
    if ($FieldName) {
        $encodedField = [System.Uri]::EscapeDataString($FieldName)
        $pathAndQuery += "?key=$encodedField"
    }
    $result = Invoke-Hub $pathAndQuery
    if ($null -eq $result) {
        Write-Err "hub unreachable; is the launcher running?"
        return 1
    }
    switch ($result.Status) {
        0 {
            Write-Err "hub.token missing; is the launcher running?"
            return 1
        }
        401 {
            Write-Err "hub returned 401 unauthorized; the launcher may have restarted (token rotated). Try again."
            return 1
        }
        200 {
            if ($FieldName) {
                # Single-field envelope: {"<field>": <value>}. Unwrap.
                try {
                    $obj = $result.Body | ConvertFrom-Json
                } catch {
                    Write-Err "hub returned 200 but body is not JSON; body=$($result.Body)"
                    return 4
                }
                $prop = $obj.PSObject.Properties[$FieldName]
                if ($null -eq $prop) {
                    Write-Err "field $FieldName not present in hub response; body=$($result.Body)"
                    return 4
                }
                $val = $prop.Value
                if ($null -eq $val) {
                    Write-Err "field $FieldName decoded null from hub response"
                    return 4
                }
                # Arrays / nested objects → emit compact JSON; scalars → raw.
                if ($val -is [System.Collections.IEnumerable] -and -not ($val -is [string])) {
                    [Console]::Out.Write(($val | ConvertTo-Json -Compress -Depth 8))
                } elseif ($val -is [psobject] -and -not ($val.GetType().IsPrimitive)) {
                    [Console]::Out.Write(($val | ConvertTo-Json -Compress -Depth 8))
                } else {
                    [Console]::Out.Write($val)
                }
                return 0
            } else {
                [Console]::Out.Write($result.Body)
                return 0
            }
        }
        404 {
            try {
                $obj = $result.Body | ConvertFrom-Json
                $code = $obj.error.code
            } catch { $code = "unknown" }
            switch ($code) {
                "project_not_found" {
                    Write-Err "project $ProjectId not registered in launcher.db"
                    return 2
                }
                "field_not_found" {
                    Write-Err "field $FieldName not in config for project $ProjectId"
                    return 4
                }
                Default {
                    Write-Err "hub 404 with unknown code $code; body=$($result.Body)"
                    return 4
                }
            }
        }
        503 {
            Write-Err "hub returned 503 service_misconfigured for project $ProjectId (primary KG binding missing — fix in launcher GUI)"
            return 3
        }
        400 {
            Write-Err "hub rejected request: $($result.Body)"
            return 4
        }
        Default {
            Write-Err "hub returned status $($result.Status); body=$($result.Body)"
            return 1
        }
    }
}

# ── Main ────────────────────────────────────────────────────────────────
if ($PSCmdlet.ParameterSetName -eq 'ResolveOnly') {
    $resolved = Resolve-ProjectId -ArgValue $ResolveProject
    if ($resolved.ExitCode -ne 0) {
        exit $resolved.ExitCode
    }
    [Console]::Out.Write($resolved.Value)
    exit 0
}

$resolved = Resolve-ProjectId -ArgValue $Project
if ($resolved.ExitCode -ne 0) {
    exit $resolved.ExitCode
}
$rc = Get-Config -ProjectId $resolved.Value -FieldName $Field
exit $rc
