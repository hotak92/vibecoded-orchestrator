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
#   3. v0.2.51 (Bug G): same pattern for Node.js (>= 18) and Podman.
#      Detection-only fail when they're truly absent + non-interactive;
#      interactive prompt + auto-install when stdin is a TTY. GPU drivers
#      stay manual (out of scope for this script).
#   4. Re-checks for everything after install, then exec's `python install.py`.
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
    #
    # v0.2.53 (Track G2 / L-P0-1): added zypper (openSUSE/SLES) + apk
    # (Alpine) branches to mirror post-install-launcher.sh:288. Without
    # these, SLES/Alpine users hit "No supported package manager found"
    # and bailed before ever reaching post-install-launcher.sh.
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
    elif command -v zypper &>/dev/null; then
        # openSUSE/SLES: python3 is the default meta-package; pip is a
        # separate package. Tumbleweed ships python311 / python312 as
        # explicit version packages — try the versioned name first.
        echo "Detected zypper (openSUSE/SLES). Will run:"
        echo "  sudo zypper install python312 python3-pip"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            # Try the versioned package first; fall back to plain python3
            # if zypper can't find it (Leap LTS / SLES 15 SP4 etc.).
            if zypper -n se -x python312 2>/dev/null | grep -q "^i\\|^v"; then
                sudo zypper install python312 python3-pip
            elif zypper -n se -x python311 2>/dev/null | grep -q "^i\\|^v"; then
                echo "  python312 not in repo; installing python311 (must be 3.11+)."
                sudo zypper install python311 python3-pip
            else
                echo "  versioned python not in repo; installing default python3 (must be 3.11+)."
                sudo zypper install python3 python3-pip
            fi
            return 0
        fi
    elif command -v apk &>/dev/null; then
        # Alpine: `python3` is the canonical name; py3-pip ships pip.
        # Alpine 3.19+ ships Python 3.11; Alpine 3.20+ ships 3.12.
        # Earlier releases are below the 3.11 floor and will fail the
        # post-install version gate — acceptable failure mode.
        echo "Detected apk (Alpine). Will run:"
        echo "  sudo apk add python3 py3-pip"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            sudo apk add python3 py3-pip
            return 0
        fi
    else
        echo "ERROR: No supported package manager found (apt/dnf/pacman/zypper/apk)." >&2
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
# v0.2.51 Bug G: Node.js detection + auto-install
#
# Node 18+ is needed for:
#   - The Playwright MCP (npx -y @playwright/mcp@latest)
#   - The bundled-npm pinning helpers (@anthropic-ai/claude-code, etc.)
#   - The Tauri launcher build path (npm during cargo tauri build)
#
# Detection: probe a few PATH candidates (node + version >= 18). fnm/nvm
# setups: install.py's _find_npx() handles the case where `npm` is on
# PATH but `npx` is only in the fnm bin dir; this script just verifies
# `node` exists since the version is what matters for the pre-flight gate.
#
# Auto-install via the same package manager we already use for Python:
# apt/dnf/pacman on Linux, brew on macOS. winget on Windows is handled
# by install.ps1.
# ---------------------------------------------------------------------------
find_node() {
    # Returns 0 + prints "node|<version>" if node >= 18 is on PATH;
    # returns 1 otherwise. We probe node directly rather than npm/npx
    # because the orchestrator's gating constraint is the Node runtime
    # version, not which front-ends ship alongside it.
    local cmd version major
    for cmd in node nodejs; do
        if command -v "$cmd" &>/dev/null; then
            # Strip leading 'v' from "v20.11.1" → "20.11.1"; tolerate broken
            # interpreters by suppressing stderr (set -e shouldn't abort).
            version=$("$cmd" --version 2>/dev/null | sed 's/^v//') || continue
            if [ -z "$version" ]; then continue; fi
            major=${version%%.*}
            # Accept Node >= 18. Older majors lack the fetch() / built-in
            # fs/promises shape that the bundled MCPs assume.
            if [ "$major" -ge 18 ] 2>/dev/null; then
                echo "$cmd|$version"
                return 0
            fi
        fi
    done
    return 1
}

