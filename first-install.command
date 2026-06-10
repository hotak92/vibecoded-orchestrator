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
# v0.2.53 (Track A + Phase 2 integration): thin shim around install.py.
# Sequence:
#   1. Python-detect (Apple Silicon Homebrew, Intel Homebrew, PATH)
#   2. cd into the repo root (Finder cwd is $HOME — M-P1-4)
#   3. Bootstrap prepass: `install.py --bootstrap --json` writes a
#      system-detection envelope to state/logs/bootstrap-prepass.json
#      (read-only probe; best-effort, never blocks the install)
#   4. Full install: `install.py <forwarded args>` runs the canonical
#      10-step flow
#   5. Launcher post-install: scripts/post-install-launcher.sh auto-
#      spawns the launcher unless --no-auto-launch was passed
# All sub-steps preserve the original argv via the ARGS array.

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
# Detect --no-auto-launch so we can skip the post-install launcher spawn.
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
# state/logs/bootstrap-prepass.json. Useful for diagnostics + future
# prepass-based blocker detection. Best-effort; failure does not stop
# the install (--bootstrap is exclusive with install flags, so it's
# invoked alone in a separate process).
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

# Keep Terminal window open after Finder launch so user can read output.
if [ -t 0 ] && [ "$status" -ne 0 ]; then
    read -n 1 -s -r -p "Press any key to close..." || true
    echo ""
fi

exit $status
