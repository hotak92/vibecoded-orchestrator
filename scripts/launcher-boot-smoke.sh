#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Part of VibeCoded Orchestrator.
#
# launcher-boot-smoke.sh — assert a built vct-launcher binary COMPLETES boot.
#
# WHY THIS EXISTS (v0.2.89 field incident): the shipped launcher panicked in
# Tauri's setup() on the main thread ("there is no reactor running" — a bare
# tokio::spawn outside any runtime context) and died before the window
# existed. `cargo test` is structurally blind to that class: #[tokio::test]
# supplies the reactor that setup() lacks. The only honest check is booting
# the real binary and watching setup() finish.
#
# PASS CRITERION: the log shows the unconditional "[vct] setup complete"
# milestone (printed at the END of setup(), i.e. after the boot-resume
# sweeps where the v0.2.89 class lived) AND no Rust panic appears. A mere
# "process still alive after N seconds" check is NOT sufficient: a fresh
# HOME boot can sit in desktop-services negotiation (keyring/portal
# activation) for longer than any reasonable window without ever reaching
# the resume sweeps — alive-but-not-booted must not pass.
#
# Usage: launcher-boot-smoke.sh <path-to-vct-launcher> [timeout-seconds]
#
# Environment handling:
#   - HOME/XDG dirs are redirected to a throwaway temp dir so the smoke
#     NEVER touches the operator's real ~/.vct state (fresh first-run boot).
#   - Display: uses xvfb-run when available (CI), else the caller's real
#     DISPLAY/WAYLAND_DISPLAY (dev machine — a window may appear briefly),
#     else fails with exit 3.
#   - D-Bus: wraps in dbus-run-session when available so the
#     single-instance plugin sees a private bus (a concurrently running
#     real launcher can't make the smoke instance exit early).
set -uo pipefail

BIN="${1:?usage: launcher-boot-smoke.sh <path-to-vct-launcher> [timeout-seconds]}"
TIMEOUT_SECS="${2:-180}"
MARKER="[vct] setup complete"

if [ ! -x "$BIN" ]; then
    echo "[boot-smoke] FAIL: binary not found or not executable: $BIN" >&2
    exit 1
fi
BIN="$(cd "$(dirname "$BIN")" && pwd)/$(basename "$BIN")"
BIN_DIR="$(dirname "$BIN")"

SMOKE_HOME="$(mktemp -d "${TMPDIR:-/tmp}/vct-boot-smoke.XXXXXX")"
LOG="$SMOKE_HOME/boot-smoke.log"

fail() {
    echo "[boot-smoke] FAIL: $1" >&2
    echo "--- last 40 log lines ---" >&2
    tail -40 "$LOG" >&2 || true
    exit 1
}

cleanup() {
    # Kill the launcher's process group, then any hub the launcher
    # auto-started from the SAME dist/build dir (path-scoped so a real
    # hub running from a different install is never touched).
    if [ -n "${SMOKE_PID:-}" ]; then
        kill -TERM -- "-$SMOKE_PID" 2>/dev/null || true
        sleep 1
        kill -KILL -- "-$SMOKE_PID" 2>/dev/null || true
    fi
    # pkill -f takes an ERE — escape regex metacharacters in the path so
    # an unusual build dir can't widen (or break) the match.
    esc_bin_dir=$(printf '%s' "$BIN_DIR" | sed -e 's/[]\/$*.^[]/\\&/g' -e 's/[(){}?+|]/\\&/g')
    pkill -f "^${esc_bin_dir}/vct-hub" 2>/dev/null || true
    rm -rf "$SMOKE_HOME"
}
trap cleanup EXIT

# Build the wrapper chain: [xvfb-run] [dbus-run-session] <binary>
RUNNER=("$BIN")
if command -v dbus-run-session >/dev/null 2>&1; then
    RUNNER=(dbus-run-session -- "${RUNNER[@]}")
fi
# xvfb-run resolution: `command -v` only checks PATH, and a non-interactive
# shell's PATH routinely lacks the dirs a package manager installed into. When
# the probe came up empty the script SILENTLY fell through to the operator's
# REAL display — a launcher window flashed onto the desktop mid-smoke, and the
# run was no longer headless (the window-flash incident). Same candidate-path
# pattern as templates/hooks/lean-ctx-rewrite.sh's lean-ctx probe: try PATH,
# then the known install locations, and SAY SO before falling back.
XVFB_RUN=""
if command -v xvfb-run >/dev/null 2>&1; then
    XVFB_RUN="xvfb-run"
