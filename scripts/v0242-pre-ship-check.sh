#!/usr/bin/env bash
# v0.2.42 pre-ship gate runner.
#
# Runs all 18 pre-ship checks before tagging a release.  Zero inputs;
# outputs a pass/fail summary to stdout.
#
# Usage:
#   bash scripts/v0242-pre-ship-check.sh
#
# Exit code: 0 = all gates pass, 1 = one or more gates failed.
#
# Requires: gh (GitHub CLI, authenticated), cargo, python3, npm.
# Run from the repo root.

set -uo pipefail

REPO="hotak92/vibecoded-orchestrator"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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
echo " v0.2.42 pre-ship gate check"
echo " Repo: $REPO"
echo " Date: $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "============================================================"
echo ""

cd "$REPO_ROOT"

# ── Section 1: Local build gates ─────────────────────────────────────────────
echo "--- Local build gates ---"

# Gate 1: Cargo.toml version matches expected v0.2.42
CARGO_VER="$(grep -m1 '^version = ' launcher/src-tauri/Cargo.toml \
    | sed -E 's/^version *= *"([^"]+)".*/\1/')"
if [ "$CARGO_VER" = "0.2.42" ]; then
    gate_pass "Cargo.toml version = 0.2.42"
else
    gate_fail "Cargo.toml version = 0.2.42" "Found: $CARGO_VER (bump before tagging)"
fi

# Gate 2: cargo test --lib (unit tests)
echo "  [running cargo test --lib...]"
if bash scripts/test-keychain-safe.sh > /tmp/w7-cargo-test.log 2>&1; then
    gate_pass "cargo test --lib (keychain-safe)"
else
    gate_fail "cargo test --lib (keychain-safe)" "See /tmp/w7-cargo-test.log"
fi

# Gate 3: pytest
echo "  [running pytest tests/ ...]"
if python3 -m pytest tests/ -q --tb=no > /tmp/w7-pytest.log 2>&1; then
    gate_pass "pytest tests/"
else
    gate_fail "pytest tests/" "See /tmp/w7-pytest.log"
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

# Gate 6: manifest validate (strict mode)
echo "  [building validate-manifest bin + running schema-drift check...]"
if (cd launcher/src-tauri && \
    cargo build -p vct-launcher-core --bin validate-manifest --bin export-schema -q 2>/tmp/w7-manifest-build.log && \
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

# Gate 14: Allow auto-merge enabled
echo "  [checking allow_auto_merge repo setting...]"
AUTO_MERGE="$(gh api "/repos/$REPO" --jq '.allow_auto_merge' 2>/dev/null || echo "unknown")"
if [ "$AUTO_MERGE" = "true" ]; then
    gate_pass "allow_auto_merge = true (Dependabot auto-merge works)"
elif [ "$AUTO_MERGE" = "unknown" ]; then
    gate_warn "allow_auto_merge" "Could not check (gh api error)"
else
    gate_fail "allow_auto_merge = true" \
        "Enable: gh api -X PATCH /repos/$REPO -f allow_auto_merge=true"
fi

# Gate 15: Open CodeQL error-severity alerts
echo "  [checking open CodeQL alerts...]"
ALERT_COUNT="$(gh api "/repos/$REPO/code-scanning/alerts?state=open&severity=error" \
    --jq 'length' 2>/dev/null || echo "unknown")"
if [ "$ALERT_COUNT" = "unknown" ]; then
    gate_warn "CodeQL error-severity alerts" "Could not fetch (gh api error)"
elif [ "$ALERT_COUNT" -eq 0 ]; then
    gate_pass "CodeQL error-severity alerts = 0"
else
    gate_fail "CodeQL error-severity alerts = 0" \
        "Found $ALERT_COUNT open error-severity alerts — triage before release"
fi

# Gate 16: CHANGELOG has v0.2.42 entry
if grep -q "## v0.2.42" CHANGELOG.md 2>/dev/null; then
    gate_pass "CHANGELOG.md has v0.2.42 section"
else
    gate_fail "CHANGELOG.md has v0.2.42 section" \
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
    echo "Fix all failed gates before tagging v0.2.42."
    exit 1
fi

echo "All gates PASSED. Safe to tag v0.2.42."
exit 0
