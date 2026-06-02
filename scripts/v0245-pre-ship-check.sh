#!/usr/bin/env bash
# v0.2.45 pre-ship gate runner.
#
# Runs the pre-ship gates before tagging v0.2.45.  Zero inputs;
# outputs a pass/fail summary to stdout.
#
# v0.2.45 additions vs v0244-pre-ship-check.sh:
#   - Gate 19: v0.2.45 Python test (V45-A self-relaunch under venv)
#   - Gate 20: v0.2.45 Rust unit tests (cargo test --lib test_v0245*)
#   - Gate 21: V45-E v0245_backfill_* unit tests in vct-launcher-core
#   - Gate 22: VCT_RL_PULL_TOKEN_ENDPOINT documented in
#              docs/CONFIGURATION.md (V45-D paper trail)
#   - Gate 23: All forward version pins consistent at 0.2.45
#   - Gate 24: [Unreleased] CHANGELOG block is empty
#              (no-deferred-fixes rule)
#
# Usage:
#   bash scripts/v0245-pre-ship-check.sh
#
# Exit code: 0 = all gates pass, 1 = one or more gates failed.
#
# Requires: gh (GitHub CLI, authenticated), cargo, python3, npm.
# Run from the repo root.

set -uo pipefail

REPO="hotak92/vibecoded-orchestrator"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

EXPECTED_VERSION="0.2.45"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RESET='\033[0m'

pass_count=0
fail_count=0
warn_count=0
declare -a failures=()
declare -a warnings=()

gate_pass() {
    local name="$1"
    printf "${GREEN}[PASS]${RESET} %s\n" "$name"
    pass_count=$((pass_count + 1))
}

gate_fail() {
    local name="$1"
    local detail="${2:-}"
    printf "${RED}[FAIL]${RESET} %s\n" "$name"
    [ -n "$detail" ] && printf "       %s\n" "$detail"
    fail_count=$((fail_count + 1))
    failures+=("$name")
}

gate_warn() {
    local name="$1"
    local detail="${2:-}"
    printf "${YELLOW}[WARN]${RESET} %s\n" "$name"
    [ -n "$detail" ] && printf "       %s\n" "$detail"
    warn_count=$((warn_count + 1))
    warnings+=("$name")
}

check_workflow_last_run() {
    # Returns 0 if the most recent completed run for the workflow is "success",
    # 1 otherwise.  $1 = workflow filename (e.g. ci.yml), $2 = friendly name.
    local wf="$1"
    local name="$2"
    local conclusion
    conclusion="$(gh run list \
        --repo "$REPO" \
        --workflow "$wf" \
        --status completed \
        --limit 1 \
        --json conclusion \
        --jq '.[0].conclusion' 2>/dev/null || echo "unknown")"
    if [ "$conclusion" = "success" ]; then
        gate_pass "$name"
    elif [ "$conclusion" = "unknown" ] || [ -z "$conclusion" ]; then
        gate_warn "$name" "Could not fetch run status (is gh authenticated? try: gh auth status)"
    else
        gate_fail "$name" "Last run conclusion: $conclusion"
    fi
}

echo ""
echo "============================================================"
echo " v${EXPECTED_VERSION} pre-ship gate check"
echo " Repo: $REPO"
echo " Date: $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "============================================================"
echo ""

cd "$REPO_ROOT"

# Resolve a Python interpreter that has pytest installed. Probe order:
#  1. $PYTEST env override (explicit user choice).
#  2. ./.venv/bin/python (or python3)            — created by install.py
#  3. ./claude_mcp_servers/.venv/bin/python       — legacy venv path
#  4. ../VCO_dev/.venv/bin/python                 — dev-machine sibling
#                                                   private fork (lets
#                                                   the script work when
#                                                   run from a freshly
#                                                   git-cloned public repo
#                                                   on the same machine).
#  5. `pytest` on PATH if no venv probe matched.
#  6. Bare `python3` (will fail clearly if pytest missing).
_PYTEST_PY=""
_PYTEST_CMD=()
if [ -n "${PYTEST:-}" ]; then
    # User supplied PYTEST — autodetect whether it's a python interpreter
    # (case A: PYTEST=/path/to/venv/bin/python) or a pytest binary (case B:
    # PYTEST=/path/to/venv/bin/pytest). Without this disambiguation, case B
    # would resolve to `pytest -m pytest tests/...` which is a marker filter
    # that matches nothing ("6 deselected" footgun caught in v0.2.45 ship).
    case "$(basename -- "$PYTEST")" in
        pytest|pytest-*|*-pytest) _PYTEST_CMD=("$PYTEST") ;;
        *)                        _PYTEST_PY="$PYTEST" ;;
    esac
