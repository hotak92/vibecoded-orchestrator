# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 L5, review R14B fix round (B1, B2, B4, B5, B6, B7).

* B1 — a git-TRACKED ``.claude/settings.local.json`` gets no machine value
  (pre-L5 layout kept, ``settings_local_json_tracked`` recorded and cleared
  once untracked); an untracked one is written with no entry; git that cannot
  answer writes nothing and records nothing.
* B6 — a value the user put in ``settings.local.json`` is kept (and listed);
  a value VCO wrote before is VCO's and is replaced; the drift check agrees.
* B5 — unregister clears every machine-local key from the new file even when
  the launcher passes only its (shorter) Rust list.
* B2 — the root install moves its service URLs and GRPC_PORT out of the root
  settings.json; a value the user changed stays.
* B4 — the launcher-less path writes KG_BASE_DIR.
* B7 — the continuity statement names the file its value came from.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib import config_projection as cp
from vco_lib import settings_local_env as sle
from vco_lib.deferral_report import DeferralReport

LOCAL = Path(".claude") / "settings.local.json"
SETTINGS = Path(".claude") / "settings.json"


def _env(path: Path) -> dict:
    return dict(json.loads(path.read_text(encoding="utf-8")).get("env") or {})


def _bundle(project: Path, **over: str) -> cp.ProjectEnvBundle:
    env = {
        "KG_COLLECTION": "Fake_KnowledgeGraph",
        "KG_BASE_DIR": str(project),
        "VCT_PROJECT_ID": "r14b-uuid",
        "WEAVIATE_URL": "http://localhost:8081",
    }
    env.update(over)
    return {"project_id": "r14b-uuid", "project_root": project, "canonical_env": env}


def _git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, env=env,
    )


def _has(project: Path, cid: str) -> bool:
    return DeferralReport.read(project).has_condition(cid)


# ─── B1 ────────────────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    _git(project, "init", "-q")
    return project


def test_tracked_local_file_gets_no_machine_value(repo: Path):
    original = b'{\n  "permissions": {"allow": ["Bash(make)"]}\n}\n'
    (repo / LOCAL).write_bytes(original)
    _git(repo, "add", "-f", str(LOCAL))  # -f: a global ignore may list it
    _git(repo, "commit", "-q", "-m", "team settings")

    cp.apply_project_env(_bundle(repo))

    assert (repo / LOCAL).read_bytes() == original, "a tracked file must not get machine values"
    settings_env = _env(repo / SETTINGS)
    assert settings_env["VCT_PROJECT_ID"] == "r14b-uuid", "pre-L5 layout keeps routing working"
    assert settings_env["KG_BASE_DIR"] == str(repo)
    assert _has(repo, sle.CID_TRACKED)
    assert sle.tracked_still(repo) is True
    from vco_lib.env_projection_check import check_env_surfaces
    assert check_env_surfaces(repo, _bundle(repo)["canonical_env"]).verdict is True

    # The remedy: untrack it; the next apply moves the keys and clears the entry.
    _git(repo, "rm", "-q", "--cached", str(LOCAL))
    assert sle.tracked_still(repo) is False
    cp.apply_project_env(_bundle(repo))
    assert _env(repo / LOCAL)["VCT_PROJECT_ID"] == "r14b-uuid"
    assert "VCT_PROJECT_ID" not in _env(repo / SETTINGS)
    assert not _has(repo, sle.CID_TRACKED)


def test_untracked_local_file_in_a_repo_is_written_without_an_entry(repo: Path):
    cp.apply_project_env(_bundle(repo))
    assert _env(repo / LOCAL)["VCT_PROJECT_ID"] == "r14b-uuid"
    assert not _has(repo, sle.CID_TRACKED)


