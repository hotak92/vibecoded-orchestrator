"""v0.2.24 §A0 audit (2026-05-22): tests for `install-bundle --update`
when the orchestrator stops shipping a file the user has modified.

Three audit cases per the design doc:

1. User-modified file DELETED upstream → must PRESERVE on disk + emit
   `bundle_user_modified_deletion_preserved` deferral entry.
2. Template references now-deleted file → no fix needed; just doesn't
   appear in `ops`. We verify by inspection (a no-op test).
3. User-added file conflicts with new-shipped path → existing
   `_file_action` flow already emits `bundle_user_modified_preserved`.
   We re-verify the contract holds for the orphan-detection commit.

All tests run fully offline — no Weaviate, no network, no subprocess
fan-out — by stubbing the bundle enumeration via the fake-orchestrator
fixture inherited from `test_install_bundle.py::_make_fake_orchestrator`.
"""

from __future__ import annotations

import json
import platform
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402

# Reuse the fake-orchestrator helper from the existing bundle test
# suite. Keeps the fixture single-source-of-truth — if it changes,
# orphan tests pick up the change automatically.
from tests.test_install_bundle import _make_fake_orchestrator  # noqa: E402


# ---------------------------------------------------------------------------
# Audit case #1: user-modified file DELETED upstream
# ---------------------------------------------------------------------------


