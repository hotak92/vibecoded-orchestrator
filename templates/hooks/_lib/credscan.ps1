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
        # -Force: without it Get-Item throws on a hidden item (every dotfile
        # on Linux/macOS, e.g. .env), and the catch below would skip the
        # scan silently.
        $info = Get-Item -LiteralPath $FilePath -Force -ErrorAction Stop
        # Skip >5 MB files (mirrors .sh sibling).
        if ($info.Length -gt 5MB) { return }
    } catch {
        return
    }

    # Read file content as text. -Raw avoids line-by-line array overhead.
    # Skip files that fail to read as text (likely binaries).
    $content = $null
    try {
        $content = Get-Content -LiteralPath $FilePath -Raw -Force -ErrorAction Stop
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
    # The ONE explicit opt-out (owner ruling 2026-10-12): a line carrying
    # the test-placeholder marker is a declared fixture and is not reported;
    # the marker covers its own line only, and nothing is inferred from how
    # a value looks. Same rule, same marker (credshapes.ps1 mirror of
    # vco_lib/credential_shapes.TEST_PLACEHOLDER_MARKER) as post-tool-security.
    if (-not (Get-Command Get-CredShapesTestPlaceholderMarker -ErrorAction SilentlyContinue)) {
        Write-Output "credential scanner UNAVAILABLE (test-placeholder marker missing from _lib/credshapes.ps1)"
        return
    }
    $marker = Get-CredShapesTestPlaceholderMarker
    $lines = $content -split "`r?`n"
    # -Path: shapes restricted to another file kind (the unquoted generic
    # secret serves env/config files only) are dropped, as in the .sh sibling.
    foreach ($shape in (Get-CredShapes -Context 'content_scan' -Path $FilePath)) {
        foreach ($ln in $lines) {
            # -cmatch, not -match: .NET -match is case-INSENSITIVE, while the
            # .sh sibling (grep -E) and post-tool-security (Python re) match
            # case-sensitively. The vocabulary spells out case where it wants
            # it ([Pp][Aa]... in the generic-secret keys); -match would make
            # e.g. a lowercased AKIA run alert on Windows only.
            if (($ln -cmatch $shape.Re) -and (-not $ln.Contains($marker))) {
                $alerts.Add($shape.Label) | Out-Null
                break
            }
        }
    }

    foreach ($a in $alerts) {
        Write-Output $a
    }
}
