# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 — SETUP-ONLY blocks must stay removed across updates, and remind.

The three ``<!-- BEGIN: SETUP-ONLY -->`` blocks ship INSIDE the AUTO-rendered
region of ``templates/ORCHESTRATOR-CLAUDE.md.template``, so before this fix a
removal by ``cleanup-setup-sections.py`` was reverted by every update and
nothing recorded that the user had already acted. What is pinned here:

* the RENDERER omits a block whose content hash is acknowledged (removal
  survives re-render), keeps an unacknowledged one (fresh install), and renders
  a CHANGED block again (a new setup step re-arms the reminder);
* the CLEANUP SCRIPT records the acknowledgement atomically and clears the
  deferral row, and is idempotent;
* a missing or corrupt acknowledgement file is treated as "nothing
  acknowledged" (blocks re-render) without crashing;
* the clear PROBE agrees with the renderer's own rule.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from unittest import mock  # noqa: E402

from vco_lib import gateway_ensure  # noqa: E402
from vco_lib import project_templates as pt  # noqa: E402
from vco_lib import rendered_root_files as rrf  # noqa: E402
from vco_lib import setup_sections as ss  # noqa: E402
from vco_lib.deferral_probes import ProbeContext  # noqa: E402
from vco_lib.deferral_probes import (  # noqa: E402
    first_run_setup_sections_still_pending,
)
from vco_lib.deferral_report import DeferralReport  # noqa: E402

CLEANUP_SCRIPT = REPO_ROOT / "templates" / "scripts" / "cleanup-setup-sections.py"

BLOCK_ONE = (
    "<!-- BEGIN: SETUP-ONLY (block one) -->\n"
    "First-run setup step ONE for {{ORCHESTRATOR_ROOT}}.\n"
    "<!-- END: SETUP-ONLY -->\n"
)
BLOCK_TWO = (
    "<!-- BEGIN: SETUP-ONLY (block two) -->\n"
    "Verifying the installation of {{ORCHESTRATOR_ROOT}}.\n"
    "<!-- END: SETUP-ONLY -->\n"
)


def _template(block_one: str = BLOCK_ONE, block_two: str = BLOCK_TWO) -> str:
    return (
        "<!-- BEGIN: AUTO (rendered) -->\n"
        "## Intro\n"
        "\n"
        + block_one
        + "\n"
        + block_two
        + "\n"
        "## Tail\n"
        "<!-- END: AUTO -->\n"
    )


def _load_cleanup_module():
    spec = importlib.util.spec_from_file_location(
        "vco_cleanup_setup_sections", CLEANUP_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="vco-setup-")
        self.root = Path(self._tmp.name)
        (self.root / "templates").mkdir(parents=True)
        self.template = self.root / "templates" / "ORCHESTRATOR-CLAUDE.md.template"
        self.template.write_text(_template(), encoding="utf-8")
        self.entry = rrf.RenderedRootFile(
            path="CLAUDE.md",
            template="templates/ORCHESTRATOR-CLAUDE.md.template",
            begin_marker="<!-- BEGIN: AUTO",
            end_marker="<!-- END: AUTO -->",
            substitutions=("ORCHESTRATOR_ROOT",),
        )
        self.make_managed()
        self.addCleanup(self._tmp.cleanup)

    def make_managed(self) -> None:
        """Install the remedy the reminder names (install.py step 5b does this
        before step 4c), which is how a root is recognised as managed."""
        target = self.root / ss.CLEANUP_SCRIPT_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CLEANUP_SCRIPT, target)

    def render(self) -> str:
        with contextlib.redirect_stderr(io.StringIO()):
            rrf.render_entry(self.root, self.entry)
        target = self.root / "CLAUDE.md"
        return target.read_text(encoding="utf-8") if target.is_file() else ""

    def claude_md(self) -> str:
        return (self.root / "CLAUDE.md").read_text(encoding="utf-8")

    def has_row(self) -> bool:
        return DeferralReport.read(self.root).has_condition(ss.CONDITION_ID)

    def run_cleanup(self) -> int:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = _load_cleanup_module().main(["--root", str(self.root)])
        return rc


