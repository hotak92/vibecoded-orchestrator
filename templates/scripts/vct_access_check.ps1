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
    return (Get-AccessHubTokenOnDisk)
}

# ── Stale-env hub-token fallback (v0.2.91, WP-D item 4) ─────────────────
#
# MUST MATCH the SSOT `vco_lib/project_config.py::_stale_env_token_fallback`
# and the mirrors in `vct_access_check.sh`, `vct_secrets_resolve.{sh,ps1}`,
# `vct_project_config.{sh,ps1}`, `claude_mcp_servers/wrappers/_base.py`,
# `launcher/tools/vct-cli/src/main.rs` and `tools/vct-secrets/vct`.
# Locked by tests/test_stale_env_token_parity_v0291.py.
#
# WHY here: `$Env:VCT_HUB_TOKEN` wins over the file, and the hub
# regenerates `hub.token` on every start — so a shell that exported the
# token before an update presents a dead credential, the hub 401s, and
# this client FAILS OPEN to "write" on every call, silently degrading the
# access gate to permissive for that shell's whole life while the on-disk
# token next to it would have answered correctly. After a PROVABLE
# refusal (401/403) we retry ONCE with the on-disk token.
#
# THE FAIL-OPEN CONTRACT IS UNCHANGED (deliberate availability choice):
# a genuine auth failure, an unreachable hub, a missing token, or a retry
# that is ALSO refused still fails open to "write" with the SAME reason
# string, warning and dropped-write metric row. This only makes the
# fail-open reached LESS OFTEN.
#
# GLOBAL TOKEN ONLY: `/projects/{id}/access/{collection}` is NOT a
# per-project-token route (the hub gates only `/env` + `/config` that
# way), so a scoped `hub.token.<id>` would itself 401 here. Unlike the
# resolver quadruplet, this mirror deliberately has no scoped branch.

# The ONE definitive line. Byte-identical across every mirror.
$Script:StaleEnvTokenMessage = 'stale VCT_HUB_TOKEN in env overridden by on-disk hub.token — run `unset VCT_HUB_TOKEN` or open a new shell'

function Get-AccessHubTokenOnDisk {
    # The ON-DISK global token, IGNORING $Env:VCT_HUB_TOKEN. Empty string
    # when nothing readable exists.
    $tokenFile = Join-Path (Get-AccessStateDir) "hub.token"
    if (Test-Path $tokenFile) {
        try {
            $raw = (Get-Content -LiteralPath $tokenFile -Raw -ErrorAction Stop).Trim()
            if ($raw) { return $raw }
        } catch { }
    }
    return ""
}

function Write-AccessStaleEnvTokenWarning {
    # NOT a fail-open event: no dropped-write metric row, no "hub
    # unreachable" phrasing. The gate WORKED — we just had to reach past
    # a dead env pin. One line, always emitted (this script is one-shot
    # and its stderr policy is deliberately per-PID).
    [Console]::Error.WriteLine(
        "[vct-access-check] WARNING: $Script:StaleEnvTokenMessage"
    )
}

function Test-AccessStaleEnvRetryIsDefinitive {
    # $true when a stale-env RETRY's status PROVES the fallback credential
    # was accepted: 2xx, or 404 (the hub answers "no row" only AFTER its
    # auth middleware accepted the bearer — a post-auth answer like a 200).
    # Status 0 means the retry never reached the hub. MUST MATCH the SSOT
    # `vco_lib/project_config.py::_retry_answer_is_definitive`.
    param([int]$Status)
    if ($Status -ge 200 -and $Status -lt 300) { return $true }
    return ($Status -eq 404)
}

