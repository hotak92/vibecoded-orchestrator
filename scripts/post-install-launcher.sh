#!/usr/bin/env bash
# post-install-launcher.sh — Ensure a working launcher GUI is ready after install.
#
# Called by first-install.sh (Linux) and first-install.command (macOS) after
# install.py completes. NOT called from first-install.bat — Windows uses its
# own equivalent inline (CMD batch is a different language; sharing helpers
# across cmd / bash is not worth the contortion).
#
# Exit contract: ALWAYS exits 0. Failure to find / download / build / launch
# the GUI is non-fatal — the user can run start-launcher.<ext> manually.
# Hard rule from the spec: "auto-launch should NEVER block first-install
# from exiting successfully."
#
# Pre-installed-tool assumptions (CANONICAL CONSTRAINT FROM USER):
#   "make sure nothing depends on assuming something being installed on
#    target PC". So we audit every tool we invoke:
#     bash       — POSIX guarantee on Linux + macOS, OK
#     curl       — NOT guaranteed on minimal Linux (Alpine, NixOS minbase);
#                  ALWAYS guaranteed on macOS. We probe for wget as fallback.
#     wget       — Linux-only typically; never on macOS. Curl fallback only.
#     python3    — Already required by install.py — guaranteed at this stage
#                  since this script runs AFTER install.py. We use it for
#                  JSON parsing of the GitHub Releases API response.
#     hdiutil    — macOS-only, ships with macOS.
#     sudo       — NOT guaranteed (root-only containers, Termux). We try
#                  without sudo first; only escalate when we know we need to
#                  and sudo exists.
#     apt/dnf/   — Linux package managers. Try whichever exists; gracefully
#       pacman      skip if none match.
#     brew       — NOT pre-installed on macOS. Detect-only; print install
#                  URL if absent.
#     node/npm/  — NEVER pre-installed. Auto-install path tries OS package
#       pnpm        manager; falls back to a clear manual-install message.
#
# Flow:
#   1. _check_prerequisites — run all detection checks upfront.
#   2. Probe candidate binary paths (release, dev, system installs).
#   3. If absent → ask user [download / build / skip] (default: download,
#      auto-default with --yes / non-TTY).
#   4a. Download path: download from GitHub Releases via curl OR wget.
#       Fall back to build path if no release asset is published yet.
#   4b. Build path: detect Node + pnpm, install if missing, install Tauri
#       Linux deps if on Linux (best-effort, sudo-only-if-available),
#       then `pnpm tauri build` (or `npm run tauri build`).
#   5. On success, spawn the launcher detached (unless --no-auto-launch).
#
# Usage:
#   post-install-launcher.sh <repo_root> [--yes] [--no-auto-launch]

set -uo pipefail
# NOTE: deliberately NOT `set -e`. We want to swallow failures and exit 0
# rather than have a single curl/build hiccup abort first-install.

# We do NOT `unset BASH_ENV` — leave user-defined output-compression shims
# (lean-ctx, etc.) active during long phases like `pnpm install`/`tauri
# build` where their progress chatter is what users actually want to see
# compressed. The audit-vs-execution mismatch that bit us on 2026-04-27
# is handled at detection time by `_resolves_to_binary` below — we only
# accept tools that resolve to a real PATH binary, never function or
# builtin shadows. That way false-positive detection (audit says "yes"
# but the binary doesn't exist) is impossible.

REPO_ROOT="${1:-}"
shift || true

# Durable install log written by both install.py and this script. Both
# the launcher and Claude Code read this on failure to figure out where
# the install got to. JSONL: one event per line, never PII. See
# docs/INSTALL_RECOVERY.md for the full schema.
_install_log_path() {
    if [ -n "${REPO_ROOT:-}" ] && [ -d "$REPO_ROOT/state/logs" ]; then
        printf '%s\n' "$REPO_ROOT/state/logs/install.jsonl"
    fi
}

_log_event() {
    # _log_event <step> <phase> <detail>
    local step="${1:-?}"
    local phase="${2:-?}"
    local detail="${3:-}"
    local path
    path="$(_install_log_path)"
    [ -z "$path" ] && return 0
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # Escape minimal: backslash, double-quote, control chars not handled.
    # Detail strings are bounded (we always pass short literals).
    local esc_detail
    esc_detail="$(printf '%s' "$detail" | sed 's/\\/\\\\/g; s/"/\\"/g')"
    printf '{"ts":"%s","actor":"post-install-launcher.sh","step":"%s","phase":"%s","detail":"%s"}\n' \
        "$ts" "$step" "$phase" "$esc_detail" >> "$path" 2>/dev/null || true
}

YES=0
AUTO_LAUNCH=1
# Env knob: VCT_NO_AUTO_LAUNCH=1 has the same effect as --no-auto-launch.
# Useful for unattended / agent / CI runs that need to control launcher
# spawning out-of-band (e.g. spawning under Xvfb, or skipping spawn
# entirely for a Playwright-driven E2E that controls the GUI itself).
# An autonomous agent inadvertently triggered the launcher's runtime-
# missing modal during a 2026-04-27 install test, which is what motivated
# this env-level escape hatch (the CLI flag was already there).
if [ "${VCT_NO_AUTO_LAUNCH:-0}" = "1" ]; then
    AUTO_LAUNCH=0
fi
for arg in "$@"; do
    case "$arg" in
        --yes|--non-interactive|--quiet) YES=1 ;;
        --no-auto-launch) AUTO_LAUNCH=0 ;;
    esac
