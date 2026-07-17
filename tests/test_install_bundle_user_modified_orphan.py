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

# v0.2.83 PLAN-v0283 WP-B2: install the WP-B1 deferral_emit fake BEFORE
# importing project_init so the function-level `from vco_lib import
# deferral_emit` in the migrated emit paths resolves (the real module lands in
# a parallel worktree). Degrades to a no-op once the real module exists.
from tests._v0283_deferral_emit_fake import install_fake_deferral_emit  # noqa: E402

install_fake_deferral_emit()

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

    def test_orphan_user_modified_is_retired_no_deferral(self):
        """v0.2.83 PLAN-v0283 B-F5: when the orchestrator stops shipping
        `foo.sh` AND the user edited their installed copy (hash differs from
        prior shipped), the file is AUTO-KEPT (never deleted) and its manifest
        entry is RETIRED — NO deferral. Pre-.83 this emitted
        `bundle_user_modified_deletion_preserved`; the deferral was pure noise
        (the file is never deleted anyway), so B-F5 replaces it with a silent
        retire + an auto-resolutions.jsonl record."""
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

        # v0.2.83 B-F5: recorded as orphan-RETIRED, NOT orphan-preserved.
        self.assertIn(rel, result["actions"]["orphan-retired"],
                      f"expected {rel} in orphan-retired: {result['actions']}")
        self.assertNotIn(rel, result["actions"]["orphan-preserved"])
        # File still on disk WITH user content (NEVER deleted).
        self.assertTrue(installed.exists(), "user-modified orphan must be kept on disk")
        self.assertEqual(
            installed.read_text(encoding="utf-8"), "# USER CUSTOM CONTENT\n",
            "user content must be untouched",
        )
        # v0.2.83 B-F5: manifest entry is RETIRED (dropped) so a future run
        # no longer re-detects the file as an orphan → the retire is one-shot.
        manifest = json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(rel, manifest["files"],
                         "manifest entry should be retired for user-modified orphan")
        # v0.2.83 B-F5: NO deferral emitted.
        report = DeferralReport.read(self.proj)
        self.assertFalse(
            report.has_condition("bundle_user_modified_deletion_preserved"),
            f"B-F5 must NOT emit the deletion-preserved deferral; entries: "
            f"{[e.condition_id for e in report.entries]}",
        )
        # v0.2.83 B-F9: an auto-resolution record was written.
        jsonl = self.proj / ".claude" / "logs" / "auto-resolutions.jsonl"
        self.assertTrue(jsonl.exists(), "B-F9 auto-resolutions.jsonl must be written")
        rows = [
            r for r in jsonl.read_text(encoding="utf-8").splitlines() if r.strip()
        ]
        parsed = [json.loads(r) for r in rows]
        self.assertTrue(
            any(
                p["condition_id"] == "bundle_user_modified_deletion_preserved"
                and p["action"] == "retired_orphan_manifest_entry"
                for p in parsed
            ),
            f"expected a retire auto-resolution row; got {parsed}",
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

    def test_orphan_retire_is_one_shot_and_never_deferred(self):
        """v0.2.83 PLAN-v0283 B-F5: the auto-keep+retire is one-shot and never
        produces a deferral to clear. Pre-.83 the first update emitted
        `bundle_user_modified_deletion_preserved` and a later update (after the
        user deleted the file) cleared it via reconcile. Under B-F5 the FIRST
        update already retires the manifest entry with NO deferral, so:
          * the first update writes no deletion-preserved deferral, AND
          * a second update sees no manifest entry → nothing to re-detect,
            still no deferral (idempotent).
        Also pins the pre-existing stale-entry self-clear: if a legacy
        deletion-preserved entry is on disk, the retire run clears it."""
        installed = self._foo_hook_path()
        installed.write_text("# USER CUSTOM\n", encoding="utf-8")
        self._delete_foo_from_orchestrator()

        # Seed a PRE-EXISTING stale deferral (as if written by a pre-.83 run)
        # so we can pin the reconciler self-clear on the retire path.
        _seed = DeferralReport.read(self.proj)
        from vco_lib.deferral_report import DeferralEntry
        _seed.add_entry(DeferralEntry(
            condition_id="bundle_user_modified_deletion_preserved",
            title="legacy stale entry",
            detected="pre-.83 entry",
            why_deferred="pre-.83",
            command_to_apply="noop",
            severity="info",
        ))
        _seed.write(self.proj)

        # First update: retires the manifest entry, clears the stale deferral,
        # emits NO new deletion-preserved deferral.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertFalse(
            DeferralReport.read(self.proj)
            .has_condition("bundle_user_modified_deletion_preserved"),
            "retire run must clear any pre-existing deletion-preserved deferral "
            "and emit none",
        )
        self.assertTrue(installed.exists(), "the file must stay on disk")

        # Second update: manifest no longer tracks the file → idempotent no-op,
        # still no deferral.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertFalse(
            DeferralReport.read(self.proj)
            .has_condition("bundle_user_modified_deletion_preserved"),
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

    def test_update_mode_user_pre_existing_file_without_manifest_entry_adopted(self):
        """v0.2.84 PLAN-v0284 D7 (P5/R2): a manifest-LESS file at a shipped
        destination (`.claude/scripts/`) whose bytes match no history is now
        ADOPTED (refreshed to shipped bytes + timestamped backup + one-time
        notice), NOT frozen-forever `preserve`. This is the EXACT P5 incident
        shape (pre-manifest / pre-history-rewrite stale files). (Was
        `test_update_mode_user_pre_existing_file_without_manifest_entry_preserved`.)

        We still guard the orphan-detection invariant: a re-shipped file goes
        through the per-op loop (adopt), NOT the orphan loop.
        """
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
        # No prior manifest entry + no history match → adopt (P5).
        self.assertIn(rel, result["actions"]["adopt"],
                      f"expected adopt for manifest-less shipped file: {result['actions']}")
        # Shipped bytes on disk; user's prior bytes captured in the backup.
        self.assertEqual(new_target.read_text(encoding="utf-8"), "# orchestrator version\n")
        backup = self.proj / result["adopt_backup_dir"] / ".claude" / "scripts" / "fresh_helper.py"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), "# USER OWNS THIS\n")
        # NO eternal deferral.
        report = DeferralReport.read(self.proj)
        self.assertFalse(report.has_condition("bundle_user_modified_preserved"))


