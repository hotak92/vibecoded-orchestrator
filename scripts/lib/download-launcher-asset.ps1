# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# download-launcher-asset.ps1 — resolve + download the prebuilt Windows
# launcher from GitHub Releases.
#
# v0.2.54 G-1.5 (Wave 0 follow-up): release.yml has shipped ONLY
# `vibecoded-orchestrator-<version>-windows-x64.zip` since the 2026-05-10
# uniform-zip packaging change (verified against the live v0.2.53 release:
# 3 x .zip + 3 x .zip.sha256, zero .exe assets). first-install.bat's
# previous inline downloader filtered `$_.name.EndsWith('.exe')`, so the
# asset lookup ALWAYS returned NO_ASSET and every Windows first-run fell
# through to the 15-30 min source build (or a loud failure without the
# Rust + Node toolchain). This is the Windows sibling of the POSIX
# M-P0-3 fix already shipped in scripts/post-install-launcher.sh.
#
# Behaviour:
#   1. Query the GitHub Releases API for the latest release.
#   2. Prefer a `*windows*.zip` asset (current packaging). The `.EndsWith`
#      check also excludes the `.zip.sha256` sidecar assets.
#   3. Legacy fallback: a `*windows*.exe` asset (pre-2026-05-10 packaging,
#      kept for if/when CI resumes shipping bare executables).
#   4. zip path: download to %TEMP%, Expand-Archive (PowerShell 5.0+,
#      ships with every Win10+ install), locate vct-launcher.exe inside
#      the archive (release zips nest it under
#      vibecoded-orchestrator-<version>-windows-x64/), copy it to
#      -DestDir. vct-hub.exe + vct-updater.exe are copied too when
#      present (they ship in the same archive since v0.2.21).
#   5. Sanity-check the landed binary is >= -MinSizeBytes (default 10 MB;
#      a healthy vct-launcher.exe is ~24-31 MB).
#
# Exit codes (consumed by first-install.bat :launch_download):
#   0  success — vct-launcher.exe landed at <DestDir>\vct-launcher.exe
#   1  generic error (network, API, extraction crash) — message on stdout
#   2  NO_ASSET — release has no matching windows .zip or .exe asset
#   3  TOO_SMALL — downloaded binary smaller than -MinSizeBytes
#   4  NO_BINARY_IN_ZIP — zip downloaded + extracted but no vct-launcher.exe inside
#
# Parameters:
#   -DestDir      directory to land vct-launcher.exe in (created if missing)
#   -ApiUrl       GitHub Releases API endpoint (overridable for tests —
#                 tests/test_download_launcher_asset_ps1.py points this at
#                 a localhost fixture server)
#   -MinSizeBytes minimum acceptable binary size (overridable for tests)

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DestDir,

    [string]$ApiUrl = 'https://api.github.com/repos/hotak92/vibecoded-orchestrator/releases/latest',

    [long]$MinSizeBytes = 10MB
)

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 renders a console progress bar during
# Invoke-WebRequest that slows a ~22 MB download by an order of
# magnitude. pwsh 7 is unaffected; silencing is harmless there.
$ProgressPreference = 'SilentlyContinue'

try {
    if (-not (Test-Path -LiteralPath $DestDir)) {
        New-Item -ItemType Directory -Path $DestDir -Force | Out-Null
    }
    $destExe = Join-Path $DestDir 'vct-launcher.exe'

    $release = Invoke-RestMethod -Uri $ApiUrl -UseBasicParsing

    # Current packaging: uniform .zip per OS. EndsWith('.zip') excludes
    # the .zip.sha256 checksum sidecars that ship alongside.
    $asset = $release.assets |
        Where-Object { $_.name -like '*windows*' -and $_.name.EndsWith('.zip') } |
        Select-Object -First 1
    $isZip = $true

    if (-not $asset) {
        # Legacy packaging fallback: bare .exe asset.
        $asset = $release.assets |
            Where-Object { $_.name -like '*windows*' -and $_.name.EndsWith('.exe') } |
            Select-Object -First 1
        $isZip = $false
    }

    if (-not $asset) {
        Write-Host 'NO_ASSET'
        exit 2
    }

    Write-Host ('Downloading ' + $asset.name)

    if (-not $isZip) {
        # Legacy direct-exe path.
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $destExe -UseBasicParsing
    }
    else {
        $tmpRoot = Join-Path ([IO.Path]::GetTempPath()) ('vct-launcher-dl-' + [IO.Path]::GetRandomFileName())
        New-Item -ItemType Directory -Path $tmpRoot -Force | Out-Null
        try {
            $zipPath = Join-Path $tmpRoot $asset.name
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -UseBasicParsing

            $extractDir = Join-Path $tmpRoot 'extracted'
            # Expand-Archive ships with PowerShell 5.0+ (every Win10+ box)
            # and with pwsh 7 — no external unzip dependency.
            Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force

            $launcherInZip = Get-ChildItem -Path $extractDir -Recurse -Filter 'vct-launcher.exe' -File |
                Select-Object -First 1
            if (-not $launcherInZip) {
                Write-Host 'NO_BINARY_IN_ZIP'
                exit 4
            }
            Copy-Item -LiteralPath $launcherInZip.FullName -Destination $destExe -Force

            # vct-hub.exe (v0.2.21+) + vct-updater.exe (v0.2.4x+) ship in
            # the same archive. Land them next to the launcher so the
            # post-install hub/updater resolvers find them without a
            # second download. Best-effort: absence is not an error
            # (older releases predate them).
            foreach ($extra in @('vct-hub.exe', 'vct-updater.exe')) {
                $extraInZip = Get-ChildItem -Path $extractDir -Recurse -Filter $extra -File |
                    Select-Object -First 1
                if ($extraInZip) {
                    Copy-Item -LiteralPath $extraInZip.FullName -Destination (Join-Path $DestDir $extra) -Force
                }
            }
        }
        finally {
            Remove-Item -LiteralPath $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    $size = (Get-Item -LiteralPath $destExe).Length
    if ($size -lt $MinSizeBytes) {
        Write-Host 'TOO_SMALL'
        exit 3
    }
    Write-Host ('Downloaded {0:N1} MB' -f ($size / 1MB))
    exit 0
}
catch {
    Write-Host ('ERR: ' + $_.Exception.Message)
    exit 1
}