done

# Non-TTY (CI, piped) is implicitly --yes for prompts.
if [ ! -t 0 ]; then
    YES=1
fi

if [ -z "$REPO_ROOT" ] || [ ! -d "$REPO_ROOT" ]; then
    echo "post-install-launcher: invalid repo root '$REPO_ROOT'" >&2
    exit 0  # non-fatal
fi

# OS detection. macOS reports darwin; Linux reports linux-gnu / linux-musl.
case "${OSTYPE:-}" in
    darwin*) OS="macos" ;;
    linux*)  OS="linux" ;;
    *)       OS="unknown" ;;
esac

# ----- _check_prerequisites — upfront capability audit -----------------------
# Reports which tools are present and which paths will be taken. This makes
# debugging trivial when something downstream surprises the user. Print-only;
# never blocks.
HAS_CURL=0
HAS_WGET=0
HAS_PYTHON3=0
HAS_NODE=0
HAS_PNPM=0
HAS_NPM=0
HAS_BREW=0
HAS_SUDO=0
HAS_HDIUTIL=0
PKGMGR=""

_check_prerequisites() {
    command -v curl    >/dev/null 2>&1 && HAS_CURL=1
    command -v wget    >/dev/null 2>&1 && HAS_WGET=1
    command -v python3 >/dev/null 2>&1 && HAS_PYTHON3=1
    # Use `command -v <tool>` AND require it to resolve to a real PATH
    # binary, not a shell function or alias. Some users have `pnpm` defined
    # as a corepack-style bash function in their interactive rc files; that
    # shadows the binary and is invisible to non-interactive subshells we
    # spawn for `pnpm install` later. Without this gate we set HAS_PNPM=1
    # then fail later with "pnpm install: command not found". Reported on
    # 2026-04-27 from a real install test.
    _resolves_to_binary() {
        local resolved
        resolved="$(command -v "$1" 2>/dev/null)"
        # `command -v` prints the function name verbatim for functions/
        # builtins; for binaries it prints an absolute path. Require the
        # latter and require the file to exist + be executable.
        case "$resolved" in
            /*) [ -x "$resolved" ] ;;
            *) return 1 ;;
        esac
    }
    # When `command -v` finds only a function/alias (not a real binary),
    # probe known-binary locations and prepend the first match to PATH so
    # the function wrapper is bypassed for our subsequent calls. Covers
    # users with fnm/nvm/lean-ctx wrappers around node/npm that point at
    # genuinely-installed binaries the wrapper just can't be invoked from
    # a non-interactive subshell context.
    _ensure_path_for_tool() {
        # Locate a real binary for $tool and put its directory(ies) on
        # PATH. Returns 0 if a binary is found (possibly after PATH
        # munging), 1 if not. Critically: also UNSETS any shell function
        # shadowing the tool name so subsequent direct invocations
        # actually run the binary, not the wrapper. Functions take
        # precedence over PATH in bash, so a wrapped tool stays wrapped
        # even after we add the bin/ to PATH.
        #
        # When the candidate is a symlink (typical for fnm/nvm/voltaa
        # which symlink ~/.local/bin/<tool> -> <real_install>/bin/<tool>),
        # we also add the symlink target's parent bin/ to PATH. Reason:
        # `npx` lives next to `npm` in fnm's real bin/ but is NOT
        # symlinked into ~/.local/bin/. Without this we'd find `npm` but
        # later fail at `npx tauri build` because npx isn't reachable.
        local tool="$1"; shift
        if _resolves_to_binary "$tool"; then
            return 0
        fi
        local cand
        for cand in "$@"; do
            if [ -x "$cand" ]; then
                local cand_dir
                cand_dir="$(dirname "$cand")"
                # Resolve a single symlink hop to find the real install
                # bin/ — so siblings (npx, corepack, node) are picked up
                # too. Don't `readlink -f`: that follows ALL hops
                # including ones into node_modules/npm/bin/npm-cli.js
                # whose dirname is wrong.
                local hop_target
                hop_target="$(readlink "$cand" 2>/dev/null || true)"
                local hop_dir=""
                if [ -n "$hop_target" ]; then
                    case "$hop_target" in
                        /*) hop_dir="$(dirname "$hop_target")" ;;
                        *)  hop_dir="$(cd "$cand_dir" && cd "$(dirname "$hop_target")" && pwd 2>/dev/null || true)" ;;
                    esac
                fi
                local d
                # Add candidate dir AND symlink target dir (in that order)
                # so the front of PATH always has the original symlink dir
                # (so the user's preferred wrapper-free entry wins) but
                # siblings via the resolved dir are still findable.
                for d in "$cand_dir" "$hop_dir"; do
                    [ -z "$d" ] && continue
                    [ ! -d "$d" ] && continue
                    case ":$PATH:" in
                        *":$d:"*) ;;
                        *) export PATH="$d:$PATH" ;;
                    esac
                done
                unset -f "$tool" 2>/dev/null || true
                if _resolves_to_binary "$tool"; then
                    return 0
                fi
            fi
        done
        # Last attempt: even if no candidate path was given, the tool
        # may already be reachable via PATH but masked by a function.
        if unset -f "$tool" 2>/dev/null && _resolves_to_binary "$tool"; then
            return 0
        fi
        return 1
    }
    _ensure_path_for_tool node \
        "$HOME/.local/bin/node" \
        "$HOME/.fnm/aliases/default/bin/node" \
        "$HOME/.nvm/versions/node/*/bin/node" \
        "/usr/local/bin/node" \
        "/usr/bin/node" \
        && HAS_NODE=1
    _ensure_path_for_tool npm \
        "$HOME/.local/bin/npm" \
        "$HOME/.fnm/aliases/default/bin/npm" \
        "$HOME/.nvm/versions/node/*/bin/npm" \
        "/usr/local/bin/npm" \
        "/usr/bin/npm" \
        && HAS_NPM=1
    _ensure_path_for_tool pnpm \
        "$HOME/.local/bin/pnpm" \
        "$HOME/.local/share/pnpm/pnpm" \
        "/usr/local/bin/pnpm" \
        "/usr/bin/pnpm" \
        && HAS_PNPM=1
    command -v brew    >/dev/null 2>&1 && HAS_BREW=1
    command -v sudo    >/dev/null 2>&1 && HAS_SUDO=1
    command -v hdiutil >/dev/null 2>&1 && HAS_HDIUTIL=1

    if [ "$OS" = "linux" ]; then
        for cmd in apt-get apt dnf pacman zypper apk; do
            if command -v "$cmd" >/dev/null 2>&1; then
                PKGMGR="$cmd"
                break
            fi
        done
    fi

    echo "[launcher] Prerequisites audit:"
    echo "  OS:        $OS"
    echo "  curl:      $([ $HAS_CURL -eq 1 ] && echo yes || echo no)"
    echo "  wget:      $([ $HAS_WGET -eq 1 ] && echo yes || echo no)"
    echo "  python3:   $([ $HAS_PYTHON3 -eq 1 ] && echo yes || echo no)"
    echo "  node:      $([ $HAS_NODE -eq 1 ] && echo yes || echo no)"
    echo "  pnpm:      $([ $HAS_PNPM -eq 1 ] && echo yes || echo no)"
    echo "  npm:       $([ $HAS_NPM -eq 1 ] && echo yes || echo no)"
    if [ "$OS" = "macos" ]; then
        echo "  brew:      $([ $HAS_BREW -eq 1 ] && echo yes || echo no)"
        echo "  hdiutil:   $([ $HAS_HDIUTIL -eq 1 ] && echo yes || echo no)"
    fi
    if [ "$OS" = "linux" ]; then
        echo "  pkg-mgr:   ${PKGMGR:-none-detected}"
    fi
    echo "  sudo:      $([ $HAS_SUDO -eq 1 ] && echo yes || echo no)"

    if [ $HAS_CURL -eq 0 ] && [ $HAS_WGET -eq 0 ]; then
        echo "[launcher] WARNING: neither curl nor wget present. Download path will be unavailable."
    fi
}

# Wrapper for sudo: only escalate if sudo is actually available; otherwise
# try without (will fail on locked-down systems but at least we tried).
_maybe_sudo() {
    if [ "$(id -u)" -eq 0 ] || [ $HAS_SUDO -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

# Cross-tool downloader: prefers curl, falls back to wget. Returns nonzero
# only if BOTH are absent OR the actual download failed.
_download() {
    local url="$1"
    local dest="$2"
    if [ $HAS_CURL -eq 1 ]; then
        curl -fL --progress-bar "$url" -o "$dest"
    elif [ $HAS_WGET -eq 1 ]; then
        wget --show-progress -qO "$dest" "$url"
    else
        return 127
    fi
}

# Cross-tool fetcher (in-memory): prefers curl, falls back to wget. Used
# for JSON API probes where a temp file would be wasteful.
_fetch() {
    local url="$1"
    if [ $HAS_CURL -eq 1 ]; then
        curl -fsSL "$url"
    elif [ $HAS_WGET -eq 1 ]; then
        wget -qO- "$url"
    else
        return 127
    fi
}

_check_prerequisites
echo ""

# ----- Step 2: probe for an existing launcher binary --------------------------
candidates_unix=(
    "$REPO_ROOT/launcher/src-tauri/target/release/vct-launcher"
    "$REPO_ROOT/launcher/src-tauri/target/release/vct-launcher-temp"
    "$REPO_ROOT/launcher/src-tauri/target/release/launcher"
    "$REPO_ROOT/launcher/src-tauri/target/debug/vct-launcher-temp"
    "$HOME/.local/share/vct-launcher/vct-launcher"
    "$HOME/.local/bin/vct-launcher"
    "/usr/bin/vct-launcher"
    "/usr/local/bin/vct-launcher"
)
candidates_mac=(
    "/Applications/VCT Launcher.app/Contents/MacOS/VCT Launcher"
    "/Applications/VCT Launcher.app/Contents/MacOS/vct-launcher"
    "$HOME/Applications/VCT Launcher.app/Contents/MacOS/VCT Launcher"
    "$HOME/Applications/VCT Launcher.app/Contents/MacOS/vct-launcher"
)

find_binary() {
    for c in "${candidates_unix[@]}"; do
        [ -x "$c" ] && { echo "$c"; return 0; }
    done
    if [ "$OS" = "macos" ]; then
        for c in "${candidates_mac[@]}"; do
            [ -x "$c" ] && { echo "$c"; return 0; }
        done
    fi
    return 1
}

LAUNCHER_BIN="$(find_binary || true)"

if [ -n "$LAUNCHER_BIN" ]; then
    echo "[launcher] Found existing binary: $LAUNCHER_BIN"
else
    # ----- Step 3: prompt for path --------------------------------------------
    echo "==============================================="
    echo "  Launcher binary not found. Choose how to get it:"
    echo "==============================================="
    echo "  [1] Download prebuilt (recommended) — fast, ~30 MB"
    echo "       (downloads from latest GitHub Release)"
    echo "  [2] Build from source — slower, requires Node + Tauri toolchain"
    echo "       (auto-installs Node if missing, then runs 'pnpm tauri build')"
    echo "  [3] Skip (build later manually)"
    echo ""

    if [ "$YES" -eq 1 ]; then
        choice=1
        echo "[launcher] --yes / non-interactive: defaulting to [1] download"
    else
        read -r -p "Your choice [1]: " choice || choice=1
        choice="${choice:-1}"
    fi

    case "$choice" in
        2) MODE="build"  ;;
        3) MODE="skip"   ;;
        *) MODE="download" ;;
    esac

    if [ "$MODE" = "skip" ]; then
        echo "[launcher] Skipped. Run 'pnpm tauri build' in launcher/ later, then start-launcher.sh."
        exit 0
    fi

    # ----- Step 4a: download prebuilt -----------------------------------------
    if [ "$MODE" = "download" ]; then
        # Download requires curl OR wget. If neither, fall back to build.
        if [ $HAS_CURL -eq 0 ] && [ $HAS_WGET -eq 0 ]; then
            echo "[launcher] No curl or wget — cannot download. Falling back to build."
            MODE="build"
        elif [ $HAS_PYTHON3 -eq 0 ]; then
            echo "[launcher] No python3 — cannot parse GitHub API response. Falling back to build."
            MODE="build"
        fi
    fi

    if [ "$MODE" = "download" ]; then
        # Resolve OS-specific URL. Names match release.yml workflow output:
        #   Linux:   *.AppImage and *.deb under bundle/appimage/ + bundle/deb/
        #            (Tauri-generated names; we glob the API response)
        #   macOS:   vct-launcher-macos-${arch}.dmg
        #   Windows: vct-launcher-windows-x64.exe (handled in .bat, not here)
        echo ""
        echo "[launcher] Probing GitHub Releases..."

        api_url="https://api.github.com/repos/hotak92/vibecoded-orchestrator/releases/latest"
        release_json="$(_fetch "$api_url" 2>/dev/null || true)"

        asset_url=""
        asset_name=""
        if [ -n "$release_json" ]; then
            # Pick the right asset for this OS. We prefer AppImage on Linux
            # (portable, no apt/dnf coupling) and the dmg for macOS arm64.
            asset_info="$(printf '%s' "$release_json" | python3 - "$OS" <<'PY'
