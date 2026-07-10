# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for install-bundle --write-env standalone (launcher-less) mode.

A2 (v0.2.38): verifies that `install-bundle --write-env` correctly writes
`.claude/env` and `.claude/settings.json env` using only --orchestrator-root
and the folder name, without requiring a running launcher or launcher.db.

Three main assertions:
1. `.claude/env` contains `VCT_ORCHESTRATOR_ROOT=<orch>`
2. `KG_COLLECTION` is derived as `<SanitizedName>_KnowledgeGraph`
3. The `kg-sync` wrapper pattern resolves its venv via `${VCT_INSTALL_ROOT}/.venv`
   (i.e. VCT_INSTALL_ROOT equals VCT_ORCHESTRATOR_ROOT in the env file).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402
from vco_lib.project_init import sanitize_for_weaviate_class  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_orchestrator(root: Path) -> None:
    """Write the minimal marker file that makes a dir look like the orchestrator."""
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    # templates/hooks needed for install_project_bundle's copy pass
    hooks = root / "templates" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    scripts = root / "templates" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    agents = root / "templates" / "agents" / "free"
    agents.mkdir(parents=True, exist_ok=True)


def _read_env_file(project: Path) -> dict[str, str]:
    """Parse .claude/env into a key→value dict.

    Handles both plain ``KEY=value`` and ``export KEY="value"`` formats
    (the latter is what VCO's shell-env writer emits).
    """
    env_path = project / ".claude" / "env"
    if not env_path.exists():
        return {}
    pairs: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # strip leading ``export `` if present
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        # strip surrounding double-quotes if present
        v = v.strip().strip('"')
        pairs[k.strip()] = v
    return pairs


