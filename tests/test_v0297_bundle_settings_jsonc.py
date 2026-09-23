# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97: the bundle's settings.json merge edits JSONC — or refuses visibly.

Before: ``project_init._merge_settings_template_for_bundle`` returned
``unchanged (user file unparseable)`` for ANY file that was not strict JSON,
so a ``.claude/settings.json`` carrying one comment never received a newly
shipped hook again, and nothing anywhere said so.

Now (``vco_lib.bundle_settings_io``): a JSONC file is edited in place through
``vco_lib.jsonc_edit`` — the hooks the merge changed are written, every
comment elsewhere survives, the result is re-parsed and verified. An edit that
cannot be verified, and a file that cannot be read at all, are refused through
``vco_lib.settings_refusal``: byte-identical file, a
``settings_write_refused_bundle_claude_settings_json`` deferral, cleared by
the next merge that succeeds. Act + leave-alone for each branch.
"""
from __future__ import annotations

import json
from pathlib import Path

from vco_lib import bundle_settings_io, jsonc_edit, project_init
from vco_lib.deferral_report import DeferralReport

CID = "settings_write_refused_bundle_claude_settings_json"
NEW_HOOK = {"type": "command", "command": "bash .claude/hooks/brand-new.sh"}
TEMPLATE = {"hooks": {"Stop": [{"matcher": "", "hooks": [NEW_HOOK]}]}}

#: A user file with comments OUTSIDE the member the merge rewrites.
JSONC_EDITABLE = """{
  // my permissions — keep this comment
  "permissions": {
    "allow": ["Bash"], // trailing note
  },
  "hooks": {}
}
"""

#: The member the merge must rewrite (`hooks.Stop`) holds a comment itself,
#: so replacing it would lose that comment: the edit must be refused.
JSONC_UNEDITABLE = """{
  "hooks": {
    "Stop": [
      // my own Stop hook, annotated
      {"matcher": "", "hooks": [{"type": "command", "command": "bash mine.sh"}]}
    ]
  }
}
"""


def _project(tmp_path: Path, settings_text: str | None) -> tuple[Path, Path, Path]:
    folder = tmp_path / "proj"
    target = folder / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    if settings_text is not None:
        target.write_text(settings_text, encoding="utf-8")
    template = tmp_path / "settings.template.json"
    template.write_text(json.dumps(TEMPLATE), encoding="utf-8")
    return folder, target, template


def _merge(template: Path, target: Path, folder: Path, *, dry_run: bool = False) -> str:
    status, _ = project_init._merge_settings_template_for_bundle(
        template, target, dry_run=dry_run, project_root=folder)
    return status


def _has_entry(folder: Path) -> bool:
    return DeferralReport.read(folder).has_condition(CID)


def _commands(data: dict) -> list[str]:
    return [h["command"] for g in data.get("hooks", {}).get("Stop", []) for h in g["hooks"]]


def test_jsonc_file_gets_the_new_hook_and_keeps_its_comments(tmp_path):
    folder, target, template = _project(tmp_path, JSONC_EDITABLE)
    assert _merge(template, target, folder) == "merged"
    text = target.read_text(encoding="utf-8")
    assert "// my permissions — keep this comment" in text
    assert "// trailing note" in text
    data = jsonc_edit.loads(text)
    assert NEW_HOOK["command"] in _commands(data)
    assert data["permissions"] == {"allow": ["Bash"]}
    assert not _has_entry(folder)


def test_jsonc_dry_run_reports_without_touching_anything(tmp_path):
    folder, target, template = _project(tmp_path, JSONC_EDITABLE)
    assert _merge(template, target, folder, dry_run=True) == "would-merge"
    assert target.read_text(encoding="utf-8") == JSONC_EDITABLE
    assert not _has_entry(folder)


def test_unverifiable_jsonc_edit_is_refused_visibly_and_left_byte_identical(tmp_path):
    folder, target, template = _project(tmp_path, JSONC_UNEDITABLE)
    before = target.read_bytes()
    assert _merge(template, target, folder) == bundle_settings_io.STATUS_EDIT_REFUSED
    assert target.read_bytes() == before
    report = DeferralReport.read(folder)
    assert report.has_condition(CID)
    ledger = (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text(encoding="utf-8")
    assert "install-bundle --folder" in ledger and "--update" in ledger


def test_unparseable_file_is_refused_visibly_but_a_dry_run_records_nothing(tmp_path):
    folder, target, template = _project(tmp_path, '{"hooks": {},, }')
    assert _merge(template, target, folder, dry_run=True) == bundle_settings_io.STATUS_UNPARSEABLE
    assert not _has_entry(folder)
    assert _merge(template, target, folder) == bundle_settings_io.STATUS_UNPARSEABLE
    assert target.read_text(encoding="utf-8") == '{"hooks": {},, }'
    assert _has_entry(folder)


def test_the_next_successful_merge_clears_the_refusal(tmp_path):
    folder, target, template = _project(tmp_path, JSONC_UNEDITABLE)
    _merge(template, target, folder)
    assert _has_entry(folder)
    # The user drops the comment that blocked the in-place edit.
    target.write_text(JSONC_UNEDITABLE.replace("      // my own Stop hook, annotated\n", ""),
                      encoding="utf-8")
    assert _merge(template, target, folder) == "merged"
    assert not _has_entry(folder)
    assert NEW_HOOK["command"] in _commands(json.loads(target.read_text(encoding="utf-8")))


def test_strict_json_keeps_the_pre_v0297_layout(tmp_path):
    folder, target, template = _project(tmp_path, json.dumps({"mine": 1}))
    assert _merge(template, target, folder) == "merged"
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert target.read_text(encoding="utf-8") == json.dumps(merged, indent=2) + "\n"
    assert merged["mine"] == 1 and NEW_HOOK["command"] in _commands(merged)



def _fake_orchestrator(root: Path) -> Path:
    (root / "templates").mkdir(parents=True)
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    for os_name in ("linux", "windows"):
        (root / "templates" / f"settings.json.{os_name}.template").write_text(
            json.dumps(TEMPLATE, indent=2), encoding="utf-8")
    return root


def _update_world(tmp_path: Path, monkeypatch, settings_text: str) -> tuple[Path, Path, dict]:
    from tests.common.launcher_db_fixture import make_launcher_db

    orch = _fake_orchestrator(tmp_path / "orch")
    folder, target, _template = _project(tmp_path, settings_text)
    db = make_launcher_db(tmp_path / "state", projects=[
        {"project_id": "p1", "name": "Proj", "folder_path": folder}])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    result = project_init.install_project_bundle(folder, orchestrator_root=orch, update_mode=True)
    return folder, target, result


def test_bundle_update_surfaces_a_refusal_in_the_envelope(tmp_path, monkeypatch):
    """The whole `install-bundle --update` path: the refusal reaches the
    result's warnings (the CLI's WARNING lines, the launcher's toast) AND
    the project's ledger; the file is untouched."""
    folder, target, result = _update_world(tmp_path, monkeypatch, JSONC_UNEDITABLE)
    assert result["settings_action"] == bundle_settings_io.STATUS_EDIT_REFUSED
    assert any("newly shipped hooks NOT added" in w for w in result["warnings"])
    # The hooks member is untouched, comment included. (The env projection
    # that runs later in the same update may add its own `env` member — a
    # separate writer editing a different member, in place.)
    after = target.read_text(encoding="utf-8")
    assert "// my own Stop hook, annotated" in after
    assert NEW_HOOK["command"] not in after
    assert _has_entry(folder)


def test_bundle_update_merges_a_jsonc_file_with_no_warning(tmp_path, monkeypatch):
    folder, target, result = _update_world(tmp_path, monkeypatch, JSONC_EDITABLE)
    assert result["settings_action"] == "merged"
    assert not any("settings.json: unchanged" in w for w in result["warnings"])
    assert "// trailing note" in target.read_text(encoding="utf-8")
    assert not _has_entry(folder)