import json, sys
os_name = sys.argv[1]
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
assets = d.get("assets", []) or []
arch = ""
try:
    import platform
    arch = platform.machine().lower()
except Exception:
    pass

def pick(predicate):
    for a in assets:
        n = (a.get("name") or "").lower()
        if predicate(n):
            return a
    return None

picked = None
if os_name == "linux":
    picked = pick(lambda n: n.endswith(".appimage"))
elif os_name == "macos":
    if arch in ("arm64", "aarch64"):
        picked = pick(lambda n: n.endswith(".dmg") and ("arm64" in n or "aarch64" in n))
    if picked is None:
        picked = pick(lambda n: n.endswith(".dmg"))

if picked:
    print(picked.get("browser_download_url", ""))
    print(picked.get("name", ""))
PY
)"
            asset_url="$(printf '%s' "$asset_info" | sed -n '1p')"
            asset_name="$(printf '%s' "$asset_info" | sed -n '2p')"
        fi

        if [ -z "$asset_url" ]; then
            echo "[launcher] No prebuilt available yet for $OS. Falling back to build."
            MODE="build"
        else
            echo "[launcher] Downloading: $asset_name"
            if [ "$OS" = "linux" ]; then
                target_dir="$HOME/.local/share/vct-launcher"
                mkdir -p "$target_dir"
                target_path="$target_dir/vct-launcher"
                if _download "$asset_url" "$target_path"; then
                    chmod +x "$target_path"
                    sz=$(stat -c%s "$target_path" 2>/dev/null || stat -f%z "$target_path" 2>/dev/null || echo 0)
                    if [ "${sz:-0}" -gt 10485760 ]; then
                        LAUNCHER_BIN="$target_path"
                        echo "[launcher] Downloaded to $target_path ($((sz / 1048576)) MB)"
                    else
                        echo "[launcher] Downloaded file looks too small ($sz bytes). Falling back to build."
                        rm -f "$target_path"
                        MODE="build"
                    fi
                else
                    echo "[launcher] Download failed. Falling back to build."
                    MODE="build"
                fi
            elif [ "$OS" = "macos" ]; then
                if [ $HAS_HDIUTIL -eq 0 ]; then
                    echo "[launcher] hdiutil missing (unexpected on macOS). Falling back to build."
                    MODE="build"
                else
                    tmp_dmg="/tmp/vct-launcher-$$.dmg"
                    if _download "$asset_url" "$tmp_dmg"; then
                        mount_point="$(hdiutil attach -nobrowse -quiet "$tmp_dmg" 2>/dev/null \
                            | awk '/\/Volumes\// {for (i=3;i<=NF;i++) printf "%s%s", $i, (i<NF?" ":""); print ""}' \
                            | tail -1)"
                        if [ -n "$mount_point" ] && [ -d "$mount_point" ]; then
                            # Try /Applications first; fall back to ~/Applications if no admin.
                            app_src="$(find "$mount_point" -maxdepth 2 -name '*.app' -type d | head -1)"
                            if [ -n "$app_src" ]; then
                                if cp -R "$app_src" /Applications/ 2>/dev/null; then
                                    dest_app="/Applications/$(basename "$app_src")"
                                else
                                    mkdir -p "$HOME/Applications"
                                    cp -R "$app_src" "$HOME/Applications/" 2>/dev/null
                                    dest_app="$HOME/Applications/$(basename "$app_src")"
                                fi
                                for cand in "$dest_app/Contents/MacOS/VCT Launcher" \
                                            "$dest_app/Contents/MacOS/vct-launcher" \
                                            "$dest_app/Contents/MacOS/vct-launcher-temp"; do
                                    if [ -x "$cand" ]; then LAUNCHER_BIN="$cand"; break; fi
                                done
                            fi
                            hdiutil detach "$mount_point" -quiet 2>/dev/null || true
                        fi
                        rm -f "$tmp_dmg"
                        if [ -z "$LAUNCHER_BIN" ]; then
                            echo "[launcher] DMG mount/copy failed. Falling back to build."
                            MODE="build"
                        fi
                    else
                        echo "[launcher] Download failed. Falling back to build."
                        MODE="build"
                    fi
                fi
            fi
        fi
    fi

    # ----- Step 4b: build from source -----------------------------------------
    if [ "$MODE" = "build" ]; then
        echo ""
        echo "[launcher] Building from source..."

        # 4b.1: ensure Node.js is installed using the user-mandated tier ladder:
        #   T1 silent auto-install (only if --yes was passed AND we have sudo
        #     + matching package manager)
        #   T2 GUI elevation — NOT viable from a bash entry-point; skip
        #   T3 user approval via bash prompt — `read -p` then run install
        #   T4 print URL + fall through to skip
        # macOS gets a Homebrew shortcut: brew install is silent-OK without
        # sudo, so it counts as T1 unconditionally if brew is present.
        if [ $HAS_NODE -eq 0 ]; then
            installed_node=0
            case "$OS" in
                linux)
                    # T1: silent only if --yes AND we can elevate AND pkgmgr known.
                    if [ "$YES" -eq 1 ] && [ -n "$PKGMGR" ]; then
                        echo "[launcher] T1 silent: installing Node via $PKGMGR (--yes)"
                        case "$PKGMGR" in
                            apt|apt-get) _maybe_sudo apt-get update && _maybe_sudo apt-get install -y nodejs npm && installed_node=1 ;;
                            dnf)         _maybe_sudo dnf install -y nodejs npm && installed_node=1 ;;
                            pacman)      _maybe_sudo pacman -S --noconfirm nodejs npm && installed_node=1 ;;
                            zypper)      _maybe_sudo zypper install -y nodejs npm && installed_node=1 ;;
                            apk)         _maybe_sudo apk add --no-cache nodejs npm && installed_node=1 ;;
                        esac
                    fi
                    # T3: terminal prompt (only if we didn't already install and have a pkgmgr).
                    if [ $installed_node -eq 0 ] && [ -t 0 ] && [ "$YES" -eq 0 ] && [ -n "$PKGMGR" ]; then
                        read -r -p "Install Node.js via $PKGMGR? [Y/n] " ans || ans="Y"
                        case "${ans:-Y}" in
                            ""|y|Y|yes|YES)
                                case "$PKGMGR" in
                                    apt|apt-get) _maybe_sudo apt-get update && _maybe_sudo apt-get install nodejs npm && installed_node=1 ;;
                                    dnf)         _maybe_sudo dnf install nodejs npm && installed_node=1 ;;
                                    pacman)      _maybe_sudo pacman -S nodejs npm && installed_node=1 ;;
                                    zypper)      _maybe_sudo zypper install nodejs npm && installed_node=1 ;;
                                    apk)         _maybe_sudo apk add nodejs npm && installed_node=1 ;;
                                esac
                                ;;
                        esac
                    fi
                    ;;
                macos)
                    # T1: brew install is silent-OK on macOS (no sudo).
                    if [ $HAS_BREW -eq 1 ]; then
                        echo "[launcher] T1 silent: brew install node"
                        brew install node && installed_node=1
                    fi
                    ;;
            esac

            # T4: URL fallback if all tiers above failed.
            if [ $installed_node -eq 0 ]; then
                echo "[launcher] T4: install Node manually from https://nodejs.org/"
                if [ "$OS" = "macos" ] && [ $HAS_BREW -eq 0 ]; then
                    echo "           Or install Homebrew first: https://brew.sh"
                fi
            fi

            # Same binary-only detection as the initial audit.
            _resolves_to_binary node && HAS_NODE=1
            _resolves_to_binary npm  && HAS_NPM=1
        fi

        if [ $HAS_NODE -eq 0 ]; then
            # All auto-install tiers failed and Node is still missing.
            # Stop here and ask the user to install it manually rather
            # than silently skipping the launcher build (which leaves
            # the user with an "Installation complete!" message they
            # can't actually use). 2026-04-27 review: silent-skip is
            # the same anti-pattern as the Joern hang — install MUST
            # surface blockers loudly with actionable guidance.
            echo ""
            echo "==============================================="
            echo "  Cannot build the launcher: Node.js is missing"
            echo "==============================================="
            echo ""
            echo "  Auto-install attempts failed. Please install Node.js manually:"
            echo "    https://nodejs.org/ (LTS, 18+ recommended)"
            case "$OS" in
                linux)
                    echo "    Or via your package manager:"
                    echo "      sudo apt install nodejs npm        # Debian/Ubuntu"
                    echo "      sudo dnf install nodejs npm        # Fedora/RHEL"
                    echo "      sudo pacman -S nodejs npm          # Arch"
                    ;;
                macos)
                    echo "    Or via Homebrew:  brew install node"
                    echo "    (Install Homebrew first if needed: https://brew.sh)"
                    ;;
            esac
            echo ""
            echo "  After installing Node, choose:"
            echo "    [r] Re-check (I just installed it)"
            echo "    [s] Skip the launcher build (run later: cd launcher && pnpm install && pnpm tauri build)"
            echo ""
            if [ "$YES" -eq 1 ] || [ ! -t 0 ]; then
                echo "[launcher] Non-interactive run: skipping. Re-run the installer with Node available, or build manually."
                MODE="skip"
            else
                while :; do
                    read -r -p "Your choice [r/s]: " ans || ans="s"
                    case "${ans:-s}" in
                        r|R|recheck|RECHECK)
                            _resolves_to_binary node && HAS_NODE=1
                            _resolves_to_binary npm  && HAS_NPM=1
                            if [ $HAS_NODE -eq 1 ]; then
                                echo "[launcher] Detected Node.js — continuing build."
                                break
                            else
                                echo "[launcher] Still no Node.js on PATH. Try again or skip."
                            fi
                            ;;
                        s|S|skip|SKIP)
                            MODE="skip"
                            break
                            ;;
                        *)
                            echo "Type 'r' to re-check or 's' to skip."
                            ;;
                    esac
                done
            fi
        fi
    fi

    if [ "$MODE" = "build" ]; then
        # 4b.2: ensure pnpm (preferred) or fall back to npm.
        PKG_MGR=""
        if [ $HAS_PNPM -eq 1 ]; then
            PKG_MGR="pnpm"
        elif [ $HAS_NPM -eq 1 ]; then
            echo "[launcher] pnpm not found. Installing via npm..."
            # Try without sudo first — fnm/nvm/voltaa users have a
            # writable npm prefix, so sudo isn't needed and would
            # actually break (fnm install dir is owned by user).
            # Fall back to sudo only if the unprivileged attempt fails.
            # Stream output so we can see WHY it failed (was 2>/dev/null
            # which masked auth failures).
            if npm install -g pnpm; then
                : # success
            elif [ "$HAS_SUDO" -eq 1 ]; then
                echo "[launcher] unprivileged npm install -g failed; retrying with sudo..."
                _maybe_sudo npm install -g pnpm || true
            fi
            # `npm install -g pnpm` may put the binary in
            # `~/.local/share/npm/bin/`, `~/.npm-global/bin/`, or
            # `/usr/local/lib/node_modules/.bin/`, depending on the npm
            # prefix config. Probe the canonical npm-prefix-bin
            # locations so we find a freshly-installed pnpm even if PATH
            # hasn't been refreshed yet, then prepend that dir to PATH
            # so the subsequent `pnpm install` invocation works.
            if _resolves_to_binary pnpm; then
                PKG_MGR="pnpm"
            else
                # Probe npm's GLOBAL prefix to find where it installed
                # pnpm. Prefer `npm prefix -g` (canonical, works on
                # npm 7+); fall back to `npm config get prefix` and
                # well-known prefix locations. `npm bin -g` was
                # removed in npm 9 — don't rely on it.
                local npm_prefix=""
                npm_prefix="$(npm prefix -g 2>/dev/null || npm config get prefix 2>/dev/null || true)"
                local probe_dirs=()
                if [ -n "$npm_prefix" ]; then
                    probe_dirs+=("$npm_prefix/bin")
                fi
                probe_dirs+=(
                    "$HOME/.local/share/npm/bin"
                    "$HOME/.npm-global/bin"
                    "/usr/local/lib/node_modules/.bin"
                )
                local cand
                for cand in "${probe_dirs[@]}"; do
                    if [ -n "$cand" ] && [ -x "$cand/pnpm" ]; then
                        case ":$PATH:" in
                            *":$cand:"*) ;;
                            *) export PATH="$cand:$PATH" ;;
                        esac
                        PKG_MGR="pnpm"
                        break
                    fi
                done
                if [ -z "$PKG_MGR" ]; then
                    echo "[launcher] pnpm install via npm failed. Falling back to npm."
                    PKG_MGR="npm"
                fi
            fi
        fi

        if [ -z "$PKG_MGR" ]; then
            # Same loud-stop pattern as the Node-missing branch above.
            echo ""
            echo "==============================================="
            echo "  Cannot build the launcher: no package manager"
            echo "==============================================="
            echo ""
            echo "  Neither pnpm nor npm is available even after install attempts."
            echo "  Install Node.js (which ships npm) — pnpm is optional, npm is enough:"
            echo "    https://nodejs.org/ (LTS, 18+ recommended)"
            echo ""
            if [ "$YES" -eq 1 ] || [ ! -t 0 ]; then
                echo "[launcher] Non-interactive run: skipping. Re-run with npm available, or build manually."
                MODE="skip"
            else
                while :; do
                    read -r -p "[r] Re-check / [s] Skip the build: " ans || ans="s"
                    case "${ans:-s}" in
                        r|R)
                            _resolves_to_binary pnpm && PKG_MGR="pnpm"
                            [ -z "$PKG_MGR" ] && _resolves_to_binary npm  && PKG_MGR="npm"
                            if [ -n "$PKG_MGR" ]; then
                                echo "[launcher] Detected $PKG_MGR — continuing."
                                break
                            else
                                echo "[launcher] Still no pnpm/npm. Try again or skip."
                            fi
                            ;;
                        s|S)
                            MODE="skip"
                            break
                            ;;
                        *) echo "Type 'r' to re-check or 's' to skip." ;;
                    esac
                done
            fi
            [ -z "$PKG_MGR" ] && MODE="skip"
        fi
    fi

    if [ "$MODE" = "build" ]; then
        # 4b.3: Linux Tauri build deps. release.yml uses libwebkit2gtk-4.1-dev
        # + libgtk-3-dev + libayatana-appindicator3-dev + librsvg2-dev +
        # libsoup-3.0-dev + libjavascriptcoregtk-4.1-dev. apt only — other
        # distros: best-effort skip with a message.
        # Same T1 → T3 → T4 ladder as Node.
        if [ "$OS" = "linux" ]; then
            case "$PKGMGR" in
                apt|apt-get)
                    deps_pkgs=(libwebkit2gtk-4.1-dev libgtk-3-dev \
                               libayatana-appindicator3-dev librsvg2-dev \
                               libsoup-3.0-dev libjavascriptcoregtk-4.1-dev \
                               build-essential curl wget file)
                    # Check EACH dep, not just webkit2gtk. Earlier code
                    # used webkit2gtk as a sentinel — but a system that
                    # had webkit2gtk-dev (e.g. from a prior Electron
                    # build) but lacked appindicator-dev would silently
                    # skip the install and hit a "Can't detect any
                    # appindicator library" panic during tauri build's
                    # bundle step. Now we audit every package and only
                    # install missing ones.
                    missing_deps=()
                    for p in "${deps_pkgs[@]}"; do
                        dpkg -s "$p" >/dev/null 2>&1 || missing_deps+=("$p")
                    done
                    if [ "${#missing_deps[@]}" -gt 0 ]; then
                        echo "[launcher] Missing Tauri Linux deps: ${missing_deps[*]}"
                        installed_deps=0
                        if [ "$YES" -eq 1 ]; then
                            echo "[launcher] T1 silent: installing missing Tauri deps (apt --yes)"
                            _maybe_sudo apt-get update && \
                                _maybe_sudo apt-get install -y "${missing_deps[@]}" && installed_deps=1
                        elif [ -t 0 ]; then
                            echo "[launcher] Tauri build needs system packages."
                            read -r -p "Install missing deps via apt? [Y/n] " ans || ans="Y"
                            case "${ans:-Y}" in
                                ""|y|Y|yes|YES)
                                    _maybe_sudo apt-get update && \
                                        _maybe_sudo apt-get install "${missing_deps[@]}" && installed_deps=1
                                    ;;
                            esac
                        fi
                        if [ $installed_deps -eq 0 ]; then
                            echo "[launcher] T4: deps not installed. Build will likely fail."
                            echo "           Manual: sudo apt install ${missing_deps[*]}"
                        fi
                    fi
                    ;;
                *)
                    echo "[launcher] Non-apt distro ($PKGMGR): skipping auto-install of Tauri deps."
                    echo "           If build fails, install webkit2gtk + gtk3 + libsoup3 + appindicator manually."
                    echo "           See https://tauri.app/start/prerequisites/"
                    ;;
            esac
        fi

        # 4b.4: install + build. Stream output (don't redirect to /dev/null —
        # spec rule: user wants to see what's happening).
        cd "$REPO_ROOT/launcher" || { echo "[launcher] cannot cd to launcher/"; MODE="skip"; }
    fi

    if [ "$MODE" = "build" ]; then
        echo "[launcher] [3/4] $PKG_MGR install"
        _log_event "build/deps" "start" "$PKG_MGR install"
        if [ "$PKG_MGR" = "pnpm" ]; then
            if pnpm install; then
                _log_event "build/deps" "ok" "pnpm install completed"
            else
                echo "[launcher] pnpm install failed."
                _log_event "build/deps" "error" "pnpm install failed"
                MODE="skip"
            fi
        else
            if npm install; then
                _log_event "build/deps" "ok" "npm install completed"
            else
                echo "[launcher] npm install failed."
                _log_event "build/deps" "error" "npm install failed"
                MODE="skip"
            fi
        fi
    fi

    if [ "$MODE" = "build" ]; then
        echo "[launcher] [4/4] tauri build (this takes 5-15 min)"
        _log_event "build/tauri" "start" "tauri build --no-bundle (using $PKG_MGR)"
        # `--no-bundle`: skip the DEB/RPM/AppImage/DMG/MSI packaging step.
        # End users only need the executable binary at
        # target/release/vct-launcher* — they're not redistributing the
        # app, so building installer bundles wastes time AND requires
        # extra system deps like libayatana-appindicator3-dev (DEB),
        # rpmbuild (RPM), etc. that often aren't installed. The bundling
        # step has nothing to do with the launcher *running* — it's
        # solely about producing redistributable artifacts. Maintainers
        # who do want bundles can run `pnpm tauri build` (no flag) in
        # launcher/ themselves.
        if [ "$PKG_MGR" = "pnpm" ]; then
            if pnpm tauri build --no-bundle; then
                _log_event "build/tauri" "ok" "release binary built"
            else
                echo "[launcher] tauri build failed."
                _log_event "build/tauri" "error" "pnpm tauri build exit non-zero"
                MODE="skip"
            fi
        else
            if npx tauri build --no-bundle; then
                _log_event "build/tauri" "ok" "release binary built"
            else
                echo "[launcher] tauri build failed."
                _log_event "build/tauri" "error" "npx tauri build exit non-zero"
                MODE="skip"
            fi
        fi
        cd - >/dev/null || true

        # Find what was just built.
        LAUNCHER_BIN="$(find_binary || true)"
        if [ -z "$LAUNCHER_BIN" ]; then
            echo "[launcher] Build reported success but no binary found in target/release/."
            echo "           See https://github.com/hotak92/vibecoded-orchestrator/blob/main/launcher/KNOWN_ISSUES.md"
            _log_event "build/locate" "error" "binary missing after build success"
        else
            _log_event "build/locate" "ok" "$LAUNCHER_BIN"
        fi
    fi
