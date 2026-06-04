#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Re-sync vendored files. The byte-identity test in
# tests/test_vendored_file_sync.py fails when they drift. Run this
# script after editing any orchestrator-side canonical source.
#
# Vendored pairs (canonical -> vendored):
#   vco_lib/rl_training_targets.py
#       -> paid-modules/vct-rl-reranker/_training_targets.py
#   claude_mcp_servers/rl_client/rl_logger.py
#       -> paid-modules/vct-rl-reranker/rl_logger.py
#
# Behaviour:
#   - Resolves the repo root via `git rev-parse --show-toplevel` so the
#     script works regardless of cwd.
#   - Refuses to run if the `paid-modules/` directory is absent.
#   - After each copy, verifies sha256 equality before logging "synced".

set -euo pipefail

# Resolve repo root robustly: ask git, anchored at the script's own
# directory so we don't accidentally pick up a parent git repo when the
# script is symlinked or invoked from elsewhere.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git -C "${SCRIPT_DIR}/.." rev-parse --show-toplevel)"

PAID_DIR="${REPO_ROOT}/paid-modules/vct-rl-reranker"
if [ ! -d "${PAID_DIR}" ]; then
    echo "error: paid-module directory does not exist: ${PAID_DIR}" >&2
    echo "       cannot sync vendored files without the destination tree." >&2
    exit 1
fi

# Pairs are: canonical_relpath::vendored_relpath
PAIRS=(
    "vco_lib/rl_training_targets.py::paid-modules/vct-rl-reranker/_training_targets.py"
    "claude_mcp_servers/rl_client/rl_logger.py::paid-modules/vct-rl-reranker/rl_logger.py"
)

sha256() {
    # Portable wrapper: prefer sha256sum (Linux), fall back to shasum (macOS).
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

for pair in "${PAIRS[@]}"; do
    canonical_rel="${pair%%::*}"
    vendored_rel="${pair##*::}"
    canonical_abs="${REPO_ROOT}/${canonical_rel}"
    vendored_abs="${REPO_ROOT}/${vendored_rel}"

    if [ ! -f "${canonical_abs}" ]; then
        echo "error: canonical source missing: ${canonical_abs}" >&2
        exit 1
    fi

    cp "${canonical_abs}" "${vendored_abs}"

    canonical_hash="$(sha256 "${canonical_abs}")"
    vendored_hash="$(sha256 "${vendored_abs}")"
    if [ "${canonical_hash}" != "${vendored_hash}" ]; then
        echo "error: post-copy hash mismatch for ${vendored_rel}" >&2
        echo "       canonical ${canonical_hash} vs vendored ${vendored_hash}" >&2
        exit 1
    fi

    echo "synced ${vendored_rel}"
done

echo "all vendored files in sync."
