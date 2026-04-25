#!/usr/bin/env bash
set -euo pipefail

echo "=== VibeCoded Tools — Orchestrator Installer ==="
echo ""

# Find Python 3.11+
PYTHON=""
for cmd in python3.12 python3.11 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        # Use a Python 2/3-compatible probe (no f-strings).
        # Suppress stderr so set -e doesn't abort on Python 2 / broken interpreters.
        version=$("$cmd" -c 'import sys; sys.stdout.write("%d.%d" % (sys.version_info[0], sys.version_info[1]))' 2>/dev/null) || continue
        if [ -z "$version" ]; then continue; fi
        major=${version%%.*}
        minor=${version##*.}
        # Accept: major>3, OR major==3 AND minor>=11. Reject Python 2.x.
        if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; }; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ required." >&2
    echo "" >&2
    echo "Install Python:" >&2
    case "${OSTYPE:-}" in
        linux*)
            echo "  Ubuntu/Debian: sudo apt install python3.12 python3.12-venv python3-pip" >&2
            echo "  Fedora:        sudo dnf install python3.12" >&2
            echo "  Arch:          sudo pacman -S python" >&2
            ;;
        darwin*)
            echo "  macOS:         brew install python@3.12" >&2
            ;;
        msys*|cygwin*)
            echo "  Windows:       winget install Python.Python.3.12  (or use install.ps1)" >&2
            ;;
        *)
            echo "  Unknown OS:    install Python 3.11+ from https://python.org" >&2
            ;;
    esac
    echo "  Or download:   https://python.org" >&2
    exit 1
fi

echo "Using Python: $PYTHON ($("$PYTHON" --version))"

# Change to script directory
cd "$(dirname "$0")"

"$PYTHON" install.py "$@"
