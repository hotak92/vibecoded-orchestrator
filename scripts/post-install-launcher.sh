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
    # _log_event <step> <phase> <detail> [<data_json>]
    # data_json (optional) is a pre-formed JSON object literal — caller
    # is responsible for valid JSON. Used for compact structured fields
    # like {"path":"/x"} or {"size_mb":42}. Detail string is escaped;
    # data is inserted verbatim so callers must escape upstream.
    local step="${1:-?}"
    local phase="${2:-?}"
    local detail="${3:-}"
    local data="${4:-}"
    local path
    path="$(_install_log_path)"
    [ -z "$path" ] && return 0
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # Escape minimal: backslash, double-quote, control chars not handled.
    # Detail strings are bounded (we always pass short literals).
    local esc_detail
    esc_detail="$(printf '%s' "$detail" | sed 's/\\/\\\\/g; s/"/\\"/g')"
    if [ -n "$data" ]; then
        printf '{"ts":"%s","actor":"post-install-launcher.sh","step":"%s","phase":"%s","detail":"%s","data":%s}\n' \
            "$ts" "$step" "$phase" "$esc_detail" "$data" >> "$path" 2>/dev/null || true
    else
        printf '{"ts":"%s","actor":"post-install-launcher.sh","step":"%s","phase":"%s","detail":"%s"}\n' \
            "$ts" "$step" "$phase" "$esc_detail" >> "$path" 2>/dev/null || true
    fi
}

# Helper to JSON-escape a string for use inside _log_event's data field.
# Handles backslash + double-quote — same rules as detail.
_json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

YES=0
AUTO_LAUNCH=1
NO_DESKTOP_ICON=0
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
        --no-desktop-icon) NO_DESKTOP_ICON=1 ;;
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

