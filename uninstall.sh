#!/usr/bin/env bash
# VibeCoded Tools — Orchestrator Uninstaller (shell wrapper)
#
# Delegates to `python install.py --uninstall`. Pass any of:
#   --keep-data         keep the container volumes ONLY (KG vectors, models); step [8]
#                       still removes the state dir (RL training data, license.key)
#                       unless you decline it
#   --remove-projects   also remove .claude/ in registered projects
#   --dry-run           print plan and exit
#   --yes               non-interactive (accept all confirmations)
#
# By default the uninstaller is interactive and prompts before each
# destructive step. It stops the model gateway, vct-hub and the VCO launcher first, then
# removes the WHOLE VCO state dir (~/.vct or $VCT_STATE_DIR, RL training
# data included; a linked state dir, or a $VCT_STATE_DIR not named .vct,
# loses only VCO's own files). It NEVER touches ~/.vct-secrets/ and NEVER touches
# user source code outside orchestrator-managed paths.

set -euo pipefail

PYTHON=""
for cmd in python3.12 python3.11 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c 'import sys; sys.stdout.write("%d.%d" % (sys.version_info[0], sys.version_info[1]))' 2>/dev/null) || continue
        if [ -z "$version" ]; then continue; fi
        major=${version%%.*}
        minor=${version##*.}
        if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; }; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ required to run the uninstaller." >&2
    exit 1
fi

cd "$(dirname "$0")"
exec "$PYTHON" install.py --uninstall "$@"
