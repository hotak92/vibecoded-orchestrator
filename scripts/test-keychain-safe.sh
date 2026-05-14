#!/usr/bin/env bash
# Run launcher Rust tests with keyring-daemon-safe defaults.
#
# Why: under parallel test execution (`cargo test` default), the launcher's
# many keychain-touching tests overwhelm gnome-keyring-daemon on Linux
# (observed: SIGTRAP-crashes on 2026-05-08 and 2026-05-13, taking the
# user's SSH-agent integration down with it). Even with the in-crate
# `keychain_serialize_lock` mutex + 150ms `paced_call` rate limiter +
# 4-attempt retry-with-backoff, the daemon has been seen to die because
# `keyring::Entry::new()` probes and direct `keyring` crate calls in
# multiple test modules bypass the pacing layer.
#
# Single-threaded test execution sidesteps the issue entirely by ensuring
# at most one keyring call is in flight at any moment. Wall-clock cost on
# a 32-thread workstation: ~40s → ~3min. Acceptable for the safety win
# (no SIGTRAP, no Ubuntu auth-recovery dialog, no kg_sync flakes from
# scheduler contention).
#
# Usage:
#   scripts/test-keychain-safe.sh                # all lib tests
#   scripts/test-keychain-safe.sh secrets_cmd::  # filter by name
#   scripts/test-keychain-safe.sh --release      # forwarded to cargo
#
# CI: `.github/workflows/ci.yml` invokes this script for the Rust job.
# Local: run this instead of `cargo test --lib` whenever the keychain is
# involved (any test that touches `secrets::*`, `set_secret_v2`,
# `register_github_pat`, or `refresh_env_after_user_secret_change`).

set -euo pipefail

# Resolve repo root from this script's path so the script is callable
# from any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST="$REPO_ROOT/launcher/src-tauri/Cargo.toml"

if [ ! -f "$MANIFEST" ]; then
    echo "[test-keychain-safe] FATAL: manifest not found at $MANIFEST" >&2
    exit 2
fi

# Forward args + always pin --test-threads=1. The `--` separator routes
# the threads flag to the test binary, not to cargo itself.
echo "[test-keychain-safe] cargo test --lib --manifest-path $MANIFEST -- --test-threads=1 $*"
exec cargo test --lib --manifest-path "$MANIFEST" "$@" -- --test-threads=1