# Mark the start of the post-install phase in the durable log. The log
# directory was created by install.py Step 8, so this lands in a real
# file (unless install.py never reached Step 8 — in which case the log
# helper silently no-ops, matching the "never-throw" contract).
_log_event "script-start" "start" \
    "post-install-launcher.sh starting on $OS" \
    "{\"os\":\"$(_json_escape "$OS")\",\"yes\":$YES,\"auto_launch\":$AUTO_LAUNCH}"

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
    # M-P0-6 (v0.2.53): Apple Silicon Homebrew installs node / npm /
    # pnpm under `/opt/homebrew/bin/...` (not `/usr/local/bin/...` —
    # that's Intel-Mac homebrew). The previous probe list only
    # checked Intel-Mac + Linux paths → Apple-Silicon users with
    # brew-installed Node showed `node: no` and fell into the
    # silent-build path. Add `/opt/homebrew/bin/...` to each probe
    # list (placed before `/usr/local/bin/...` so it wins on Apple
    # Silicon when both happen to exist).
    #
    # Also: re-source brew shellenv if brew is on disk but not in
    # PATH. `install.sh` does this for python detection (line ~158)
    # but post-install-launcher.sh runs in a fresh subshell — the
    # env doesn't propagate. Doing it here means subsequent `brew
    # install` / `node` / `npm` calls in this script find the right
    # binaries on Apple Silicon.
    if [ "$OS" = "macos" ] && [ -x "/opt/homebrew/bin/brew" ]; then
        # shellcheck disable=SC1091
        eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)" || true
    fi
    _ensure_path_for_tool node \
        "$HOME/.local/bin/node" \
        "$HOME/.fnm/aliases/default/bin/node" \
        "$HOME/.nvm/versions/node/*/bin/node" \
        "/opt/homebrew/bin/node" \
        "/usr/local/bin/node" \
        "/usr/bin/node" \
        && HAS_NODE=1
    _ensure_path_for_tool npm \
        "$HOME/.local/bin/npm" \
        "$HOME/.fnm/aliases/default/bin/npm" \
        "$HOME/.nvm/versions/node/*/bin/npm" \
        "/opt/homebrew/bin/npm" \
        "/usr/local/bin/npm" \
        "/usr/bin/npm" \
        && HAS_NPM=1
    _ensure_path_for_tool pnpm \
        "$HOME/.local/bin/pnpm" \
        "$HOME/.local/share/pnpm/pnpm" \
        "/opt/homebrew/bin/pnpm" \
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

    # Emit a structured audit event so the launcher / Claude Code can
    # tell at a glance which paths were viable on this machine. Bool
    # encoded as 1/0 to keep JSON compact.
    _log_event "audit" "ok" "prerequisites audited" \
        "{\"curl\":$HAS_CURL,\"wget\":$HAS_WGET,\"python3\":$HAS_PYTHON3,\"node\":$HAS_NODE,\"npm\":$HAS_NPM,\"pnpm\":$HAS_PNPM,\"sudo\":$HAS_SUDO,\"pkgmgr\":\"$(_json_escape "$PKGMGR")\"}"
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
# Discovery order (highest-priority first):
#   1. Locally built binary (developers running `pnpm tauri build` directly).
#   2. Bundled prebuilt at launcher/dist/<arch>/ (default for end users).
#   3. System install paths (~/.local/share, /usr/local, etc.) — for users who
#      installed a launcher via apt/brew/winget at some earlier point.
# See launcher/dist/README.md for the bundling layout.
candidates_unix=(
    # 1. Locally built (contributors)
    "$REPO_ROOT/launcher/src-tauri/target/release/vct-launcher"
    "$REPO_ROOT/launcher/src-tauri/target/release/vct-launcher-temp"
    "$REPO_ROOT/launcher/src-tauri/target/release/launcher"
    "$REPO_ROOT/launcher/src-tauri/target/debug/vct-launcher-temp"
    # 2. Bundled-in-repo prebuilt (default for end users — sidesteps the
    #    build-from-source path that was the launch-blocker for users
    #    without Node + Rust + webkit2gtk-dev). 30 MB committed binaries
    #    per arch; see launcher/dist/README.md.
    "$REPO_ROOT/launcher/dist/linux-x64/vct-launcher"
    # 3. System installs
    "$HOME/.local/share/vct-launcher/vct-launcher"
    "$HOME/.local/bin/vct-launcher"
    "/usr/bin/vct-launcher"
    "/usr/local/bin/vct-launcher"
)
candidates_mac=(
    # Locally built (contributors)
    "$REPO_ROOT/launcher/src-tauri/target/release/vct-launcher"
    "$REPO_ROOT/launcher/src-tauri/target/release/vct-launcher-temp"
    # Bundled prebuilt — canonical dist dir is `macos-arm64/` since
    # v0.2.13 (install.py:16956). Both flat-file and .app bundle modes
    # supported (release.yml emits the flat file; native Gatekeeper
    # signing path emits the .app).
    # M-P0-2 (v0.2.53): added macos-arm64 paths; experimental_macOS
    # retained as legacy fallback for old checkouts.
    "$REPO_ROOT/launcher/dist/macos-arm64/vct-launcher"
    "$REPO_ROOT/launcher/dist/macos-arm64/vct-launcher.app/Contents/MacOS/vct-launcher"
    "$REPO_ROOT/launcher/dist/macos-arm64/vct-launcher.app/Contents/MacOS/VCT Launcher"
    # Legacy (pre-v0.2.13) — fallback only.
    "$REPO_ROOT/launcher/dist/experimental_macOS/vct-launcher"
    "$REPO_ROOT/launcher/dist/experimental_macOS/vct-launcher.app/Contents/MacOS/vct-launcher"
    "$REPO_ROOT/launcher/dist/experimental_macOS/vct-launcher.app/Contents/MacOS/VCT Launcher"
    # System installs
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

# Staleness check for bundled binaries. Reads <binary>.metadata.json (the
# manifest scripts/build-bundled-launcher.sh writes alongside the binary)
# and compares its source_hash against the current launcher subtree's
# git ls-tree hash. If they don't match, the bundled binary was built
# from an older snapshot of launcher/src-tauri or launcher/src — likely
# missing recent commands or behavior. Returns 0 (fresh) or 1 (stale).
#
# Only checks bundled binaries (paths under launcher/dist/). Locally
# built binaries (target/release/) and system installs are assumed
# fresh — the user opted in to those.
_bundled_binary_is_fresh() {
    local bin="$1"
    case "$bin" in
        "$REPO_ROOT"/launcher/dist/*) ;;
        *) return 0 ;;  # not a bundled binary; staleness check skipped
    esac
    local meta="${bin}.metadata.json"
    if [ ! -f "$meta" ]; then
        # No metadata → can't verify freshness. Treat as stale to err
        # toward correctness; users can override by deleting the
        # metadata file or moving the binary outside dist/.
        echo "[launcher] ${bin##*/} has no metadata.json — treating as stale"
        return 1
    fi
    if ! command -v git >/dev/null 2>&1; then
        # Without git we can't compute the live hash. Trust the bundle.
        return 0
    fi
    local meta_hash live_hash
    meta_hash="$(grep -oE '"source_hash"[^"]*"[^"]*"' "$meta" 2>/dev/null | sed -E 's/.*"source_hash"[^"]*"([^"]*)".*/\1/')"
    if [ -z "$meta_hash" ] || [ "$meta_hash" = "null" ]; then
        return 0  # metadata didn't capture a hash — be lenient
    fi
    live_hash="$(cd "$REPO_ROOT" && git ls-tree HEAD launcher/src-tauri/src/ launcher/src/ launcher/src-tauri/Cargo.toml launcher/src-tauri/Cargo.lock launcher/package.json 2>/dev/null | git hash-object --stdin 2>/dev/null || echo '')"
    if [ -z "$live_hash" ]; then
        return 0  # not in a git checkout (tarball install) — trust the bundle
    fi
    if [ "$meta_hash" = "$live_hash" ]; then
        # Hash matches but the binary may still be broken — a build that
        # ran with an empty `launcher/build/` produces a release binary
        # that compiles fine but has NO embedded frontend, leaving the
        # webview to fail with "Could not connect to localhost" at
        # startup. This regressed in 5abb8cf (2026-04-28). Sanity-check
        # that embedded SvelteKit assets are present before trusting the
        # binary. `strings` is part of binutils — present on every
        # supported host with a Rust toolchain. If absent, skip the
        # check (don't false-fail on minimal containers).
        if command -v strings >/dev/null 2>&1; then
            local embedded_count
            embedded_count="$(strings "$bin" 2>/dev/null | grep -c '_app/immutable/assets' || true)"
            if [ "${embedded_count:-0}" -lt 5 ]; then
                echo "[launcher] ${bin##*/} hash matches but frontend is NOT embedded (found $embedded_count asset refs, expected >=5)."
                echo "           Refusing to trust — bundled binary was built with an empty launcher/build/."
                return 1
            fi
        fi
        return 0
    fi
    echo "[launcher] ${bin##*/} is stale (built from a different launcher source)"
    echo "           bundled source_hash=$meta_hash"
    echo "           live    source_hash=$live_hash"
    return 1
}

