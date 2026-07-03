# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# vct_project_config.ps1 — PowerShell counterpart of
# `vct_project_config.sh`. See the .sh header for the full contract;
# this file mirrors the bash version's behaviour using PowerShell-
# native primitives (Invoke-WebRequest in a portable try/catch wrapper
# that handles non-2xx responses on BOTH PS 5.1 and PS 7+).
#
# Compatibility (v0.2.53 Track H W-P1-4): supports BOTH Windows
# PowerShell 5.1 (bundled with every Win7+ install) and PowerShell 7+
# (pwsh.exe — separate install). Earlier revisions used the PS 7+ only
# `-SkipHttpErrorCheck` flag, which parse-fails on PS 5.1 and causes
# the access-matrix gate to fail closed — bricking KG writes on every
# stock Windows machine until the user manually installed pwsh 7. The
# new Invoke-HubRequest helper achieves the same "read non-2xx body
# inline" semantics by catching the WebException PS 5.1 throws and
# extracting StatusCode + body from the exception's Response property.
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

# Resolver protocol version this client understands. MUST stay in
# lock-step with `RESOLVER_PROTOCOL_VERSION` in vco_lib/project_config.py
# and the bash sibling. When the hub reports a HIGHER value, we emit
# one best-effort stderr warning (forward-compat safety net) and
# continue — the hub's response shape is additive across versions.
$Script:RESOLVER_PROTOCOL_VERSION = 1