fi

# ----- Step 5: auto-launch (unless --no-auto-launch) --------------------------
if [ -z "$LAUNCHER_BIN" ]; then
    # Final fallback. Every auto-install path either failed or was
    # declined. Tell the user explicitly, and offer the smartest
    # recovery option vco itself enables: "open Claude Code in this
    # repo and ask it to fix the install." That's literally what the
    # orchestrator is for, and the user already has Claude Code (it's
    # the install.py:[10/10] check that runs before we get here).
    echo ""
    echo "==============================================="
    echo "  Launcher build did not complete"
    echo "==============================================="
    echo ""
    echo "  Manual build (when you've installed the missing prereqs):"
    echo "    cd $REPO_ROOT/launcher"
    echo "    pnpm install     # or: npm install"
    echo "    pnpm tauri build # or: npx tauri build"
    echo "    cd $REPO_ROOT && ./start-launcher.sh"
    echo ""
    echo "  Detailed recovery doc (humans + Claude both read this):"
    echo "    $REPO_ROOT/docs/INSTALL_RECOVERY.md"
    echo ""
    echo "  Stuck? Let Claude Code diagnose this for you:"
    echo "    cd $REPO_ROOT"
    echo "    claude   # opens Claude Code in this repo"
    echo ""
    echo "  Then paste this exact prompt to Claude:"
    echo "    -------- COPY BELOW --------"
    echo "    Read docs/INSTALL_RECOVERY.md and follow it. The launcher"
    echo "    build did not complete during first-install. The launcher"
    echo "    is the GUI for managing projects, services, hooks, RL"
    echo "    retrieval, the knowledge graph dashboard, and the audit log"
    echo "    — without it the user loses the project wizard, KG/codegraph"
    echo "    visualizations, hook toggles, and module install flow."
    echo "    DO NOT just skip the launcher build or tell me to do it"
    echo "    manually. Find why the build failed (missing prereqs, system"
    echo "    deps, perms, fnm/lean-ctx wrappers) and ACTUALLY install/fix"
    echo "    what's needed, then run pnpm tauri build (or npm + npx tauri)"
    echo "    until I have a working binary at launcher/src-tauri/target/"
    echo "    release/. After that, run ./start-launcher.sh and confirm"
    echo "    the launcher window opens."
    echo "    -------- COPY ABOVE --------"
    echo ""
    echo "  The repo's own knowledge graph + hooks give Claude full"
    echo "  context to debug your specific machine. That's why vco exists."
    echo ""
    exit 0