# ---------------------------------------------------------------------------
# v0.2.81: knowledge-retirement branch (curated nodes go root-only)
# ---------------------------------------------------------------------------


class KnowledgeRetirementTests(unittest.TestCase):
    """v0.2.81 data-safety branch: on a NON-root project's first post-.81
    update, prior curated `knowledge/**` manifest entries that are no longer
    shipped must NOT be orphan-deleted (mass-delete) NOR orphan-preserved
    (113-file deferral). They go into `knowledge-retired`: file left on disk
    UNTOUCHED, manifest entry pruned, NO deferral. Root targets are exempt —
    a curated node genuinely removed from templates/knowledge/ still
    orphan-processes normally at the root."""

    def _make_orch_with_knowledge(self, root: Path) -> None:
        _make_fake_orchestrator(root)
        kg = root / "templates" / "knowledge"
        kg.mkdir(parents=True, exist_ok=True)
        (kg / "TAG_HIERARCHY.md").write_text("# tags\nv1\n", encoding="utf-8")
        concepts = kg / "concepts"
        concepts.mkdir(exist_ok=True)
        (concepts / "alpha.md").write_text("# Alpha\nshipped-v1\n", encoding="utf-8")
        (concepts / "beta.md").write_text("# Beta\nshipped-v1\n", encoding="utf-8")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-bundle-kgretire-"))
        self.orch = self.tmp / "orchestrator"
        self.orch.mkdir()
        self._make_orch_with_knowledge(self.orch)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _seed_pre81_nonroot_project(self, proj: Path) -> tuple[str, str]:
        """Simulate a project installed BEFORE v0.2.81: curated knowledge
        files on disk + recorded in the manifest. Returns the two curated
        rels (hash-matching, user-modified)."""
        # Install as if this WERE the root once to lay down curated nodes +
        # a manifest that lists them, then re-target as non-root on update.
        # Simplest deterministic path: force the enumerator to include
        # curated by installing with folder == orch (root) into a temp, then
        # copy the resulting knowledge/ + manifest into the non-root project.
        root_stage = self.tmp / "root_stage"
        root_stage.mkdir()
        self._make_orch_with_knowledge(root_stage)  # its own templates
        project_init.install_project_bundle(
            root_stage, orchestrator_root=root_stage, update_mode=False,
        )
        import shutil
        # Copy knowledge/ + the manifest into the non-root project.
        shutil.copytree(root_stage / "knowledge", proj / "knowledge")
        (proj / ".claude").mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            root_stage / ".claude" / ".vco-manifest.json",
            proj / ".claude" / ".vco-manifest.json",
        )
        return "knowledge/concepts/alpha.md", "knowledge/concepts/beta.md"

    def test_retirement_act_leaves_disk_prunes_manifest_no_deferral(self):
        """The ACT case of the destructive-gate: a curated node whose hash
        still matches the shipped baseline (would be orphan-DELETED) and a
        user-modified one (would be orphan-PRESERVED + deferred) BOTH stay on
        disk, land in `knowledge-retired`, are pruned from the manifest, and
        emit NO deletion deferral."""
        proj = self.tmp / "nonroot_project"
        proj.mkdir()
        rel_untouched, rel_modified = self._seed_pre81_nonroot_project(proj)

        # User modifies one of the two curated copies.
        modified_disk = proj / Path(rel_modified)
        modified_disk.write_text("# Beta\nUSER EDIT\n", encoding="utf-8")
        untouched_disk = proj / Path(rel_untouched)
        untouched_before = untouched_disk.read_text(encoding="utf-8")

        result = project_init.install_project_bundle(
            proj, orchestrator_root=self.orch, update_mode=True,
        )

        # Both curated rels retired.
        self.assertIn(rel_untouched, result["actions"]["knowledge-retired"])
        self.assertIn(rel_modified, result["actions"]["knowledge-retired"])
        # NEITHER in orphan buckets.
        self.assertNotIn(rel_untouched, result["actions"]["orphan-deleted"])
        self.assertNotIn(rel_untouched, result["actions"]["orphan-preserved"])
        self.assertNotIn(rel_modified, result["actions"]["orphan-deleted"])
        self.assertNotIn(rel_modified, result["actions"]["orphan-preserved"])
        # BOTH files still on disk, bytes untouched (leave-alone).
        self.assertTrue(untouched_disk.exists())
        self.assertEqual(untouched_disk.read_text(encoding="utf-8"), untouched_before)
        self.assertTrue(modified_disk.exists())
        self.assertEqual(modified_disk.read_text(encoding="utf-8"), "# Beta\nUSER EDIT\n")
        # Manifest pruned of both.
        manifest = json.loads(
            (proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(rel_untouched, manifest["files"])
        self.assertNotIn(rel_modified, manifest["files"])
        # NO deletion deferral for these files.
        report = DeferralReport.read(proj)
        self.assertFalse(
            report.has_condition("bundle_user_modified_deletion_preserved"),
            "retirement must NOT emit an orphan-deletion deferral",
        )

    def test_retirement_matches_windows_shaped_manifest_keys(self):
        """FINAL-REVIEW B1 (Windows-only silent mass-delete): pre-v0.2.81
        manifest keys are raw `dest_rel` = `str(Path("knowledge") / rel)`,
        so on Windows they are backslash-shaped (`knowledge\\concepts\\a.md`).
        The retirement branch must normalize the separator BEFORE its
        `startswith("knowledge/")` test — otherwise a Windows non-root
        project's first post-.81 update MISSES retirement, the curated copies
        fall into the orphan machinery, and get MASS-DELETED (+ deferrals +
        re-embed). Runs on POSIX by rewriting the manifest keys to the
        Windows form; the fix makes retirement fire before any disk logic, so
        the assertion is deterministic cross-OS."""
        proj = self.tmp / "nonroot_win_project"
        proj.mkdir()
        rel_untouched, rel_modified = self._seed_pre81_nonroot_project(proj)

        # Rewrite the manifest so the curated keys are BACKSLASH-shaped, as a
        # real Windows pre-.81 install would have recorded them. Disk paths
        # stay POSIX (this test host); only the manifest keys are Windows-form.
        manifest_path = proj / ".claude" / ".vco-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        win_untouched = rel_untouched.replace("/", "\\")
        win_modified = rel_modified.replace("/", "\\")
        for posix_key, win_key in ((rel_untouched, win_untouched),
                                   (rel_modified, win_modified)):
            if posix_key in manifest["files"]:
                manifest["files"][win_key] = manifest["files"].pop(posix_key)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        untouched_disk = proj / Path(rel_untouched)
        untouched_before = untouched_disk.read_text(encoding="utf-8")

        result = project_init.install_project_bundle(
            proj, orchestrator_root=self.orch, update_mode=True,
        )

        # Windows-shaped keys must land in retirement, NOT the orphan buckets.
        self.assertIn(win_untouched, result["actions"]["knowledge-retired"])
        self.assertIn(win_modified, result["actions"]["knowledge-retired"])
        self.assertNotIn(win_untouched, result["actions"]["orphan-deleted"])
        self.assertNotIn(win_modified, result["actions"]["orphan-deleted"])
        # File NOT deleted (the mass-delete the bug would have caused).
        self.assertTrue(untouched_disk.exists())
        self.assertEqual(untouched_disk.read_text(encoding="utf-8"), untouched_before)
        # No deletion deferral.
        report = DeferralReport.read(proj)
        self.assertFalse(
            report.has_condition("bundle_user_modified_deletion_preserved"),
            "Windows-shaped retirement must NOT emit an orphan-deletion deferral",
        )

    def test_retirement_leave_alone_inverse_root_orphan_still_deletes(self):
        """The LEAVE-ALONE inverse of the exemption: at the ROOT, a curated
        node genuinely removed from templates/knowledge/ is orphan-DELETED
        normally (hash-matching) — proving the retirement branch is
        non-root-scoped, not a blanket knowledge exemption."""
        root = self.tmp / "root_target"
        root.mkdir()
        self._make_orch_with_knowledge(root)
        project_init.install_project_bundle(
            root, orchestrator_root=root, update_mode=False,
        )
        # Upstream removes concepts/beta.md from the root's own templates.
        (root / "templates" / "knowledge" / "concepts" / "beta.md").unlink()

        result = project_init.install_project_bundle(
            root, orchestrator_root=root, update_mode=True,
        )
        rel = "knowledge/concepts/beta.md"
        # Root target → NOT retired; normal orphan semantics (hash-matching
        # → safe delete).
        self.assertNotIn(rel, result["actions"]["knowledge-retired"])
        self.assertIn(rel, result["actions"]["orphan-deleted"])
        self.assertFalse((root / Path(rel)).exists())

    def test_retirement_allowlist_files_never_retired(self):
        """Allowlisted per-project files (TAG_HIERARCHY.md) are in ops for
        every target, so they're re-shipped/preserved normally and never
        land in `knowledge-retired`."""
        proj = self.tmp / "nonroot_allowlist"
        proj.mkdir()
        self._seed_pre81_nonroot_project(proj)

        result = project_init.install_project_bundle(
            proj, orchestrator_root=self.orch, update_mode=True,
        )
        rel = "knowledge/TAG_HIERARCHY.md"
        self.assertNotIn(rel, result["actions"]["knowledge-retired"])
        # It IS re-shipped (still on disk).
        self.assertTrue((proj / Path(rel)).exists())


if __name__ == "__main__":
    unittest.main()
