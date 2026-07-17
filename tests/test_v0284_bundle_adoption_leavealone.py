# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.84 PLAN-v0284 D7 (P5 / ruling R2): shipped-file adoption LEAVE-ALONE
battery.

Adoption is surgical. This suite pins every "do NOT adopt" path:
  * regenerated-data file ⇒ keep-regenerated (untouched, no backup).
  * `.disabled/` companion ⇒ skip-disabled.
  * first-install pre-existing file ⇒ skip-existing (no adoption, no backup).
  * user-authored file NOT at a shipped destination ⇒ never touched.
  * BACKUP-WRITE FAILURE (unwritable backups dir) ⇒ NO adoption, preserve +
    deferral as today (never destroy bytes without a captured copy).
  * history-heal + stale-root-heal ⇒ plain `overwrite`, NO backup.
"""
from __future__ import annotations

import json
import os
import platform
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._v0284_bundle_fixtures import bundle_ext, make_fake_orchestrator  # noqa: E402
from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


class AdoptionLeaveAloneTests(unittest.TestCase):
    """Leave-alone battery on a NON-ROOT project (folder ≠ orchestrator_root)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0284-adopt-la-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        make_fake_orchestrator(self.orch)
        assert self.proj.resolve() != self.orch.resolve()  # A3: non-root
        self.ext = bundle_ext()
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )

    def tearDown(self):
        import shutil
        # Restore any perms we tightened so rmtree can clean up.
        for p in self.tmp.rglob("*"):
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _foo(self) -> Path:
        return self.proj / ".claude" / "hooks" / f"foo.{self.ext}"

    def _bump(self, body: str) -> None:
        (self.orch / "templates" / "hooks" / f"foo.{self.ext}").write_text(
            body, encoding="utf-8",
        )

    def _backups_root(self) -> Path:
        return self.proj / ".claude" / "backups" / "bundle-adoptions"

    # ---- skip-existing (first install) — no adoption ----

    def test_first_install_skip_existing_no_adoption_no_backup(self):
        """First-install (update_mode=False) with a pre-existing custom file ⇒
        skip-existing, NOT adoption — no backup dir created."""
        proj2 = self.tmp / "project2"
        proj2.mkdir()
        target = proj2 / ".claude" / "hooks" / f"foo.{self.ext}"
        target.parent.mkdir(parents=True)
        target.write_text("PRE-EXISTING USER FILE\n", encoding="utf-8")
        result = project_init.install_project_bundle(
            proj2, orchestrator_root=self.orch, update_mode=False,
        )
        rel = str(Path(".claude") / "hooks" / f"foo.{self.ext}")
        self.assertIn(rel, result["actions"]["skip-existing"])
        self.assertNotIn(rel, result["actions"]["adopt"])
        self.assertEqual(target.read_text(encoding="utf-8"), "PRE-EXISTING USER FILE\n")
        self.assertFalse(
            (proj2 / ".claude" / "backups" / "bundle-adoptions").exists()
        )

    # ---- keep-regenerated — no adoption, no backup ----

    def test_regenerated_data_file_kept_not_adopted(self):
        """A `regenerated_data` op that diverged ⇒ keep-regenerated (untouched),
        never adopted (no backup)."""
        old_action = project_init._file_action

        def _wrapped(op, target_path, **kw):
            # Force foo to be treated as regenerated data.
            if op.dest_rel.endswith(f"foo.{self.ext}"):
                op = op._replace(regenerated_data=True) if hasattr(op, "_replace") else op
            return old_action(op, target_path, **kw)

        self._foo().write_text("REGENERATED LOCAL CACHE\n", encoding="utf-8")
        self._bump("#!/bin/sh\necho v2\n")
        # `_BundleFileOp` may be a dataclass, not a namedtuple — patch the op's
        # regenerated_data via the enumerate step instead.
        real_enum = project_init._enumerate_bundle_files

        def _enum(*a, **k):
            ops = real_enum(*a, **k)
            for op in ops:
                if op.dest_rel.endswith(f"foo.{self.ext}"):
                    object.__setattr__(op, "regenerated_data", True)
            return ops

        with mock.patch.object(project_init, "_enumerate_bundle_files", _enum):
            result = project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True,
            )
        rel = str(Path(".claude") / "hooks" / f"foo.{self.ext}")
        self.assertIn(rel, result["actions"]["keep-regenerated"])
        self.assertNotIn(rel, result["actions"]["adopt"])
        # Local kept, not refreshed.
        self.assertEqual(self._foo().read_text(encoding="utf-8"), "REGENERATED LOCAL CACHE\n")
        self.assertFalse(self._backups_root().exists())

    # ---- disabled companion — skip-disabled ----

    def test_disabled_agent_companion_skipped_not_adopted(self):
        """An agent disabled via `.claude/agents.disabled/<name>.md` ⇒
        skip-disabled; it is never adopted/backed up (the user's disable choice
        survives). The shared fixture ships `coder.md`, so we use a distinct
        agent name to isolate the disabled-companion classification."""
        # Ship a second agent so it enters the op set with a clean slate.
        agents = self.orch / "templates" / "agents" / "free"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "planner.md").write_text("# Planner\n", encoding="utf-8")
        # User disabled it.
        disabled = self.proj / ".claude" / "agents.disabled"
        disabled.mkdir(parents=True, exist_ok=True)
        (disabled / "planner.md").write_text("# Planner (disabled)\n", encoding="utf-8")

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        rel = str(Path(".claude") / "agents" / "planner.md")
        self.assertIn(rel, result["actions"]["skip-disabled"])
        self.assertNotIn(rel, result["actions"]["adopt"])
        # Enabled-side file was NOT created (disable choice honored).
        self.assertFalse((self.proj / ".claude" / "agents" / "planner.md").exists())
        # Not backed up.
        for p in self._backups_root().rglob("*"):
            self.assertNotEqual(p.name, "planner.md")

    # ---- user-authored file NOT at a shipped destination — untouched ----

    def test_user_file_outside_shipped_set_untouched(self):
        """A user-authored file that is not in `_enumerate_bundle_files` is
        neither classified nor adopted nor backed up."""
        user_file = self.proj / ".claude" / "my_notes.md"
        user_file.write_text("MY PRIVATE NOTES\n", encoding="utf-8")
        self._foo().write_text("EDIT\n", encoding="utf-8")  # a real adoption too
        self._bump("#!/bin/sh\necho v2\n")
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        # Untouched.
        self.assertEqual(user_file.read_text(encoding="utf-8"), "MY PRIVATE NOTES\n")
        # Not in any backup tree.
        for p in self._backups_root().rglob("*"):
            self.assertNotEqual(p.name, "my_notes.md")

    # ---- BACKUP-FAILURE FALLBACK — no adoption, preserve + deferral ----

    def test_backup_failure_falls_back_to_preserve_and_deferral(self):
        """When the backup write raises, the file is NOT adopted: it falls back
        to today's `preserve` + `bundle_user_modified_preserved` deferral, and
        the on-disk bytes are UNCHANGED (never destroyed without a captured
        copy)."""
        old = "MY LOCAL EDIT\n"
        self._foo().write_text(old, encoding="utf-8")
        self._bump("#!/bin/sh\necho v2\n")

        with mock.patch.object(
            project_init, "_backup_bytes_for_adoption",
            side_effect=OSError("disk full"),
        ):
            result = project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True,
            )
        rel = str(Path(".claude") / "hooks" / f"foo.{self.ext}")
        # Fell back to preserve, NOT adopt.
        self.assertIn(rel, result["actions"]["preserve"])
        self.assertNotIn(rel, result["actions"]["adopt"])
        # On-disk bytes untouched (shipped write never ran).
        self.assertEqual(self._foo().read_text(encoding="utf-8"), old)
        # Deferral emitted (the producer survives ONLY for this fallback).
        report = DeferralReport.read(self.proj)
        self.assertTrue(report.has_condition("bundle_user_modified_preserved"))
        # A warning surfaced the fallback.
        self.assertTrue(
            any("adoption backup failed" in w for w in result["warnings"]),
            f"expected a backup-failure warning; got {result['warnings']}",
        )

    @unittest.skipIf(platform.system() == "Windows", "POSIX chmod semantics")
    def test_unwritable_backups_dir_falls_back_to_preserve(self):
        """A real unwritable backups dir (chmod 0o500 on an existing
        `.claude/backups`) forces the backup write to fail ⇒ preserve fallback,
        bytes untouched, deferral emitted."""
        if os.geteuid() == 0:
            self.skipTest("root bypasses POSIX permission bits")
        old = "MY LOCAL EDIT\n"
        self._foo().write_text(old, encoding="utf-8")
        self._bump("#!/bin/sh\necho v2\n")
        # Pre-create .claude/backups as a read-only dir so the mkdir of the
        # `bundle-adoptions/<ts>` subtree fails.
        backups = self.proj / ".claude" / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        os.chmod(backups, stat.S_IRUSR | stat.S_IXUSR)  # r-x, no write
        try:
            result = project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True,
            )
        finally:
            os.chmod(backups, 0o700)
        rel = str(Path(".claude") / "hooks" / f"foo.{self.ext}")
        self.assertIn(rel, result["actions"]["preserve"])
        self.assertNotIn(rel, result["actions"]["adopt"])
        self.assertEqual(self._foo().read_text(encoding="utf-8"), old)
        report = DeferralReport.read(self.proj)
        self.assertTrue(report.has_condition("bundle_user_modified_preserved"))

    # ---- knowledge/** is USER-OWNED state — preserve, never adopt ----

    def test_user_modified_knowledge_node_preserved_not_adopted(self):
        """v0.2.84 PLAN-v0284 D7 (data-safety): a user-modified `knowledge/**`
        KG node is USER-OWNED content — it stays `preserve`, NEVER adopted
        (adopting would DESTROY the user's own knowledge). Pinned alongside
        test_v52_c_kg_as_user_state.py."""
        # Ship a per-project knowledge node (TAG_HIERARCHY.md is allowlisted).
        kdir = self.orch / "templates" / "knowledge"
        kdir.mkdir(parents=True, exist_ok=True)
        (kdir / "TAG_HIERARCHY.md").write_text("# Tags\nshipped-v1\n", encoding="utf-8")
        # First install materializes it.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        node = self.proj / "knowledge" / "TAG_HIERARCHY.md"
        self.assertTrue(node.exists())
        # User edits their KG node; orchestrator bumps the shipped version.
        node.write_text("# Tags\nMY OWN KG CONTENT\n", encoding="utf-8")
        (kdir / "TAG_HIERARCHY.md").write_text("# Tags\nshipped-v2\n", encoding="utf-8")

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        rel = str(Path("knowledge") / "TAG_HIERARCHY.md")
        self.assertIn(rel, result["actions"]["preserve"])
        self.assertNotIn(rel, result["actions"]["adopt"])
        # User's KG content is INTACT on disk (not overwritten, not backed up
        # then replaced — it simply stayed).
        self.assertEqual(node.read_text(encoding="utf-8"), "# Tags\nMY OWN KG CONTENT\n")
        # No adoption backup for a knowledge node.
        if self._backups_root().exists():
            for p in self._backups_root().rglob("*"):
                self.assertNotEqual(p.name, "TAG_HIERARCHY.md")

    # ---- heal paths — overwrite, NO backup ----

    def test_stale_root_heal_overwrites_without_backup(self):
        """A file that round-trips to the source bytes under a DIFFERENT (stale)
        orchestrator root heals via `overwrite` — NO adoption, NO backup.

        The heal only matches when the installed file embeds a baked
        `<old_root>/claude_mcp_servers/...` path (see
        `_stale_orchestrator_root_heal_match`), so the agent template carries
        `{{ORCHESTRATOR_ROOT}}/claude_mcp_servers/.venv/bin/python`.
        """
        agents = self.orch / "templates" / "agents" / "free"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "healme.md").write_text(
            "# HealMe\nVenv: {{ORCHESTRATOR_ROOT}}/claude_mcp_servers/.venv/bin/python\n",
            encoding="utf-8",
        )
        # First install to materialize the substituted agent under THIS root.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        installed = self.proj / ".claude" / "agents" / "healme.md"
        self.assertTrue(installed.exists())
        # Drop its manifest entry so we hit the heal branches (not overwrite).
        m = json.loads((self.proj / ".claude" / ".vco-manifest.json").read_text())
        rel = str(Path(".claude") / "agents" / "healme.md")
        m["files"].pop(rel, None)
        (self.proj / ".claude" / ".vco-manifest.json").write_text(json.dumps(m, indent=2))
        # Move the orchestrator so the installed file references a STALE root
        # (the installed bytes still bake the OLD absolute path).
        new_root = self.tmp / "orchestrator-moved"
        import shutil
        shutil.copytree(self.orch, new_root)
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=new_root, update_mode=True,
        )
        # Healed via overwrite (bytes now reference the new root), never adopt.
        self.assertNotIn(rel, result["actions"]["adopt"])
        self.assertIn(rel, result["actions"]["overwrite"])
        # No backup was taken for a heal (provably VCO bytes).
        healme_backups = [
            p for p in self._backups_root().rglob("*") if p.name == "healme.md"
        ] if self._backups_root().exists() else []
        self.assertEqual(healme_backups, [], "heal path must not create a backup")


if __name__ == "__main__":
    unittest.main()
