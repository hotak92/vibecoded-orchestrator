# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for `_install_hooks_and_settings` and the smart-merge helpers in
install.py.

Covers the per-target-project hook distribution feature added 2026-04-28
to close the README "roadmap" gap (20 hooks were previously orchestrator-only).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import install  # type: ignore  # noqa: E402


class SmartMergeFreshProjectTest(unittest.TestCase):
    """When .claude/settings.json doesn't exist, the template is written
    verbatim (with an extra trailing newline) and every template hook command
    ends up registered."""

    def test_creates_file_from_template_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".claude" / "settings.json"
            template = REPO_ROOT / "templates" / "settings.json.template"
            self.assertTrue(template.exists(), "template must ship in repo")

            action = install._merge_settings_template(template, target)

            self.assertEqual(action, "created")
            self.assertTrue(target.exists())
            data = json.loads(target.read_text(encoding="utf-8"))
            # Every event from the template should be present.
            self.assertIn("hooks", data)
            for event in (
                "SessionStart", "PreCompact", "PostCompact", "UserPromptSubmit",
                "PreToolUse", "Stop", "PostToolUse",
            ):
                self.assertIn(event, data["hooks"], f"missing event: {event}")
            # Permissions baseline carried over.
            self.assertIn("permissions", data)
            self.assertIn("allow", data["permissions"])
            self.assertIn("deny", data["permissions"])
            # Origin marker preserved.
            self.assertIn("_template_origin", data)


class SmartMergePreservesUserSettingsTest(unittest.TestCase):
    """When the project already has a partial settings.json, user keys win,
    and template hook commands are appended without duplicating existing ones."""

    def test_merge_preserves_user_keys_and_appends_missing_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".claude" / "settings.json"
            target.parent.mkdir(parents=True)

            # User has a custom permission list and ONE existing hook command
            # that overlaps with the template (must NOT be duplicated).
            user_existing = {
                "permissions": {"allow": ["Bash(my-tool *)"]},
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo my-custom-stop-hook",
                                    "timeout": 3,
                                }
                            ]
                        }
                    ],
                },
                "myCustomKey": "do-not-touch",
            }
            target.write_text(json.dumps(user_existing, indent=2))

            template = REPO_ROOT / "templates" / "settings.json.template"
            action = install._merge_settings_template(template, target)
            self.assertEqual(action, "merged")

            merged = json.loads(target.read_text(encoding="utf-8"))

            # User scalar key preserved verbatim.
            self.assertEqual(merged.get("myCustomKey"), "do-not-touch")
            # User permission list is the dict value at allow — should be kept
            # since the user supplied it (template's allow list shouldn't replace it).
            self.assertIn("Bash(my-tool *)", merged["permissions"]["allow"])

            # Stop event: user's custom hook still there + template hooks appended
            # (cost-tracker.sh and notify-stop.sh) — user's command not duplicated.
            stop_entries = merged["hooks"]["Stop"]
            all_cmds = [
                h.get("command", "")
                for entry in stop_entries
                for h in entry.get("hooks", [])
            ]
            self.assertEqual(
                sum(1 for c in all_cmds if "my-custom-stop-hook" in c), 1,
                "user's custom hook must appear exactly once",
            )
            self.assertTrue(
                any("cost-tracker.sh" in c for c in all_cmds),
                "template's cost-tracker should be appended",
            )
            self.assertTrue(
                any("notify-stop.sh" in c for c in all_cmds),
                "template's notify-stop should be appended",
            )

            # Events the user didn't define should be added wholesale from template.
            self.assertIn("SessionStart", merged["hooks"])
            self.assertIn("PostToolUse", merged["hooks"])


class HooksDirectorySyntaxTest(unittest.TestCase):
    """Every shipped hook must be valid bash and must not blow up when invoked
    with VCT_INSTALL_ROOT unset (the "external project" case)."""

    def test_all_template_hooks_pass_bash_syntax_check(self) -> None:
        hooks_dir = REPO_ROOT / "templates" / "hooks"
        self.assertTrue(hooks_dir.exists())
        hooks = sorted(hooks_dir.glob("*.sh"))
        self.assertGreaterEqual(len(hooks), 15, "expected the full hook set")
        failures = []
        for h in hooks:
            result = subprocess.run(
                ["bash", "-n", str(h)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                failures.append(f"{h.name}: {result.stderr}")
        self.assertEqual(failures, [], "bash -n failed for: " + "\n".join(failures))


class InstallHooksAndSettingsIntegrationTest(unittest.TestCase):
    """End-to-end: exercise `_install_hooks_and_settings` against a fake
    PROJECT_ROOT and confirm hooks land in .claude/hooks/ and settings.json
    is written from the template."""

    def test_install_into_fresh_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            # Stage a fake "orchestrator install" by symlinking templates/.
            (tmp_root / "templates").symlink_to(REPO_ROOT / "templates")

            fake_args = argparse.Namespace(with_hooks=True)

            with patch.object(install, "PROJECT_ROOT", tmp_root):
                summary = install._install_hooks_and_settings(fake_args)

            self.assertIn("hooks", summary)
            self.assertIn("settings.json created", summary)

            hooks_dst = tmp_root / ".claude" / "hooks"
            self.assertTrue(hooks_dst.exists())
            installed = list(hooks_dst.glob("*.sh"))
            self.assertGreaterEqual(len(installed), 15)
            # Executable bit preserved by shutil.copy2.
            for h in installed:
                self.assertTrue(h.stat().st_mode & 0o111, f"{h.name} not executable")

            settings_path = tmp_root / ".claude" / "settings.json"
            self.assertTrue(settings_path.exists())
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertIn("hooks", data)
            self.assertIn("SessionStart", data["hooks"])

    def test_no_hooks_flag_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            (tmp_root / "templates").symlink_to(REPO_ROOT / "templates")
            fake_args = argparse.Namespace(with_hooks=False)
            with patch.object(install, "PROJECT_ROOT", tmp_root):
                summary = install._install_hooks_and_settings(fake_args)
            self.assertEqual(summary, "")
            self.assertFalse((tmp_root / ".claude" / "hooks").exists())

    def test_scripts_directory_is_copied(self) -> None:
        """Scripts in templates/scripts/ should land in .claude/scripts/.

        Specifically precompact_prune.py, which is referenced by
        pre-compact-save.sh — without it the hook fails silently.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            (tmp_root / "templates").symlink_to(REPO_ROOT / "templates")
            fake_args = argparse.Namespace(with_hooks=True)
            with patch.object(install, "PROJECT_ROOT", tmp_root):
                summary = install._install_hooks_and_settings(fake_args)

            scripts_dst = tmp_root / ".claude" / "scripts"
            self.assertTrue(
                scripts_dst.exists(),
                ".claude/scripts/ should be created when templates/scripts/ has files",
            )
            installed = list(scripts_dst.glob("*.py"))
            self.assertGreaterEqual(len(installed), 1, "at least one script expected")
            # precompact_prune.py specifically — wired by pre-compact-save.sh
            self.assertTrue(
                (scripts_dst / "precompact_prune.py").is_file(),
                "precompact_prune.py must be copied so pre-compact-save.sh works",
            )
            self.assertIn("scripts", summary)


if __name__ == "__main__":
    unittest.main()
