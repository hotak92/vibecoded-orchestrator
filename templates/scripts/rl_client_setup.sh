#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# rl_client_setup.sh — per-project install-time setup for the RL client.
#
# Creates the local data directories used by ``rl_client.rl_logger`` so
# the free-tier retrieval data collection (always-on by default; opt-out
# via Preferences) has somewhere to write before the first event fires.
#
# Opt-out: set ``RL_LOCAL_LOGGING_DISABLED=true`` in your
# ``<project>/.claude/env`` (the Preferences toggle does this for you).
# When that env is set, the logger short-circuits and writes nothing —
# but this script still creates the directories so flipping the flag
# back doesn't require a re-install.
#
# Cross-OS pair: ``rl_client_setup.ps1`` (Windows equivalent).
#
# The launcher's ``allocate_rl_port`` flow writes ``RL_PROJECT_ROOT``
# into ``.claude/settings.json::env`` so the logger picks the right
# log path on multi-project machines.

set -euo pipefail

LOCAL_RL_DATA_DIR="${HOME}/.claude/retrieval_rl_data"
PROJECT_RL_DATA_DIR=".claude/rl-data"

# 1. Shared per-machine log dir. Created chmod 700 — these logs may
#    contain query embeddings the user considers sensitive.
mkdir -p "${LOCAL_RL_DATA_DIR}"
chmod 700 "${LOCAL_RL_DATA_DIR}" 2>/dev/null || true

# 2. Per-project cache dir. Lives inside the project so the cleanup
#    workflow (Preferences "Clear local cache") can remove it without
#    touching the shared machine-wide log.
mkdir -p "${PROJECT_RL_DATA_DIR}"
chmod 700 "${PROJECT_RL_DATA_DIR}" 2>/dev/null || true

# 3. Friendly notice (only when stdout is a tty — so the per-project
#    install bundle install doesn't add noise to scripted invocations).
if [ -t 1 ]; then
    echo "[rl_client_setup] Per-project dir:    ${PROJECT_RL_DATA_DIR}"
    echo "[rl_client_setup] Per-machine log dir: ${LOCAL_RL_DATA_DIR}"
    echo "[rl_client_setup] Opt-out: set RL_LOCAL_LOGGING_DISABLED=true in .claude/env"
fi

exit 0
