# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — VCO projects are not compacted while idle (owner 2026-10-18).

Claude Code 2.1.299 compacts a 200k+ token conversation left idle ~54 min.
The documented off switch is the top-level settings key
``"idleCompaction": false``. Contract pinned here:

* both shipped settings templates carry it, as a JSON boolean ``false``;
* a fresh bundle writes it into ``.claude/settings.json``;
* a bundle update ADDS it to a project whose file lacks it (the ordinary
  user-wins settings merge — no special case);
* a value the user set (``true`` or ``false``) is never replaced;
* the merge is idempotent (a second update reports ``unchanged``);
* only the project's ``.claude/settings.json`` is written — never
  ``.claude/settings.local.json`` and never the user's ``~/.claude``.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from vco_lib import bundle_settings_io, project_init

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = {
    "linux": REPO_ROOT / "templates" / "settings.json.linux.template",
    "windows": REPO_ROOT / "templates" / "settings.json.windows.template",
}


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _merge(template: Path, target: Path) -> str:
    status, _ = bundle_settings_io.merge_settings_template(
        template, target, dry_run=False, write=_write,
        project_root=target.parent.parent)
    return status


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_template_ships_idle_compaction_off_as_a_boolean(os_name):
    data = json.loads(TEMPLATES[os_name].read_text(encoding="utf-8"))
    assert data["idleCompaction"] is False
    # The normal auto-compact switch is NOT touched by VCO.
    assert "autoCompactEnabled" not in data


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_fresh_settings_get_the_key(tmp_path, os_name):
    target = tmp_path / "proj" / ".claude" / "settings.json"
    assert _merge(TEMPLATES[os_name], target) == "created"
    assert json.loads(target.read_text(encoding="utf-8"))["idleCompaction"] is False


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_update_adds_the_key_and_is_idempotent(tmp_path, os_name):
    target = tmp_path / "proj" / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"permissions": {"allow": ["Bash(mine *)"]}}),
                      encoding="utf-8")
    assert _merge(TEMPLATES[os_name], target) == "merged"
    after = json.loads(target.read_text(encoding="utf-8"))
    assert after["idleCompaction"] is False
    assert "Bash(mine *)" in after["permissions"]["allow"]
    assert _merge(TEMPLATES[os_name], target) == "unchanged"


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
@pytest.mark.parametrize("user_value", [True, False])
def test_a_user_value_is_kept(tmp_path, os_name, user_value):
    target = tmp_path / "proj" / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"idleCompaction": user_value}), encoding="utf-8")
    _merge(TEMPLATES[os_name], target)
    assert json.loads(target.read_text(encoding="utf-8"))["idleCompaction"] is user_value


def _fake_orchestrator(root: Path) -> Path:
    (root / "templates").mkdir(parents=True)
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    for path in TEMPLATES.values():
        shutil.copy(path, root / "templates" / path.name)
    return root


def _bundle_update(tmp_path: Path, monkeypatch, settings: dict | None) -> tuple[Path, dict]:
    from tests.common.launcher_db_fixture import make_launcher_db

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    orch = _fake_orchestrator(tmp_path / "orch")
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    if settings is not None:
        (folder / ".claude" / "settings.json").write_text(json.dumps(settings),
                                                          encoding="utf-8")
    db = make_launcher_db(tmp_path / "state", projects=[
        {"project_id": "p1", "name": "Proj", "folder_path": folder}])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    result = project_init.install_project_bundle(folder, orchestrator_root=orch,
                                                 update_mode=True)
    return folder, result


def test_bundle_update_delivers_the_key_to_an_existing_project(tmp_path, monkeypatch):
    folder, result = _bundle_update(tmp_path, monkeypatch, {"permissions": {"allow": []}})
    assert result["settings_action"] == "merged"
    data = json.loads((folder / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data["idleCompaction"] is False
    # Only the project's settings.json: no local file, nothing in the user's home.
    assert not (folder / ".claude" / "settings.local.json").exists()
    assert not (tmp_path / "home" / ".claude" / "settings.json").exists()


def test_bundle_update_keeps_a_user_true(tmp_path, monkeypatch):
    folder, _result = _bundle_update(tmp_path, monkeypatch, {"idleCompaction": True})
    data = json.loads((folder / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data["idleCompaction"] is True
