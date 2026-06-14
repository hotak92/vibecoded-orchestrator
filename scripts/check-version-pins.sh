#!/usr/bin/env bash
# check-version-pins.sh — fast, standalone version-pin consistency gate.
#
# v0.2.57: the orchestrator's version lives in a Cargo workspace + npm +
# Tauri + the module manifest + the Python package. Before v0.2.57 these
# drifted silently (the hub crate sat at 0.2.54 while the launcher shipped
# 0.2.56, so /health reported the wrong version). This gate asserts every
# pin agrees with the canonical source (pyproject.toml) AND that the
# workspace-inheriting Rust crates have NOT re-grown a literal version.
#
# Designed to run in CI in ~1s (no build, no test suite) — UNLIKE the full
# `pre-ship-check.sh`, which also runs cargo + pytest. `pre-ship-check.sh`
# SOURCES this file so the pin logic lives in exactly one place.
#
# Usage:
#   scripts/check-version-pins.sh                # version from pyproject.toml
#   EXPECTED_VERSION=0.2.57 scripts/check-version-pins.sh
#   scripts/check-version-pins.sh 0.2.57
#
# Exit 0 = all pins agree. Exit 1 = drift (prints each offending file).
# Exit 2 = could not resolve the expected version.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ── Resolve expected version: $1 > $EXPECTED_VERSION > pyproject.toml ──────
_EXPECTED=""
if [ "$#" -ge 1 ] && [ -n "${1:-}" ]; then
    _EXPECTED="$1"
elif [ -n "${EXPECTED_VERSION:-}" ]; then
    _EXPECTED="$EXPECTED_VERSION"
else
    _EXPECTED="$(grep -m1 -E '^version = ' pyproject.toml 2>/dev/null \
        | sed -E 's/^version *= *"([^"]+)".*/\1/')"
fi
if [ -z "$_EXPECTED" ]; then
    echo "ERROR: could not resolve expected version (pyproject.toml unreadable?)" >&2
    exit 2
fi

# ── Pin definitions (the canonical lists) ─────────────────────────────────
# LITERAL pins — files carrying `version = "X"` (TOML) or `"version": "X"`
# (JSON) that must equal the release. The workspace ROOT Cargo.toml stays
# here (its [workspace.package] literal is checked specially below).
VERSION_PIN_FILES=(
    "pyproject.toml"
    "vct-module.json"
    "launcher/package.json"
    "launcher/package-lock.json"
    "launcher/src-tauri/Cargo.toml"
    "launcher/src-tauri/tauri.conf.json"
)
# Crates that MUST inherit the workspace version (`version.workspace = true`,
# no literal). A literal here is the per-crate drift v0.2.57 eliminated.
WORKSPACE_INHERITED_CRATES=(
    "launcher/src-tauri/vct-hub/Cargo.toml"
    "launcher/src-tauri/vct-updater/Cargo.toml"
    "launcher/src-tauri/vct-launcher-core/Cargo.toml"
)

# vcheck_run_pins <expected> : populate the global `VCHECK_FAILURES` array
# with any pin/inherit mismatch. Returns 0 if all good, 1 otherwise. This
# is the SINGLE source of pin-checking logic, sourced by pre-ship-check.sh.
vcheck_run_pins() {
    local expected="$1"
    VCHECK_FAILURES=()

    local f got
    for f in "${VERSION_PIN_FILES[@]}"; do
        if [ ! -f "$f" ]; then
            VCHECK_FAILURES+=("$f (missing)")
            continue
        fi
        if ! grep -q "\"version\": \"$expected\"" "$f" 2>/dev/null \
            && ! grep -q "^version = \"$expected\"" "$f" 2>/dev/null; then
            # N2: allow leading whitespace so the "got:" hint works for
            # indented JSON pins (e.g. vct-module.json's `  "version":`).
            got="$(grep -m1 -E '^[[:space:]]*(version = |"version": )' "$f" \
                | sed -E 's/.*"([^"]+)".*/\1/' | head -c 80)"
            VCHECK_FAILURES+=("$f (got: ${got:-<none>})")
        fi
    done

    # C1 (review fix): package-lock.json carries the root version in TWO
    # places — top-level "version" AND packages[""]["version"] (npm keeps
    # them in sync). The grep loop above only matches the FIRST occurrence
    # (the root one), so a stale packages[""] would slip through — exactly
    # the field bump-version.sh was built to handle. Check it explicitly,
    # JSON-aware, so the GATE closes the same gap the bump script does.
    local pkg_lock="launcher/package-lock.json"
    if [ -f "$pkg_lock" ]; then
        if command -v python3 >/dev/null 2>&1; then
            local lock_report
            lock_report="$(python3 - "$pkg_lock" "$expected" <<'PY'
import json, sys
p, expected = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(p))
except Exception as e:
    print(f"unparseable ({e})"); sys.exit(0)
bad = []
root = d.get("version")
if root != expected:
    bad.append(f'root="{root}"')
pkg0 = d.get("packages", {}).get("", {}).get("version")
if pkg0 != expected:
    bad.append(f'packages[""]="{pkg0}"')
if bad:
    print("; ".join(bad))
PY
)"
            if [ -n "$lock_report" ]; then
                VCHECK_FAILURES+=("$pkg_lock ($lock_report, expected $expected)")
            fi
        fi
        # (If python3 is absent the grep-loop above still catches root drift;
        # the JSON-aware packages[""] check is best-effort on top of it.)
    fi

    # Workspace ROOT Cargo.toml: the literal must live under
    # [workspace.package] (not a stray top-level pin), so dropping
    # workspace inheritance is caught.
    local ws_root="launcher/src-tauri/Cargo.toml"
    if ! awk '
            /^\[workspace\.package\]/ { in_wp = 1; next }
            in_wp && /^version *= *"/ { print; exit }
            /^\[/ && in_wp { in_wp = 0 }
        ' "$ws_root" 2>/dev/null | grep -q "\"$expected\""; then
        VCHECK_FAILURES+=("$ws_root ([workspace.package] version != $expected)")
    fi

    # Inherited crates: must declare `version.workspace = true`, no literal.
    local crate
    for crate in "${WORKSPACE_INHERITED_CRATES[@]}"; do
        if [ ! -f "$crate" ]; then
            VCHECK_FAILURES+=("$crate (missing)")
            continue
        fi
        if ! grep -qE '^version\.workspace *= *true' "$crate"; then
            VCHECK_FAILURES+=("$crate (no 'version.workspace = true')")
        elif grep -qE '^version *= *"[0-9]' "$crate"; then
            VCHECK_FAILURES+=("$crate (has a LITERAL version — should inherit)")
        fi
    done

    [ "${#VCHECK_FAILURES[@]}" -eq 0 ]
}

# ── When EXECUTED directly (not sourced), run + report. ───────────────────
# `${BASH_SOURCE[0]}` == `$0` only when this file is the entrypoint.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "Version-pin check — expected v$_EXPECTED"
    if vcheck_run_pins "$_EXPECTED"; then
        echo "OK — all ${#VERSION_PIN_FILES[@]} literal pins + workspace-package literal agree, and ${#WORKSPACE_INHERITED_CRATES[@]} crates inherit (version.workspace = true)."
        exit 0
    else
        echo "FAIL — version drift detected:" >&2
        for m in "${VCHECK_FAILURES[@]}"; do echo "  - $m" >&2; done
        echo "" >&2
        echo "Fix: run \`scripts/bump-version.sh $_EXPECTED\` (single-command bump), then re-run." >&2
        exit 1
    fi
fi
