# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/utf8-nobom.ps1 - v0.2.102 W6-R5FIX (review R5-8): the ONE BOM-free
# UTF-8 file writer for the PowerShell hooks and scripts.
#
# Windows PowerShell 5.1 - what the Windows settings template launches
# (`powershell -File`) - writes a UTF-8 BOM for `-Encoding UTF8` / `utf8` on
# Set-Content, Add-Content and Out-File. A Python reader that opens the file
# with encoding="utf-8" then sees U+FEFF in front of the first line, and a
# json.loads of that line fails (the first row is lost). pwsh 7 writes no BOM,
# so the defect never shows on a pwsh-7 dev box. These functions write
# through .NET with UTF8Encoding($false) on every PowerShell version.
#
# Semantics kept from the cmdlets they replace:
#   * -Value with several items: one item per line, each followed by
#     [Environment]::NewLine (what Set-Content / Add-Content write);
#     -NoNewline concatenates the items with nothing in between and adds no
#     trailing newline (Set-Content -NoNewline).
#   * the path is resolved against the PowerShell location, as the cmdlets
#     do - never against the process's .NET current directory.
#   * failures THROW. A caller that had -ErrorAction Stop keeps its
#     try/catch; one that had -ErrorAction SilentlyContinue wraps the call
#     in try { } catch { }.
#
# ps1-only by design (PS1_ONLY_LIB in .github/scripts/check_hook_parity.py
# and tests/test_v0292_sibling_parity_blind_spots.py): bash writes the bytes
# it is given (printf / echo / >), so the .sh siblings have no BOM to avoid.
# tests/test_v02102_w6r5fix_ps1_bom.py bans `-Encoding utf8` in the shipped
# .ps1 files outside a justified line.

function ConvertTo-VcoContentText {
    param($Value, [switch]$NoNewline)
    $items = @()
    if ($null -ne $Value) { $items = @($Value | ForEach-Object { [string]$_ }) }
    if ($NoNewline) { return ($items -join '') }
    $sb = New-Object System.Text.StringBuilder
    foreach ($i in $items) { [void]$sb.Append($i).Append([Environment]::NewLine) }
    return $sb.ToString()
}

function Resolve-VcoWritePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

# Set-VcoUtf8Content: replace the file's content (Set-Content without a BOM).
function Set-VcoUtf8Content {
    param([Parameter(Mandatory = $true)][string]$Path, $Value, [switch]$NoNewline)
    $text = ConvertTo-VcoContentText -Value $Value -NoNewline:$NoNewline
    [System.IO.File]::WriteAllText((Resolve-VcoWritePath $Path), $text,
        (New-Object System.Text.UTF8Encoding($false)))
}

# Add-VcoUtf8Content: append to the file, creating it if absent (Add-Content
# without a BOM - the BOM matters exactly when the append CREATES the file).
function Add-VcoUtf8Content {
    param([Parameter(Mandatory = $true)][string]$Path, $Value, [switch]$NoNewline)
    $text = ConvertTo-VcoContentText -Value $Value -NoNewline:$NoNewline
    [System.IO.File]::AppendAllText((Resolve-VcoWritePath $Path), $text,
        (New-Object System.Text.UTF8Encoding($false)))
}
