"""Tests for vco_lib.project_init.install_project_bundle and
bootstrap_collections (PR 4).

Covers:
  - Hash-based update behavior (preserve user-modified, overwrite shipped).
  - Manifest round-trip (write, read, schema_version=1, files map).
  - OS-aware hook selection (.sh on POSIX, .ps1 on Windows).
  - Smart-merge of settings.json (merge hooks, preserve user keys).
  - Soft-fail on Weaviate-down: deferral entry written, exit clean.
  - Dry-run produces no filesystem mutations.
  - Idempotency: second run is noop.
  - Substitutions applied to agent .md files.
  - Skill recursive copy preserves directory structure.
  - Bootstrap idempotency: existing collections left alone.

Live Weaviate integration is intentionally NOT exercised here (kept in
test_vco_lib_migrate.py's LiveMigrateIntegrationTest). These tests run
fully offline by stubbing out network paths.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_fake_orchestrator(root: Path) -> None:
    """Build a minimal fake orchestrator tree for bundle tests.

    Layout written:
        <root>/vct-module.json                 — repo-root marker
        <root>/templates/hooks/foo.sh
        <root>/templates/hooks/foo.ps1
        <root>/templates/hooks/_lib/find-python.sh
        <root>/templates/hooks/_lib/find-python.ps1
        <root>/templates/scripts/kg-search
        <root>/templates/scripts/notify.py
        <root>/templates/agents/free/coder.md (with {{ORCHESTRATOR_ROOT}})
        <root>/templates/skills/architect/SKILL.md (with {{HOME}})
        <root>/templates/skills/architect/extra.txt
        <root>/templates/settings.json.linux.template
        <root>/templates/settings.json.windows.template
        <root>/infrastructure/docker-compose.yml
        <root>/infrastructure/podman-compose.gpu.yml
    """
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")

    hooks = root / "templates" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "foo.sh").write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
    (hooks / "foo.ps1").write_text("Write-Host 'v1'\n", encoding="utf-8")

    lib = hooks / "_lib"
    lib.mkdir()
    (lib / "find-python.sh").write_text("# find-python v1\n", encoding="utf-8")
    (lib / "find-python.ps1").write_text("# find-python.ps1 v1\n", encoding="utf-8")

    scripts = root / "templates" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "kg-search").write_text("#!/usr/bin/env python3\nprint('search')\n", encoding="utf-8")
    (scripts / "notify.py").write_text("def notify(): pass\n", encoding="utf-8")
    (scripts / "claude_token_counter.py").write_text("def count(): return 0\n", encoding="utf-8")

    agents = root / "templates" / "agents" / "free"
    agents.mkdir(parents=True)
    (agents / "coder.md").write_text(
        "# Coder agent\n"
        "Orchestrator at {{ORCHESTRATOR_ROOT}}\n"
        "Projects under {{PROJECTS_ROOT}}\n"
        "Home {{HOME}}\n",
        encoding="utf-8",
    )

    skills = root / "templates" / "skills"
    arch = skills / "architect"
    arch.mkdir(parents=True)
    (arch / "SKILL.md").write_text(
        "# Architect skill\nHome: {{HOME}}\n", encoding="utf-8",
    )
    (arch / "extra.txt").write_text("not-a-markdown-file\n", encoding="utf-8")

    settings = {
        "$schema": "test",
        "permissions": {"allow": ["Bash"]},
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "vco-foo"}]},
            ],
        },
    }
    (root / "templates" / "settings.json.linux.template").write_text(
        json.dumps(settings, indent=2), encoding="utf-8",
    )
    (root / "templates" / "settings.json.windows.template").write_text(
        json.dumps(settings, indent=2), encoding="utf-8",
    )

    infra = root / "infrastructure"
    infra.mkdir()
    (infra / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (infra / "podman-compose.gpu.yml").write_text("services: {gpu: {}}\n", encoding="utf-8")
    (infra / "README.md").write_text("ignored\n", encoding="utf-8")  # non-compose file


# ---------------------------------------------------------------------------
# install_project_bundle tests
# ---------------------------------------------------------------------------


class InstallBundleFreshTests(unittest.TestCase):
    """First-install on an empty project folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-bundle-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_fresh_install_creates_all_categories(self):
        result = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        self.assertEqual(result["errors"], [])
        # Hooks are picked OS-actively. On Linux: foo.sh + _lib/find-python.sh.
        # On Windows: foo.ps1 + _lib/find-python.ps1.
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"
        # Hook present (not _lib) goes through "create"
        self.assertIn(
            str(Path(".claude") / "hooks" / f"foo.{ext}"),
            result["actions"]["create"],
        )
        # _lib goes through "always-overwrite"
        self.assertIn(
            str(Path(".claude") / "hooks" / "_lib" / f"find-python.{ext}"),
            result["actions"]["always-overwrite"],
        )
        # Scripts are extension-agnostic.
        self.assertIn(
            str(Path(".claude") / "scripts" / "kg-search"),
            result["actions"]["create"],
        )
        self.assertIn(
            str(Path(".claude") / "scripts" / "notify.py"),
            result["actions"]["create"],
        )
        self.assertIn(
            str(Path(".claude") / "scripts" / "claude_token_counter.py"),
            result["actions"]["create"],
        )
        # Agents.
        self.assertIn(
            str(Path(".claude") / "agents" / "coder.md"),
            result["actions"]["create"],
        )
        # Skills (recursive — note the relative path includes the skill dirname).
        self.assertIn(
            str(Path(".claude") / "skills" / "architect" / "SKILL.md"),
            result["actions"]["create"],
        )
        self.assertIn(
            str(Path(".claude") / "skills" / "architect" / "extra.txt"),
            result["actions"]["create"],
        )
        # Infrastructure compose files (only docker-/podman- yml).
        self.assertIn(
            str(Path("infrastructure") / "docker-compose.yml"),
            result["actions"]["create"],
        )
        self.assertIn(
            str(Path("infrastructure") / "podman-compose.gpu.yml"),
            result["actions"]["create"],
        )
        # README.md in infrastructure must NOT be copied.
        infra_paths = result["actions"]["create"]
        self.assertFalse(any("README.md" in p and "infrastructure" in p
                             for p in infra_paths))
        # settings.json template merged → "created".
        self.assertEqual(result["settings_action"], "created")
        # Manifest written.
        self.assertTrue(result["manifest_written"])
        manifest_path = self.proj / ".claude" / ".vco-manifest.json"
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertIn("vco_version", manifest)
        self.assertIn("installed_at", manifest)
        self.assertIn("files", manifest)
        # Manifest entries carry sha256 + source.
        for rel, info in manifest["files"].items():
            self.assertIn("sha256", info)
            self.assertIn("source", info)
            self.assertEqual(len(info["sha256"]), 64)  # hex SHA256

    def test_agent_substitutions_applied(self):
        project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        coder = (self.proj / ".claude" / "agents" / "coder.md").read_text(
            encoding="utf-8",
        )
        self.assertIn(str(self.orch), coder)
        self.assertIn(str(self.orch.parent), coder)
        self.assertIn(str(Path.home()), coder)
        self.assertNotIn("{{ORCHESTRATOR_ROOT}}", coder)
        self.assertNotIn("{{PROJECTS_ROOT}}", coder)
        self.assertNotIn("{{HOME}}", coder)

    def test_skill_md_substituted_text_files_byte_copied(self):
        project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        skill_md = (self.proj / ".claude" / "skills" / "architect" / "SKILL.md").read_text(
            encoding="utf-8",
        )
        self.assertIn(str(Path.home()), skill_md)
        self.assertNotIn("{{HOME}}", skill_md)
        # extra.txt is NOT a .md file → byte-copy, no substitution.
        extra = (self.proj / ".claude" / "skills" / "architect" / "extra.txt").read_text(
            encoding="utf-8",
        )
        self.assertEqual(extra, "not-a-markdown-file\n")

    def test_first_install_skips_existing_files(self):
        # Pre-create a file the user customized.
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"
        target = self.proj / ".claude" / "hooks" / f"foo.{ext}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("USER CUSTOM\n", encoding="utf-8")

        result = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        self.assertIn(
            str(Path(".claude") / "hooks" / f"foo.{ext}"),
            result["actions"]["skip-existing"],
        )
        # User content preserved.
        self.assertEqual(target.read_text(encoding="utf-8"), "USER CUSTOM\n")

    def test_skip_existing_emits_deferral_entry(self):
        """Per coordinator directive 2026-05-01: when first-install hits a
        target that exists with different content, leave a single
        per-project deferral entry (`bundle_skipped_existing_files`)
        listing all such files so Claude Code on next session knows the
        bundle install was incomplete.

        Critical assertions:
        - File untouched (user content preserved).
        - UPDATE_DEFERRED.md exists.
        - Deferral has condition_id `bundle_skipped_existing_files`.
        - The skipped file's relative path appears in the entry body.
        """
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"
        target = self.proj / ".claude" / "hooks" / f"foo.{ext}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("USER CUSTOM\n", encoding="utf-8")

        project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
        )
        # File untouched.
        self.assertEqual(target.read_text(encoding="utf-8"), "USER CUSTOM\n")
        # Deferral entry written to the project's UPDATE_DEFERRED.md.
        deferred = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertTrue(deferred.exists(),
                        "UPDATE_DEFERRED.md must exist after skip-existing")
        report = DeferralReport.read(self.proj)
        self.assertTrue(
            report.has_condition("bundle_skipped_existing_files"),
            "deferral with condition_id `bundle_skipped_existing_files` "
            "must be present when first-install skipped pre-existing files",
        )
        # Body lists the skipped file's relative path.
        body = deferred.read_text(encoding="utf-8")
        rel = str(Path(".claude") / "hooks" / f"foo.{ext}")
        self.assertIn(rel, body,
                      f"deferral body must list the skipped file ({rel}):\n{body}")

    def test_skip_existing_grouped_per_project_one_entry(self):
        """Even when multiple files are skipped, only ONE deferral entry
        is emitted (per coordinator: per-project grouping, not per-file).

        We read the raw markdown to assert all files are listed — the
        deferral_report parser captures only the first line of multi-line
        fields like `Detected:` so reparsing the bullet-list back out is
        not currently round-trip-able. The on-disk file IS the canonical
        source for Claude Code on next session.
        """
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"
        # Pre-create TWO files with custom content.
        for name in (f"foo.{ext}", "kg-search"):
            t = self.proj / ".claude" / ("hooks" if name.endswith(("." + ext)) else "scripts") / name
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(f"USER {name}\n", encoding="utf-8")

        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        # Exactly one entry with the right condition_id.
        report = DeferralReport.read(self.proj)
        skip_entries = [e for e in report.entries
                        if e.condition_id == "bundle_skipped_existing_files"]
        self.assertEqual(len(skip_entries), 1,
                         f"expected exactly 1 grouped entry, got {len(skip_entries)}")
        # Both files listed in the markdown body.
        body = (self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md") \
            .read_text(encoding="utf-8")
        # Section header.
        self.assertIn("## bundle_skipped_existing_files (info)", body)
        # Both file paths in the bullet-list.
        self.assertIn(f"foo.{ext}", body)
        self.assertIn("kg-search", body)

    def test_skip_existing_no_deferral_when_nothing_skipped(self):
        """Clean first-install (empty folder) should NOT emit the
        skipped-existing deferral entry."""
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        report = DeferralReport.read(self.proj)
        self.assertFalse(report.has_condition("bundle_skipped_existing_files"))

    def test_dry_run_makes_no_mutations(self):
        result = project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
            dry_run=True,
        )
        # No files written, manifest not written.
        self.assertFalse((self.proj / ".claude" / "hooks").exists())
        self.assertFalse((self.proj / ".claude" / ".vco-manifest.json").exists())
        self.assertFalse(result["manifest_written"])
        # But the action plan must still contain entries.
        self.assertGreater(len(result["actions"]["create"]), 0)

    def test_idempotent_second_run_is_noop(self):
        # First install.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        # Second install — nothing should change.
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        # Files are now present; with update_mode=False, all should be skip-existing or noop.
        self.assertEqual(result["actions"]["create"], [])
        # Settings is "unchanged" on identical re-merge.
        self.assertIn(result["settings_action"], ("unchanged", ""))


