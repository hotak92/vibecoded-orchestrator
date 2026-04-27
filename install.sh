#!/usr/bin/env bash
set -euo pipefail

# VibeCoded Tools — Orchestrator Installer (Linux / macOS)
#
# This wrapper:
#   1. Detects an existing Python 3.11+ on PATH.
#   2. If absent, offers to install one (apt / dnf / pacman / brew).
#      Auto-install is INTERACTIVE: we prompt before invoking sudo or
#      brew; pass --non-interactive (or --quiet) to disable auto-install
#      and just fail with an install hint.
#   3. Re-checks for Python after install, then exec's `python install.py`.
#
# Why a shell wrapper instead of bootstrapping in Python: chicken-and-egg
# — install.py needs Python to run. We could ship a standalone bootstrap
# binary (Rust/Go) or use `uv` (Astral) to provision Python; both are
# tracked for v1.1. For v1.0 the lightest touch is a shell wrapper that
# leans on the system package manager.

echo "=== VibeCoded Tools — Orchestrator Installer ==="
echo ""

# Parse our own pre-flight flags (everything else is forwarded to install.py).
NON_INTERACTIVE=0
for arg in "$@"; do
    case "$arg" in
        --non-interactive|--quiet) NON_INTERACTIVE=1 ;;
    esac
done
# Honour CI-style env vars too.
if [ -n "${CI:-}" ] || [ -n "${VCT_NON_INTERACTIVE:-}" ]; then
    NON_INTERACTIVE=1
fi
# No TTY → non-interactive (can't prompt).
if [ ! -t 0 ]; then
    NON_INTERACTIVE=1
fi

# ---------------------------------------------------------------------------
# Python detection
# ---------------------------------------------------------------------------
find_python() {
    local cmd version major minor
    for cmd in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            # Python 2/3-compatible probe (no f-strings).
            # Suppress stderr so set -e doesn't abort on broken interpreters.
            version=$("$cmd" -c 'import sys; sys.stdout.write("%d.%d" % (sys.version_info[0], sys.version_info[1]))' 2>/dev/null) || continue
            if [ -z "$version" ]; then continue; fi
            major=${version%%.*}
            minor=${version##*.}
            # Accept: major>3, OR major==3 AND minor>=11. Reject Python 2.x.
            if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; }; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# Auto-install Python
# ---------------------------------------------------------------------------
print_manual_hint() {
    echo "" >&2
    echo "Install Python 3.11+ manually, then re-run ./install.sh:" >&2
    case "${OSTYPE:-}" in
        linux*)
            echo "  Ubuntu/Debian: sudo apt install python3.12 python3.12-venv python3-pip" >&2
            echo "  Fedora:        sudo dnf install python3.12" >&2
            echo "  Arch:          sudo pacman -S python python-pip" >&2
            ;;
        darwin*)
            echo "  macOS (brew):  brew install python@3.12" >&2
            echo "                 (Homebrew: https://brew.sh)" >&2
            echo "  macOS (download): https://www.python.org/downloads/" >&2
            ;;
        *)
            echo "  Download:      https://python.org/downloads/" >&2
            ;;
    esac
    echo "  Docs:          https://github.com/hotak92/vibecoded-orchestrator#prerequisites" >&2
}

prompt_yes() {
    # Returns 0 (yes) by default in interactive mode; 1 (no) in non-interactive.
    local question="$1"
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        return 1
    fi
    local reply
    read -r -p "$question [Y/n] " reply || return 1
    case "${reply:-Y}" in
        [Yy]*|"") return 0 ;;
        *) return 1 ;;
    esac
}