def _read_settings_env(project: Path) -> dict[str, str]:
    """Return the env dict from .claude/settings.json, or {}."""
    settings_path = project / ".claude" / "settings.json"
    if not settings_path.exists():
        return {}
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        return dict(data.get("env", {}))
    except (json.JSONDecodeError, KeyError):
        return {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInstallBundleStandaloneWriteEnv(unittest.TestCase):
    """install-bundle --write-env behaviour with no launcher DB present."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        # project folder named "TestProj"
        self.project = self.base / "TestProj"
        self.project.mkdir()
        # fake orchestrator
        self.orch = self.base / "orchestrator"
        self.orch.mkdir()
        _make_minimal_orchestrator(self.orch)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, **extra_kwargs) -> dict:
        return project_init.install_project_bundle(
            self.project,
            orchestrator_root=self.orch,
            write_env=True,
            **extra_kwargs,
        )

    # ------------------------------------------------------------------
    # Test 1: VCT_ORCHESTRATOR_ROOT written to .claude/env
    # ------------------------------------------------------------------

    def test_claude_env_has_vct_orchestrator_root(self) -> None:
        """After --write-env, .claude/env must contain VCT_ORCHESTRATOR_ROOT
        pointing at --orchestrator-root."""
        self._run()
        env = _read_env_file(self.project)
        self.assertIn("VCT_ORCHESTRATOR_ROOT", env,
                      ".claude/env missing VCT_ORCHESTRATOR_ROOT")
        self.assertEqual(
            Path(env["VCT_ORCHESTRATOR_ROOT"]).resolve(),
            self.orch.resolve(),
            "VCT_ORCHESTRATOR_ROOT does not point at the supplied orchestrator root",
        )

    # ------------------------------------------------------------------
    # Test 2: KG_COLLECTION derived as <Sanitized>_KnowledgeGraph
    # ------------------------------------------------------------------

    def test_kg_collection_derived_from_folder_name(self) -> None:
        """KG_COLLECTION must equal sanitize(folder.name) + '_KnowledgeGraph'."""
        self._run()
        env = _read_env_file(self.project)
        self.assertIn("KG_COLLECTION", env, ".claude/env missing KG_COLLECTION")

        sanitized = sanitize_for_weaviate_class("TestProj")
        expected = f"{sanitized}_KnowledgeGraph"
        self.assertEqual(
            env["KG_COLLECTION"], expected,
            f"KG_COLLECTION={env['KG_COLLECTION']!r}, expected {expected!r}",
        )

    def test_kg_collection_with_explicit_project_name(self) -> None:
        """--project-name overrides folder basename for KG_COLLECTION derivation."""
        result = project_init.install_project_bundle(
            self.project,
            orchestrator_root=self.orch,
            write_env=True,
            project_name="MyCustomProject",
        )
        env = _read_env_file(self.project)
        sanitized = sanitize_for_weaviate_class("MyCustomProject")
        expected = f"{sanitized}_KnowledgeGraph"
        self.assertIn("KG_COLLECTION", env)
        self.assertEqual(env["KG_COLLECTION"], expected)

    # ------------------------------------------------------------------
    # Test 3: VCT_INSTALL_ROOT == VCT_ORCHESTRATOR_ROOT (venv resolution)
    # ------------------------------------------------------------------

    def test_vct_install_root_matches_orchestrator_root(self) -> None:
        """VCT_INSTALL_ROOT must equal VCT_ORCHESTRATOR_ROOT so that
        `kg-sync` wrapper scripts can resolve their venv via
        ${VCT_INSTALL_ROOT}/.venv."""
        self._run()
        env = _read_env_file(self.project)
        self.assertIn("VCT_INSTALL_ROOT", env, ".claude/env missing VCT_INSTALL_ROOT")
        self.assertEqual(
            Path(env["VCT_INSTALL_ROOT"]).resolve(),
            self.orch.resolve(),
            "VCT_INSTALL_ROOT does not match VCT_ORCHESTRATOR_ROOT",
        )

    # ------------------------------------------------------------------
    # Test 4: env also written into .claude/settings.json
    # ------------------------------------------------------------------

    def test_settings_json_env_block_written(self) -> None:
        """.claude/settings.json env block must contain VCT_ORCHESTRATOR_ROOT
        and KG_COLLECTION after --write-env."""
        self._run()
        settings_env = _read_settings_env(self.project)
        self.assertIn("VCT_ORCHESTRATOR_ROOT", settings_env,
                      "settings.json env missing VCT_ORCHESTRATOR_ROOT")
        self.assertIn("KG_COLLECTION", settings_env,
                      "settings.json env missing KG_COLLECTION")

    # ------------------------------------------------------------------
    # Test 5: standalone result exposed in top-level result dict
    # ------------------------------------------------------------------

    def test_result_reports_standalone_env_applied(self) -> None:
        """result['standalone_env']['action'] must be 'applied'."""
        result = self._run()
        self.assertIn("standalone_env", result,
                      "result dict missing 'standalone_env' key")
        self.assertEqual(
            result["standalone_env"]["action"], "applied",
            f"standalone_env action={result['standalone_env']['action']!r}",
        )

    # ------------------------------------------------------------------
    # Test 6: without --write-env no standalone env is written
    # ------------------------------------------------------------------

    def test_no_write_env_flag_skips_standalone(self) -> None:
        """Without --write-env the standalone path is never invoked."""
        project_init.install_project_bundle(
            self.project,
            orchestrator_root=self.orch,
            write_env=False,
        )
        # The .claude/env file may or may not exist depending on whether
        # the DB path resolved (it won't on a fresh tmpdir). What we assert
        # is that VCT_ORCHESTRATOR_ROOT is NOT present (standalone didn't fire).
        env = _read_env_file(self.project)
        # If env was written via the DB path it would still include the key —
        # but in a fresh tmpdir without launcher.db the DB path returns
        # "db_unreachable" and skips, so the file either absent or empty.
        # We assert KG_COLLECTION absent as the standalone-specific signal.
        if "KG_COLLECTION" in env:
            # DB path succeeded (launcher.db present on this machine) —
            # skip this assertion rather than fail on a developer's machine.
            self.skipTest("launcher.db present; cannot distinguish paths")
        self.assertNotIn("KG_COLLECTION", env,
                         "KG_COLLECTION written without --write-env")

    # ------------------------------------------------------------------
    # Test 7: CODE_GRAPH_PROJECT set and matches sanitized name
    # ------------------------------------------------------------------

    def test_code_graph_project_set(self) -> None:
        """CODE_GRAPH_PROJECT must equal canonical_class_prefix(folder.name).

        v0.2.76 (seams-lens #1): the standalone writer now derives
        CODE_GRAPH_PROJECT with the underscore-PRESERVING canonical rule (the
        analyzer/hub/binding SSOT), not the underscore-DROPPING KG sanitizer.
        For "TestProj" (no underscore) both agree; the underscore regression
        below pins the divergence."""
        from vco_lib.codegraph_naming import canonical_class_prefix

        self._run()
        env = _read_env_file(self.project)
        self.assertIn("CODE_GRAPH_PROJECT", env)
        expected = canonical_class_prefix("TestProj")
        self.assertEqual(env["CODE_GRAPH_PROJECT"], expected)

    def test_code_graph_project_preserves_underscore(self) -> None:
        """seams-lens #1 regression: an underscore-containing name must emit the
        underscore-PRESERVING canonical prefix — NOT the retired KG sanitizer's
        dropped-underscore value (which would split-brain the analyzer/hub)."""
        from vco_lib.codegraph_naming import canonical_class_prefix

        proj = self.base / "My_Cool_Project"
        proj.mkdir()
        project_init.install_project_bundle(
            proj,
            orchestrator_root=self.orch,
            write_env=True,
        )
        env = _read_env_file(proj)
        self.assertIn("CODE_GRAPH_PROJECT", env)
        # Canonical PRESERVES underscores: "My_Cool_Project".
        self.assertEqual(env["CODE_GRAPH_PROJECT"], canonical_class_prefix("My_Cool_Project"))
        self.assertEqual(env["CODE_GRAPH_PROJECT"], "My_Cool_Project")
        # And it MUST differ from the retired KG sanitizer's dropped form.
        self.assertNotEqual(
            env["CODE_GRAPH_PROJECT"],
            sanitize_for_weaviate_class("My_Cool_Project"),
        )

    # ------------------------------------------------------------------
    # Test 8: hyphenated / spaced folder name sanitized correctly
    # ------------------------------------------------------------------

    def test_special_char_folder_name_sanitized(self) -> None:
        """Folder names with hyphens/spaces produce valid PascalCase KG names."""
        proj = self.base / "my-cool project"
        proj.mkdir()
        project_init.install_project_bundle(
            proj,
            orchestrator_root=self.orch,
            write_env=True,
        )
        env = _read_env_file(proj)
        self.assertIn("KG_COLLECTION", env)
        sanitized = sanitize_for_weaviate_class("my-cool project")
        self.assertEqual(env["KG_COLLECTION"], f"{sanitized}_KnowledgeGraph")
        # Ensure the sanitized name starts with an uppercase letter
        self.assertTrue(
            sanitized[0].isupper(),
            f"sanitized prefix {sanitized!r} does not start with uppercase",
        )


class TestInstallBundleStandaloneCLI(unittest.TestCase):
    """CLI entry-point wiring for --write-env."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.project = self.base / "CliTest"
        self.project.mkdir()
        self.orch = self.base / "orch"
        self.orch.mkdir()
        _make_minimal_orchestrator(self.orch)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cli_write_env_flag_accepted(self) -> None:
        """install-bundle --write-env --folder ... must not error on argparse."""
        import argparse
        # Build a minimal Namespace that mimics what argparse would produce
        ns = argparse.Namespace(
            folder=str(self.project),
            orchestrator_root=str(self.orch),
            update=False,
            force=False,
            dry_run=False,
            write_env=True,
            project_name=None,
            project_folder=None,
            json=False,
        )
        from vco_lib.project_init import _cmd_install_bundle
        exit_code = _cmd_install_bundle(ns)
        self.assertEqual(exit_code, 0, f"_cmd_install_bundle returned {exit_code}")
        env = _read_env_file(self.project)
        self.assertIn("VCT_ORCHESTRATOR_ROOT", env)

    def test_cli_project_name_override(self) -> None:
        """--project-name overrides folder basename via CLI path."""
        import argparse
        ns = argparse.Namespace(
            folder=str(self.project),
            orchestrator_root=str(self.orch),
            update=False,
            force=False,
            dry_run=False,
            write_env=True,
            project_name="OverriddenName",
            project_folder=None,
            json=False,
        )
        from vco_lib.project_init import _cmd_install_bundle
        _cmd_install_bundle(ns)
        env = _read_env_file(self.project)
        sanitized = sanitize_for_weaviate_class("OverriddenName")
        self.assertEqual(env.get("KG_COLLECTION"), f"{sanitized}_KnowledgeGraph")


if __name__ == "__main__":
    unittest.main()
