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

# ── Rate-limited fall-through warning emission ─────────────────────────
#
# Mirrors templates/scripts/vct_project_config.sh _emit_warning() and
# vco_lib/resolver_warn.py emit_warning_if_allowed(). When the script
# falls through to its env-fallback path, ONE diagnostic line is
# emitted to stderr per (pid, error_kind) per 5-min window. The
# suppression state is persisted at
#     $env:VCT_STATE_DIR\cache\resolver_warn.jsonl
# (or $HOME\.vct\cache\resolver_warn.jsonl). VCO_HOOK_DEBUG=1 bypasses.
#
# Concurrency: writes go through System.IO.FileStream with FileShare.Read
# in a tight scope as a coarse mutex; concurrent writers serialize on
# the open. We don't use [System.Threading.Mutex] because Mutex on
# Linux pwsh is process-local (not OS-wide); a file-based lock matches
# the bash flock semantics.
#
# Rotation: when the JSONL exceeds 1 MiB, truncate to most-recent 100
# rows.

function Get-RWStateDir {
    if ($Env:VCT_STATE_DIR) { return $Env:VCT_STATE_DIR }
    return (Join-Path $HOME ".vct")
}

function Get-RWCacheDir { return (Join-Path (Get-RWStateDir) "cache") }
function Get-RWJsonlPath { return (Join-Path (Get-RWCacheDir) "resolver_warn.jsonl") }
function Get-RWLockPath { return (Join-Path (Get-RWCacheDir) "resolver_warn.jsonl.lock") }

function Initialize-RWCacheDir {
    $dir = Get-RWCacheDir
    if (-not (Test-Path $dir)) {
        try { New-Item -ItemType Directory -Path $dir -Force | Out-Null } catch { }
    }
}

function Test-RWShouldSuppress {
    param([string]$Key, [long]$Now)
    $jsonl = Get-RWJsonlPath
    if (-not (Test-Path $jsonl)) { return $false }
    $marker = "`"key`":`"$Key`""
    $lastTs = $null
    try {
        $lines = Get-Content -LiteralPath $jsonl -ErrorAction Stop
    } catch { return $false }
    foreach ($line in $lines) {
        if ($line -and $line.Contains($marker)) {
            if ($line -match '"ts":(\d+)') {
                $lastTs = [long]$Matches[1]
            }
        }
    }
    if ($null -eq $lastTs) { return $false }
    $delta = $Now - $lastTs
    return ($delta -ge 0 -and $delta -lt 300)
}

function Invoke-RWMaybeRotate {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    try {
        $size = (Get-Item -LiteralPath $Path).Length
    } catch { return }
    if ($size -le 1048576) { return }
    try {
        $tail = Get-Content -LiteralPath $Path -Tail 100 -ErrorAction Stop
        $tmp = "$Path.rot.tmp"
        Set-Content -LiteralPath $tmp -Value $tail -Encoding utf8 -NoNewline:$false
        Move-Item -LiteralPath $tmp -Destination $Path -Force
    } catch {
        try { Remove-Item -LiteralPath "$Path.rot.tmp" -ErrorAction SilentlyContinue } catch { }
    }
}

function ConvertTo-RWJsonString {
    param([string]$Value)
    if ($null -eq $Value) { return "" }
    $s = $Value
    $s = $s -replace '\\', '\\'
    $s = $s -replace '"', '\"'
    $s = $s -replace "`r", ' '
    $s = $s -replace "`n", ' '
    $s = $s -replace "`t", ' '
    return $s
}

