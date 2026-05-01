# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for `_install_hooks_and_settings` and the smart-merge helpers in
install.py.

Covers the per-target-project hook distribution feature added 2026-04-28
to close the README "roadmap" gap (20 hooks were previously orchestrator-only),
and the .sh / .ps1 OS-active install behaviour added 2026-04-30 (audit F1).
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


def _linux_template() -> Path:
    return REPO_ROOT / "templates" / "settings.json.linux.template"


def _windows_template() -> Path:
    return REPO_ROOT / "templates" / "settings.json.windows.template"


class SmartMergeFreshProjectTest(unittest.TestCase):
    """When .claude/settings.json doesn't exist, the template is written
    verbatim (with an extra trailing newline) and every template hook command
    ends up registered."""

    def test_creates_file_from_linux_template_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".claude" / "settings.json"
            template = _linux_template()
            self.assertTrue(template.exists(), "linux template must ship in repo")

            action = install._merge_settings_template(template, target)

            self.assertEqual(action, "created")
            self.assertTrue(target.exists())
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn("hooks", data)
            for event in (
                "SessionStart", "PreCompact", "PostCompact", "UserPromptSubmit",
                "PreToolUse", "Stop", "PostToolUse",
            ):
                self.assertIn(event, data["hooks"], f"missing event: {event}")
            self.assertIn("permissions", data)
            self.assertIn("allow", data["permissions"])
            self.assertIn("deny", data["permissions"])
            self.assertIn("_template_origin", data)


