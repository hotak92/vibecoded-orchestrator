# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 (R18-01 follow-up) — a project created before 0.2.100 and never
updated has no previous render to prove its CLAUDE.md untouched. The split
migration must still recognise it (render of a RELEASED template → silent
conversion, no backup, no row) instead of raising a false "edited" alarm.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import legacy_claude_md, materialize, project_init  # noqa: E402
from vco_lib import project_templates as pt  # noqa: E402
from vco_lib.deferral_report import MANAGED_REGION_OPEN, DeferralReport  # noqa: E402

CID = pt.USER_SECTION_REVIEW_CID


def _table() -> dict:
    return tomllib.loads(legacy_claude_md.TABLE_PATH.read_text(encoding="utf-8"))


class TestTheReferenceData:
    def test_entries_are_distinct_hash_pinned_and_cover_the_marker_era(self):
        entries = _table()["template"]
        digests = [e["sha256"] for e in entries]
        assert len(set(digests)) == len(digests), "identical versions are deduplicated"
        for e in entries:
            assert hashlib.sha256(e["body"].encode("utf-8")).hexdigest() == e["sha256"]
            assert e["version"] == e["tags"][0]
        assert entries[0]["version"] == "v0.2.33"
        assert "v0.2.99" in entries[-1]["tags"]

    def test_entries_are_the_tagged_bytes(self):
        """Where the release tags are available (a git checkout), every entry is
        byte-for-byte what `git show <tag>:templates/CLAUDE.md.template` gives."""
        for e in _table()["template"]:
            proc = subprocess.run(
                ["git", "show", f"{e['version']}:templates/CLAUDE.md.template"],
                cwd=REPO_ROOT, capture_output=True)
            if proc.returncode != 0:
                pytest.skip("release tags not available in this checkout")
            assert proc.stdout.decode("utf-8") == e["body"], e["version"]


def _orch(tmp: Path) -> Path:
    orch = tmp / "orch"
    (orch / "templates").mkdir(parents=True)
    (orch / "vct-module.json").write_text("{}\n", encoding="utf-8")
    py = materialize.venv_python_path(orch)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", encoding="utf-8")
    (orch / "templates" / "CLAUDE.md.template").write_bytes(
        (REPO_ROOT / "templates" / "CLAUDE.md.template").read_bytes())
    return orch


def _old_renderer(template: str, *, name: str, project: Path, orch: Path,
                  modules: set) -> str:
    """The pre-v0.2.100 bundle's CLAUDE.md, verbatim in behaviour: conditional
    sections, then a raw ``str.replace`` loop, wrapped whole in the markers."""
    text = project_init.render_conditional_blocks(template, active_modules=modules)
    for k, v in {"{{PROJECT_NAME}}": name, "{{PROJECT_ROOT}}": str(project),
                 "{{ORCHESTRATOR_ROOT}}": str(orch)}.items():
        text = text.replace(k, v)
    return project_init.merge_managed_region(existing_claude_md="", new_managed_body=text)


def _old_project(tmp: Path, version: str, *, name: str, modules: set,
                 edit: bool = False) -> tuple:
    orch = _orch(tmp)
    project = tmp / "my-proj"
    project.mkdir()
    body = next(e["body"] for e in _table()["template"] if e["version"] == version)
    text = _old_renderer(body, name=name, project=project, orch=orch, modules=modules)
    if edit:
        text = text.replace("## Tech Stack", "## Tech Stack\n\nRust + Tauri, really.", 1)
    (project / "CLAUDE.md").write_text(text + "\n## Added below\nmine\n", encoding="utf-8")
    return orch, project  # created, never updated: no sidecar, no state


def _update(project: Path, orch: Path, name=None) -> dict:
    with contextlib.redirect_stderr(io.StringIO()):
        return project_init.install_project_bundle(
            project, orchestrator_root=orch, update_mode=True, project_name=name)


def _backups(project: Path) -> list:
    root = project / ".claude" / "backups" / "bundle-adoptions"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _cids(project: Path) -> set:
    return {e.condition_id for e in DeferralReport.read(project).entries}


class TestNeverUpdatedPreSplitProject:
    @pytest.mark.parametrize("version,name,given,modules", [
        ("v0.2.86", "my-proj", None, {"diagrams"}),         # folder-name identity
        ("v0.2.98", "Custom Name", "Custom Name", set()),     # name given at install
        ("v0.2.33", "my-proj", None, set()),                  # first marked release
    ])
    def test_untouched_old_render_converts_silently(self, tmp_path, version, name,
                                                    given, modules):
        orch, project = _old_project(tmp_path, version, name=name, modules=modules)
        res = _update(project, orch, given)
        text = (project / "CLAUDE.md").read_text(encoding="utf-8")
        assert res["templates"]["claude_md_migrated"] == ["migrated-untouched"]
        assert res["templates"]["claude_md_legacy_match"] == [version]
        assert _backups(project) == []
        assert CID not in _cids(project)
        assert text.count(MANAGED_REGION_OPEN) == 1
        assert text.count("## Project Overview") == 1
        assert pt.managed_body(text).lstrip("-\n").startswith("## VCO Paths")
        assert text.endswith("\n## Added below\nmine\n")

    def test_an_edited_old_render_takes_the_edited_route(self, tmp_path):
        orch, project = _old_project(tmp_path, "v0.2.86", name="my-proj",
                                     modules={"diagrams"}, edit=True)
        res = _update(project, orch)
        text = (project / "CLAUDE.md").read_text(encoding="utf-8")
        assert res["templates"]["claude_md_migrated"] == ["migrated-edited"]
        assert "Rust + Tauri, really." in text[:text.index(MANAGED_REGION_OPEN)]
        assert len(_backups(project)) == 1
        assert CID in _cids(project)
