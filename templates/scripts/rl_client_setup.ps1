# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# rl_client_setup.ps1 — per-project install-time setup for the RL client (Windows).
#
# Creates the local data directories used by ``rl_client.rl_logger`` so
# the free-tier retrieval data collection (always-on by default; opt-out
# via Preferences) has somewhere to write before the first event fires.
#
# Opt-out: set ``RL_LOCAL_LOGGING_DISABLED=true`` in your
# ``<project>\.claude\env`` (the Preferences toggle does this for you).
# When that env is set, the logger short-circuits and writes nothing —
# but this script still creates the directories so flipping the flag
# back doesn't require a re-install.
#
# Cross-OS pair: ``rl_client_setup.sh`` (POSIX equivalent).
#
# The launcher's ``allocate_rl_port`` flow writes ``RL_PROJECT_ROOT``
# into ``.claude\settings.json::env`` so the logger picks the right
# log path on multi-project machines.

$ErrorActionPreference = 'Stop'

$LocalRlDataDir = Join-Path $env:USERPROFILE '.claude\retrieval_rl_data'
$ProjectRlDataDir = '.claude\rl-data'

# 1. Shared per-machine log dir.
if (-not (Test-Path $LocalRlDataDir)) {
    New-Item -ItemType Directory -Force -Path $LocalRlDataDir | Out-Null
}

# 2. Per-project cache dir.
if (-not (Test-Path $ProjectRlDataDir)) {
    New-Item -ItemType Directory -Force -Path $ProjectRlDataDir | Out-Null
}

# 3. Friendly notice when running interactively.
if ([Environment]::UserInteractive) {
    Write-Host "[rl_client_setup] Per-project dir:    $ProjectRlDataDir"
    Write-Host "[rl_client_setup] Per-machine log dir: $LocalRlDataDir"
    Write-Host "[rl_client_setup] Opt-out: set RL_LOCAL_LOGGING_DISABLED=true in .claude\env"
}

exit 0
