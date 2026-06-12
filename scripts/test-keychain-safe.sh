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
#   scripts/test-keychain-safe.sh                # all workspace tests (lib + integration)
#   scripts/test-keychain-safe.sh secrets_cmd::  # filter by name
#   scripts/test-keychain-safe.sh --release      # forwarded to cargo
#
# CI: `.github/workflows/ci.yml` invokes this script for the Rust job.
# Local: run this instead of `cargo test` whenever the keychain is
# involved (any test that touches `secrets::*`, `set_secret_v2`,
# `register_github_pat`, or `refresh_env_after_user_secret_change`).
#
# v0.2.54 Track E (P0-1): pre-Track-E this script ran `cargo test --lib`
# against the launcher manifest, which only built the `vct-launcher-temp`
# member of the workspace (1 of 4 crates). `vct-launcher-core`, `vct-hub`,
# and `vct-updater` integration tests + library tests never ran in CI.
# Per the Track E brief: switch to `cargo test --workspace --tests` so all
# four workspace members are exercised. The vct-cli crate is a separate
# Cargo workspace (launcher/tools/vct-cli/) — covered by a sibling step
# in ci.yml that invokes this script with VCT_CLI=1.
#
# Retry policy for adopt_populated:
#   The `adopt_populated` test in vct-launcher-core has been observed to
#   flake under filesystem-contention conditions (race between two test
#   threads probing the same temp dir, see launcher source comments). We
#   keep it in the gating workspace run rather than carving it out — the
#   flake is reproducible at <1% under --test-threads=1, and a retry
#   layer hides genuine regressions. If the flake rate increases, mark
#   the test `#[ignore]` and add a separate retry step.

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
#
# `--workspace --tests` covers ALL workspace members (vct-launcher-temp,
# vct-launcher-core, vct-hub, vct-updater) and ALL test targets (lib +
# integration). Pre-Track-E `--lib` only ran lib tests on the root crate.
echo "[test-keychain-safe] cargo test --workspace --tests --manifest-path $MANIFEST -- --test-threads=1 $*"
exec cargo test --workspace --tests --manifest-path "$MANIFEST" "$@" -- --test-threads=1