function Get-AccessStaleEnvFallbackToken {
    # Returns the on-disk token to retry with, or $null. Rules, in order
    # (identical in every mirror):
    #   1. VCT_HUB_TOKEN_STRICT=1        → $null (the pin is authoritative)
    #   2. VCT_HUB_TOKEN unset/empty     → $null (nothing was pinned)
    #   3. no readable on-disk token     → $null (nothing better to try)
    #   4. on-disk == env (trimmed)      → $null (the pin is not stale)
    # Trimmed, case-sensitive comparison to the literal 1 — the SSOT's
    # spelling, so a value written with a trailing newline/CR means the
    # same thing in bash, PowerShell, Rust and Python.
    if ($Env:VCT_HUB_TOKEN_STRICT -and
        $Env:VCT_HUB_TOKEN_STRICT.Trim() -ceq '1') { return $null }
    if (-not $Env:VCT_HUB_TOKEN) { return $null }
    $envTok = $Env:VCT_HUB_TOKEN.Trim()
    if ($envTok.Length -eq 0) { return $null }
    $diskTok = Get-AccessHubTokenOnDisk
    if (-not $diskTok) { return $null }
    if ($diskTok -ceq $envTok) { return $null }
    return $diskTok
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

# ── One request with an explicit bearer ──────────────────────────────────
#
# Extracted (v0.2.91) so the stale-env retry can re-issue the SAME call
# with a different token. On the FIRST attempt every catch arm keeps its
# original `Invoke-AccessFailOpen` behaviour — those are terminal (exit 0)
# by design, so a connection-level failure still fails open immediately.
#
# `-NoFailOpen` is for the RETRY only (v0.2.91 wave-3, MINOR-2). Without
# it, a retry whose connection failed EXITED with `url_error_*` — while
# the bash sibling, whose `do_request` merely returns non-zero, keeps the
# ORIGINAL `hub_auth_401`. Two clients emitting different reasons for the
# same event breaks cross-client aggregation of `dropped_writes.jsonl`,
# and the exiting one is the wrong answer besides: a transport failure on
# the retry says nothing about the request we actually made. With the
# switch a transport failure returns `Status = 0`, which the caller's
# adopt gate rejects, so the original refusal stands.
function Invoke-AccessRequest {
    param([string]$Token, [string]$Url, [switch]$NoFailOpen)

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
        -Uri $Url `
        -Method GET `
        -Headers @{ 'Authorization' = "Bearer $Token" } `
        -TimeoutSec 5 `
        -UseBasicParsing `
        -ErrorAction Stop
    $statusCode = [int]$response.StatusCode
    $responseBody = [string]$response.Content
} catch [System.Net.WebException] {
    # PS 5.1: 4xx/5xx throws WebException. Extract status + body.
    $webResp = $_.Exception.Response
    if ($null -eq $webResp) {
        if ($NoFailOpen) { return @{ Status = 0; Body = '' } }
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
        if ($NoFailOpen) { return @{ Status = 0; Body = '' } }
        Invoke-AccessFailOpen -Reason "url_error_$($_.Exception.GetType().Name)"
    }
} catch [Microsoft.PowerShell.Commands.HttpResponseException] {
    # PS 7: HttpResponseException carries Response + ErrorDetails.Message.
    $psResp = $_.Exception.Response
    if ($null -eq $psResp) {
        if ($NoFailOpen) { return @{ Status = 0; Body = '' } }
        Invoke-AccessFailOpen -Reason "url_error_$($_.Exception.GetType().Name)"
    }
    try { $statusCode = [int]$psResp.StatusCode } catch { $statusCode = 0 }
    try {
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $responseBody = [string]$_.ErrorDetails.Message
        }
    } catch { $responseBody = '' }
    if ($statusCode -le 0) {
        if ($NoFailOpen) { return @{ Status = 0; Body = '' } }
        Invoke-AccessFailOpen -Reason "url_error_$($_.Exception.GetType().Name)"
    }
} catch {
    # Connection refused, DNS failure, TLS error, etc. — all fail-open.
    if ($NoFailOpen) { return @{ Status = 0; Body = '' } }
    Invoke-AccessFailOpen -Reason "url_error_$($_.Exception.GetType().Name)"
}
    return @{ Status = $statusCode; Body = $responseBody }
}

# ── Main ─────────────────────────────────────────────────────────────────

$hubToken = Get-AccessHubToken
if (-not $hubToken) {
    Invoke-AccessFailOpen -Reason 'no_hub_token'
}

$hubPort = Get-AccessHubPort
$url = "http://127.0.0.1:${hubPort}/api/v1/projects/${ProjectId}/access/${Collection}"

$attempt = Invoke-AccessRequest -Token $hubToken -Url $url
$statusCode = $attempt.Status
$responseBody = $attempt.Body

# v0.2.91 (WP-D item 4): a PROVABLE credential refusal is the ONLY
# trigger for the one-shot on-disk-token retry. A strict pin, an absent
# env token, an identical on-disk token, a retry that is ALSO refused, a
# retry whose connection failed, or a retry whose answer does not PROVE
# the fallback credential was accepted all fall through with the ORIGINAL
# status — so every fail-open reason string, warning and metric row below
# is byte-identical to pre-v0.2.91, and identical to the bash sibling's.
if ($statusCode -eq 401 -or $statusCode -eq 403) {
    $fallbackToken = Get-AccessStaleEnvFallbackToken
    if ($null -ne $fallbackToken) {
        # -NoFailOpen: a transport failure on the RETRY must not exit with
        # `url_error_*` (MINOR-2) — it returns Status 0, which the gate
        # below rejects, leaving the original `hub_auth_401`.
        $retry = Invoke-AccessRequest -Token $fallbackToken -Url $url -NoFailOpen
        if (Test-AccessStaleEnvRetryIsDefinitive -Status $retry.Status) {
            Write-AccessStaleEnvTokenWarning
            $statusCode = $retry.Status
            $responseBody = $retry.Body
        }
    }
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