attempt_install_linux() {
    # Detect package manager. We deliberately DON'T pass -y; user must
    # confirm at the package manager prompt. We do print a heads-up.
    if command -v apt-get &>/dev/null; then
        echo "Detected apt (Debian/Ubuntu). Will run:"
        echo "  sudo apt-get update && sudo apt-get install python3.12 python3.12-venv python3-pip"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            sudo apt-get update
            # python3.12 may not be in older Ubuntu repos — fall back to python3 if it isn't.
            if apt-cache show python3.12 2>/dev/null | grep -q "^Package: python3.12"; then
                sudo apt-get install python3.12 python3.12-venv python3-pip
            else
                echo "  python3.12 not in repo; installing default python3 (must be 3.11+)."
                sudo apt-get install python3 python3-venv python3-pip
            fi
            return 0
        fi
    elif command -v dnf &>/dev/null; then
        echo "Detected dnf (Fedora/RHEL). Will run:"
        echo "  sudo dnf install python3.12"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            sudo dnf install python3.12
            return 0
        fi
    elif command -v pacman &>/dev/null; then
        echo "Detected pacman (Arch). Will run:"
        echo "  sudo pacman -S python python-pip"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            sudo pacman -S python python-pip
            return 0
        fi
    else
        echo "ERROR: No supported package manager found (apt/dnf/pacman)." >&2
        return 1
    fi
    # User declined.
    return 1
}

attempt_install_macos() {
    # We never auto-install Homebrew itself: the official installer is
    # interactive (asks for sudo password, may need to install Xcode CLT)
    # and bootstrapping a package manager from a wrapper script is the
    # kind of side-effect we don't want to silently do for the user.
    #
    # Homebrew PATH gotcha: a freshly installed brew may not be on PATH
    # in this shell yet. Canonical detection (per Homebrew's Tips and
    # Tricks: https://docs.brew.sh/Tips-and-Tricks):
    #   - Apple Silicon: /opt/homebrew/bin/brew
    #   - Intel:         /usr/local/bin/brew
    #   - Linuxbrew:     /home/linuxbrew/.linuxbrew/bin/brew
    # We probe those locations and if found, source `brew shellenv` so
    # subsequent `brew install` calls resolve.
    if ! command -v brew >/dev/null 2>&1; then
        for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew /home/linuxbrew/.linuxbrew/bin/brew; do
            if [ -x "$candidate" ]; then
                eval "$("$candidate" shellenv)"
                break
            fi
        done
    fi
    if ! command -v brew &>/dev/null; then
        echo "ERROR: Homebrew not found." >&2
        echo "" >&2
        echo "       Two ways to get Python 3.11+ on macOS:" >&2
        echo "" >&2
        echo "       1) Install Homebrew, then re-run this script:" >&2
        echo "            https://brew.sh" >&2
        echo "          Once installed: brew install python@3.12" >&2
        echo "" >&2
        echo "       2) Or download the official installer:" >&2
        echo "            https://www.python.org/downloads/" >&2
        echo "" >&2
        echo "       Then re-run ./install.sh." >&2
        return 1
    fi
    echo "Detected Homebrew. Will run:"
    echo "  brew install python@3.12"
    if prompt_yes "Proceed?"; then
        # `brew install` does not require sudo for Homebrew-managed
        # prefixes (/opt/homebrew on Apple Silicon, /usr/local on Intel).
        # Run it uninteractively — no extra prompt needed.
        brew install python@3.12
        return 0
    fi
    return 1
}

attempt_install_python() {
    case "${OSTYPE:-}" in
        linux*)        attempt_install_linux ;;
        darwin*)       attempt_install_macos ;;
        msys*|cygwin*) echo "ERROR: Use install.ps1 on Windows." >&2; return 1 ;;
        *)             echo "ERROR: Unknown OS '${OSTYPE:-unknown}'; auto-install unsupported." >&2; return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
PYTHON=$(find_python || true)

if [ -z "$PYTHON" ]; then
    echo "Python 3.11+ not found on PATH."

    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        echo "ERROR: non-interactive mode — refusing to auto-install Python." >&2
        print_manual_hint
        exit 1
    fi

    echo ""
    echo "vibecoded-orchestrator requires Python 3.11 or newer."
    echo ""
    if attempt_install_python; then
        echo ""
        echo "Re-checking for Python..."
        PYTHON=$(find_python || true)
        if [ -z "$PYTHON" ]; then
            echo "ERROR: Python install appeared to succeed but no 3.11+ interpreter is on PATH." >&2
            echo "       You may need to open a new shell or update PATH." >&2
            print_manual_hint
            exit 1
        fi
    else
        print_manual_hint
        exit 1
    fi
fi

echo "Using Python: $PYTHON ($("$PYTHON" --version))"

# Change to script directory
cd "$(dirname "$0")"

exec "$PYTHON" install.py "$@"
