# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``vco verify-env-projection`` (Phase 0.C acceptance).

Stubbed Phase 0.B APIs (post-merge integration checklist for the
reviewer):

* ``vco_lib.config_projection.project_env_from_db(project_id: str) -> dict[str, str]``
  — returns the canonical env bundle for the project. Raises
  ``LookupError`` when the project is not registered in the launcher DB.
* ``vco_lib.config_projection.apply_project_env(bundle, *, project_folder: Path) -> ApplyResult``
  — writes the bundle atomically to all three on-disk surfaces.
  Return must expose ``.ok`` and ``.message`` (dict or attr-style).
* ``vco_lib.config_projection.resolve_project_folder(project_id) -> Path``
  — maps a slug/rowid to its on-disk folder root.
* ``vco_lib.config_projection.list_registered_projects() -> Iterable[Mapping[str, str]]``
  — for ``--all``; yields ``{"id": ..., "slug": ..., "folder": ...}``.

Coverage:
* All-match → exit 0.
* Mutation to one of the three surfaces → drift detected → exit 1.
* ``--fix`` repairs to byte-identical state.
* Round-trip idempotency: a second ``--fix`` is a no-op.
* JSON envelope schema sane.
* Project not found → exit 2 (project_not_found).
* DB unreadable → exit 2 (db_unreadable).
* ``--all`` aggregates worst exit code across multiple projects.
* ``--fix`` failure from apply_project_env propagates as exit 3.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.cli import verify  # noqa: E402


# ---------------------------------------------------------------------------
# Canonical fixture: one project, one env bundle.
# ---------------------------------------------------------------------------

CANONICAL_BUNDLE: dict[str, str] = {
    "KG_COLLECTION": "MyProject_KnowledgeGraph",
    "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
    "DEVELOPMENT_COLLECTION": "MyProject_Development",
    "PROJECT_NAME": "MyProject",
    "VCT_KG_ACCESS_LIST": "MyProject_KnowledgeGraph,OtherProject_KnowledgeGraph",
    "VCT_CODE_GRAPH_ACCESS_LIST": "MyProject",
    "SHARED_KG_WRITE_DISABLED": "false",
}


