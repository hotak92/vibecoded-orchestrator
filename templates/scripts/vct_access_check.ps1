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
# Requires PowerShell 7+. PowerShell 5.1 (Windows-bundled) has buggy
# Invoke-WebRequest error handling for non-2xx responses; users on
# Windows should install pwsh 7+ or shell out to WSL bash.
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

# Invoke-WebRequest with 5s timeout. -SkipHttpErrorCheck (PS 7+) lets us
# inspect non-2xx responses inline instead of having them throw. The
# overall try/catch still wraps in case the connection itself fails
# (refused, DNS, etc.).
$response = $null
$statusCode = 0
try {
    $response = Invoke-WebRequest `
        -Uri $url `
        -Method GET `
        -Headers @{ 'Authorization' = "Bearer $hubToken" } `
        -TimeoutSec 5 `
        -SkipHttpErrorCheck `
        -UseBasicParsing `
        -ErrorAction Stop
    $statusCode = [int]$response.StatusCode
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
$body = $response.Content
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
