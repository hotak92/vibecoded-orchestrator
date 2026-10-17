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

# Get-VcoPrLinkTarget <item> - the target of a symbolic link or junction, or
# $null. Only a SymbolicLink / Junction (LinkType), or an item carrying the
# ReparsePoint attribute, has one: PowerShell's `Target` CodeProperty also
# lists a hard-linked FILE's other names, which are not links to follow
# (v0.2.102 W6-LOWS, R3 C-2). `LinkTarget` is read first (PowerShell 7.x),
# `Target` is the 5.1 fallback; both are guarded with PSObject.Properties.
function Get-VcoPrLinkTarget {
    param($Item)
    if (-not $Item) { return $null }
    $isLink = $false
    if ($Item.PSObject.Properties['LinkType'] -and ([string]$Item.LinkType -in @('SymbolicLink', 'Junction'))) { $isLink = $true }
    if (-not $isLink -and $Item.PSObject.Properties['Attributes']) {
        try {
            if ([System.IO.FileAttributes]$Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { $isLink = $true }
        } catch { }
    }
    if (-not $isLink) { return $null }
    $target = $null
    if ($Item.PSObject.Properties['LinkTarget']) { $target = $Item.LinkTarget }
    if (-not $target -and $Item.PSObject.Properties['Target']) { $target = @($Item.Target)[0] }
    if (-not $target) { return $null }
    return [string]$target
}

# Split-VcoPrPath <path> - @(root, segments...) of a path WITHOUT collapsing
# `.` or `..` (GetFullPath would collapse them lexically, before any link is
# resolved). A relative path is anchored at the process directory, which is
# what GetFullPath uses. A rooted but not fully qualified Windows form
# (`\x`, `C:x`) has no unambiguous lexical split and falls back to GetFullPath.
function Split-VcoPrPath {
    param([string]$Path)
    if (-not [System.IO.Path]::IsPathRooted($Path)) {
        $Path = [System.IO.Path]::Combine([System.IO.Directory]::GetCurrentDirectory(), $Path)
    }
    $root = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($Path))
    $rawRoot = [System.IO.Path]::GetPathRoot($Path)
    if (-not [string]::Equals(($rawRoot -replace '/', '\'), ($root -replace '/', '\'), [System.StringComparison]::OrdinalIgnoreCase)) {
        $Path = [System.IO.Path]::GetFullPath($Path)
        $rawRoot = $root
    }
    $segs = @($Path.Substring($rawRoot.Length) -split '[\\/]' | Where-Object { $_ -and $_ -ne '.' })
    return ,(@($root) + $segs)
}

# Resolve-VcoPrRealPath <path> - every symlink/junction in the path resolved,
# component by component, with Path.resolve() semantics (the .sh's `cd -P`):
# a `..` segment means "parent of the ALREADY-RESOLVED path", never a lexical
# collapse done before the links are resolved, and a relative link target is
# queued segment by segment against the link's own (resolved) directory, so a
# `..` inside a target is resolved the same way (v0.2.102 W6-LOWS, R3 C-1).
# A component that does not exist is kept as given; a link loop (>40 hops)
# returns the lexically normalised full path.
function Resolve-VcoPrRealPath {
    param([string]$Path)
    $full = ConvertTo-VcoPrFullPath $Path
    if (-not $full) { return "" }
    try { $parts = Split-VcoPrPath $Path } catch { return $full }
    $acc = [string]$parts[0]
    $queue = [System.Collections.Generic.List[string]]::new()
    for ($i = 1; $i -lt $parts.Count; $i++) { $queue.Add([string]$parts[$i]) }
    $hops = 0
    while ($queue.Count -gt 0) {
        $seg = $queue[0]
        $queue.RemoveAt(0)
        if ($seg -eq '.') { continue }
        if ($seg -eq '..') {
            $up = [System.IO.Path]::GetDirectoryName($acc)
            if ($up) { $acc = $up }
            continue
        }
        $next = Join-Path $acc $seg
        $item = $null
        try { $item = Get-Item -LiteralPath $next -Force -ErrorAction Stop } catch { $item = $null }
        $target = Get-VcoPrLinkTarget $item
        if (-not $target) { $acc = $next; continue }
        $hops++
        if ($hops -gt 40) { return $full }
        # A rooted target restarts at its own root; a relative one continues
        # from the link's directory ($acc, already resolved).
        $tsegs = @()
        if ([System.IO.Path]::IsPathRooted($target)) {
            try { $tparts = Split-VcoPrPath $target } catch { return $full }
            $acc = [string]$tparts[0]
            for ($j = 1; $j -lt $tparts.Count; $j++) { $tsegs += [string]$tparts[$j] }
        } else {
            $tsegs = @($target -split '[\\/]' | Where-Object { $_ -and $_ -ne '.' })
        }
        for ($k = $tsegs.Count - 1; $k -ge 0; $k--) { $queue.Insert(0, $tsegs[$k]) }
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
    # The directory test runs on the RESOLVED path: `link/../a` names the
    # parent of the link's target, not the lexical `a` (R3 C-1).
    $dir = Resolve-VcoPrRealPath $Start
    if (-not $dir) { return "" }
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return "" }
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

# ---------------------------------------------------------------------------
# Path containment (v0.2.104 E-1/E-2). MUST MATCH vco_path_under_root /
# vco_path_in_project in _lib/project-root.sh.
#
# Test-VcoPathUnderRoot -Root <dir> -Path <file> - $true when Path sits
# strictly inside Root, compared raw first and then with both sides
# symlink-resolved. A path EQUAL to the root is not "under" it.
function Test-VcoPathUnderRoot {
    param([string]$Root, [string]$Path)
    if (-not $Root -or -not $Path) { return $false }
    $cmp = if ($IsWindows -or $env:OS -eq 'Windows_NT') { [System.StringComparison]::OrdinalIgnoreCase } else { [System.StringComparison]::Ordinal }
    $r = $Root.TrimEnd('/', '\')
    foreach ($sep in @('/', '\')) {
        if ($Path.StartsWith($r + $sep, $cmp)) { return $true }
    }
    $rp = Resolve-VcoPrRealPath $Root
    $pp = Resolve-VcoPrRealPath $Path
    if (-not $rp -or -not $pp) { return $false }
    $rp = $rp.TrimEnd('/', '\')
    foreach ($sep in @('/', '\')) {
        if ($pp.StartsWith($rp + $sep, $cmp)) { return $true }
    }
    return $false
}

# Test-VcoPathInProject -Root <dir> -Path <file> - $true when Path is under
# Root or under one of the ENABLED extra code folders listed in
# <Root>/.claude/state/codegraph_extra_paths_cache.txt (the cache
# _lib/route-touched-path.ps1 maintains). Missing cache = no extras.
function Test-VcoPathInProject {
    param([string]$Root, [string]$Path)
    if (Test-VcoPathUnderRoot -Root $Root -Path $Path) { return $true }
    $cache = Join-Path (Join-Path (Join-Path $Root '.claude') 'state') 'codegraph_extra_paths_cache.txt'
    if (-not (Test-Path -LiteralPath $cache -PathType Leaf)) { return $false }
    try { $lines = Get-Content -LiteralPath $cache -ErrorAction Stop } catch { return $false }
    foreach ($line in $lines) {
        $l = ([string]$line).TrimEnd("`r")
        if (-not $l) { continue }
        if (Test-VcoPathUnderRoot -Root $l -Path $Path) { return $true }
    }
    return $false
}
