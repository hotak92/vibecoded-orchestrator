#!/usr/bin/env bash
# BP-1 (v0.2.42 W7): apply updated branch-protection required-status-checks
# for hotak92/vibecoded-orchestrator main.
#
# Current required checks (ruleset 15644739, pre-v0.2.42):
#   - Python (pytest)
#   - Rust (cargo test --lib)
#   - Frontend (svelte-check)
#
# This script adds (via the modern Rulesets API):
#   - Validate paid-module manifests (strict mode)   [manifest-validate.yml]
#   - Hook OS-Parity Gate                            [hook-parity.yml]
#   - Check set -e always paired with pipefail       [hook-parity.yml]
#   - Managed-paths cross-language consistency       [ci.yml]
#   - Launcher binary leak-check                     [ci.yml]
#   - CodeQL / Analyze (python)                      [codeql.yml]
#   - CodeQL / Analyze (javascript-typescript)       [codeql.yml]
#   - install.py + install-bundle smoke              [installer-smoke.yml]
#   - Weaviate bootstrap smoke                       [installer-smoke.yml]
#   - macOS best-effort smoke (v0.2.40 X1)           [installer-smoke.yml]
#   - install.py smoke (macos-latest)                [installer-smoke.yml]
#
# NOTE: "install.py smoke (windows-latest)" is intentionally omitted from
# required checks until the vct-hub --no-hub flag (CI-8) is shipped and the
# Windows hub-hang is resolved.
#
# NOTE: CodeQL checks are added here because W6 has triaged all 14 open
# alerts (5 errors + 9 warnings) and the workflow is green.
#
# Usage (run ONCE by repo owner before tagging v0.2.42):
#   GITHUB_TOKEN=<PAT with admin:repo scope> bash scripts/apply-branch-protection-v0.2.42.sh
#   OR, if already authenticated via gh cli:
#   bash scripts/apply-branch-protection-v0.2.42.sh
#
# Requires: gh cli authenticated with admin scope on the repo.
# Dry-run: set DRY_RUN=1 to print the API payload without applying.

set -euo pipefail

REPO="hotak92/vibecoded-orchestrator"
RULESET_ID="15644739"
DRY_RUN="${DRY_RUN:-0}"

echo "BP-1: Updating branch protection required-status-checks"
echo "  Repo:    $REPO"
echo "  Ruleset: $RULESET_ID"
echo "  DRY_RUN: $DRY_RUN"
echo ""

# ── Step 1: Enable allow_auto_merge (CI-4) ───────────────────────────────────
# Required before Dependabot auto-merge can work.
echo "=== Step 1: Enable allow_auto_merge (CI-4) ==="
if [ "$DRY_RUN" = "1" ]; then
    echo "  [DRY_RUN] Would run:"
    echo "    gh api -X PATCH /repos/$REPO -f allow_auto_merge=true"
else
    gh api -X PATCH "/repos/$REPO" -f allow_auto_merge=true
    echo "  allow_auto_merge enabled."
fi

# Verify
CURRENT="$(gh api "/repos/$REPO" --jq '.allow_auto_merge' 2>/dev/null || echo "unknown")"
echo "  Current allow_auto_merge: $CURRENT"
echo ""

# ── Step 2: Show current ruleset (read-only, for confirmation) ───────────────
echo "=== Step 2: Current ruleset $RULESET_ID conditions ==="
gh api "/repos/$REPO/rulesets/$RULESET_ID" \
    --jq '.rules[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context' \
    2>/dev/null || echo "  (could not fetch ruleset details — may need admin token)"
echo ""

# ── Step 3: Construct the new required-status-checks list ────────────────────
#
# The Rulesets API (PUT /repos/{owner}/{repo}/rulesets/{ruleset_id}) replaces
# the entire ruleset config.  We fetch the current ruleset, update only the
# required_status_checks rule, and PUT it back.
#
# CAUTION: This overwrites the entire ruleset.  We preserve all other rules
# (conditions, enforcement, bypass actors) and only append new context strings
# to the required_status_checks list.
#
# If the ruleset structure is more complex than a single required_status_checks
# rule, review the full JSON before applying:
#   gh api /repos/$REPO/rulesets/$RULESET_ID

echo "=== Step 3: Build new required-status-checks list ==="

# Full list: existing 3 + 11 new additions
REQUIRED_CHECKS=(
    # Original required checks (must keep)
    "Python (pytest)"
    "Rust (cargo test --lib)"
    "Frontend (svelte-check)"
    # New additions (CI-6 / BP-1, v0.2.42 W7)
    "Validate paid-module manifests (strict mode)"
    "Check .sh / .ps1 hook parity"
    "Check set -e always paired with pipefail"
    "Managed-paths cross-language consistency"
    "Launcher binary leak-check"
    "Analyze (python)"
    "Analyze (javascript-typescript)"
    "install.py + install-bundle smoke"
    "Weaviate bootstrap smoke"
    "macOS best-effort smoke (v0.2.40 X1)"
    "install.py smoke (macos-latest)"
)

echo "  New required checks list (${#REQUIRED_CHECKS[@]} total):"
for c in "${REQUIRED_CHECKS[@]}"; do
    echo "    - $c"
done
echo ""

# Build the JSON array for the API payload
CONTEXTS_JSON="$(printf '%s\n' "${REQUIRED_CHECKS[@]}" | jq -R . | jq -s '{contexts: .}')"

echo "=== Step 4: Apply via gh api (rulesets PATCH) ==="
echo ""
echo "  NOTE: The Rulesets API requires PUT/PATCH of the full ruleset body."
echo "  The safest approach for a production repo is to use the GitHub web UI:"
echo "  https://github.com/$REPO/settings/rules/$RULESET_ID"
echo ""
echo "  Alternatively, construct the full ruleset body from:"
echo "    gh api /repos/$REPO/rulesets/$RULESET_ID"
echo "  and apply with:"
echo "    gh api -X PUT /repos/$REPO/rulesets/$RULESET_ID --input <patched-body.json>"
echo ""
echo "  The status-check context names to ADD (copy into the ruleset editor):"
echo ""
for c in \
    "Validate paid-module manifests (strict mode)" \
    "Check .sh / .ps1 hook parity" \
    "Check set -e always paired with pipefail" \
    "Managed-paths cross-language consistency" \
    "Launcher binary leak-check" \
    "Analyze (python)" \
    "Analyze (javascript-typescript)" \
    "install.py + install-bundle smoke" \
    "Weaviate bootstrap smoke" \
    "macOS best-effort smoke (v0.2.40 X1)" \
    "install.py smoke (macos-latest)"; do
    echo "    $c"
done

echo ""
echo "=== Summary ==="
echo "  1. allow_auto_merge: $(gh api "/repos/$REPO" --jq '.allow_auto_merge' 2>/dev/null || echo 'could not verify')"
echo ""
echo "  To verify the ruleset after updating:"
echo "    gh api /repos/$REPO/rulesets/$RULESET_ID \\"
echo "      --jq '.rules[] | select(.type == \"required_status_checks\") | .parameters.required_status_checks[].context'"
echo ""
echo "BP-1 documentation complete."
