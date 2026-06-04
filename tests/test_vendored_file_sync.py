# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Byte-identity drift tests for vendored files.

Several files exist in two locations in this repo:

1. An *orchestrator-side canonical* source under ``vco_lib/`` or
   ``claude_mcp_servers/rl_client/`` (AGPL, shipped with the open-source
   orchestrator clone).
2. A *vendored copy* under ``paid-modules/vct-rl-reranker/`` (consumed by
   the paid RL reranker module, which is built from a sibling sub-repo).

The vendored copies MUST stay byte-identical to their canonical sources.
If they drift, the paid module starts running stale logic while the
orchestrator runs the new logic — silent semantic divergence that is
extremely hard to diagnose at runtime (different reward shaping,
different target keys, different log formats, etc.).

This module enforces byte-identity at test time. One test per pair so
a failure unambiguously names the drifted pair. To re-sync after editing
a canonical file, run::

    ./scripts/sync-vendored-files.sh

Or manually::

    cp <orchestrator_path> <paid_path>

Skip behaviour: if either side of a pair is missing on disk (e.g. the
``paid-modules/`` sub-tree is absent in third-party-adoption builds or
trimmed CI configurations, or this is a worktree based on a commit that
predates the canonical file), the test SKIPs rather than fails — drift
checks only make sense when both files exist.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Pairs are (orchestrator_side_canonical, paid_module_side_vendored).
# The canonical side is the source of truth; the vendored side is the
# destination of the sync.
VENDORED_PAIRS: list[tuple[str, str]] = [
    (
        "vco_lib/rl_training_targets.py",
        "paid-modules/vct-rl-reranker/_training_targets.py",
    ),
    (
        "claude_mcp_servers/rl_client/rl_logger.py",
        "paid-modules/vct-rl-reranker/rl_logger.py",
    ),
]


def _sha256(path: Path) -> str:
    """Return the hex SHA-256 digest of ``path``."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _drift_message(orchestrator_path: Path, paid_path: Path) -> str:
    """Build a clear, actionable failure message pointing at the fix."""
    return (
        f"{orchestrator_path} and {paid_path} have drifted out of sync.\n"
        f"Run: ./scripts/sync-vendored-files.sh\n"
        f"Or copy manually: cp {orchestrator_path} {paid_path}"
    )


def _check_pair(orchestrator_rel: str, paid_rel: str) -> None:
    """Skip if either side is missing, otherwise assert byte-identity."""
    orchestrator_path = REPO_ROOT / orchestrator_rel
    paid_path = REPO_ROOT / paid_rel

    if not orchestrator_path.exists():
        pytest.skip(
            f"Canonical orchestrator-side file missing: {orchestrator_path}"
        )
    if not paid_path.exists():
        pytest.skip(
            f"Vendored paid-module-side file missing: {paid_path} "
            f"(paid-modules/ may be absent in this build)"
        )

    orchestrator_hash = _sha256(orchestrator_path)
    paid_hash = _sha256(paid_path)

    assert orchestrator_hash == paid_hash, _drift_message(
        orchestrator_path, paid_path
    )


def test_rl_training_targets_sync() -> None:
    """vco_lib/rl_training_targets.py must equal paid-modules/.../_training_targets.py."""
    _check_pair(*VENDORED_PAIRS[0])


def test_rl_logger_sync() -> None:
    """claude_mcp_servers/rl_client/rl_logger.py must equal paid-modules/.../rl_logger.py."""
    _check_pair(*VENDORED_PAIRS[1])
