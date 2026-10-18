# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 L5 — no machine-specific value in the committable settings.json.

``.claude/settings.json`` is committable again (users commit it; Claude Code
reads the committed copy). VCO used to write this machine's absolute paths
(home dir, username), its launcher-DB ids and its service URLs/ports into the
file's ``env`` — published by the first ``git push``. Those keys
(:data:`vco_lib.config_projection.MACHINE_LOCAL_ENV_KEYS`) now go to
``.claude/settings.local.json`` ``env`` — Claude Code's per-machine file,
merged over ``settings.json`` key by key — which VCO keeps out of git.

Pinned here:

* inventory — after an install (launcher-less bundle AND the DB-backed
  projection) no value in ``settings.json`` contains the temp "home" path,
  the project folder, the orchestrator root, a service URL or a launcher id;
  every one of them is in ``settings.local.json``;
* migration — an old ``settings.json`` carrying VCO's machine keys beside a
  user key: VCO's keys move, the user's key and every other setting stay,
  ``settings.local.json``'s own user content stays, a second run is a byte
  no-op; a refused ``settings.local.json`` leaves ``settings.json`` carrying
  the full set (routing never breaks);
* the drift check compares each file over its own keys;
* consumers that read the env from disk resolve a key carried ONLY by
  ``settings.local.json`` (the merged reader);
* git: ``.claude/settings.local.json`` is excluded on every bundle run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib import config_projection as cp
from vco_lib import git_exclude, project_init
from vco_lib.claude_settings_env import read_settings_env

LOCAL_REL = Path(".claude") / "settings.local.json"
SETTINGS_REL = Path(".claude") / "settings.json"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _env(path: Path) -> dict:
    return dict(_json(path).get("env") or {})