fi

if [ "$AUTO_LAUNCH" -eq 0 ]; then
    echo ""
    echo "[launcher] --no-auto-launch set. Run start-launcher.sh to open the GUI."
    exit 0
fi

echo ""
echo "Installation complete. Opening launcher..."

# macOS Gatekeeper: anything downloaded carries the com.apple.quarantine
# extended attribute. Until we have an Apple Developer ID cert, that means
# Gatekeeper will block the unsigned launcher binary on first launch with
# "developer cannot be verified". Pre-emptively strip the attribute since
# the user already authorized this script (which got here past Gatekeeper).
# This is the documented Apple workaround for unsigned binaries; replaces
# the manual right-click → Open dance for the launcher.
if [ "$OS" = "macos" ]; then
    xattr -d com.apple.quarantine "$LAUNCHER_BIN" 2>/dev/null || true
    # If we're inside an .app bundle, also clear it recursively.
    case "$LAUNCHER_BIN" in
        */Contents/MacOS/*)
            app_root="${LAUNCHER_BIN%/Contents/MacOS/*}"
            if [ -d "$app_root" ]; then
                xattr -dr com.apple.quarantine "$app_root" 2>/dev/null || true
            fi
            ;;
    esac
fi

# Spawn detached. nohup + & + setsid (where available) decouples from this
# shell so first-install can exit without killing the GUI. Redirect stdio
# to /dev/null. Suppress any spawn failure: never block exit-0.
if command -v setsid >/dev/null 2>&1; then
    (setsid nohup "$LAUNCHER_BIN" >/dev/null 2>&1 < /dev/null &) || true
else
    (nohup "$LAUNCHER_BIN" >/dev/null 2>&1 < /dev/null &) || true
fi
disown 2>/dev/null || true

exit 0