LAUNCHER_BIN="$(find_binary || true)"

# If find_binary picked a bundled binary, verify it's fresh. Stale =
# fall through to download / build paths.
if [ -n "$LAUNCHER_BIN" ] && ! _bundled_binary_is_fresh "$LAUNCHER_BIN"; then
    echo "[launcher] Skipping stale bundled binary; will try download/build."
    LAUNCHER_BIN=""
fi

if [ -n "$LAUNCHER_BIN" ]; then
    echo "[launcher] Found existing binary: $LAUNCHER_BIN"
    _log_event "binary-probe" "ok" "existing launcher binary found" \
        "{\"path\":\"$(_json_escape "$LAUNCHER_BIN")\"}"
else
    _log_event "binary-probe" "skip" "no existing launcher binary on disk"
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
# M-P0-3 (v0.2.53): release.yml currently ships .zip assets for both
# macOS and Linux (vibecoded-orchestrator-<ver>-macos-arm64.zip,
# vibecoded-orchestrator-<ver>-linux-x64.zip). The previous filter
# only accepted .dmg (macOS) / .appimage (Linux), so picked was
# always None → every user fell into the build path. Prefer .zip and
# keep the legacy formats as a fallback for if/when CI starts shipping
# them again.
if os_name == "linux":
    if arch in ("x86_64", "amd64", "x64"):
        picked = pick(lambda n: n.endswith(".zip") and ("linux-x64" in n or "linux_x64" in n or "linux" in n))
    if picked is None:
        picked = pick(lambda n: n.endswith(".zip") and "linux" in n)
    if picked is None:
        # Legacy fallback (CI may resume publishing AppImages later).
        picked = pick(lambda n: n.endswith(".appimage"))
