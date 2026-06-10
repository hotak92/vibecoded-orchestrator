# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# vct_access_check.ps1 — KG access matrix gate client (v0.2.49 Phase 8).
#
# PowerShell counterpart of `vct_access_check.sh`. See the .sh header
# for the full contract; this file mirrors the bash version's behaviour
# using PowerShell-native primitives (Invoke-WebRequest with explicit
# timeout + error suppression, Write-Host -ForegroundColor Yellow for
# stderr WARNING emission).
#
# Compatibility (v0.2.53 Track H W-P1-4): supports BOTH Windows
# PowerShell 5.1 (bundled with every Win7+ install) and PowerShell 7+
# (pwsh.exe — separate install). Earlier revisions used the PS 7+ only
# `-SkipHttpErrorCheck` flag, which parse-fails on PS 5.1 and bricked
# the access-matrix gate on every stock Windows machine. The new
# try/catch wrapper covers both modes: PS 5.1 throws WebException on
# 4xx/5xx (response readable via $_.Exception.Response); PS 7 throws
# HttpResponseException (response readable via $_.ErrorDetails.Message
# or $_.Exception.Response). The hub-unreachable fail-open contract is
# unchanged across both PS versions.
#
# Hub contract (consumed; main chat lands the server side at
# `launcher/src-tauri/vct-hub/src/project_state_api.rs`):
#
#   GET http://127.0.0.1:${VCT_HUB_PORT}/api/v1/projects/{id}/access/{collection}
#   Headers: Authorization: Bearer <hub.token>
#   Response 200: {"level": "read" | "write" | "none"}
#   Response 401/404/5xx OR timeout → fail-open default "write"
#
# Fail-open contract: the script ALWAYS prints a valid level + exits 0.
# Hub unreachable / authentication failed / project missing → print
# "write" + emit ONE stderr warning + log a dropped-write-metric row.
# This is DELIBERATE: a closed-circuit policy would brick all KG writes
# during a launcher restart, which is unacceptable UX. The warning
# surfaces the degraded state so the user notices.
#
# Usage:
#   .\vct_access_check.ps1 <project_id> <collection_name>
#     Prints "read" | "write" | "none" on stdout. Exit 0 always.
#
# Hub discovery + auth: mirrors vct_project_config.ps1 (port file,
# token file, env-var overrides for tests).
#
# ── Rate-limit scope (cross-client documentation) ──────────────────────
#
# This script keys its rate-limit on "$PID:$reason" (PID-scoped). Every
# hook invocation is a fresh PowerShell process, so PID-scoped means
# EACH hook firing emits at least one WARNING per failure reason. That
# matches `vct_access_check.sh` byte-for-byte and is INTENTIONAL — for
# ephemeral hook callers we want the user to SEE the degraded state
# every time it occurs, not have it silently suppressed by a long-lived
# rate-limit window.
#
# Contrast: `vco_lib/access_resolver.py` (consumed by the long-running
# MCP server `claude_mcp_servers/weaviate_mcp/server.py`) uses a
# process-scoped rate-limit (just "$reason", no PID prefix) so a single
# MCP process doesn't spam WARNINGs every 5 minutes for the same
# persistent failure.
#
# The divergence is by design: hook invocations are episodic and
# user-visible; MCP processes are long-lived and emit through Python
# `logging`. If you find yourself thinking "should I align these?" the
# answer is no — read this paragraph again.
#
# Dropped-write metric: every fail-open emission appends a row to
# $env:VCT_STATE_DIR\cache\dropped_writes.jsonl with timestamp +
# project_id + collection + reason. Caller can ingest this for
# observability.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$ProjectId,

    [Parameter(Mandatory = $true, Position = 1)]
    [string]$Collection
)

# v0.2.49 Phase 8: resolver protocol version. MUST stay in lock-step
# with the bash sibling's RESOLVER_PROTOCOL_VERSION constant and
# vco_lib/access_resolver.py's same-named module attribute.
$Script:RESOLVER_PROTOCOL_VERSION = 1

# ── Hub discovery (mirrors vct_project_config.ps1 / vct_access_check.sh) ─

function Get-AccessStateDir {
    if ($Env:VCT_STATE_DIR) { return $Env:VCT_STATE_DIR }
    return (Join-Path $HOME ".vct")
}

function Get-AccessHubPort {
    if ($Env:VCT_HUB_PORT) {
        try { return [int]$Env:VCT_HUB_PORT } catch { }
    }
    $portFile = Join-Path (Get-AccessStateDir) "hub.port"
    if (Test-Path $portFile) {
        try {
            $raw = (Get-Content -LiteralPath $portFile -Raw -ErrorAction Stop).Trim()
            if ($raw -match '^[0-9]+$') { return [int]$raw }
        } catch { }
    }
    return 7700
}

