# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — V52-AG layer 3: a bundle materialization records its version.

The ``bundle_materialization`` row of ``artifact_schema_versions`` was only
ever stamped "born at canonical" by the schema-migration runner, so a bump of
the manifest schema would have left every project's row behind with nothing to
advance it. The bundle now records the version it actually applied — the
``schema_version`` its engine wrote into ``.claude/.vco-manifest.json`` — and
the runner (the registry's reader) sees ``UP_TO_DATE`` right after.

Covers the SSOT function (act / every leave-alone reason), its CLI verb (the
launcher spawns it), the reader seeing the written row, and install.py's root
call site running BEFORE its runner pass.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.common.child_env import child_env  # noqa: E402
from tests.common.launcher_db_fixture import add_project, create_empty_launcher_db  # noqa: E402
from vco_lib import artifact_version_registry as avr  # noqa: E402
from vco_lib import schema_migration_runner as smr  # noqa: E402
from vco_lib import schema_versions as sv  # noqa: E402

CANON = sv.canonical_version("bundle_materialization")


@pytest.fixture
def db(tmp_path):
    path = create_empty_launcher_db(tmp_path / "launcher.db", up_to=33)
    add_project(path, project_id="p1", name="p", folder_path=str(tmp_path / "proj"),
                slug="p1", created_at=1, updated_at=1)
    return path


def _manifest(folder: Path, version) -> None:
    target = folder / ".claude" / ".vco-manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"schema_version": version, "files": {}}), encoding="utf-8")


def _status(db_path):
    return avr.check_artifact_version(db_path, project_id="p1",
                                      artifact_type="bundle_materialization",
                                      artifact_name=avr.DEFAULT_ARTIFACT_NAME)


def test_a_materialized_bundle_records_its_version(tmp_path, db):
    """ACT: manifest at canonical → the row is written at canonical."""
    folder = tmp_path / "proj"
    _manifest(folder, CANON)
    assert _status(db) is avr.ArtifactVersionStatus.NEVER_MATERIALIZED
    out = avr.record_bundle_materialization(db, project_id="p1", folder=folder, now_ms=5)
    assert out == {"ok": True, "action": "registered", "project_id": "p1",
                   "artifact_type": "bundle_materialization", "schema_version": CANON}
    assert _status(db) is avr.ArtifactVersionStatus.UP_TO_DATE


@pytest.mark.parametrize("version,code", [
    (None, "no_manifest"),
    ("two", "manifest_unreadable"),
    (True, "manifest_unreadable"),
    (CANON - 1, "manifest_version_mismatch"),
])
def test_no_evidence_of_the_current_version_records_nothing(tmp_path, db, version, code):
    """LEAVE-ALONE: no manifest, a non-integer version, or an older manifest
    (the bundle did not rewrite it) — the registry is untouched."""
    folder = tmp_path / "proj"
    if version is not None:
        _manifest(folder, version)
    out = avr.record_bundle_materialization(db, project_id="p1", folder=folder)
    assert out["ok"] is False and out["code"] == code, out
    assert out["error"]
    assert _status(db) is avr.ArtifactVersionStatus.NEVER_MATERIALIZED


def test_a_missing_launcher_db_is_reported_never_created(tmp_path):
    folder = tmp_path / "proj"
    _manifest(folder, CANON)
    missing = tmp_path / "nowhere" / "launcher.db"
    out = avr.record_bundle_materialization(missing, project_id="p1", folder=folder)
    assert out["code"] == "no_launcher_db"
    assert not missing.exists()


def test_a_failed_registry_write_is_reported_not_raised(tmp_path, db):
    """SOFT-FAIL: an unregistered project id violates the FK — reported."""
    folder = tmp_path / "proj"
    _manifest(folder, CANON)
    out = avr.record_bundle_materialization(db, project_id="ghost", folder=folder)
    assert out["ok"] is False and out["code"] == "registry_write_failed", out


def test_the_cli_verb_prints_one_json_object_and_exits_by_outcome(tmp_path, db):
    """The argv the launcher spawns (`--folder --project-id --db`)."""
    folder = tmp_path / "proj"
    argv = [sys.executable, "-m", "vco_lib.artifact_version_registry",
            "record-bundle-materialization", "--folder", str(folder),
            "--project-id", "p1", "--db", str(db)]
    refused = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=60,
                             env=child_env())
    assert refused.returncode == 1, refused.stderr
    assert json.loads(refused.stdout)["code"] == "no_manifest"
    _manifest(folder, CANON)
    done = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=60,
                             env=child_env())
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["action"] == "registered"


def test_the_runner_reads_the_row_the_bundle_wrote(tmp_path, db):
    """The READER: the runner resolves the same artifact name and reports the
    recorded row as current rather than stamping one of its own."""
    folder = tmp_path / "proj"
    _manifest(folder, CANON)
    avr.record_bundle_materialization(db, project_id="p1", folder=folder, now_ms=5)
    assert smr._resolve_artifact_names("bundle_materialization", {}, None) == [
        avr.DEFAULT_ARTIFACT_NAME]
    report = smr.run_schema_migrations(
        db_path=db, project_id="p1", migrations_dir=tmp_path / "no-migrations",
        env={}, check=True, weaviate_url="http://127.0.0.1:9",
        live_drift_probe=lambda *_: (False, []),
        codegraph_drift_probe=lambda *_: (False, []),
    )
    rows = {(a, n): d for (a, n, d) in report.up_to_date}
    assert rows.get(("bundle_materialization", "default")) == "recorded version current"
    assert not any(a == "bundle_materialization" for (a, _, _) in report.registered)


@pytest.mark.parametrize("manifest_version,expected", [
    (CANON, avr.ArtifactVersionStatus.UP_TO_DATE),
    (CANON - 1, avr.ArtifactVersionStatus.NEVER_MATERIALIZED),
])
def test_install_py_records_the_root_bundle_before_its_runner_pass(
        tmp_path, db, monkeypatch, capsys, manifest_version, expected):
    """install.py's step 7d runs the SAME function for the root, and the row
    exists by the time its runner is called (observed from inside the runner).
    A manifest the bundle did not rewrite records nothing and says so."""
    import install
    from vco_lib import launcher_db_reader
    from vco_lib.deferral_report import DeferralReport

    root = tmp_path / "proj"
    _manifest(root, manifest_version)
    seen = {}

    def _runner(**kw):
        seen["status"] = _status(kw["db_path"])
        return smr.MigrationRunReport()

    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    monkeypatch.setattr(install._project_init, "_launcher_db_path", lambda: db)
    monkeypatch.setattr(launcher_db_reader, "get_orchestrator_root_project_id", lambda: "p1")
    monkeypatch.setattr(smr, "resolve_codegraph_migration_inputs", lambda *a, **k: ({}, {}, None))
    monkeypatch.setattr(smr, "run_schema_migrations", _runner)
    for side_leg in ("_run_additive_temporal_props_migration", "_migrate_kg_named_vector_slots",
                     "_emit_lowercase_codegraph_cleanup_deferrals"):
        monkeypatch.setattr(install, side_leg, lambda *_a, **_k: None)
    from vco_lib import codegraph_registry_reconcile as cgrr
    monkeypatch.setattr(cgrr, "reconcile_codegraph_registry", lambda *a, **k: None)

    install._run_schema_migration_scripts(DeferralReport())
    assert seen["status"] is expected
    out = capsys.readouterr().out
    assert ("bundle version NOT recorded" in out) is (manifest_version != CANON), out