# v0.2.24-A4: schema_version drift now routes through Emit-Warning's
# cross-invocation rate-limit (suppression key keyed on hub_version, not
# on PID). The process-local one-shot guard was removed — Emit-Warning's
# JSONL-backed suppression handles dedup across the entire 5-min window.
function Test-SchemaVersionWarning {
    param([string]$Body)
    if (-not $Body) { return }
    try {
        $obj = $Body | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return
    }
    # Missing field → pre-v0.2.22 hub. No warning.
    $prop = $obj.PSObject.Properties['schema_version']
    if ($null -eq $prop) { return }
    $raw = $prop.Value
    if ($null -eq $raw) { return }
    $hubVersion = 0
    if (-not [int]::TryParse([string]$raw, [ref]$hubVersion)) {
        # Malformed value — defensive degradation, no warning, no crash.
        return
    }
    if ($hubVersion -gt $Script:RESOLVER_PROTOCOL_VERSION) {
        $line = ("[vct_project_config] WARNING: hub schema_version={0} > client RESOLVER_PROTOCOL_VERSION={1}; some fields may be unknown. Update the orchestrator clone or downgrade the hub." -f `
            $hubVersion, $Script:RESOLVER_PROTOCOL_VERSION)
        # Suppression key keyed on the OBSERVED hub version (not on PID)
        # so every hook invocation against the same drifted hub shares
        # one 5-min window.
        Emit-Warning `
            -ErrorKind "schema_version_drift" `
            -Detail ("hub_version={0} client_version={1}" -f $hubVersion, $Script:RESOLVER_PROTOCOL_VERSION) `
            -SuppressKey ("schema_version_drift_{0}" -f $hubVersion) `
            -StderrLine $line
    }
}

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
    # Optional overrides (v0.2.24-A4):
    #   -SuppressKey  → replaces the default "<pid>:<ErrorKind>" key.
    #                   Use when the same warning should be suppressed
    #                   ACROSS PIDs (e.g. schema_version_drift_<v>).
    #   -StderrLine   → replaces the default
    #                   "[vct] project_config: ..." line verbatim.
    param(
        [Parameter(Mandatory = $true)][string]$ErrorKind,
        [string]$Detail = '',
        [string]$SuppressKey = '',
        [string]$StderrLine = ''
    )
    $pid_ = $PID
    $consumer = Split-Path -Leaf $PSCommandPath
    if (-not $consumer) { $consumer = 'vct_project_config.ps1' }
    if ($SuppressKey) {
        $key = $SuppressKey
    } else {
        $key = "${pid_}:${ErrorKind}"
    }
    $now = [long][Math]::Floor(([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()))

    Initialize-RWCacheDir

    if ($Env:VCO_HOOK_DEBUG -ne '1') {
        if (Test-RWShouldSuppress -Key $key -Now $now) { return }
    }

    # Stderr line: default fixed shape matches bash + python siblings.
    # Caller may pass -StderrLine to override (used by schema-version
    # drift so the legacy "[vct_project_config] WARNING: ..." format
    # stays stable across the rate-limit refactor).
    if ($StderrLine) {
        [Console]::Error.WriteLine($StderrLine)
    } else {
        [Console]::Error.WriteLine(
            "[vct] project_config: ${ErrorKind}: ${Detail}. Falling back to env. (rate-limited; set VCO_HOOK_DEBUG=1 to see every occurrence)"
        )
    }

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
# CORRUPT-INPUT CONTRACT (F-8) — MUST MATCH the bash sibling
# `vct_project_config.sh::hub_port` and the python sibling
# `vco_lib/project_config.py::_discover_hub` (port branch):
#   * env `VCT_HUB_PORT` or `hub.port` file that is non-numeric / garbage
#     → emit ONE rate-limited stderr warning (kind `hub_port_invalid`) and
#     fall through to the default port 7700. NEVER throw (the previous
#     `[int]$Env:VCT_HUB_PORT` cast raised a TERMINATING error that took
#     the Windows hook host down — F-8 item #2).
#   * `hub.port` present but UNREADABLE → warn (`hub_port_unreadable`) +
#     default. The previous `Get-Content -Raw` inside `Test-Path` had no
#     try/catch and threw on perm-denied (F-8 item #3).
# A valid numeric port matches `^\d+$`. ONE conservative contract:
# invalid content → warn + default, never crash, never emit garbage.
function Get-HubPort {
    if ($Env:VCT_HUB_PORT) {
        if ($Env:VCT_HUB_PORT -match '^\d+$') {
            return [int]$Env:VCT_HUB_PORT
        }
        Emit-Warning -ErrorKind "hub_port_invalid" `
            -Detail "VCT_HUB_PORT is not a positive integer; using default 7700"
        return 7700
    }
    $stateDir = if ($Env:VCT_STATE_DIR) { $Env:VCT_STATE_DIR } else { Join-Path $HOME ".vct" }
    $portFile = Join-Path $stateDir "hub.port"
    if (Test-Path $portFile) {
        $raw = $null
        try {
            $raw = (Get-Content -Raw -Path $portFile -ErrorAction Stop).Trim()
        } catch {
            Emit-Warning -ErrorKind "hub_port_unreadable" `
                -Detail "hub.port is not readable; using default 7700"
            return 7700
        }
        if ($raw -match '^\d+$') {
            return [int]$raw
        }
        if ($raw.Length -gt 0) {
            Emit-Warning -ErrorKind "hub_port_invalid" `
                -Detail "hub.port contains non-integer content; using default 7700"
        }
        # empty (whitespace-only / truncated write) → silent default.
    }
    return 7700
}

# ── Hub token discovery ─────────────────────────────────────────────────
# CORRUPT-INPUT CONTRACT (F-8) — MUST MATCH the bash sibling
# `vct_project_config.sh::hub_token` and the python sibling
# `vco_lib/project_config.py::_discover_hub` (token branch):
#   * env `VCT_HUB_TOKEN` set → used verbatim after trim (any non-empty
#     string is a legitimate token; no format to validate).
#   * `hub.token` present but UNREADABLE → emit ONE rate-limited stderr
#     warning (kind `hub_token_unreadable`) and return $null. The token
#     has NO sane default, so unreadable/absent → "no token" → caller
#     degrades to hub_unreachable. NEVER throw (the previous
#     `Get-Content -Raw` had no try/catch — F-8 item #3).
function Get-HubToken {
    if ($Env:VCT_HUB_TOKEN) {
        $t = $Env:VCT_HUB_TOKEN.Trim()
        if ($t.Length -gt 0) { return $t }
    }
    $stateDir = if ($Env:VCT_STATE_DIR) { $Env:VCT_STATE_DIR } else { Join-Path $HOME ".vct" }
    $tokenFile = Join-Path $stateDir "hub.token"
    if (Test-Path $tokenFile) {
        try {
            $raw = (Get-Content -Raw -Path $tokenFile -ErrorAction Stop).Trim()
        } catch {
            Emit-Warning -ErrorKind "hub_token_unreadable" `
                -Detail "hub.token is not readable; treating as no token"
            return $null
        }
        if ($raw.Length -gt 0) { return $raw }
    }
    return $null
}

# ── HTTP helper ─────────────────────────────────────────────────────────
# Returns a hashtable: @{ Status = <int>; Body = <string> } on success,
# @{ Status = 0; Body = '' } if token is missing, or $null on connection
# failure.
#
# Compatibility (W-P1-4): works on BOTH PS 7+ (where Invoke-WebRequest
# treats non-2xx as a normal response when -SkipHttpErrorCheck is set)
# AND PS 5.1 (where Invoke-WebRequest throws WebException on 4xx/5xx and
# we extract StatusCode + body from the exception's Response property).
# We deliberately do NOT use -SkipHttpErrorCheck — that flag parse-fails
# on PS 5.1 before the cmdlet runs, taking the whole script down with
# it. The try/catch below covers both modes uniformly.
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
            -Headers $headers -TimeoutSec 5 `
            -ErrorAction Stop
        return @{ Status = [int]$resp.StatusCode; Body = $resp.Content }
    } catch [System.Net.WebException] {
        # PS 5.1 throws WebException for 4xx/5xx. Extract status + body
        # from the response stream. If the exception carries no Response
        # (connection-level failure: refused, DNS, TLS handshake),
        # fall through to the catch-all below.
        $webResp = $_.Exception.Response
        if ($null -eq $webResp) {
            return $null
        }
        try {
            $status = [int]$webResp.StatusCode
        } catch {
            $status = 0
        }
        $body = ''
        try {
            $stream = $webResp.GetResponseStream()
            if ($null -ne $stream) {
                $reader = New-Object System.IO.StreamReader($stream)
                try {
                    $body = $reader.ReadToEnd()
                } finally {
                    $reader.Dispose()
                }
            }
        } catch {
            # Body read failed (stream already disposed, encoding error);
            # return what we have (status was the load-bearing field).
            $body = ''
        }
        if ($status -gt 0) {
            return @{ Status = $status; Body = $body }
        }
        return $null
    } catch [Microsoft.PowerShell.Commands.HttpResponseException] {
        # PS 7 path: HttpResponseException is the PS 7-native error type
        # thrown when -SkipHttpErrorCheck is NOT set. Same shape as the
        # WebException branch above — extract status + body.
        $psResp = $_.Exception.Response
        if ($null -eq $psResp) {
            return $null
        }
        $status = 0
        try { $status = [int]$psResp.StatusCode } catch { }
        # PS 7's $_.ErrorDetails.Message often carries the body verbatim.
        $body = ''
        try {
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                $body = [string]$_.ErrorDetails.Message
            }
        } catch { }
        if ($status -gt 0) {
            return @{ Status = $status; Body = $body }
        }
        return $null
    } catch {
        # Connection refused / DNS / TLS error / any other unmatched
        # exception — no response.
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
            # Forward-compat check: warn (once, best-effort) if the
            # hub reports a higher schema_version than we understand.
            # Single-field envelopes omit `schema_version`; the helper
            # treats that as "no warning" (defensive degradation).
            try { Test-SchemaVersionWarning -Body $result.Body } catch { }
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
                # v0.2.47 (extras): consumer-friendly render for
                # code_graph_extra_paths — array of {path, enabled,
                # last_indexed_commit?} on the wire, newline-delimited
                # paths of `enabled=true` rows on stdout. Matches the
                # bash sibling's shape so cross-OS callers see identical
                # output. Empty enabled set → no stdout + exit 0.
                if ($FieldName -eq "code_graph_extra_paths") {
                    if ($val -is [System.Collections.IEnumerable] -and -not ($val -is [string])) {
                        foreach ($row in $val) {
                            if ($null -eq $row) { continue }
                            $rowEnabled = $row.PSObject.Properties["enabled"]
                            $rowPath = $row.PSObject.Properties["path"]
                            if ($null -ne $rowEnabled -and $rowEnabled.Value -eq $true `
                                -and $null -ne $rowPath -and $rowPath.Value) {
                                [Console]::Out.WriteLine([string]$rowPath.Value)
                            }
                        }
                    }
                    return 0
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
