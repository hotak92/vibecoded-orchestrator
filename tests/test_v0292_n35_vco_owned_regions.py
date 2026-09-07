# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-15 (N3): a VCO-owned region is never user divergence.

`template_review_pending` was a permanent false positive with three causes,
all of which are "VCO compared its own injected content against a reference
that never had it":

  1. the deferral-reminder block `deferral_report` splices into CLAUDE.md
     whenever a ledger exists — so ANY project carrying ANY deferral read as
     diverged, forever;
  2. the `>>>VCO_MANAGED>>>` marker LINES `merge_managed_region` wraps around
     the render when VCO CREATES a project's CLAUDE.md, while the reference
     sidecar is written UNwrapped — so a pristine, never-human-touched project
     diverged on its SECOND bundle run, by construction;
  3. on an orchestrator root, a comparison against the wrong document
     entirely: the root's CLAUDE.md is rendered from
     `ORCHESTRATOR-CLAUDE.md.template` while `_PROJECT_LEVEL_TEMPLATES` walks
     the PROJECT `CLAUDE.md.template`.

BOTH BRANCHES are asserted throughout: the nudge is a user-facing feature and
must still fire for a genuine user edit. Tests named `..._still_diverges`
(and `test_a_user_edit_*`) are the LEAVE-ALONE half — they pass before AND
after the fix, and their job is to prove the feature was not neutered.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import deferral_report as dr  # noqa: E402
from vco_lib import project_init as pi  # noqa: E402
from vco_lib.deferral_report import (  # noqa: E402
    MANAGED_REGION_CLOSE,
    MANAGED_REGION_OPEN,
    strip_vco_owned_regions,
)

REF_REL = Path(".claude") / "context" / "templates" / "CLAUDE.md.reference.md"


# ══════════════════════════════════════════════════════════════════════
# The pure stripper
# ══════════════════════════════════════════════════════════════════════
class TestStripVcoOwnedRegions:
    def test_reminder_block_removed(self):
        body = "# Doc\n\nprose\n"
        with_block = "# Doc\n\n" + dr._reminder_block() + "\nprose\n"
        assert strip_vco_owned_regions(with_block) == body

    def test_managed_marker_lines_removed_body_kept(self):
        text = f"{MANAGED_REGION_OPEN}\nbody a\nbody b\n{MANAGED_REGION_CLOSE}\n"
        assert strip_vco_owned_regions(text) == "body a\nbody b\n"

    def test_auto_region_removed_whole(self):
        text = (
            "<!-- BEGIN: AUTO (rendered by install.py from "
            "templates/ORCHESTRATOR-CLAUDE.md.template) -->\n"
            "orchestrator body\n"
            "<!-- END: AUTO -->\n"
            "user tail\n"
        )
        assert strip_vco_owned_regions(text) == "user tail\n"

    def test_all_three_families_at_once(self):
        text = (
            f"{MANAGED_REGION_OPEN}\n"
            "rendered body\n"
            f"{MANAGED_REGION_CLOSE}\n"
            "\n"
            + dr._reminder_block()
            + "\n"
            "<!-- BEGIN: AUTO -->\nauto\n<!-- END: AUTO -->\n"
            "\n"
            "my own notes\n"
        )
        out = strip_vco_owned_regions(text)
        assert "rendered body" in out
        assert "my own notes" in out
        for gone in (
            MANAGED_REGION_OPEN, MANAGED_REGION_CLOSE,
            dr._REMINDER_BEGIN, dr._REMINDER_END,
            "<!-- BEGIN: AUTO", "<!-- END: AUTO -->", "auto",
        ):
            assert gone not in out, gone

    def test_idempotent(self):
        text = (
            f"{MANAGED_REGION_OPEN}\nbody\n{MANAGED_REGION_CLOSE}\n\n"
            + dr._reminder_block()
        )
        once = strip_vco_owned_regions(text)
        assert strip_vco_owned_regions(once) == once

    def test_no_regions_is_identity(self):
        text = "# Plain\n\nnothing owned here.\n"
        assert strip_vco_owned_regions(text) is not None
        assert strip_vco_owned_regions(text) == text

    def test_a_marker_quoted_inside_a_code_fence_is_left_alone(self):
        """LEAVE-ALONE. VCO's own shareable CLAUDE.md documents these markers
        inside fences; a fence-blind stripper would delete the user's prose
        between a quoted marker and a later real one."""
        text = (
            "# Docs\n\n"
            "```\n"
            f"{MANAGED_REGION_OPEN}\n"
            "<!-- BEGIN: AUTO -->\n"
            f"{dr._REMINDER_BEGIN}\n"
            "```\n\n"
            "real prose\n"
        )
        assert strip_vco_owned_regions(text) == text

    def test_crlf_document_keeps_its_body(self):
        """Tri-OS: a Windows checkout is CRLF. The stripper must find the
        markers and must not eat the body."""
        text = (
            f"{MANAGED_REGION_OPEN}\r\nbody a\r\nbody b\r\n"
            f"{MANAGED_REGION_CLOSE}\r\n"
        )
        out = strip_vco_owned_regions(text)
        assert MANAGED_REGION_OPEN not in out and MANAGED_REGION_CLOSE not in out
        assert "body a" in out and "body b" in out


