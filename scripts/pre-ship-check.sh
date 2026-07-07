#!/usr/bin/env bash
# Generic pre-ship gate runner — no version baked in.
#
# Runs the pre-ship gates before tagging the next release. Zero inputs
# by default; outputs a pass/fail summary to stdout.
#
# Origin (2026-06-05, post-v0.2.47):
#   v0.2.47 shipped with vct-module.json::version still at 0.2.46 because
#   the pre-ship-check script that would have caught it (v0246-pre-ship-
#   check.sh) was tied to the specific version "0.2.46". Without a v0247-
#   pre-ship-check.sh being authored, the version-pin gate didn't run at
#   all. The launcher then deadlocked the update flow at 300s with a
#   misleading "still building" modal whenever it saw on_disk > source.
#
# Fix design (v0.2.48):
#   - One script, one canonical name (this file).
#   - EXPECTED_VERSION derived from pyproject.toml at runtime (single
#     source of truth) — or overridden via $1 / $EXPECTED_VERSION for
#     testing.
#   - The version-pin lists + checking logic live in ONE place,
#     `scripts/check-version-pins.sh` (also the per-push CI `version-pins`
#     job). Section 4 sources it and calls `vcheck_run_pins`. Add/remove a
#     pinned file THERE, not here.
#   - Version-specific test-file presence checks are intentionally NOT
#     re-introduced — the `pytest tests/` and `cargo test --lib` gates
#     already exercise them; tying gate-presence to a particular release
#     name is what we're moving away from.
#
# Usage:
#   bash scripts/pre-ship-check.sh                  # auto-detect from pyproject.toml
#   bash scripts/pre-ship-check.sh 0.2.49            # override version
#   EXPECTED_VERSION=0.2.49 bash scripts/pre-ship-check.sh
#
# Exit code: 0 = all gates pass, 1 = one or more gates failed.
#
# Requires: gh (GitHub CLI, authenticated), cargo, python3, npm.
# Run from the repo root.

set -uo pipefail

REPO="hotak92/vibecoded-orchestrator"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Resolve EXPECTED_VERSION ───────────────────────────────────────────
# Priority: $1 (argv) > $EXPECTED_VERSION (env) > pyproject.toml.
# pyproject.toml is the canonical version source for the orchestrator
# (the package definition consumed by `pip install .` + every other
# manifest pin in the repo trails it). Single source of truth means the
# script can never disagree with the actual release.
EXPECTED_VERSION=""
if [ "$#" -ge 1 ] && [ -n "$1" ]; then
    EXPECTED_VERSION="$1"
elif [ -n "${EXPECTED_VERSION:-}" ]; then
    : # already set in env
