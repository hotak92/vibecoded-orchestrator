# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``vco verify-env-projection`` (Phase 0.C acceptance).

Stubbed Phase 0.B APIs (post-merge integration checklist for the
reviewer):

* ``vco_lib.config_projection.project_env_from_db(project_id: str) -> dict[str, str]``
  — returns the canonical env bundle for the project. Raises
  ``LookupError`` when the project is not registered in the launcher DB.
* ``vco_lib.config_projection.apply_project_env(bundle, *, project_folder: Path) -> ApplyResult``
  — writes the bundle to the surfaces ``apply`` writes by default
  (``.claude/settings.json`` ``env`` and ``.claude/env``; v0.2.97 — the
  VS Code surface is neither written nor compared).
  Return must expose ``.ok`` and ``.message`` (dict or attr-style).
* ``vco_lib.config_projection.resolve_project_folder(project_id) -> Path``
  — maps a slug/rowid to its on-disk folder root.
* ``vco_lib.config_projection.list_registered_projects() -> Iterable[Mapping[str, str]]``
  — for ``--all``; yields ``{"id": ..., "slug": ..., "folder": ...}``.

Coverage:
* All-match → exit 0.
* Mutation to one of the applied surfaces → drift detected → exit 1;
  ``.vscode/settings.json`` is not compared (v0.2.97).
* JSONC settings read correctly; an unreadable surface → exit 2
  (``cannot_verify``), never ok / drift (v0.2.97).
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
from typing import Mapping

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
    """Lay down the surfaces ``config_projection.apply_project_env`` writes by
    default, in canonical state. ``.claude/env`` is rendered by the writer's
    OWN block builder (a user export sits outside it, as in the field).

    Also leaves a ``.vscode/settings.json`` behind — a surface ``apply`` does
    not write unless asked, holding a value from long ago — so every test
    here also proves the verifier does not compare it.
    """
    from vco_lib.config_projection import _build_managed_block

    claude_dir = folder / ".claude"
    vscode_dir = folder / ".vscode"
    claude_dir.mkdir(parents=True, exist_ok=True)
    vscode_dir.mkdir(parents=True, exist_ok=True)

    # .claude/settings.json — top-level "env" mapping.
    settings = {"env": dict(bundle)}
    (claude_dir / "settings.json").write_text(
        json.dumps(settings, indent=2, sort_keys=True), encoding="utf-8"
    )

    # .claude/env — the managed block, plus a user line outside it.
    (claude_dir / "env").write_text(
        'export MY_OWN="kept"\n' + _build_managed_block(dict(bundle)), encoding="utf-8"
    )

    # .vscode/settings.json — NOT an applied surface; deliberately stale.
    vscode_settings = {"claude-code.env": {"KG_COLLECTION": "Acme_KnowledgeGraph"}}
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
        'export PROJECT_NAME="MyProject"', 'export PROJECT_NAME="Tampered"'
    )
    assert "Tampered" in content
    env_path.write_text(content, encoding="utf-8")

    code = verify.cmd_verify_env_projection(_args())
    assert code == verify.EXIT_DRIFT
    out = capsys.readouterr().out
    assert ".claude/env" in out
    assert "PROJECT_NAME" in out
    assert "Tampered" in out


def test_vscode_settings_is_not_compared(stub_db, project_folder, capsys):
    """LEAVE-ALONE (v0.2.97): ``apply`` does not write ``.vscode/settings.json``
    unless a caller asks, so its content — stale, drifted or absent — is not
    evidence about the projection. RED before: every project that had never
    opted into that surface reported DRIFT forever."""
    vscode_path = project_folder / ".vscode" / "settings.json"
    payload = json.loads(vscode_path.read_text(encoding="utf-8"))
    payload["claude-code.env"]["DEVELOPMENT_COLLECTION"] = "Drifted_Development"
    vscode_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    assert verify.cmd_verify_env_projection(_args()) == verify.EXIT_OK
    vscode_path.unlink()
    assert verify.cmd_verify_env_projection(_args()) == verify.EXIT_OK
    out = capsys.readouterr().out
    assert ".vscode" not in out