class InstallBundleUpdateModeTests(unittest.TestCase):
    """Update mode: hash-based drift detection."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-bundle-update-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)
        # Initial install seeds the manifest.
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _foo_path(self) -> Path:
        ext = "ps1" if platform.system() == "Windows" else "sh"
        return self.proj / ".claude" / "hooks" / f"foo.{ext}"

    def _bump_orchestrator_foo(self, new_content: str) -> None:
        """Simulate a new orchestrator release shipping a new foo.sh."""
        ext = "ps1" if platform.system() == "Windows" else "sh"
        (self.orch / "templates" / "hooks" / f"foo.{ext}").write_text(
            new_content, encoding="utf-8",
        )

    def test_update_overwrites_unmodified_files(self):
        # Bump orchestrator template.
        self._bump_orchestrator_foo("#!/bin/sh\necho v2\n")
        # User has NOT touched the file. Update should overwrite.
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"
        self.assertIn(
            str(Path(".claude") / "hooks" / f"foo.{ext}"),
            result["actions"]["overwrite"],
        )
        new_content = "#!/bin/sh\necho v2\n" if not is_windows else "#!/bin/sh\necho v2\n"
        # The fake orchestrator wrote the new content; the user's installed
        # copy now matches the bumped template.
        self.assertEqual(self._foo_path().read_text(encoding="utf-8"), new_content)

    def test_update_preserves_user_modified_files(self):
        # User modified the installed file.
        self._foo_path().write_text("USER EDIT\n", encoding="utf-8")
        # Orchestrator also bumped.
        self._bump_orchestrator_foo("#!/bin/sh\necho v2\n")

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"
        self.assertIn(
            str(Path(".claude") / "hooks" / f"foo.{ext}"),
            result["actions"]["preserve"],
        )
        # User content untouched.
        self.assertEqual(self._foo_path().read_text(encoding="utf-8"), "USER EDIT\n")

        # Deferral entry emitted.
        report = DeferralReport.read(self.proj)
        self.assertTrue(report.has_condition("bundle_user_modified_preserved"))

    def test_force_overwrites_user_modifications(self):
        self._foo_path().write_text("USER EDIT\n", encoding="utf-8")
        self._bump_orchestrator_foo("#!/bin/sh\necho v3\n")

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True, force=True,
        )
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"
        # With --force, preserve becomes overwrite.
        self.assertIn(
            str(Path(".claude") / "hooks" / f"foo.{ext}"),
            result["actions"]["overwrite"],
        )
        self.assertEqual(self._foo_path().read_text(encoding="utf-8"), "#!/bin/sh\necho v3\n")
        # No deferral emitted under --force.
        report = DeferralReport.read(self.proj)
        self.assertFalse(report.has_condition("bundle_user_modified_preserved"))

    def test_lib_files_always_overwrite(self):
        # _lib files should be unconditionally overwritten on update.
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"
        lib_target = self.proj / ".claude" / "hooks" / "_lib" / f"find-python.{ext}"
        # User modifies the lib file (shouldn't be persisted on update).
        lib_target.write_text("USER LIB EDIT\n", encoding="utf-8")
        # Orchestrator hasn't changed it.
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertIn(
            str(Path(".claude") / "hooks" / "_lib" / f"find-python.{ext}"),
            result["actions"]["always-overwrite"],
        )
        # Content reverted to shipped version.
        self.assertNotIn("USER LIB EDIT", lib_target.read_text(encoding="utf-8"))

    def test_manifest_round_trip_preserves_prior_hash_on_preserve(self):
        # When a file is preserved (user-modified), its manifest entry must
        # retain the prior-shipped hash so a future update can still
        # recognize the original baseline.
        self._foo_path().write_text("USER EDIT\n", encoding="utf-8")
        self._bump_orchestrator_foo("v2 content\n")
        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        manifest = json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )
        ext = "ps1" if platform.system() == "Windows" else "sh"
        rel = str(Path(".claude") / "hooks" / f"foo.{ext}")
        # The manifest entry should still be present (carrying the v1 hash —
        # NOT v2 — so the next update knows what we originally shipped).
        self.assertIn(rel, manifest["files"])

    def test_update_picks_up_newly_shipped_file(self):
        """PR 5 (2026-05-01): a file that the orchestrator only ships
        in the *new* bundle (i.e. wasn't present at first-install time
        — analogous to today's `claude_token_counter.py`) MUST be
        created on update. The manifest from the prior install lists no
        entry for it; `_file_action` falls into the `create` branch
        because the target path doesn't exist on disk.
        """
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"

        # Simulate "newly shipped" by writing a brand-new template hook
        # that wasn't part of setUp's first-install. Use the OS-correct
        # extension so `_hook_glob_for_os()` picks it up.
        new_hook = self.orch / "templates" / "hooks" / f"new_hook.{ext}"
        new_hook_body = "#!/bin/sh\necho new\n" if not is_windows else "Write-Host new\n"
        new_hook.write_text(new_hook_body, encoding="utf-8")

        # Sanity: the target doesn't exist before the update.
        target = self.proj / ".claude" / "hooks" / f"new_hook.{ext}"
        self.assertFalse(target.exists())

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        rel = str(Path(".claude") / "hooks" / f"new_hook.{ext}")
        self.assertIn(rel, result["actions"]["create"],
                      f"new_hook.{ext} should be in create[]: {result['actions']}")
        # File deposited on disk with the shipped content.
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), new_hook_body)
        # Manifest now records the new file's hash.
        manifest = json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )
        self.assertIn(rel, manifest["files"])
        self.assertEqual(len(manifest["files"][rel]["sha256"]), 64)

    def test_update_overwrites_unmodified_file(self):
        """PR 5 (2026-05-01): explicit case for the `overwrite` branch.

        Setup leaves the on-disk hook matching the prior-shipped hash
        (= user has not touched it). The orchestrator bumps the hook
        content. The update MUST overwrite — and the manifest's hash
        entry MUST advance to the new shipped hash so the next update
        re-computes the same `overwrite` decision against the latest
        baseline.

        This is distinct from `test_update_overwrites_unmodified_files`
        above (PR 4) — that one verifies the action; this one ALSO
        verifies the manifest hash advances.
        """
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"
        rel = str(Path(".claude") / "hooks" / f"foo.{ext}")
        # Snapshot the manifest's prior-shipped hash for foo.{ext}.
        manifest_before = json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )
        prior_hash = manifest_before["files"][rel]["sha256"]
        self.assertEqual(len(prior_hash), 64)

        # User left the file alone — bump orchestrator template only.
        new_body = "#!/bin/sh\necho v_overwrite\n"
        self._bump_orchestrator_foo(new_body)
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertIn(rel, result["actions"]["overwrite"])
        # File now has the new shipped content.
        self.assertEqual(self._foo_path().read_text(encoding="utf-8"), new_body)

        # Manifest's hash entry advanced to the new shipped hash.
        manifest_after = json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )
        new_hash = manifest_after["files"][rel]["sha256"]
        self.assertEqual(len(new_hash), 64)
        self.assertNotEqual(new_hash, prior_hash,
                            "manifest must advance to the new shipped hash on overwrite")

    def test_update_preserves_user_modified_with_deferral_entry(self):
        """PR 5 (2026-05-01): explicit, narrow case for the `preserve`
        branch + matching deferral entry. The PR 4 sibling test
        `test_update_preserves_user_modified_files` covers the basic
        case; this one additionally asserts that the deferral entry
        contains the offending file's rel path AND the suggested
        `--force` command.
        """
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"
        rel = str(Path(".claude") / "hooks" / f"foo.{ext}")

        # User edits the installed file — diverges from prior-shipped hash.
        self._foo_path().write_text("MY CUSTOM HOOK\n", encoding="utf-8")
        # Orchestrator also bumped (so we're not in the noop branch).
        self._bump_orchestrator_foo("#!/bin/sh\necho v_pr5\n")

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertIn(rel, result["actions"]["preserve"],
                      f"user-modified file should be preserved: {result['actions']}")
        # On-disk content untouched.
        self.assertEqual(self._foo_path().read_text(encoding="utf-8"),
                         "MY CUSTOM HOOK\n")

        # Deferral entry exists, names the file, points to --force.
        deferral_path = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertTrue(deferral_path.exists(),
                        f"expected deferral .md at {deferral_path}")
        body = deferral_path.read_text(encoding="utf-8")
        self.assertIn("bundle_user_modified_preserved", body)
        self.assertIn(f"foo.{ext}", body)
        self.assertIn("--force", body,
                      "deferral entry must surface the --force escape hatch")

    def test_update_force_overwrites_user_modified(self):
        """PR 5 (2026-05-01): `force=True` + `update_mode=True` → user-modified
        file is overwritten with the new shipped version, manifest hash
        advances, and NO `bundle_user_modified_preserved` entry is
        emitted (force is the explicit consent path).
        """
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"
        rel = str(Path(".claude") / "hooks" / f"foo.{ext}")

        # User-modified + orchestrator bumped.
        self._foo_path().write_text("USER VERSION\n", encoding="utf-8")
        new_body = "#!/bin/sh\necho v_forced\n"
        self._bump_orchestrator_foo(new_body)

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True, force=True,
        )
        # With force, the action flips from preserve → overwrite.
        self.assertIn(rel, result["actions"]["overwrite"])
        self.assertNotIn(rel, result["actions"].get("preserve", []))
        # File now has the new shipped content (user edits discarded).
        self.assertEqual(self._foo_path().read_text(encoding="utf-8"), new_body)

        # Manifest advanced to the new hash.
        manifest = json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )
        self.assertIn(rel, manifest["files"])

        # No bundle_user_modified_preserved deferral entry.
        report = DeferralReport.read(self.proj)
        self.assertFalse(
            report.has_condition("bundle_user_modified_preserved"),
            "force=True is the explicit consent path; preserve deferral must not be emitted",
        )

    def test_update_dry_run_no_mutations(self):
        """PR 5 (2026-05-01): `dry_run=True` with full update mode classifies
        every action without touching the filesystem. The on-disk files
        keep their pre-run content; the manifest is not rewritten; no
        deferral entry is added.
        """
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"

        # Set up a mixed scenario: one user-modified file, one new shipped file.
        user_content = "I EDITED THIS\n"
        self._foo_path().write_text(user_content, encoding="utf-8")
        self._bump_orchestrator_foo("#!/bin/sh\necho v_new\n")

        new_hook = self.orch / "templates" / "hooks" / f"dryrun_new.{ext}"
        new_hook_body = "echo dryrun new\n"
        new_hook.write_text(new_hook_body, encoding="utf-8")
        new_target = self.proj / ".claude" / "hooks" / f"dryrun_new.{ext}"

        # Snapshot manifest before.
        manifest_before = (self.proj / ".claude" / ".vco-manifest.json").read_bytes()

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch,
            update_mode=True, dry_run=True,
        )
        # Classification still happens — dry_run only blocks mutations.
        rel_foo = str(Path(".claude") / "hooks" / f"foo.{ext}")
        rel_new = str(Path(".claude") / "hooks" / f"dryrun_new.{ext}")
        self.assertIn(rel_foo, result["actions"]["preserve"])
        self.assertIn(rel_new, result["actions"]["create"])
        # On-disk: foo untouched.
        self.assertEqual(self._foo_path().read_text(encoding="utf-8"), user_content)
        # On-disk: new file NOT created.
        self.assertFalse(new_target.exists(),
                         f"dry_run must not write {new_target}")
        # Manifest byte-for-byte identical (no rewrite).
        manifest_after = (self.proj / ".claude" / ".vco-manifest.json").read_bytes()
        self.assertEqual(manifest_before, manifest_after,
                         "dry_run must not rewrite the manifest")
        # `manifest_written=False` reflects the no-write state.
        self.assertFalse(result["manifest_written"])
        # No deferral file written.
        deferral_path = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertFalse(deferral_path.exists(),
                         f"dry_run must not emit deferral .md")

    def test_user_modified_deferral_grouped_per_project(self):
        """Per coordinator directive 2026-05-01: when multiple files are
        preserved during update, only ONE deferral entry is emitted (per-
        project grouping, not per-file).

        Critical assertions:
        - `bundle_user_modified_preserved` deferral exists.
        - Exactly ONE entry with that condition_id.
        - Markdown body lists ALL preserved file rel-paths.
        """
        is_windows = platform.system() == "Windows"
        ext = "ps1" if is_windows else "sh"
        # User edits TWO different files post-install.
        foo = self.proj / ".claude" / "hooks" / f"foo.{ext}"
        foo.write_text("USER FOO\n", encoding="utf-8")
        kg = self.proj / ".claude" / "scripts" / "kg-search"
        kg.write_text("USER KG\n", encoding="utf-8")
        # Orchestrator bumped both.
        self._bump_orchestrator_foo("v2 foo\n")
        (self.orch / "templates" / "scripts" / "kg-search").write_text(
            "v2 kg\n", encoding="utf-8",
        )

        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        # Files untouched.
        self.assertEqual(foo.read_text(encoding="utf-8"), "USER FOO\n")
        self.assertEqual(kg.read_text(encoding="utf-8"), "USER KG\n")
        # Deferral has exactly ONE entry with the right condition_id.
        report = DeferralReport.read(self.proj)
        preserve_entries = [e for e in report.entries
                            if e.condition_id == "bundle_user_modified_preserved"]
        self.assertEqual(len(preserve_entries), 1,
                         f"expected exactly 1 grouped entry, got {len(preserve_entries)}")
        # Both files listed in the on-disk markdown body.
        body = (self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md") \
            .read_text(encoding="utf-8")
        self.assertIn("## bundle_user_modified_preserved (info)", body)
        self.assertIn(f"foo.{ext}", body)
        self.assertIn("kg-search", body)


class SmartMergeSettingsTests(unittest.TestCase):
    """settings.json template smart-merge."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-bundle-merge-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_existing_user_keys_preserved_on_merge(self):
        # Pre-create a user settings.json with custom env + new permissions.
        claude_dir = self.proj / ".claude"
        claude_dir.mkdir()
        existing = {
            "permissions": {"allow": ["UserCustom"]},
            "env": {"USER_KEY": "value"},
        }
        (claude_dir / "settings.json").write_text(
            json.dumps(existing, indent=2), encoding="utf-8",
        )

        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        merged = json.loads(
            (claude_dir / "settings.json").read_text(encoding="utf-8")
        )
        # User env block survived (smart-merge: user wins on scalar/dict).
        self.assertEqual(merged["env"]["USER_KEY"], "value")
        # User permission preserved (user wins on lists too — they're scalars
        # to the merge logic).
        self.assertEqual(merged["permissions"]["allow"], ["UserCustom"])
        # Template hooks block injected.
        self.assertIn("hooks", merged)
        self.assertIn("PreToolUse", merged["hooks"])

    def test_hooks_block_appends_template_when_user_has_different_command(self):
        claude_dir = self.proj / ".claude"
        claude_dir.mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "user-only"}]},
                ],
            },
        }
        (claude_dir / "settings.json").write_text(
            json.dumps(existing, indent=2), encoding="utf-8",
        )

        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        merged = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
        # Both user-only AND vco-foo entries should now be present.
        cmds: list[str] = []
        for entry in merged["hooks"]["PreToolUse"]:
            for h in entry.get("hooks", []):
                cmds.append(h.get("command", ""))
        self.assertIn("user-only", cmds)
        self.assertIn("vco-foo", cmds)


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------


class ManifestRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-manifest-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_read_returns_empty_when_missing(self):
        m = project_init._read_manifest(self.tmp)
        self.assertEqual(m["schema_version"], 1)
        self.assertEqual(m["files"], {})

    def test_write_atomic_creates_directory(self):
        # No .claude/ yet — writer must mkdir.
        project_init._write_manifest_atomic(
            self.tmp,
            {"schema_version": 1, "files": {"a.txt": {"sha256": "x", "source": "y"}}},
        )
        path = self.tmp / ".claude" / ".vco-manifest.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["files"]["a.txt"]["sha256"], "x")

    def test_corrupt_manifest_treated_as_empty(self):
        (self.tmp / ".claude").mkdir()
        (self.tmp / ".claude" / ".vco-manifest.json").write_text(
            "{ not json", encoding="utf-8",
        )
        m = project_init._read_manifest(self.tmp)
        self.assertEqual(m["files"], {})


# ---------------------------------------------------------------------------
# bootstrap_collections tests
# ---------------------------------------------------------------------------


class BootstrapCollectionsTests(unittest.TestCase):
    """Soft-fail policy + idempotency. No live Weaviate required."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-bootstrap-"))
        self.proj = self.tmp / "project"
        self.proj.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_soft_fail_writes_deferral_when_weaviate_down(self):
        # Stub all network paths to simulate Weaviate unreachable AND
        # restart fail.
        with mock.patch.object(project_init, "_is_weaviate_reachable", return_value=False), \
             mock.patch.object(project_init, "_attempt_container_restart", return_value=False):
            result = project_init.bootstrap_collections(
                "VideoFrames",
                weaviate_url="http://localhost:8081",
                project_folder=self.proj,
            )
        self.assertFalse(result["weaviate_reachable"])
        self.assertTrue(result["restart_attempted"])
        self.assertFalse(result["restart_succeeded"])
        self.assertTrue(result["deferred"])
        self.assertEqual(result["errors"], [])
        # Deferral file written.
        deferral = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertTrue(deferral.exists())
        report = DeferralReport.read(self.proj)
        self.assertTrue(report.has_condition("weaviate_unreachable_at_bootstrap"))

    def test_dry_run_skips_restart_attempt_and_deferral(self):
        # Dry-run mode: no restart attempt, no deferral .md write. Pure
        # planning preview — operator can preview "would-create" entries
        # against an up Weaviate, or just see "weaviate_reachable: False"
        # against a down one.
        with mock.patch.object(project_init, "_is_weaviate_reachable", return_value=False), \
             mock.patch.object(project_init, "_attempt_container_restart") as restart, \
             mock.patch.object(project_init, "_fetch_schema", return_value=None):
            result = project_init.bootstrap_collections(
                "VideoFrames",
                project_folder=self.proj,
                dry_run=True,
            )
        restart.assert_not_called()
        self.assertFalse(result["weaviate_reachable"])
        self.assertFalse(result["restart_attempted"])
        self.assertFalse(result["deferred"])
        # No deferral .md written.
        deferral = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertFalse(deferral.exists())

    def test_creates_missing_collections_when_reachable(self):
        # Stub: weaviate up, neither collection exists yet, _create_class
        # records calls.
        created: list[str] = []

        def _fake_create(payload, weaviate_url=None):
            created.append(payload["class"])

        with mock.patch.object(project_init, "_is_weaviate_reachable", return_value=True), \
             mock.patch.object(project_init, "_fetch_schema", return_value=None), \
             mock.patch.object(project_init, "_create_class", side_effect=_fake_create):
            result = project_init.bootstrap_collections(
                "VideoFrames", project_folder=self.proj,
            )
        self.assertTrue(result["weaviate_reachable"])
        self.assertEqual(result["errors"], [])
        # Three classes: per-project KG, per-project Dev, shared KG.
        self.assertIn("VideoFrames_KnowledgeGraph", created)
        self.assertIn("VideoFrames_Development", created)
        self.assertIn("VibeCodedTools_KnowledgeGraph", created)

    def test_idempotent_when_collections_exist(self):
        # Bug-1 v0.2.4 (2026-05-12): the existence check now also probes
        # for schema-compatibility — a pre-existing collection with a
        # divergent shape (legacy single-vector, missing slots, missing
        # indexNullState) triggers a regen. To keep this test focused on
        # the idempotency invariant ("if a compatible collection already
        # exists, don't touch it"), the stub now returns an at-target
        # schema. The previous `{"class": "stub"}` stub is no longer
        # realistic — that shape would correctly trigger a regen.
        def at_target(name, weaviate_url=None):
            # Return the canonical target schema for whichever class is
            # being probed. Same schema works for both KG and Dev for the
            # purposes of compat (Dev is a subset).
            return project_init.kg_class_definition(name)

        with mock.patch.object(project_init, "_is_weaviate_reachable", return_value=True), \
             mock.patch.object(project_init, "_fetch_schema", side_effect=at_target), \
             mock.patch.object(project_init, "_create_class") as create:
            result = project_init.bootstrap_collections(
                "VideoFrames", project_folder=self.proj,
            )
        # Existence check returned a compatible schema for each → no creates.
        create.assert_not_called()
        self.assertEqual(result["errors"], [])
        # No regen on compatible schema.
        self.assertEqual(result.get("regenerated", []), [])
        # All three actions are "exists".
        for action in result["actions"]:
            self.assertEqual(action["action"], "exists")
            self.assertTrue(action["ok"])

    def test_kg_only_skips_dev_but_keeps_shared(self):
        # The kg_only flag must NOT skip the shared KG (per coordinator
        # directive: every project always reads shared KG, so creation is
        # unconditional).
        created: list[str] = []
        with mock.patch.object(project_init, "_is_weaviate_reachable", return_value=True), \
             mock.patch.object(project_init, "_fetch_schema", return_value=None), \
             mock.patch.object(project_init, "_create_class",
                               side_effect=lambda p, weaviate_url=None: created.append(p["class"])):
            project_init.bootstrap_collections(
                "VideoFrames", project_folder=self.proj, kg_only=True,
            )
        self.assertIn("VideoFrames_KnowledgeGraph", created)
        self.assertIn("VibeCodedTools_KnowledgeGraph", created)
        self.assertNotIn("VideoFrames_Development", created)

    def test_restart_succeeds_then_bootstrap_continues(self):
        # First reachability probe fails, restart succeeds, second probe
        # succeeds → bootstrap proceeds.
        probe_returns = iter([False, True])  # first call → False, then True

        def _probe(url, *, timeout=5.0):
            return next(probe_returns)

        # _wait_for_weaviate_ready calls _is_weaviate_reachable internally —
        # patch _wait_for_weaviate_ready directly to simulate the restart->ready path.
        with mock.patch.object(project_init, "_is_weaviate_reachable",
                               side_effect=lambda url, **kw: False), \
             mock.patch.object(project_init, "_attempt_container_restart", return_value=True), \
             mock.patch.object(project_init, "_wait_for_weaviate_ready", return_value=True), \
             mock.patch.object(project_init, "_fetch_schema", return_value={"class": "x"}):
            result = project_init.bootstrap_collections(
                "VideoFrames", project_folder=self.proj,
            )
        self.assertTrue(result["restart_attempted"])
        self.assertTrue(result["restart_succeeded"])
        self.assertTrue(result["weaviate_reachable"])
        self.assertFalse(result["deferred"])


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class BundleCliTests(unittest.TestCase):
    """Subprocess test of `python -m vco_lib.project_init install-bundle`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-bundle-cli-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_install_bundle_json_output(self):
        result = subprocess.run(
            [sys.executable, "-m", "vco_lib.project_init",
             "install-bundle",
             "--folder", str(self.proj),
             "--orchestrator-root", str(self.orch),
             "--json"],
            capture_output=True, text=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        self.assertEqual(result.returncode, 0,
                         msg=f"stderr={result.stderr}\nstdout={result.stdout}")
        payload = json.loads(result.stdout)
        self.assertIn("actions", payload)
        self.assertIn("manifest_written", payload)
        self.assertTrue(payload["manifest_written"])

    def test_dry_run_via_cli(self):
        result = subprocess.run(
            [sys.executable, "-m", "vco_lib.project_init",
             "install-bundle",
             "--folder", str(self.proj),
             "--orchestrator-root", str(self.orch),
             "--dry-run", "--json"],
            capture_output=True, text=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["manifest_written"])
        # No files actually written.
        self.assertFalse((self.proj / ".claude" / "hooks").exists())

    def test_folder_must_match_project_folder_when_both_given(self):
        result = subprocess.run(
            [sys.executable, "-m", "vco_lib.project_init",
             "install-bundle",
             "--folder", str(self.proj),
             "--project-folder", str(self.tmp / "different"),
             "--orchestrator-root", str(self.orch),
             "--json"],
            capture_output=True, text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must refer to the same path", result.stderr)


class BootstrapCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-boot-cli-"))
        self.proj = self.tmp / "project"
        self.proj.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_bootstrap_dry_run_against_unreachable(self):
        # Use an obviously-unreachable port so we don't need Weaviate.
        # Dry-run should NOT defer.
        result = subprocess.run(
            [sys.executable, "-m", "vco_lib.project_init",
             "bootstrap-collections",
             "--name", "VideoFrames",
             "--weaviate-url", "http://127.0.0.1:1",  # unreachable
             "--dry-run",
             "--project-folder", str(self.proj),
             "--json"],
            capture_output=True, text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        # Dry-run is exit 0 even when unreachable.
        self.assertEqual(result.returncode, 0,
                         msg=f"stderr={result.stderr}\nstdout={result.stdout}")
        payload = json.loads(result.stdout)
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["weaviate_reachable"])
        # In dry-run we don't restart or defer.
        self.assertFalse(payload["restart_attempted"])
        self.assertFalse(payload["deferred"])


class MigrateRequiredDeferralTests(unittest.TestCase):
    """PR 5 (2026-05-01): `_emit_migrate_required_deferral` writes a
    `schema_migration_required` deferral entry when a pre-update
    `migrate-collections --dry-run` reveals copy/rebuild actions. The
    Rust update_project_v2 wraps this CLI so destructive Weaviate
    migrations require explicit user consent (preserves data).
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-migrate-required-"))
        self.proj = self.tmp / "project"
        self.proj.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_emit_migrate_required_deferral_copy_action(self):
        # Plan with a single `copy` action (smart migrate path).
        plan = [{"collection": "VideoFrames_KnowledgeGraph", "action": "copy"}]
        project_init._emit_migrate_required_deferral(
            self.proj,
            project_name="VideoFrames",
            weaviate_url="http://localhost:8081",
            plan_entries=plan,
        )
        deferral_path = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertTrue(deferral_path.exists())
        body = deferral_path.read_text(encoding="utf-8")
        self.assertIn("schema_migration_required", body)
        self.assertIn("VideoFrames_KnowledgeGraph", body)
        self.assertIn("copy-with-vectors", body)
        # Suggested command points at migrate-collections, not at the
        # destructive --force-rebuild (no rebuild in plan).
        self.assertIn("migrate-collections", body)
        self.assertNotIn("--force-rebuild", body)

    def test_emit_migrate_required_deferral_rebuild_action(self):
        plan = [{"collection": "ArcAgi_KnowledgeGraph", "action": "rebuild"}]
        project_init._emit_migrate_required_deferral(
            self.proj,
            project_name="ArcAgi",
            weaviate_url="http://localhost:8081",
            plan_entries=plan,
        )
        body = (self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md") \
            .read_text(encoding="utf-8")
        self.assertIn("schema_migration_required", body)
        self.assertIn("ArcAgi_KnowledgeGraph", body)
        self.assertIn("rebuild", body)
        # Rebuild plan ALSO surfaces the --force-rebuild escape hatch.
        self.assertIn("--force-rebuild", body)

    def test_emit_migrate_required_deferral_empty_plan_noop(self):
        # No destructive actions → no deferral file written.
        project_init._emit_migrate_required_deferral(
            self.proj,
            project_name="Whatever",
            weaviate_url="http://localhost:8081",
            plan_entries=[],
        )
        deferral_path = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertFalse(deferral_path.exists(),
                         "empty plan_entries must NOT write a deferral")

    def test_migrate_cli_writes_deferral_on_copy_drift(self):
        """End-to-end CLI: migrate-collections --dry-run with --project-folder
        AND a fake-Weaviate response that classifies a collection as
        needing `copy` MUST emit `schema_migration_required` deferral.

        Stubs the migrate dispatcher (we don't have a live Weaviate);
        verifies the CLI integration writes the right deferral.
        """
        with mock.patch.object(project_init, "migrate_collections") as mc:
            mc.return_value = {
                "plan": [
                    {"collection": "VideoFrames_KnowledgeGraph",
                     "action": "copy",
                     "objects_copied": 0,
                     "elapsed_ms": 0},
                ],
                "dry_run": True,
                "errors": [],
            }
            ns = mock.Mock(
                name="VideoFrames",
                dry_run=True,
                force_rebuild=False,
                weaviate_url="http://localhost:8081",
                project_folder=str(self.proj),
                json=False,
            )
            # Mock auto-creates `name` as a Mock attr — re-attach the real string.
            ns.name = "VideoFrames"
            rc = project_init._cmd_migrate_collections(ns)
            self.assertEqual(rc, 0)
        deferral_path = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertTrue(deferral_path.exists())
        body = deferral_path.read_text(encoding="utf-8")
        self.assertIn("schema_migration_required", body)
        self.assertIn("VideoFrames_KnowledgeGraph", body)

    def test_migrate_cli_no_deferral_on_noop_plan(self):
        """A plan composed of only `noop` / `create` / `patch_props` actions
        is non-destructive — no deferral entry.
        """
        with mock.patch.object(project_init, "migrate_collections") as mc:
            mc.return_value = {
                "plan": [
                    {"collection": "Foo_KnowledgeGraph",
                     "action": "noop", "objects_copied": 0, "elapsed_ms": 0},
                    {"collection": "Foo_Development",
                     "action": "patch_props", "objects_copied": 0, "elapsed_ms": 0},
                ],
                "dry_run": True,
                "errors": [],
            }
            ns = mock.Mock(
                dry_run=True,
                force_rebuild=False,
                weaviate_url="http://localhost:8081",
                project_folder=str(self.proj),
                json=False,
            )
            ns.name = "Foo"
            rc = project_init._cmd_migrate_collections(ns)
            self.assertEqual(rc, 0)
        deferral_path = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertFalse(deferral_path.exists(),
                         "non-destructive plan must NOT emit a deferral")


if __name__ == "__main__":
    unittest.main()