class OrphanDetectionTests(unittest.TestCase):
    """Verify the v0.2.24 §A0 orphan-detection contract:
    - Files in prior manifest that are NOT in the new ops AND match
      the prior shipped hash → SAFE-DELETE on disk + drop from manifest.
    - Files in prior manifest that are NOT in the new ops AND DIFFER
      from the prior shipped hash → PRESERVE on disk + emit
      `bundle_user_modified_deletion_preserved` deferral.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-bundle-orphan-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)

        # First install — produces a manifest with the full shipped set.
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        self.assertEqual(result["errors"], [], f"first install failed: {result}")

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _foo_hook_path(self) -> Path:
        ext = "ps1" if platform.system() == "Windows" else "sh"
        return self.proj / ".claude" / "hooks" / f"foo.{ext}"

    def _foo_hook_template(self) -> Path:
        ext = "ps1" if platform.system() == "Windows" else "sh"
        return self.orch / "templates" / "hooks" / f"foo.{ext}"

    def _delete_foo_from_orchestrator(self) -> None:
        """Simulate "upstream removed this file": delete both .sh and
        .ps1 variants so `_enumerate_bundle_files` skips them on the
        next pass."""
        for ext in ("sh", "ps1"):
            tmpl = self.orch / "templates" / "hooks" / f"foo.{ext}"
            if tmpl.exists():
                tmpl.unlink()

    def test_orphan_user_untouched_is_safe_deleted(self):
        """When the orchestrator stops shipping `foo.sh` AND the user
        never edited their installed copy (hash matches prior shipped),
        the orphan-detection logic should SAFE-DELETE the file from
        disk. No deferral — the file was pure VCO content and is
        unambiguously safe to remove."""
        installed = self._foo_hook_path()
        self.assertTrue(installed.exists(), "first install should have written foo")
        # User did NOT modify. Hash on disk = prior shipped hash.

        # Upstream removes foo.sh / foo.ps1.
        self._delete_foo_from_orchestrator()

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        ext = "ps1" if platform.system() == "Windows" else "sh"
        rel = str(Path(".claude") / "hooks" / f"foo.{ext}")

        # Action recorded as orphan-deleted.
        self.assertIn(rel, result["actions"]["orphan-deleted"],
                      f"expected {rel} in orphan-deleted: {result['actions']}")
        # File is gone from disk.
        self.assertFalse(installed.exists(), "foo should have been deleted")
        # Manifest no longer carries the entry.
        manifest = json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(rel, manifest["files"],
                         "manifest should drop the orphan entry")
        # No deferral entry — safe delete is silent.
        report = DeferralReport.read(self.proj)
        self.assertFalse(report.has_condition("bundle_user_modified_deletion_preserved"))

    def test_orphan_user_modified_is_preserved_with_deferral(self):
        """When the orchestrator stops shipping `foo.sh` AND the user
        edited their installed copy (hash differs from prior shipped),
        the orphan-detection logic MUST preserve the file on disk and
        emit `bundle_user_modified_deletion_preserved` so the user
        knows VCO no longer manages it."""
        installed = self._foo_hook_path()
        # User modifies their installed copy.
        installed.write_text("# USER CUSTOM CONTENT\n", encoding="utf-8")

        # Upstream removes foo.sh / foo.ps1.
        self._delete_foo_from_orchestrator()

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        ext = "ps1" if platform.system() == "Windows" else "sh"
        rel = str(Path(".claude") / "hooks" / f"foo.{ext}")

        # Action recorded as orphan-preserved.
        self.assertIn(rel, result["actions"]["orphan-preserved"],
                      f"expected {rel} in orphan-preserved: {result['actions']}")
        # File still on disk WITH user content.
        self.assertTrue(installed.exists(), "user-modified orphan should be preserved")
        self.assertEqual(
            installed.read_text(encoding="utf-8"), "# USER CUSTOM CONTENT\n",
            "user content must be untouched",
        )
        # Manifest still carries the entry (so a future re-ship can recognize the baseline).
        manifest = json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )
        self.assertIn(rel, manifest["files"],
                      "manifest should preserve orphan entry for user-modified files")
        # Deferral entry emitted.
        report = DeferralReport.read(self.proj)
        self.assertTrue(
            report.has_condition("bundle_user_modified_deletion_preserved"),
            f"missing deferral; entries: {[e.condition_id for e in report.entries]}",
        )
        # Deferral lists the affected file in the on-disk markdown body
        # (the DeferralReport parser captures `detected` as a single line
        # of the markdown, but the rendered file contains the full multi-
        # line file bullet list).
        deferred = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertTrue(deferred.exists(), "UPDATE_DEFERRED.md should be written")
        body = deferred.read_text(encoding="utf-8")
        self.assertIn(rel, body, "deferral body should list the orphan file")
        self.assertIn(
            "## bundle_user_modified_deletion_preserved",
            body,
            "deferral body should carry the condition_id header",
        )

    def test_orphan_missing_on_disk_is_silently_dropped(self):
        """If the prior manifest mentions a file the user already
        deleted from disk (e.g. via a manual `rm` between installs),
        the orphan logic should silently drop the manifest entry — NO
        action, NO deferral, NO error."""
        installed = self._foo_hook_path()
        # User pre-deletes the file.
        installed.unlink()

        # Upstream also stops shipping it.
        self._delete_foo_from_orchestrator()

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        ext = "ps1" if platform.system() == "Windows" else "sh"
        rel = str(Path(".claude") / "hooks" / f"foo.{ext}")

        # NOT in orphan-deleted (we don't take credit for what the user
        # already did) and NOT in orphan-preserved (it wasn't on disk).
        self.assertNotIn(rel, result["actions"]["orphan-deleted"])
        self.assertNotIn(rel, result["actions"]["orphan-preserved"])
        # Manifest no longer carries the entry.
        manifest = json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(rel, manifest["files"])
        # No deferral.
        report = DeferralReport.read(self.proj)
        self.assertFalse(report.has_condition("bundle_user_modified_deletion_preserved"))

    def test_orphan_deferral_clears_when_user_resolves(self):
        """After an orphan-preserved deferral fires, the user has two
        options: keep the file (dismiss) or delete it. If they delete
        it manually, the NEXT install run must clear the stale deferral
        entry via the reconcile pass."""
        installed = self._foo_hook_path()
        installed.write_text("# USER CUSTOM\n", encoding="utf-8")
        self._delete_foo_from_orchestrator()

        # First update: emits the deferral.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertTrue(
            DeferralReport.read(self.proj)
            .has_condition("bundle_user_modified_deletion_preserved")
        )

        # User deletes the orphan manually.
        installed.unlink()

        # Second update: orphan is gone → reconcile should drop the deferral.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertFalse(
            DeferralReport.read(self.proj)
            .has_condition("bundle_user_modified_deletion_preserved"),
            "reconcile should have cleared the resolved deferral",
        )


# ---------------------------------------------------------------------------
# Audit case #2: template references now-deleted file (verified by spec)
# ---------------------------------------------------------------------------


class TemplateReferencingDeletedFileTests(unittest.TestCase):
    """v0.2.24 §A0 audit case #2: template references a now-deleted file.

    The orchestrator's bundle enumeration walks `templates/` directly
    via `_enumerate_bundle_files` — there's no separate "template
    manifest" that could go stale. When upstream deletes a template
    file, it simply doesn't appear in the next `ops` list. The
    `_install_bundle_files` function never sees it; nothing to error
    on.

    This test verifies the by-design behavior holds (it would catch a
    regression where someone added a side index of expected templates
    and forgot to keep it in sync).
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-bundle-tmpl-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_deletion_of_template_file_does_not_error(self):
        """Delete an entire template subtree (skills/architect/) and
        verify install-bundle still succeeds with errors=[]."""
        # First install pulls everything including skills/architect.
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        self.assertEqual(result["errors"], [])

        # Now upstream "deletes" the entire skills/architect tree.
        import shutil
        shutil.rmtree(self.orch / "templates" / "skills" / "architect")

        # Second install: no error.
        result2 = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertEqual(result2["errors"], [],
                         f"upstream deletion shouldn't error: {result2}")
        # The deleted templates' installed counterparts become orphans
        # (and since the user never modified them, they get SAFE-DELETED).
        # Verify by checking they're no longer on disk.
        self.assertFalse(
            (self.proj / ".claude" / "skills" / "architect" / "SKILL.md").exists(),
            "untouched orphan should be removed",
        )