function Emit-Warning {
    param(
        [Parameter(Mandatory = $true)][string]$ErrorKind,
        [string]$Detail = ''
    )
    $pid_ = $PID
    $consumer = Split-Path -Leaf $PSCommandPath
    if (-not $consumer) { $consumer = 'vct_project_config.ps1' }
    $key = "${pid_}:${ErrorKind}"
    $now = [long][Math]::Floor(([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()))

    Initialize-RWCacheDir

    if ($Env:VCO_HOOK_DEBUG -ne '1') {
        if (Test-RWShouldSuppress -Key $key -Now $now) { return }
    }

    # Stderr line: same fixed shape across all three resolver clients.
    [Console]::Error.WriteLine(
        "[vct] project_config: ${ErrorKind}: ${Detail}. Falling back to env. (rate-limited; set VCO_HOOK_DEBUG=1 to see every occurrence)"
    )

    # Cap detail at 200 chars (approximation of 200 bytes for ASCII-heavy text).
    $clipped = if ($Detail.Length -gt 200) { $Detail.Substring(0, 200) } else { $Detail }
    $user = if ($Env:USER) { $Env:USER } elseif ($Env:USERNAME) { $Env:USERNAME } else { 'unknown' }

    $row = '{{"ts":{0},"pid":{1},"consumer":"{2}","consumer_pid":{1},"error_kind":"{3}","key":"{4}","detail":"{5}","user":"{6}"}}' -f `
        $now, $pid_, `
        (ConvertTo-RWJsonString $consumer), `
        (ConvertTo-RWJsonString $ErrorKind), `
        (ConvertTo-RWJsonString $key), `
        (ConvertTo-RWJsonString $clipped), `
        (ConvertTo-RWJsonString $user)

    $jsonl = Get-RWJsonlPath
    $lockPath = Get-RWLockPath
    # Coarse mutex: open the lockfile with FileShare.None so concurrent
    # writers queue. Then append to the JSONL with FileShare.Read. Wrap
    # in retry-loop (50 ms × 20) for race robustness.
    $lockStream = $null
    $attempts = 0
    while ($null -eq $lockStream -and $attempts -lt 20) {
        try {
            $lockStream = [System.IO.File]::Open(
                $lockPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
        } catch {
            Start-Sleep -Milliseconds 50
            $attempts++
        }
    }
    try {
        Add-Content -LiteralPath $jsonl -Value $row -Encoding utf8 -ErrorAction Stop
    } catch {
        # Append failed (disk full etc.) — warning was already emitted.
    } finally {
        if ($null -ne $lockStream) {
            try { $lockStream.Close() } catch { }
            try { $lockStream.Dispose() } catch { }
        }
    }

    Invoke-RWMaybeRotate -Path $jsonl
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
        Emit-Warning -ErrorKind "hub_unreachable" -Detail "hub unreachable; is the launcher running?"
        return @{ ExitCode = 1 }
    }
    switch ($result.Status) {
        0 {
            Emit-Warning -ErrorKind "hub_unreachable" -Detail "hub.token missing; is the launcher running?"
            return @{ ExitCode = 1 }
        }
        401 {
            Emit-Warning -ErrorKind "hub_unauthorized" -Detail "401 unauthorized on by-path; launcher may have restarted (token rotated)"
            return @{ ExitCode = 1 }
        }
        200 {
            try {
                $obj = $result.Body | ConvertFrom-Json
            } catch {
                Emit-Warning -ErrorKind "hub_unreachable" -Detail "by-path 200 but body is not JSON; body=$($result.Body)"
                return @{ ExitCode = 2 }
            }
            if (-not $obj.id) {
                Emit-Warning -ErrorKind "hub_unreachable" -Detail "by-path 200 but no .id field; body=$($result.Body)"
                return @{ ExitCode = 2 }
            }
            return @{ ExitCode = 0; Value = $obj.id }
        }
        404 {
            Emit-Warning -ErrorKind "project_not_registered" -Detail "no project registered at path: $ArgValue"
            return @{ ExitCode = 2 }
        }
        400 {
            Emit-Warning -ErrorKind "project_not_registered" -Detail "hub rejected path query: $($result.Body)"
            return @{ ExitCode = 2 }
        }
        Default {
            Emit-Warning -ErrorKind "hub_unreachable" -Detail "hub returned status $($result.Status) for by-path lookup; body=$($result.Body)"
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
        Emit-Warning -ErrorKind "hub_unreachable" -Detail "hub unreachable; is the launcher running?"
        return 1
    }
    switch ($result.Status) {
        0 {
            Emit-Warning -ErrorKind "hub_unreachable" -Detail "hub.token missing; is the launcher running?"
            return 1
        }
        401 {
            Emit-Warning -ErrorKind "hub_unauthorized" -Detail "401 unauthorized; launcher may have restarted (token rotated)"
            return 1
        }
        200 {
            if ($FieldName) {
                # Single-field envelope: {"<field>": <value>}. Unwrap.
                try {
                    $obj = $result.Body | ConvertFrom-Json
                } catch {
                    Emit-Warning -ErrorKind "field_decode_failed" -Detail "200 but body is not JSON; body=$($result.Body)"
                    return 4
                }
                $prop = $obj.PSObject.Properties[$FieldName]
                if ($null -eq $prop) {
                    Emit-Warning -ErrorKind "field_not_found" -Detail "field $FieldName not present in hub response; body=$($result.Body)"
                    return 4
                }
                $val = $prop.Value
                if ($null -eq $val) {
                    Emit-Warning -ErrorKind "field_decode_failed" -Detail "field $FieldName decoded null from hub response"
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
                    Emit-Warning -ErrorKind "project_not_registered" -Detail "project $ProjectId not registered in launcher.db"
                    return 2
                }
                "field_not_found" {
                    Emit-Warning -ErrorKind "field_not_found" -Detail "field $FieldName not in config for project $ProjectId"
                    return 4
                }
                Default {
                    Emit-Warning -ErrorKind "field_not_found" -Detail "hub 404 with unknown code $code; body=$($result.Body)"
                    return 4
                }
            }
        }
        503 {
            Emit-Warning -ErrorKind "service_misconfigured" -Detail "503 for project $ProjectId (primary KG binding missing — fix in launcher GUI)"
            return 3
        }
        400 {
            Emit-Warning -ErrorKind "field_not_found" -Detail "hub rejected request: $($result.Body)"
            return 4
        }
        Default {
            Emit-Warning -ErrorKind "hub_unreachable" -Detail "hub returned status $($result.Status); body=$($result.Body)"
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
