"""Tests for the CODE_GRAPH_PROJECT / PROJECT_NAME env-block plumbing
(PR-7 / v0.2.11 — project-name + KG_COLLECTION resolution fix).

Covers two surfaces:

  1. install.py:
     - `_derive_orchestrator_project_name()` — reads vct-module.json::name,
       sanitizes via project_init.sanitize_for_weaviate_class.
     - `_backfill_code_graph_project_env()` — idempotent migration of a
       pre-PR-7 .claude/settings.json::env block that lacks PROJECT_NAME /
       CODE_GRAPH_PROJECT keys.

  2. vco_lib.project_init:
     - `_backfill_code_graph_project_env_in_project()` — per-project
       equivalent of the orchestrator-side backfill. Idempotent contract
       under `install-bundle --update`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from vco_lib import project_init  # noqa: E402


class DeriveOrchestratorProjectNameTests(unittest.TestCase):
    """`_derive_orchestrator_project_name` resolves the canonical
    Weaviate-safe orchestrator project name. Reads vct-module.json::name
    and runs it through the shared sanitizer. Falls back to
    "VibeCodedOrchestrator" if the manifest is missing / unparseable /
    yields the sanitizer's fallback prefix.
    """

    def test_returns_sanitized_manifest_name(self):
        # The actual on-disk manifest carries `"name": "VibeCoded Orchestrator"`.
        # Through sanitize_for_weaviate_class that becomes "VibeCodedOrchestrator".
        result = install._derive_orchestrator_project_name()
        self.assertEqual(result, "VibeCodedOrchestrator")

    def test_fallback_when_manifest_missing(self):
        # Simulate the manifest being absent: patch PROJECT_ROOT to a
        # tmpdir that has no vct-module.json. The function should fall
        # back to the hardcoded "VibeCodedOrchestrator" rather than
        # raising.
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            with mock.patch.object(install, "PROJECT_ROOT", tmp_root):
                result = install._derive_orchestrator_project_name()
            self.assertEqual(result, "VibeCodedOrchestrator")

    def test_fallback_when_manifest_unparseable(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            (tmp_root / "vct-module.json").write_text(
                "not-valid-json{{}}", encoding="utf-8",
            )
            with mock.patch.object(install, "PROJECT_ROOT", tmp_root):
                result = install._derive_orchestrator_project_name()
            self.assertEqual(result, "VibeCodedOrchestrator")

    def test_fallback_when_manifest_name_empty(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            (tmp_root / "vct-module.json").write_text(
                json.dumps({"name": ""}), encoding="utf-8",
            )
            with mock.patch.object(install, "PROJECT_ROOT", tmp_root):
                result = install._derive_orchestrator_project_name()
            self.assertEqual(result, "VibeCodedOrchestrator")

    def test_fallback_when_sanitizer_returns_prefix(self):
        # A name with no alphanumeric chars passes through sanitize_for_
        # weaviate_class and returns _FALLBACK_PREFIX ("vct"). The
        # orchestrator-derive function rejects this and uses its own
        # fallback so we never write the per-project sentinel into the
        # orchestrator's env block.
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            (tmp_root / "vct-module.json").write_text(
                json.dumps({"name": "..."}), encoding="utf-8",
            )
            with mock.patch.object(install, "PROJECT_ROOT", tmp_root):
                result = install._derive_orchestrator_project_name()
            self.assertEqual(result, "VibeCodedOrchestrator")

    def test_custom_name_is_pascal_cased(self):
        # Confirm the sanitizer pipeline is applied: "my-fancy-orch" →
        # "MyFancyOrch", not the raw string.
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            (tmp_root / "vct-module.json").write_text(
                json.dumps({"name": "my-fancy-orch"}), encoding="utf-8",
            )
            with mock.patch.object(install, "PROJECT_ROOT", tmp_root):
                result = install._derive_orchestrator_project_name()
            self.assertEqual(result, "MyFancyOrch")


class BackfillCodeGraphProjectEnvTests(unittest.TestCase):
    """`_backfill_code_graph_project_env` — idempotent fill-in of two
    missing keys in `.claude/settings.json::env`.

    Contract:
      - Missing settings file → action="missing" (no-op).
      - File unparseable → action="unparseable" (no-op, preserves user file).
      - Both keys present → action="noop".
      - Missing `env` block → creates it with both keys.
      - One or both keys missing → action="backfilled", added_keys lists
        the keys that were just written.
    """

    def _write_settings(self, td: Path, env_block: dict | None) -> Path:
        f = td / ".claude" / "settings.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {"permissions": {"allow": []}}
        if env_block is not None:
            payload["env"] = env_block
        f.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return f

    def test_missing_file_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / ".claude" / "settings.json"
            result = install._backfill_code_graph_project_env(f)
            self.assertEqual(result["action"], "missing")
            self.assertEqual(result["added_keys"], [])

    def test_unparseable_file_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "settings.json"
            f.write_text("not json {{}}", encoding="utf-8")
            result = install._backfill_code_graph_project_env(f)
            self.assertEqual(result["action"], "unparseable")
            self.assertEqual(result["added_keys"], [])

    def test_both_keys_already_present_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._write_settings(
                Path(td),
                {"PROJECT_NAME": "Foo", "CODE_GRAPH_PROJECT": "Foo",
                 "KG_COLLECTION": "Foo_KnowledgeGraph"},
            )
            result = install._backfill_code_graph_project_env(f)
            self.assertEqual(result["action"], "noop")
            self.assertEqual(result["added_keys"], [])
            # User-set values preserved verbatim.
            data = json.loads(f.read_text(encoding="utf-8"))
            self.assertEqual(data["env"]["PROJECT_NAME"], "Foo")
            self.assertEqual(data["env"]["CODE_GRAPH_PROJECT"], "Foo")

    def test_missing_env_block_creates_it(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._write_settings(Path(td), None)  # No env block.
            result = install._backfill_code_graph_project_env(f)
            self.assertEqual(result["action"], "backfilled")
            self.assertEqual(sorted(result["added_keys"]),
                             ["CODE_GRAPH_PROJECT", "PROJECT_NAME"])
            data = json.loads(f.read_text(encoding="utf-8"))
            self.assertIn("env", data)
            self.assertIn("PROJECT_NAME", data["env"])
            self.assertIn("CODE_GRAPH_PROJECT", data["env"])

    def test_both_keys_missing_fills_both(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._write_settings(
                Path(td), {"BASH_ENV": "/some/path"},
            )
            result = install._backfill_code_graph_project_env(f)
            self.assertEqual(result["action"], "backfilled")
            self.assertEqual(sorted(result["added_keys"]),
                             ["CODE_GRAPH_PROJECT", "PROJECT_NAME"])
            data = json.loads(f.read_text(encoding="utf-8"))
            # Existing user-set key preserved.
            self.assertEqual(data["env"]["BASH_ENV"], "/some/path")

    def test_only_project_name_missing_fills_one(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._write_settings(
                Path(td),
                {"CODE_GRAPH_PROJECT": "Foo"},
            )
            result = install._backfill_code_graph_project_env(f)
            self.assertEqual(result["action"], "backfilled")
            self.assertEqual(result["added_keys"], ["PROJECT_NAME"])
            data = json.loads(f.read_text(encoding="utf-8"))
            # User value preserved on the already-present key.
            self.assertEqual(data["env"]["CODE_GRAPH_PROJECT"], "Foo")

    def test_only_code_graph_project_missing_fills_one(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._write_settings(
                Path(td),
                {"PROJECT_NAME": "Foo"},
            )
            result = install._backfill_code_graph_project_env(f)
            self.assertEqual(result["action"], "backfilled")
            self.assertEqual(result["added_keys"], ["CODE_GRAPH_PROJECT"])
            data = json.loads(f.read_text(encoding="utf-8"))
            self.assertEqual(data["env"]["PROJECT_NAME"], "Foo")

    def test_idempotent_under_repeat_invocation(self):
        # Running the backfill twice in a row must produce noop on the
        # second call (every key already present after the first).
        with tempfile.TemporaryDirectory() as td:
            f = self._write_settings(Path(td), {})
            first = install._backfill_code_graph_project_env(f)
            second = install._backfill_code_graph_project_env(f)
            self.assertEqual(first["action"], "backfilled")
            self.assertEqual(second["action"], "noop")
            self.assertEqual(second["added_keys"], [])

    def test_user_set_value_never_overwritten(self):
        # Critical safety: backfill MUST NOT touch existing values, even
        # if they look "wrong" (e.g. user pinned a custom basename).
        with tempfile.TemporaryDirectory() as td:
            f = self._write_settings(
                Path(td),
                {"PROJECT_NAME": "UserCustomName",
                 "CODE_GRAPH_PROJECT": "AnotherCustomName"},
            )
            result = install._backfill_code_graph_project_env(f)
            self.assertEqual(result["action"], "noop")
            data = json.loads(f.read_text(encoding="utf-8"))
            self.assertEqual(data["env"]["PROJECT_NAME"], "UserCustomName")
            self.assertEqual(data["env"]["CODE_GRAPH_PROJECT"], "AnotherCustomName")


class BackfillInProjectTests(unittest.TestCase):
    """`_backfill_code_graph_project_env_in_project` — per-project
    backfill for `install-bundle --update`. Same idempotency contract as
    the orchestrator-side helper, with one extra branch for resolving
    the project name from the existing KG_COLLECTION / folder basename.
    """

    def _write_proj_settings(self, folder: Path, env_block: dict | None) -> Path:
        f = folder / ".claude" / "settings.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {"permissions": {"allow": []}}
        if env_block is not None:
            payload["env"] = env_block
        f.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return f

    def test_missing_settings_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            result = project_init._backfill_code_graph_project_env_in_project(folder)
            self.assertEqual(result["action"], "missing")
            self.assertEqual(result["added_keys"], [])

    def test_unparseable_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            f = folder / ".claude" / "settings.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("not json", encoding="utf-8")
            result = project_init._backfill_code_graph_project_env_in_project(folder)
            self.assertEqual(result["action"], "unparseable")

    def test_explicit_project_name_wins(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._write_proj_settings(folder, {"KG_COLLECTION": "Existing_KnowledgeGraph"})
            result = project_init._backfill_code_graph_project_env_in_project(
                folder, project_name="ExplicitOverride",
            )
            self.assertEqual(result["action"], "backfilled")
            self.assertEqual(result["resolved_name"], "ExplicitOverride")
            data = json.loads((folder / ".claude" / "settings.json").read_text())
            self.assertEqual(data["env"]["PROJECT_NAME"], "ExplicitOverride")
            self.assertEqual(data["env"]["CODE_GRAPH_PROJECT"], "ExplicitOverride")

    def test_derives_name_from_kg_collection(self):
        # KG_COLLECTION "ARTup_KnowledgeGraph" → derived basename "ARTup".
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._write_proj_settings(
                folder, {"KG_COLLECTION": "ARTup_KnowledgeGraph"},
            )
            result = project_init._backfill_code_graph_project_env_in_project(folder)
            self.assertEqual(result["action"], "backfilled")
            self.assertEqual(result["resolved_name"], "ARTup")

    def test_derives_name_from_existing_project_name_field(self):
        # PROJECT_NAME present but CODE_GRAPH_PROJECT missing → CGP picks
        # up PROJECT_NAME's value so the two stay in sync.
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._write_proj_settings(folder, {"PROJECT_NAME": "AlreadyHere"})
            result = project_init._backfill_code_graph_project_env_in_project(folder)
            self.assertEqual(result["action"], "backfilled")
            self.assertEqual(result["added_keys"], ["CODE_GRAPH_PROJECT"])
            self.assertEqual(result["resolved_name"], "AlreadyHere")

    def test_derives_name_from_folder_basename_as_last_resort(self):
        # No env-side hints → folder.name sanitized via the shared
        # sanitizer (last-resort fallback).
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            folder = parent / "my-project"
            folder.mkdir()
            self._write_proj_settings(folder, {})
            result = project_init._backfill_code_graph_project_env_in_project(folder)
            self.assertEqual(result["action"], "backfilled")
            self.assertEqual(result["resolved_name"], "MyProject")

    def test_idempotent_under_repeat_invocation(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._write_proj_settings(folder, {})
            first = project_init._backfill_code_graph_project_env_in_project(folder)
            second = project_init._backfill_code_graph_project_env_in_project(folder)
            self.assertEqual(first["action"], "backfilled")
            self.assertEqual(second["action"], "noop")

    def test_both_keys_present_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._write_proj_settings(
                folder,
                {"PROJECT_NAME": "X", "CODE_GRAPH_PROJECT": "X"},
            )
            result = project_init._backfill_code_graph_project_env_in_project(folder)
            self.assertEqual(result["action"], "noop")
            self.assertEqual(result["added_keys"], [])


class VscodeExcludesBackfillTests(unittest.TestCase):
    """`_backfill_vscode_excludes_in_project` — addendum-4 to PR-7.

    Forensic context: VS Code OOM-kill on workspaces >10 GB / >50k
    files when the canonical watcher/search/Pylance exclude block is
    missing. The backfill ships the block on every `install-bundle`
    pass; existing projects catch up via the user-wins merge semantics.
    """

    CANONICAL_KEYS = {
        "files.watcherExclude",
        "files.exclude",
        "search.exclude",
        "python.analysis.exclude",
        "python.analysis.indexing",
    }

    def _write_vscode(self, folder: Path, payload: dict | None) -> Path:
        f = folder / ".vscode" / "settings.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        if payload is not None:
            f.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return f

    def test_fresh_create_writes_all_canonical_keys(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            result = project_init._backfill_vscode_excludes_in_project(folder)
            self.assertEqual(result["action"], "created")
            self.assertEqual(set(result["added_keys"]), self.CANONICAL_KEYS)
            # File now exists and contains the canonical block.
            data = json.loads(
                (folder / ".vscode" / "settings.json").read_text(encoding="utf-8")
            )
            for key in self.CANONICAL_KEYS:
                self.assertIn(key, data, f"missing canonical key {key}")
            # Specific assertion: watcherExclude includes the high-churn dirs
            # that triggered the 2026-05-16 OOM (cargo target/, .venv, etc.).
            we = data["files.watcherExclude"]
            self.assertTrue(we.get("**/target/**"))
            self.assertTrue(we.get("**/.venv/**"))
            self.assertTrue(we.get("**/__pycache__/**"))
            # Pylance indexing disabled — critical for the OOM fix.
            self.assertEqual(data["python.analysis.indexing"], False)

    def test_unparseable_file_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            f = folder / ".vscode" / "settings.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            # Common case: trailing comma in hand-edited JSONC. Standard
            # json.loads rejects → action=unparseable, user file untouched.
            f.write_text('{"some": "key",}', encoding="utf-8")
            result = project_init._backfill_vscode_excludes_in_project(folder)
            self.assertEqual(result["action"], "unparseable")
            self.assertEqual(result["added_keys"], [])
            # File contents preserved verbatim.
            self.assertEqual(f.read_text(encoding="utf-8"), '{"some": "key",}')

    def test_existing_file_missing_keys_backfilled(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            # Simulate a project that has `.vscode/settings.json` for the
            # claude-code.env block but no exclude block (pre-v0.2.11 state).
            self._write_vscode(folder, {
                "claude-code.env": {"WEAVIATE_URL": "http://localhost:8081"},
                "editor.tabSize": 2,
            })
            result = project_init._backfill_vscode_excludes_in_project(folder)
            self.assertEqual(result["action"], "backfilled")
            self.assertEqual(set(result["added_keys"]), self.CANONICAL_KEYS)
            # User's claude-code.env block preserved verbatim.
            data = json.loads(
                (folder / ".vscode" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(data["claude-code.env"]["WEAVIATE_URL"],
                             "http://localhost:8081")
            self.assertEqual(data["editor.tabSize"], 2)
            # And the exclude block is now present.
            for key in self.CANONICAL_KEYS:
                self.assertIn(key, data)

    def test_existing_file_with_excludes_is_noop_user_wins(self):
        # User already set files.watcherExclude (perhaps via the UI). The
        # backfill MUST NOT touch it, even if the value differs from our
        # canonical defaults — that's the user-wins contract.
        user_excludes = {"**/custom-dir/**": True}
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._write_vscode(folder, {
                "files.watcherExclude": user_excludes,
                "files.exclude": {},  # User explicitly emptied.
                "search.exclude": {"**/my-search-skip": True},
                "python.analysis.exclude": ["**/custom/**"],
                "python.analysis.indexing": True,  # User chose ON.
            })
            result = project_init._backfill_vscode_excludes_in_project(folder)
            self.assertEqual(result["action"], "noop")
            self.assertEqual(result["added_keys"], [])
            data = json.loads(
                (folder / ".vscode" / "settings.json").read_text(encoding="utf-8")
            )
            # Every user value preserved.
            self.assertEqual(data["files.watcherExclude"], user_excludes)
            self.assertEqual(data["files.exclude"], {})
            self.assertEqual(data["search.exclude"], {"**/my-search-skip": True})
            self.assertEqual(data["python.analysis.exclude"], ["**/custom/**"])
            self.assertEqual(data["python.analysis.indexing"], True)

    def test_partial_backfill_only_adds_missing(self):
        # User has files.watcherExclude but no Pylance block. Backfill
        # adds only the missing keys; the present key is preserved.
        user_we = {"**/user-defined/**": True}
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self._write_vscode(folder, {"files.watcherExclude": user_we})
            result = project_init._backfill_vscode_excludes_in_project(folder)
            self.assertEqual(result["action"], "backfilled")
            missing = self.CANONICAL_KEYS - {"files.watcherExclude"}
            self.assertEqual(set(result["added_keys"]), missing)
            data = json.loads(
                (folder / ".vscode" / "settings.json").read_text(encoding="utf-8")
            )
            # User key preserved exactly.
            self.assertEqual(data["files.watcherExclude"], user_we)
            # Other canonical keys filled in.
            self.assertIn("python.analysis.indexing", data)

    def test_idempotent_under_repeat_invocation(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            first = project_init._backfill_vscode_excludes_in_project(folder)
            second = project_init._backfill_vscode_excludes_in_project(folder)
            self.assertEqual(first["action"], "created")
            self.assertEqual(second["action"], "noop")
            self.assertEqual(second["added_keys"], [])

    def test_install_py_mirror_delegates_to_same_helper(self):
        # The orchestrator-side `install._backfill_vscode_excludes` is a
        # thin wrapper around the project_init helper. Verify it routes
        # correctly when given an explicit settings_file path.
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            settings_file = folder / ".vscode" / "settings.json"
            result = install._backfill_vscode_excludes(settings_file)
            self.assertEqual(result["action"], "created")
            self.assertTrue(settings_file.exists())
            data = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertEqual(data["python.analysis.indexing"], False)


class PostFileEditHookEnvResolutionTests(unittest.TestCase):
    """Functional check on `post-file-edit.sh`: the resolved
    CODE_GRAPH_PROJECT_RESOLVED value is what the hook would pass as
    arg 3 to `code-graph-incremental.sh`. We isolate the resolution
    expression itself by sourcing only the relevant bash logic — no
    actual code-graph subprocess invocation.

    This catches regressions where a future edit re-introduces the
    "ClaudeOrchestrator" literal or breaks the env-fallback chain.
    """

    HOOK_PATHS = [
        REPO_ROOT / "templates" / "hooks" / "post-file-edit.sh",
        REPO_ROOT / ".claude" / "hooks" / "post-file-edit.sh",
    ]

    def test_no_hardcoded_claudeorchestrator_in_hook_bodies(self):
        # Defence-in-depth: parser-level guarantee no shipped hook still
        # carries the legacy literal as an active (non-comment) string.
        for path in self.HOOK_PATHS:
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                self.assertNotIn(
                    '"ClaudeOrchestrator"',
                    line,
                    f"{path}:{line_no} still references ClaudeOrchestrator: {line!r}",
                )

    def test_hook_resolves_explicit_code_graph_project_env(self):
        for path in self.HOOK_PATHS:
            # Extract the one-liner that builds CODE_GRAPH_PROJECT_RESOLVED.
            # We use the same parameter-expansion shape the hook emits.
            expr = (
                'CODE_GRAPH_PROJECT_RESOLVED='
                '"${CODE_GRAPH_PROJECT:-${PROJECT_NAME:-$(basename "$PROJECT_ROOT")}}"'
                '; echo "$CODE_GRAPH_PROJECT_RESOLVED"'
            )
            env = os.environ.copy()
            env["CODE_GRAPH_PROJECT"] = "TestProj"
            env["PROJECT_NAME"] = "ShouldBeIgnored"
            env["PROJECT_ROOT"] = "/tmp/should-also-be-ignored"
            res = subprocess.run(
                ["bash", "-c", expr], env=env,
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, f"bash failed: {res.stderr}")
            self.assertEqual(res.stdout.strip(), "TestProj")
            # Confirm the literal expression appears in the actual hook,
            # not just in our test string.
            hook_text = path.read_text(encoding="utf-8")
            self.assertIn(
                '"${CODE_GRAPH_PROJECT:-${PROJECT_NAME:-$(basename "$PROJECT_ROOT")}}"',
                hook_text,
                f"{path} no longer emits the canonical fallback expression",
            )

    def test_hook_falls_back_to_project_name(self):
        expr = (
            'CODE_GRAPH_PROJECT_RESOLVED='
            '"${CODE_GRAPH_PROJECT:-${PROJECT_NAME:-$(basename "$PROJECT_ROOT")}}"'
            '; echo "$CODE_GRAPH_PROJECT_RESOLVED"'
        )
        env = os.environ.copy()
        env.pop("CODE_GRAPH_PROJECT", None)
        env["PROJECT_NAME"] = "FromProjectName"
        env["PROJECT_ROOT"] = "/tmp/whatever"
        res = subprocess.run(
            ["bash", "-c", expr], env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "FromProjectName")

    def test_hook_falls_back_to_basename_as_last_resort(self):
        expr = (
            'CODE_GRAPH_PROJECT_RESOLVED='
            '"${CODE_GRAPH_PROJECT:-${PROJECT_NAME:-$(basename "$PROJECT_ROOT")}}"'
            '; echo "$CODE_GRAPH_PROJECT_RESOLVED"'
        )
        env = os.environ.copy()
        env.pop("CODE_GRAPH_PROJECT", None)
        env.pop("PROJECT_NAME", None)
        env["PROJECT_ROOT"] = "/tmp/whatever-project"
        res = subprocess.run(
            ["bash", "-c", expr], env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "whatever-project")


if __name__ == "__main__":
    unittest.main()
