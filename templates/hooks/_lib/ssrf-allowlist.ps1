# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# _lib/ssrf-allowlist.ps1 - WHICH private-network URLs the pre-tool-use SSRF
# guard lets a WebFetch reach: this machine's own VCO services.
# POSIX mirror: _lib/ssrf-allowlist.sh (MUST stay logically identical; see its
# header for the full contract).
#
# v0.2.100 WP-18B: the allowlist used to be the literal port set
# 8081|8082|11435|11440|7860. It is now DERIVED at run time from the
# projected env (config_projection writes these from the service_endpoints
# rows):
#
#   Weaviate   WEAVIATE_URL, else http://localhost:$WEAVIATE_PORT, else :8081
#   Ollama     OLLAMA_URL, else :11435
#   code-embed CODE_EMBED_SERVICE_URL, else :11440
#   vct-hub    vct_project_config.ps1 -HubPort (VCT_HUB_PORT -> hub.port -> 7700)
#   always     :8082 and Gradio :7860
#
# Literal ports are the per-service fallback only. A loopback pair is allowed
# as localhost, 127.0.0.1 and [::1]; matching is on the URL's authority.
#
# Usage:
#     . (Join-Path $LibDir "ssrf-allowlist.ps1")
#     Test-VcoSsrfUrlAllowed -Url $url -ProjectRoot $ProjectRoot   # $true = allowed
#     Get-VcoSsrfAllowedPairs -ProjectRoot $ProjectRoot            # host:port strings

# The ONE PowerShell-executable pick (pwsh, else powershell 5.1) for the
# hub-port child spawn below.
if (-not (Get-Variable -Name PsExe -ErrorAction SilentlyContinue)) {
    . (Join-Path $PSScriptRoot "resolve-powershell.ps1")
}

function Get-VcoSsrfAuthority {
    param([string]$Url)
    if ($null -eq $Url) { return $null }
    $u = $Url
    $scheme = "http"
    $idx = $u.IndexOf("://")
    if ($idx -ge 0) {
        $scheme = $u.Substring(0, $idx)
        $u = $u.Substring($idx + 3)
    }
    $scheme = $scheme.ToLowerInvariant()
    $cut = $u.IndexOfAny([char[]]@('/', '?', '#'))
    $auth = if ($cut -ge 0) { $u.Substring(0, $cut) } else { $u }
    $at = $auth.LastIndexOf('@')
    if ($at -ge 0) { $auth = $auth.Substring($at + 1) }
    $auth = $auth.ToLowerInvariant()
    if ($auth.StartsWith('[')) {
        $close = $auth.IndexOf(']')
        if ($close -lt 0) { $close = $auth.Length - 1 }
        $hostPart = $auth.Substring(0, $close + 1)
        $port = $auth.Substring($close + 1)
        if ($port.StartsWith(':')) { $port = $port.Substring(1) }
    } elseif ($auth.Contains(':')) {
        $colon = $auth.LastIndexOf(':')
        $hostPart = $auth.Substring(0, $colon)
        $port = $auth.Substring($colon + 1)
    } else {
        $hostPart = $auth
        $port = ""
    }
    if (-not $port) { $port = if ($scheme -eq 'https') { '443' } else { '80' } }
    if (-not $hostPart -or $port -notmatch '^[0123456789]{1,5}$') { return $null }
    return ('{0}:{1}' -f $hostPart, [int]$port)
}

function Expand-VcoSsrfPair {
    param([string]$Pair)
    $colon = $Pair.LastIndexOf(':')
    $hostPart = $Pair.Substring(0, $colon)
    $port = $Pair.Substring($colon + 1)
    if ($hostPart -in @('localhost', '127.0.0.1', '[::1]')) {
        return @("localhost:$port", "127.0.0.1:$port", "[::1]:$port")
    }
    return @($Pair)
}

function Get-VcoSsrfEnv {
    param([string]$Name)
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ($null -eq $v) { return "" }
    return $v.Trim()
}

function Get-VcoSsrfHubPort {
    param([string]$ProjectRoot)
    $resolver = Join-Path $ProjectRoot ".claude/scripts/vct_project_config.ps1"
    $p = ""
    if (Test-Path -LiteralPath $resolver) {
        try {
            $p = (& $PsExe -NoProfile -File $resolver -HubPort 2>$null | Select-Object -First 1)
            if ($null -eq $p) { $p = "" }
            $p = ([string]$p).Trim()
        } catch { $p = "" }
    }
    if ($p -notmatch '^[0123456789]{1,5}$') { $p = "7700" }
    return $p
}

function Get-VcoSsrfAllowedPairs {
    param([string]$ProjectRoot = ".")
    $urls = New-Object System.Collections.Generic.List[string]
    $v = Get-VcoSsrfEnv "WEAVIATE_URL"
    if (-not $v) {
        $port = Get-VcoSsrfEnv "WEAVIATE_PORT"
        if (-not $port) { $port = "8081" }
        $v = "http://localhost:$port"
    }
    $urls.Add($v)
    $v = Get-VcoSsrfEnv "OLLAMA_URL"
    if (-not $v) { $v = "http://localhost:11435" }
    $urls.Add($v)
    $v = Get-VcoSsrfEnv "CODE_EMBED_SERVICE_URL"
    if (-not $v) { $v = "http://localhost:11440" }
    $urls.Add($v)
    $urls.Add("http://localhost:" + (Get-VcoSsrfHubPort -ProjectRoot $ProjectRoot))
    $urls.Add("http://localhost:8082")
    $urls.Add("http://localhost:7860")
    $pairs = New-Object System.Collections.Generic.List[string]
    foreach ($u in $urls) {
        $pair = Get-VcoSsrfAuthority -Url $u
        if (-not $pair) { continue }
        foreach ($p in (Expand-VcoSsrfPair -Pair $pair)) { $pairs.Add($p) }
    }
    return $pairs.ToArray()
}

function Test-VcoSsrfUrlAllowed {
    param([string]$Url, [string]$ProjectRoot = ".")
    $target = Get-VcoSsrfAuthority -Url $Url
    if (-not $target) { return $false }
    return ((Get-VcoSsrfAllowedPairs -ProjectRoot $ProjectRoot) -contains $target)
}