elif os_name == "macos":
    if arch in ("arm64", "aarch64"):
        picked = pick(lambda n: n.endswith(".zip") and ("arm64" in n or "aarch64" in n or "macos" in n))
    if picked is None:
        picked = pick(lambda n: n.endswith(".zip") and "macos" in n)
    if picked is None:
        # Legacy fallback (CI may resume publishing DMGs later).
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
            _log_event "download" "skip" "no prebuilt asset for $OS" \
                "{\"os\":\"$(_json_escape "$OS")\"}"
            MODE="build"
        else
            echo "[launcher] Downloading: $asset_name"
            _log_event "download" "start" "downloading $asset_name" \
                "{\"asset\":\"$(_json_escape "$asset_name")\"}"
            # M-P0-3 (v0.2.53): release.yml ships .zip assets for both
            # macOS and Linux. The earlier dispatch assumed .appimage
            # (Linux) / .dmg (macOS) which never arrived. Now: detect
            # the actual asset extension from $asset_name and pick the
            # right extraction path. Legacy .appimage + .dmg branches
            # are kept for when CI resumes shipping those formats.
            asset_ext_lower="$(printf '%s' "$asset_name" | tr '[:upper:]' '[:lower:]')"
            case "$asset_ext_lower" in
                *.zip) asset_kind="zip" ;;
                *.appimage) asset_kind="appimage" ;;
                *.dmg) asset_kind="dmg" ;;
                *) asset_kind="" ;;
            esac

            tmp_dir="$(mktemp -d -t vct-launcher.XXXXXX 2>/dev/null \
                        || mktemp -d "${TMPDIR:-/tmp}/vct-launcher.XXXXXX")"

            if [ "$asset_kind" = "zip" ]; then
                # Unified .zip extraction path (works on both macOS via
                # BSD `unzip` and Linux via Info-ZIP `unzip`). Look for:
                #   1. A flat `vct-launcher` (or `.exe` on Windows — N/A here).
                #   2. A `.app` bundle (macOS-only signed build).
                # Mirrors the dist/<os-arch>/ layout the CI uses.
                tmp_zip="$tmp_dir/launcher.zip"
                if ! _download "$asset_url" "$tmp_zip"; then
                    echo "[launcher] Download failed. Falling back to build."
                    _log_event "download" "error" "$OS zip download failed (curl/wget exit non-zero)"
                    rm -rf "$tmp_dir"
                    MODE="build"
                elif ! command -v unzip >/dev/null 2>&1; then
                    echo "[launcher] unzip missing — cannot extract $asset_name. Falling back to build."
                    _log_event "download" "error" "$OS unzip missing for zip extraction"
                    rm -rf "$tmp_dir"
                    MODE="build"
                else
                    extract_dir="$tmp_dir/extracted"
                    mkdir -p "$extract_dir"
                    if ! unzip -q "$tmp_zip" -d "$extract_dir" 2>/dev/null; then
                        echo "[launcher] unzip failed on $asset_name. Falling back to build."
                        _log_event "download" "error" "$OS unzip extraction failed"
                        rm -rf "$tmp_dir"
                        MODE="build"
                    else
                        # Locate launcher binary inside extracted tree.
                        bin_in_zip=""
                        if [ "$OS" = "macos" ]; then
                            # Prefer .app bundle if present.
                            app_in_zip="$(find "$extract_dir" -maxdepth 5 -name '*.app' -type d 2>/dev/null | head -1)"
                            if [ -n "$app_in_zip" ]; then
                                # Install bundle to /Applications (preferred)
                                # or ~/Applications (fallback if no admin).
                                if cp -R "$app_in_zip" /Applications/ 2>/dev/null; then
                                    dest_app="/Applications/$(basename "$app_in_zip")"
                                else
                                    mkdir -p "$HOME/Applications"
                                    cp -R "$app_in_zip" "$HOME/Applications/" 2>/dev/null
                                    dest_app="$HOME/Applications/$(basename "$app_in_zip")"
                                fi
                                for cand in "$dest_app/Contents/MacOS/VCT Launcher" \
                                            "$dest_app/Contents/MacOS/vct-launcher" \
                                            "$dest_app/Contents/MacOS/vct-launcher-temp"; do
                                    if [ -x "$cand" ]; then bin_in_zip="$cand"; break; fi
                                done
                            fi
                            # Fallback: flat vct-launcher inside zip.
                            if [ -z "$bin_in_zip" ]; then
                                bin_in_zip="$(find "$extract_dir" -maxdepth 5 \
                                    \( -name 'vct-launcher' -o -name 'vct-launcher-temp' \) \
                                    -type f 2>/dev/null | head -1)"
                                if [ -n "$bin_in_zip" ]; then
                                    target_dir="$HOME/.local/share/vct-launcher"
                                    mkdir -p "$target_dir"
                                    cp "$bin_in_zip" "$target_dir/vct-launcher"
                                    chmod +x "$target_dir/vct-launcher"
                                    bin_in_zip="$target_dir/vct-launcher"
                                fi
                            fi
                        else
                            # Linux: flat vct-launcher.
                            bin_in_zip="$(find "$extract_dir" -maxdepth 5 \
                                \( -name 'vct-launcher' -o -name 'vct-launcher-temp' \) \
                                -type f 2>/dev/null | head -1)"
                            if [ -n "$bin_in_zip" ]; then
                                target_dir="$HOME/.local/share/vct-launcher"
                                mkdir -p "$target_dir"
                                cp "$bin_in_zip" "$target_dir/vct-launcher"
                                chmod +x "$target_dir/vct-launcher"
                                bin_in_zip="$target_dir/vct-launcher"
                            fi
                        fi
                        rm -rf "$tmp_dir"
                        if [ -n "$bin_in_zip" ] && [ -x "$bin_in_zip" ]; then
                            LAUNCHER_BIN="$bin_in_zip"
                            echo "[launcher] Extracted launcher to $LAUNCHER_BIN"
                            _log_event "download" "ok" "$OS zip extracted" \
                                "{\"path\":\"$(_json_escape "$LAUNCHER_BIN")\"}"
                        else
                            echo "[launcher] No launcher binary found inside $asset_name. Falling back to build."
                            _log_event "download" "error" "$OS zip missing launcher binary"
                            MODE="build"
                        fi
                    fi
                fi
            elif [ "$asset_kind" = "appimage" ] && [ "$OS" = "linux" ]; then
                # Legacy Linux .appimage path (kept for if/when CI
                # resumes publishing AppImages — currently disabled).
                target_dir="$HOME/.local/share/vct-launcher"
                mkdir -p "$target_dir"
                target_path="$target_dir/vct-launcher"
                if _download "$asset_url" "$target_path"; then
                    chmod +x "$target_path"
                    sz=$(stat -c%s "$target_path" 2>/dev/null || stat -f%z "$target_path" 2>/dev/null || echo 0)
                    if [ "${sz:-0}" -gt 10485760 ]; then
                        LAUNCHER_BIN="$target_path"
                        echo "[launcher] Downloaded to $target_path ($((sz / 1048576)) MB)"
                        _log_event "download" "ok" "linux appimage downloaded" \
                            "{\"path\":\"$(_json_escape "$target_path")\",\"size_mb\":$((sz / 1048576))}"
                    else
                        echo "[launcher] Downloaded file looks too small ($sz bytes). Falling back to build."
                        rm -f "$target_path"
                        _log_event "download" "error" "downloaded file too small" \
                            "{\"size_bytes\":${sz:-0}}"
                        MODE="build"
                    fi
                else
                    echo "[launcher] Download failed. Falling back to build."
                    _log_event "download" "error" "linux appimage download failed (curl/wget exit non-zero)"
                    MODE="build"
                fi
                rm -rf "$tmp_dir"
            elif [ "$asset_kind" = "dmg" ] && [ "$OS" = "macos" ]; then
                # Legacy macOS .dmg path. Kept for if/when CI resumes
                # publishing DMGs. unreachable today because pick()
                # prefers .zip first.
                if [ $HAS_HDIUTIL -eq 0 ]; then
                    echo "[launcher] hdiutil missing (unexpected on macOS). Falling back to build."
                    _log_event "download" "error" "macos hdiutil missing"
                    rm -rf "$tmp_dir"
                    MODE="build"
                else
                    tmp_dmg="$tmp_dir/launcher.dmg"
                    if _download "$asset_url" "$tmp_dmg"; then
                        mount_point="$(hdiutil attach -nobrowse -quiet "$tmp_dmg" 2>/dev/null \
                            | awk '/\/Volumes\// {for (i=3;i<=NF;i++) printf "%s%s", $i, (i<NF?" ":""); print ""}' \
                            | tail -1)"
                        if [ -n "$mount_point" ] && [ -d "$mount_point" ]; then
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
                        rm -rf "$tmp_dir"
                        if [ -z "$LAUNCHER_BIN" ]; then
                            echo "[launcher] DMG mount/copy failed. Falling back to build."
                            _log_event "download" "error" "macos dmg mount/copy failed"
                            MODE="build"
                        else
                            _log_event "download" "ok" "macos dmg installed" \
                                "{\"path\":\"$(_json_escape "$LAUNCHER_BIN")\"}"
                        fi
                    else
                        echo "[launcher] Download failed. Falling back to build."
                        _log_event "download" "error" "macos download failed"
                        rm -rf "$tmp_dir"
                        MODE="build"
                    fi
                fi
            else
                echo "[launcher] Unrecognised asset extension for $asset_name (kind='$asset_kind'). Falling back to build."
                _log_event "download" "error" "$OS unknown asset kind" \
                    "{\"asset\":\"$(_json_escape "$asset_name")\"}"
                rm -rf "$tmp_dir"
                MODE="build"
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
                # M-P0-5 (v0.2.53): `local` outside a function aborts
                # under `set -u` (script runs under `set -uo pipefail`
                # at line 50). The previous use of `local` here printed
                # "local: can only be used in a function" + tripped
                # the next `$npm_prefix` reference with an "unbound
                # variable" abort → script exited 127 silently from
                # the user's perspective. The block isn't inside a
                # function, so plain assignment is correct.
                npm_prefix=""
                npm_prefix="$(npm prefix -g 2>/dev/null || npm config get prefix 2>/dev/null || true)"
                probe_dirs=()
                if [ -n "$npm_prefix" ]; then
                    probe_dirs+=("$npm_prefix/bin")
                fi
                probe_dirs+=(
                    "$HOME/.local/share/npm/bin"
                    "$HOME/.npm-global/bin"
                    "/usr/local/lib/node_modules/.bin"
                )
                # `cand` is the loop variable below; just leave it
                # plain (no `local`). This is the third site that hit
                # the bug.
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
                        # Build a minimal JSON array of missing dep names
                        # for the structured `data` field. Newlines/quotes
                        # in package names are impossible (apt forbids
                        # them) so a simple comma-join is safe.
                        _missing_json="["
                        _first=1
                        for _p in "${missing_deps[@]}"; do
                            if [ $_first -eq 1 ]; then _first=0; else _missing_json+=","; fi
                            _missing_json+="\"$(_json_escape "$_p")\""
                        done
                        _missing_json+="]"
                        _log_event "apt-deps" "start" \
                            "${#missing_deps[@]} apt deps to install" \
                            "{\"missing\":$_missing_json}"
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
                            _log_event "apt-deps" "error" \
                                "deps not installed (T4 manual hint)" \
                                "{\"missing\":$_missing_json}"
                        else
                            _log_event "apt-deps" "ok" "apt deps installed"
                        fi
                    else
                        _log_event "apt-deps" "skip" "all Tauri Linux deps already present"
                    fi
                    ;;
                *)
                    echo "[launcher] Non-apt distro ($PKGMGR): skipping auto-install of Tauri deps."
                    echo "           If build fails, install webkit2gtk + gtk3 + libsoup3 + appindicator manually."
                    echo "           See https://tauri.app/start/prerequisites/"
                    _log_event "apt-deps" "skip" \
                        "non-apt distro ($PKGMGR); manual deps required" \
                        "{\"pkgmgr\":\"$(_json_escape "$PKGMGR")\"}"
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

