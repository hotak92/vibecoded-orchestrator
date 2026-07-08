"""Tests for the v0.2.63 "Safe add" feature in
vco_lib.project_init.install_project_bundle.

Safe add (per-add opt-in, default OFF) protects a project's sensitive,
often-committed project-root `.env`:

  - The Rust launcher writes a `.env.vco.reference` sidecar and skips the
    `.env` append + b12 KG_COLLECTION rewrite (Rust-side; not exercised here).
  - The Python bundle step (run with `--safe-add`):
      1. detects the `.env.vco.reference` sidecar on disk and records a
         `safe_add_skipped_env_merge` deferral row;
      2. appends the VCO-created paths to the project's LOCAL-only
         `.git/info/exclude` (never the tracked `.gitignore`) and records a
         `safe_add_git_exclude_updated` deferral row.

  - The `.claude/settings.json` + `.vscode/settings.json` merges are UNCHANGED
    under safe-add (those files are rarely committed) — verified here.

Default behaviour (safe_add=False) must be byte-for-byte unchanged: no
safe-add deferrals, `.git/info/exclude` untouched.

These tests run fully offline (the fixture orchestrator tree has no Weaviate
dependency; legacy-collection detection soft-fails to empty lists when
Weaviate is unreachable).
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

# Reuse the canonical fixture orchestrator builder from the main bundle test
# (single source of truth for the fake templates/ + infrastructure/ tree).
from tests.test_install_bundle import _make_fake_orchestrator  # noqa: E402


_DEFERRED_REL = Path(".claude") / "context" / "UPDATE_DEFERRED.md"
_ENV_SIDECAR = ".env.vco.reference"


class SafeAddBundleTests(unittest.TestCase):
    """Safe-add behaviour on a fixture project that already has the three
    sensitive files + a git repo."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-safeadd-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)

        # Pre-existing sensitive project-root .env (simulates a project the
        # user already has and commits). Generic descriptors only — no real
        # personal-project names.
        self.env_path = self.proj / ".env"
        self.env_original = (
            "# user's own committed env\n"
            "KG_COLLECTION=LegacyBareName\n"
            "SOME_USER_KEY=keep-me\n"
        )
        self.env_path.write_text(self.env_original, encoding="utf-8")

        # Pre-existing .claude/settings.json (rarely committed → still merges).
        self.claude_settings = self.proj / ".claude" / "settings.json"
        self.claude_settings.parent.mkdir(parents=True, exist_ok=True)
        self.claude_settings.write_text(
            json.dumps({"permissions": {"allow": ["UserTool"]}}, indent=2)
            + "\n",
            encoding="utf-8",
        )

        # Pre-existing .vscode/settings.json (rarely committed → still
        # backfills the exclude block).
        self.vscode_settings = self.proj / ".vscode" / "settings.json"
        self.vscode_settings.parent.mkdir(parents=True, exist_ok=True)
        self.vscode_settings.write_text(
            json.dumps({"editor.tabSize": 4}, indent=2) + "\n",
            encoding="utf-8",
        )

        # Make the project a git repo with a TRACKED .gitignore — safe-add
        # must NOT touch the tracked .gitignore, only .git/info/exclude.
        git_dir = self.proj / ".git"
        git_dir.mkdir()
        (git_dir / "info").mkdir()
        self.gitignore = self.proj / ".gitignore"
        self.gitignore_original = "node_modules/\n*.pyc\n"
        self.gitignore.write_text(self.gitignore_original, encoding="utf-8")
        self.exclude_path = git_dir / "info" / "exclude"

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    # --- helpers --------------------------------------------------------

    def _write_env_sidecar(self):
        """Simulate the Rust launcher writing the `.env.vco.reference`
        sidecar (Rust owns the `.env` skip; Python only records the
        deferral when the sidecar exists on disk)."""
        (self.proj / _ENV_SIDECAR).write_text(
            "# VCO reference — NOT the live .env\n"
            "KG_COLLECTION=Project_KnowledgeGraph\n"
            "PROJECT_NAME=Project\n",
            encoding="utf-8",
        )

    def _run(self, *, safe_add: bool):
        return project_init.install_project_bundle(
            self.proj,
            orchestrator_root=self.orch,
            update_mode=False,
            safe_add=safe_add,
        )

    def _deferral_ids(self):
        report = DeferralReport.read(self.proj)
        return {e.condition_id for e in report.entries}

    # --- safe_add=True --------------------------------------------------

    def test_env_never_modified_under_safe_add(self):
        self._write_env_sidecar()
        self._run(safe_add=True)
        # The live .env must be byte-for-byte unchanged.
        self.assertEqual(
            self.env_path.read_text(encoding="utf-8"),
            self.env_original,
            "safe-add must NOT append to / rewrite the project-root .env",
        )

    def test_env_sidecar_present_yields_env_deferral(self):
        self._write_env_sidecar()
        self._run(safe_add=True)
        ids = self._deferral_ids()
        self.assertIn("safe_add_skipped_env_merge", ids)
        # The deferral still lands in the on-disk UPDATE_DEFERRED.md so the
        # project's Claude sees it at session start.
        deferred_md = (
            self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        )
        self.assertTrue(deferred_md.exists())
        self.assertIn(
            "safe_add_skipped_env_merge",
            deferred_md.read_text(encoding="utf-8"),
        )
        # The deferral names the file, the sidecar, and a diff command.
        report = DeferralReport.read(self.proj)
        entry = next(
            e for e in report.entries
            if e.condition_id == "safe_add_skipped_env_merge"
        )
        self.assertIn(_ENV_SIDECAR, entry.detected)
        self.assertIn(".env", entry.detected)
        self.assertIn("diff", entry.command_to_apply)
        self.assertIn("dismiss-deferral", entry.command_to_apply)

        # v0.2.64 accuracy correction: the message must make clear VCO env is
        # ALREADY active (MCP via .claude/settings.json, CLI via
        # `source .claude/env`) and that ONLY the `source ./.env` convenience
        # is skipped — NOT KG routing. It must NOT regress to the old
        # misleading "KG routing is whatever your .env already had" wording.
        self.assertIn(".claude/settings.json", entry.detected)
        self.assertIn(".claude/env", entry.detected)
        self.assertNotIn(
            "not VCO's launcher-resolved values",
            entry.detected,
            "deferral must not claim KG routing falls back to the stale .env",
        )
        # The recommended remediation is `source .claude/env`, not `./.env`.
        self.assertIn("source", entry.command_to_apply)
        self.assertIn(".claude/env", entry.command_to_apply)
        # Nothing is actually broken, so the deferral is informational, not a
        # warning (mirrors `safe_add_git_exclude_updated`).
        self.assertEqual(entry.severity, "info")

    def test_settings_json_still_merges_under_safe_add(self):
        """Scope correction: .claude/settings.json is NOT gated by safe-add."""
        self._write_env_sidecar()
        result = self._run(safe_add=True)
        # The fixture template carries a hooks block + permissions; merging
        # into the existing file yields "merged".
        self.assertEqual(result["settings_action"], "merged")
        merged = json.loads(self.claude_settings.read_text(encoding="utf-8"))
        # User key preserved AND template hooks merged in.
        self.assertIn("UserTool", merged["permissions"]["allow"])
        self.assertIn("hooks", merged)

    def test_env_projection_fully_active_under_safe_add(self):
        """F2.3 end-to-end: the v0.2.63 safe-add contract promises that ONLY
        the project-root `.env` merge is skipped — the two VCO-owned env
        channels (`.claude/env` shell channel + `.claude/settings.json` env
        block, both consumed for KG routing) are STILL projected in full.

        `install_project_bundle(safe_add=True)` owns the `.env` skip + sidecar
        deferral; the canonical env projection is a SEPARATE step
        (`apply_project_env`, the Python entry the Rust launcher shells out to
        UNCONDITIONALLY, before the safe-add branch runs). This test drives
        BOTH halves against one safe-add project and asserts:
          1. project-root `.env` byte-for-byte untouched,
          2. `.env.vco.reference` sidecar present,
          3. `safe_add_skipped_env_merge` deferral emitted,
          4. `.claude/env` carries the canonical KG routing exports,
          5. `.claude/settings.json` env block carries the same canonical
             values — proving per-project KG routing is NOT degraded.
        """
        from vco_lib.config_projection import (
            CLAUDE_ENV_MANAGED_BEGIN,
            apply_project_env,
        )

        self._write_env_sidecar()
        # (1)-(3): the bundle step (safe-add) — .env skip + sidecar deferral.
        self._run(safe_add=True)
        self.assertEqual(
            self.env_path.read_text(encoding="utf-8"),
            self.env_original,
            "safe-add must NOT touch the project-root .env",
        )
        self.assertTrue(
            (self.proj / _ENV_SIDECAR).exists(),
            "the .env.vco.reference sidecar must be present under safe-add",
        )
        self.assertIn("safe_add_skipped_env_merge", self._deferral_ids())

        # (4)-(5): the canonical env projection — the step the Rust launcher
        # runs UNCONDITIONALLY (safe-add does not gate it). Mirror the DB-
        # sourced ProjectEnvBundle shape used by the byte-identical parity
        # test. Generic project name — no personal-project descriptors.
        bundle = {
            "canonical_env": {
                "KG_COLLECTION": "Project_KnowledgeGraph",
                "DEVELOPMENT_COLLECTION": "Project_Development",
                "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
                "SHARED_KG_WRITE_DISABLED": "false",
                "SHARED_KG_OPT_OUT": "false",
                "PROJECT_NAME": "Project",
                "CODE_GRAPH_PROJECT": "Project",
                "ACTIVE_EMBEDDING": "qwen3",
                "WEAVIATE_URL": "http://localhost:8081",
                "WEAVIATE_PORT": "8081",
                "OLLAMA_URL": "http://localhost:11435",
                "OLLAMA_PORT": "11435",
                "CODE_EMBED_URL": "http://localhost:11440",
                "CODE_EMBED_PORT": "11440",
            },
            "project_id": "safe-add-fixture",
            "project_root": self.proj,
        }
        apply_project_env(bundle, surfaces=["claude_env", "claude_settings_json"])

        # (4) .claude/env — VCO's shell channel — carries canonical exports.
        claude_env = (self.proj / ".claude" / "env").read_text(encoding="utf-8")
        self.assertIn(CLAUDE_ENV_MANAGED_BEGIN, claude_env)
        self.assertIn(
            'export KG_COLLECTION="Project_KnowledgeGraph"', claude_env,
            "safe-add must NOT skip the .claude/env KG routing projection",
        )
        self.assertIn('export PROJECT_NAME="Project"', claude_env)

        # (5) .claude/settings.json env block — the MCP channel — matches.
        settings = json.loads(
            self.claude_settings.read_text(encoding="utf-8")
        )
        self.assertEqual(settings["env"]["KG_COLLECTION"], "Project_KnowledgeGraph")
        self.assertEqual(settings["env"]["PROJECT_NAME"], "Project")
        # And the earlier bundle merge is still intact (env projection is a
        # separate, non-clobbering surface).
        self.assertIn("UserTool", settings["permissions"]["allow"])

        # Cross-check: the live project-root .env is STILL the user's original
        # (env projection wrote the .claude surfaces, never the committed .env).
        self.assertEqual(
            self.env_path.read_text(encoding="utf-8"),
            self.env_original,
            "env projection must target .claude/ surfaces, never the committed .env",
        )

    def test_vscode_settings_still_backfills_under_safe_add(self):
        """Scope correction: .vscode/settings.json is NOT gated by safe-add."""
        self._write_env_sidecar()
        result = self._run(safe_add=True)
        self.assertIn(
            result.get("backfill_vscode_excludes", {}).get("action"),
            ("backfilled", "noop"),
        )
        data = json.loads(self.vscode_settings.read_text(encoding="utf-8"))
        # User key preserved; an exclude key was added.
        self.assertEqual(data["editor.tabSize"], 4)
        self.assertTrue(
            any(k.startswith("files.") or "Exclude" in k for k in data),
            "expected a VS Code exclude block to be backfilled",
        )

    def test_git_info_exclude_gets_only_vco_created_paths(self):
        # C1 fix (check whether files are VCO-shipped or user-authored): the exclude
        # list is computed from the files VCO actually created — VCO-exclusive
        # namespaces (.claude/) collapse to a glob; everything else is a SPECIFIC
        # path. We must NEVER blanket-exclude a dir the user may own.
        self._write_env_sidecar()
        self._run(safe_add=True)
        self.assertTrue(self.exclude_path.exists())
        lines = [
            ln.strip()
            for ln in self.exclude_path.read_text(encoding="utf-8").splitlines()
        ]
        # VCO's own namespace + the Rust sidecar are excluded.
        self.assertIn("/.claude/", lines)
        self.assertIn("/.env.vco.reference", lines)
        # Collision-safety: NO bare dir-glob for namespaces the user can own.
        # The user's pre-existing .vscode/settings.json was MERGED (not created
        # by VCO), so neither the dir nor the user's file may be excluded.
        self.assertNotIn("/.vscode/", lines)
        self.assertNotIn("/.vscode/settings.json", lines)
        self.assertNotIn("/knowledge/", lines)
        # infrastructure/ files VCO copied are excluded SPECIFICALLY (per-file),
        # never as the bare `/infrastructure/` dir-glob.
        self.assertNotIn("/infrastructure/", lines)

    def test_tracked_gitignore_untouched(self):
        self._write_env_sidecar()
        self._run(safe_add=True)
        self.assertEqual(
            self.gitignore.read_text(encoding="utf-8"),
            self.gitignore_original,
            "safe-add must NEVER modify the tracked .gitignore",
        )

    def test_git_exclude_deferral_emitted(self):
        self._write_env_sidecar()
        self._run(safe_add=True)
        self.assertIn("safe_add_git_exclude_updated", self._deferral_ids())

    def test_git_info_exclude_idempotent(self):
        """A second safe-add run must not duplicate the exclude lines."""
        self._write_env_sidecar()
        self._run(safe_add=True)
        first = self.exclude_path.read_text(encoding="utf-8")
        # Second run (sidecar still present).
        self._run(safe_add=True)
        second = self.exclude_path.read_text(encoding="utf-8")
        # The VCO block should appear exactly once.
        self.assertEqual(
            first.count("/.claude/"), 1,
        )
        self.assertEqual(
            second.count("/.claude/"), 1,
            "re-running safe-add must not duplicate .git/info/exclude lines",
        )

    def test_no_git_repo_is_soft_noop(self):
        """If there's no .git dir, the exclude step is a no-op (no crash)."""
        import shutil
        shutil.rmtree(self.proj / ".git")
        self._write_env_sidecar()
        result = self._run(safe_add=True)
        self.assertEqual(
            result.get("safe_add_git_exclude", {}).get("action"),
            "not_a_git_repo",
        )
        # The env deferral still fires (it doesn't depend on git).
        self.assertIn("safe_add_skipped_env_merge", self._deferral_ids())
        # No git-exclude deferral when nothing was appended.
        self.assertNotIn("safe_add_git_exclude_updated", self._deferral_ids())

    # --- safe_add=False (default) --------------------------------------

    def test_default_off_no_safe_add_deferrals(self):
        # No sidecar written (default flow doesn't write one).
        self._run(safe_add=False)
        ids = self._deferral_ids()
        self.assertNotIn("safe_add_skipped_env_merge", ids)
        self.assertNotIn("safe_add_git_exclude_updated", ids)

    def test_default_off_git_info_exclude_covered_by_g1(self):
        """v0.2.75 P2 (G1 widened): a non-safe fresh add gets the SAME
        local-only `.git/info/exclude` coverage — via the G1 block, not
        the safe-add branch (so no safe-add deferrals; see
        test_default_off_no_safe_add_deferrals). Pre-v0.2.75 this test
        pinned the gap: the default add left VCO paths committable until
        the first bundle update."""
        result = self._run(safe_add=False)
        self.assertIn("g1_git_exclude", result)
        self.assertTrue(
            self.exclude_path.exists()
            and "/.claude/" in self.exclude_path.read_text(encoding="utf-8"),
            "non-safe add must ALSO keep VCO paths out of the user's commits",
        )

    def test_default_off_settings_still_merges(self):
        """Sanity: settings.json merge behaviour is identical to today."""
        result = self._run(safe_add=False)
        self.assertEqual(result["settings_action"], "merged")