print_node_manual_hint() {
    echo "" >&2
    echo "Install Node.js 18+ manually, then re-run ./install.sh:" >&2
    case "${OSTYPE:-}" in
        linux*)
            echo "  Ubuntu/Debian:    sudo apt install nodejs npm" >&2
            echo "  Fedora:           sudo dnf install nodejs npm" >&2
            echo "  Arch:             sudo pacman -S nodejs npm" >&2
            echo "  openSUSE/SLES:    sudo zypper install nodejs npm" >&2
            echo "  Alpine:           sudo apk add nodejs npm" >&2
            echo "  Or via fnm:       curl -fsSL https://fnm.vercel.app/install | bash" >&2
            ;;
        darwin*)
            echo "  macOS (brew):  brew install node" >&2
            echo "  Or download:   https://nodejs.org/" >&2
            ;;
        *)
            echo "  Download:      https://nodejs.org/" >&2
            ;;
    esac
}

attempt_install_node_linux() {
    # v0.2.53 (Track G2 / L-P0-1): added zypper + apk branches to mirror
    # post-install-launcher.sh:709-714 / 722-727.
    if command -v apt-get &>/dev/null; then
        echo "Detected apt (Debian/Ubuntu). Will run:"
        echo "  sudo apt-get install nodejs npm"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            # Note: Debian/Ubuntu's `nodejs` package is sometimes older than
            # 18 on long-LTS distros. The post-install re-probe will catch
            # that and surface the NodeSource hint.
            sudo apt-get install -y nodejs npm
            return 0
        fi
    elif command -v dnf &>/dev/null; then
        echo "Detected dnf (Fedora/RHEL). Will run:"
        echo "  sudo dnf install nodejs npm"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            sudo dnf install -y nodejs npm
            return 0
        fi
    elif command -v pacman &>/dev/null; then
        echo "Detected pacman (Arch). Will run:"
        echo "  sudo pacman -S nodejs npm"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            sudo pacman -S --noconfirm nodejs npm
            return 0
        fi
    elif command -v zypper &>/dev/null; then
        echo "Detected zypper (openSUSE/SLES). Will run:"
        echo "  sudo zypper install nodejs npm"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            # zypper -y: non-interactive. SLES/Leap ship nodejs (current
            # LTS) under the standard repo; Tumbleweed ships nodejs22.
            sudo zypper install -y nodejs npm
            return 0
        fi
    elif command -v apk &>/dev/null; then
        echo "Detected apk (Alpine). Will run:"
        echo "  sudo apk add nodejs npm"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            # Alpine 3.19+ ships Node 20; older releases ship Node 18.
            # `apk add` is non-interactive by default.
            sudo apk add --no-cache nodejs npm
            return 0
        fi
    else
        echo "ERROR: No supported package manager found (apt/dnf/pacman/zypper/apk)." >&2
        return 1
    fi
    return 1
}

attempt_install_node_macos() {
    # Re-probe brew the same way as attempt_install_macos (canonical
    # Homebrew prefixes); user may have installed brew between the
    # Python check and now.
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
        echo "       Install Homebrew first: https://brew.sh" >&2
        echo "       Then: brew install node" >&2
        return 1
    fi
    echo "Detected Homebrew. Will run:"
    echo "  brew install node"
    if prompt_yes "Proceed?"; then
        brew install node
        return 0
    fi
    return 1
}

attempt_install_node() {
    case "${OSTYPE:-}" in
        linux*)        attempt_install_node_linux ;;
        darwin*)       attempt_install_node_macos ;;
        msys*|cygwin*) echo "ERROR: Use install.ps1 on Windows." >&2; return 1 ;;
        *)             echo "ERROR: Unknown OS '${OSTYPE:-unknown}'; auto-install unsupported." >&2; return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# v0.2.51 Bug G: Podman detection + auto-install
