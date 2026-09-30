# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 review R18-01 — the project CLAUDE.md is SPLIT.

Owner decision (verbatim): "split + keep edits, but what if the update updates
the user editable region? I think only in that case, we should notify in
update_deferred about needing to check the new CLAUDE.md template and verify
what needs updating in user's section of the materialized artifact".

* The VCO-managed region (between the markers) is re-rendered on every update.
* The USER SECTION (outside the markers) is written only at creation and never
  rewritten; a change to its TEMPLATE writes a sidecar and ONE
  ``claude_md_user_section_review`` row — no row when it did not change.
* A pre-split file is migrated once: untouched → split layout; edited → the
  user's text is kept verbatim above the markers, backed up, and reviewed.
* The launcher's module toggle (``render_claude_md``) follows the same rules.

Every test drives the real entry points (``install_project_bundle`` /
``render_claude_md``) against a fixture orchestrator whose template has the
real shape: user section, markers, managed body, text after the markers.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import materialize, project_init  # noqa: E402
from vco_lib import project_templates as pt  # noqa: E402
from vco_lib.deferral_report import (  # noqa: E402
    MANAGED_REGION_CLOSE,
    MANAGED_REGION_OPEN,
    DeferralReport,
    strip_vco_owned_regions,
)

CID = pt.USER_SECTION_REVIEW_CID

USER_V1 = """# {{PROJECT_NAME}} — Project Instructions

Your part is outside the markers.

## Project Overview

_(Describe the project.)_

"""
USER_V2 = USER_V1.replace("_(Describe the project.)_",
                          "_(Describe the project, its users and its limits.)_")
MANAGED_V1 = "## SESSION START\n\nRead CONTEXT_STATE.md. Orchestrator: {{ORCHESTRATOR_ROOT}}\n"
MANAGED_V2 = MANAGED_V1 + "\n## New VCO section\n\nsomething new\n"
BOTTOM = "\n\n---\n\n_(Add project-specific sections below.)_\n"


def _template(user: str = USER_V1, managed: str = MANAGED_V1) -> str:
    return f"{user}{MANAGED_REGION_OPEN}\n{managed}{MANAGED_REGION_CLOSE}{BOTTOM}"


def _orch(tmp: Path, template: str) -> Path:
    orch = tmp / "orch"
    (orch / "templates").mkdir(parents=True, exist_ok=True)
    (orch / "vct-module.json").write_text("{}\n", encoding="utf-8")
    py = materialize.venv_python_path(orch)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", encoding="utf-8")
    (orch / "templates" / "CLAUDE.md.template").write_text(template, encoding="utf-8")
    return orch


def _set_template(orch: Path, template: str) -> None:
    (orch / "templates" / "CLAUDE.md.template").write_text(template, encoding="utf-8")


def _bundle(project: Path, orch: Path, *, update: bool) -> dict:
    with contextlib.redirect_stderr(io.StringIO()):
        return project_init.install_project_bundle(
            project, orchestrator_root=orch, update_mode=update)


def _rows(project: Path) -> dict:
    return {e.condition_id: e for e in DeferralReport.read(project).entries}


def _backups(project: Path) -> list:
    root = project / ".claude" / "backups" / "bundle-adoptions"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _live(project: Path) -> str:
    return (project / "CLAUDE.md").read_text(encoding="utf-8")


def _user_part(text: str) -> str:
    return text[:text.index(MANAGED_REGION_OPEN)]


@pytest.fixture
def installed(tmp_path):
    orch = _orch(tmp_path, _template())
    project = tmp_path / "proj"
    project.mkdir()
    _bundle(project, orch, update=False)
    return orch, project


