# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.84 review F3 — `_cmd_migrate_collections` env injection is SCOPED.

Pre-.84 `_cmd_migrate_collections` wrote KG_COLLECTION / DEVELOPMENT_COLLECTION /
DIAGRAMS_COLLECTION into ``os.environ`` (to feed the synchronous
`migrate_collections` dispatcher + a `subprocess.run(env=dict(os.environ))`
re-ingest child) and NEVER reverted them. In the in-process test suite this
leaked the ``<Name>_*`` values into later-collected tests — a real side effect
that `tests/test_migrate_rebuild_recovery.py` had to paper over with an explicit
setUp/tearDown backup/restore. The fix wraps the injection in `_scoped_environ`
so the three keys are restored on exit.

These tests pin:
  * ACT + LEAVE-ALONE: after `_cmd_migrate_collections` returns, the three env
    keys carry their EXACT pre-call values (present→restored, absent→removed) —
    for a present-before value AND an absent-before value, and even when the
    dispatcher raises.
  * The subprocess CONTRACT is preserved: the injected values ARE visible to the
    synchronous callee while the block is active (the reingest child, spawned via
    `dict(os.environ)`, sees them).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


_INJECTED_KEYS = ("KG_COLLECTION", "DEVELOPMENT_COLLECTION", "DIAGRAMS_COLLECTION")


def _stub_args(**overrides) -> argparse.Namespace:
    base = dict(
        name="LeakProbe",
        all_projects=False,
        force_rebuild=False,
        dry_run=True,
        weaviate_url="http://localhost:9",
        include_code=False,
        project_folder=None,  # tier-3 name-derive; no launcher.db / subprocess
        json=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _snapshot() -> dict:
    return {k: os.environ.get(k) for k in _INJECTED_KEYS}


def _restore(snap: dict) -> None:
    for k, v in snap.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _canned() -> dict:
    return {"plan": [], "dry_run": True, "errors": []}


def test_migrate_collections_env_present_before_is_restored() -> None:
    """LEAVE-ALONE (present-before): a KG_COLLECTION set BEFORE the call must be
    RESTORED to its exact prior value afterward, not left at the injected value."""
    outer_snap = _snapshot()
    try:
        os.environ["KG_COLLECTION"] = "PreExisting_KnowledgeGraph"
        os.environ["DEVELOPMENT_COLLECTION"] = "PreExisting_Development"
        os.environ["DIAGRAMS_COLLECTION"] = "PreExisting_Diagrams"

        args = _stub_args()
        with mock.patch.object(project_init, "migrate_collections",
                               return_value=_canned()), \
             mock.patch.object(sys, "stdout"):
            rc = project_init._cmd_migrate_collections(args)
        assert rc == 0

        assert os.environ.get("KG_COLLECTION") == "PreExisting_KnowledgeGraph"
        assert os.environ.get("DEVELOPMENT_COLLECTION") == "PreExisting_Development"
        assert os.environ.get("DIAGRAMS_COLLECTION") == "PreExisting_Diagrams"
    finally:
        _restore(outer_snap)


def test_migrate_collections_env_absent_before_is_removed() -> None:
    """ACT (absent-before): keys that were ABSENT before the call must be REMOVED
    again afterward — no `LeakProbe_*` value survives into the process env. This
    is the exact leak that polluted the suite twice."""
    outer_snap = _snapshot()
    try:
        for k in _INJECTED_KEYS:
            os.environ.pop(k, None)

        args = _stub_args()
        with mock.patch.object(project_init, "migrate_collections",
                               return_value=_canned()), \
             mock.patch.object(sys, "stdout"):
            rc = project_init._cmd_migrate_collections(args)
        assert rc == 0

        for k in _INJECTED_KEYS:
            assert k not in os.environ, (
                f"{k} leaked into os.environ after _cmd_migrate_collections "
                f"(value={os.environ.get(k)!r})"
            )
    finally:
        _restore(outer_snap)


def test_migrate_collections_env_restored_even_on_exception() -> None:
    """The restore runs in a `finally`: a dispatcher exception must still leave the
    three keys at their pre-call values (absent stays absent)."""
    outer_snap = _snapshot()
    try:
        for k in _INJECTED_KEYS:
            os.environ.pop(k, None)

        args = _stub_args()
        boom = RuntimeError("dispatcher blew up mid-migrate")
        with mock.patch.object(project_init, "migrate_collections",
                               side_effect=boom), \
             mock.patch.object(sys, "stdout"):
            try:
                project_init._cmd_migrate_collections(args)
            except RuntimeError as e:
                assert e is boom
            else:  # pragma: no cover - the mock must raise
                raise AssertionError("expected the dispatcher exception to propagate")

        for k in _INJECTED_KEYS:
            assert k not in os.environ, f"{k} leaked after an exception"
    finally:
        _restore(outer_snap)


def test_migrate_collections_injects_values_for_the_synchronous_callee() -> None:
    """CONTRACT: while the scoped block is active, the injected values ARE visible
    to the synchronous dispatcher — the scope reverts them only AFTER the callee
    (and any `dict(os.environ)` child it spawns) has run. We assert by capturing
    the env `migrate_collections` observes at call time."""
    outer_snap = _snapshot()
    try:
        for k in _INJECTED_KEYS:
            os.environ.pop(k, None)

        observed: dict = {}

        def _capture(ns, *, dry_run=False, weaviate_url=None):
            for k in _INJECTED_KEYS:
                observed[k] = os.environ.get(k)
            return _canned()

        args = _stub_args()
        with mock.patch.object(project_init, "migrate_collections",
                               side_effect=_capture), \
             mock.patch.object(sys, "stdout"):
            project_init._cmd_migrate_collections(args)

        # The dispatcher SAW the name-derived values (tier-3 from --name).
        assert observed["KG_COLLECTION"], (
            "the dispatcher must observe an injected KG_COLLECTION while the "
            "scoped block is active"
        )
        assert observed["KG_COLLECTION"].startswith("LeakProbe")
        assert observed["DEVELOPMENT_COLLECTION"].startswith("LeakProbe")
        # ... but they are gone again afterward (scope reverted).
        for k in _INJECTED_KEYS:
            assert k not in os.environ
    finally:
        _restore(outer_snap)
