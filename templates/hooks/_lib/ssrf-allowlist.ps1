# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# _lib/ssrf-allowlist.ps1 - the pre-tool-use SSRF guard's ONE URL decision:
# which WebFetch URLs are this machine's own VCO services (allowed), which
# target a private / internal address (blocked), and which are public (pass).
# POSIX mirror: _lib/ssrf-allowlist.sh (MUST stay logically identical; see its
# header for the full contract). The shared case table
# tests/fixtures/ssrf_cases.json runs against both.
#
# v0.2.100 WP-18B: the allowed host:port pairs are DERIVED at run time from
# the projected env (config_projection writes these from the service_endpoints
# rows):
#
#   Weaviate   WEAVIATE_URL, else http://localhost:$WEAVIATE_PORT, else :8081
#   Ollama     OLLAMA_URL, else :11435
#   code-embed CODE_EMBED_SERVICE_URL, else :11440
#   vct-hub    vct_project_config.ps1 -HubPort (VCT_HUB_PORT -> hub.port -> 7700)
#   always     :8082 and Gradio :7860
#
# Literal ports are the per-service fallback only. A loopback pair is allowed
# as localhost, 127.0.0.1 and [::1].
#
# Reviews R18-04 / R18-05: allow and block both read the URL through ONE
# parser (ConvertFrom-VcoSsrfUrl) that follows WHATWG for http(s): tab/LF/CR
# removed, scheme case-insensitive, `\` is `/`, userinfo ends at the LAST `@`,
# the host is percent-decoded, lower-cased, trailing dots dropped, numeric
# IPv4 in every WHATWG spelling, bracketed IPv6 expanded to eight hextets.
# ALLOW also needs a CLEAN authority (no `@`, `\`, `%`, whitespace, control).
# A non-ASCII host is NFKC-normalised and IDNA-encoded by the Python standard
# library (Convert-VcoSsrfIdna, via the hooks' find-python locator), so
# fullwidth localhost is judged as `localhost`; it is never CLEAN.
# BLOCK: an internal parsed host, an unreadable URL, a non-ASCII host that
# cannot be converted (no Python, or the conversion fails), or the legacy
# substring pattern (case-insensitive, as it always was here).
#
# Usage:
#     . (Join-Path $LibDir "ssrf-allowlist.ps1")
#     Get-VcoSsrfVerdict -Url $url -ProjectRoot $ProjectRoot     # allow|block|pass
#     Test-VcoSsrfUrlAllowed -Url $url -ProjectRoot $ProjectRoot   # $true = allowed
#     Test-VcoSsrfUrlBlocked -Url $url                             # $true = blocked
#     Get-VcoSsrfAllowedPairs -ProjectRoot $ProjectRoot            # host:port strings

# The ONE PowerShell-executable pick (pwsh, else powershell 5.1) for the
# hub-port child spawn below.
if (-not (Get-Variable -Name PsExe -ErrorAction SilentlyContinue)) {
    . (Join-Path $PSScriptRoot "resolve-powershell.ps1")
}

# The hooks' ONE Python locator (sets $PY); the hook normally sourced it already.
if (-not (Get-Variable -Name PY -ErrorAction SilentlyContinue) -or -not $PY) {
    $vcoSsrfFindPy = Join-Path $PSScriptRoot "find-python.ps1"
    if (Test-Path -LiteralPath $vcoSsrfFindPy) { . $vcoSsrfFindPy }
}

# Stdlib-only IDNA conversion: argv[1] is the host's UTF-8 bytes in hex (so
# no console encoding can alter them); prints the ASCII form.
# MUST MATCH VCO_SSRF_IDNA_PY in ssrf-allowlist.sh.
$script:VcoSsrfIdnaPy = 'import sys, unicodedata
h = unicodedata.normalize("NFKC", bytes.fromhex(sys.argv[1]).decode("utf-8"))
sys.stdout.write(h.encode("idna").decode("ascii"))'

# MUST MATCH VCO_SSRF_LEGACY_PATTERN in ssrf-allowlist.sh.
$script:VcoSsrfLegacyPattern = '(localhost|127\.|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.|192\.168\.[0-9]+\.|169\.254\.[0-9]+\.|0\.0\.0\.0|::1)'

