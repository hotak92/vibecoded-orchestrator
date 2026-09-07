# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/metrics-dir.sh — the ONE shell-side answer to "where do VCO's metrics
# live?". Sourced by every hook that reads or writes a `*.jsonl` telemetry
# stream (cost-tracker, post-compact, stop-failure-notify, kg-update-nudge,
# subagent-stop-reconcile, embedding-failures-surface).
#
# Lockstep sibling: `_lib/metrics-dir.ps1`. Change one, change both — they are
# executed against each other and against the Python SSOT by
# `tests/test_v0292_wp8_metrics_shell_parity.py`.
#
# v0.2.92 W7. The metrics home moved OUT of `~/.claude`: that directory is
# Claude Code's, and the standing directive is that VCO writes nothing under
# it the harness did not ask for. The streams now live beside every other
# piece of VCO state, under `$VCT_STATE_DIR` (default `~/.vct`).
#
# Before this helper, six .sh hooks + six .ps1 siblings each reconstructed the
# path inline, and they had ALREADY drifted on how they find the home
# directory alone: `$HOME`, `[System.Environment]::GetFolderPath('UserProfile')`,
# and `$env:HOME`-then-`$env:USERPROFILE` were all in use. One home, two
# flavours, pinned to the Python resolver by a parity test.
#
# Python source of truth: `vco_lib/paths.py`
#   vct_metrics_dir()           -> $VCT_STATE_DIR/metrics  (default ~/.vct/metrics)
#   legacy_claude_metrics_dir() -> $VCT_CLAUDE_DIR/metrics (default ~/.claude/metrics)
# `tests/test_v0292_wp8_metrics_shell_parity.py` executes this file and the
# .ps1 sibling and asserts both agree with those two functions across the
# override matrix. Change the rule here and that test fails until the Python
# side moves with it — that is the point of it.
#
# After sourcing, the caller has:
#
#   VCO_METRICS_DIR         write target — the new home, OR the archive while
#                           a verified copy is still owed (see below)
#   VCO_METRICS_HOME        the new home, unconditionally
#   VCO_LEGACY_METRICS_DIR  the frozen archive, unconditionally
#   VCO_METRICS_MIGRATED    1 when the copy is done / not needed, else 0
#
# **Writers switch only after a verified copy.** `VCO_METRICS_DIR` is the new
# home when — and only when — one of these holds:
#
#   * the archive does not exist (fresh install: nothing to copy), or
#   * the archive holds no `*.jsonl` (nothing to copy), or
#   * `<home>/.migrated-from-claude.json` exists — the record that
#     `vco_lib.metrics_migration` copied AND verified every archived file.
#
# Otherwise writers stay on the archive, where that machine's history already
# is. Nothing is stranded and nothing is double-counted; the next migration
# run finishes the job and the writers move on their own.
#
# **The archive is never written to by the migration and never deleted by
# anything.** This helper only ever READS the archive path.
#
# This file is sourced, never executed, so it has no shebang. It is a library,
# not a hook — it is NOT registered in settings.json.

