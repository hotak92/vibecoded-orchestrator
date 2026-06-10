# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# scripts/lib/asset-ref-count.ps1 — PowerShell sibling of asset-ref-count.sh.
#
# Counts SvelteKit asset references inside a launcher binary to detect
# "no embedded frontend" builds. See asset-ref-count.sh for the full
# rationale + drift history.
#
# Usage (dot-source):
#   . "$PSScriptRoot\..\lib\asset-ref-count.ps1"
#   $count = Get-AssetRefCount -Path "C:\path\to\binary.exe"
#   if (Test-AssetRefCount -Path "C:\path\to\binary.exe") { "embedded" }
#
# Marker substring is the SAME broad form `_app/immutable/` used by
# the bash sibling — drift between the two would defeat the entire
# purpose of the dedup.

$script:AssetRefMarker = "_app/immutable/"
if (-not $env:VCT_ASSET_REF_MIN) {
    $script:AssetRefMin = 5
} else {
    $script:AssetRefMin = [int]$env:VCT_ASSET_REF_MIN
}

function Get-AssetRefCount {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return 0
    }
    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
    } catch {
        return 0
    }
    # ASCII-decode then split-count. This matches the embedded
    # one-liner in start-launcher.bat / first-install.bat (see
    # shell-scripts-dedup audit Finding 4) and produces the same
    # count as `strings <bin> | grep -c '_app/immutable/'`.
    $s = [System.Text.Encoding]::ASCII.GetString($bytes)
    $sepArray = [string[]]@($script:AssetRefMarker)
    return ($s.Split($sepArray, [System.StringSplitOptions]::None).Count - 1)
}

function Test-AssetRefCount {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )
    $count = Get-AssetRefCount -Path $Path
    return $count -ge $script:AssetRefMin
}