def _home_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A fake ``/home/<user>`` with a project and an orchestrator clone."""
    home = tmp_path / "home" / "l5-private-username"
    project = home / "code" / "MyProj"
    orch = home / "vco"
    project.mkdir(parents=True)
    (orch / "templates" / "hooks").mkdir(parents=True)
    (orch / "templates" / "scripts").mkdir(parents=True)
    (orch / "templates" / "agents" / "free").mkdir(parents=True)
    (orch / "vct-module.json").write_text("{}\n", encoding="utf-8")
    return home, project, orch


def _assert_nothing_machine_in(env: dict, *needles: str) -> None:
    for key, value in env.items():
        assert key not in cp.MACHINE_LOCAL_ENV_KEYS, f"machine key {key} in settings.json"
        for needle in needles:
            assert needle not in str(value), f"{key}={value!r} leaks {needle!r}"


# ─── inventory ──────────────────────────────────────────────────────────


def test_launcher_less_install_keeps_machine_values_out_of_settings_json(tmp_path):
    home, project, orch = _home_tree(tmp_path)
    project_init.install_project_bundle(project, orchestrator_root=orch, write_env=True)

    settings_env = _env(project / SETTINGS_REL)
    assert settings_env.get("KG_COLLECTION"), "the portable keys still land in settings.json"
    _assert_nothing_machine_in(settings_env, str(home), "l5-private-username", "localhost", "127.0.0.1")

    local_env = _env(project / LOCAL_REL)
    assert Path(local_env["VCT_ORCHESTRATOR_ROOT"]) == orch.resolve()
    assert local_env["WEAVIATE_URL"].startswith("http")
    assert set(local_env) <= cp.MACHINE_LOCAL_ENV_KEYS


def test_db_backed_projection_keeps_machine_values_out_of_settings_json(tmp_path):
    home, project, _orch = _home_tree(tmp_path)
    db = tmp_path / "launcher.db"
    make_launcher_db(
        db,
        projects=[
            {"project_id": "l5-machine-uuid", "name": "MyProj",
             "folder_path": str(project), "slug": "myproj"},
            {"project_id": "peer-uuid", "name": "SecretPeerProject",
             "folder_path": str(home / "code" / "peer"), "slug": "peer"},
        ],
        # A peer on THIS machine granted read access: its name must not
        # reach the committable file either.
        codegraph_access=[("peer-uuid", "l5-machine-uuid", "read")],
    )
    bundle = cp.project_env_from_db("l5-machine-uuid", db_path=db, orchestrator_root=_orch)
    report = cp.apply_project_env(bundle)

    assert cp._SURFACE_CLAUDE_SETTINGS_LOCAL in report
    settings_env = _env(project / SETTINGS_REL)
    _assert_nothing_machine_in(
        settings_env, str(home), "l5-machine-uuid", "http://", ":8081", "SecretPeerProject",
    )
    local_env = _env(project / LOCAL_REL)
    assert local_env["VCT_PROJECT_ID"] == "l5-machine-uuid"
    assert "SecretPeerProject" in local_env.get("VCT_CODE_GRAPH_ACCESS_LIST", "")
    assert local_env["WEAVIATE_URL"] == bundle["canonical_env"]["WEAVIATE_URL"]
    # The two files together are exactly the bundle (nothing lost, nothing added).
    merged = {**settings_env, **local_env}
    assert merged == dict(bundle["canonical_env"])
    # .claude/env (git-excluded) still carries every key.
    claude_env = (project / ".claude" / "env").read_text(encoding="utf-8")
    assert 'export VCT_PROJECT_ID="l5-machine-uuid"' in claude_env


# ─── migration ──────────────────────────────────────────────────────────


def _bundle(project: Path) -> cp.ProjectEnvBundle:
    return {
        "project_id": "mig-uuid",
        "project_root": project,
        "canonical_env": {
            "KG_COLLECTION": "Fake_KnowledgeGraph",
            "PROJECT_NAME": "Fake",
            "KG_BASE_DIR": str(project),
            "VCT_PROJECT_ID": "mig-uuid",
            "WEAVIATE_URL": "http://localhost:8081",
            "VCT_ORCHESTRATOR_ROOT": "C:\\Users\\someone\\vco",
        },
    }


def _old_layout(project: Path) -> None:
    """What a pre-L5 VCO left: every canonical key in settings.json, beside a
    user key and the user's hooks/permissions; a settings.local.json the user
    (or Claude Code) already has."""
    claude = project / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)"]},
        "env": {
            "USER_OWN": "keep-me",
            "KG_COLLECTION": "Fake_KnowledgeGraph",
            "PROJECT_NAME": "Fake",
            "KG_BASE_DIR": "/home/olduser/old/path",
            "VCT_PROJECT_ID": "mig-uuid",
            "WEAVIATE_URL": "http://localhost:8081",
            "VCT_ORCHESTRATOR_ROOT": "C:\\Users\\someone\\vco",
        },
    }, indent=2), encoding="utf-8")
    (claude / "settings.local.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(make)"]},
        "env": {"MY_LOCAL_TOKENLESS": "mine"},
    }, indent=2), encoding="utf-8")


def test_migration_moves_only_vcos_keys_and_keeps_user_content(tmp_path):
    project = tmp_path / "proj"
    _old_layout(project)
    cp.apply_project_env(_bundle(project), surfaces=["claude_settings_json"])

    settings = _json(project / SETTINGS_REL)
    assert settings["permissions"] == {"allow": ["Bash(ls)"]}
    assert settings["env"] == {
        "USER_OWN": "keep-me",
        "KG_COLLECTION": "Fake_KnowledgeGraph",
        "PROJECT_NAME": "Fake",
    }
    local = _json(project / LOCAL_REL)
    assert local["permissions"] == {"allow": ["Bash(make)"]}
    assert local["env"] == {
        "MY_LOCAL_TOKENLESS": "mine",
        "KG_BASE_DIR": str(project),
        "VCT_PROJECT_ID": "mig-uuid",
        "WEAVIATE_URL": "http://localhost:8081",
        "VCT_ORCHESTRATOR_ROOT": "C:\\Users\\someone\\vco",  # a Windows path, verbatim
    }


def test_migration_is_idempotent(tmp_path):
    project = tmp_path / "proj"
    _old_layout(project)
    cp.apply_project_env(_bundle(project))
    first = {p: (project / p).read_bytes() for p in (SETTINGS_REL, LOCAL_REL)}
    cp.apply_project_env(_bundle(project))
    assert {p: (project / p).read_bytes() for p in first} == first


def test_a_removed_machine_key_leaves_the_local_file(tmp_path):
    project = tmp_path / "proj"
    _old_layout(project)
    cp.apply_project_env(_bundle(project))
    bundle = _bundle(project)
    del bundle["canonical_env"]["VCT_ORCHESTRATOR_ROOT"]
    cp.apply_project_env(bundle)
    assert "VCT_ORCHESTRATOR_ROOT" not in _env(project / LOCAL_REL)
    assert "VCT_ORCHESTRATOR_ROOT" not in _env(project / SETTINGS_REL)
    assert _env(project / LOCAL_REL)["MY_LOCAL_TOKENLESS"] == "mine"


def test_a_refused_local_file_keeps_settings_json_routing_whole(tmp_path):
    """Leave-alone arm: when settings.local.json cannot be edited, VCO must
    not strip the machine keys out of settings.json (they would then be in
    neither file) — it writes the full set there as before, and records the
    refusal."""
    project = tmp_path / "proj"
    _old_layout(project)
    broken = b"{ this is not json"
    (project / LOCAL_REL).write_bytes(broken)
    with pytest.raises(cp.SettingsWriteRefused):
        cp.apply_project_env(_bundle(project))
    assert (project / LOCAL_REL).read_bytes() == broken
    env = _env(project / SETTINGS_REL)
    assert env["KG_BASE_DIR"] == str(project)
    assert env["VCT_PROJECT_ID"] == "mig-uuid"
    assert env["USER_OWN"] == "keep-me"


def test_jsonc_local_file_is_edited_in_place(tmp_path):
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / LOCAL_REL).write_text(
        '{\n  // my machine\n  "env": {"MINE": "1",},\n}\n', encoding="utf-8",
    )
    cp.apply_project_env(_bundle(project))
    text = (project / LOCAL_REL).read_text(encoding="utf-8")
    assert "// my machine" in text
    env = read_settings_env(project)
    assert env["MINE"] == "1" and env["VCT_PROJECT_ID"] == "mig-uuid"


def test_legacy_vscode_block_loses_vcos_machine_values_only_on_evidence(tmp_path):
    project = tmp_path / "proj"
    _old_layout(project)
    vscode = project / ".vscode" / "settings.json"
    vscode.parent.mkdir()
    vscode.write_text(json.dumps({
        "editor.tabSize": 2,
        "claude-code.env": {
            "KG_BASE_DIR": str(project),           # VCO's value → removed
            "WEAVIATE_URL": "http://other:1234",   # not VCO's → kept
        },
    }), encoding="utf-8")
    cp.apply_project_env(_bundle(project))
    data = _json(vscode)
    assert data["editor.tabSize"] == 2
    assert data["claude-code.env"] == {"WEAVIATE_URL": "http://other:1234"}


# ─── drift check ────────────────────────────────────────────────────────


def test_drift_check_compares_each_file_over_its_own_keys(tmp_path):
    from vco_lib.env_projection_check import check_env_surfaces

    project = tmp_path / "proj"
    _old_layout(project)
    bundle = _bundle(project)
    before = check_env_surfaces(project, bundle["canonical_env"])
    drift_keys = {(r["surface"], r["key"]) for r in before.drift}
    assert (".claude/settings.json", "VCT_PROJECT_ID") in drift_keys, (
        "a machine key left in settings.json is drift"
    )
    cp.apply_project_env(bundle)
    after = check_env_surfaces(project, bundle["canonical_env"])
    assert after.verdict is True, after.drift


# ─── consumers ──────────────────────────────────────────────────────────


def _only_local(project: Path, env: dict) -> None:
    claude = project / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "settings.json").write_text(json.dumps({"env": {"KG_COLLECTION": "P_KnowledgeGraph"}}), encoding="utf-8")
    (claude / "settings.local.json").write_text(json.dumps({"env": env}), encoding="utf-8")


def test_merged_reader_local_wins_per_key(tmp_path):
    _only_local(tmp_path, {"WEAVIATE_URL": "http://localhost:8081", "KG_COLLECTION": "Local_KG"})
    env = read_settings_env(tmp_path)
    assert env == {"KG_COLLECTION": "Local_KG", "WEAVIATE_URL": "http://localhost:8081"}


def test_disk_env_consumers_see_a_key_only_the_local_file_carries(tmp_path):
    from vco_lib import install_weaviate, kg_embed_model, knowledge_residue

    _only_local(tmp_path, {"WEAVIATE_URL": "http://localhost:8081", "SHARED_KG_READ_DISABLED": "true"})
    assert knowledge_residue.project_env_value(tmp_path, "WEAVIATE_URL") == "http://localhost:8081"
    assert knowledge_residue.shared_read_disabled_for(tmp_path) is True
    assert kg_embed_model._project_env(tmp_path)["WEAVIATE_URL"] == "http://localhost:8081"
    stale = install_weaviate.resolve_settings_weaviate_env(tmp_path)
    assert stale == {"WEAVIATE_URL": "http://localhost:8081", "KG_COLLECTION": "P_KnowledgeGraph"}


def test_service_continuity_reads_the_local_endpoint(tmp_path):
    from vco_lib import service_reconcile

    _only_local(tmp_path, {"WEAVIATE_URL": "http://localhost:8099"})
    got = service_reconcile._continuity(tmp_path)
    assert "weaviate" in got and "8099" in repr(got["weaviate"])


def test_deferral_retry_reads_shared_collection_from_the_merged_layer(tmp_path):
    from vco_lib import deferral_retry

    _only_local(tmp_path, {"SHARED_KG_COLLECTION": "Shared_KG"})
    assert deferral_retry._shared_collection_for(tmp_path, env={}) == "Shared_KG"


# ─── git ────────────────────────────────────────────────────────────────


def test_settings_local_json_is_excluded_on_every_bundle_run():
    assert git_exclude.exclude_pattern_for(".claude/settings.local.json") == "/.claude/settings.local.json"
    entries = git_exclude.safe_add_exclude_entries(
        {"actions": {"noop": [".claude/hooks/a.sh"]}}, Path("/nonexistent-l5"),
    )
    assert "/.claude/settings.local.json" in entries
    # settings.json itself stays committable.
    assert git_exclude.exclude_pattern_for(".claude/settings.json") is None
