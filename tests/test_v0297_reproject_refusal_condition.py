# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97: a settings refusal during ``reproject-all`` is reported as what it is.

The projection leaves an unparseable ``.claude/settings.json`` byte-identical
and records ``settings_write_refused_claude_settings_json`` in that project's
ledger. ``reproject-all`` then ALSO emitted
``codegraph_access_list_reprojection_failed`` — "the code-graph access-list
migration failed, re-run reproject-all" — which names the wrong cause and a
fix that cannot work. A refusal is now a ``refused`` outcome with no
code-graph entry; a genuine failure (leave-alone twin) still gets one.
"""
from __future__ import annotations

from pathlib import Path

from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib import config_projection as cp
from vco_lib.deferral_report import DeferralReport

CODEGRAPH_CID = "codegraph_access_list_reprojection_failed"
REFUSAL_CID = "settings_write_refused_claude_settings_json"


def _db(tmp_path: Path, folder: Path) -> Path:
    return make_launcher_db(tmp_path / "state" / "launcher.db", projects=[{
        "project_id": "p1", "name": "Proj", "folder_path": str(folder), "slug": "proj",
        "kg_primary": "Proj_KnowledgeGraph",
    }])


def test_refused_settings_file_emits_only_the_accurate_condition(tmp_path):
    folder = tmp_path / "proj"
    settings = folder / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"hooks": {},, }')
    report = DeferralReport()
    outcomes = cp.reproject_all_registered_projects(
        db_path=_db(tmp_path, folder), surfaces=["claude_settings_json"],
        deferral_report=report)
    assert [o["status"] for o in outcomes] == ["refused"]
    assert not any(e.condition_id == CODEGRAPH_CID for e in report.entries), report.entries
    assert DeferralReport.read(folder).has_condition(REFUSAL_CID)
    assert settings.read_bytes() == b'{"hooks": {},, }'


def test_a_genuine_failure_still_emits_the_codegraph_condition(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory must be", encoding="utf-8")
    folder = blocker / "proj"
    report = DeferralReport()
    outcomes = cp.reproject_all_registered_projects(
        db_path=_db(tmp_path, folder), surfaces=["claude_settings_json"],
        deferral_report=report)
    assert [o["status"] for o in outcomes] == ["failed"]
    assert any(e.condition_id == CODEGRAPH_CID for e in report.entries)


def test_update_summary_names_a_refusal_apart_from_a_failure(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(cp, "reproject_all_registered_projects", lambda **_kw: [
        {"project_id": "p1", "project_name": "P", "status": "refused",
         "detail": "x", "keys_written": []},
    ])
    cp.run_update_reprojection_step(print_fn=lines.append, deferral_report=object())
    assert lines and "settings file left untouched" in lines[0]
    assert "deferred" not in lines[0]
