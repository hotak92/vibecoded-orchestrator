#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# first-install.command — VibeCoded Tools first-time installer (macOS).
#
# Why .command: Finder treats `.command` files as double-clickable
# shell scripts. The `.sh` extension opens in TextEdit by default. We
# ship two files (not a symlink) because Git on Windows doesn't
# preserve symlinks reliably.
#
# v0.2.53 (Track A): thin shim. Python-detect (Apple-Silicon + Intel
# Homebrew) + cd + exec install.py. Heavy lifting lives in install.py
# via its `--bootstrap` mode (docs/INSTALL_ARCHITECTURE_v2.md §4).
#
# TODO(Phase 2 integration, v0.2.53 → v0.2.54):
#   - Append `--bootstrap` to the install.py argv below once Track B's
#     bootstrap-mode dispatch is on main.
#   - Post-install-launcher.sh dispatch moves into install.py too; users
#     hitting the v0.2.53 shim before --bootstrap lands need to run
#     start-launcher.command by hand after install.py completes.

set -euo pipefail

# M-P1-4: Finder cwd is $HOME, not the script's dir.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Python candidate cascade. Apple Silicon Homebrew installs under
# /opt/homebrew (default since 2020); Intel Homebrew under /usr/local.
# Try Apple Silicon first since it's the modern default; fall back
# to PATH probes which catch Intel Homebrew + python.org installs.
PYTHON=""
for cand in \
    /opt/homebrew/opt/python@3.13/bin/python3.13 \
    /opt/homebrew/opt/python@3.12/bin/python3.12 \
    /opt/homebrew/opt/python@3.11/bin/python3.11 \
    /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11 \
    python3.13 python3.12 python3.11 python3 python
do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PYTHON="$cand"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Python 3.11+ required but not found."
    if [ -x "/opt/homebrew/bin/brew" ] || [ -x "/usr/local/bin/brew" ]; then
        echo "Install with: brew install python@3.13"
        if [ -t 0 ]; then
            printf "Install Python 3.13 via Homebrew now? [Y/n] "
            read -r ans || ans="Y"
            case "${ans:-Y}" in
                ""|y|Y|yes|YES)
                    brew install python@3.13
                    # Re-probe.
                    for cand in /opt/homebrew/bin/python3.13 /opt/homebrew/opt/python@3.13/bin/python3.13 /usr/local/bin/python3.13; do
                        if [ -x "$cand" ]; then PYTHON="$cand"; break; fi
                    done
                    ;;
            esac
        fi
    else
        echo "Install Homebrew first: https://brew.sh"
        echo "Then: brew install python@3.13"
    fi
fi

if [ -z "$PYTHON" ]; then
    echo "Cannot proceed without Python 3.11+."
    # Keep Terminal window open after Finder launch.
    if [ -t 0 ]; then
        read -n 1 -s -r -p "Press any key to close..." || true
        echo ""
    fi
    exit 1
fi

# Forward all arguments verbatim. Translate --non-interactive → --yes.
ARGS=()
for a in "$@"; do
    case "$a" in
        --non-interactive) ARGS+=("--yes") ;;
        *) ARGS+=("$a") ;;
    esac
done

# TODO(Phase 2): add `--bootstrap` to ARGS once Track B's dispatch lands.
exec "$PYTHON" install.py ${ARGS[@]+"${ARGS[@]}"}