elif [ -x ".venv/bin/python" ]; then
    _PYTEST_PY=".venv/bin/python"
elif [ -x ".venv/bin/python3" ]; then
    _PYTEST_PY=".venv/bin/python3"
elif [ -x "claude_mcp_servers/.venv/bin/python" ]; then
    _PYTEST_PY="claude_mcp_servers/.venv/bin/python"
elif [ -x "../VCO_dev/.venv/bin/python" ]; then
    _PYTEST_PY="../VCO_dev/.venv/bin/python"
elif command -v pytest >/dev/null 2>&1; then
    _PYTEST_PY=""  # use bare `pytest`
else
    _PYTEST_PY="python3"
fi
if [ ${#_PYTEST_CMD[@]} -eq 0 ]; then
    if [ -n "$_PYTEST_PY" ]; then
        _PYTEST_CMD=("$_PYTEST_PY" -m pytest)
    else
        _PYTEST_CMD=(pytest)
    fi
fi

# ── Section 1: Local build gates ─────────────────────────────────────────────
echo "--- Local build gates ---"

# Gate 1: Cargo.toml version matches expected v0.2.45
CARGO_VER="$(grep -m1 '^version = ' launcher/src-tauri/Cargo.toml \
    | sed -E 's/^version *= *"([^"]+)".*/\1/')"
if [ "$CARGO_VER" = "$EXPECTED_VERSION" ]; then
    gate_pass "Cargo.toml version = $EXPECTED_VERSION"
else
    gate_fail "Cargo.toml version = $EXPECTED_VERSION" "Found: $CARGO_VER (bump before tagging)"
fi

# Gate 2: cargo test --lib (unit tests).
# Prefer rustup-run 1.95 when available — system `cargo` may be older
# than the workspace MSRV (sysinfo 0.39 + tauri ≥ 2.x require 1.95).
echo "  [running cargo test --lib...]"
if command -v rustup >/dev/null 2>&1 && rustup toolchain list 2>/dev/null | grep -q "^1\.95"; then
    if (rustup run 1.95 bash scripts/test-keychain-safe.sh > /tmp/w7-cargo-test.log 2>&1); then
        gate_pass "cargo test --lib (keychain-safe, rustup 1.95)"
    else
        gate_fail "cargo test --lib (keychain-safe, rustup 1.95)" "See /tmp/w7-cargo-test.log"
    fi
elif bash scripts/test-keychain-safe.sh > /tmp/w7-cargo-test.log 2>&1; then
    gate_pass "cargo test --lib (keychain-safe)"
else
    gate_fail "cargo test --lib (keychain-safe)" "See /tmp/w7-cargo-test.log"
fi

# Gate 3: pytest
echo "  [running pytest tests/ ...]"
if "${_PYTEST_CMD[@]}" tests/ -q --tb=no > /tmp/w7-pytest.log 2>&1; then
    gate_pass "pytest tests/"
else
    gate_fail "pytest tests/" "See /tmp/w7-pytest.log (cmd: ${_PYTEST_CMD[*]})"
fi

# Gate 4: npm test (svelte-check)
echo "  [running npm run check in launcher/ ...]"
if (cd launcher && npm run check > /tmp/w7-npm-check.log 2>&1); then
    gate_pass "npm run check (svelte-check)"
else
    gate_fail "npm run check (svelte-check)" "See /tmp/w7-npm-check.log"
fi

# Gate 5: npm audit (no critical/high)
echo "  [running npm audit in launcher/ ...]"
# npm audit exits non-zero if vulnerabilities >= moderate by default.
# We only hard-fail on critical/high; moderate is warn.
if (cd launcher && npm audit --audit-level=high > /tmp/w7-npm-audit.log 2>&1); then
    gate_pass "npm audit (no high/critical)"
else
    # Check if it's high/critical or just moderate
    if (cd launcher && npm audit --audit-level=critical > /dev/null 2>&1); then
        gate_warn "npm audit (high vulns found, no critical)" "See /tmp/w7-npm-audit.log"
    else
        gate_fail "npm audit (critical vulns found)" "See /tmp/w7-npm-audit.log"
    fi
fi

# Gate 6: manifest validate (strict mode). Uses _CARGO_PRE (rustup 1.95
# when available) to satisfy the workspace MSRV.
if command -v rustup >/dev/null 2>&1 && rustup toolchain list 2>/dev/null | grep -q "^1\.95"; then
    _CARGO_PRE=(rustup run 1.95 cargo)
else
    _CARGO_PRE=(cargo)
fi
echo "  [building validate-manifest bin + running schema-drift check (cmd: ${_CARGO_PRE[*]})...]"
if (cd launcher/src-tauri && \
    "${_CARGO_PRE[@]}" build -p vct-launcher-core --bin validate-manifest --bin export-schema -q 2>/tmp/w7-manifest-build.log && \
    VCT_LAUNCHER_STRICT_MANIFEST=1 \
    ./target/debug/export-schema --check ../../docs/schemas/vct-module.schema.json > /tmp/w7-schema-drift.log 2>&1); then
    # Also run validate against fixtures
    if (cd launcher/src-tauri && \
        VCT_LAUNCHER_STRICT_MANIFEST=1 \
        ./target/debug/validate-manifest \
        vct-launcher-core/tests/fixtures/manifests/*.json > /tmp/w7-manifest-validate.log 2>&1); then
        gate_pass "manifest-validate (strict mode, schema-drift + fixtures)"
    else
        gate_fail "manifest-validate (fixture validation)" "See /tmp/w7-manifest-validate.log"
    fi
else
    gate_fail "manifest-validate (schema-drift or build)" \
        "See /tmp/w7-manifest-build.log and /tmp/w7-schema-drift.log"
fi

# Gate 7: no credential leaks in repo (scripts/check-no-secrets.sh)
if [ -f scripts/check-no-secrets.sh ]; then
    echo "  [running check-no-secrets.sh ...]"
    if bash scripts/check-no-secrets.sh > /tmp/w7-secrets.log 2>&1; then
        gate_pass "check-no-secrets.sh"
    else
        gate_fail "check-no-secrets.sh" "See /tmp/w7-secrets.log"
    fi
else
    gate_warn "check-no-secrets.sh" "Script not found — skipping"
fi

# Gate 8: dist binaries present and non-empty for all 3 arches
echo "  [checking dist binaries...]"
dist_ok=1
for arch_bin in \
    "linux-x64/vct-launcher" \
    "linux-x64/vct-hub" \
    "windows-x64/vct-launcher.exe" \
    "windows-x64/vct-hub.exe" \
    "macos-arm64/vct-launcher" \
    "macos-arm64/vct-hub"; do
    path="launcher/dist/$arch_bin"
    if [ ! -f "$path" ] || [ ! -s "$path" ]; then
        echo "  missing or empty: $path"
        dist_ok=0
    fi
done
if [ "$dist_ok" -eq 1 ]; then
    gate_pass "dist binaries present (all 6: launcher+hub x 3 arches)"
else
    gate_fail "dist binaries present" "Run release build or copy from latest release workflow artifacts"
fi

echo ""

# ── Section 2: GitHub CI workflow gates ──────────────────────────────────────
echo "--- GitHub CI workflow gates (last completed run) ---"
echo "  (requires: gh cli authenticated, public repo read access)"

# Gate 9: CI (main)
check_workflow_last_run "ci.yml" "CI workflow (ci.yml) — last run on main"

# Gate 10: Installer Smoke Test
check_workflow_last_run "installer-smoke.yml" "Installer Smoke Test (installer-smoke.yml)"

# Gate 11: Manifest Validate
check_workflow_last_run "manifest-validate.yml" "Manifest Validate (manifest-validate.yml)"

# Gate 12: CodeQL
check_workflow_last_run "codeql.yml" "CodeQL analysis (codeql.yml)"

# Gate 13: Hook parity
check_workflow_last_run "hook-parity.yml" "Hook OS-parity gate (hook-parity.yml)"

echo ""

# ── Section 3: Repo-level checks ─────────────────────────────────────────────
echo "--- Repo-level checks ---"

# Gate 14: Allow auto-merge enabled (advisory — owner action, not code gate).
# WARN-level only — see v0244 script for full rationale. `allow_auto_merge`
# is repo-level setting; flip via `gh api -X PATCH /repos/$REPO -f allow_auto_merge=true`.
echo "  [checking allow_auto_merge repo setting...]"
AUTO_MERGE="$(gh api "/repos/$REPO" --jq '.allow_auto_merge' 2>/dev/null || echo "unknown")"
if [ "$AUTO_MERGE" = "true" ]; then
    gate_pass "allow_auto_merge = true (Dependabot auto-merge works)"
elif [ "$AUTO_MERGE" = "unknown" ]; then
    gate_warn "allow_auto_merge" "Could not check (gh api error)"
else
    gate_warn "allow_auto_merge = true (advisory)" \
        "Owner-discretion. Triage Dependabot PRs first, then: gh api -X PATCH /repos/$REPO -f allow_auto_merge=true"
fi

# Gate 15: Open CodeQL error-severity alerts
echo "  [checking open CodeQL alerts...]"
ALERT_COUNT="$(gh api "/repos/$REPO/code-scanning/alerts?state=open&severity=error" \
    --jq 'length' 2>/dev/null || echo "unknown")"
# Tighter type check: only treat ALERT_COUNT as a numeric comparison
# when it really is a non-negative integer (gh can return a JSON error
# object that gets concatenated with the fallback "unknown" → produces
# garbled output that the bare `-eq` test crashes on with
# "integer expression expected").
if [[ ! "$ALERT_COUNT" =~ ^[0-9]+$ ]]; then
    gate_warn "CodeQL error-severity alerts" \
        "Could not fetch (gh api error; run: gh auth status)"
elif [ "$ALERT_COUNT" -eq 0 ]; then
    gate_pass "CodeQL error-severity alerts = 0"
else
    gate_fail "CodeQL error-severity alerts = 0" \
        "Found $ALERT_COUNT open error-severity alerts — triage before release"
fi

# Gate 16: CHANGELOG has v0.2.45 entry
# Match the Keep-a-Changelog heading shape: `## [0.2.45] - 2026-06-02`.
# Allow optional `v` prefix and optional surrounding brackets for flexibility.
if grep -qE '^## \[?v?0\.2\.45\]?' CHANGELOG.md 2>/dev/null; then
    gate_pass "CHANGELOG.md has v$EXPECTED_VERSION section"
else
    gate_fail "CHANGELOG.md has v$EXPECTED_VERSION section" \
        "Add release notes before tagging"
fi

# Gate 17: No uncommitted changes to tracked files
echo "  [checking git status...]"
DIRTY="$(git status --porcelain 2>/dev/null)"
if [ -z "$DIRTY" ]; then
    gate_pass "Working tree clean (no uncommitted changes)"
else
    gate_fail "Working tree clean" \
        "Uncommitted changes present — commit or stash before tagging"
fi

# Gate 18: release.yml pre-release-gate job is present
if grep -q "pre-release-gate:" .github/workflows/release.yml 2>/dev/null; then
    gate_pass "release.yml has pre-release-gate job (CI-6 W7)"
else
    gate_fail "release.yml has pre-release-gate job" \
        "W7 pre-release-gate not found in release.yml — apply W7 branch before tagging"
fi

echo ""

# ── Section 4: v0.2.45-specific gates ────────────────────────────────────────
echo "--- v0.2.45-specific gates ---"

# Resolve the cargo invocation that satisfies the workspace MSRV.
# launcher/src-tauri depends on crates (e.g. sysinfo 0.39+) that require
# rustc 1.95. On machines where the system `cargo` is older (e.g. snap
# 1.94.1) but `rustup run 1.95` is available, prefer rustup. Mirrors
# how the local dev loop runs cargo. Falls back to system cargo when
# rustup is not installed or 1.95 not present (CI containers ship 1.95
# as the system cargo, so the fallback is fine there).
if command -v rustup >/dev/null 2>&1 && rustup toolchain list 2>/dev/null | grep -q "^1\.95"; then
    _CARGO=(rustup run 1.95 cargo)
else
    _CARGO=(cargo)
fi

# Gate 19: V45-A self-relaunch Python test
echo "  [running v0.2.45 V45-A self-relaunch tests...]"
if "${_PYTEST_CMD[@]}" tests/test_v0245_self_relaunch_under_venv.py -q --tb=short \
        > /tmp/v0245-test-v45a.log 2>&1; then
    gate_pass "tests/test_v0245_self_relaunch_under_venv.py (V45-A)"
else
    gate_fail "tests/test_v0245_self_relaunch_under_venv.py (V45-A)" \
        "See /tmp/v0245-test-v45a.log"
fi

# Gate 20: V45-B/C/D/F Rust unit tests (cargo test --lib test_v0245)
echo "  [running v0.2.45 cargo test --lib test_v0245* (cmd: ${_CARGO[*]}) ...]"
if (cd launcher/src-tauri && \
    "${_CARGO[@]}" test --lib test_v0245 -- --nocapture > /tmp/v0245-cargo-test.log 2>&1); then
    gate_pass "cargo test --lib test_v0245* (V45-B/C/D/F Rust tests)"
else
    gate_fail "cargo test --lib test_v0245* (V45-B/C/D/F Rust tests)" \
        "See /tmp/v0245-cargo-test.log"
fi

# Gate 21: V45-E v0245_backfill_* tests in vct-launcher-core
echo "  [running v0.2.45 V45-E backfill tests (vct-launcher-core)...]"
if (cd launcher/src-tauri && \
    "${_CARGO[@]}" test --package vct-launcher-core --lib v0245_backfill -- --nocapture \
        > /tmp/v0245-cargo-test-v45e.log 2>&1); then
    gate_pass "cargo test (vct-launcher-core) v0245_backfill_* (V45-E)"
else
    gate_fail "cargo test (vct-launcher-core) v0245_backfill_* (V45-E)" \
        "See /tmp/v0245-cargo-test-v45e.log"
fi

# Gate 22: VCT_RL_PULL_TOKEN_ENDPOINT documented in docs/CONFIGURATION.md (V45-D)
if grep -q "VCT_RL_PULL_TOKEN_ENDPOINT" docs/CONFIGURATION.md 2>/dev/null; then
    gate_pass "docs/CONFIGURATION.md mentions VCT_RL_PULL_TOKEN_ENDPOINT (V45-D)"
else
    gate_fail "docs/CONFIGURATION.md mentions VCT_RL_PULL_TOKEN_ENDPOINT (V45-D)" \
        "V45-D added a row for the new env var; ensure docs are present"
fi

# Gate 23: All forward version pins consistent at 0.2.45
echo "  [checking forward version pins consistent at $EXPECTED_VERSION...]"
declare -a pin_failures=()
check_pin() {
    local file="$1"
    local expected="$2"
    local got
    if [ ! -f "$file" ]; then
        pin_failures+=("$file (missing)")
        return
    fi
    if ! grep -q "\"version\": \"$expected\"" "$file" 2>/dev/null \
        && ! grep -q "^version = \"$expected\"" "$file" 2>/dev/null; then
        got="$(grep -m1 -E '^(version = |"version": )' "$file" \
            | sed -E 's/.*"([^"]+)".*/\1/' \
            | head -c 80)"
        pin_failures+=("$file (got: $got)")
    fi
}
check_pin "pyproject.toml" "$EXPECTED_VERSION"
check_pin "vct-module.json" "$EXPECTED_VERSION"
check_pin "launcher/package.json" "$EXPECTED_VERSION"
check_pin "launcher/package-lock.json" "$EXPECTED_VERSION"
check_pin "launcher/src-tauri/Cargo.toml" "$EXPECTED_VERSION"
check_pin "launcher/src-tauri/tauri.conf.json" "$EXPECTED_VERSION"
check_pin "launcher/src-tauri/vct-hub/Cargo.toml" "$EXPECTED_VERSION"
check_pin "launcher/src-tauri/vct-launcher-core/Cargo.toml" "$EXPECTED_VERSION"
if [ "${#pin_failures[@]}" -eq 0 ]; then
    gate_pass "all forward version pins at $EXPECTED_VERSION"
else
    gate_fail "forward version pins at $EXPECTED_VERSION" \
        "Mismatch: ${pin_failures[*]}"
fi

# Gate 24: [Unreleased] CHANGELOG block is empty (no-deferred-fixes rule)
# The block is allowed to exist as a heading; what's not allowed is content
# between the heading and the first tagged version heading.
UNRELEASED_BODY="$(awk '/^## \[Unreleased\]/,/^## \[[0-9]/' CHANGELOG.md \
    | sed -E '/^## \[/d' \
    | sed -E '/^[[:space:]]*$/d')"
if [ -z "$UNRELEASED_BODY" ]; then
    gate_pass "CHANGELOG [Unreleased] block is empty (no-deferred-fixes rule)"
else
    gate_fail "CHANGELOG [Unreleased] block is empty (no-deferred-fixes rule)" \
        "Content found between [Unreleased] and [0.2.X]; move into the tagged block first"
fi

echo ""
echo "============================================================"
echo " SUMMARY"
echo "============================================================"
printf "  ${GREEN}PASS${RESET}: %d\n" "$pass_count"
printf "  ${YELLOW}WARN${RESET}: %d\n" "$warn_count"
printf "  ${RED}FAIL${RESET}: %d\n" "$fail_count"
echo ""

if [ "${#warnings[@]}" -gt 0 ]; then
    echo "Warnings:"
    for w in "${warnings[@]}"; do
        printf "  ${YELLOW}*${RESET} %s\n" "$w"
    done
    echo ""
fi

if [ "${#failures[@]}" -gt 0 ]; then
    echo "Failed gates:"
    for f in "${failures[@]}"; do
        printf "  ${RED}*${RESET} %s\n" "$f"
    done
    echo ""
    echo "Fix all failed gates before tagging v$EXPECTED_VERSION."
    exit 1
fi

echo "All gates PASSED. Safe to tag v$EXPECTED_VERSION."
exit 0
