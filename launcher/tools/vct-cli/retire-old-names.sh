#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Sourced by install.sh: removes copies of THIS CLI that an earlier install.sh
# put in ~/.local/bin under the program's former names (`vct` before v0.1.0,
# `vco` until v0.2.96), and reports copies elsewhere on PATH.
#
# The rule — what counts as "this CLI under a former name", and what may be
# removed — lives in ONE place, vco_lib/launcher_cli_identity.py, which
# `vco doctor` also uses to report (never remove) such copies. This file only
# finds a Python to run it with. It needs nothing beyond the standard library,
# so a bare python3 is enough; the checkout is put first on PYTHONPATH so the
# rule that runs is this checkout's.

# vct_cli_retire_old_names <bin_dir>
vct_cli_retire_old_names() {
    local bin_dir="$1" repo py candidate
    repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
    py=""
    for candidate in "$repo/.venv/bin/python" python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            py="$candidate"
            break
        fi
    done
    if [ -z "$py" ]; then
        echo "[vct-cli] ERROR: no Python found (tried $repo/.venv/bin/python, python3, python)." >&2
        echo "          VCO needs Python 3; install it, then re-run install.sh." >&2
        return 1
    fi
    PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}" \
        "$py" -m vco_lib.launcher_cli_identity retire --bin-dir "$bin_dir"
}