def _write_canonical_surfaces(folder: Path, bundle: Mapping[str, str]) -> None:
    """Lay down the three on-disk surfaces in canonical state. Mirrors
    what ``config_projection.apply_project_env`` is expected to produce.
    """
    claude_dir = folder / ".claude"
    vscode_dir = folder / ".vscode"
    claude_dir.mkdir(parents=True, exist_ok=True)
    vscode_dir.mkdir(parents=True, exist_ok=True)

    # .claude/settings.json — top-level "env" mapping.
    settings = {"env": dict(bundle)}
    (claude_dir / "settings.json").write_text(
        json.dumps(settings, indent=2, sort_keys=True), encoding="utf-8"
    )

    # .claude/env — KEY=VALUE shell-style, alphabetical.
    lines = [f"{k}={v}" for k, v in sorted(bundle.items())]
    (claude_dir / "env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # .vscode/settings.json — claude-code.env mapping.
    vscode_settings = {"claude-code.env": dict(bundle)}
    (vscode_dir / "settings.json").write_text(
        json.dumps(vscode_settings, indent=2, sort_keys=True), encoding="utf-8"
    )


@pytest.fixture
def project_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "myproject"
    folder.mkdir()
    _write_canonical_surfaces(folder, CANONICAL_BUNDLE)
    return folder


@pytest.fixture
def stub_db(monkeypatch, project_folder):
    """Stub Phase 0.B resolver + folder-resolver for ``myproject``."""
    def _from_db(project_id: str) -> dict[str, str]:
        if project_id != "myproject":
            raise LookupError(f"project not found: {project_id}")
        return dict(CANONICAL_BUNDLE)

    def _resolve_folder(project_id: str) -> Path:
        if project_id != "myproject":
            raise LookupError(f"project folder not found: {project_id}")
        return project_folder

    monkeypatch.setattr(verify, "_project_env_from_db", _from_db)
    monkeypatch.setattr(verify, "_resolve_project_folder", _resolve_folder)
    return project_folder


def _stub_apply(monkeypatch, *, project_folder: Path, ok: bool = True, message: str = "ok"):
    """Default apply: writes canonical surfaces back to disk."""
    calls = {"n": 0, "last_bundle": None}

    def _fake_apply(bundle, *, project_folder=project_folder):
        calls["n"] += 1
        calls["last_bundle"] = dict(bundle)
        if ok:
            _write_canonical_surfaces(project_folder, bundle)
        return {"ok": ok, "message": message}

    monkeypatch.setattr(verify, "_apply_project_env", _fake_apply)
    return calls


def _args(
    project_id: str | None = "myproject",
    *,
    json_mode: bool = False,
    fix: bool = False,
    all_: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        project_id=project_id,
        json=json_mode,
        fix=fix,
        all=all_,
    )


# ---------------------------------------------------------------------------
# Tests — single-project verify
# ---------------------------------------------------------------------------


def test_canonical_state_exits_zero(stub_db, capsys):
    code = verify.cmd_verify_env_projection(_args())
    assert code == verify.EXIT_OK
    out = capsys.readouterr().out
    assert "OK" in out
    assert "myproject" in out


def test_mutation_claude_settings_detected(stub_db, project_folder, capsys):
    # Mutate just the .claude/settings.json value for one key.
    settings_path = project_folder / ".claude" / "settings.json"
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    payload["env"]["KG_COLLECTION"] = "WrongName_KnowledgeGraph"
    settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    code = verify.cmd_verify_env_projection(_args())
    assert code == verify.EXIT_DRIFT
    out = capsys.readouterr().out
    assert "DRIFT" in out
    assert ".claude/settings.json" in out
    assert "KG_COLLECTION" in out
    assert "WrongName_KnowledgeGraph" in out


def test_mutation_claude_env_detected(stub_db, project_folder, capsys):
    # Mutate just the .claude/env file.
    env_path = project_folder / ".claude" / "env"
    content = env_path.read_text(encoding="utf-8").replace(
        "PROJECT_NAME=MyProject", "PROJECT_NAME=Tampered"
    )
    env_path.write_text(content, encoding="utf-8")

    code = verify.cmd_verify_env_projection(_args())
    assert code == verify.EXIT_DRIFT
    out = capsys.readouterr().out
    assert ".claude/env" in out
    assert "PROJECT_NAME" in out
    assert "Tampered" in out


def test_mutation_vscode_settings_detected(stub_db, project_folder, capsys):
    vscode_path = project_folder / ".vscode" / "settings.json"
    payload = json.loads(vscode_path.read_text(encoding="utf-8"))
    payload["claude-code.env"]["DEVELOPMENT_COLLECTION"] = "Drifted_Development"
    vscode_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    code = verify.cmd_verify_env_projection(_args())
    assert code == verify.EXIT_DRIFT
    out = capsys.readouterr().out
    assert ".vscode/settings.json" in out
    assert "Drifted_Development" in out


def test_missing_surface_treated_as_full_drift(stub_db, project_folder, capsys):
    # Delete the .claude/env entirely → every key on that surface should
    # register as drift.
    (project_folder / ".claude" / "env").unlink()
    code = verify.cmd_verify_env_projection(_args())
    assert code == verify.EXIT_DRIFT
    out = capsys.readouterr().out
    assert ".claude/env" in out
    assert "<missing>" in out


def test_fix_repairs_to_canonical_state(stub_db, project_folder, monkeypatch):
    # Mutate one surface.
    settings_path = project_folder / ".claude" / "settings.json"
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    payload["env"]["KG_COLLECTION"] = "Drifted"
    settings_path.write_text(json.dumps(payload), encoding="utf-8")

    apply_calls = _stub_apply(monkeypatch, project_folder=project_folder)

    code = verify.cmd_verify_env_projection(_args(fix=True))
    assert code == verify.EXIT_OK
    assert apply_calls["n"] == 1
    # Bundle passed to apply matches canonical.
    assert apply_calls["last_bundle"] == CANONICAL_BUNDLE

    # Round-trip idempotency check: a second --fix is a no-op (apply
    # may or may not be re-invoked depending on whether the first verify
    # finds drift, but exit code must remain OK).
    code2 = verify.cmd_verify_env_projection(_args(fix=True))
    assert code2 == verify.EXIT_OK


def test_fix_failure_apply_returns_not_ok(stub_db, project_folder, monkeypatch, capsys):
    # Mutate to force --fix.
    (project_folder / ".claude" / "env").unlink()
    _stub_apply(
        monkeypatch,
        project_folder=project_folder,
        ok=False,
        message="permission denied writing .claude/env",
    )
    code = verify.cmd_verify_env_projection(_args(fix=True))
    assert code == verify.EXIT_USAGE
    err = capsys.readouterr().err
    assert "permission denied" in err


def test_fix_failure_apply_raises(stub_db, project_folder, monkeypatch, capsys):
    (project_folder / ".claude" / "env").unlink()

    def _boom(bundle, *, project_folder):
        raise OSError("disk is full")

    monkeypatch.setattr(verify, "_apply_project_env", _boom)
    code = verify.cmd_verify_env_projection(_args(fix=True))
    assert code == verify.EXIT_USAGE
    err = capsys.readouterr().err
    assert "disk is full" in err


def test_fix_idempotency_broken_exits_three(stub_db, project_folder, monkeypatch, capsys):
    """If apply_project_env claims ok but the surfaces still drift after,
    that's a broken projection contract — we MUST surface it, not pretend
    everything's fine."""
    # Mutate one surface to force --fix to run.
    (project_folder / ".claude" / "env").unlink()

    def _lying_apply(bundle, *, project_folder=project_folder):
        # Claim success but don't actually write anything.
        return {"ok": True, "message": "lied about writing"}

    monkeypatch.setattr(verify, "_apply_project_env", _lying_apply)
    code = verify.cmd_verify_env_projection(_args(fix=True))
    assert code == verify.EXIT_USAGE
    err = capsys.readouterr().err
    assert "idempotency check" in err or "idempotent" in err


def test_project_not_found_exits_two(monkeypatch, capsys):
    def _missing(_id):
        raise LookupError("no such project")

    monkeypatch.setattr(verify, "_project_env_from_db", _missing)
    monkeypatch.setattr(verify, "_resolve_project_folder", _missing)
    code = verify.cmd_verify_env_projection(_args("ghost"))
    assert code == verify.EXIT_TOOL_MISSING
    err = capsys.readouterr().err
    assert "not found" in err
    assert "ghost" in err


def test_db_unreadable_exits_two(monkeypatch, capsys):
    def _boom(_id):
        raise RuntimeError("sqlite locked")

    monkeypatch.setattr(verify, "_project_env_from_db", _boom)
    monkeypatch.setattr(verify, "_resolve_project_folder", _boom)
    code = verify.cmd_verify_env_projection(_args("anything"))
    assert code == verify.EXIT_TOOL_MISSING
    err = capsys.readouterr().err
    assert "sqlite locked" in err or "db_unreadable" in err.lower() or "unreadable" in err.lower()


def test_json_schema_ok(stub_db, capsys):
    code = verify.cmd_verify_env_projection(_args(json_mode=True))
    assert code == verify.EXIT_OK
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["command"] == "verify-env-projection"
    assert payload["project_id"] == "myproject"
    assert payload["exit_code"] == verify.EXIT_OK
    assert payload["overall"] == "ok"
    assert set(payload["expected_keys"]) == set(CANONICAL_BUNDLE.keys())


def test_json_schema_drift(stub_db, project_folder, capsys):
    settings_path = project_folder / ".claude" / "settings.json"
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    payload["env"]["KG_COLLECTION"] = "Wrong"
    settings_path.write_text(json.dumps(payload), encoding="utf-8")

    code = verify.cmd_verify_env_projection(_args(json_mode=True))
    assert code == verify.EXIT_DRIFT
    out = capsys.readouterr().out
    obj = json.loads(out.strip())
    assert obj["overall"] == "drift"
    drift = obj["drift"]
    assert any(d["key"] == "KG_COLLECTION" for d in drift)
    target = next(d for d in drift if d["key"] == "KG_COLLECTION")
    assert target["surface"] == ".claude/settings.json"
    assert target["expected"] == CANONICAL_BUNDLE["KG_COLLECTION"]
    assert target["actual"] == "Wrong"


# ---------------------------------------------------------------------------
# Tests — --all
# ---------------------------------------------------------------------------


def test_all_aggregates_worst_exit(tmp_path, monkeypatch, capsys):
    # Two projects: A canonical, B with a mutation.
    folder_a = tmp_path / "a"; folder_a.mkdir()
    folder_b = tmp_path / "b"; folder_b.mkdir()
    _write_canonical_surfaces(folder_a, CANONICAL_BUNDLE)
    _write_canonical_surfaces(folder_b, CANONICAL_BUNDLE)
    # Mutate B's settings.
    settings_b = folder_b / ".claude" / "settings.json"
    payload = json.loads(settings_b.read_text(encoding="utf-8"))
    payload["env"]["PROJECT_NAME"] = "Tampered"
    settings_b.write_text(json.dumps(payload), encoding="utf-8")

    def _from_db(pid: str) -> dict[str, str]:
        return dict(CANONICAL_BUNDLE)

    def _resolve_folder(pid: str) -> Path:
        return {"a": folder_a, "b": folder_b}[pid]

    def _list():
        return [
            {"id": "a", "slug": "a", "folder": str(folder_a)},
            {"id": "b", "slug": "b", "folder": str(folder_b)},
        ]

    monkeypatch.setattr(verify, "_project_env_from_db", _from_db)
    monkeypatch.setattr(verify, "_resolve_project_folder", _resolve_folder)
    monkeypatch.setattr(verify, "_list_registered_projects", _list)

    code = verify.cmd_verify_env_projection(_args(project_id=None, all_=True))
    assert code == verify.EXIT_DRIFT
    out = capsys.readouterr().out
    assert "Tampered" in out or "DRIFT" in out


def test_all_db_unreadable(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("hub down")

    monkeypatch.setattr(verify, "_list_registered_projects", _boom)
    code = verify.cmd_verify_env_projection(_args(project_id=None, all_=True))
    assert code == verify.EXIT_TOOL_MISSING
    err = capsys.readouterr().err
    assert "hub down" in err or "cannot list projects" in err
