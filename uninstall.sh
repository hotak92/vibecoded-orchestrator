#!/usr/bin/env bash
# VibeCoded Tools — Orchestrator Uninstaller (shell wrapper)
#
# Delegates to `python install.py --uninstall`. Pass any of:
#   --keep-data         keep container volumes
#   --remove-projects   also remove .claude/ in registered projects
#   --dry-run           print plan and exit
#   --yes               non-interactive (accept all confirmations)
#
# By default the uninstaller is interactive and prompts before each
# destructive step. It NEVER touches ~/.vct-secrets/ and NEVER touches
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
