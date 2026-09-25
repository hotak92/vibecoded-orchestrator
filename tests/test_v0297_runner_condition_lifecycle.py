# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — the schema-migration runner's ledger ids: declared, truthful,
and cleared when over.

1. The runner's error details carry their condition id as a trailing
   ``[id]`` tag; ``build_deferral_entries`` turns it into the ledger id. Two
   of those ids (``schema_migration_script_missing``,
   ``schema_migration_probe_unreachable``) were emitted but never declared in
   ``deferral_conditions.toml`` — the completeness scan only knew
   ``condition_id=`` shapes. The runner now declares its emittable ids, the
   decoder refuses an undeclared tag, and a completed per-project pass clears
   the runner ids it did not re-emit.
2. A ``bundle_materialization`` row behind canonical used to be reported as
   ``schema_migration_script_missing`` ("no edge shipped", remedy: inspect
   the migration). The row is written by the bundle's own record, so behind
   means THAT record did not happen: ``schema_version_unrecorded``, remedy:
   re-run the bundle update. An absent row is never stamped by the runner.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.common.launcher_db_fixture import add_project, create_empty_launcher_db  # noqa: E402
from vco_lib import artifact_version_registry as avr  # noqa: E402
from vco_lib import deferral_emit, deferral_registry, project_init  # noqa: E402
from vco_lib import schema_migration_runner as smr  # noqa: E402
from vco_lib import schema_versions as sv  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402


@pytest.fixture
def db(tmp_path):
    path = create_empty_launcher_db(tmp_path / "launcher.db", up_to=33)
    add_project(path, project_id="p1", name="p", folder_path=str(tmp_path),
                slug="p1", created_at=1, updated_at=1)
    return path


def _put_row_behind(db_path: Path, artifact_type: str) -> int:
    canonical = sv.canonical_version(artifact_type)
    assert avr.register_artifact_version(
        db_path, project_id="p1", artifact_type=artifact_type,
        artifact_name=avr.DEFAULT_ARTIFACT_NAME, schema_version=canonical,
        materialized_at=1)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE artifact_schema_versions SET schema_version = ? "
                     "WHERE artifact_type = ?", (canonical - 1, artifact_type))
    return canonical


def _run(db_path: Path, tmp_path: Path, **kw) -> smr.MigrationRunReport:
    empty = tmp_path / "no-migrations"
    empty.mkdir(exist_ok=True)
    kw.setdefault("project_id", "p1")
    return smr.run_schema_migrations(
        db_path=db_path, migrations_dir=empty, env={},
        weaviate_url="http://127.0.0.1:9", include_orchestrator_wide=False,
        live_drift_probe=lambda *_: (False, []),
        codegraph_drift_probe=lambda *_: (False, []), now_ms=1, **kw)


def _errors(report, artifact_type):
    return [d for (a, _n, d) in report.errors if a == artifact_type]


# ── 2. behind ≠ missing edge ────────────────────────────────────────────────


def test_a_bundle_row_behind_canonical_is_unrecorded_not_a_missing_edge(db, tmp_path):
    canonical = _put_row_behind(db, "bundle_materialization")
    report = _run(db, tmp_path)
    [detail] = _errors(report, "bundle_materialization")
    assert detail.endswith("[schema_version_unrecorded]"), detail
    assert f"v{canonical - 1}" in detail and f"v{canonical}" in detail
    [entry] = [e for e in smr.build_deferral_entries(report)
               if e.condition_id == "schema_version_unrecorded"]
    assert "Update bundle" in entry.command_to_apply
    assert "install.py --update" in entry.command_to_apply
    assert "migrate-schema" not in entry.command_to_apply
    assert "none is missing" in entry.why_deferred


def test_any_other_type_behind_with_no_edge_is_still_a_missing_edge(db, tmp_path):
    """The contrast: a type this runner migrates keeps its true finding."""
    _put_row_behind(db, "module_settings_shape")
    report = _run(db, tmp_path)
    [detail] = _errors(report, "module_settings_shape")
    assert detail.endswith("[schema_migration_script_missing]"), detail
    [entry] = [e for e in smr.build_deferral_entries(report)
               if e.condition_id == "schema_migration_script_missing"]
    assert "migrate-schema" in entry.command_to_apply