def _as_jsonc(path: Path) -> None:
    text = path.read_text(encoding="utf-8").rstrip()
    path.write_text("// my note\n" + text[:-1].rstrip() + ",\n}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        json.loads(path.read_text(encoding="utf-8"))


def test_jsonc_settings_file_verifies_clean(stub_db, project_folder):
    """JSONC (v0.2.97). RED before: the strict reader read a commented
    settings.json as holding nothing and reported every key as drift."""
    _as_jsonc(project_folder / ".claude" / "settings.json")
    assert verify.cmd_verify_env_projection(_args()) == verify.EXIT_OK


def test_jsonc_settings_drift_names_the_real_value(stub_db, project_folder, capsys):
    """ACT on JSONC: a real mismatch is reported with the value on disk,
    not as ``<missing>``."""
    settings_path = project_folder / ".claude" / "settings.json"
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    payload["env"]["KG_COLLECTION"] = "Wrong"
    settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _as_jsonc(settings_path)
    assert verify.cmd_verify_env_projection(_args(json_mode=True)) == verify.EXIT_DRIFT
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["drift"] == [{"surface": ".claude/settings.json", "key": "KG_COLLECTION",
                             "expected": CANONICAL_BUNDLE["KG_COLLECTION"], "actual": "Wrong"}]


def test_a_canonical_key_the_projection_omits_is_drift(stub_db, project_folder, capsys):
    """``apply`` DELETES a canonical key its bundle omits, so one left in a
    surface is drift (``expected`` = ``<absent>``). A non-canonical key is
    the user's and is never reported."""
    settings_path = project_folder / ".claude" / "settings.json"
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    payload["env"]["VCT_ORCHESTRATOR_ROOT"] = "/somewhere/old"
    payload["env"]["MY_OWN_KEY"] = "mine"
    settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    assert verify.cmd_verify_env_projection(_args(json_mode=True)) == verify.EXIT_DRIFT
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["drift"] == [{"surface": ".claude/settings.json", "key": "VCT_ORCHESTRATOR_ROOT",
                             "expected": "<absent>", "actual": "/somewhere/old"}]


@pytest.mark.parametrize("rel,raw", [
    (".claude/settings.json", b"{ not jsonc at all"),
    (".claude/settings.json", b"[1, 2]\n"),
    (".claude/settings.json", b"\xff\xfe{\"env\": {}}"),
    (".claude/env", b"export KG_COLLECTION=\"\xff\xfe\"\n"),
])
def test_an_unreadable_surface_is_cannot_verify(stub_db, project_folder, monkeypatch,
                                                capsys, rel, raw):
    """UNREADABLE (v0.2.97): never ``ok``, never ``drift`` — exit 2 with the
    file and the reason, and ``--fix`` does not run on it. RED before: a
    broken settings.json read as empty and was reported as full DRIFT."""
    (project_folder / rel).write_bytes(raw)
    apply_calls = _stub_apply(monkeypatch, project_folder=project_folder)
    for fix in (False, True):
        code = verify.cmd_verify_env_projection(_args(json_mode=True, fix=fix))
        assert code == verify.EXIT_TOOL_MISSING
        obj = json.loads(capsys.readouterr().out.strip())
        assert obj["overall"] == "cannot_verify"
        assert [u["surface"] for u in obj["unreadable"]] == [rel]
        assert "drift" not in obj
    assert apply_calls["n"] == 0
    verify.cmd_verify_env_projection(_args())
    assert "CANNOT VERIFY" in capsys.readouterr().err


def test_fix_writes_exactly_the_surfaces_it_verifies(tmp_path, monkeypatch):
    """The REAL ``apply`` behind ``--fix``: the default surfaces are
    repaired, and no ``.vscode/settings.json`` is created. RED before: --fix
    forced the VS Code surface into every project it touched."""
    folder = tmp_path / "fresh"
    (folder / ".claude").mkdir(parents=True)
    (folder / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {}, "env": {"KG_COLLECTION": "Drifted"}}), encoding="utf-8")
    monkeypatch.setattr(verify, "_project_env_from_db", lambda _pid: dict(CANONICAL_BUNDLE))
    monkeypatch.setattr(verify, "_resolve_project_folder", lambda _pid: folder)
    assert verify.cmd_verify_env_projection(_args("p")) == verify.EXIT_DRIFT
    assert verify.cmd_verify_env_projection(_args("p", fix=True)) == verify.EXIT_OK
    assert not (folder / ".vscode").exists()
    assert verify.cmd_verify_env_projection(_args("p")) == verify.EXIT_OK
    assert json.loads((folder / ".claude" / "settings.json").read_text())["hooks"] == {}


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
    folder_a = tmp_path / "a"
    folder_a.mkdir()
    folder_b = tmp_path / "b"
    folder_b.mkdir()
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
