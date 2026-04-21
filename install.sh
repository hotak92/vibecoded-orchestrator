#!/usr/bin/env bash
set -euo pipefail

echo "=== VibeCoded Tools — Orchestrator Installer ==="
echo ""

# Find Python 3.11+
PYTHON=""
for cmd in python3.12 python3.11 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ required."
    echo ""
    echo "Install Python:"
    if [[ "$OSTYPE" == "linux"* ]]; then
        echo "  Ubuntu/Debian: sudo apt install python3.12"
        echo "  Fedora:        sudo dnf install python3.12"
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        echo "  macOS:         brew install python@3.12"
    fi
    echo "  Or download:   https://python.org"
    exit 1
fi

echo "Using Python: $PYTHON ($("$PYTHON" --version))"

# Change to script directory
cd "$(dirname "$0")"

"$PYTHON" install.py "$@"
