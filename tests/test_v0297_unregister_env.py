# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 review R6: the unregister removes VCO's routing keys from
``.claude/env`` and the two JSON env blocks ONLY where the value is VCO's.

``vco_lib.unregister_env`` compares each value with what
``config_projection.project_env_from_db`` projects for the project (computed
before the row is deleted). VCO's ``.claude/env`` block goes whole (its
markers prove it); outside it, and in the JSON blocks, a different value is
the user's — left and reported by name. A ``# KEY=`` comment stays.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib.config_projection import apply_project_env, project_env_from_db
from vco_lib.unregister_env import projected_env, strip_routing_keys

KEYS = ["KG_COLLECTION", "PROJECT_NAME", "DEVELOPMENT_COLLECTION", "WEAVIATE_URL"]
ALL_SURFACES = ("claude_settings_json", "claude_env", "vscode_settings_json")


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path]:
    """A registered project whose three surfaces VCO has projected."""
    folder = tmp_path / "acme"
    folder.mkdir()
    db = tmp_path / "launcher.db"
    make_launcher_db(db, projects=[{
        "project_id": "p-1", "name": "Acme", "folder_path": str(folder), "slug": "acme",
    }])
    apply_project_env(project_env_from_db("p-1", db_path=db), surfaces=ALL_SURFACES)
    return folder, db


def _env_block(folder: Path, rel: str, env_key: str) -> dict:
    return json.loads((folder / rel).read_text(encoding="utf-8")).get(env_key, {})


def test_vcos_values_go_on_every_surface(project: tuple[Path, Path]) -> None:
    """Act: the projected values are VCO's — removed from all three; the
    `.claude/env` block goes whole, markers included."""
    folder, db = project
    expected = projected_env("p-1", db_path=db)
    assert _env_block(folder, ".claude/settings.json", "env")["KG_COLLECTION"] == expected["KG_COLLECTION"]

    result = strip_routing_keys(folder, KEYS, project_id="p-1", db_path=db)

    assert result["projection"] == "resolved" and result["errors"] == []
    assert "KG_COLLECTION" in result["removed"][".claude/settings.json"]
    assert "KG_COLLECTION" in result["removed"][".vscode/settings.json"]
    assert "KG_COLLECTION" in result["removed"][".claude/env"]
    assert result["left"] == {}
    claude_env = (folder / ".claude" / "env").read_text(encoding="utf-8")
    assert "vco-managed" not in claude_env and "KG_COLLECTION" not in claude_env
    for rel, env_key in ((".claude/settings.json", "env"), (".vscode/settings.json", "claude-code.env")):
        assert not set(KEYS) & set(_env_block(folder, rel, env_key)), rel


def test_a_users_own_value_is_left_and_reported_on_every_surface(project: tuple[Path, Path]) -> None:
    """Leave-alone: a routing key holding a value VCO does not project is the
    user's — on each surface it stays and is named in the reply."""
    folder, db = project
    for rel, env_key in ((".claude/settings.json", "env"), (".vscode/settings.json", "claude-code.env")):
        path = folder / rel
        data = json.loads(path.read_text(encoding="utf-8"))
        data[env_key]["KG_COLLECTION"] = "Mine_KG"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    claude_env = folder / ".claude" / "env"
    claude_env.write_text(
        claude_env.read_text(encoding="utf-8")
        + 'export PROJECT_NAME="Mine"\n# KG_COLLECTION=commented_by_me\n',
        encoding="utf-8",
    )

    result = strip_routing_keys(folder, KEYS, project_id="p-1", db_path=db)

    assert result["left"] == {
        ".claude/env": ["PROJECT_NAME"],
        ".claude/settings.json": ["KG_COLLECTION"],
        ".vscode/settings.json": ["KG_COLLECTION"],
    }
    assert _env_block(folder, ".claude/settings.json", "env")["KG_COLLECTION"] == "Mine_KG"
    assert _env_block(folder, ".vscode/settings.json", "claude-code.env")["KG_COLLECTION"] == "Mine_KG"
    assert claude_env.read_text(encoding="utf-8") == (
        'export PROJECT_NAME="Mine"\n# KG_COLLECTION=commented_by_me\n'
    )


def test_a_legacy_line_outside_the_block_goes_only_when_it_holds_vcos_value(
    project: tuple[Path, Path],
) -> None:
    """`.claude/env` outside the block: equal value → VCO's (removed); CRLF
    of the kept lines is preserved."""
    folder, db = project
    expected = projected_env("p-1", db_path=db)
    claude_env = folder / ".claude" / "env"
    claude_env.write_bytes(
        f'export KG_COLLECTION="{expected["KG_COLLECTION"]}"\r\nexport MINE=1\r\n'.encode()
        + claude_env.read_bytes()
    )
    result = strip_routing_keys(folder, KEYS, project_id="p-1", db_path=db)
    assert claude_env.read_bytes() == b"export MINE=1\r\n"
    assert ".claude/env" not in result["left"]


def test_without_a_projection_nothing_outside_the_block_goes(project: tuple[Path, Path]) -> None:
    """An unknown project (the row is gone) proves nothing: VCO's block still
    goes (its markers are the evidence), every other routing key stays and is
    reported, and the reply says why."""
    folder, db = project
    before_settings = (folder / ".claude" / "settings.json").read_bytes()
    result = strip_routing_keys(folder, KEYS, project_id="p-gone", db_path=db)
    assert result["projection"] == "unavailable"
    assert "ProjectNotFound" in str(result["projection_error"])
    assert (folder / ".claude" / "settings.json").read_bytes() == before_settings
    assert "KG_COLLECTION" in result["left"][".claude/settings.json"]
    assert "vco-managed" not in (folder / ".claude" / "env").read_text(encoding="utf-8")


def test_the_cli_answers_one_json_object_with_names_only(project: tuple[Path, Path]) -> None:
    folder, db = project
    done = subprocess.run(
        [sys.executable, "-m", "vco_lib.unregister_env", "strip-routing",
         "--project-folder", str(folder), "--project-id", "p-1", "--db-path", str(db)],
        input=json.dumps({"keys": KEYS}), capture_output=True, text=True, env=child_env(),
    )
    assert done.returncode == 0, done.stderr
    reply = json.loads(done.stdout)
    assert reply["ok"] is True and reply["projection"] == "resolved"
    assert "KG_COLLECTION" in reply["removed"][".claude/settings.json"]
    assert "Acme_" not in done.stdout, "names only — never a value"
