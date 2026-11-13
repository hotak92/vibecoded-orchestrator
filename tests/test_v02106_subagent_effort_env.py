# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — subagents run at effort ``medium`` unless something names one.

Claude Code 2.1.316 added ``CLAUDE_CODE_SUBAGENT_EFFORT``: the effort a
subagent runs at when neither the dispatch nor the definition names one.
Without it a subagent inherits the session's effort, so a main chat at
``high`` silently runs every lane at ``high`` — the opposite of VCO's
medium-default rule. VCO ships ``CLAUDE_CODE_SUBAGENT_EFFORT=medium`` in each
project's ``.claude/settings.json`` ``env`` block — the canonical env channel
— delivered exactly the way ``"idleCompaction": false`` is (v0.2.104): a
shipped settings-template key the ordinary user-wins settings merge
(``vco_lib.settings_merge.smart_merge_settings``) applies.

Contract pinned here:

* both shipped settings templates carry the key under ``env``;
* a fresh bundle writes it into ``.claude/settings.json``;
* a bundle update ADDS it to a project whose file lacks it (no special case);
* a value the user set is never replaced;
* the merge is idempotent (a second update reports ``unchanged``);
* only the project's ``.claude/settings.json`` carries it — never
  ``.claude/settings.local.json`` (it is not a machine-local key) and never
  the user's ``~/.claude``;
* both shipped CLAUDE.md templates document it next to the effort rule.
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

ENV_KEY = "CLAUDE_CODE_SUBAGENT_EFFORT"
ENV_VALUE = "medium"


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _merge(template: Path, target: Path) -> str:
    status, _ = bundle_settings_io.merge_settings_template(
        template, target, dry_run=False, write=_write,
        project_root=target.parent.parent)
    return status


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_template_ships_the_key_under_env(os_name):
    data = json.loads(TEMPLATES[os_name].read_text(encoding="utf-8"))
    assert data["env"][ENV_KEY] == ENV_VALUE


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_fresh_settings_get_the_key(tmp_path, os_name):
    target = tmp_path / "proj" / ".claude" / "settings.json"
    assert _merge(TEMPLATES[os_name], target) == "created"
    assert json.loads(target.read_text(encoding="utf-8"))["env"][ENV_KEY] == ENV_VALUE


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_update_adds_the_key_and_is_idempotent(tmp_path, os_name):
    target = tmp_path / "proj" / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"permissions": {"allow": ["Bash(mine *)"]}}),
                      encoding="utf-8")
    assert _merge(TEMPLATES[os_name], target) == "merged"
    after = json.loads(target.read_text(encoding="utf-8"))
    assert after["env"][ENV_KEY] == ENV_VALUE
    assert "Bash(mine *)" in after["permissions"]["allow"]
    assert _merge(TEMPLATES[os_name], target) == "unchanged"


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
@pytest.mark.parametrize("user_value", ["high", "low"])
def test_a_user_value_is_kept(tmp_path, os_name, user_value):
    target = tmp_path / "proj" / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"env": {ENV_KEY: user_value}}), encoding="utf-8")
    _merge(TEMPLATES[os_name], target)
    assert json.loads(target.read_text(encoding="utf-8"))["env"][ENV_KEY] == user_value


def _fake_orchestrator(root: Path) -> Path:
    (root / "templates").mkdir(parents=True)
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    for path in TEMPLATES.values():
        shutil.copy(path, root / "templates" / path.name)
    return root


def test_bundle_update_delivers_the_key_to_an_existing_project(tmp_path, monkeypatch):
    """End to end through ``install_project_bundle`` — and the key lands in
    the project's settings.json only."""
    from tests.common.launcher_db_fixture import make_launcher_db

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    orch = _fake_orchestrator(tmp_path / "orch")
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    (folder / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": []}}), encoding="utf-8")
    db = make_launcher_db(tmp_path / "state", projects=[
        {"project_id": "p1", "name": "Proj", "folder_path": folder}])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    result = project_init.install_project_bundle(folder, orchestrator_root=orch,
                                                 update_mode=True)
    assert result["settings_action"] == "merged"
    data = json.loads((folder / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data["env"][ENV_KEY] == ENV_VALUE
    # Not the machine-local file: the key is portable, so the local layer
    # (machine-local keys only, v0.2.104 L5) never carries it.
    local = folder / ".claude" / "settings.local.json"
    if local.exists():
        local_env = json.loads(local.read_text(encoding="utf-8")).get("env", {})
        assert ENV_KEY not in local_env
    # And never the user's global settings.
    assert not (home / ".claude" / "settings.json").exists()


@pytest.mark.parametrize(
    "name", ["CLAUDE.md.template", "ORCHESTRATOR-CLAUDE.md.template"])
def test_shipped_guidance_documents_the_key(name):
    text = (REPO_ROOT / "templates" / name).read_text(encoding="utf-8")
    assert f"{ENV_KEY}={ENV_VALUE}" in text, name
