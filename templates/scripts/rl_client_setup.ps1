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
# Cross-OS pair: ``rl_client_setup.sh`` (POSIX equivalent). The two are
# executed against each other and against the Python SSOT by
# ``tests/test_v0292_regclean_rl_setup_state_root.py`` — change one, change
# both.
#
# The launcher's ``allocate_rl_port`` flow writes ``RL_PROJECT_ROOT``
# into ``.claude\settings.json::env`` so the logger picks the right
# log path on multi-project machines.
#
# ─── v0.2.92, register item 28: this script no longer writes under ~/.claude
#
# It used to create ``$env:USERPROFILE\.claude\retrieval_rl_data``, and —
# unlike the Python default it was pairing with — nothing could steer that
# literal. It runs from ``vco_lib/project_init.py::_run_rl_client_setup``,
# called UNCONDITIONALLY (except ``--dry-run``) by ``install_project_bundle``,
# so it was the live creator of that directory on every project install AND
# every update, for every user. The standing directive (2026-08-29) is that
# VCO writes NOTHING under ``~/.claude`` except what the harness itself
# requires: that directory belongs to Claude Code.
#
# The corpus home is now ``<vct_root>\retrieval_rl_data``, the SAME answer
# ``claude_mcp_servers/rl_client/rl_logger.py::default_rl_data_dir()`` gives
# (``vco_lib.paths.vct_root_dir() / "retrieval_rl_data"``), so the directory
# this script creates is the directory the logger writes to.
#
# **The pre-v0.2.92 corpus stays exactly where it is.** Nothing here moves,
# copies, reads or deletes ``~\.claude\retrieval_rl_data``. That corpus is
# frozen (v0.2.47 replaced the JSONL sink with the vct-hub ``rl_events``
# table) and its four remaining consumers all address the OLD path BY NAME —
# the paid RL container's bind mount, the launcher dashboard reader, the
# Preferences "delete local retrieval data" action, and
# ``claude_mcp_servers/scripts/migrate_rl_jsonl_to_db.py``. Only the WRITE
# DEFAULT moved; the archive is deleted by nobody but the user.
#
# **Why the state root is resolved inline here** rather than through
# ``.claude\scripts\vct_project_config.ps1``: that resolver answers
# per-PROJECT questions from the hub (``GET /api/v1/projects/{id}/config``),
# and the hub's ``ProjectConfigResponse`` carries no state-root/RL-corpus
# field — asking it would cost a round-trip and still not answer this
# question. It is also the wrong dependency for an install-time step that must
# succeed on a machine whose launcher has never started. So this is a class-C
# cross-language mirror of a two-line rule (CLAUDE.md A>B>C), permitted only
# WITH an enforcing parity test — see the test named above. The same
# trade-off, for the same reason, produced ``_lib\metrics-dir.ps1``.

$ErrorActionPreference = 'Stop'

# Mirror of ``pathlib.Path.home()``: .NET's UserProfile folder resolves to
# %USERPROFILE% on Windows and to $HOME on Linux/macOS, which is exactly what
# Python does. Falls back only if that returns empty. Same helper shape as
# ``_lib\metrics-dir.ps1::Get-VcoMetricsUserHome``.
function Get-RlClientUserHome {
    $userHome = [System.Environment]::GetFolderPath('UserProfile')
    if (-not $userHome) { $userHome = $env:USERPROFILE }
    if (-not $userHome) { $userHome = $env:HOME }
    if (-not $userHome) { $userHome = $HOME }
    return $userHome
}

# ``vco_lib.paths.vct_root_dir()`` in two lines: $VCT_STATE_DIR, else
# <home>\.vct. Nothing else — no orchestrator-root ladder, no hub call, no
# third tier. Returns "" when neither is resolvable; the caller then skips the
# per-machine directory rather than inventing a root.
function Get-RlClientStateRoot {
    if ($env:VCT_STATE_DIR) { return $env:VCT_STATE_DIR }
    $userHome = Get-RlClientUserHome
    if ($userHome) { return (Join-Path $userHome '.vct') }
    return ""
}

$StateRoot = Get-RlClientStateRoot
$LocalRlDataDir = ""
if ($StateRoot) { $LocalRlDataDir = Join-Path $StateRoot 'retrieval_rl_data' }
# Join-Path rather than a '.claude\rl-data' literal so the same file is correct
# under pwsh on any OS (the parity test executes it on the Linux runner).
$ProjectRlDataDir = Join-Path '.claude' 'rl-data'

# 1. Shared per-machine log dir. Skipped when the state root could not be
#    resolved at all: a machine with neither $VCT_STATE_DIR nor a home
#    directory gets no directory rather than one under a guessed path.
if ($LocalRlDataDir) {
    if (-not (Test-Path -LiteralPath $LocalRlDataDir)) {
        New-Item -ItemType Directory -Force -Path $LocalRlDataDir | Out-Null
    }
}

# 2. Per-project cache dir.
if (-not (Test-Path -LiteralPath $ProjectRlDataDir)) {
    New-Item -ItemType Directory -Force -Path $ProjectRlDataDir | Out-Null
}

# 3. Friendly notice when running interactively.
if ([Environment]::UserInteractive) {
    $shown = if ($LocalRlDataDir) { $LocalRlDataDir } else { '(unresolved — set VCT_STATE_DIR or a home directory)' }
    Write-Host "[rl_client_setup] Per-project dir:    $ProjectRlDataDir"
    Write-Host "[rl_client_setup] Per-machine log dir: $shown"
    Write-Host "[rl_client_setup] Opt-out: set RL_LOCAL_LOGGING_DISABLED=true in .claude\env"
}

exit 0
