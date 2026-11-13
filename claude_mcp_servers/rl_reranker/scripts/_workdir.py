# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared setup for the maintainer training scripts in this directory.

The scripts are run by hand on a maintainer machine, never by the container or
the launcher. They write corpora, splits, results and candidate checkpoints, so
all of that goes to an explicit work directory OUTSIDE the checkout:

    RL_TRAIN_WORKDIR=/path/to/workdir python scripts/train_and_eval.py

An unset ``RL_TRAIN_WORKDIR`` is an error (no guessed location), and nothing is
ever written into ``weights/``: that tree is the shipped set, governed by
``weights/manifest.json`` (see ``bundled_weights.py``). Promoting a candidate
into it is a deliberate commit, not a side effect of a run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: ``claude_mcp_servers/`` — the directory that makes ``rl_reranker`` importable.
PACKAGE_PARENT = Path(__file__).resolve().parents[2]


def ensure_package_importable() -> None:
    """Put ``claude_mcp_servers/`` on ``sys.path`` so ``import rl_reranker`` works."""
    root = str(PACKAGE_PARENT)
    if root not in sys.path:
        sys.path.insert(0, root)


def work_dir() -> Path:
    """Return ``$RL_TRAIN_WORKDIR`` (created if missing); exit loudly when unset."""
    raw = os.environ.get("RL_TRAIN_WORKDIR", "").strip()
    if not raw:
        sys.exit(
            "RL_TRAIN_WORKDIR is not set: point it at a directory outside the "
            "checkout for corpora, results and candidate checkpoints."
        )
    path = Path(raw).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path
