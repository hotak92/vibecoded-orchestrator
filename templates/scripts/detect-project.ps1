# detect-project.ps1 — Auto-detect which project a file belongs to.
#
# PowerShell sibling of detect-project.sh (v0.2.50 audit, 2026-06-08).
# Created because native-Windows users (no WSL) cannot source the .sh
# version; pre-edit-context-inject.ps1 + other PowerShell hooks were
# already referencing this file but it didn't ship in the bundle —
# silent loss of multi-codebase auto-detection on Windows. Same shape
# as the v0.2.49 Phase 8 trigger (templates/scripts/*.sh shipped without
# .ps1 sibling). UTF-8 BOM at file start is required for PS 5.1 (cf.
# tests/test_ps1_utf8_bom.py).
#
# Given a file path, checks if it's under the current project root.
# If not, looks for a sibling project folder under the common parent
# (e.g. ~/dev/) and returns that project's name.
#
# Usage:
#   . "$PSScriptRoot/detect-project.ps1"
#   $project = Get-ProjectForFile -FilePath "C:\path\to\file.py" -CurrentRoot "C:\current\project\root"
#   # Returns project name (e.g. "MyProject") or empty string for current project
#
# The returned name matches Weaviate collection prefixes (e.g.
# MyProject_CodeFunction).

function Get-ProjectForFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [Parameter(Mandatory = $true)] [string] $CurrentRoot
    )

    # Normalize: strip trailing slash/backslash.
    $CurrentRoot = $CurrentRoot.TrimEnd('/', '\')

    # If file is under current project root, no override needed.
    # Use OrdinalIgnoreCase to be portable across Linux (case-sensitive
    # FS) and Windows (case-insensitive FS) — the comparison still works
    # correctly on Linux paths because real-world VCO project roots
    # don't differ only in case.
    $rootWithSep = $CurrentRoot + [IO.Path]::DirectorySeparatorChar
    $altRootWithSep = $CurrentRoot + '/'
    if ($FilePath.StartsWith($rootWithSep, [StringComparison]::OrdinalIgnoreCase) -or
        $FilePath.StartsWith($altRootWithSep, [StringComparison]::OrdinalIgnoreCase)) {
        return ''
    }

    # Find the common parent directory (direct parent of project roots).
    $parentDir = Split-Path -Parent $CurrentRoot
    if (-not $parentDir) { return '' }
    $parentDir = $parentDir.TrimEnd('/', '\')

    $parentWithSep = $parentDir + [IO.Path]::DirectorySeparatorChar
    $altParentWithSep = $parentDir + '/'

    # Check if the file is under a sibling folder of CurrentRoot.
    $relative = $null
    if ($FilePath.StartsWith($parentWithSep, [StringComparison]::OrdinalIgnoreCase)) {
        $relative = $FilePath.Substring($parentWithSep.Length)
    } elseif ($FilePath.StartsWith($altParentWithSep, [StringComparison]::OrdinalIgnoreCase)) {
        $relative = $FilePath.Substring($altParentWithSep.Length)
    } else {
        return ''
    }

    # Extract the sibling folder name (first path component after parent).
    # Split on either separator to handle mixed-style paths on Windows.
    $siblingName = ($relative -split '[\\/]', 2)[0]
    if (-not $siblingName) { return '' }

    # Verify it's an actual directory (not a file in parent).
    $siblingPath = Join-Path $parentDir $siblingName
    if (Test-Path -LiteralPath $siblingPath -PathType Container) {
        return $siblingName
    }

    # File is not under any sibling project — return empty (use default).
    return ''
}