#
# install.py also has Podman install logic (see _prompt_install_container_runtime).
# Why duplicate here: install.py runs AFTER Python is verified, but the
# user can have Podman missing at THIS layer too — surfacing it pre-Python
# saves a round-trip if the user wants to install both in the same sudo
# session. install.py's logic is the canonical fallback; this is opportunistic
# pre-flight.
#
# Daemon-start (systemctl --user start podman.socket / podman machine start)
# is install.py's responsibility — done after settings.json is written
# because the resolved storage paths may affect the rootless socket config.
# ---------------------------------------------------------------------------
find_podman() {
    # Detection-only: returns 0 if `podman` binary is on PATH, regardless
    # of whether the daemon/socket is currently responsive. install.py's
    # daemon-start step handles the running-state check.
    command -v podman &>/dev/null
}

find_container_runtime() {
    # Returns 0 if EITHER podman OR docker is on PATH (binary present).
    # Prefer podman per the project convention (no license, native on
    # Linux). Docker presence is acceptable — install.py will use it.
    find_podman && return 0
    command -v docker &>/dev/null
}

print_podman_manual_hint() {
    echo "" >&2
    echo "Install Podman manually, then re-run ./install.sh:" >&2
    case "${OSTYPE:-}" in
        linux*)
            echo "  Ubuntu/Debian:    sudo apt install podman" >&2
            echo "  Fedora:           sudo dnf install podman" >&2
            echo "  Arch:             sudo pacman -S podman" >&2
            echo "  openSUSE/SLES:    sudo zypper install podman" >&2
            echo "  Alpine:           sudo apk add podman" >&2
            ;;
        darwin*)
            echo "  macOS (brew):  brew install podman" >&2
            echo "  Then:          podman machine init && podman machine start" >&2
            ;;
        *)
            echo "  Download:      https://podman.io/getting-started/installation" >&2
            ;;
    esac
}

attempt_install_podman_linux() {
    # v0.2.53 (Track G2 / L-P0-1): added zypper + apk branches. Mirrors
    # install.py:_prompt_install_container_runtime's Linux ladder.
    if command -v apt-get &>/dev/null; then
        echo "Detected apt (Debian/Ubuntu). Will run:"
        echo "  sudo apt-get install podman"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            sudo apt-get install -y podman
            return 0
        fi
    elif command -v dnf &>/dev/null; then
        echo "Detected dnf (Fedora/RHEL). Will run:"
        echo "  sudo dnf install podman"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            sudo dnf install -y podman
            return 0
        fi
    elif command -v pacman &>/dev/null; then
        echo "Detected pacman (Arch). Will run:"
        echo "  sudo pacman -S podman"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            sudo pacman -S --noconfirm podman
            return 0
        fi
    elif command -v zypper &>/dev/null; then
        echo "Detected zypper (openSUSE/SLES). Will run:"
        echo "  sudo zypper install podman"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            sudo zypper install -y podman
            return 0
        fi
    elif command -v apk &>/dev/null; then
        echo "Detected apk (Alpine). Will run:"
        echo "  sudo apk add podman"
        if prompt_yes "Proceed? You'll be asked for your sudo password."; then
            # Alpine ships podman in the community repo. If community
            # isn't enabled, apk surfaces "unable to select packages:
            # podman" — acceptable failure mode.
            sudo apk add --no-cache podman
            return 0
        fi
    else
        echo "ERROR: No supported package manager found (apt/dnf/pacman/zypper/apk)." >&2
        return 1
    fi
    return 1
}

attempt_install_podman_macos() {
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
        echo "       Install Homebrew first: https://brew.sh" >&2
        echo "       Then: brew install podman" >&2
        return 1
    fi
    echo "Detected Homebrew. Will run:"
    echo "  brew install podman"
    if prompt_yes "Proceed?"; then
        brew install podman
        if [ $? -eq 0 ]; then
            # podman machine init + start are interactive and can take
            # 1-2 minutes (downloads a VM image). Defer to install.py
            # which has the deferral pattern + the platform-specific
            # daemon-start logic.
            echo ""
            echo "Podman installed. The macOS VM ('podman machine') will be"
            echo "initialized by install.py later in this run."
        fi
        return 0
    fi
    return 1
}