class TestFirstInstall:
    def test_writes_both_parts_and_records_the_user_template(self, installed):
        orch, project = installed
        text = _live(project)
        top = _user_part(text)
        assert "## Project Overview" in top and "proj — Project Instructions" in top
        assert pt.managed_body(text).startswith("## SESSION START")
        assert str(orch) in pt.managed_body(text)
        assert text.endswith(BOTTOM)
        assert text.count(MANAGED_REGION_OPEN) == 1
        state = json.loads((project / pt.USER_SECTION_STATE_REL).read_text(encoding="utf-8"))
        assert state["acknowledged_template_sha256"] == pt.user_section_template_hash(_template())
        assert CID not in _rows(project)

    def test_the_shipped_template_has_the_split(self):
        raw = (REPO_ROOT / "templates" / "CLAUDE.md.template").read_text(encoding="utf-8")
        top, body, bottom = pt.split_claude_md(raw)
        for heading in ("## Project Overview", "## Tech Stack", "## Key Paths"):
            assert heading in top and heading not in body
        assert "## SESSION START" in body and "## SESSION START" not in top
        assert "Add project-specific sections below" in bottom
        # Line 3 names which part is the user's.
        assert "OUTSIDE the `VCO_MANAGED` marker" in raw.splitlines()[2] \
            or "OUTSIDE the `VCO_MANAGED` marker" in top


class TestUpdate:
    def test_unchanged_user_template_leaves_the_user_section_and_emits_nothing(self, installed):
        orch, project = installed
        live = project / "CLAUDE.md"
        text = _live(project).replace("_(Describe the project.)_", "MY OVERVIEW")
        live.write_text(text + "\n## My notes\nkeep\n", encoding="utf-8")
        _set_template(orch, _template(managed=MANAGED_V2))
        res = _bundle(project, orch, update=True)
        after = _live(project)
        assert "MY OVERVIEW" in _user_part(after)
        assert after.endswith("\n## My notes\nkeep\n")
        assert "## New VCO section" in pt.managed_body(after)
        assert after.count("## Project Overview") == 1, "the user section is never re-added"
        assert res["templates"]["managed_backups"] == [] and _backups(project) == []
        assert CID not in _rows(project)
        assert not (project / pt.USER_SECTION_SIDECAR_REL).exists()

    def test_changed_user_template_keeps_the_user_text_writes_a_sidecar_and_one_row(
            self, installed):
        orch, project = installed
        live = project / "CLAUDE.md"
        live.write_text(_live(project).replace("_(Describe the project.)_", "MY OVERVIEW"),
                        encoding="utf-8")
        _set_template(orch, _template(user=USER_V2))
        _bundle(project, orch, update=True)
        after = _live(project)
        assert "MY OVERVIEW" in _user_part(after)
        assert "its users and its limits" not in after
        sidecar = (project / pt.USER_SECTION_SIDECAR_REL).read_text(encoding="utf-8")
        assert "its users and its limits" in sidecar
        row = _rows(project)[CID]
        assert row.resolved_disposition == "action_required"
        assert row.dismiss_fields["reason"] == "template_changed"
        assert row.dismiss_fields["template_sha256"] == pt.user_section_template_hash(
            _template(user=USER_V2))
        # A second update with the same template: still exactly one row, first
        # detection kept.
        _bundle(project, orch, update=True)
        again = _rows(project)[CID]
        assert again.detected_at == row.detected_at
        assert [e.condition_id for e in DeferralReport.read(project).entries].count(CID) == 1

    def test_dismissal_acknowledges_until_the_template_changes_again(self, installed):
        orch, project = installed
        _set_template(orch, _template(user=USER_V2))
        _bundle(project, orch, update=True)
        assert CID in _rows(project)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = project_init.main(["dismiss-deferral", "--folder", str(project),
                                    "--condition-id", CID])
        assert rc == 0 and CID not in _rows(project)
        _bundle(project, orch, update=True)
        assert CID not in _rows(project)
        state = json.loads((project / pt.USER_SECTION_STATE_REL).read_text(encoding="utf-8"))
        assert state["acknowledged_template_sha256"] == pt.user_section_template_hash(
            _template(user=USER_V2))
        _set_template(orch, _template(user=USER_V2 + "\nOne more line.\n"))
        _bundle(project, orch, update=True)
        assert CID in _rows(project)

    def test_an_edited_managed_region_is_still_backed_up_then_replaced(self, installed):
        orch, project = installed
        live = project / "CLAUDE.md"
        live.write_text(_live(project).replace("Read CONTEXT_STATE.md.", "MY MANAGED EDIT"),
                        encoding="utf-8")
        res = _bundle(project, orch, update=True)
        assert "MY MANAGED EDIT" not in _live(project)
        (backup,) = _backups(project)
        assert "MY MANAGED EDIT" in backup.read_text(encoding="utf-8")
        assert res["templates"]["managed_backups"]
        assert CID not in _rows(project), "a managed-region edit is not a user-section review"


