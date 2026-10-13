# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/project-root.ps1 - PowerShell sibling of _lib/project-root.sh: the
# answer to "which VCO project is here?" for hooks.
#
# MUST MATCH vco_lib/project_root.py :: resolve_project_root (the Python SSOT)
# and _lib/project-root.sh. C-rule mirror (hooks may run with no vco_lib):
# identical data, and tests/test_v02102_w6w_project_root_parity.py executes
# py / sh / ps1 (and the stdlib mirror in agent-skill-keyword-match.py) over
# one shared fixture table.
#
# The rule (v0.2.102 W6-W):
#   1. $env:CLAUDE_PROJECT_DIR when set and not blank (trimmed) - taken as given.
#   2. Else the nearest folder at or above the start dir (default: the current
#      location) that has .claude/settings.json or .claude/env. A bare .claude/
#      is NOT a marker; a .git is NOT a boundary.
#   3. The walk stops at the user's home and never accepts it. "Home" is EITHER
#      VCT_USER_HOME_OVERRIDE (else the OS home) OR the OS home
#      ([Environment]::GetFolderPath('UserProfile'), which is what Python's
#      Path.home() answers on every OS).
#   4. Nothing found -> "". Callers that WRITE skip the write; they never fall
#      back to the current location.
#
# Known, documented difference: Python resolves symlinks (Path.resolve());
# this file walks the full path as given. Both name the same directory; the
# parity test compares real paths.
#
# Sourced, never executed. Not a hook; not registered in settings.json.

function ConvertTo-VcoPrFullPath {
    param([string]$Path)
    if (-not $Path) { return "" }
    try {
        $full = [System.IO.Path]::GetFullPath($Path)
    } catch {
        return ""
    }
    # Drop a trailing separator unless it is the filesystem root.
    $trimmed = $full.TrimEnd([char[]]@('\', '/'))
    if (-not $trimmed) { return $full }
    if ($trimmed -match '^[A-Za-z]:$') { return $full }
    return $trimmed
}

function Get-VcoPrHomes {
    $osHome = [System.Environment]::GetFolderPath('UserProfile')
    if (-not $osHome) { $osHome = $env:HOME }
    if (-not $osHome) { $osHome = $env:USERPROFILE }
    $override = ""
    if ($env:VCT_USER_HOME_OVERRIDE) { $override = $env:VCT_USER_HOME_OVERRIDE.Trim() }
    $homeA = if ($override) { $override } else { $osHome }
    $homes = @()
    foreach ($h in @($homeA, $osHome)) {
        if ($h) {
            $full = ConvertTo-VcoPrFullPath $h
            if ($full) { $homes += $full }
        }
    }
    return ,$homes
}

# Find-VcoProjectRoot [-Start <dir>] - the walk alone (rungs 2-4).
function Find-VcoProjectRoot {
    param([string]$Start = "")
    if (-not $Start) { $Start = (Get-Location).Path }
    if (-not (Test-Path -LiteralPath $Start -PathType Container)) { return "" }
    $dir = ConvertTo-VcoPrFullPath $Start
    if (-not $dir) { return "" }
    $homes = Get-VcoPrHomes
    $cmp = if ($IsWindows -or $env:OS -eq 'Windows_NT') { [System.StringComparison]::OrdinalIgnoreCase } else { [System.StringComparison]::Ordinal }
    while ($dir) {
        foreach ($h in $homes) {
            if ([string]::Equals($dir, $h, $cmp)) { return "" }
        }
        $claude = Join-Path $dir ".claude"
        if ((Test-Path -LiteralPath (Join-Path $claude "settings.json") -PathType Leaf) -or
            (Test-Path -LiteralPath (Join-Path $claude "env") -PathType Leaf)) {
            return $dir
        }
        $parent = [System.IO.Directory]::GetParent($dir)
        if ($null -eq $parent) { return "" }
        $dir = ConvertTo-VcoPrFullPath $parent.FullName
    }
    return ""
}

# Get-VcoProjectRoot [-Start <dir>] - the full rule. Returns the root or "".
function Get-VcoProjectRoot {
    param([string]$Start = "")
    if ($env:CLAUDE_PROJECT_DIR) {
        $ws = $env:CLAUDE_PROJECT_DIR.Trim()
        if ($ws) { return $ws }
    }
    return Find-VcoProjectRoot -Start $Start
}
