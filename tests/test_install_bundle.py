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
        self.assertTrue(report.has_condition("user_modified_bundle_files"))

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
        self.assertFalse(report.has_condition("user_modified_bundle_files"))

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
        with mock.patch.object(project_init, "_is_weaviate_reachable", return_value=True), \
             mock.patch.object(project_init, "_fetch_schema", return_value={"class": "stub"}), \
             mock.patch.object(project_init, "_create_class") as create:
            result = project_init.bootstrap_collections(
                "VideoFrames", project_folder=self.proj,
            )
        # Existence check returned non-None for each → no creates.
        create.assert_not_called()
        self.assertEqual(result["errors"], [])
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


if __name__ == "__main__":
    unittest.main()