class SafeAddGitExcludeUnitTests(unittest.TestCase):
    """Direct unit tests for the `_append_git_info_exclude` primitive."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-gitex-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    _SAMPLE_PATHS = ("/.claude/", "/.vco-manifest.json", "/CLAUDE.md")

    def test_not_a_git_repo(self):
        res = project_init._append_git_info_exclude(
            self.tmp, self._SAMPLE_PATHS,
        )
        self.assertEqual(res["action"], "not_a_git_repo")
        self.assertEqual(res["added"], [])

    def test_git_file_pointer_is_skipped(self):
        # Worktree/submodule layout: `.git` is a FILE, not a dir → skip.
        (self.tmp / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        res = project_init._append_git_info_exclude(
            self.tmp, self._SAMPLE_PATHS,
        )
        self.assertEqual(res["action"], "not_a_git_repo")

    def test_appended_then_noop(self):
        (self.tmp / ".git").mkdir()
        res1 = project_init._append_git_info_exclude(
            self.tmp, ("/.claude/", "/CLAUDE.md"),
        )
        self.assertEqual(res1["action"], "appended")
        self.assertEqual(set(res1["added"]), {"/.claude/", "/CLAUDE.md"})
        # Second call → everything present → noop.
        res2 = project_init._append_git_info_exclude(
            self.tmp, ("/.claude/", "/CLAUDE.md"),
        )
        self.assertEqual(res2["action"], "noop")
        self.assertEqual(res2["added"], [])

    def test_preserves_existing_exclude_content(self):
        git_dir = self.tmp / ".git"
        (git_dir / "info").mkdir(parents=True)
        existing = "# user line\nbuild/\n"
        (git_dir / "info" / "exclude").write_text(existing, encoding="utf-8")
        project_init._append_git_info_exclude(self.tmp, ("/CLAUDE.md",))
        text = (git_dir / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn("# user line", text)
        self.assertIn("build/", text)
        self.assertIn("/CLAUDE.md", text)


if __name__ == "__main__":
    if platform.system() == "Windows":
        # The tests are OS-agnostic, but skip is harmless to note.
        pass
    unittest.main()