class TestPreSplitMigration:
    """Files written before the split: the WHOLE template inside the markers
    and no recorded user-section state."""

    OLD_TEMPLATE = "# {{PROJECT_NAME}}\n\n## Project Overview\n\n_(Describe.)_\n\n" + MANAGED_V1

    def _old_install(self, tmp_path, *, edit: bool):
        orch = _orch(tmp_path, self.OLD_TEMPLATE)  # marker-less, as before 0.2.100
        project = tmp_path / "proj"
        project.mkdir()
        _bundle(project, orch, update=False)
        (project / pt.USER_SECTION_STATE_REL).unlink()  # the old code wrote none
        live = project / "CLAUDE.md"
        # The old update path wrote the reference sidecar = the old render
        # (no markers) and left the live file alone.
        ref = project / ".claude" / "context" / "templates" / "CLAUDE.md.reference.md"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(pt.managed_body(_live(project)) + "\n", encoding="utf-8")
        live.write_text(_live(project) + "\n## Added below\nmine\n", encoding="utf-8")
        if edit:
            live.write_text(_live(project).replace("_(Describe.)_", "MY REAL OVERVIEW"),
                            encoding="utf-8")
        _set_template(orch, _template())
        return orch, project

    def test_untouched_old_layout_is_converted_to_the_split(self, tmp_path):
        orch, project = self._old_install(tmp_path, edit=False)
        res = _bundle(project, orch, update=True)
        text = _live(project)
        assert text.count(MANAGED_REGION_OPEN) == 1
        assert "Your part is outside the markers." in _user_part(text)
        assert pt.managed_body(text).startswith("## SESSION START")
        assert "_(Describe.)_" not in text, "the untouched old body is replaced"
        assert text.endswith("\n## Added below\nmine\n")
        assert _backups(project) == []
        assert CID not in _rows(project)
        assert res["templates"]["claude_md_migrated"] == ["migrated-untouched"]

    def test_edited_old_layout_keeps_the_text_outside_the_markers(self, tmp_path):
        orch, project = self._old_install(tmp_path, edit=True)
        before = _live(project)
        res = _bundle(project, orch, update=True)
        text = _live(project)
        user = _user_part(text)
        assert "MY REAL OVERVIEW" in user, "the user's text is kept, verbatim"
        old_body = pt.managed_body(before)
        assert old_body in user
        assert pt.managed_body(text).startswith("## SESSION START")
        assert text.endswith("\n## Added below\nmine\n")
        (backup,) = _backups(project)
        assert backup.read_text(encoding="utf-8") == before
        row = _rows(project)[CID]
        assert row.dismiss_fields["reason"] == "migrated"
        assert res["templates"]["claude_md_migrated"] == ["migrated-edited"]
        # Idempotent: the next update moves nothing again.
        _bundle(project, orch, update=True)
        assert _live(project) == text and len(_backups(project)) == 1


