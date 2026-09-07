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
# Cross-OS pair: ``rl_client_setup.ps1`` (Windows equivalent). The two are
# executed against each other and against the Python SSOT by
# ``tests/test_v0292_regclean_rl_setup_state_root.py`` — change one, change
# both.
#
# The launcher's ``allocate_rl_port`` flow writes ``RL_PROJECT_ROOT``
# into ``.claude/settings.json::env`` so the logger picks the right
# log path on multi-project machines.
#
# ─── v0.2.92, register item 28: this script no longer writes under ~/.claude
#
# It used to `mkdir -p "${HOME}/.claude/retrieval_rl_data"`, and — unlike the
# Python default it was pairing with — nothing could steer that literal. It
# runs from `vco_lib/project_init.py::_run_rl_client_setup`, called
# UNCONDITIONALLY (except `--dry-run`) by `install_project_bundle`, so it was
# the live creator of that directory on every project install AND every
# update, for every user. The standing directive (2026-08-29) is that VCO
# writes NOTHING under `~/.claude` except what the harness itself requires:
# that directory belongs to Claude Code.
#
# The corpus home is now `<vct_root>/retrieval_rl_data`, the SAME answer
# `claude_mcp_servers/rl_client/rl_logger.py::default_rl_data_dir()` gives
# (`vco_lib.paths.vct_root_dir() / "retrieval_rl_data"`), so the directory
# this script creates is the directory the logger writes to.
#
# **The pre-v0.2.92 corpus stays exactly where it is.** Nothing here moves,
# copies, reads or deletes `~/.claude/retrieval_rl_data`. That corpus is
# frozen (v0.2.47 replaced the JSONL sink with the vct-hub `rl_events` table)
# and its four remaining consumers all address the OLD path BY NAME — the paid
# RL container's bind mount, the launcher dashboard reader, the Preferences
# "delete local retrieval data" action, and
# `claude_mcp_servers/scripts/migrate_rl_jsonl_to_db.py`. Only the WRITE
# DEFAULT moved; the archive is deleted by nobody but the user.
#
# **Why the state root is resolved inline here** rather than through
# `.claude/scripts/vct_project_config.sh`: that resolver answers per-PROJECT
# questions from the hub (`GET /api/v1/projects/{id}/config`), and the hub's
# `ProjectConfigResponse` carries no state-root/RL-corpus field — asking it
# would cost a round-trip and still not answer this question. It is also the
# wrong dependency for an install-time step that must succeed on a machine
# whose launcher has never started. So this is a class-C cross-language mirror
# of a two-line rule (CLAUDE.md A>B>C), which is permitted only WITH an
# enforcing parity test — see the test named above, which executes this file
# and the .ps1 sibling and compares both against `vco_lib.paths.vct_root_dir`.
# The same trade-off, for the same reason, produced `_lib/metrics-dir.sh`.

set -euo pipefail

# Resolve the user's home the way `pathlib.Path.home()` does, with the one
# fallback that matters (cmd.exe routing through Git Bash sets USERPROFILE but
# not HOME). Never guesses a literal path; empty output means "unknown", which
# the caller treats as "skip the per-machine directory" rather than inventing
# a root.  Mirrors `_lib/metrics-dir.sh::_vco_metrics_user_home`.
_rl_user_home() {
    if [ -n "${HOME:-}" ]; then
        printf '%s' "$HOME"
        return 0
    fi
    if [ -n "${USERPROFILE:-}" ]; then
        printf '%s' "$USERPROFILE"
        return 0
    fi
    return 1
}

# `vco_lib.paths.vct_root_dir()` in two lines: $VCT_STATE_DIR, else <home>/.vct.
# Nothing else — no orchestrator-root ladder, no hub call, no third tier.
_rl_state_root() {
    local home
    if [ -n "${VCT_STATE_DIR:-}" ]; then
        printf '%s' "$VCT_STATE_DIR"
        return 0
    fi
    home="$(_rl_user_home 2>/dev/null || printf '')"
    if [ -n "$home" ]; then
        printf '%s' "$home/.vct"
        return 0
    fi
    return 1
}

LOCAL_RL_DATA_DIR="$(_rl_state_root 2>/dev/null || printf '')"
if [ -n "$LOCAL_RL_DATA_DIR" ]; then
    LOCAL_RL_DATA_DIR="${LOCAL_RL_DATA_DIR}/retrieval_rl_data"
fi
PROJECT_RL_DATA_DIR=".claude/rl-data"

# 1. Shared per-machine log dir. Created chmod 700 — these logs may
#    contain query embeddings the user considers sensitive. Skipped when the
#    state root could not be resolved at all: a machine with neither
#    $VCT_STATE_DIR nor a home directory gets no directory rather than one
#    under a guessed path.
if [ -n "$LOCAL_RL_DATA_DIR" ]; then
    mkdir -p "${LOCAL_RL_DATA_DIR}"
    chmod 700 "${LOCAL_RL_DATA_DIR}" 2>/dev/null || true
fi

# 2. Per-project cache dir. Lives inside the project so the cleanup
#    workflow (Preferences "Clear local cache") can remove it without
#    touching the shared machine-wide log.
mkdir -p "${PROJECT_RL_DATA_DIR}"
chmod 700 "${PROJECT_RL_DATA_DIR}" 2>/dev/null || true

# 3. Friendly notice (only when stdout is a tty — so the per-project
#    install bundle install doesn't add noise to scripted invocations).
if [ -t 1 ]; then
    echo "[rl_client_setup] Per-project dir:    ${PROJECT_RL_DATA_DIR}"
    echo "[rl_client_setup] Per-machine log dir: ${LOCAL_RL_DATA_DIR:-(unresolved — set VCT_STATE_DIR or HOME)}"
    echo "[rl_client_setup] Opt-out: set RL_LOCAL_LOGGING_DISABLED=true in .claude/env"
fi

exit 0