attempt_install_podman() {
    case "${OSTYPE:-}" in
        linux*)        attempt_install_podman_linux ;;
        darwin*)       attempt_install_podman_macos ;;
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

# ---------------------------------------------------------------------------
# v0.2.51 Bug G: Node.js + Podman pre-flight (best-effort).
#
# These are NOT install-blockers — install.py soft-fails when they're
# missing (Playwright skips, container setup gets a clear prompt). But
# if the user is at this prompt anyway, offering to install in the same
# sudo session is a strict UX win.
#
# Non-interactive mode (--yes / --quiet / CI / no TTY): skip silently
# and let install.py handle the downstream consequences.
# ---------------------------------------------------------------------------
if NODE_INFO=$(find_node); then
    NODE_VERSION="${NODE_INFO#*|}"
    echo "Found Node.js: $NODE_VERSION"
else
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        echo "Node.js 18+ not detected (non-interactive — skipping auto-install)."
        echo "  Playwright MCP + Tauri launcher build will be limited until installed."
    else
        echo "Node.js 18+ not detected."
        if prompt_yes "Install Node.js now? (Playwright MCP + launcher build need it)"; then
            if attempt_install_node; then
                echo "Re-checking for Node.js..."
                if NODE_INFO=$(find_node); then
                    NODE_VERSION="${NODE_INFO#*|}"
                    echo "Found Node.js: $NODE_VERSION"
                else
                    echo "WARN: Node.js install appeared to succeed but `node --version` still reports < 18 or not found." >&2
                    echo "      Open a new shell or update PATH; install.py will surface a deferral if needed." >&2
                    print_node_manual_hint
                fi
            else
                print_node_manual_hint
                echo "Continuing install — Node.js is non-blocking."
            fi
        else
            echo "Skipped — install.py will note missing Node.js in its summary."
        fi
    fi
fi

if find_container_runtime; then
    if find_podman; then
        echo "Found container runtime: podman ($(podman --version 2>/dev/null | head -1))"
    else
        echo "Found container runtime: docker ($(docker --version 2>/dev/null | head -1))"
    fi
else
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        echo "No container runtime detected (non-interactive — skipping auto-install)."
        echo "  install.py will surface a prompt later in this run."
    else
        echo "No container runtime (podman or docker) detected."
        if prompt_yes "Install Podman now? (recommended over Docker — no license, native)"; then
            if attempt_install_podman; then
                echo "Re-checking for podman..."
                if find_podman; then
                    echo "Found podman: $(podman --version 2>/dev/null | head -1)"
                else
                    echo "WARN: Podman install appeared to succeed but `podman` not on PATH." >&2
                    echo "      Open a new shell; install.py will re-probe + surface a deferral if needed." >&2
                    print_podman_manual_hint
                fi
            else
                print_podman_manual_hint
                echo "Continuing — install.py will prompt again if no runtime is present."
            fi
        else
            echo "Skipped — install.py will prompt again later."
        fi
    fi
fi

# Change to script directory
cd "$(dirname "$0")"

# Translate install.sh-only flags to ones install.py accepts.
# install.sh advertises --non-interactive in its own help (used to skip
# the Python auto-install prompt). Earlier versions forwarded the literal
# string to install.py, which argparse-rejected because install.py only
# knows --yes / --quiet. Translate before forwarding so the public flag
# surface stays consistent. (Reported 2026-05-06: bash first-install.sh
# --non-interactive failed with "unrecognized arguments: --non-interactive".)
INSTALL_PY_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --non-interactive) INSTALL_PY_ARGS+=("--yes") ;;
        *) INSTALL_PY_ARGS+=("$arg") ;;
    esac
done

exec "$PYTHON" install.py "${INSTALL_PY_ARGS[@]}"