def test_git_that_cannot_answer_writes_nothing_and_records_nothing(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(sle, "layer_state", lambda _folder: sle.UNKNOWN)
    cp.apply_project_env(_bundle(project))
    assert not (project / LOCAL).exists()
    assert _env(project / SETTINGS)["VCT_PROJECT_ID"] == "r14b-uuid"
    assert not _has(project, sle.CID_TRACKED)


def test_the_tracked_condition_is_declared_with_its_probe():
    from vco_lib import deferral_probes

    text = (Path(cp.__file__).parent / "deferral_conditions.toml").read_text(encoding="utf-8")
    assert f"[conditions.{sle.CID_TRACKED}]" in text
    assert f"[conditions.{sle.CID_USER_KEPT}]" in text
    assert "settings_local_json_still_tracked" in deferral_probes.PROBES
    assert "settings_local_user_values_still_kept" in deferral_probes.PROBES


# ─── B6 ────────────────────────────────────────────────────────────────


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    return project


def test_a_users_own_value_is_kept_and_listed(tmp_path):
    project = _project(tmp_path)
    (project / LOCAL).write_text(json.dumps({"env": {"WEAVIATE_URL": "http://mine:1"}}), encoding="utf-8")
    cp.apply_project_env(_bundle(project))
    local = _env(project / LOCAL)
    assert local["WEAVIATE_URL"] == "http://mine:1", "the user's override must survive"
    assert local["VCT_PROJECT_ID"] == "r14b-uuid"
    assert _has(project, sle.CID_USER_KEPT)
    assert sle.user_kept_still(project) is True
    from vco_lib.env_projection_check import check_env_surfaces
    assert check_env_surfaces(project, _bundle(project)["canonical_env"]).verdict is True
    before = (project / LOCAL).read_bytes()
    cp.apply_project_env(_bundle(project))
    assert (project / LOCAL).read_bytes() == before

    # The user hands the key back: VCO writes its value and the entry clears.
    data = json.loads((project / LOCAL).read_text(encoding="utf-8"))
    del data["env"]["WEAVIATE_URL"]
    (project / LOCAL).write_text(json.dumps(data), encoding="utf-8")
    cp.apply_project_env(_bundle(project))
    assert _env(project / LOCAL)["WEAVIATE_URL"] == "http://localhost:8081"
    assert not _has(project, sle.CID_USER_KEPT)
    assert sle.user_kept_still(project) is False


def test_a_value_vco_wrote_before_is_replaced_even_when_claude_env_is_stale(tmp_path):
    """Red-proof for the written-values record: the settings layer alone is
    re-applied (``.claude/env`` keeps the old value), and VCO's own earlier
    value must still be recognised as VCO's."""
    project = _project(tmp_path)
    cp.apply_project_env(_bundle(project, WEAVIATE_URL="http://localhost:1111"),
                         surfaces=["claude_settings_json"])
    cp.apply_project_env(_bundle(project, WEAVIATE_URL="http://localhost:2222"),
                         surfaces=["claude_settings_json"])
    cp.apply_project_env(_bundle(project, WEAVIATE_URL="http://localhost:3333"),
                         surfaces=["claude_settings_json"])
    assert _env(project / LOCAL)["WEAVIATE_URL"] == "http://localhost:3333"
    assert not _has(project, sle.CID_USER_KEPT)


def test_a_pre_l5_value_equal_to_settings_json_is_vcos(tmp_path):
    project = _project(tmp_path)
    (project / SETTINGS).write_text(json.dumps({"env": {"WEAVIATE_URL": "http://old:8081"}}), encoding="utf-8")
    (project / LOCAL).write_text(json.dumps({"env": {"WEAVIATE_URL": "http://old:8081"}}), encoding="utf-8")
    cp.apply_project_env(_bundle(project))
    assert _env(project / LOCAL)["WEAVIATE_URL"] == "http://localhost:8081"
    assert not _has(project, sle.CID_USER_KEPT)


# ─── B5 ────────────────────────────────────────────────────────────────


def _rust_keys() -> list[str]:
    from tests.test_canonical_env_key_parity_v0273 import _extract_rust_canonical_keys

    return [k for k in _extract_rust_canonical_keys() if k != "GITHUB_TOKEN"]


def test_unregister_clears_every_machine_key_from_the_local_file(tmp_path):
    from vco_lib import unregister_env

    project = tmp_path / "proj"
    project.mkdir()
    db = make_launcher_db(tmp_path / "launcher.db", projects=[{
        "project_id": "unreg-uuid", "name": "Fake", "folder_path": str(project), "slug": "fake",
    }])
    bundle = cp.project_env_from_db("unreg-uuid", db_path=db)
    cp.apply_project_env(bundle)
    assert "VCT_PROJECT_ID" in _env(project / LOCAL)
    assert "VCT_PROJECT_ID" not in _rust_keys(), "the case this test exists for"

    unregister_env.strip_routing_keys(project, _rust_keys(), project_id="unreg-uuid", db_path=db)
    left = set(_env(project / LOCAL)) & cp.MACHINE_LOCAL_ENV_KEYS
    assert not left, f"machine keys left after unregister: {sorted(left)}"


def test_unregister_strip_considers_the_whole_python_table():
    from vco_lib import unregister_env

    assert cp.MACHINE_LOCAL_ENV_KEYS <= unregister_env.routing_keys()
    assert set(_rust_keys()) <= unregister_env.routing_keys()


# ─── B2 ────────────────────────────────────────────────────────────────


@pytest.fixture
def install_py(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parent.parent / "install.py"
    spec = importlib.util.spec_from_file_location("install_py_l5_r14b", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_py_l5_r14b"] = mod
    spec.loader.exec_module(mod)
    root = tmp_path / "root"
    (root / ".claude").mkdir(parents=True)
    monkeypatch.setattr(mod, "PROJECT_ROOT", root)
    return mod, root


EMBED = {"text_model": "qwen3-embedding:0.6b", "active_embedding": "qwen3", "code_backend": "ollama"}


def _route(mod, root: Path) -> list[str]:
    """What install.py's main() calls after the settings step (both modes)."""
    from vco_lib.self_install import route_root_machine_env

    return route_root_machine_env(root, lambda: mod._build_vco_settings_defaults(EMBED)["env"])


def test_root_install_moves_its_machine_env_to_settings_local(install_py):
    mod, root = install_py
    defaults = mod._build_vco_settings_defaults(EMBED)
    (root / SETTINGS).write_text(json.dumps(defaults, indent=2), encoding="utf-8")

    moved = _route(mod, root)

    assert set(moved) == {"WEAVIATE_URL", "OLLAMA_URL", "CODE_EMBED_SERVICE_URL", "GRPC_PORT"}
    settings = json.loads((root / SETTINGS).read_text(encoding="utf-8"))
    assert not set(settings["env"]) & set(moved)
    assert settings["env"]["KG_COLLECTION"] == defaults["env"]["KG_COLLECTION"]
    assert settings["permissions"] == defaults["permissions"], "the rest of the file is untouched"
    local = _env(root / LOCAL)
    assert local["GRPC_PORT"] == defaults["env"]["GRPC_PORT"]
    assert _route(mod, root) == [], "idempotent"


def test_root_install_leaves_a_value_the_user_changed(install_py):
    mod, root = install_py
    defaults = mod._build_vco_settings_defaults(EMBED)
    defaults["env"]["WEAVIATE_URL"] = "http://my-own:9999"
    (root / SETTINGS).write_text(json.dumps(defaults, indent=2), encoding="utf-8")
    moved = _route(mod, root)
    assert "WEAVIATE_URL" not in moved
    assert json.loads((root / SETTINGS).read_text())["env"]["WEAVIATE_URL"] == "http://my-own:9999"


# ─── B4 / B7 ───────────────────────────────────────────────────────────


def test_launcher_less_install_writes_kg_base_dir(tmp_path):
    from vco_lib.project_init import _apply_standalone_env

    folder = tmp_path / "standalone"
    (folder / ".claude").mkdir(parents=True)
    result = _apply_standalone_env(folder, orchestrator_root=None, project_name="Fake")
    assert result["action"] == "applied", result
    assert _env(folder / LOCAL)["KG_BASE_DIR"] == str(folder.resolve())
    assert "KG_BASE_DIR" not in _env(folder / SETTINGS)


def test_continuity_names_the_file_the_value_came_from(tmp_path):
    from vco_lib import service_reconcile

    (tmp_path / ".claude").mkdir()
    (tmp_path / SETTINGS).write_text(json.dumps({"env": {"WEAVIATE_URL": "http://localhost:8099"}}), encoding="utf-8")
    (tmp_path / LOCAL).write_text(json.dumps({"env": {"OLLAMA_URL": "http://localhost:11999"}}), encoding="utf-8")
    got = service_reconcile._continuity(tmp_path)
    assert "settings.json" in repr(got["weaviate"]) and "settings.local.json" not in repr(got["weaviate"])
    assert "settings.local.json" in repr(got["ollama"])
