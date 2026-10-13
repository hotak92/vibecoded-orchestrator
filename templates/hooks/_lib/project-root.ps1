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
# Symlinks and junctions: like Python's Path.resolve() and the .sh mirror's
# `cd -P`, the start dir and both homes are resolved to their real paths
# (Resolve-VcoPrRealPath) before the walk, so a link above the project cannot
# hide its marker.
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

# Resolve-VcoPrRealPath <path> - every symlink/junction in an existing path
# resolved, component by component (Path.resolve() semantics). A component that
# cannot be inspected is kept as given; a link loop (>40 hops) returns the
# unresolved full path.
function Resolve-VcoPrRealPath {
    param([string]$Path)
    $full = ConvertTo-VcoPrFullPath $Path
    if (-not $full) { return "" }
    $acc = [System.IO.Path]::GetPathRoot($full)
    $queue = [System.Collections.Generic.List[string]]::new()
    foreach ($seg in ($full.Substring($acc.Length) -split '[\\/]')) { if ($seg) { $queue.Add($seg) } }
    $hops = 0
    while ($queue.Count -gt 0) {
        $seg = $queue[0]
        $queue.RemoveAt(0)
        $next = Join-Path $acc $seg
        $item = $null
        try { $item = Get-Item -LiteralPath $next -Force -ErrorAction Stop } catch { $item = $null }
        $target = $null
        if ($item) {
            if ($item.PSObject.Properties['LinkTarget']) { $target = $item.LinkTarget }
            if (-not $target -and $item.PSObject.Properties['Target']) { $target = @($item.Target)[0] }
        }
        if (-not $target) { $acc = $next; continue }
        $hops++
        if ($hops -gt 40) { return $full }
        if (-not [System.IO.Path]::IsPathRooted($target)) { $target = Join-Path $acc $target }
        $tfull = ConvertTo-VcoPrFullPath $target
        if (-not $tfull) { return $full }
        $acc = [System.IO.Path]::GetPathRoot($tfull)
        $tsegs = @($tfull.Substring($acc.Length) -split '[\\/]' | Where-Object { $_ })
        for ($i = $tsegs.Count - 1; $i -ge 0; $i--) { $queue.Insert(0, $tsegs[$i]) }
    }
    return (ConvertTo-VcoPrFullPath $acc)
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
            $full = Resolve-VcoPrRealPath $h
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
    $dir = Resolve-VcoPrRealPath $Start
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