# ----- Desktop shortcut (opt-out) ---------------------------------------------
# Create a launcher shortcut so the user can double-click to open the GUI.
# Per-user only (no sudo, no /usr/share/applications). Idempotent: re-running
# the install simply overwrites the entry with the current binary path.
#
#   Linux : ~/.local/share/applications/vct-launcher.desktop
#           + ~/Desktop/vct-launcher.desktop (when ~/Desktop exists)
#   macOS : symlink ~/Applications/VCT Launcher → the .app bundle
#           (only when LAUNCHER_BIN is inside a .app structure)
#   The Windows path lives in first-install.bat — Windows needs a .lnk
#   shortcut written via PowerShell, which is awkward to call from bash.
#
# Opt-out: --no-desktop-icon CLI flag, VCT_NO_DESKTOP_ICON=1 env var, or
# answering n to the interactive prompt. Default Y because most users
# want the icon.
_create_desktop_shortcut() {
    if [ "${VCT_NO_DESKTOP_ICON:-0}" = "1" ] || [ "${NO_DESKTOP_ICON:-0}" = "1" ]; then
        echo "[launcher] Skipping desktop shortcut (opt-out)."
        return 0
    fi

    # Interactive prompt unless --yes or non-TTY.
    if [ "$YES" -eq 0 ] && [ -t 0 ]; then
        local ans
        read -r -p "Create a desktop shortcut for the launcher? [Y/n] " ans || ans="Y"
        case "${ans:-Y}" in
            n|N|no|NO) echo "[launcher] Skipping desktop shortcut."; return 0 ;;
        esac
    fi

    case "$OS" in
        linux) _create_linux_desktop_entry ;;
        macos) _create_macos_app_link ;;
        *)     return 0 ;;
    esac
}