else
    EXPECTED_VERSION="$(grep -m1 -E '^version = ' "$REPO_ROOT/pyproject.toml" \
        | sed -E 's/^version *= *"([^"]+)".*/\1/')"
fi
if [ -z "$EXPECTED_VERSION" ]; then
    echo "ERROR: could not resolve EXPECTED_VERSION (pyproject.toml unreadable?)" >&2
    exit 2
fi

# ── Canonical version-pin lists ─────────────────────────────────────────
# v0.2.57: the pin lists (VERSION_PIN_FILES + WORKSPACE_INHERITED_CRATES)
# AND the checking logic (vcheck_run_pins) now live in ONE place:
# scripts/check-version-pins.sh. Section 4 below sources that file and
# calls vcheck_run_pins, so the per-push CI `version-pins` job and this
# release-time gate run identical logic and can never drift. (Don't
# re-declare the lists here — that would shadow the sourced canonical
# copies and reintroduce the multi-source-of-truth class this fixes.)

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
    # Returns 0 if the most recent completed run of the workflow ON MAIN
    # is "success", 1 otherwise.  $1 = workflow filename (e.g. ci.yml),
    # $2 = friendly name.
    #
    # v0.2.49 fix: pre-fix this queried the most-recent completed run
    # across ALL branches — which meant a failing Dependabot PR (e.g.
    # `dependabot/npm_and_yarn/launcher/vite-8.0.14`) would block the
    # tag even though main itself was green. The release-discipline
    # rule applies to the release branch + main, not "any branch in
    # the repo". Filter by `--branch main` so the check matches the
    # rule's actual scope.
    local wf="$1"
    local name="$2"
    local conclusion
    conclusion="$(gh run list \
        --repo "$REPO" \
        --workflow "$wf" \
        --branch main \
        --status completed \
        --limit 1 \
        --json conclusion \
        --jq '.[0].conclusion' 2>/dev/null || echo "unknown")"
    if [ "$conclusion" = "success" ]; then
        gate_pass "$name"
    elif [ "$conclusion" = "unknown" ] || [ -z "$conclusion" ]; then
        gate_warn "$name" "Could not fetch run status (is gh authenticated? try: gh auth status)"
    else
        gate_fail "$name" "Last run conclusion on main: $conclusion"
    fi
}

echo ""
echo "============================================================"
echo " Pre-ship gate check — v${EXPECTED_VERSION}"
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

# Resolve the cargo invocation that satisfies the workspace MSRV.
# launcher/src-tauri depends on crates (e.g. sysinfo 0.39+) that require
# rustc 1.95. On machines where the system `cargo` is older (e.g. snap
# 1.94.1) but `rustup run 1.95` is available, prefer rustup. Falls back
# to system cargo when rustup is not installed or 1.95 not present (CI
# containers ship 1.95 as the system cargo, so the fallback is fine).
if command -v rustup >/dev/null 2>&1 && rustup toolchain list 2>/dev/null | grep -q "^1\.95"; then
    _CARGO=(rustup run 1.95 cargo)
else
    _CARGO=(cargo)
fi

# ── Section 1: Local build gates ─────────────────────────────────────────────
echo "--- Local build gates ---"

# Gate 1: Cargo.toml version matches expected
CARGO_VER="$(grep -m1 '^version = ' launcher/src-tauri/Cargo.toml \
    | sed -E 's/^version *= *"([^"]+)".*/\1/')"
if [ "$CARGO_VER" = "$EXPECTED_VERSION" ]; then
    gate_pass "Cargo.toml version = $EXPECTED_VERSION"
else
    gate_fail "Cargo.toml version = $EXPECTED_VERSION" "Found: $CARGO_VER (bump before tagging)"
fi

# Gate 2: cargo test --lib (unit tests).
echo "  [running cargo test --lib (keychain-safe)...]"
if bash scripts/test-keychain-safe.sh > /tmp/preship-cargo-test.log 2>&1; then
    gate_pass "cargo test --lib (keychain-safe)"
else
    gate_fail "cargo test --lib (keychain-safe)" "See /tmp/preship-cargo-test.log"
fi

# Gate 3: pytest
echo "  [running pytest tests/ ...]"
if "${_PYTEST_CMD[@]}" tests/ -q --tb=no > /tmp/preship-pytest.log 2>&1; then
    gate_pass "pytest tests/"
else
    gate_fail "pytest tests/" "See /tmp/preship-pytest.log (cmd: ${_PYTEST_CMD[*]})"
fi

# Gate 4: npm test (svelte-check)
echo "  [running npm run check in launcher/ ...]"
if (cd launcher && npm run check > /tmp/preship-npm-check.log 2>&1); then
    gate_pass "npm run check (svelte-check)"
else
    gate_fail "npm run check (svelte-check)" "See /tmp/preship-npm-check.log"
fi

# Gate 5: npm audit (no critical/high)
echo "  [running npm audit in launcher/ ...]"
# npm audit exits non-zero if vulnerabilities >= moderate by default.
# We only hard-fail on critical/high; moderate is warn.
if (cd launcher && npm audit --audit-level=high > /tmp/preship-npm-audit.log 2>&1); then
    gate_pass "npm audit (no high/critical)"
else
    # Check if it's high/critical or just moderate
    if (cd launcher && npm audit --audit-level=critical > /dev/null 2>&1); then
        gate_warn "npm audit (high vulns found, no critical)" "See /tmp/preship-npm-audit.log"
    else
        gate_fail "npm audit (critical vulns found)" "See /tmp/preship-npm-audit.log"
    fi
fi

# Gate 6: manifest validate (strict mode) + schema-drift check.
echo "  [building validate-manifest + schema-drift check (cmd: ${_CARGO[*]})...]"
if (cd launcher/src-tauri && \
    "${_CARGO[@]}" build -p vct-launcher-core --bin validate-manifest --bin export-schema -q 2>/tmp/preship-manifest-build.log && \
    VCT_LAUNCHER_STRICT_MANIFEST=1 \
    ./target/debug/export-schema --check ../../docs/schemas/vct-module.schema.json > /tmp/preship-schema-drift.log 2>&1); then
    if (cd launcher/src-tauri && \
        VCT_LAUNCHER_STRICT_MANIFEST=1 \
        ./target/debug/validate-manifest \
        vct-launcher-core/tests/fixtures/manifests/*.json > /tmp/preship-manifest-validate.log 2>&1); then
        gate_pass "manifest-validate (strict mode, schema-drift + fixtures)"
    else
        gate_fail "manifest-validate (fixture validation)" "See /tmp/preship-manifest-validate.log"
    fi
else
    gate_fail "manifest-validate (schema-drift or build)" \
        "See /tmp/preship-manifest-build.log and /tmp/preship-schema-drift.log"
fi

# Gate 7: no credential leaks in repo
if [ -f scripts/check-no-secrets.sh ]; then
    echo "  [running check-no-secrets.sh ...]"
    if bash scripts/check-no-secrets.sh > /tmp/preship-secrets.log 2>&1; then
        gate_pass "check-no-secrets.sh"
    else
        gate_fail "check-no-secrets.sh" "See /tmp/preship-secrets.log"
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
if [[ ! "$ALERT_COUNT" =~ ^[0-9]+$ ]]; then
    gate_warn "CodeQL error-severity alerts" \
        "Could not fetch (gh api error; run: gh auth status)"
elif [ "$ALERT_COUNT" -eq 0 ]; then
    gate_pass "CodeQL error-severity alerts = 0"
else
    gate_fail "CodeQL error-severity alerts = 0" \
        "Found $ALERT_COUNT open error-severity alerts — triage before release"
fi

# Gate 23: Open Dependabot alerts (v0.2.75 P2a). Mirrors Gate 15's
# gh-api + severity-filter shape for the Dependabot alert surface.
# (Numbering: 19/20 are historically retired — see the seam comment
# above Gate 21 — so new gates take 23+.)
#
#   critical/high open alert  → hard FAIL, listing each alert.
#   medium/low open alert     → advisory WARN only (Gate-14 style).
#   gh api error / 403 / 404  → LOUD WARN, never a silent false PASS.
echo "  [checking open Dependabot alerts...]"
DEPBOT_LINES="$(gh api "/repos/$REPO/dependabot/alerts?state=open&per_page=100" \
    --paginate \
    --jq '.[] | [(.number|tostring), (.security_vulnerability.severity // .security_advisory.severity // "unknown"), .dependency.package.name, .dependency.manifest_path] | join("|")' \
    2>/dev/null)"
DEPBOT_RC=$?
if [ "$DEPBOT_RC" -ne 0 ]; then
    gate_warn "Dependabot open alerts (P2a)" \
        "LOUD ADVISORY — could not query /repos/$REPO/dependabot/alerts (gh exit $DEPBOT_RC). This is NOT a confirmation of zero alerts. Likely cause: token missing the security_events scope (classic PAT) or the 'Dependabot alerts' repo permission (fine-grained PAT). Fix: gh auth refresh -s security_events, then re-run this gate."
elif [ -z "$DEPBOT_LINES" ]; then
    gate_pass "Dependabot open alerts = 0"
else
    _dep_n_crit_high=0
    _dep_n_med_low=0
    while IFS='|' read -r _dep_num _dep_sev _dep_pkg _dep_manifest; do
        [ -z "$_dep_num" ] && continue
        case "$_dep_sev" in
            critical|high)
                _dep_n_crit_high=$((_dep_n_crit_high + 1))
                printf '  [dependabot] OPEN alert #%s severity=%s package=%s manifest=%s\n' \
                    "$_dep_num" "$_dep_sev" "$_dep_pkg" "$_dep_manifest"
                ;;
            *)
                _dep_n_med_low=$((_dep_n_med_low + 1))
                printf '  [dependabot] open (advisory) alert #%s severity=%s package=%s manifest=%s\n' \
                    "$_dep_num" "$_dep_sev" "$_dep_pkg" "$_dep_manifest"
                ;;
        esac
    done <<< "$DEPBOT_LINES"
    if [ "$_dep_n_crit_high" -gt 0 ]; then
        gate_fail "Dependabot open alerts (critical/high = 0)" \
            "$_dep_n_crit_high critical/high open alert(s) listed above. Remediation: fix the dependency, OR dismiss the alert WITH a dismissal reason AND an inline rationale comment next to the pin (see the transformers CVE-2026-4372 precedent in requirements.txt)."
    else
        gate_warn "Dependabot open alerts (medium/low only — advisory)" \
            "$_dep_n_med_low medium/low open alert(s) listed above. Triage before release."
    fi
fi

# Gate 16: CHANGELOG has v$EXPECTED_VERSION entry.
# Match the Keep-a-Changelog heading shape: `## [0.2.X] - 2026-MM-DD`.
# Allow optional `v` prefix and optional surrounding brackets for flexibility.
# Use a regex escape on the version (dots are regex metachars) so a
# version "0.2.48" doesn't accidentally match "0.2.X" or "0X2X48".
CHANGELOG_VERSION_RE="$(printf '%s' "$EXPECTED_VERSION" | sed -E 's/\./\\./g')"
if grep -qE "^## \[?v?${CHANGELOG_VERSION_RE}\]?" CHANGELOG.md 2>/dev/null; then
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

# Gate 18: release.yml pre-release-gate job is present (structural).
if grep -q "pre-release-gate:" .github/workflows/release.yml 2>/dev/null; then
    gate_pass "release.yml has pre-release-gate job (structural)"
else
    gate_fail "release.yml has pre-release-gate job" \
        "pre-release-gate job not found in release.yml"
fi

# Gate 21: Pre-tag privacy check (scrubs operational private state from tracked tree).
if [ -x scripts/check-pre-tag-privacy.sh ]; then
    if bash scripts/check-pre-tag-privacy.sh > /tmp/preship-privacy.log 2>&1; then
        gate_pass "pre-tag privacy check (no contributor-name / hostname / private-path leaks)"
    else
        gate_fail "pre-tag privacy check" \
            "See /tmp/preship-privacy.log — private operational state leaked into tracked tree"
    fi
else
    gate_fail "pre-tag privacy check script present and executable" \
        "scripts/check-pre-tag-privacy.sh missing or not executable"
fi

# ── Gate 22: tri-OS install smoke green on main (v0.2.53 Track D, M-P1-8). ───
#
# The install-smoke-tri-os.yml workflow runs the full first-install.{sh,
# command,bat} flow against ubuntu-22.04, ubuntu-24.04, macos-14,
# windows-latest, and fedora-40 (matrix.label) using a fresh git clone
# — the same code path third-party users exercise. Per
# docs/INSTALL_ARCHITECTURE_v2.md §9.5 / §6 row M-P1-8 (Track D), no
# release tag may be pushed unless EVERY matrix entry of the most-recent
# completed run on main was successful within the last 24 hours.
#
# Implementation: query GitHub Actions REST API for the latest completed
# run, then check the per-job conclusion of all 5 matrix entries (the
# workflow uses `fail-fast: false` so each matrix leg has its own
# conclusion). A single failed leg → FAIL. The 24-hour window prevents
# stale-success masking: if the workflow hasn't run in the last day,
# we WARN (the daily cron should catch this; releasing right after a
# multi-day infrastructure outage is uncommon).
#
# Dry-run support: `--dry-run` flag prints the gh CLI invocation that
# would be made + exits with the WARN state, so the gate can be exercised
# at PR time without a fresh successful run existing yet. The flag is
# parsed from $1 if it's literally `--dry-run`; legacy
# `bash pre-ship-check.sh 0.2.53` argument shape still works because
# `--dry-run` would never be a valid version string.
TRI_OS_WORKFLOW="install-smoke-tri-os.yml"
TRI_OS_DRY_RUN=0
for _arg in "$@"; do
    [ "$_arg" = "--dry-run" ] && TRI_OS_DRY_RUN=1
done
if [ "$TRI_OS_DRY_RUN" -eq 1 ]; then
    gate_warn "tri-OS install smoke green on main (v0.2.53 M-P1-8)" \
        "--dry-run: would query: gh run list --repo $REPO --workflow $TRI_OS_WORKFLOW --branch main --status completed --limit 1"
else
    # Fetch the most-recent completed run on main: id, conclusion,
    # createdAt. createdAt is ISO 8601 UTC; we check it's within 24h.
    _tri_run_json="$(gh run list \
        --repo "$REPO" \
        --workflow "$TRI_OS_WORKFLOW" \
        --branch main \
        --status completed \
        --limit 1 \
        --json databaseId,conclusion,createdAt \
        --jq '.[0] // empty' 2>/dev/null || echo "")"
    if [ -z "$_tri_run_json" ]; then
        gate_warn "tri-OS install smoke green on main (v0.2.53 M-P1-8)" \
            "No completed run found on main for $TRI_OS_WORKFLOW yet (workflow may have just landed; let it run before tagging)"
    else
        _tri_conclusion="$(echo "$_tri_run_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("conclusion",""))' 2>/dev/null || echo "")"
        _tri_run_id="$(echo "$_tri_run_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("databaseId",""))' 2>/dev/null || echo "")"
        _tri_created="$(echo "$_tri_run_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("createdAt",""))' 2>/dev/null || echo "")"
        # Reject runs older than 24h — release-tagging against a stale
        # green is exactly the failure mode this gate is meant to catch.
        _tri_age_ok=1
        if [ -n "$_tri_created" ]; then
            _now_epoch="$(date -u +%s)"
            # macOS date doesn't grok -d; use python.
            _tri_age_ok="$(python3 - "$_tri_created" "$_now_epoch" <<'PY'
import sys, datetime
created_iso, now_epoch = sys.argv[1], int(sys.argv[2])
created = datetime.datetime.strptime(created_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
age_h = (now_epoch - int(created.timestamp())) / 3600
print(1 if age_h <= 24 else 0)
PY
)"
        fi
        if [ "$_tri_conclusion" != "success" ]; then
            gate_fail "tri-OS install smoke green on main (v0.2.53 M-P1-8)" \
                "Run $_tri_run_id concluded: $_tri_conclusion (expected: success). gh run view $_tri_run_id --repo $REPO"
        elif [ "$_tri_age_ok" != "1" ]; then
            gate_warn "tri-OS install smoke green on main (v0.2.53 M-P1-8)" \
                "Last success ($_tri_run_id at $_tri_created) is older than 24h; trigger a fresh run before tagging: gh workflow run $TRI_OS_WORKFLOW --repo $REPO --ref main"
        else
            # Verify every matrix leg in the run succeeded, not just the
            # overall conclusion. (The aggregated `conclusion` reflects
            # fail-fast=false semantics: even if one leg failed it can
            # show up as `failure`, which the check above catches — but
            # we double-check at the per-job level for defense in depth.)
            _tri_legs="$(gh run view "$_tri_run_id" \
                --repo "$REPO" \
                --json jobs \
                --jq '[.jobs[] | select(.name | startswith("install smoke ")) | {name, conclusion}]' 2>/dev/null || echo "[]")"
            _tri_n_legs="$(echo "$_tri_legs" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo "0")"
            _tri_n_fail="$(echo "$_tri_legs" | python3 -c 'import json,sys; print(sum(1 for j in json.load(sys.stdin) if j.get("conclusion") != "success"))' 2>/dev/null || echo "0")"
            if [ "$_tri_n_legs" -lt 5 ]; then
                gate_warn "tri-OS install smoke green on main (v0.2.53 M-P1-8)" \
                    "Run $_tri_run_id has only $_tri_n_legs install-smoke matrix legs (expected 5: ubuntu-22.04, ubuntu-24.04, macos-14, windows-latest, fedora-40)"
            elif [ "$_tri_n_fail" -gt 0 ]; then
                gate_fail "tri-OS install smoke green on main (v0.2.53 M-P1-8)" \
                    "Run $_tri_run_id: $_tri_n_fail / $_tri_n_legs matrix legs failed. gh run view $_tri_run_id --repo $REPO"
            else
                gate_pass "tri-OS install smoke green on main (v0.2.53 M-P1-8): run $_tri_run_id, $_tri_n_legs/$_tri_n_legs matrix legs green within last 24h"
            fi
        fi
    fi
fi

echo ""

# ── Section 4: Version-pin consistency ───────────────────────────────────────
# v0.2.57: the pin-checking logic lives in ONE place — scripts/check-version-pins.sh
# (also run standalone by the CI `version-pins` job). We SOURCE it and call
# its `vcheck_run_pins` helper so CI and release-time use identical logic
# and can't drift. The sourced file defines VERSION_PIN_FILES +
# WORKSPACE_INHERITED_CRATES + vcheck_run_pins; sourcing it (not executing)
# is a no-op for output because its report block is guarded by
# `[ "${BASH_SOURCE[0]}" = "$0" ]`.
echo "--- Version-pin consistency (all files at v$EXPECTED_VERSION) ---"
# shellcheck source=scripts/check-version-pins.sh
. "$SCRIPT_DIR/check-version-pins.sh"
echo "  [checking ${#VERSION_PIN_FILES[@]} literal pins + ${#WORKSPACE_INHERITED_CRATES[@]} inherited crates at $EXPECTED_VERSION...]"

if vcheck_run_pins "$EXPECTED_VERSION"; then
    gate_pass "all version pins agree at $EXPECTED_VERSION (literals + [workspace.package] + ${#WORKSPACE_INHERITED_CRATES[@]} inherited crates)"
else
    gate_fail "version-pin / workspace-inheritance drift at $EXPECTED_VERSION" \
        "Run scripts/bump-version.sh $EXPECTED_VERSION. Offending: ${VCHECK_FAILURES[*]}"
fi

# Gate (no-deferred-fixes): [Unreleased] CHANGELOG block must be empty.
# The block heading is allowed to exist; what's not allowed is content
# between the heading and the first tagged version heading.
UNRELEASED_BODY="$(awk '/^## \[Unreleased\]/,/^## \[[0-9]/' CHANGELOG.md \
    | sed -E '/^## \[/d' \
    | sed -E '/^[[:space:]]*$/d')"
if [ -z "$UNRELEASED_BODY" ]; then
    gate_pass "CHANGELOG [Unreleased] block is empty (no-deferred-fixes rule)"
else
    gate_fail "CHANGELOG [Unreleased] block is empty (no-deferred-fixes rule)" \
        "Content found between [Unreleased] and the next tagged heading; move into the tagged block first"
fi

echo ""

# ── Section 5: live re-embed regression protection (V46-C) ───────────────────
# Structural gate carried forward from v0.2.46. Catches the v0.2.42-v0.2.45
# recurring re-embed bug by running the diff-gate code path against a real
# Weaviate (V46-B's live integration tests), not just unit tests with
# mocked _batch_query_weaviate_content_hashes. SKIPs cleanly when Weaviate
# is unreachable (CI without Weaviate).
LIVE_GATE_TEST="tests/test_v0246_v46b_live_ci10_diff_gate.py"
if [ -f "$LIVE_GATE_TEST" ]; then
    echo "--- Live re-embed regression protection (V46-C) ---"
    _WEAVIATE_PROBE_URL="${WEAVIATE_URL:-http://localhost:8081}"
    if ! curl -sf "${_WEAVIATE_PROBE_URL}/v1/.well-known/ready" >/dev/null 2>&1; then
        gate_warn "Live re-embed regression protection (V46-C)" \
            "SKIP — Weaviate not reachable at ${_WEAVIATE_PROBE_URL}; only enforced when Weaviate is up. This gate MUST pass on the release machine before tagging."
    else
        if "${_PYTEST_CMD[@]}" -q "$LIVE_GATE_TEST" \
                -k "V46BLiveDiffGateTest and three_rows_returns_three_entries" \
                > /tmp/preship-live-diff.log 2>&1; then
            gate_pass "live diff-gate fetches stored hashes correctly"
        else
            gate_fail "live diff-gate FAILED — re-embed regression detected" \
                "See /tmp/preship-live-diff.log. The v0.2.42-v0.2.45 recurring re-embed bug is back."
        fi

        if "${_PYTEST_CMD[@]}" -q "$LIVE_GATE_TEST" \
                -k "V46BLivePruneTest and finds_and_deletes" \
                > /tmp/preship-live-prune.log 2>&1; then
            gate_pass "live prune deletes stale rows"
        else
            gate_fail "live prune FAILED — V0243-6 batch-delete bug is back" \
                "See /tmp/preship-live-prune.log."
        fi
    fi
    echo ""
fi

# ── Summary ──────────────────────────────────────────────────────────────────
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