def test_an_absent_bundle_row_is_never_stamped_by_the_runner(db, tmp_path):
    report = _run(db, tmp_path)
    assert not _errors(report, "bundle_materialization")
    assert not [r for r in report.registered if r[0] == "bundle_materialization"]
    assert avr.check_artifact_version(
        db, project_id="p1", artifact_type="bundle_materialization",
        artifact_name=avr.DEFAULT_ARTIFACT_NAME,
    ) is avr.ArtifactVersionStatus.NEVER_MATERIALIZED


def test_no_project_means_no_bundle_row_at_all(db, tmp_path):
    report = _run(db, tmp_path, project_id=None)
    assert not [r for r in report.registered + report.errors
                if r[0] == "bundle_materialization"]


# ── 1. declared ids, decoder, paired clear ─────────────────────────────────


def test_every_emittable_runner_id_is_declared_in_the_registry():
    ids = smr.ERROR_CONDITION_IDS | {smr.UNTAGGED_CONDITION_ID}
    for cid in ids:
        assert deferral_registry.matches_registered_pattern(cid), cid
    for prefix in smr.ERROR_CONDITION_PREFIXES:
        assert deferral_registry.matches_registered_pattern(prefix + "4_to_5")


def test_the_decoder_files_an_undeclared_tag_under_the_untagged_id():
    report = smr.MigrationRunReport()
    report.errors.append(("a", "n", "boom [schema_migration_script_missing]"))
    report.errors.append(("b", "n", "boom [not_a_declared_id]"))
    report.errors.append(("c", "n", "no tag at all"))
    report.errors.append(("d", "n", "x [schema_migration_needs_choice]"))
    assert [e.condition_id for e in smr.build_deferral_entries(report)] == [
        "schema_migration_script_missing",
        smr.UNTAGGED_CONDITION_ID,
        smr.UNTAGGED_CONDITION_ID,
    ]


def test_runner_conditions_to_clear_act_and_leave_alone():
    present = {
        "schema_migration_script_missing",       # runner, not re-emitted → clear
        "schema_migration_probe_unreachable",    # runner, re-emitted → keep
        "schema_migration_failed_4_to_5",        # runner edge id → clear
        "schema_migration_failed_migrate-development-temporal-props",  # install.py's → keep
        "schema_regenerate_or_defer_X",          # a user choice → keep
        "hub_restart_failed_after_abort",        # foreign → keep
    }
    assert smr.runner_conditions_to_clear(
        present, {"schema_migration_probe_unreachable"}) == [
        "schema_migration_failed_4_to_5", "schema_migration_script_missing"]


def _entry(cid: str) -> DeferralEntry:
    return DeferralEntry(condition_id=cid, title="t", detected="d",
                         why_deferred="w", command_to_apply="c", severity="warning")


def _cids(folder: Path) -> set:
    return {e.condition_id for e in DeferralReport.read(folder).entries}


def test_a_per_project_pass_clears_what_it_did_not_re_emit(tmp_path):
    deferral_emit.emit_entries(tmp_path, [
        _entry("schema_migration_script_missing"), _entry("schema_version_unrecorded")])
    project_init._clear_runner_conditions_not_reemitted(
        tmp_path, [_entry("schema_version_unrecorded")], include_wide=False)
    assert _cids(tmp_path) == {"schema_version_unrecorded"}


def test_the_orchestrator_root_ledger_is_left_to_install_py(tmp_path, monkeypatch):
    """A per-project pass over the root does not evaluate orchestrator-wide
    artifacts, so it must not clear the root ledger's runner ids."""
    monkeypatch.setattr(project_init, "__file__", str(tmp_path / "vco_lib" / "project_init.py"))
    deferral_emit.emit_entries(tmp_path, [_entry("schema_migration_script_missing")])
    project_init._clear_runner_conditions_not_reemitted(tmp_path, [], include_wide=False)
    assert _cids(tmp_path) == {"schema_migration_script_missing"}
    project_init._clear_runner_conditions_not_reemitted(tmp_path, [], include_wide=True)
    assert _cids(tmp_path) == set()