_create_linux_desktop_entry() {
    # Pick the highest-resolution icon shipped with the launcher source
    # tree. Falls back to the first available size.
    local icon_path=""
    local icon_candidates=(
        "$REPO_ROOT/launcher/src-tauri/icons/icon.png"
        "$REPO_ROOT/launcher/src-tauri/icons/128x128@2x.png"
        "$REPO_ROOT/launcher/src-tauri/icons/128x128.png"
    )
    for c in "${icon_candidates[@]}"; do
        if [ -f "$c" ]; then
            icon_path="$c"
            break
        fi
    done

    local apps_dir="$HOME/.local/share/applications"
    mkdir -p "$apps_dir"
    local desktop_file="$apps_dir/vct-launcher.desktop"
    cat > "$desktop_file" <<DESKTOP_EOF
[Desktop Entry]
Type=Application
Name=VCT Launcher
GenericName=VibeCoded Tools Launcher
Comment=Project manager + KG/codegraph dashboards for vibecoded-orchestrator
Exec="$LAUNCHER_BIN"
Icon=$icon_path
Terminal=false
Categories=Development;IDE;
StartupWMClass=vct-launcher
StartupNotify=true
DESKTOP_EOF
    chmod 644 "$desktop_file"
    echo "[launcher] Desktop entry: $desktop_file"

    # Optional: copy a clickable shortcut to ~/Desktop/ if the user has a
    # standard desktop directory. Many distros + DEs honor this; some
    # require "Allow Launching" via right-click first (GNOME 42+). The
    # Applications entry above already gets the launcher into the system
    # menu / activities overview regardless.
    if [ -d "$HOME/Desktop" ]; then
        cp "$desktop_file" "$HOME/Desktop/vct-launcher.desktop" 2>/dev/null && \
            chmod +x "$HOME/Desktop/vct-launcher.desktop" 2>/dev/null && \
            echo "[launcher] Desktop icon: $HOME/Desktop/vct-launcher.desktop" && \
            echo "           (GNOME 42+: right-click → 'Allow Launching' on first use)"
    fi

    # Refresh the desktop entry cache so the launcher appears in the system
    # menu without requiring a logout. Failure is non-fatal.
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$apps_dir" 2>/dev/null || true
    fi

    _log_event "desktop-icon" "ok" "Linux desktop entry created" \
        "{\"path\":\"$(_json_escape "$desktop_file")\"}"
}