class TestRendererLifecycle(_Case):
    def test_fresh_render_renders_all_blocks_and_emits_the_deferral(self) -> None:
        body = self.render()
        self.assertIn("block one", body)
        self.assertIn("block two", body)
        self.assertIn(str(self.root), body, "the placeholder inside a block renders")
        self.assertTrue(self.has_row(), "a fresh install must remind the agent")
        entry = DeferralReport.read(self.root).entry_for(ss.CONDITION_ID)
        self.assertEqual(entry.resolved_disposition, "action_required")
        self.assertIn("For your Claude assistant", entry.why_deferred)
        self.assertIn("cleanup-setup-sections.py", entry.command_to_apply)

    def test_acknowledged_block_stays_removed_across_a_rerender(self) -> None:
        self.render()
        self.assertEqual(self.run_cleanup(), 0)
        self.assertNotIn("block one", self.claude_md())
        self.assertNotIn("block two", self.claude_md())

        # The update re-renders the whole AUTO block from the template.
        body = self.render()
        self.assertNotIn("block one", body, "an acked block must not come back")
        self.assertNotIn("block two", body)
        self.assertFalse(self.has_row(), "all blocks acknowledged ⇒ the row clears")

    def test_unmanaged_tree_makes_no_pending_claim(self) -> None:
        """A bare clone (no bundle ⇒ no remedy to run) must not claim work.

        The v0.2.92 invariant ("a fresh render makes no pending claim") still
        holds for a tree that merely carries the template; the blocks render,
        but no deferral row is raised because the cleanup script the reminder
        names is not installed there.
        """
        (self.root / ss.CLEANUP_SCRIPT_REL).unlink()
        body = self.render()
        self.assertIn("block one", body)
        self.assertIn("block two", body)
        self.assertFalse(self.has_row(), "no remedy ⇒ no claim")
        self.assertFalse(ss.is_managed_install_root(self.root))

    def test_changed_block_content_re_renders_and_re_arms_the_reminder(self) -> None:
        self.render()
        self.assertEqual(self.run_cleanup(), 0)
        self.assertNotIn("block one", self.claude_md())

        self.template.write_text(
            _template(block_one=BLOCK_ONE.replace("step ONE", "step ONE (revised)")),
            encoding="utf-8",
        )
        body = self.render()
        self.assertIn("block one", body, "a CHANGED block renders again")
        self.assertIn("revised", body)
        self.assertNotIn("block two", body, "the unchanged block stays removed")
        self.assertTrue(self.has_row(), "a new setup step re-arms the reminder")


class TestCleanupScript(_Case):
    def test_removes_blocks_records_ack_and_clears_the_deferral(self) -> None:
        self.render()
        self.assertTrue(self.has_row())
        self.assertEqual(self.run_cleanup(), 0)
        body = self.claude_md()
        self.assertNotIn("SETUP-ONLY", body)
        self.assertIn("## Intro", body, "non-setup content is preserved")
        self.assertIn("## Tail", body)
        ack = json.loads(ss.ack_path(self.root).read_text(encoding="utf-8"))
        self.assertEqual(ack["format_version"], ss.ACK_FORMAT_VERSION)
        self.assertEqual(len(ack["acknowledged"]), 2)
        self.assertFalse(self.has_row(), "running the script clears the deferral")

    def test_second_run_is_a_noop(self) -> None:
        self.render()
        self.assertEqual(self.run_cleanup(), 0)
        first = self.claude_md()
        self.assertEqual(self.run_cleanup(), 0)
        self.assertEqual(self.claude_md(), first, "idempotent second run")

    def test_ack_write_failure_leaves_the_file_untouched(self) -> None:
        self.render()
        original = self.claude_md()
        # Make the ack unwritable: ack path's parent becomes a FILE, so
        # mkdir/atomic write fails.
        state = self.root / ".claude" / "state"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("not a directory\n", encoding="utf-8")
        self.assertEqual(self.run_cleanup(), 1, "an un-recordable removal must fail")
        self.assertEqual(self.claude_md(), original, "nothing is half-applied")


