# SPDX-License-Identifier: AGPL-3.0-or-later
<#
.SYNOPSIS
    Add UTF-8 BOM to .ps1 files that contain non-ASCII bytes.

.DESCRIPTION
    Windows PowerShell 5.1 (the default on Win10/11) reads .ps1 files
    as Windows-1252 unless they start with a UTF-8 BOM. Files saved as
    UTF-8 without BOM containing em-dash, smart quotes, or any other
    non-ASCII characters get mis-decoded and produce parser errors at
    runtime — typically far from the actual offending character.

    This script walks the repo, finds every .ps1 with non-ASCII bytes
    and no BOM, and prepends a UTF-8 BOM. Pure-ASCII files and files
    that already have a BOM are skipped.

    Idempotent. Safe to re-run.

    Backed by tests/test_ps1_utf8_bom.py — the test will fail if any
    non-ASCII .ps1 lacks the BOM, so CI catches regressions.

.PARAMETER Path
    Root directory to scan. Defaults to the repo root.

.EXAMPLE
    .\scripts\fix-ps1-bom.ps1
    Fix all .ps1 files in the repo.

.EXAMPLE
    .\scripts\fix-ps1-bom.ps1 -Path templates\hooks
    Fix only files under templates\hooks.
#>
[CmdletBinding()]
param(
    [string]$Path = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$bom = [byte[]](0xEF, 0xBB, 0xBF)
$fixed = 0
$skipped = 0

Get-ChildItem -Path $Path -Recurse -Filter *.ps1 -File | ForEach-Object {
    $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
    if ($bytes.Length -lt 3) {
        $skipped++
        return
    }
    $hasBom = ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    $hasNonAscii = ($bytes | Where-Object { $_ -gt 127 } | Select-Object -First 1) -ne $null

    if ($hasNonAscii -and -not $hasBom) {
        $newBytes = New-Object byte[] ($bom.Length + $bytes.Length)
        [Array]::Copy($bom, 0, $newBytes, 0, $bom.Length)
        [Array]::Copy($bytes, 0, $newBytes, $bom.Length, $bytes.Length)
        [System.IO.File]::WriteAllBytes($_.FullName, $newBytes)
        Write-Host "  fixed: $($_.FullName.Substring($Path.Length + 1))"
        $fixed++
    } else {
        $skipped++
    }
}

Write-Host ""
Write-Host "Fixed:   $fixed"
Write-Host "Skipped: $skipped (already BOM or pure ASCII)"
