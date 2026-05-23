# SPDX-License-Identifier: AGPL-3.0-or-later
#
# post-install-launcher.ps1 — Windows parity helper for the desktop icon
# step of install.py / first-install.bat.
#
# Why this file exists (#25 polish, v0.2.31):
#   install.py L8649 already has a structured Windows branch that invokes
#   `powershell.exe -File scripts/post-install-launcher.ps1 -RepoRoot
#   <root> -NoAutoLaunch`. Before v0.2.31 that PS1 didn't exist, so direct
#   `python install.py` invocations on Windows hit the fallback branch and
#   printed a "(TODO)" note — the user's only path to a desktop icon was
#   first-install.bat (which has its own inline shortcut writer at L527).
#   This helper closes that gap: install.py and first-install.bat now use
#   the same .lnk-writing logic.
#
# Scope (deliberately minimal):
#   - Refresh `Desktop\VCT Launcher.lnk` so it points at the freshly-built
#     `<install-root>\launcher\dist\windows-x64\vct-launcher.exe` (or
#     `launcher\src-tauri\target\release\vct-launcher.exe` for
#     contributor builds — first-match-wins probe).
#   - Idempotent: re-running overwrites the existing .lnk with current
#     paths (so a moved install root self-heals on next post-install run).
#   - Also writes a Start Menu entry under
#     `%APPDATA%\Microsoft\Windows\Start Menu\Programs\VCT Launcher.lnk`
#     so the launcher shows up in Start search (parity with what
#     first-install.bat L530 does).
#
# Out of scope:
#   - Building the launcher (caller is responsible — install.py already
#     drives the build via first-install.bat or the wizard).
#   - Auto-launching the GUI (install.py passes -NoAutoLaunch, since it
#     exits right after and we don't want a duplicate spawn).
#   - System-tray registration (handled by the launcher binary itself
#     once it starts).
#
# Compatibility:
#   - Targets PowerShell 5.1+ (ships with every Windows 10+ install,
#     no pwsh dependency). Uses WScript.Shell COM (universally available
#     since Windows 95) — same approach as first-install.bat L534.
#
# Exit contract:
#   0 on success, including the "shortcut already exists" case (we
#     overwrite — `WScript.Shell.CreateShortcut` does that natively).
#   1 only on actual failure (binary not found, COM object unavailable,
#     write permission denied). install.py treats non-zero as non-fatal
#     and logs it — desktop icon failure must never block install.

[CmdletBinding()]
param(
    # Positional: the orchestrator install root (the directory containing
    # `launcher\`). install.py L8654 passes this as `-RepoRoot <path>`.
    [Parameter(Position = 0)]
    [string]$RepoRoot,

    # `-NoAutoLaunch`: install.py always passes this so it can spawn the
    # GUI itself (or skip — install.py exits next, so we'd just race for
    # the same .lnk if both spawned). Reserved for future symmetry with
    # the bash helper's --no-auto-launch; this PS1 doesn't auto-spawn the
    # GUI today (Tauri spawn from PowerShell is OS-managed via the .lnk).
    [switch]$NoAutoLaunch,

    # Pretty-print path to log under, falls back to $RepoRoot\state\logs
    # to match install.py + post-install-launcher.sh.
    [string]$LogPath
)

# Resolve install root: explicit param > $env:VCT_INSTALL_ROOT > script's
# parent directory (assumes scripts/ sits at install root).
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    if (-not [string]::IsNullOrWhiteSpace($env:VCT_INSTALL_ROOT)) {
        $RepoRoot = $env:VCT_INSTALL_ROOT
    } else {
        # PSScriptRoot is the directory containing THIS script
        # (post-install-launcher.ps1). Two levels up from `scripts\` is
        # the install root.
        $RepoRoot = Split-Path -Parent $PSScriptRoot
    }
}

if (-not (Test-Path -Path $RepoRoot -PathType Container)) {
    Write-Host "[launcher] post-install-launcher.ps1: install root '$RepoRoot' not a directory" -ForegroundColor Yellow
    exit 1
}

# Normalise to a clean absolute path (handles trailing slashes, dotted
# paths, mixed separators). `Resolve-Path` is safe — we already
# Test-Path'd the directory above.
$RepoRoot = (Resolve-Path -Path $RepoRoot).Path

Write-Host "[launcher] post-install-launcher.ps1 (install root: $RepoRoot)"

# ---------------------------------------------------------------------------
# Locate the launcher binary
# ---------------------------------------------------------------------------
# First-match-wins probe. Mirror of the order in first-install.bat L145:
#   1. Locally built (contributors)
#   2. Bundled prebuilt
#   3. System install locations
# We don't do the staleness check here — first-install.bat / install.py
# already ran it before invoking us; if a binary exists at this point
# it's considered usable.