class TestAcknowledgementRobustness(_Case):
    def test_missing_ack_treats_every_block_as_unacknowledged(self) -> None:
        self.render()
        self.assertEqual(ss.acknowledged_hashes(self.root), frozenset())
        self.assertEqual(len(ss.pending_rendered_blocks(self.root) or ()), 2)

    def test_corrupt_ack_is_treated_as_unacknowledged_without_crashing(self) -> None:
        for junk in ('{"acknowledged": "nope"}', "not json at all", "[1,2,3]", ""):
            ss.ack_path(self.root).parent.mkdir(parents=True, exist_ok=True)
            ss.ack_path(self.root).write_text(junk, encoding="utf-8")
            self.assertEqual(ss.acknowledged_hashes(self.root), frozenset())
            body = self.render()
            self.assertIn("block one", body, f"junk {junk!r} must not suppress a block")
            self.assertIn("block two", body)

    def test_non_string_ack_entries_are_ignored(self) -> None:
        ss.ack_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        ss.ack_path(self.root).write_text(
            json.dumps({"format_version": 1, "acknowledged": [123, None, "abc", ""]}),
            encoding="utf-8",
        )
        self.assertEqual(ss.acknowledged_hashes(self.root), frozenset({"abc"}))


class TestClearProbe(_Case):
    def test_probe_reports_pending_then_clears(self) -> None:
        self.render()
        ctx = ProbeContext(folder=self.root)
        self.assertIs(first_run_setup_sections_still_pending(ctx), True)
        self.assertEqual(self.run_cleanup(), 0)
        self.assertIs(first_run_setup_sections_still_pending(ctx), False)

    def test_probe_is_false_when_no_block_is_rendered(self) -> None:
        self.template.write_text(
            _template(block_one="", block_two=""), encoding="utf-8"
        )
        self.render()
        ctx = ProbeContext(folder=self.root)
        self.assertIs(first_run_setup_sections_still_pending(ctx), False)

    def test_probe_returns_none_when_it_cannot_look(self) -> None:
        # A directory where CLAUDE.md should be → unreadable, not "over".
        (self.root / "CLAUDE.md").mkdir()
        ctx = ProbeContext(folder=self.root)
        self.assertIsNone(first_run_setup_sections_still_pending(ctx))


USER_TEMPLATE = REPO_ROOT / "templates" / "CLAUDE.md.template"
SCOPING_HEADING = "## First session: offer to scope agents and skills"