# ---------------------------------------------------------------------------
# Audit case #3: user-added file conflicts with new-shipped path
# ---------------------------------------------------------------------------


class UserAddedFileConflictsWithNewShippedPathTests(unittest.TestCase):
    """v0.2.24 §A0 audit case #3: user-added file conflicts with a
    path the new shipped manifest wants to install.

    Two sub-cases:
    (a) update_mode (re-install): the user's pre-existing file has no
        prior manifest entry, so `_file_action` returns `preserve` and
        emits `bundle_user_modified_preserved`.
    (b) first-install mode: the user's pre-existing file goes through
        `skip-existing` and emits `bundle_skipped_existing_files`.

    Both paths already existed before A0; the orphan-detection commit
    must not regress them.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-bundle-conflict-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_first_install_user_pre_existing_file_goes_to_skip_existing(self):
        """User has a pre-existing `.claude/scripts/notify.py` with
        their own content before VCO ever installs. First-install
        must NOT overwrite — emits `bundle_skipped_existing_files`."""
        target = self.proj / ".claude" / "scripts" / "notify.py"
        target.parent.mkdir(parents=True)
        target.write_text("# PRE-EXISTING USER\n", encoding="utf-8")

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        rel = str(Path(".claude") / "scripts" / "notify.py")
        self.assertIn(rel, result["actions"]["skip-existing"],
                      f"expected skip-existing: {result['actions']}")
        # User content untouched.
        self.assertEqual(target.read_text(encoding="utf-8"), "# PRE-EXISTING USER\n")
        # Deferral entry emitted.
        report = DeferralReport.read(self.proj)
        self.assertTrue(
            report.has_condition("bundle_skipped_existing_files"),
            f"missing deferral; entries: {[e.condition_id for e in report.entries]}",
        )

    def test_update_mode_user_pre_existing_file_without_manifest_entry_preserved(self):
        """Edge: user manually adds a file that the orchestrator only
        starts shipping NOW (no prior manifest entry). In update mode
        this should go through `preserve` (no prior baseline known →
        default to safety) and emit `bundle_user_modified_preserved`.

        This matches the legacy contract — we re-test here because
        the orphan-detection logic must NOT touch files that the new
        ops DOES re-ship (they go through the regular per-op loop, not
        the orphan loop)."""
        # First install with the fake orchestrator's set.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        # User pre-creates a file that the orchestrator's NEW bundle
        # adds. Simulate "new shipping" by writing a brand-new
        # orchestrator-side template file with the same target path
        # that the user already has.
        new_target = self.proj / ".claude" / "scripts" / "fresh_helper.py"
        new_target.write_text("# USER OWNS THIS\n", encoding="utf-8")
        # Orchestrator now ships it too:
        (self.orch / "templates" / "scripts" / "fresh_helper.py").write_text(
            "# orchestrator version\n", encoding="utf-8",
        )

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        rel = str(Path(".claude") / "scripts" / "fresh_helper.py")
        # No prior manifest entry → `_file_action` returns preserve.
        self.assertIn(rel, result["actions"]["preserve"],
                      f"expected preserve for user-added file: {result['actions']}")
        # User content untouched.
        self.assertEqual(new_target.read_text(encoding="utf-8"), "# USER OWNS THIS\n")
        # Deferral entry.
        report = DeferralReport.read(self.proj)
        self.assertTrue(report.has_condition("bundle_user_modified_preserved"))


if __name__ == "__main__":
    unittest.main()
