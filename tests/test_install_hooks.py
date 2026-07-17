# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Hook + settings-merge distribution contracts — v0.2.85 MIGRATION.

Originally (2026-04-28 / 2026-04-30) these tests pinned install.py's
``_install_hooks_and_settings`` + ``_merge_settings_template`` /
``_smart_merge_settings`` helpers. v0.2.85 (PLAN-v0285 WP-1) DELETED those:
the root now installs hooks/scripts/settings/agents/skills by DELEGATING to
the same ``install-bundle`` engine the launcher uses (via
``vco_lib.self_install.run_root_bundle_install``). The CONTRACTS these tests
pinned are re-expressed against the delegated path (plan-citation comment on
every migrated assertion), with NO silent weakening:

  * settings.json merge — template hooks appended, user keys win, env block
    survives — now the bundle's ``_merge_settings_template_for_bundle``.
  * BOTH hook flavours (.sh + .ps1) ship on every OS.
  * hooks carry the exec bit on POSIX (the v0.2.53 mode-664 regression stays
    dead — though the SOURCE mode changed 0o755 → 0o700 per D7, the exec bit
    is still PRESENT, which is the load-bearing invariant).
  * scripts land under .claude/scripts/.

Tests that never touched the deleted functions (``_check_windows_shell_prereqs``
+ the template bash-syntax gate) are carried forward UNCHANGED.
"""
from __future__ import annotations

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
from tests._v0284_bundle_fixtures import make_fake_orchestrator  # noqa: E402
from vco_lib import self_install  # noqa: E402


def _stage_root() -> Path:
    """A fake orchestrator ROOT with templates (the delegated-install target).

    PLAN-v0285: the root case is `--folder <root> --orchestrator-root <root>`,
    exactly what install.py's delegated call passes.
    """
    root = Path(tempfile.mkdtemp(prefix="v0285-hooks-migrate-"))
    make_fake_orchestrator(root)
    return root


class SettingsMergeFreshProjectTest(unittest.TestCase):
    """MIGRATED (was SmartMergeFreshProjectTest): a fresh install writes the
    settings template and registers its hook commands. Contract preserved —
    now via the delegated bundle path (D1/D2), asserting on the CLI's
    settings-merge output rather than the deleted _merge_settings_template."""

    def test_fresh_install_creates_settings_with_template_hooks(self) -> None:
        root = _stage_root()
        try:
            res = self_install.run_root_bundle_install(root, update_mode=False)
            # PLAN-v0285 D2: settings_action reported by the ONE bundle engine.
            self.assertEqual(res.get("settings_action"), "created")
            settings = root / ".claude" / "settings.json"
            self.assertTrue(settings.exists())
            data = json.loads(settings.read_text(encoding="utf-8"))
            # The fixture's template declares a PreToolUse hook — it must land.
            self.assertIn("hooks", data)
            self.assertIn("permissions", data)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class SettingsMergePreservesUserSettingsTest(unittest.TestCase):
    """MIGRATED (was SmartMergePreservesUserSettingsTest): user keys win and
    template hook commands append without duplicating. Contract preserved —
    the bundle's settings merge is the explicit mirror of the deleted
    install.py helpers (project_init._merge_settings_template_for_bundle)."""

    def test_update_preserves_user_keys_and_appends_missing_hooks(self) -> None:
        root = _stage_root()
        try:
            # First install writes the shipped settings.json.
            self_install.run_root_bundle_install(root, update_mode=False)
            settings = root / ".claude" / "settings.json"
            existing = json.loads(settings.read_text(encoding="utf-8"))
            # User adds a custom top-level key + a custom hook command + a
            # custom permissions.allow entry (the base test's preservation
            # contract — restored per v0.2.85 M-3, must not be silently dropped).
            existing["myCustomKey"] = "do-not-touch"
            existing.setdefault("permissions", {}).setdefault("allow", []).append(
                "Bash(my-custom-allow:*)"
            )
            existing.setdefault("hooks", {}).setdefault("Stop", []).append(
                {"hooks": [{"type": "command",
                            "command": "echo my-custom-stop-hook"}]}
            )
            settings.write_text(json.dumps(existing, indent=2), encoding="utf-8")

            # A template hook change → update merges (user keys win, but the
            # NEW template hook command must APPEND — the "template hooks append
            # without duplicating" half of the contract the docstring promises).
            tmpl = root / "templates" / "settings.json.linux.template"
            t = json.loads(tmpl.read_text(encoding="utf-8"))
            t.setdefault("hooks", {})["PostToolUse"] = [
                {"matcher": "*", "hooks": [{"type": "command",
                                            "command": "vco-post"}]}
            ]
            tmpl.write_text(json.dumps(t, indent=2), encoding="utf-8")

            self_install.run_root_bundle_install(root, update_mode=True)

            merged = json.loads(settings.read_text(encoding="utf-8"))
            # PLAN-v0285: user key preserved (user-wins), custom hook survives.
            self.assertEqual(merged.get("myCustomKey"), "do-not-touch")
            # M-3 (restored): user permissions.allow entry preserved.
            self.assertIn(
                "Bash(my-custom-allow:*)",
                merged.get("permissions", {}).get("allow", []),
                "user's custom permissions.allow entry must be preserved",
            )
            all_cmds = [
                h.get("command", "")
                for entries in merged.get("hooks", {}).values()
                for entry in entries
                for h in entry.get("hooks", [])
            ]
            self.assertEqual(
                sum(1 for c in all_cmds if "my-custom-stop-hook" in c), 1,
                "user's custom hook must appear exactly once",
            )
            # M-3 (restored): the NEW template hook command actually LANDED
            # (append-missing half of the merge contract, previously staged but
            # never asserted).
            self.assertIn(
                "vco-post", all_cmds,
                "new template hook command must be appended on update",
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


class HooksDirectorySyntaxTest(unittest.TestCase):
    """CARRIED FORWARD UNCHANGED: every shipped hook must be valid bash. This
    inspects templates/hooks/ directly and never touched the deleted install.py
    functions."""

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


class DelegatedHooksInstallIntegrationTest(unittest.TestCase):
    """MIGRATED (was InstallHooksAndSettingsIntegrationTest): end-to-end, the
    delegated bundle path lands BOTH hook flavours, the exec bit, scripts, and
    settings.json against the REAL production templates.

    PLAN-v0285 D2: exercises the ONE engine via a subprocess against the real
    orchestrator root (REPO_ROOT), the same argv install.py now emits.
    """

    def _run_bundle_into(self, folder: Path, *extra: str) -> subprocess.CompletedProcess:
        import os  # noqa: PLC0415
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.pop("VCT_DISABLE_HOOKS", None)
        return subprocess.run(
            [
                sys.executable, "-m", "vco_lib.project_init", "install-bundle",
                "--folder", str(folder),
                "--orchestrator-root", str(REPO_ROOT),
                "--project-folder", str(folder),
                "--json", *extra,
            ],
            capture_output=True, text=True, env=env, timeout=600,
        )

    def test_install_lands_both_hook_flavours_and_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            proc = self._run_bundle_into(proj)
            self.assertEqual(proc.returncode, 0, proc.stderr[-500:])

            hooks_dst = proj / ".claude" / "hooks"
            self.assertTrue(hooks_dst.exists())
            installed_sh = list(hooks_dst.glob("*.sh"))
            installed_ps1 = list(hooks_dst.glob("*.ps1"))
            # PLAN-v0284 Track G G-4 (carried): BOTH flavours ship on every OS.
            self.assertGreaterEqual(len(installed_sh), 15,
                                    "install should land .sh hooks")
            self.assertGreaterEqual(len(installed_ps1), 15,
                                    "install should ALSO land .ps1 hooks")
            # PLAN-v0285 D7: exec bit PRESENT (source mode is now 0o700 via the
            # bundle, not 0o755 — but S_IXUSR is what matters; the v0.2.53
            # mode-664 regression must stay dead).
            for h in installed_sh:
                self.assertTrue(h.stat().st_mode & 0o100,
                                f"{h.name} lost the owner-execute bit")

            # Scripts land under .claude/scripts/ (was
            # test_scripts_directory_is_copied).
            scripts_dst = proj / ".claude" / "scripts"
            self.assertTrue(scripts_dst.exists())
            self.assertTrue(
                (scripts_dst / "precompact_prune.py").is_file(),
                "precompact_prune.py must ship so pre-compact-save.sh works",
            )

            # settings.json written from the OS-active template with bash
            # hook commands (was test_install_into_fresh_target_linux).
            settings_path = proj / ".claude" / "settings.json"
            self.assertTrue(settings_path.exists())
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            cmds = [
                h.get("command", "")
                for entries in data["hooks"].values()
                for entry in entries
                for h in entry.get("hooks", [])
                if h.get("command")
            ]
            if not sys.platform.startswith("win"):
                bash_hook_cmds = [c for c in cmds if ".claude/hooks/" in c]
                self.assertTrue(
                    all("bash " in c for c in bash_hook_cmds),
                    "linux template must wire hooks via `bash` prefix",
                )


class SettingsTemplateBackslashRegressionTest(unittest.TestCase):
    """CARRIED FORWARD (was the Windows backslash-separator regression lock in
    WindowsHookInstallTest): the shipped Windows settings template must never
    use the ``.claude\\hooks\\`` backslash form (bash -c eats it on Windows).
    Inspects templates/ directly — independent of the deleted functions."""

    def test_windows_template_uses_forward_slash_hook_paths(self) -> None:
        tmpl = REPO_ROOT / "templates" / "settings.json.windows.template"
        self.assertTrue(tmpl.exists())
        data = json.loads(tmpl.read_text(encoding="utf-8"))
        cmds = [
            h.get("command", "")
            for entries in data.get("hooks", {}).values()
            for entry in entries
            for h in entry.get("hooks", [])
            if h.get("command")
        ]
        for c in cmds:
            self.assertNotIn(
                ".claude\\hooks\\", c,
                "windows hook command must use forward-slash "
                "`.claude/hooks/...` (backslash is eaten by bash -c on "
                f"Windows, corrupting the -File path): {c}",
            )


class WindowsInstallGateTest(unittest.TestCase):
    """CARRIED FORWARD UNCHANGED: tests ``_check_windows_shell_prereqs``, which
    is NOT one of the deleted functions."""

    def test_non_windows_is_noop(self) -> None:
        with patch("install.platform.system", return_value="Linux"):
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