class TestPreSplitDecidedFromContent:
    """Review R18F-01: pre-split vs split is decided from the CONTENT. A stale
    or foreign state file (it is untracked, so it survives a git checkout of a
    branch whose CLAUDE.md is still pre-split) or a user line above the opening
    marker must never send a pre-split file down the split branch, which
    replaces the managed body — the user's text included."""

    OLD_TEMPLATE = TestPreSplitMigration.OLD_TEMPLATE
    _old_install = TestPreSplitMigration._old_install
    USER_LINES = ("MY REAL OVERVIEW", "## Added below", "mine")

    def _stale_state(self, project: Path) -> None:
        state = project / pt.USER_SECTION_STATE_REL
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({
            "acknowledged_template_sha256": pt.user_section_template_hash(_template()),
            "managed_body_sha256": "0" * 64,
            "recorded_by": "created",
        }), encoding="utf-8")

    def _assert_kept_and_migrated(self, project: Path, res: dict, extra=()) -> str:
        text = _live(project)
        for line in (*self.USER_LINES, *extra):
            assert line in text, f"user line lost from the live file: {line!r}"
        assert "MY REAL OVERVIEW" in _user_part(text)
        assert pt.managed_body(text).startswith("## SESSION START")
        assert res["templates"]["claude_md_migrated"] == ["migrated-edited"]
        assert _rows(project)[CID].dismiss_fields["reason"] == "migrated"
        assert len(_backups(project)) == 1
        return text

    def test_state_file_present_with_pre_split_content(self, tmp_path):
        orch, project = self._old_install(tmp_path, edit=True)
        self._stale_state(project)
        res = _bundle(project, orch, update=True)
        self._assert_kept_and_migrated(project, res)

    def test_text_above_the_marker_with_pre_split_content(self, tmp_path):
        orch, project = self._old_install(tmp_path, edit=True)
        live = project / "CLAUDE.md"
        live.write_text("note\n\n" + _live(project), encoding="utf-8")
        res = _bundle(project, orch, update=True)
        text = self._assert_kept_and_migrated(project, res, extra=("note",))
        assert strip_vco_owned_regions(text).lstrip().startswith("note\n"), \
            "text above the marker stays first (after VCO's own reminder block)"

    def test_both_at_once_through_the_module_toggle(self, tmp_path):
        orch, project = self._old_install(tmp_path, edit=True)
        self._stale_state(project)
        live = project / "CLAUDE.md"
        live.write_text("note\n\n" + _live(project), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            out = project_init.render_claude_md(project, orchestrator_root=orch,
                                                project_name="proj")
        text = _live(project)
        for line in (*self.USER_LINES, "note"):
            assert line in text
        assert out["user_section_review"] == ["migrated"]

    def test_a_split_file_with_state_stays_on_the_split_branch(self, installed):
        """Leave-alone twin: a genuinely split file is not 'migrated'."""
        orch, project = installed
        live = project / "CLAUDE.md"
        live.write_text("note\n\n" + _live(project), encoding="utf-8")
        _set_template(orch, _template(managed=MANAGED_V2))
        res = _bundle(project, orch, update=True)
        assert not res["templates"].get("claude_md_migrated")
        text = _live(project)
        assert text.startswith("note\n") and text.count("## Project Overview") == 1
        assert _backups(project) == []


class TestModuleTogglePath:
    """``render_claude_md`` (the launcher's re-render-claude-md) follows the
    same rules as the bundle update (R18-01 (d))."""

    def test_toggle_keeps_the_user_section_and_backs_up_an_edited_region(self, installed):
        orch, project = installed
        live = project / "CLAUDE.md"
        live.write_text(_live(project).replace("_(Describe the project.)_", "MY OVERVIEW"),
                        encoding="utf-8")
        _bundle(project, orch, update=True)  # reference sidecar
        live.write_text(_live(project).replace("Read CONTEXT_STATE.md.", "MY MANAGED EDIT"),
                        encoding="utf-8")
        _set_template(orch, _template(managed=MANAGED_V2))
        with contextlib.redirect_stderr(io.StringIO()):
            out = project_init.render_claude_md(project, orchestrator_root=orch,
                                                project_name="proj")
        text = _live(project)
        assert "MY OVERVIEW" in _user_part(text)
        assert "## New VCO section" in pt.managed_body(text)
        assert text.count("## Project Overview") == 1
        assert "MY MANAGED EDIT" not in text
        (backup,) = _backups(project)
        assert "MY MANAGED EDIT" in backup.read_text(encoding="utf-8")
        assert out["managed_backups"]
        # The toggle refreshed the reference, so the next update is quiet.
        res = _bundle(project, orch, update=True)
        assert res["templates"]["managed_backups"] == []

    def test_toggle_with_a_changed_user_template_emits_the_row(self, installed):
        orch, project = installed
        _set_template(orch, _template(user=USER_V2))
        with contextlib.redirect_stderr(io.StringIO()):
            out = project_init.render_claude_md(project, orchestrator_root=orch,
                                                project_name="proj")
        assert out["user_section_review"] == ["template_changed"]
        assert CID in _rows(project)
        assert "its users and its limits" not in _live(project)

    def test_toggle_on_a_missing_file_creates_both_parts(self, tmp_path):
        orch = _orch(tmp_path, _template())
        project = tmp_path / "proj"
        project.mkdir()
        project_init.render_claude_md(project, orchestrator_root=orch, project_name="proj")
        text = _live(project)
        assert "## Project Overview" in _user_part(text)
        assert (project / pt.USER_SECTION_STATE_REL).is_file()