class SmartMergePreservesUserSettingsTest(unittest.TestCase):
    """When the project already has a partial settings.json, user keys win,
    and template hook commands are appended without duplicating existing ones."""

    def test_merge_preserves_user_keys_and_appends_missing_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".claude" / "settings.json"
            target.parent.mkdir(parents=True)

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

            template = _linux_template()
            action = install._merge_settings_template(template, target)
            self.assertEqual(action, "merged")

            merged = json.loads(target.read_text(encoding="utf-8"))

            self.assertEqual(merged.get("myCustomKey"), "do-not-touch")
            self.assertIn("Bash(my-tool *)", merged["permissions"]["allow"])

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
    is written from the OS-active template."""

    def test_install_into_fresh_target_linux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            (tmp_root / "templates").symlink_to(REPO_ROOT / "templates")

            fake_args = argparse.Namespace(with_hooks=True)

            with patch.object(install, "PROJECT_ROOT", tmp_root), \
                    patch("install.platform.system", return_value="Linux"):
                summary = install._install_hooks_and_settings(fake_args)

            self.assertIn("hooks", summary)
            self.assertIn("settings.json created", summary)

            hooks_dst = tmp_root / ".claude" / "hooks"
            self.assertTrue(hooks_dst.exists())
            installed_sh = list(hooks_dst.glob("*.sh"))
            installed_ps1 = list(hooks_dst.glob("*.ps1"))
            self.assertGreaterEqual(len(installed_sh), 15,
                                    "linux install should land .sh hooks")
            self.assertEqual(installed_ps1, [],
                             "linux install must NOT land .ps1 hooks")
            for h in installed_sh:
                self.assertTrue(h.stat().st_mode & 0o111, f"{h.name} not executable")

            settings_path = tmp_root / ".claude" / "settings.json"
            self.assertTrue(settings_path.exists())
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            # Linux template uses bash hook commands.
            cmds = []
            for event_entries in data["hooks"].values():
                for entry in event_entries:
                    for h in entry.get("hooks", []):
                        c = h.get("command", "")
                        if c:
                            cmds.append(c)
            bash_hook_cmds = [c for c in cmds if ".claude/hooks/" in c]
            self.assertTrue(
                all("bash " in c for c in bash_hook_cmds),
                "linux template must wire hooks via `bash` prefix",
            )

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
            self.assertTrue(
                (scripts_dst / "precompact_prune.py").is_file(),
                "precompact_prune.py must be copied so pre-compact-save.sh works",
            )
            self.assertIn("scripts", summary)


class WindowsHookInstallTest(unittest.TestCase):
    """`_install_hooks_and_settings` on Windows hosts copies `.ps1` siblings
    instead of `.sh`, and writes the Windows settings template."""

    def test_windows_install_picks_ps1_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            (tmp_root / "templates").symlink_to(REPO_ROOT / "templates")
            fake_args = argparse.Namespace(with_hooks=True)
            with patch.object(install, "PROJECT_ROOT", tmp_root), \
                    patch("install.platform.system", return_value="Windows"):
                install._install_hooks_and_settings(fake_args)

            hooks_dst = tmp_root / ".claude" / "hooks"
            installed_ps1 = list(hooks_dst.glob("*.ps1"))
            installed_sh = list(hooks_dst.glob("*.sh"))
            self.assertGreaterEqual(
                len(installed_ps1), 15,
                "windows install should land .ps1 hooks",
            )
            self.assertEqual(
                installed_sh, [],
                "windows install must NOT land .sh hooks",
            )
            # _lib subdir should also pick the Windows variant.
            lib_dst = hooks_dst / "_lib"
            if lib_dst.exists():
                self.assertTrue(
                    (lib_dst / "find-python.ps1").exists(),
                    "_lib/find-python.ps1 should land on Windows",
                )
                self.assertFalse(
                    (lib_dst / "find-python.sh").exists(),
                    "_lib/find-python.sh should NOT land on Windows",
                )

    def test_windows_install_uses_windows_settings_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            (tmp_root / "templates").symlink_to(REPO_ROOT / "templates")
            fake_args = argparse.Namespace(with_hooks=True)
            with patch.object(install, "PROJECT_ROOT", tmp_root), \
                    patch("install.platform.system", return_value="Windows"):
                install._install_hooks_and_settings(fake_args)

            settings_path = tmp_root / ".claude" / "settings.json"
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            cmds = []
            for event_entries in data["hooks"].values():
                for entry in event_entries:
                    for h in entry.get("hooks", []):
                        c = h.get("command", "")
                        if c:
                            cmds.append(c)
            ps1_cmds = [c for c in cmds if ".claude\\hooks\\" in c or ".ps1" in c]
            self.assertTrue(ps1_cmds, "windows template should reference .ps1 hooks")
            for c in ps1_cmds:
                self.assertIn(
                    "powershell",
                    c.lower(),
                    f"every .ps1 hook command should be invoked via powershell: {c}",
                )
                self.assertNotIn(
                    "bash ", c,
                    f"windows template must not use bash prefix: {c}",
                )


class WindowsInstallGateTest(unittest.TestCase):
    """The Windows install gate refuses install when neither PowerShell 5.1+
    nor Git Bash is available, and warns when only Git Bash is missing."""

    def test_non_windows_is_noop(self) -> None:
        with patch("install.platform.system", return_value="Linux"):
            # Should not raise.
            install._check_windows_shell_prereqs()
        with patch("install.platform.system", return_value="Darwin"):
            install._check_windows_shell_prereqs()

    def test_refuses_windows_without_pwsh(self) -> None:
        with patch("install.platform.system", return_value="Windows"), \
                patch("install._windows_powershell_version", return_value=None), \
                patch("install._windows_has_git_bash", return_value=False):
            with self.assertRaises(SystemExit) as cm:
                install._check_windows_shell_prereqs()
            self.assertIn("PowerShell 5.1", str(cm.exception))

    def test_refuses_windows_with_only_git_bash(self) -> None:
        with patch("install.platform.system", return_value="Windows"), \
                patch("install._windows_powershell_version", return_value=None), \
                patch("install._windows_has_git_bash", return_value=True):
            with self.assertRaises(SystemExit) as cm:
                install._check_windows_shell_prereqs()
            # Same actionable message as the no-shell case.
            self.assertIn("PowerShell 5.1", str(cm.exception))

    def test_passes_windows_with_pwsh_and_git_bash(self) -> None:
        with patch("install.platform.system", return_value="Windows"), \
                patch("install._windows_powershell_version", return_value=(5, 1)), \
                patch("install._windows_has_git_bash", return_value=True):
            install._check_windows_shell_prereqs()  # should not raise

    def test_warns_on_windows_with_pwsh_but_no_git_bash(self) -> None:
        import io
        captured = io.StringIO()
        with patch("install.platform.system", return_value="Windows"), \
                patch("install._windows_powershell_version", return_value=(7, 4)), \
                patch("install._windows_has_git_bash", return_value=False), \
                patch("sys.stderr", captured):
            install._check_windows_shell_prereqs()
        self.assertIn("Git Bash", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