function Get-AccessHubToken {
    if ($Env:VCT_HUB_TOKEN) { return $Env:VCT_HUB_TOKEN }
    $tokenFile = Join-Path (Get-AccessStateDir) "hub.token"
    if (Test-Path $tokenFile) {
        try {
            $raw = (Get-Content -LiteralPath $tokenFile -Raw -ErrorAction Stop).Trim()
            if ($raw) { return $raw }
        } catch { }
    }
    return ""
}

# ── Fail-open path: print "write", emit metric, log warning ─────────────

function Write-AccessMetric {
    param([string]$Reason)
    try {
        $cacheDir = Join-Path (Get-AccessStateDir) "cache"
        if (-not (Test-Path $cacheDir)) {
            New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null
        }
        $jsonl = Join-Path $cacheDir "dropped_writes.jsonl"
        $ts = [long][Math]::Floor(([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()))
        # ConvertTo-Json with -Compress keeps it one row per line. Quote-
        # escape via -ScriptBlock (uses PowerShell's own escaper) so a
        # weird collection_name like `My"KG` doesn't break the JSONL.
        $row = [pscustomobject]@{
            ts         = $ts
            project_id = $ProjectId
            collection = $Collection
            reason     = $Reason
            fail_open  = $true
        }
        $line = $row | ConvertTo-Json -Compress -Depth 3
        # Append with no BOM (PowerShell 7's Add-Content defaults to
        # UTF-8 no-BOM). On 5.1 the default would be Default codepage —
        # but we require 7+ per the script header so this is safe.
        Add-Content -LiteralPath $jsonl -Value $line -Encoding utf8 -ErrorAction SilentlyContinue
    } catch {
        # Metric write failure must not break the fail-open contract.
    }
}

function Test-AccessShouldSuppressWarning {
    param([string]$Key, [long]$Now)
    if ($Env:VCO_HOOK_DEBUG -eq '1') { return $false }
    $cacheDir = Join-Path (Get-AccessStateDir) "cache"
    $jsonl = Join-Path $cacheDir "access_check_warn.jsonl"
    if (-not (Test-Path $jsonl)) { return $false }
    $marker = "`"key`":`"$Key`""
    $cutoff = $Now - 300
    try {
        $lines = Get-Content -LiteralPath $jsonl -ErrorAction Stop
    } catch { return $false }
    foreach ($line in $lines) {
        if ($line -and $line.Contains($marker)) {
            if ($line -match '"ts":([0-9]+)') {
                $ts = [long]$Matches[1]
                if ($ts -ge $cutoff) { return $true }
            }
        }
    }
    return $false
}

function Write-AccessWarning {
    param([string]$Reason)
    $now = [long][Math]::Floor(([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()))
    # PID-scoped key — matches bash sibling. See "Rate-limit scope"
    # docstring at top.
    $key = "${PID}:${Reason}"

    if (Test-AccessShouldSuppressWarning -Key $key -Now $now) { return }

    try {
        $cacheDir = Join-Path (Get-AccessStateDir) "cache"
        if (-not (Test-Path $cacheDir)) {
            New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null
        }
        $jsonl = Join-Path $cacheDir "access_check_warn.jsonl"
        $row = [pscustomobject]@{
            ts     = $now
            pid    = $PID
            key    = $key
            reason = $Reason
        }
        $line = $row | ConvertTo-Json -Compress -Depth 3
        Add-Content -LiteralPath $jsonl -Value $line -Encoding utf8 -ErrorAction SilentlyContinue
    } catch { }

    [Console]::Error.WriteLine(
        "[vct-access-check] WARNING: hub unreachable ($Reason); failing open to write level (rate-limited)"
    )
}

function Invoke-AccessFailOpen {
    param([string]$Reason)
    Write-AccessMetric -Reason $Reason
    Write-AccessWarning -Reason $Reason
    Write-Output 'write'
    exit 0
}

# ── Main ─────────────────────────────────────────────────────────────────

$hubToken = Get-AccessHubToken
if (-not $hubToken) {
    Invoke-AccessFailOpen -Reason 'no_hub_token'
}

$hubPort = Get-AccessHubPort
$url = "http://127.0.0.1:${hubPort}/api/v1/projects/${ProjectId}/access/${Collection}"

# Invoke-WebRequest with 5s timeout, PS 5.1- and PS 7-compatible
# (W-P1-4). The previous version used `-SkipHttpErrorCheck` which is
# PS 7+ only — under PS 5.1 the script parse-failed before running,
# defeating the access-matrix gate on stock Windows (the parse error
# was caught by the hook caller's own fallback as "no stdout → assume
# write", which technically still allowed writes, but bypassed the
# entire policy mechanism). The new try/catch chain handles 4xx/5xx
# uniformly on both PS versions; only true connection failures (no
# response object) fall into Invoke-AccessFailOpen.
$response = $null
$statusCode = 0
$responseBody = ''
try {
    $response = Invoke-WebRequest `
        -Uri $url `
        -Method GET `
        -Headers @{ 'Authorization' = "Bearer $hubToken" } `
        -TimeoutSec 5 `
        -UseBasicParsing `
        -ErrorAction Stop
    $statusCode = [int]$response.StatusCode
    $responseBody = [string]$response.Content
} catch [System.Net.WebException] {
    # PS 5.1: 4xx/5xx throws WebException. Extract status + body.
    $webResp = $_.Exception.Response
    if ($null -eq $webResp) {
        Invoke-AccessFailOpen -Reason "url_error_$($_.Exception.GetType().Name)"
    }
    try { $statusCode = [int]$webResp.StatusCode } catch { $statusCode = 0 }
    try {
        $stream = $webResp.GetResponseStream()
        if ($null -ne $stream) {
            $reader = New-Object System.IO.StreamReader($stream)
            try {
                $responseBody = $reader.ReadToEnd()
            } finally {
                $reader.Dispose()
            }
        }
    } catch {
        $responseBody = ''
    }
    if ($statusCode -le 0) {
        Invoke-AccessFailOpen -Reason "url_error_$($_.Exception.GetType().Name)"
    }
} catch [Microsoft.PowerShell.Commands.HttpResponseException] {
    # PS 7: HttpResponseException carries Response + ErrorDetails.Message.
    $psResp = $_.Exception.Response
    if ($null -eq $psResp) {
        Invoke-AccessFailOpen -Reason "url_error_$($_.Exception.GetType().Name)"
    }
    try { $statusCode = [int]$psResp.StatusCode } catch { $statusCode = 0 }
    try {
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $responseBody = [string]$_.ErrorDetails.Message
        }
    } catch { $responseBody = '' }
    if ($statusCode -le 0) {
        Invoke-AccessFailOpen -Reason "url_error_$($_.Exception.GetType().Name)"
    }
} catch {
    # Connection refused, DNS failure, TLS error, etc. — all fail-open.
    Invoke-AccessFailOpen -Reason "url_error_$($_.Exception.GetType().Name)"
}

if ($statusCode -ge 500) {
    Invoke-AccessFailOpen -Reason "hub_5xx_${statusCode}"
}
if ($statusCode -eq 401) {
    Invoke-AccessFailOpen -Reason 'hub_auth_401'
}
if ($statusCode -eq 404) {
    # 404 = project_id not registered OR no access row for this
    # collection. Per the hub contract, the row-absent default IS
    # "none" — but per the fail-open spirit (over-grant on degraded
    # state rather than block legitimate writes), 404 yields "write"
    # with a metric emission so the user can investigate WHY their
    # project isn't registered. Matches bash + Python siblings.
    Invoke-AccessFailOpen -Reason 'hub_404_no_row'
}
if ($statusCode -ne 200) {
    Invoke-AccessFailOpen -Reason "hub_unexpected_${statusCode}"
}

# Parse {"level": "..."} from response body. Strict allowlist on the
# returned value — anything outside {read, write, none} is treated as
# malformed.
#
# $responseBody is populated by the try/catch chain above for ALL
# branches that reach this point (statusCode == 200). The successful
# Invoke-WebRequest branch copies $response.Content; the WebException
# / HttpResponseException branches read the response stream / error
# details. (The 200-OK path in PS 5.1 doesn't throw, so $response is
# populated and we already wrote $response.Content into $responseBody.)
$body = $responseBody
$level = ''
try {
    $parsed = $body | ConvertFrom-Json -ErrorAction Stop
    if ($parsed -and $parsed.PSObject.Properties['level']) {
        $level = [string]$parsed.level
    }
} catch {
    Invoke-AccessFailOpen -Reason 'hub_malformed_json'
}

if (-not $level -or ($level -notin @('read', 'write', 'none'))) {
    Invoke-AccessFailOpen -Reason 'hub_malformed_level'
}

Write-Output $level
exit 0
