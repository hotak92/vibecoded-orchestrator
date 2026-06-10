#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# first-install.sh — VibeCoded Tools first-time installer (Linux).
#
# v0.2.53 (Track A + Phase 2 integration): thin shim around install.py.
# Sequence:
#   1. Python-detect (Linux distro cascade, including linuxbrew)
#   2. cd into the repo root
#   3. Bootstrap prepass: `install.py --bootstrap --json` writes a
#      system-detection envelope to state/logs/bootstrap-prepass.json
#      (read-only probe; no install side effects). Best-effort: failure
#      here does not block the full install.
#   4. Full install: `install.py <forwarded args>` runs the canonical
#      10-step flow.
#   5. Launcher post-install: scripts/post-install-launcher.sh auto-
#      spawns the launcher (download/build/launch). Honors
#      --no-auto-launch passthrough.
# All sub-steps preserve the original argv via the ARGS array.

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
# (legacy alias that install.py doesn't recognise). Also detect
# --no-auto-launch so we can skip the post-install launcher spawn.
ARGS=()
AUTO_LAUNCH=1
for a in "$@"; do
    case "$a" in
        --non-interactive) ARGS+=("--yes") ;;
        --no-auto-launch)  AUTO_LAUNCH=0 ;;
        *) ARGS+=("$a") ;;
    esac
done

# ---- Step 1: bootstrap prepass (read-only) ----
# Probes Python/Node/Podman/Docker/GPU/RAM/OS into a JSON envelope at
# state/logs/bootstrap-prepass.json. Useful for diagnostic + opens the
# door to v0.2.54 prepass-based blocker detection. Best-effort:
# failure does not stop the install (--bootstrap is exclusive with
# --update etc., so we invoke it ALONE in a separate process).
mkdir -p state/logs 2>/dev/null || true
"$PYTHON" install.py --bootstrap --json \
    > state/logs/bootstrap-prepass.json 2>/dev/null \
    || true

# ---- Step 2: full install ----
# Forward the user's argv unchanged (NOT --bootstrap; bootstrap is
# probe-only and exclusive with install flags like --update).
"$PYTHON" install.py ${ARGS[@]+"${ARGS[@]}"}
status=$?

# ---- Step 3: launcher post-install (auto-spawn) ----
# Only if install.py succeeded AND user didn't pass --no-auto-launch.
# Soft-fail: a broken launcher spawn must NOT mask a successful install.
if [ "$status" -eq 0 ] && [ "$AUTO_LAUNCH" -eq 1 ] \
   && [ -x scripts/post-install-launcher.sh ]; then
    bash scripts/post-install-launcher.sh "$SCRIPT_DIR" \
        ${ARGS[@]+"${ARGS[@]}"} \
        || true
fi

exit $status
