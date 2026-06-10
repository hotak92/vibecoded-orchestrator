#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# first-install.sh — VibeCoded Tools first-time installer (Linux).
#
# v0.2.53 (Track A): thin shim. Python-detect + cd + exec install.py.
# Everything else (container runtime, Node, Tauri build, launcher
# post-install) is handled by install.py via its `--bootstrap` mode
# once Track B's commit lands (see docs/INSTALL_ARCHITECTURE_v2.md §4).
#
# TODO(Phase 2 integration, v0.2.53 → v0.2.54):
#   - Append `--bootstrap` to the install.py argv below once Track B's
#     bootstrap-mode dispatch is on main. Until then, install.py runs
#     its normal flow which doesn't yet auto-spawn post-install-launcher.sh.
#   - Re-run the launcher post-install (scripts/post-install-launcher.sh)
#     is currently routed via this shim → install.sh → return; the
#     thin shape skips that wrapper. Users who hit the v0.2.53 shim
#     before --bootstrap lands will need to run start-launcher.sh by
#     hand after install.py completes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Python candidate cascade (newest first). Linuxbrew gets a separate
# /home/linuxbrew/.linuxbrew/bin/... probe after PATH probes.
PYTHON=""
for cand in \
    python3.13 python3.12 python3.11 python3 python \
    /home/linuxbrew/.linuxbrew/bin/python3.13 \
    /home/linuxbrew/.linuxbrew/bin/python3.12 \
    /home/linuxbrew/.linuxbrew/bin/python3.11 \
    /home/linuxbrew/.linuxbrew/bin/python3
do
    if command -v "$cand" >/dev/null 2>&1; then
        # Verify >= 3.11 — Python 2 / very-old 3.x are rejected.
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PYTHON="$cand"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Python 3.11+ required but not found."
    # Distro-aware install hint (apt / dnf / pacman / zypper / apk).
    if command -v apt-get >/dev/null 2>&1; then
        HINT="sudo apt-get install -y python3 python3-venv python3-pip"
    elif command -v dnf >/dev/null 2>&1; then
        HINT="sudo dnf install -y python3 python3-pip"
    elif command -v pacman >/dev/null 2>&1; then
        HINT="sudo pacman -S --noconfirm python python-pip"
    elif command -v zypper >/dev/null 2>&1; then
        HINT="sudo zypper install -y python3 python3-pip"
    elif command -v apk >/dev/null 2>&1; then
        HINT="sudo apk add python3 py3-pip"
    else
        HINT="install Python 3.11+ via your distro's package manager or https://www.python.org/downloads/"
    fi
    echo "Install hint: $HINT"
    exit 1
fi

# Forward all arguments verbatim. Translate --non-interactive → --yes
# (legacy alias that install.py doesn't recognise).
ARGS=()
for a in "$@"; do
    case "$a" in
        --non-interactive) ARGS+=("--yes") ;;
        *) ARGS+=("$a") ;;
    esac
done

# TODO(Phase 2): add `--bootstrap` to ARGS once Track B's dispatch lands.
exec "$PYTHON" install.py ${ARGS[@]+"${ARGS[@]}"}