# WHATWG IPv4-number parse (0x.. hex, 0.. octal, else decimal); $null on failure.
function ConvertFrom-VcoSsrfNumber {
    param([string]$Part)
    if (-not $Part) { return $null }
    $s = $Part
    $radix = 10
    if ($s -cmatch '^0[xX]') {
        $radix = 16
        $s = $s.Substring(2)
    } elseif ($s.Length -ge 2 -and $s.StartsWith('0')) {
        $radix = 8
        $s = $s.Substring(1)
    }
    if ($s.Length -eq 0) { return [long]0 }
    if ($radix -eq 16) { if ($s -cnotmatch '^[0-9a-fA-F]+$') { return $null } }
    elseif ($radix -eq 8) { if ($s -cnotmatch '^[0-7]+$') { return $null } }
    else { if ($s -cnotmatch '^[0-9]+$') { return $null } }
    $s = $s.TrimStart('0')
    if ($s.Length -eq 0) { return [long]0 }
    if ($s.Length -gt 11) { return $null }
    $v = [Convert]::ToInt64($s, $radix)
    if ($v -gt 4294967295) { return $null }
    return $v
}

function Test-VcoSsrfEndsInNumber {
    param([string]$HostName)
    $last = $HostName.Substring($HostName.LastIndexOf('.') + 1)
    return (($last -cmatch '^[0-9]+$') -or ($last -cmatch '^0[xX][0-9a-fA-F]*$'))
}

# WHATWG IPv4 parse of a host whose trailing dots are already dropped; $null on failure.
function ConvertFrom-VcoSsrfIPv4 {
    param([string]$HostName)
    if (-not $HostName -or $HostName.StartsWith('.') -or $HostName.EndsWith('.') -or $HostName.Contains('..')) { return $null }
    $parts = $HostName.Split('.')
    $n = $parts.Length
    if ($n -lt 1 -or $n -gt 4) { return $null }
    [long]$v = 0
    for ($i = 0; $i -lt $n - 1; $i++) {
        $x = ConvertFrom-VcoSsrfNumber -Part $parts[$i]
        if ($null -eq $x -or $x -gt 255) { return $null }
        $v += ([long]$x -shl (8 * (3 - $i)))
    }
    $x = ConvertFrom-VcoSsrfNumber -Part $parts[$n - 1]
    if ($null -eq $x) { return $null }
    if ($x -ge ([long]1 -shl (8 * (5 - $n)))) { return $null }
    return ($v + $x)
}

function Format-VcoSsrfIPv4 {
    param([long]$Value)
    return ('{0}.{1}.{2}.{3}' -f (($Value -shr 24) -band 255), (($Value -shr 16) -band 255),
        (($Value -shr 8) -band 255), ($Value -band 255))
}

# Bracket-less, lower-case IPv6 literal -> eight hextet values; $null on failure.
function ConvertFrom-VcoSsrfIPv6 {
    param([string]$Inner)
    $s = $Inner
    if (-not $s -or $s -cnotmatch '^[0-9a-f:.]+$') { return $null }
    if ($s.Contains('.')) {
        $dq = $s.Substring($s.LastIndexOf(':') + 1)
        $m = [regex]::Match($dq, '^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$')
        if (-not $m.Success) { return $null }
        [long]$v4 = 0
        for ($g = 1; $g -le 4; $g++) {
            $o = [long]$m.Groups[$g].Value
            if ($o -gt 255) { return $null }
            $v4 = $v4 * 256 + $o
        }
        $s = $s.Substring(0, $s.Length - $dq.Length) + ('{0:x}:{1:x}' -f ($v4 -shr 16), ($v4 -band 65535))
    }
    $compressed = $s.Contains('::')
    $head = $s
    $tail = ''
    if ($compressed) {
        $k = $s.IndexOf('::')
        $head = $s.Substring(0, $k)
        $tail = $s.Substring($k + 2)
        if ($tail.Contains('::')) { return $null }
    }
    foreach ($g in @($head, $tail)) {
        if ($g.StartsWith(':') -or $g.EndsWith(':')) { return $null }
    }
    $hg = @()
    $tg = @()
    if ($head) { $hg = $head.Split(':') }
    if ($tail) { $tg = $tail.Split(':') }
    if ($compressed) {
        if (($hg.Count + $tg.Count) -gt 7) { return $null }
    } elseif ($hg.Count -ne 8) { return $null }
    $all = New-Object System.Collections.Generic.List[string]
    foreach ($g in $hg) { $all.Add($g) }
    if ($compressed) { for ($z = $hg.Count + $tg.Count; $z -lt 8; $z++) { $all.Add('0') } }
    foreach ($g in $tg) { $all.Add($g) }
    $out = New-Object System.Collections.Generic.List[long]
    foreach ($g in $all) {
        if ($g -cnotmatch '^[0-9a-f]{1,4}$') { return $null }
        $out.Add([Convert]::ToInt64($g, 16))
    }
    if ($out.Count -ne 8) { return $null }
    return ,($out.ToArray())
}