# ══════════════════════════════════════════════════════════════════════
# The divergence check — fixture projects
# ══════════════════════════════════════════════════════════════════════
def _fresh_project(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    pi._install_project_level_templates(
        folder, orchestrator_root=REPO_ROOT, project_name="Fixture",
        dry_run=False,
    )
    return folder


def _rerun(folder: Path) -> list:
    return pi._install_project_level_templates(
        folder, orchestrator_root=REPO_ROOT, project_name="Fixture",
        dry_run=False,
    )["diverged"]


class TestTheProjectAxis:
    def test_a_pristine_vco_created_project_does_not_diverge(self, tmp_path):
        """RED before the fix: the managed-region marker LINES alone made a
        project VCO itself created read as user-divergent on run 2."""
        folder = _fresh_project(tmp_path)
        assert _rerun(folder) == []

    def test_vcos_own_reminder_block_does_not_diverge(self, tmp_path):
        """RED before the fix. This is the large already-damaged population:
        every project carrying any live deferral at all."""
        folder = _fresh_project(tmp_path)
        dr._ensure_claude_md_reminder(folder)
        assert dr._REMINDER_BEGIN in (folder / "CLAUDE.md").read_text(
            encoding="utf-8")
        assert _rerun(folder) == []

    def test_a_user_edit_outside_the_region_still_diverges(self, tmp_path):
        """LEAVE-ALONE: green both ways. The nudge is a feature."""
        folder = _fresh_project(tmp_path)
        cm = folder / "CLAUDE.md"
        cm.write_text(cm.read_text(encoding="utf-8")
                      + "\n## My section\nhand written\n", encoding="utf-8")
        assert _rerun(folder) == ["CLAUDE.md"]

    def test_a_user_edit_inside_the_managed_region_still_diverges(self, tmp_path):
        """LEAVE-ALONE, and the WP-15/WP-16 split in miniature: only the
        MARKER LINES are stripped, never the body, so an edit between them is
        still seen."""
        folder = _fresh_project(tmp_path)
        cm = folder / "CLAUDE.md"
        cm.write_text(
            cm.read_text(encoding="utf-8").replace(
                MANAGED_REGION_CLOSE, "my line\n" + MANAGED_REGION_CLOSE),
            encoding="utf-8",
        )
        assert _rerun(folder) == ["CLAUDE.md"]

    def test_a_user_edit_to_context_state_still_diverges(self, tmp_path):
        """LEAVE-ALONE: the other two entries are untouched by this change."""
        folder = _fresh_project(tmp_path)
        (folder / ".claude" / "CONTEXT_STATE.md").write_text(
            "totally different\n", encoding="utf-8")
        assert _rerun(folder) == [".claude/CONTEXT_STATE.md"]

    def test_crlf_live_file_does_not_diverge(self, tmp_path):
        """Tri-OS: a Windows checkout of a pristine project. The whitespace
        normaliser must not be fooled by line endings, and neither must the
        region strip that now runs before it."""
        folder = _fresh_project(tmp_path)
        cm = folder / "CLAUDE.md"
        crlf = cm.read_text(encoding="utf-8").replace("\n", "\r\n")
        cm.write_bytes(crlf.encode("utf-8"))
        assert _rerun(folder) == []


# ══════════════════════════════════════════════════════════════════════
# The orchestrator-root axis
# ══════════════════════════════════════════════════════════════════════
def _root_fixture(tmp_path: Path) -> tuple:
    root = tmp_path / "orch"
    (root / "templates").mkdir(parents=True)
    (root / ".claude" / "context" / "templates").mkdir(parents=True)
    for name in ("CLAUDE.md.template", "CONTEXT_STATE.md.template",
                 "MEMORY.md.template"):
        (root / "templates" / name).write_text(
            f"# {{{{PROJECT_NAME}}}} — {name}\nbody\n", encoding="utf-8")
    # The root's LIVE CLAUDE.md is the ORCHESTRATOR render, AUTO-wrapped.
    (root / "CLAUDE.md").write_text(
        "<!-- BEGIN: AUTO (rendered by install.py from "
        "templates/ORCHESTRATOR-CLAUDE.md.template) -->\n"
        "# Orchestrator instructions\nmuch longer document\n"
        "<!-- END: AUTO -->\n",
        encoding="utf-8",
    )
    (root / ".claude" / "CONTEXT_STATE.md").write_text(
        "# Fixture — CONTEXT_STATE.md.template\nbody\n", encoding="utf-8")
    (root / "MEMORY.md").write_text(
        "# Fixture — MEMORY.md.template\nbody\n", encoding="utf-8")
    stale = root / REF_REL
    stale.write_text("# Fixture — CLAUDE.md.template\nbody\n", encoding="utf-8")
    return root, stale


class TestTheOrchestratorRootAxis:
    def test_root_claude_md_is_not_compared(self, tmp_path):
        """RED before the fix: every orchestrator root was permanently
        divergent because an 889-line orchestrator render was compared
        against a 158-line PROJECT render."""
        root, _ = _root_fixture(tmp_path)
        out = pi._install_project_level_templates(
            root, orchestrator_root=root, project_name="Fixture",
            dry_run=False,
        )
        assert "CLAUDE.md" not in out["diverged"]

    def test_root_stale_sidecar_is_removed(self, tmp_path):
        """The already-damaged clearing mechanism for the root population:
        the sidecar is a VCO-GENERATED artifact under a VCO-owned directory,
        regenerated by every bundle run — not user data."""
        root, stale = _root_fixture(tmp_path)
        assert stale.is_file()
        pi._install_project_level_templates(
            root, orchestrator_root=root, project_name="Fixture",
            dry_run=False,
        )
        assert not stale.exists()

    def test_root_live_claude_md_is_never_touched(self, tmp_path):
        """Never destroy user data: ONLY the sidecar goes."""
        root, _ = _root_fixture(tmp_path)
        before = (root / "CLAUDE.md").read_bytes()
        pi._install_project_level_templates(
            root, orchestrator_root=root, project_name="Fixture",
            dry_run=False,
        )
        assert (root / "CLAUDE.md").read_bytes() == before

    def test_root_dry_run_removes_nothing(self, tmp_path):
        root, stale = _root_fixture(tmp_path)
        pi._install_project_level_templates(
            root, orchestrator_root=root, project_name="Fixture",
            dry_run=True,
        )
        assert stale.is_file()

    def test_root_still_compares_the_other_two(self, tmp_path):
        """LEAVE-ALONE: CONTEXT_STATE.md and MEMORY.md ARE the project-shaped
        files on the root, so the exclusion is scoped to CLAUDE.md."""
        root, _ = _root_fixture(tmp_path)
        (root / ".claude" / "CONTEXT_STATE.md").write_text(
            "hand edited\n", encoding="utf-8")
        out = pi._install_project_level_templates(
            root, orchestrator_root=root, project_name="Fixture",
            dry_run=False,
        )
        assert out["diverged"] == [".claude/CONTEXT_STATE.md"]
        assert any("CONTEXT_STATE.md.reference.md" in p
                   for p in out["reference_written"])
        assert not any("CLAUDE.md.reference.md" in p
                       for p in out["reference_written"])

    def test_root_install_through_the_real_engine_clears_the_damage(self, tmp_path):
        """THE already-damaged clearing mechanism, end to end.

        Delivery check 2 (one engine): a root install reaches this code the
        same way a project does — `self_install.run_root_bundle_install` drives
        the `install-bundle` CLI and no `--skip-kind` covers project-level
        templates. So the ordinary "Update orchestrator" run IS the repair for
        every existing root, with no user step."""
        root, stale = _root_fixture(tmp_path)
        (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
        res = pi.install_project_bundle(
            root, orchestrator_root=root, update_mode=True)
        assert "CLAUDE.md" not in res["templates"]["diverged"]
        assert not stale.exists()
        # The live root CLAUDE.md keeps its AUTO body verbatim. (This run also
        # spliced a reminder block into it, because the bundle emitted a
        # ledger entry — the very injection that used to be counted as user
        # divergence, here demonstrably not counted.)
        live = (root / "CLAUDE.md").read_text(encoding="utf-8")
        assert "<!-- BEGIN: AUTO" in live and "much longer document" in live

    def test_a_non_root_project_is_still_compared(self, tmp_path):
        """LEAVE-ALONE: the exclusion keys on PATH identity via the EXISTING
        `_is_root_bundle_target` home, so a project that merely LOOKS
        orchestrator-shaped is unaffected."""
        root, _ = _root_fixture(tmp_path)
        other = tmp_path / "someone-elses-project"
        (other / ".claude").mkdir(parents=True)
        (other / "CLAUDE.md").write_text("# totally bespoke\n", encoding="utf-8")
        out = pi._install_project_level_templates(
            other, orchestrator_root=root, project_name="Fixture",
            dry_run=False,
        )
        assert out["diverged"] == ["CLAUDE.md"]


# ══════════════════════════════════════════════════════════════════════
# Promise / mechanism pins
# ══════════════════════════════════════════════════════════════════════
class TestMechanismPins:
    def test_normalise_for_diff_stays_whitespace_only(self):
        """The region strip is a SEPARATE step on purpose. If a future editor
        folds it into the normaliser, that normaliser stops being lossless and
        every other caller inherits the loss."""
        text = f"{MANAGED_REGION_OPEN}\nkeep\n{MANAGED_REGION_CLOSE}\n"
        assert MANAGED_REGION_OPEN in pi._normalise_for_diff(text)

    def test_there_is_one_reminder_span_finder(self):
        """`_find_all_reminder_marker_spans` is a binding of the ONE scanner,
        not a second implementation."""
        import inspect
        src = inspect.getsource(dr._find_all_reminder_marker_spans)
        assert "_find_marker_spans(" in src
        assert "splitlines" not in src

    def test_the_mutating_stripper_never_removes_auto_or_managed(self):
        """HAZARD PIN. `strip_vco_owned_regions` is compare-only; the file
        MUTATOR must keep touching the reminder pair alone, or the next
        install would delete the orchestrator root's whole CLAUDE.md body."""
        text = (
            "<!-- BEGIN: AUTO -->\nroot body\n<!-- END: AUTO -->\n\n"
            + dr._reminder_block()
        )
        out = dr._strip_reminder_from_claude_md(text)
        assert "root body" in out
        assert "<!-- BEGIN: AUTO -->" in out
        assert dr._REMINDER_BEGIN not in out

    @pytest.mark.parametrize("name", ["MANAGED_REGION_OPEN", "MANAGED_REGION_CLOSE"])
    def test_managed_markers_have_one_home(self, name):
        assert getattr(pi, name) is getattr(dr, name)
