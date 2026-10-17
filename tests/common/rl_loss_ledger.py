# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Test helpers for the RL telemetry loss ledger (``vco_lib.rl_telemetry_loss``).

Since v0.2.104 a record can live in the ledger file OR, briefly, in a spilled
file under ``<ledger>.pending.d/`` (its writer found the lock busy).
``iter_records`` is the reader of record for "every loss line"; a test that
read the ledger file raw would see a spilled line as lost. One home for both
the read and the per-test cleanup, so every RL test reads the ledger the way
``vco doctor`` does.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def loss_records(path: Optional[Path] = None) -> list[dict]:
    """Every loss record — ledger lines plus not-yet-drained spills, once each."""
    from vco_lib.rl_telemetry_loss import iter_records

    return list(iter_records(path))


def clean_loss_ledger(path: Optional[Path] = None) -> None:
    """Remove the ledger file and every spilled file beside it."""
    from vco_lib.rl_telemetry_loss import loss_log_path, pending_dir

    p = path or loss_log_path()
    p.unlink(missing_ok=True)
    pdir = pending_dir(p)
    if pdir.is_dir():
        for f in pdir.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
