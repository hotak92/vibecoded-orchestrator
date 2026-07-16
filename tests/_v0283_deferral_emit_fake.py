# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Test-only faithful fake for ``vco_lib.deferral_emit`` (WP-B1's module).

WP-B2 ships code that imports the REAL ``vco_lib.deferral_emit`` per the
PLAN-v0283 D7 contract. That module is authored in a PARALLEL worktree
(WP-B1) and may not exist in the WP-B2 worktree at development time, so the
WP-B2 test suite injects THIS faithful fake into ``sys.modules`` before the
producer code imports it (function-level imports resolve the fake).

The fake mirrors the D7 signatures EXACTLY so that when WP-B1 lands, the
production import contract is already proven:

    LOCK_REL = Path(".claude") / "context" / ".update-deferred.lock"

    @contextmanager
    def locked_report(folder) -> Iterator[DeferralReport]: ...
    def emit_entries(folder, entries, *, log=None) -> bool
    def emit(folder, entry, *, log=None) -> bool
    def resolve_conditions(folder, condition_ids, *, log=None) -> int
    def record_auto_resolution(folder, condition_id, action, detail, *, log=None) -> None

Behaviour is implemented against the REAL ``vco_lib.deferral_report`` so the
end-to-end auto-resolution paths (emit / resolve / self-clear) exercise the
genuine read/add/write/mark_resolved semantics. Locking is a best-effort
no-op here (concurrency is WP-B1's concern; WP-B2 tests are single-process).

Usage (per-file, at import time, BEFORE importing project_init):

    from tests._v0283_deferral_emit_fake import install_fake_deferral_emit
    install_fake_deferral_emit()
"""
from __future__ import annotations

import json
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from vco_lib.deferral_report import DeferralEntry, DeferralReport

LOCK_REL = Path(".claude") / "context" / ".update-deferred.lock"
_AUTO_RESOLUTIONS_REL = Path(".claude") / "logs" / "auto-resolutions.jsonl"


def _log_line(log, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:  # noqa: BLE001 — a logger must never break emission
        pass


@contextmanager
def locked_report(folder: Path) -> Iterator[DeferralReport]:
    """Read → yield → write the report. Best-effort no-lock in the fake."""
    folder = Path(folder)
    report = DeferralReport.read(folder)
    yield report
    report.write(folder)


def emit_entries(
    folder: Path,
    entries: Sequence[DeferralEntry],
    *,
    log=None,
) -> bool:
    entries = list(entries)
    if not entries:
        return False
    with locked_report(folder) as report:
        for entry in entries:
            report.add_entry(entry)
    _log_line(log, f"[vct] emitted {len(entries)} deferral entr(y/ies)")
    return True


def emit(folder: Path, entry: DeferralEntry, *, log=None) -> bool:
    return emit_entries(folder, [entry], log=log)


def resolve_conditions(
    folder: Path,
    condition_ids: Sequence[str],
    *,
    log=None,
) -> int:
    condition_ids = list(condition_ids)
    if not condition_ids:
        return 0
    resolved = 0
    with locked_report(folder) as report:
        for cid in condition_ids:
            if report.has_condition(cid):
                report.mark_resolved(cid)
                resolved += 1
    if resolved:
        _log_line(log, f"[vct] resolved {resolved} deferral condition(s)")
    return resolved


def record_auto_resolution(
    folder: Path,
    condition_id: str,
    action: str,
    detail: str,
    *,
    log=None,
) -> None:
    folder = Path(folder)
    _log_line(log, f"[vct] auto-resolved: {condition_id} — {action}: {detail}")
    target = folder / _AUTO_RESOLUTIONS_REL
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "condition_id": condition_id,
            "action": action,
            "detail": detail,
        }
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        # Best-effort: a logging failure never breaks the automation.
        pass


def install_fake_deferral_emit() -> types.ModuleType:
    """Inject the fake module into ``sys.modules`` and return it.

    Idempotent: if a ``vco_lib.deferral_emit`` module is already registered
    (e.g. WP-B1 has landed), the REAL module is left in place and returned —
    so this helper degrades to a no-op once the genuine module exists.
    """
    existing = sys.modules.get("vco_lib.deferral_emit")
    if existing is not None:
        return existing
    # v0.2.83 coordinator fix: the REAL module always wins when importable.
    # The original guard only consulted sys.modules, so at conftest time
    # (before anything imported vco_lib.deferral_emit) the fake silently
    # SHADOWED WP-B1's real module on the merged tree — every in-process
    # test validated the fake, not the shipped emitter. Import-first makes
    # this helper a true no-op once the real module exists; the fake is
    # only reachable on a tree where vco_lib/deferral_emit.py is absent.
    try:
        import vco_lib.deferral_emit as _real  # noqa: PLC0415 — lazy by design
        return _real
    except ImportError:
        pass
    mod = types.ModuleType("vco_lib.deferral_emit")
    mod.LOCK_REL = LOCK_REL
    mod.locked_report = locked_report
    mod.emit_entries = emit_entries
    mod.emit = emit
    mod.resolve_conditions = resolve_conditions
    mod.record_auto_resolution = record_auto_resolution
    sys.modules["vco_lib.deferral_emit"] = mod
    return mod


def read_auto_resolutions(folder: Path) -> list[dict]:
    """Test helper: parse the JSONL written by ``record_auto_resolution``."""
    target = Path(folder) / _AUTO_RESOLUTIONS_REL
    if not target.exists():
        return []
    rows: list[dict] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows
