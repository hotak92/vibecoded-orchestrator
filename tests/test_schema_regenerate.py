# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the Piece-4 "Regenerate now" guarded recreate action.

Covers SPEC-v0260-migration-runner.md test items:
  T-modal-regenerate  — apply choice "regenerate" invokes the guarded recreate;
                        on success unregister→drop→re-sync→register at canonical;
                        on GUARD-2 refusal (exit 3) returns the refusal WITHOUT
                        dropping.
  + per-type dispatch  — shared-KG routes through migrate-shared-kg-schema;
                        per-project routes through migrate-collections
                        --force-rebuild; codegraph routes through
                        code-graph-analyze --force.
  + the --regenerate CLI surface actually performs the recreate (no longer the
    pending_piece4 stub).

Every subprocess/script call is injected via the ``runner`` parameter, so no
live Weaviate / shell is needed. ``when`` (materialized_at) is injected because
the agent env has no wall clock.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from vco_lib import artifact_version_registry as avr  # noqa: E402
from vco_lib import schema_regenerate as sregen  # noqa: E402
from vco_lib import schema_versions as sv  # noqa: E402


@pytest.fixture
def db_with_v033(tmp_path):
    """Fresh launcher.db with migrations 1..33 + a project row."""
    db_path = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE _schema_migrations ("
        "  version INTEGER PRIMARY KEY,"
        "  description TEXT NOT NULL,"
        "  applied_at INTEGER NOT NULL"
        ")"
    )
    migrations_dir = (
        _REPO / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "db" / "migrations"
    )
    for f in sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql")):
        conn.executescript(f.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO projects (id, name, folder_path, host, slug, "
        "created_at, updated_at, rl_port) "
        "VALUES ('p1', 'test', '/tmp/p1', 'base', 'p1', 1, 1, NULL)"
    )
    conn.commit()
    conn.close()
    return db_path


class _FakeRunner:
    """Captures invoked commands; returns a scripted CompletedProcess."""

    def __init__(self, *, returncode=0, stdout="", stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            cmd, self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def _mk_scripts(orch_root: Path, folder: Path):
    """Create the migrate-shared-kg-schema + code-graph-analyze script files
    (existence-checked by the regenerate dispatch; bodies never run because the
    runner is faked)."""
    (orch_root / "scripts").mkdir(parents=True, exist_ok=True)
    (orch_root / "scripts" / "migrate-shared-kg-schema.sh").write_text("#!/bin/sh\nexit 0\n")
    (orch_root / "scripts" / "migrate-shared-kg-schema.ps1").write_text("exit 0\n")
    cgdir = folder / ".claude" / "scripts"
    cgdir.mkdir(parents=True, exist_ok=True)
    (cgdir / "code-graph-analyze").write_text("#!/bin/sh\nexit 0\n")
    (cgdir / "code-graph-analyze.ps1").write_text("exit 0\n")


# ---------------------------------------------------------------------------
# Shared-KG branch (the canonical POLICY STEP 3 case)
# ---------------------------------------------------------------------------


def test_shared_kg_regenerate_success_registers(db_with_v033, tmp_path, monkeypatch):
    orch = tmp_path / "orch"
    folder = tmp_path / "proj"
    _mk_scripts(orch, folder)
    monkeypatch.setattr(sys, "platform", "linux")
    runner = _FakeRunner(returncode=0, stderr="[migrate-shared-kg] Done.\n")

    res = sregen.regenerate_derived_collection(
        artifact_type="shared_kg_collection",
        artifact_name="VibeCodedOrchestrator_KnowledgeGraph",
        folder=folder,
        db_path=db_with_v033,
        project_id=None,  # orchestrator-wide → NULL key
        project_name=None,
        env={"WEAVIATE_URL": "http://x:8081"},
        weaviate_url="http://x:8081",
        when=42,
        orchestrator_root=orch,
        runner=runner,
    )

    assert res.ok is True
    assert res.dropped is True
    assert res.refused is False
    assert res.registered is True
    # The guarded script was the command invoked, cwd=orchestrator root.
    assert any("migrate-shared-kg-schema.sh" in str(c[0]) for c in runner.calls)
    # SHARED_KG_COLLECTION pinned to the requested class in the script env.
    _, kwargs = runner.calls[0]
    assert kwargs["env"]["SHARED_KG_COLLECTION"] == "VibeCodedOrchestrator_KnowledgeGraph"
    # Registered at canonical, keyed NULL (orchestrator-wide).
    canonical = sv.canonical_version("shared_kg_collection")
    status = avr.check_artifact_version(
        db_with_v033, project_id=None,
        artifact_type="shared_kg_collection",
        artifact_name="VibeCodedOrchestrator_KnowledgeGraph",
    )
    assert status == avr.ArtifactVersionStatus.UP_TO_DATE
    assert canonical >= 1


@pytest.mark.parametrize("exit_code,guard", [(3, "cross-project"), (4, "kg-sync")])
def test_shared_kg_regenerate_guard_refusal_no_drop(
    db_with_v033, tmp_path, monkeypatch, exit_code, guard
):
    orch = tmp_path / "orch"
    folder = tmp_path / "proj"
    _mk_scripts(orch, folder)
    monkeypatch.setattr(sys, "platform", "linux")
    runner = _FakeRunner(returncode=exit_code, stderr="[migrate-shared-kg] REFUSED\n")

    res = sregen.regenerate_derived_collection(
        artifact_type="shared_kg_collection",
        artifact_name="VibeCodedOrchestrator_KnowledgeGraph",
        folder=folder, db_path=db_with_v033, project_id=None, project_name=None,
        env={}, weaviate_url="http://x:8081", when=42,
        orchestrator_root=orch, runner=runner,
    )

    assert res.refused is True
    assert res.ok is False
    assert res.dropped is False  # NOTHING dropped on a guard refusal
    assert res.registered is False
    # The registry row was NOT created (stale fingerprint re-prompts next time).
    status = avr.check_artifact_version(
        db_with_v033, project_id=None,
        artifact_type="shared_kg_collection",
        artifact_name="VibeCodedOrchestrator_KnowledgeGraph",
    )
    assert status == avr.ArtifactVersionStatus.NEVER_MATERIALIZED


def test_shared_kg_missing_script_errors_no_drop(db_with_v033, tmp_path, monkeypatch):
    orch = tmp_path / "orch"  # no scripts/ dir
    folder = tmp_path / "proj"
    monkeypatch.setattr(sys, "platform", "linux")
    runner = _FakeRunner()
    res = sregen.regenerate_derived_collection(
        artifact_type="shared_kg_collection",
        artifact_name="VibeCodedOrchestrator_KnowledgeGraph",
        folder=folder, db_path=db_with_v033, project_id=None, project_name=None,
        env={}, weaviate_url="http://x:8081", when=42,
        orchestrator_root=orch, runner=runner,
    )
    assert res.error is not None
    assert res.dropped is False
    assert runner.calls == []  # script never invoked when absent


# ---------------------------------------------------------------------------
# Per-project KG / Dev / Diagrams branch (migrate-collections --force-rebuild)
# ---------------------------------------------------------------------------


def test_per_project_kg_regenerate_success(db_with_v033, tmp_path):
    orch = tmp_path / "orch"
    folder = tmp_path / "proj"
    (orch / "scripts").mkdir(parents=True, exist_ok=True)
    # migrate-collections returns a JSON envelope with a rebuild + clean reingest.
    envelope = json.dumps({
        "plan": [{"collection": "P1_KnowledgeGraph", "action": "rebuild"}],
        "reingest_required": False,
        "errors": [],
    })
    runner = _FakeRunner(returncode=0, stdout=envelope)
    res = sregen.regenerate_derived_collection(
        artifact_type="kg_collection",
        artifact_name="P1_KnowledgeGraph",
        folder=folder, db_path=db_with_v033, project_id="p1", project_name="P1",
        env={}, weaviate_url="http://x:8081", when=99,
        orchestrator_root=orch, runner=runner,
    )
    assert res.ok is True
    assert res.dropped is True
    assert res.registered is True
    # The --force-rebuild --project-folder CLI was the command invoked.
    cmd0 = runner.calls[0][0]
    assert "migrate-collections" in cmd0
    assert "--force-rebuild" in cmd0
    assert "--project-folder" in cmd0
    # Keyed by the real project_id (not NULL).
    status = avr.check_artifact_version(
        db_with_v033, project_id="p1",
        artifact_type="kg_collection", artifact_name="P1_KnowledgeGraph",
    )
    assert status == avr.ArtifactVersionStatus.UP_TO_DATE


def test_per_project_reingest_required_does_not_register(db_with_v033, tmp_path):
    orch = tmp_path / "orch"
    folder = tmp_path / "proj"
    (orch / "scripts").mkdir(parents=True, exist_ok=True)
    envelope = json.dumps({
        "plan": [{"collection": "P1_KnowledgeGraph", "action": "rebuild"}],
        "reingest_required": True,  # drop happened but re-ingest didn't complete
        "errors": [],
    })
    runner = _FakeRunner(returncode=0, stdout=envelope)
    res = sregen.regenerate_derived_collection(
        artifact_type="kg_collection", artifact_name="P1_KnowledgeGraph",
        folder=folder, db_path=db_with_v033, project_id="p1", project_name="P1",
        env={}, weaviate_url="http://x:8081", when=99,
        orchestrator_root=orch, runner=runner,
    )
    assert res.ok is False
    assert res.dropped is True       # the drop DID happen (data on disk → safe)
    assert res.registered is False   # but NOT registered (re-ingest unproven)
    assert res.reingest_incomplete is True  # C1: flag set for the deferral
    assert res.error is not None
    # Registry untouched → next update re-detects.
    status = avr.check_artifact_version(
        db_with_v033, project_id="p1",
        artifact_type="kg_collection", artifact_name="P1_KnowledgeGraph",
    )
    assert status == avr.ArtifactVersionStatus.NEVER_MATERIALIZED


def test_per_project_requires_project_name(db_with_v033, tmp_path):
    res = sregen.regenerate_derived_collection(
        artifact_type="development_collection", artifact_name="P1_Development",
        folder=tmp_path, db_path=db_with_v033, project_id="p1", project_name=None,
        env={}, weaviate_url="http://x:8081", when=1,
        orchestrator_root=tmp_path, runner=_FakeRunner(),
    )
    assert res.ok is False
    assert res.error is not None and "project name" in res.error


# ---------------------------------------------------------------------------
# Codegraph branch (code-graph-analyze --force-recreate)
# ---------------------------------------------------------------------------


def test_codegraph_regenerate_success(db_with_v033, tmp_path, monkeypatch):
    orch = tmp_path / "orch"
    folder = tmp_path / "proj"
    _mk_scripts(orch, folder)
    monkeypatch.setattr(sys, "platform", "linux")
    runner = _FakeRunner(returncode=0)
    res = sregen.regenerate_derived_collection(
        artifact_type="codegraph_collection", artifact_name="P1_CodeFunction",
        folder=folder, db_path=db_with_v033, project_id="p1", project_name="P1",
        env={}, weaviate_url="http://x:8081", when=7,
        orchestrator_root=orch, runner=runner,
    )
    assert res.ok is True
    assert res.dropped is True
    cmd0 = runner.calls[0][0]
    assert any("code-graph-analyze" in str(part) for part in cmd0)
    # C2: the EXACT --force-recreate flag must be present (NOT the --force
    # abbreviation, which analyze_code_graph.py only accepts via argparse
    # prefix-matching and would break the moment another --force* flag exists).
    assert "--force-recreate" in cmd0
    assert "--force" not in cmd0  # the bare abbreviation must NOT be used


def test_codegraph_regenerate_success_windows(db_with_v033, tmp_path, monkeypatch):
    """The Windows .ps1 path also passes the explicit --force-recreate flag."""
    orch = tmp_path / "orch"
    folder = tmp_path / "proj"
    _mk_scripts(orch, folder)
    monkeypatch.setattr(sys, "platform", "win32")
    runner = _FakeRunner(returncode=0)
    res = sregen.regenerate_derived_collection(
        artifact_type="codegraph_collection", artifact_name="P1_CodeFunction",
        folder=folder, db_path=db_with_v033, project_id="p1", project_name="P1",
        env={}, weaviate_url="http://x:8081", when=7,
        orchestrator_root=orch, runner=runner,
    )
    assert res.ok is True
    cmd0 = runner.calls[0][0]
    assert "--force-recreate" in cmd0
    assert "--force" not in cmd0


# ---------------------------------------------------------------------------
# Unknown type / unregisterable type
# ---------------------------------------------------------------------------


def test_unknown_artifact_type(db_with_v033, tmp_path):
    res = sregen.regenerate_derived_collection(
        artifact_type="does_not_exist", artifact_name="X",
        folder=tmp_path, db_path=db_with_v033, project_id=None, project_name=None,
        env={}, weaviate_url="http://x:8081", when=1,
        orchestrator_root=tmp_path, runner=_FakeRunner(),
    )
    assert res.ok is False
    assert res.error is not None


# ---------------------------------------------------------------------------
# The --regenerate CLI surface actually performs the recreate (no stub)
# ---------------------------------------------------------------------------


def test_cli_regenerate_invokes_real_recreate(db_with_v033, tmp_path, monkeypatch, capsys):
    """`migrate-schema --regenerate ... --artifact-name ...` is no longer the
    pending_piece4 stub — it runs the guarded recreate + emits a JSON result
    carrying ok/dropped/registered (not action=pending_piece4)."""
    from vco_lib import project_init as pinit

    orch = Path(pinit.__file__).resolve().parent.parent  # real clone root
    folder = tmp_path / "proj"
    folder.mkdir()
    # Fake the actual recreate so no live Weaviate is touched.
    sentinel = {}

    def _fake_regen(**kwargs):
        sentinel.update(kwargs)
        return sregen.RegenerateResult(
            artifact_type=kwargs["artifact_type"],
            artifact_name=kwargs["artifact_name"],
            ok=True, dropped=True, registered=True, detail="faked ok",
        )

    monkeypatch.setattr(sregen, "regenerate_derived_collection", _fake_regen)
    monkeypatch.setenv("WEAVIATE_URL", "http://x:8081")
    monkeypatch.setenv("PROJECT_NAME", "P1")

    args = __import__("argparse").Namespace(
        folder=str(folder), db=str(db_with_v033), project_id="p1",
        migrations_dir=None, include_orchestrator_wide=False,
        regenerate="shared_kg_collection",
        artifact_name="VibeCodedOrchestrator_KnowledgeGraph",
        project_name=None, strict=False, now_ms=123, check=False,
    )
    rc = pinit._cmd_migrate_schema(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["action"] == "regenerate"       # NOT pending_piece4
    assert out["ok"] is True
    assert out["dropped"] is True
    # shared_kg is orchestrator-wide → project_id passed as None.
    assert sentinel["project_id"] is None
    assert sentinel["when"] == 123


# ---------------------------------------------------------------------------
# C1 — dropped-but-not-reingested → schema_reingest_incomplete_<slug> deferral
# ---------------------------------------------------------------------------


def test_c1_build_reingest_incomplete_entry():
    """The builder emits a schema_reingest_incomplete_<slug> entry only for a
    dropped-but-not-ok-with-reingest_incomplete result, naming the right
    remediation command per artifact type."""
    folder = Path("/tmp/proj")
    # KG / Dev / Diagrams → kg-sync remediation.
    kg = sregen.RegenerateResult(
        artifact_type="kg_collection", artifact_name="P1_KnowledgeGraph",
        dropped=True, ok=False, reingest_incomplete=True, error="reingest_required",
    )
    entry = sregen.build_reingest_incomplete_entry(kg, folder)
    assert entry is not None
    assert entry.condition_id == "schema_reingest_incomplete_P1_KnowledgeGraph"
    assert "kg-sync --all" in entry.command_to_apply
    assert entry.severity == "warning"

    # Codegraph → code-graph-analyze --force-recreate remediation.
    cg = sregen.RegenerateResult(
        artifact_type="codegraph_collection", artifact_name="P1_CodeFunction",
        dropped=True, ok=False, reingest_incomplete=True,
    )
    cg_entry = sregen.build_reingest_incomplete_entry(cg, folder)
    assert cg_entry is not None
    assert "code-graph-analyze . --force-recreate" in cg_entry.command_to_apply

    # A clean (ok) result → no entry (defensive).
    ok = sregen.RegenerateResult(
        artifact_type="kg_collection", artifact_name="P1_KnowledgeGraph",
        dropped=True, ok=True, registered=True,
    )
    assert sregen.build_reingest_incomplete_entry(ok, folder) is None


def test_c1_cli_writes_reingest_incomplete_deferral(
    db_with_v033, tmp_path, monkeypatch, capsys
):
    """A dropped-but-not-reingested regenerate via the CLI PERSISTS the
    schema_reingest_incomplete_<slug> entry to UPDATE_DEFERRED.md — so the next
    update/session re-ingests instead of the runner silently registering an
    empty collection at canonical."""
    from vco_lib import project_init as pinit
    from vco_lib.deferral_report import DeferralReport

    folder = tmp_path / "proj"
    folder.mkdir()

    def _fake_regen(**kwargs):
        # Simulate the migrate-collections reingest_required outcome.
        return sregen.RegenerateResult(
            artifact_type=kwargs["artifact_type"],
            artifact_name=kwargs["artifact_name"],
            ok=False, dropped=True, registered=False, reingest_incomplete=True,
            error="reingest_required=true",
        )

    monkeypatch.setattr(sregen, "regenerate_derived_collection", _fake_regen)
    monkeypatch.setenv("WEAVIATE_URL", "http://x:8081")
    monkeypatch.setenv("PROJECT_NAME", "P1")

    args = __import__("argparse").Namespace(
        folder=str(folder), db=str(db_with_v033), project_id="p1",
        migrations_dir=None, include_orchestrator_wide=False,
        regenerate="kg_collection", artifact_name="P1_KnowledgeGraph",
        project_name="P1", strict=False, now_ms=1, check=False,
    )
    rc = pinit._cmd_migrate_schema(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["reingest_incomplete"] is True
    assert out["reingest_deferral_written"] is True

    # The deferral actually landed in UPDATE_DEFERRED.md (durable).
    report = DeferralReport.read(folder)
    assert report.has_condition("schema_reingest_incomplete_P1_KnowledgeGraph")


def test_c1_cli_no_deferral_on_clean_regenerate(
    db_with_v033, tmp_path, monkeypatch, capsys
):
    """A clean (ok) regenerate writes NO reingest-incomplete deferral."""
    from vco_lib import project_init as pinit
    from vco_lib.deferral_report import DeferralReport

    folder = tmp_path / "proj"
    folder.mkdir()

    def _fake_regen(**kwargs):
        return sregen.RegenerateResult(
            artifact_type=kwargs["artifact_type"],
            artifact_name=kwargs["artifact_name"],
            ok=True, dropped=True, registered=True,
        )

    monkeypatch.setattr(sregen, "regenerate_derived_collection", _fake_regen)
    monkeypatch.setenv("WEAVIATE_URL", "http://x:8081")

    args = __import__("argparse").Namespace(
        folder=str(folder), db=str(db_with_v033), project_id="p1",
        migrations_dir=None, include_orchestrator_wide=False,
        regenerate="kg_collection", artifact_name="P1_KnowledgeGraph",
        project_name="P1", strict=False, now_ms=1, check=False,
    )
    pinit._cmd_migrate_schema(args)
    out = json.loads(capsys.readouterr().out)
    assert out["reingest_deferral_written"] is False
    report = DeferralReport.read(folder)
    assert not report.has_condition("schema_reingest_incomplete_P1_KnowledgeGraph")


def test_regenerate_check_is_a_dry_run_never_mutates(
    db_with_v033, tmp_path, monkeypatch, capsys
):
    """`--regenerate <type> --check` must NOT perform the destructive recreate.

    Regression for the dogfood-caught bug: the --regenerate handler ignored
    --check and called regenerate_derived_collection() unconditionally, so a
    dry-run actually dropped+rebuilt the live collection (and hung on the
    re-ingest). --check must return a plan WITHOUT touching Weaviate.
    """
    from vco_lib import project_init as pinit

    folder = tmp_path / "proj"
    folder.mkdir()

    called = {"n": 0}

    def _must_not_run(**kwargs):  # pragma: no cover - asserted not-called
        called["n"] += 1
        raise AssertionError(
            "regenerate_derived_collection MUST NOT be called under --check"
        )

    monkeypatch.setattr(sregen, "regenerate_derived_collection", _must_not_run)
    monkeypatch.setenv("WEAVIATE_URL", "http://x:8081")

    args = __import__("argparse").Namespace(
        folder=str(folder), db=str(db_with_v033), project_id="p1",
        migrations_dir=None, include_orchestrator_wide=False,
        regenerate="kg_collection", artifact_name="P1_KnowledgeGraph",
        project_name="P1", strict=False, now_ms=1, check=True,  # ← the dry-run
    )
    rc = pinit._cmd_migrate_schema(args)
    out = json.loads(capsys.readouterr().out)

    assert called["n"] == 0, "destructive recreate ran under --check"
    assert rc == 0
    assert out["mode"] == "regenerate-check"
    assert out["would_regenerate"] is True
    assert out["ok"] is True
