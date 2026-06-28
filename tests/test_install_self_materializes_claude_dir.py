# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``install._materialize_orchestrator_self_claude_dir``.

PR-39 (v0.2.12, 2026-05-16). The public repo no longer ships
``.claude/{hooks,scripts,settings.json}`` — those used to be 50 +36 +1
files duplicating ``templates/`` byte-for-byte (a CI gate enforced parity,
which made the duplication double maintenance burden). install.py now
renders the orchestrator-self's runtime ``.claude/`` from ``templates/``
at install time, using the same template pipeline downstream user
projects already go through via ``vco_lib.project_init``.

Coverage:
  * ``materialize_creates_hooks_byte_identical_with_templates``
  * ``materialize_creates_scripts_byte_identical_with_templates``
  * ``materialize_creates_settings_json_from_linux_template`` (POSIX runs)
  * ``materialize_creates_settings_json_from_windows_template`` (mocked)
  * ``materialize_is_idempotent_when_called_twice``
  * ``materialize_soft_fails_on_missing_template_dir``
  * ``materialize_preserves_hook_executable_bit_on_posix``
"""
from __future__ import annotations

import filecmp
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import install  # type: ignore  # noqa: E402


def _stage_install_root() -> Path:
    """Create a fake install_root with templates/ populated from the real
    repo and .claude/ empty. Returns the tmpdir path; caller is responsible
    for cleanup via ``shutil.rmtree``.
    """
    tmp = Path(tempfile.mkdtemp(prefix="pr39-materialize-test-"))
    # Copy the real templates/hooks + templates/scripts + the two settings
    # templates so the test exercises the actual production templates.
    templates_dst = tmp / "templates"
    templates_dst.mkdir()
    shutil.copytree(REPO_ROOT / "templates" / "hooks", templates_dst / "hooks")
    shutil.copytree(REPO_ROOT / "templates" / "scripts",
                    templates_dst / "scripts")
    for tname in (
        "settings.json.linux.template",
        "settings.json.windows.template",
    ):
        shutil.copy2(REPO_ROOT / "templates" / tname,
                     templates_dst / tname)
    return tmp


def _count_template_files(subdir: str) -> int:
    """Count regular files directly under templates/<subdir>/ (non-recursive)."""
    return sum(
        1 for p in (REPO_ROOT / "templates" / subdir).iterdir() if p.is_file()
    )


class MaterializeCopiesHooksBytewiseTest(unittest.TestCase):
    """``.claude/hooks/`` ends up byte-identical with ``templates/hooks/``."""

    def test_hooks_dir_matches_templates_byte_for_byte(self) -> None:
        install_root = _stage_install_root()
        try:
            install._materialize_orchestrator_self_claude_dir(install_root)

            hooks_dst = install_root / ".claude" / "hooks"
            self.assertTrue(hooks_dst.is_dir(),
                            ".claude/hooks/ must exist after materialize")

            # Every template hook file is present + byte-identical in
            # .claude/hooks/. We compare by filename only (subdir _lib is
            # handled by the next assertion).
            for src in (REPO_ROOT / "templates" / "hooks").iterdir():
                if not src.is_file():
                    continue
                dst = hooks_dst / src.name
                self.assertTrue(
                    dst.is_file(),
                    f"missing hook in .claude/hooks: {src.name}",
                )
                self.assertTrue(
                    filecmp.cmp(src, dst, shallow=False),
                    f"hook content drift: {src.name}",
                )

            # _lib/ subdirectory (cross-OS helper scripts) is also present.
            lib_src = REPO_ROOT / "templates" / "hooks" / "_lib"
            if lib_src.is_dir():
                lib_dst = hooks_dst / "_lib"
                self.assertTrue(lib_dst.is_dir(),
                                "_lib/ subdir missing from .claude/hooks/")
                for src in lib_src.iterdir():
                    if not src.is_file():
                        continue
                    dst = lib_dst / src.name
                    self.assertTrue(dst.is_file(),
                                    f"missing _lib file: {src.name}")
                    self.assertTrue(
                        filecmp.cmp(src, dst, shallow=False),
                        f"_lib content drift: {src.name}",
                    )
        finally:
            shutil.rmtree(install_root)


class MaterializeCopiesScriptsBytewiseTest(unittest.TestCase):
    """``.claude/scripts/`` ends up byte-identical with ``templates/scripts/``."""

    def test_scripts_dir_matches_templates_byte_for_byte(self) -> None:
        install_root = _stage_install_root()
        try:
            install._materialize_orchestrator_self_claude_dir(install_root)

            scripts_dst = install_root / ".claude" / "scripts"
            self.assertTrue(scripts_dst.is_dir(),
                            ".claude/scripts/ must exist after materialize")

            for src in (REPO_ROOT / "templates" / "scripts").iterdir():
                if not src.is_file():
                    continue
                dst = scripts_dst / src.name
                self.assertTrue(
                    dst.is_file(),
                    f"missing script in .claude/scripts: {src.name}",
                )
                self.assertTrue(
                    filecmp.cmp(src, dst, shallow=False),
                    f"script content drift: {src.name}",
                )

            # Sanity check on count — should equal the template count.
            copied = sum(1 for p in scripts_dst.iterdir() if p.is_file())
            expected = _count_template_files("scripts")
            self.assertEqual(
                copied, expected,
                f"copied {copied} scripts, templates has {expected}",
            )
        finally:
            shutil.rmtree(install_root)


class MaterializeRendersLinuxSettingsTest(unittest.TestCase):
    """On POSIX hosts (the dispatch fallback), settings.json is rendered
    from the linux template with placeholders substituted."""

    def test_settings_json_matches_linux_template_with_substitution(self) -> None:
        if sys.platform.startswith("win"):
            self.skipTest("Linux/macOS branch — Windows path tested separately")

        install_root = _stage_install_root()
        try:
            install._materialize_orchestrator_self_claude_dir(install_root)

            settings_path = install_root / ".claude" / "settings.json"
            self.assertTrue(settings_path.is_file(),
                            ".claude/settings.json must exist")

            content = settings_path.read_text(encoding="utf-8")
            # Must parse as valid JSON (no broken substitution).
            parsed = json.loads(content)
            self.assertIn("hooks", parsed)
            self.assertIn("permissions", parsed)

            # Compare against template after applying the same substitution.
            template = (
                install_root / "templates" / "settings.json.linux.template"
            ).read_text(encoding="utf-8")
            expected = template.replace("{{PROJECT_NAME}}",
                                        "VibeCoded Orchestrator")
            self.assertEqual(content, expected)
        finally:
            shutil.rmtree(install_root)


class MaterializeRendersWindowsSettingsTest(unittest.TestCase):
    """Windows dispatch path uses settings.json.windows.template."""

    def test_settings_json_uses_windows_template_when_platform_is_windows(self) -> None:
        install_root = _stage_install_root()
        try:
            with patch("install.platform.system", return_value="Windows"):
                install._materialize_orchestrator_self_claude_dir(install_root)

            settings_path = install_root / ".claude" / "settings.json"
            self.assertTrue(settings_path.is_file())
            content = settings_path.read_text(encoding="utf-8")
            template = (
                install_root / "templates" / "settings.json.windows.template"
            ).read_text(encoding="utf-8")
            expected = template.replace("{{PROJECT_NAME}}",
                                        "VibeCoded Orchestrator")
            self.assertEqual(content, expected)

            # And it must NOT be the linux template (sanity check the dispatch).
            linux_template = (
                install_root / "templates" / "settings.json.linux.template"
            ).read_text(encoding="utf-8")
            self.assertNotEqual(content, linux_template)
        finally:
            shutil.rmtree(install_root)


class MaterializeIsIdempotentTest(unittest.TestCase):
    """Re-running the materialize step doesn't crash and produces the same
    result. This is the contract for ``install.py --update`` users."""

    def test_second_call_yields_identical_tree(self) -> None:
        install_root = _stage_install_root()
        try:
            install._materialize_orchestrator_self_claude_dir(install_root)

            # Snapshot the rendered tree state.
            first_snapshot: dict[str, bytes] = {}
            for p in (install_root / ".claude").rglob("*"):
                if p.is_file():
                    first_snapshot[str(p.relative_to(install_root))] = p.read_bytes()

            # Second call — must succeed without raising.
            install._materialize_orchestrator_self_claude_dir(install_root)

            second_snapshot: dict[str, bytes] = {}
            for p in (install_root / ".claude").rglob("*"):
                if p.is_file():
                    second_snapshot[str(p.relative_to(install_root))] = p.read_bytes()

            self.assertEqual(
                set(first_snapshot.keys()),
                set(second_snapshot.keys()),
                "file set changed between calls",
            )
            # `.vco-manifest.json` writes a fresh `updated_at` ISO-second
            # timestamp on every render — deliberately, so per-project
            # bundle-update can diff "last render" timestamps. When the
            # two calls span a second boundary, bytes differ but EFFECTIVE
            # content is identical. Compare it semantically by stripping
            # the volatile timestamp fields. (v0.2.45 V45-I: fix for flaky
            # CI runs where the second boundary was crossed; the test
            # contract is "idempotent content", not "byte-identical".)
            manifest_rel = ".claude/.vco-manifest.json"
            volatile_manifest_fields = ("updated_at", "installed_at")
            for relpath in first_snapshot:
                first_bytes = first_snapshot[relpath]
                second_bytes = second_snapshot[relpath]
                if relpath == manifest_rel:
                    first_obj = json.loads(first_bytes.decode("utf-8"))
                    second_obj = json.loads(second_bytes.decode("utf-8"))
                    for field in volatile_manifest_fields:
                        first_obj.pop(field, None)
                        second_obj.pop(field, None)
                    self.assertEqual(
                        first_obj, second_obj,
                        "manifest content (excluding volatile timestamp "
                        "fields) changed between calls",
                    )
                else:
                    self.assertEqual(
                        first_bytes, second_bytes,
                        f"file content changed between calls: {relpath}",
                    )
        finally:
            shutil.rmtree(install_root)


class MaterializeSoftFailsOnMissingTemplatesTest(unittest.TestCase):
    """A missing templates/hooks or templates/scripts dir should warn and
    continue, not abort the install. We DO NOT delete the parent
    templates/ — that's a different failure mode (caller's responsibility)."""

    def test_missing_hooks_dir_does_not_raise(self) -> None:
        install_root = _stage_install_root()
        try:
            shutil.rmtree(install_root / "templates" / "hooks")

            # Must not raise — soft-fail with warning.
            install._materialize_orchestrator_self_claude_dir(install_root)

            # Scripts + settings.json should still have been rendered.
            self.assertTrue(
                (install_root / ".claude" / "scripts").is_dir(),
                "scripts/ should still be materialized when hooks/ is missing",
            )
            self.assertTrue(
                (install_root / ".claude" / "settings.json").is_file(),
                "settings.json should still be rendered when hooks/ is missing",
            )
        finally:
            shutil.rmtree(install_root)

    def test_missing_scripts_dir_does_not_raise(self) -> None:
        install_root = _stage_install_root()
        try:
            shutil.rmtree(install_root / "templates" / "scripts")

            install._materialize_orchestrator_self_claude_dir(install_root)

            self.assertTrue(
                (install_root / ".claude" / "hooks").is_dir(),
                "hooks/ should still be materialized when scripts/ is missing",
            )
        finally:
            shutil.rmtree(install_root)


class MaterializePreservesExecBitTest(unittest.TestCase):
    """copy2 preserves mtime + mode. Verify the executable bit is preserved
    on POSIX for hook .sh files (these are invoked by Claude Code as
    ``bash <path>``, but contributor expectations + git index also expect
    +x on .sh files)."""

    def test_executable_bit_preserved_on_posix_hooks(self) -> None:
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only: Windows doesn't have the +x bit")

        install_root = _stage_install_root()
        try:
            install._materialize_orchestrator_self_claude_dir(install_root)

            # Find at least one .sh hook in templates that has +x set, and
            # verify the destination has +x too.
            template_hooks = REPO_ROOT / "templates" / "hooks"
            checked = 0
            for src in template_hooks.iterdir():
                if not (src.is_file() and src.suffix == ".sh"):
                    continue
                src_mode = src.stat().st_mode
                if not (src_mode & stat.S_IXUSR):
                    continue
                dst = install_root / ".claude" / "hooks" / src.name
                self.assertTrue(dst.exists(), f"missing hook: {src.name}")
                dst_mode = dst.stat().st_mode
                self.assertTrue(
                    dst_mode & stat.S_IXUSR,
                    f"executable bit lost on {src.name}: {oct(dst_mode)}",
                )
                checked += 1
            # Sanity: we expect to have checked at least a few hooks. If
            # NONE of the template .sh files are +x in the contributor's
            # checkout, the test is silently a no-op — flag that.
            self.assertGreater(
                checked, 0,
                "no .sh hooks with +x found in templates/hooks; "
                "test would silently pass without exercising the assertion",
            )
        finally:
            shutil.rmtree(install_root)


class SkipFlagShortCircuitsTest(unittest.TestCase):
    """The ``--skip-materialize-claude-dir`` CLI flag is wired through
    main() and produces a 'skip' install-log event when set. We exercise
    the function itself only — main() integration is covered by other
    tests."""

    def test_skip_flag_is_registered_in_argparse(self) -> None:
        # Black-box: just make sure the parser knows the flag. Stand-in
        # for "main() respects it" without bringing up the whole install
        # pipeline (which would attempt to start podman, etc.).
        try:
            from install import main as _main_fn  # noqa: F401
        except ImportError:
            self.skipTest("install.py main not importable in this env")

        # The arg list is built inside main(); easiest check: dry-parse
        # using `--help` and grep for the flag name. We capture stdout
        # via a tempfile to avoid noisy test output.
        import subprocess

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        # --help exits 0 in argparse.
        self.assertEqual(
            result.returncode, 0,
            f"install.py --help exited {result.returncode}; stderr: {result.stderr[:400]}",
        )
        self.assertIn(
            "--skip-materialize-claude-dir", result.stdout,
            "CLI flag missing from --help output",
        )


class MaterializeSymlinkConsolidatedDeferralTest(unittest.TestCase):
    """v0.2.70 FIX-3: when the self-materialize hits 2+ symlink redirects in one
    run, it must emit ONE consolidated `symlink_preserved_under_install_path`
    deferral listing ALL pairs — not 8 single-pair emits that collapse to
    last-write-wins."""

    def _make_symlink_or_skip(self, target: Path, link: Path) -> None:
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError) as exc:
            self.skipTest(
                f"cannot create symlink on this platform: {exc} "
                "(Windows requires developer-mode or admin)"
            )

    def test_two_symlinked_targets_collapse_to_one_entry_naming_both(self) -> None:
        from vco_lib.deferral_report import DeferralReport

        install_root = _stage_install_root()
        try:
            claude_dir = install_root / ".claude"
            claude_dir.mkdir()
            # Symlink BOTH .claude/hooks AND .claude/scripts → 2 redirect pairs.
            ext_hooks = install_root / "external-hooks"
            ext_hooks.mkdir()
            ext_scripts = install_root / "external-scripts"
            ext_scripts.mkdir()
            self._make_symlink_or_skip(ext_hooks, claude_dir / "hooks")
            self._make_symlink_or_skip(ext_scripts, claude_dir / "scripts")

            report = DeferralReport()
            install._materialize_orchestrator_self_claude_dir(
                install_root, deferral_report=report,
            )

            symlink_entries = [
                e for e in report.entries
                if e.condition_id == "symlink_preserved_under_install_path"
            ]
            # Exactly ONE consolidated entry (not one-per-redirect, not
            # last-write-wins down to a single pair).
            self.assertEqual(
                len(symlink_entries), 1,
                f"expected ONE consolidated symlink deferral; got "
                f"{len(symlink_entries)}",
            )
            detected = symlink_entries[0].detected
            # BOTH redirected paths must be named (no data loss).
            self.assertIn("hooks", detected)
            self.assertIn("scripts", detected)
            # Both .vco-new siblings actually got written.
            self.assertTrue((claude_dir / "hooks.vco-new").is_dir())
            self.assertTrue((claude_dir / "scripts.vco-new").is_dir())
            # The originals are untouched symlinks.
            self.assertTrue((claude_dir / "hooks").is_symlink())
            self.assertTrue((claude_dir / "scripts").is_symlink())
        finally:
            shutil.rmtree(install_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
