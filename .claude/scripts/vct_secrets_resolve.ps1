# vct_secrets_resolve.ps1 — bridge between bundled wrappers/hooks and the
# launcher's hub HTTP API. PowerShell counterpart of
# `vct_secrets_resolve.sh`. See the .sh header for the architectural
# context (post-Fix-#3 cleanup, hub is the source-of-truth, no file-side
# secret mirror).
#
# Usage:
#   .\vct_secrets_resolve.ps1 <project_id_or_folder> <secret_key>
#       Print the value of <secret_key> on stdout. Exits with:
#         0  success
#         1  hub unreachable
#         2  project not registered
#         3  key not active for this project
#         4  key not found in hub response
#
#   .\vct_secrets_resolve.ps1 resolve-project <folder>
#       Print the project_id for <folder>. Same exit codes (0/1/2).
#
# Hub discovery:
#   1. $Env:VCT_HUB_PORT
#   2. ${VCT_STATE_DIR or $HOME\.vct}\hub.port
#   3. Default 7700.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Arg1,

    [Parameter(Mandatory = $true, Position = 1)]
    [string]$Arg2
)

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# Mirrors `templates/scripts/vct_secrets_resolve.sh` — kept in lockstep
# by the template-drift gate. Both copies (templates/ + .claude/) are
# byte-identical; the hub owns the project-root lookup.
# VCO-REWIRE-END: orchestrator-root-resolution

function Write-Err {
    param([string]$Message)
    [Console]::Error.WriteLine("[vct-secrets-resolve] $Message")
}

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

function Invoke-Hub {
    param([string]$PathAndQuery)
    $port = Get-HubPort
    $url = "http://127.0.0.1:$port/api/v1/$PathAndQuery"
    try {
        $response = Invoke-WebRequest -Uri $url -Method Get -UseBasicParsing `
            -TimeoutSec 5 -ErrorAction Stop
        return @{
            Status = [int]$response.StatusCode
            Body   = $response.Content
        }
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        if ($resp -ne $null) {
            $stream = $resp.GetResponseStream()
            $reader = New-Object System.IO.StreamReader($stream)
            $body = $reader.ReadToEnd()
            return @{
                Status = [int]$resp.StatusCode
                Body   = $body
            }
        }
        return $null  # connection refused / DNS / etc.
    } catch {
        # Newer .NET wraps the same in System.Net.Http.HttpRequestException.
        if ($_.Exception.Response) {
            $resp = $_.Exception.Response
            return @{
                Status = [int]$resp.StatusCode
                Body   = ($resp.Content.ReadAsStringAsync().Result)
            }
        }
        return $null
    }
}

function Test-LooksLikePath {
    param([string]$Value)
    return ($Value.StartsWith("/") -or $Value.StartsWith("./") -or
            $Value.StartsWith("../") -or $Value.Contains("/") -or
            $Value.Contains("\") -or $Value.Length -ge 2 -and $Value.Substring(1, 1) -eq ":")
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

function Read-Key {
    param([string]$ProjectArg, [string]$Key)
    $resolved = Resolve-ProjectId -ArgValue $ProjectArg
    if ($resolved.ExitCode -ne 0) {
        return $resolved.ExitCode
    }
    $pid_ = $resolved.Value
    $encodedPid = [System.Uri]::EscapeDataString($pid_)
    $encodedKey = [System.Uri]::EscapeDataString($Key)
    $result = Invoke-Hub "projects/$encodedPid/env?key=$encodedKey"
    if ($null -eq $result) {
        Write-Err "hub unreachable; is the launcher running?"
        return 1
    }
    switch ($result.Status) {
        200 {
            try {
                $obj = $result.Body | ConvertFrom-Json
            } catch {
                Write-Err "hub returned 200 but body is not JSON; body=$($result.Body)"
                return 4
            }
            $val = $obj.PSObject.Properties[$Key]
            if ($null -eq $val) {
                Write-Err "hub returned 200 but no $Key field; body=$($result.Body)"
                return 4
            }
            [Console]::Out.Write($val.Value)
            return 0
        }
        404 {
            try {
                $obj = $result.Body | ConvertFrom-Json
                $code = $obj.error.code
            } catch { $code = "unknown" }
            switch ($code) {
                "project_not_found" {
                    Write-Err "project $pid_ not found in launcher.db"
                    return 2
                }
                "key_not_active" {
                    Write-Err "key $Key not active for project $pid_ (paused, or not declared by any installed module)"
                    return 3
                }
                Default {
                    Write-Err "hub 404 with unknown code $code; body=$($result.Body)"
                    return 4
                }
            }
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
$ErrorActionPreference = "Stop"

if ($Arg1 -eq "resolve-project") {
    $resolved = Resolve-ProjectId -ArgValue $Arg2
    if ($resolved.ExitCode -ne 0) {
        exit $resolved.ExitCode
    }
    [Console]::Out.Write($resolved.Value)
    exit 0
}

$rc = Read-Key -ProjectArg $Arg1 -Key $Arg2
exit $rc