else
    for _cand in \
        /usr/bin/xvfb-run \
        /usr/local/bin/xvfb-run \
        "$HOME/.local/bin/xvfb-run" \
        /opt/homebrew/bin/xvfb-run \
        /home/linuxbrew/.linuxbrew/bin/xvfb-run; do
        if [ -x "$_cand" ]; then
            XVFB_RUN="$_cand"
            break
        fi
    done
fi
if [ -n "$XVFB_RUN" ]; then
    RUNNER=("$XVFB_RUN" --auto-servernum "${RUNNER[@]}")
elif [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "[boot-smoke] FAIL: no display and no xvfb-run — install xvfb" >&2
    exit 3
else
    echo "[boot-smoke] NOTE: xvfb-run not found on PATH or at any candidate" \
         "path — running against the REAL display" \
         "(DISPLAY='${DISPLAY:-}' WAYLAND_DISPLAY='${WAYLAND_DISPLAY:-}')." \
         "A launcher window WILL appear briefly. Install xvfb to run headless." >&2
fi

echo "[boot-smoke] booting $BIN (isolated HOME=$SMOKE_HOME, waiting up to ${TIMEOUT_SECS}s for '$MARKER')"
# Service URLs are forced to an unroutable port: the operator's shell may
# carry real backend URLs (dev workspaces export WEAVIATE_URL), and the
# smoke instance must never scan or write real backends — its throwaway
# DB would re-surface long-resolved maintenance modals against the real
# Weaviate, and any click in the smoke window would act under the wrong
# project context. VCT_WEAVIATE_URL is the launcher's PREFERRED override
# (it wins over the legacy WEAVIATE_URL name at every resolution site,
# including the maintenance-scan path), so it must be pinned too.
# VCT_STATE_DIR / VCT_SECRETS_DIR are pinned INSIDE the throwaway HOME:
# both are documented dev knobs that would otherwise bypass HOME
# isolation entirely and point the smoke at the operator's real
# launcher.db / secrets store.
# setsid caveat: if the spawned child were already a process-group leader
# (interactive job-control shells), setsid would fork and $! would name
# the exited parent; both wired call sites (pre-ship gate, release.yml)
# run non-interactive bash, where the child is never a leader.
setsid env \
    HOME="$SMOKE_HOME" \
    XDG_CONFIG_HOME="$SMOKE_HOME/.config" \
    XDG_DATA_HOME="$SMOKE_HOME/.local/share" \
    XDG_CACHE_HOME="$SMOKE_HOME/.cache" \
    VCT_STATE_DIR="$SMOKE_HOME/.vct" \
    VCT_SECRETS_DIR="$SMOKE_HOME/.vct-secrets" \
    VCT_WEAVIATE_URL="http://127.0.0.1:9" \
    WEAVIATE_URL="http://127.0.0.1:9" \
    OLLAMA_URL="http://127.0.0.1:9" \
    CODE_EMBED_SERVICE_URL="http://127.0.0.1:9" \
    "${RUNNER[@]}" >"$LOG" 2>&1 &
SMOKE_PID=$!

elapsed=0
while [ "$elapsed" -lt "$TIMEOUT_SECS" ]; do
    if grep -aF "panicked at" "$LOG" >/dev/null 2>&1; then
        fail "Rust panic during boot"
    fi
    if grep -aF "$MARKER" "$LOG" >/dev/null 2>&1; then
        echo "[boot-smoke] PASS: setup complete after ${elapsed}s, no panics"
        exit 0
    fi
    if ! kill -0 "$SMOKE_PID" 2>/dev/null; then
        wait "$SMOKE_PID" 2>/dev/null && rc=0 || rc=$?
        fail "launcher exited (rc=$rc) before setup completed (${elapsed}s)"
    fi
    sleep 1
    elapsed=$((elapsed + 1))
done

fail "timed out after ${TIMEOUT_SECS}s waiting for '$MARKER' (process alive but boot never completed)"