class _ProjectCase(unittest.TestCase):
    """12a (v0.2.101 wave 2): the SAME lifecycle on a PROJECT's CLAUDE.md.

    The scoping block ships inside the managed region of
    ``templates/CLAUDE.md.template``; the project render path must honour the
    acknowledgement exactly like the root path (one home:
    ``setup_sections.apply_to_render``), emit ``first_run_setup_pending`` with
    the PROJECT prose (template name + root label), and clear/re-arm with the
    same rules.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="vco-setup-proj-")
        self.tmp = Path(self._tmp.name)
        self.orch = self.tmp / "orch"
        (self.orch / "templates").mkdir(parents=True)
        (self.orch / "vct-module.json").write_text("{}\n", encoding="utf-8")
        (self.orch / "templates" / "CLAUDE.md.template").write_bytes(
            USER_TEMPLATE.read_bytes())
        self.project = self.tmp / "proj"
        self.project.mkdir()
        # A bundled project IS a managed install root: the remedy the row
        # names ships into <project>/.claude/scripts/.
        script = self.project / ss.CLEANUP_SCRIPT_REL
        script.parent.mkdir(parents=True)
        shutil.copy2(CLEANUP_SCRIPT, script)
        self.addCleanup(self._tmp.cleanup)

    def render(self) -> dict:
        # Hermetic machine state: no gateway (the scoping block is module-
        # independent — this only keeps the resolver's probes off the machine).
        signal = gateway_ensure.MachineGatewaySignal(
            configured=False, registration="test", panel="test", reason="test")
        with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(
                gateway_ensure, "machine_gateway_signal", lambda *a, **k: signal):
            return pt.install_project_level_templates(
                self.project, orchestrator_root=self.orch,
                project_name="Proj", dry_run=False)

    def claude_md(self) -> str:
        return (self.project / "CLAUDE.md").read_text(encoding="utf-8")

    def has_row(self) -> bool:
        return DeferralReport.read(self.project).has_condition(ss.CONDITION_ID)

    def run_cleanup(self) -> int:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = _load_cleanup_module().main(["--root", str(self.project)])
        return rc


class TestProjectFolderLifecycle(_ProjectCase):
    def test_fresh_project_render_carries_the_block_and_the_project_row(
            self) -> None:
        self.render()
        self.assertIn(SCOPING_HEADING, self.claude_md())
        self.assertTrue(self.has_row(), "a fresh project must remind the agent")
        entry = DeferralReport.read(self.project).entry_for(ss.CONDITION_ID)
        # The parametrised prose names the PROJECT template and root label.
        self.assertIn("templates/CLAUDE.md.template", entry.detected)
        self.assertIn("this project's root", entry.why_deferred)
        self.assertIn("For your Claude assistant", entry.why_deferred)
        self.assertIn("cleanup-setup-sections.py", entry.command_to_apply)
        self.assertIn(str(self.project), entry.command_to_apply)

    def test_cleanup_removal_survives_the_rerender_and_clears_the_row(
            self) -> None:
        self.render()
        self.assertEqual(self.run_cleanup(), 0)
        self.assertNotIn(SCOPING_HEADING, self.claude_md())
        # The bundle update re-renders the managed body from the template —
        # the acknowledged block must not come back, and the row clears.
        self.render()
        self.assertNotIn(SCOPING_HEADING, self.claude_md())
        self.assertFalse(self.has_row(), "all blocks acknowledged ⇒ row clears")

    def test_changed_block_re_renders_and_re_arms_on_a_project(self) -> None:
        self.render()
        self.assertEqual(self.run_cleanup(), 0)
        self.assertNotIn(SCOPING_HEADING, self.claude_md())
        tpl = self.orch / "templates" / "CLAUDE.md.template"
        tpl.write_text(
            tpl.read_text(encoding="utf-8").replace(
                SCOPING_HEADING, SCOPING_HEADING + " (revised)", 1),
            encoding="utf-8")
        self.render()
        self.assertIn(SCOPING_HEADING, self.claude_md(), "a CHANGED block renders again")
        self.assertIn("revised", self.claude_md())
        self.assertTrue(self.has_row(), "a changed block re-arms the reminder")


class TestBuildEntryParametrisation(_ProjectCase):
    def test_project_call_names_the_project_template_and_root(self) -> None:
        blocks = ss.find_blocks(BLOCK_ONE)
        entry = ss.build_entry(
            self.project, blocks,
            template_name=ss.PROJECT_TEMPLATE_NAME,
            root_label=ss.PROJECT_ROOT_LABEL)
        self.assertIn(ss.PROJECT_TEMPLATE_NAME, entry.detected)
        self.assertIn("this project's root", entry.why_deferred)
        self.assertNotIn(ss.ROOT_TEMPLATE_NAME, entry.detected)

    def test_default_call_keeps_the_root_prose(self) -> None:
        blocks = ss.find_blocks(BLOCK_ONE)
        entry = ss.build_entry(self.project, blocks)
        self.assertIn(ss.ROOT_TEMPLATE_NAME, entry.detected)
        self.assertIn("the orchestrator root", entry.why_deferred)


class TestMarkerRule(unittest.TestCase):
    def test_strip_consumes_one_blank_separator(self) -> None:
        text = "a\n" + BLOCK_ONE + "\nb\n"
        cleaned, removed = ss.strip_blocks(text, lambda _b: True)
        self.assertEqual(len(removed), 1)
        self.assertEqual(cleaned, "a\nb\n")

    def test_unmatched_marker_raises(self) -> None:
        with self.assertRaises(ValueError):
            ss.find_blocks("<!-- BEGIN: SETUP-ONLY (x) -->\nno end\n")
        with self.assertRaises(ValueError):
            ss.find_blocks("<!-- END: SETUP-ONLY -->\n")

    def test_hash_is_over_the_exact_region(self) -> None:
        block = ss.find_blocks(BLOCK_ONE)[0]
        self.assertEqual(
            block.sha256, hashlib.sha256(BLOCK_ONE.encode("utf-8")).hexdigest()
        )


if __name__ == "__main__":
    unittest.main()