_create_macos_app_link() {
    # Only meaningful when LAUNCHER_BIN points inside a .app bundle.
    case "$LAUNCHER_BIN" in
        */Contents/MacOS/*) ;;
        *)
            echo "[launcher] macOS shortcut skipped: launcher is not a .app bundle"
            return 0
            ;;
    esac
    local app_root="${LAUNCHER_BIN%/Contents/MacOS/*}"
    if [ ! -d "$app_root" ]; then
        return 0
    fi
    mkdir -p "$HOME/Applications"
    local link_target="$HOME/Applications/$(basename "$app_root")"
    if [ -L "$link_target" ] || [ -e "$link_target" ]; then
        rm -f "$link_target" 2>/dev/null || true
    fi
    ln -s "$app_root" "$link_target" 2>/dev/null && \
        echo "[launcher] Application link: $link_target" && \
        _log_event "desktop-icon" "ok" "macOS Applications link" \
            "{\"path\":\"$(_json_escape "$link_target")\"}"
}

_create_desktop_shortcut

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
#
# `$!` (PID of last bg job) is unset under set -u because the
# `(... &)` subshell scope closes before we read it. Use the param-default
# form `${!:-0}` to keep set -u happy. The PID is informational only —
# we use it for the log event, not for any wait/signal logic.
_spawn_pid="0"
if command -v setsid >/dev/null 2>&1; then
    (setsid nohup "$LAUNCHER_BIN" >/dev/null 2>&1 < /dev/null &) || true
    _spawn_pid="${!:-0}"
else
    (nohup "$LAUNCHER_BIN" >/dev/null 2>&1 < /dev/null &) || true
    _spawn_pid="${!:-0}"
fi
disown 2>/dev/null || true

# Note: the subshell pid above is the subshell, not the launcher itself,
# but it's the closest correlation we have without running `pgrep` from
# this script. The launcher's own pid file (~/.vct/launcher.pid) is a
# better source for runtime tooling — this event mostly says "we tried".
_log_event "spawn" "ok" "launcher detached" \
    "{\"binary\":\"$(_json_escape "$LAUNCHER_BIN")\",\"subshell_pid\":${_spawn_pid:-0}}"

exit 0
