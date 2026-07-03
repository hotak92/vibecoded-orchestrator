# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 M2 — `check-code-formats-schema` gate (generalized formats-check).

The v0.2.57 kg_node_formats machinery was generalized into
``_run_formats_schema_check`` with per-artifact parameters (one home). The KG
branch is regression-covered by test_regenerated_data_file_class.py
(unchanged); THIS file covers the new code_formats branch:

  * soft ``skipped`` while ``code_formats`` is not yet in the schema registry
    (the window before the release's schema_versions entry lands).
  * first materialize → registered at canonical.
  * RECREATE_NEEDED → KEEP-REGENERATED: old-schema sidecar DELETED (the
    generator rebuilds), registry re-registered at canonical.
  * deletion failure → deferred (sidecar untouched, no half-migrated state).
  * REFUSE_DOWNGRADE → info deferral, sidecar untouched.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init as pi  # noqa: E402
from vco_lib import schema_versions as sv  # noqa: E402


def _mk_artifact_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE artifact_schema_versions(
            project_id TEXT, artifact_type TEXT NOT NULL, artifact_name TEXT NOT NULL,
            schema_version INTEGER NOT NULL, materialized_at INTEGER NOT NULL,
            PRIMARY KEY(project_id, artifact_type, artifact_name))"""
    )
    con.commit()
    con.close()


def _seed_row(db: Path, version: int) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO artifact_schema_versions "
        "VALUES(NULL,'code_formats','default',?,1)",
        (version,),
    )
    con.commit()
    con.close()


def _stored_version(db: Path):
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT schema_version FROM artifact_schema_versions "
        "WHERE artifact_type='code_formats'"
    ).fetchone()
    con.close()
    return row[0] if row else None


@pytest.fixture()
def proj(tmp_path):
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    (folder / ".claude" / ".code_formats.json").write_text(
        '{"src/a.py::a.f": {"collection": "CodeFunction", "one_liner": "old"}}'
    )
    db = tmp_path / "launcher.db"
    _mk_artifact_db(db)
    return folder, db


def _args(folder: Path, db: Path, now_ms: int = 1000) -> argparse.Namespace:
    return argparse.Namespace(
        folder=str(folder), db=str(db), project_id=None, now_ms=now_ms
    )


def _run(folder, db, capsys, now_ms=1000):
    rc = pi._cmd_check_code_formats_schema(_args(folder, db, now_ms))
    out = json.loads(capsys.readouterr().out)
    return rc, out


@pytest.fixture()
def registered_type(monkeypatch):
    """Simulate the release's schema_versions entry (integrator-applied):
    CODE_FORMATS_SCHEMA_VERSION = 1, classified "derived" (regen cache)."""
    monkeypatch.setitem(sv.CANONICAL_VERSIONS, "code_formats", 1)
    monkeypatch.setitem(sv.ARTIFACT_STATE_CLASSIFICATION, "code_formats", "derived")


def test_skipped_while_type_not_in_registry(proj, capsys, monkeypatch):
    # Defensive path: if code_formats is absent from CANONICAL_VERSIONS (e.g.
    # a partial/rolled-back registry) the check must soft no-op — exit 0,
    # sidecar untouched, nothing stored. code_formats is now a permanent
    # registry entry (v0.2.73 M2), so we delete it for the duration of this
    # test to exercise the absent-type branch.
    folder, db = proj
    monkeypatch.delitem(sv.CANONICAL_VERSIONS, "code_formats", raising=False)
    monkeypatch.delitem(sv.ARTIFACT_STATE_CLASSIFICATION, "code_formats", raising=False)
    assert "code_formats" not in sv.CANONICAL_VERSIONS
    rc, out = _run(folder, db, capsys)
    assert rc == 0
    assert out["action"] == "skipped"
    assert (folder / ".claude" / ".code_formats.json").exists()
    assert _stored_version(db) is None


def test_first_materialize_registers_at_canonical(proj, capsys, registered_type):
    folder, db = proj
    rc, out = _run(folder, db, capsys)
    assert rc == 0
    assert out["status"] == "NEVER_MATERIALIZED"
    assert out["action"] == "registered"
    assert _stored_version(db) == 1
    # Sidecar untouched on plain registration.
    assert "old" in (folder / ".claude" / ".code_formats.json").read_text()


def test_second_run_up_to_date(proj, capsys, registered_type):
    folder, db = proj
    _run(folder, db, capsys, now_ms=1000)
    rc, out = _run(folder, db, capsys, now_ms=2000)
    assert out["status"] == "UP_TO_DATE"


def test_recreate_needed_deletes_sidecar_keep_regenerated(
        proj, capsys, registered_type):
    folder, db = proj
    _seed_row(db, 0)
    rc, out = _run(folder, db, capsys, now_ms=3000)
    assert rc == 0
    assert out["status"] == "RECREATE_NEEDED"
    assert out["action"] == "keep-regenerated"
    assert out["regenerated"] is True
    # KEEP-REGENERATED: old-schema sidecar is GONE (generator rebuilds it).
    assert not (folder / ".claude" / ".code_formats.json").exists()
    assert _stored_version(db) == 1


def test_recreate_needed_no_sidecar_is_clean_success(
        proj, capsys, registered_type):
    folder, db = proj
    (folder / ".claude" / ".code_formats.json").unlink()
    _seed_row(db, 0)
    rc, out = _run(folder, db, capsys, now_ms=3000)
    assert out["action"] == "keep-regenerated"
    assert _stored_version(db) == 1


def test_recreate_deletion_failure_defers_sidecar_intact(
        proj, capsys, registered_type, monkeypatch):
    folder, db = proj
    _seed_row(db, 0)
    monkeypatch.setattr(
        pi, "_regenerate_code_formats",
        lambda f: (False, "could not delete old-schema sidecar: locked"),
    )
    rc, out = _run(folder, db, capsys, now_ms=3000)
    assert rc == 0
    assert out["action"] == "deferred"
    assert out["deferral_written"] is True
    # No half-migrated state: sidecar intact, registry NOT advanced.
    assert (folder / ".claude" / ".code_formats.json").exists()
    assert _stored_version(db) == 0
    deferred = (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
    assert "code_formats_schema_migration_pending" in deferred


def test_refuse_downgrade_defers_sidecar_intact(proj, capsys, registered_type):
    folder, db = proj
    _seed_row(db, 99)
    rc, out = _run(folder, db, capsys, now_ms=3000)
    assert out["status"] == "REFUSE_DOWNGRADE"
    assert out["action"] == "deferred-downgrade"
    assert (folder / ".claude" / ".code_formats.json").exists()
    assert _stored_version(db) == 99
    deferred = (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
    assert "code_formats_schema_migration_pending" in deferred


def test_regenerate_code_formats_unlink_error_reports_false(tmp_path):
    # A directory where the sidecar file should be → unlink raises OSError →
    # (False, reason) so the caller defers.
    folder = tmp_path / "proj"
    (folder / ".claude" / ".code_formats.json").mkdir(parents=True)
    ok, detail = pi._regenerate_code_formats(folder)
    assert ok is False
    assert "could not delete" in detail


def test_subcommand_registered_in_parser():
    parser = pi._build_arg_parser()
    # argparse raises SystemExit(2) on unknown subcommands; a clean parse
    # proves registration + flag wiring.
    ns = parser.parse_args(
        ["check-code-formats-schema", "--folder", "/tmp/x", "--now-ms", "5"]
    )
    assert ns.func is pi._cmd_check_code_formats_schema
    assert ns.now_ms == 5