# Resolve the user's home the way `pathlib.Path.home()` does, with a fallback
# for the one shell that can reach here without $HOME (cmd.exe routing through
# Git Bash). Never guesses a literal path.
_vco_metrics_user_home() {
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

# Populate the four VCO_METRICS_* variables. Idempotent; never fails the
# caller (a hook must not die because a metrics path could not be resolved).
vco_resolve_metrics_dirs() {
    local home state_root
    home="$(_vco_metrics_user_home 2>/dev/null || printf '')"

    if [ -n "${VCT_STATE_DIR:-}" ]; then
        state_root="$VCT_STATE_DIR"
    elif [ -n "$home" ]; then
        state_root="$home/.vct"
    else
        state_root=""
    fi

    if [ -n "${VCT_CLAUDE_DIR:-}" ]; then
        VCO_LEGACY_METRICS_DIR="$VCT_CLAUDE_DIR/metrics"
    elif [ -n "$home" ]; then
        VCO_LEGACY_METRICS_DIR="$home/.claude/metrics"
    else
        VCO_LEGACY_METRICS_DIR=""
    fi

    if [ -n "$state_root" ]; then
        VCO_METRICS_HOME="$state_root/metrics"
    else
        VCO_METRICS_HOME=""
    fi

    # Migration state. "No archive" and "empty archive" both count as done:
    # there is nothing to copy, so there is nothing to wait for.
    VCO_METRICS_MIGRATED=1
    if [ -n "$VCO_LEGACY_METRICS_DIR" ] && [ -d "$VCO_LEGACY_METRICS_DIR" ]; then
        if [ -n "$VCO_METRICS_HOME" ] && [ -f "$VCO_METRICS_HOME/.migrated-from-claude.json" ]; then
            VCO_METRICS_MIGRATED=1
        else
            # `ls` rather than a glob so an unreadable archive answers "no
            # jsonl" instead of leaving the literal pattern in the variable.
            if ls "$VCO_LEGACY_METRICS_DIR"/*.jsonl >/dev/null 2>&1; then
                VCO_METRICS_MIGRATED=0
            fi
        fi
    fi

    if [ "$VCO_METRICS_MIGRATED" = "1" ] && [ -n "$VCO_METRICS_HOME" ]; then
        VCO_METRICS_DIR="$VCO_METRICS_HOME"
    else
        VCO_METRICS_DIR="$VCO_LEGACY_METRICS_DIR"
    fi

    export VCO_METRICS_DIR VCO_METRICS_HOME VCO_LEGACY_METRICS_DIR VCO_METRICS_MIGRATED
    return 0
}

# `mkdir -p` the write target and echo it. Empty output (and a non-zero
# return) when it could not be resolved or created — callers treat that as
# "skip the metrics write", never as a reason to fail the hook.
vco_metrics_dir() {
    [ -n "${VCO_METRICS_DIR:-}" ] || vco_resolve_metrics_dirs
    [ -n "${VCO_METRICS_DIR:-}" ] || return 1
    mkdir -p "$VCO_METRICS_DIR" 2>/dev/null || return 1
    printf '%s' "$VCO_METRICS_DIR"
}

# Echo the FIRST existing copy of a metrics file, new home before archive, so
# a reader on a machine that has not migrated (or whose user-modified hook
# still appends to the archive) still finds its data. Empty when neither
# exists; callers decide whether that is an error.
vco_metrics_read_file() {
    local name="$1" candidate
    [ -n "$name" ] || return 1
    [ -n "${VCO_METRICS_HOME:-}${VCO_LEGACY_METRICS_DIR:-}" ] || vco_resolve_metrics_dirs
    for candidate in "${VCO_METRICS_HOME:-}" "${VCO_LEGACY_METRICS_DIR:-}"; do
        [ -n "$candidate" ] || continue
        if [ -f "$candidate/$name" ]; then
            printf '%s' "$candidate/$name"
            return 0
        fi
    done
    return 1
}

# Best-effort trigger for the once-per-machine COPY. Costs one `[ -d ]` on a
# fresh install and one `[ -f ]` after the first success — the Python is only
# spawned while a copy is genuinely owed.
#
# Soft-fails in every direction: no VCO venv, no vco_lib, a migration that
# errors — all leave `VCO_METRICS_MIGRATED=0`, which keeps writers on the
# archive. That is the safe state, not a broken one: the data stays where the
# reader already looks, and the next session tries again.
vco_metrics_migrate_once() {
    local script_dir="$1"
    [ -n "${VCO_METRICS_MIGRATED:-}" ] || vco_resolve_metrics_dirs
    [ "${VCO_METRICS_MIGRATED:-1}" = "0" ] || return 0
    [ -n "${VCO_METRICS_HOME:-}" ] || return 0
    [ -f "$script_dir/_lib/resolve-vco-venv.sh" ] || return 0
    # shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
    . "$script_dir/_lib/resolve-vco-venv.sh"
    resolve_vco_venv_python "$script_dir" 2>/dev/null || true
    [ -n "${VCO_VENV_PYTHON:-}" ] || return 0
    "$VCO_VENV_PYTHON" -m vco_lib.metrics_migration --quiet >/dev/null 2>&1 || true
    # Re-resolve so this invocation's writes already use the new home when the
    # copy just succeeded.
    vco_resolve_metrics_dirs
    return 0
}
