# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/credscan.ps1 — Windows sibling of _lib/credscan.sh. Shared
# credential-pattern scanner used by post-tool-security.ps1 and the
# V52-L.1 SubagentStop reconciler.
#
# Function:
#   Scan-FileForCredentials <FilePath>
#     Echoes each matched label (one per line). Empty output = clean.
#     Returns nothing meaningful — caller checks the captured output.
#
# Patterns are kept in lockstep with credscan.sh / post-tool-security.ps1.

function Scan-FileForCredentials {
    param([string]$FilePath)

    if (-not $FilePath) { return }
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) { return }

    try {
        $info = Get-Item -LiteralPath $FilePath -ErrorAction Stop
        # Skip >5 MB files (mirrors .sh sibling).
        if ($info.Length -gt 5MB) { return }
    } catch {
        return
    }

    # Read file content as text. -Raw avoids line-by-line array overhead.
    # Skip files that fail to read as text (likely binaries).
    $content = $null
    try {
        $content = Get-Content -LiteralPath $FilePath -Raw -ErrorAction Stop
    } catch {
        return
    }
    if (-not $content) { return }

    # Quick binary sniff: NUL byte in first 8 KB → skip. Mirrors the
    # `file -b --mime` heuristic in the .sh sibling.
    $sniff = if ($content.Length -gt 8192) {
        $content.Substring(0, 8192)
    } else {
        $content
    }
    if ($sniff -match "`0") { return }

    $alerts = New-Object System.Collections.Generic.List[string]

    if ($content -match 'sk-(ant-api03|[a-zA-Z0-9]{30,})-[a-zA-Z0-9]') {
        $alerts.Add("Anthropic/OpenAI API key") | Out-Null
    }
    if ($content -match 'AKIA[A-Z0-9]{16}') {
        $alerts.Add("AWS access key") | Out-Null
    }
    if ($content -match 'gh[pousr]_[a-zA-Z0-9]{36}') {
        $alerts.Add("GitHub token") | Out-Null
    }
    if ($content -match 'BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY') {
        $alerts.Add("PEM private key") | Out-Null
    }
    if ($content -match '(SECRET|API_KEY|ACCESS_TOKEN|PRIVATE_KEY)\s*[:=]\s*["''][a-zA-Z0-9+/=_\-]{32,}') {
        $alerts.Add("Generic secret") | Out-Null
    }
    if ($content -match 'VCT_HOOK_LEAK_PROBE_a3f7c2') {
        $alerts.Add("Hook leak-test marker") | Out-Null
    }

    foreach ($a in $alerts) {
        Write-Output $a
    }
}
