# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/credscan.ps1 - Windows sibling of _lib/credscan.sh. Shared
# credential-pattern scanner used by post-tool-security.ps1 and the
# V52-L.1 SubagentStop reconciler.
#
# Function:
#   Scan-FileForCredentials <FilePath>
#     Echoes each matched label (one per line). Empty output = clean.
#     Returns nothing meaningful - caller checks the captured output.
#
# PATTERN SOURCE (changed - read this before adding a regex):
#   The patterns are NOT defined here. Both this file and post-tool-security.ps1
#   read the SAME vocabulary from _lib/credshapes.ps1 ('content_scan' context),
#   whose SSOT is vco_lib/credential_shapes.py. The previous arrangement kept
#   this list in hand-maintained lockstep with three other copies, which is how
#   it fell behind: it was missing the GitHub fine-grained PAT shape and the
#   unquoted dotenv-style generic-secret shape, and its 'Anthropic/OpenAI API
#   key' label sat over a pattern that matched neither modern OpenAI project
#   keys nor OpenRouter. Add shapes to the SSOT, never here.
#
# Never echoes any part of the file's contents - labels only.

# Locate the vocabulary next to this helper. $PSScriptRoot is populated when a
# file is dot-sourced, with MyInvocation as the fallback for older hosts.
$CredScanLibDir = $PSScriptRoot
if (-not $CredScanLibDir) {
    $CredScanLibDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$CredShapesLib = Join-Path $CredScanLibDir "credshapes.ps1"
if (Test-Path -LiteralPath $CredShapesLib -PathType Leaf) {
    . $CredShapesLib
}

function Scan-FileForCredentials {
    param([string]$FilePath)

    if (-not $FilePath) { return }
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) { return }

    # A MISSING vocabulary must not look like a clean file. Callers treat empty
    # output as "no credentials found", so degrading silently here would turn a
    # broken install into a permanent all-clear. Emit a real alert label
    # instead, so the miss surfaces through the same path as any finding.
    if (-not (Get-Command Get-CredShapes -ErrorAction SilentlyContinue)) {
        Write-Output "credential scanner UNAVAILABLE (_lib/credshapes.ps1 missing)"
        return
    }

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

    # Quick binary sniff: NUL byte in first 8 KB -> skip. Mirrors the
    # `file -b --mime` heuristic in the .sh sibling.
    $sniff = if ($content.Length -gt 8192) {
        $content.Substring(0, 8192)
    } else {
        $content
    }
    if ($sniff -match "`0") { return }

    $alerts = New-Object System.Collections.Generic.List[string]

    # One pass per shape, in SSOT declaration order so the reported label
    # sequence matches the .sh sibling exactly.
    foreach ($shape in (Get-CredShapes -Context 'content_scan')) {
        if ($content -match $shape.Re) {
            $alerts.Add($shape.Label) | Out-Null
        }
    }

    foreach ($a in $alerts) {
        Write-Output $a
    }
}