# Percent-decode a host to its raw bytes (literal text as UTF-8). Returns a
# byte[] or $null on a decoded control / DEL byte; a malformed `%` stays literal.
function ConvertFrom-VcoSsrfPercent {
    param([string]$Text)
    $bytes = New-Object System.Collections.Generic.List[byte]
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $i = 0
    $run = 0
    while ($i -lt $Text.Length) {
        if ($Text[$i] -eq '%' -and ($i + 2) -lt $Text.Length -and ($Text.Substring($i + 1, 2) -cmatch '^[0-9A-Fa-f]{2}$')) {
            if ($i -gt $run) { $bytes.AddRange($utf8.GetBytes($Text.Substring($run, $i - $run))) }
            $val = [Convert]::ToInt32($Text.Substring($i + 1, 2), 16)
            if ($val -lt 32 -or $val -eq 127) { return $null }
            $bytes.Add([byte]$val)
            $i += 3
            $run = $i
            continue
        }
        $i++
    }
    if ($Text.Length -gt $run) { $bytes.AddRange($utf8.GetBytes($Text.Substring($run))) }
    return ,($bytes.ToArray())
}

# Raw host bytes -> NFKC-normalised IDNA (ASCII) form, or $null when no Python
# is found or the conversion fails.
function Convert-VcoSsrfIdna {
    param([byte[]]$Bytes)
    if (-not (Get-Variable -Name PY -ErrorAction SilentlyContinue) -or -not $PY) { return $null }
    $hex = ([BitConverter]::ToString($Bytes)).Replace('-', '').ToLowerInvariant()
    if (-not $hex) { return $null }
    try {
        $out = & $PY -I -c $script:VcoSsrfIdnaPy $hex 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
    } catch { return $null }
    $out = ([string]($out -join '')).Trim()
    if (-not $out -or $out -cmatch '[^\x20-\x7e]') { return $null }
    return $out
}

function Get-VcoSsrfDefaultPort {
    param([string]$Scheme)
    switch ($Scheme) {
        'https' { return '443' }
        'wss' { return '443' }
        'ftp' { return '21' }
        default { return '80' }
    }
}