$candidates = @(
    (Join-Path $RepoRoot 'launcher\src-tauri\target\release\vct-launcher.exe'),
    (Join-Path $RepoRoot 'launcher\src-tauri\target\release\vct-launcher-temp.exe'),
    (Join-Path $RepoRoot 'launcher\src-tauri\target\release\launcher.exe'),
    (Join-Path $RepoRoot 'launcher\src-tauri\target\debug\vct-launcher-temp.exe'),
    (Join-Path $RepoRoot 'launcher\dist\windows-x64\vct-launcher.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\VCT Launcher\vct-launcher.exe'),
    (Join-Path $env:LOCALAPPDATA 'vct-launcher\vct-launcher.exe')
)

$LauncherBin = $null
foreach ($cand in $candidates) {
    if (Test-Path -Path $cand -PathType Leaf) {
        $LauncherBin = $cand
        break
    }
}

if ([string]::IsNullOrEmpty($LauncherBin)) {
    # Soft-fail: log + exit 1. install.py logs the rc as non-fatal so
    # this never blocks install. The bash helper does the same when
    # find_binary fails.
    Write-Host "[launcher] No launcher binary found under $RepoRoot — skipping shortcut refresh." -ForegroundColor Yellow
    Write-Host "[launcher] Probed:"
    foreach ($c in $candidates) { Write-Host "             $c" }
    exit 1
}

Write-Host "[launcher] Launcher binary: $LauncherBin"

# ---------------------------------------------------------------------------
# Create / refresh shortcuts
# ---------------------------------------------------------------------------
# `WScript.Shell.CreateShortcut($path)` returns either the existing
# shortcut object (mutate-in-place) or a fresh one if the file doesn't
# exist — `.Save()` then writes it. This is idempotent: overwriting an
# existing .lnk with the same target produces a byte-identical file.
#
# Shortcut paths mirror first-install.bat L530:
#   Desktop:   %USERPROFILE%\Desktop\VCT Launcher.lnk
#   StartMenu: %APPDATA%\Microsoft\Windows\Start Menu\Programs\VCT Launcher.lnk

$DesktopDir = [Environment]::GetFolderPath('Desktop')
$StartMenuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'

# StartMenuDir may not exist on heavily-locked-down installs; create it.
# Desktop is essentially guaranteed but defensive Test-Path doesn't hurt.
if (-not (Test-Path -Path $StartMenuDir -PathType Container)) {
    try {
        New-Item -Path $StartMenuDir -ItemType Directory -Force | Out-Null
    } catch {
        Write-Host "[launcher] Cannot create Start Menu dir: $StartMenuDir ($_)" -ForegroundColor Yellow
        # Don't fail the whole script over a Start Menu hiccup — the
        # Desktop .lnk is the primary entry point.
    }
}

$shortcutTargets = @()
if (Test-Path -Path $DesktopDir -PathType Container) {
    $shortcutTargets += (Join-Path $DesktopDir 'VCT Launcher.lnk')
} else {
    Write-Host "[launcher] No Desktop directory ($DesktopDir) — skipping Desktop shortcut." -ForegroundColor Yellow
}
if (Test-Path -Path $StartMenuDir -PathType Container) {
    $shortcutTargets += (Join-Path $StartMenuDir 'VCT Launcher.lnk')
}

if ($shortcutTargets.Count -eq 0) {
    Write-Host "[launcher] No writable shortcut targets — nothing to do." -ForegroundColor Yellow
    exit 1
}

$workingDir = Split-Path -Parent $LauncherBin
$wsShell = $null
try {
    $wsShell = New-Object -ComObject WScript.Shell
} catch {
    Write-Host "[launcher] Cannot instantiate WScript.Shell COM object: $_" -ForegroundColor Yellow
    exit 1
}

$createdCount = 0
foreach ($lnkPath in $shortcutTargets) {
    try {
        $shortcut = $wsShell.CreateShortcut($lnkPath)
        $shortcut.TargetPath = $LauncherBin
        $shortcut.WorkingDirectory = $workingDir
        $shortcut.Description = 'VibeCoded Tools Launcher'
        # IconLocation '<binary>,0' tells Explorer to use the .exe's
        # first embedded icon resource. Tauri bakes the launcher icon
        # into the binary at build time, so the .lnk picks it up
        # automatically — no separate .ico file needed.
        $shortcut.IconLocation = "$LauncherBin,0"
        $shortcut.Save()
        Write-Host "[launcher] Shortcut: $lnkPath"
        $createdCount++
    } catch {
        # Per-shortcut failure (e.g. RO Desktop on some corp profiles)
        # is logged but doesn't abort the others.
        Write-Host "[launcher] Failed to write $lnkPath ($_)" -ForegroundColor Yellow
    }
}

# Release the COM object. PowerShell normally cleans up on script exit,
# but explicit release is the documented best practice for COM interop.
if ($null -ne $wsShell) {
    try {
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($wsShell) | Out-Null
    } catch {
        # Marshal.ReleaseComObject can throw on already-released objects
        # under some PS versions; harmless. Swallow.
    }
    $wsShell = $null
}

if ($createdCount -eq 0) {
    Write-Host "[launcher] No shortcuts created (all writes failed)." -ForegroundColor Yellow
    exit 1
}

if ($NoAutoLaunch) {
    Write-Host "[launcher] -NoAutoLaunch set — install.py will exit; user opens launcher from the shortcut."
}

exit 0
