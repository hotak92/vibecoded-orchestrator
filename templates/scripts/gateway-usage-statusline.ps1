# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# gateway-usage-statusline.ps1 — PowerShell sibling of
# gateway-usage-statusline.sh: one compact line of subscription usage for
# Claude Code's `statusLine`, e.g.
#
#   Claude 5h 31% · wk 27% · Fable 12% │ GLM 5h 10% · wk 72% │ Qwen 1.2M tok/mo
#
# It prints what the local model gateway answers on
# `GET /usage/windows?format=line` — the line is RENDERED by the gateway
# (model_router.usage_windows.render_line), so the two scripts cannot
# disagree about the format. The gateway answers from its own cache; this
# script never calls a vendor.
#
# Enable it yourself (VCO does not write `statusLine` into your settings: a
# project-level value would override one you set in ~/.claude/settings.json):
#
#   "statusLine": {"type": "command",
#                  "command": "pwsh -NoProfile -File .claude/scripts/gateway-usage-statusline.ps1"}
#
# Contract: exit 0, always; print the line or nothing. Bounded (a 600 ms
# HttpClient timeout) and SILENT on every failure.
#
# Secrets: the host token goes into one request header in-process; it never
# reaches argv or the output.
#
# Port resolution MUST MATCH vco_lib.vscode_settings.resolve_gateway_ports
# and the .sh sibling (env pin, live port file, last-port record, default).

$ErrorActionPreference = 'Stop'

function Get-ValidPort([string]$Value) {
    if ($null -eq $Value) { return $null }
    $v = ($Value -replace '\s', '')
    if ($v -notmatch '^\d{1,5}$') { return $null }
    $n = [int]$v
    if ($n -gt 0 -and $n -lt 65536) { return $n }
    return $null
}

function Get-PortFromFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $first = Get-Content -LiteralPath $Path -TotalCount 1 -ErrorAction Stop
    return Get-ValidPort ([string]$first)
}

try {
    $stateDir = if ($Env:VCT_STATE_DIR) { $Env:VCT_STATE_DIR } else { Join-Path $HOME '.vct' }

    $port = Get-ValidPort $Env:VCT_MODEL_GATEWAY_PORT
    if (-not $port) { $port = Get-PortFromFile (Join-Path $stateDir 'model-gateway.port') }
    if (-not $port) { $port = Get-PortFromFile (Join-Path $stateDir 'model-gateway.last-port') }
    # MUST MATCH model_router.config.DEFAULT_PORT.
    if (-not $port) { $port = 11436 }

    $tokenFile = Join-Path $stateDir 'model-gateway.token'
    if (-not (Test-Path -LiteralPath $tokenFile -PathType Leaf)) { exit 0 }
    $token = ([string](Get-Content -LiteralPath $tokenFile -TotalCount 1 -ErrorAction Stop)) -replace '\s', ''
    if (-not $token) { exit 0 }

    Add-Type -AssemblyName System.Net.Http
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromMilliseconds(600)
    try {
        $request = [System.Net.Http.HttpRequestMessage]::new(
            [System.Net.Http.HttpMethod]::Get,
            "http://127.0.0.1:$port/usage/windows?format=line")
        [void]$request.Headers.TryAddWithoutValidation('Authorization', "Bearer $token")
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) { exit 0 }
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    } finally {
        $client.Dispose()
    }
    $line = (($body -split "`n")[0]).TrimEnd("`r")
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    [Console]::Out.Write($line)
} catch {
    # Silent by contract: a status line never shows an error.
}
exit 0