# Parse a URL as described in ssrf-allowlist.sh's header. Returns $null when
# the URL cannot be read, else @{ Host; Kind (name|ipv4|ipv6|nonascii); Port;
# Clean; V4 (ipv4 value); H (ipv6 hextets) }.
function ConvertFrom-VcoSsrfUrl {
    param([string]$Url)
    if ($null -eq $Url) { return $null }
    $u = $Url.Replace("`t", '').Replace("`n", '').Replace("`r", '')
    $u = $u.Trim([char[]](@(0..32 | ForEach-Object { [char]$_ })))
    $clean = $true
    $m = [regex]::Match($u, '^([A-Za-z][A-Za-z0-9+.-]*):')
    if ($m.Success) {
        $scheme = $m.Groups[1].Value.ToLowerInvariant()
        $rest = $u.Substring($m.Length)
        if (@('http', 'https', 'ws', 'wss', 'ftp') -notcontains $scheme) {
            if ($rest.StartsWith('//')) {
                $rest = $rest.Substring(2)
            } else {
                $scheme = 'http'
                $rest = $u
            }
        }
    } else {
        $scheme = 'http'
        $rest = $u
    }
    $rest = $rest.TrimStart([char[]]@('/', '\'))
    $cut = $rest.IndexOfAny([char[]]@('/', '?', '#'))
    $raw = if ($cut -ge 0) { $rest.Substring(0, $cut) } else { $rest }
    if ($raw.Contains('\')) { $clean = $false }
    $rest = $rest.Replace('\', '/')
    $cut = $rest.IndexOfAny([char[]]@('/', '?', '#'))
    $auth = if ($cut -ge 0) { $rest.Substring(0, $cut) } else { $rest }
    if ($auth.Contains('@')) {
        $clean = $false
        $auth = $auth.Substring($auth.LastIndexOf('@') + 1)
    }
    if ($auth.Contains('%')) { $clean = $false }
    if ($auth -match '[\x00-\x20\x7f]') { $clean = $false }
    $port = ''
    if ($auth.StartsWith('[')) {
        $close = $auth.IndexOf(']')
        if ($close -lt 0) { return $null }
        $hostPart = $auth.Substring(0, $close + 1)
        $port = $auth.Substring($close + 1)
        if ($port) {
            if (-not $port.StartsWith(':')) { return $null }
            $port = $port.Substring(1)
        }
    } else {
        $colon = $auth.IndexOf(':')
        if ($colon -ge 0) {
            $hostPart = $auth.Substring(0, $colon)
            $port = $auth.Substring($colon + 1)
        } else {
            $hostPart = $auth
        }
    }
    if (-not $port) { $port = Get-VcoSsrfDefaultPort -Scheme $scheme }
    if ($port -cnotmatch '^[0-9]+$') { return $null }
    $port = $port.TrimStart('0')
    if (-not $port) { $port = '0' }
    if ($port.Length -gt 5 -or [int]$port -gt 65535) { return $null }
    if (-not $hostPart) { return $null }
    $r = @{ Host = ''; Kind = ''; Port = $port; Clean = $clean; V4 = $null; H = $null }
    if ($hostPart.StartsWith('[')) {
        $inner = $hostPart.Substring(1, $hostPart.Length - 2).ToLowerInvariant()
        $h6 = ConvertFrom-VcoSsrfIPv6 -Inner $inner
        if ($null -eq $h6) { return $null }
        $r.Kind = 'ipv6'
        $r.H = $h6
        $r.Host = '[' + ((@($h6) | ForEach-Object { '{0:x}' -f $_ }) -join ':') + ']'
        return $r
    }
    $raw = ConvertFrom-VcoSsrfPercent -Text $hostPart
    if ($null -eq $raw) { return $null }
    $nonAscii = $false
    # Same test as the .sh `^[ -~]*$`: any byte outside printable ASCII.
    foreach ($x in $raw) { if ($x -ge 0x7f -or $x -lt 0x20) { $nonAscii = $true; break } }
    if ($nonAscii) {
        $r.Clean = $false
        $h = Convert-VcoSsrfIdna -Bytes $raw
        if ($null -eq $h) {
            $r.Kind = 'nonascii'
            $r.Host = (New-Object System.Text.UTF8Encoding($false)).GetString($raw)
            return $r
        }
    } else {
        $h = [System.Text.Encoding]::ASCII.GetString($raw)
    }
    if ($h -cmatch '[\]\[ #/:<>?@\\^|%]') { return $null }
    $h = $h.ToLowerInvariant().TrimEnd('.')
    if (-not $h) { return $null }
    if (Test-VcoSsrfEndsInNumber -HostName $h) {
        $v4 = ConvertFrom-VcoSsrfIPv4 -HostName $h
        if ($null -eq $v4) { return $null }
        $r.Kind = 'ipv4'
        $r.V4 = [long]$v4
        $r.Host = Format-VcoSsrfIPv4 -Value $v4
        return $r
    }
    $r.Kind = 'name'
    $r.Host = $h
    return $r
}

# $true when the 32-bit IPv4 value is not a public unicast address.
function Test-VcoSsrfV4Internal {
    param([long]$Value)
    $a = $Value -shr 24
    if (@(0, 10, 127) -contains $a) { return $true }
    if ($a -ge 224) { return $true }                                         # multicast + 240/4 + broadcast
    if (($Value -band 0xffc00000L) -eq 0x64400000L) { return $true }         # 100.64/10 CGNAT
    if (($Value -shr 16) -eq 0xa9fe) { return $true }                        # 169.254/16
    if (($Value -band 0xfff00000L) -eq 0xac100000L) { return $true }         # 172.16/12
    if (($Value -shr 8) -eq 0xc00000) { return $true }                       # 192.0.0/24
    if (($Value -shr 16) -eq 0xc0a8) { return $true }                        # 192.168/16
    if (($Value -band 0xfffe0000L) -eq 0xc6120000L) { return $true }         # 198.18/15
    return $false
}

# $true when eight IPv6 hextets are not a public unicast address (an embedded
# IPv4 is judged as IPv4).
function Test-VcoSsrfV6Internal {
    param([long[]]$H)
    $h0 = $H[0]
    if (($h0 -band 0xfe00) -eq 0xfc00) { return $true }   # fc00::/7 ULA
    if (($h0 -band 0xffc0) -eq 0xfe80) { return $true }   # fe80::/10 link-local
    if (($h0 -band 0xffc0) -eq 0xfec0) { return $true }   # fec0::/10 site-local
    if (($h0 -band 0xff00) -eq 0xff00) { return $true }   # ff00::/8 multicast
    if ($h0 -eq 0 -and $H[1] -eq 0 -and $H[2] -eq 0 -and $H[3] -eq 0 -and $H[4] -eq 0 -and
        ($H[5] -eq 0 -or $H[5] -eq 65535)) {
        # ::/96 (::, ::1, IPv4-compatible) and ::ffff:0:0/96 (IPv4-mapped).
        return (Test-VcoSsrfV4Internal -Value (($H[6] -shl 16) + $H[7]))
    }
    if ($h0 -eq 0x64 -and $H[1] -eq 0xff9b -and $H[2] -eq 0 -and $H[3] -eq 0 -and $H[4] -eq 0 -and $H[5] -eq 0) {
        return (Test-VcoSsrfV4Internal -Value (($H[6] -shl 16) + $H[7]))  # 64:ff9b::/96 NAT64
    }
    if ($h0 -eq 0x2002) {
        return (Test-VcoSsrfV4Internal -Value (($H[1] -shl 16) + $H[2]))  # 2002::/16 6to4
    }
    return $false
}

# $true when the URL targets a private / internal address, cannot be read,
# has a non-ASCII host, or matches the legacy pattern.
function Test-VcoSsrfUrlBlocked {
    param([string]$Url)
    if ($null -eq $Url) { return $true }
    if ($Url -match $script:VcoSsrfLegacyPattern) { return $true }
    $p = ConvertFrom-VcoSsrfUrl -Url $Url
    if ($null -eq $p) { return $true }
    switch ($p.Kind) {
        'nonascii' { return $true }
        'ipv4' { return (Test-VcoSsrfV4Internal -Value $p.V4) }
        'ipv6' { return (Test-VcoSsrfV6Internal -H $p.H) }
        'name' { return (($p.Host -eq 'localhost') -or $p.Host.EndsWith('.localhost')) }
    }
    return $true
}

# The canonical `host:port` of a URL (the pair the allowlist compares), or $null.
function Get-VcoSsrfAuthority {
    param([string]$Url)
    $p = ConvertFrom-VcoSsrfUrl -Url $Url
    if ($null -eq $p) { return $null }
    return ('{0}:{1}' -f $p.Host, $p.Port)
}

function Expand-VcoSsrfPair {
    param([string]$Pair)
    $colon = $Pair.LastIndexOf(':')
    $hostPart = $Pair.Substring(0, $colon)
    $port = $Pair.Substring($colon + 1)
    if ($hostPart -in @('localhost', '127.0.0.1', '[0:0:0:0:0:0:0:1]')) {
        return @("localhost:$port", "127.0.0.1:$port", "[0:0:0:0:0:0:0:1]:$port")
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
    $p = ConvertFrom-VcoSsrfUrl -Url $Url
    if ($null -eq $p -or -not $p.Clean) { return $false }
    $target = '{0}:{1}' -f $p.Host, $p.Port
    return ((Get-VcoSsrfAllowedPairs -ProjectRoot $ProjectRoot) -ccontains $target)
}

function Get-VcoSsrfVerdict {
    param([string]$Url, [string]$ProjectRoot = ".")
    if (Test-VcoSsrfUrlAllowed -Url $Url -ProjectRoot $ProjectRoot) { return 'allow' }
    if (Test-VcoSsrfUrlBlocked -Url $Url) { return 'block' }
    return 'pass'
}
