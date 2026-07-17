# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Root .claude/ materialization contracts — v0.2.85 MIGRATION.

Originally (PR-39, v0.2.12) these tests pinned install.py's
``_materialize_orchestrator_self_claude_dir`` byte-identity / exec-bit /
idempotence / soft-fail / symlink-consolidation / skip-flag behaviour.
v0.2.85 (PLAN-v0285 WP-1) DELETED that function: the root now installs its
runtime .claude/ by DELEGATING to the ``install-bundle`` engine (via
``vco_lib.self_install.run_root_bundle_install``).

Every pinned CONTRACT is re-expressed against the delegated path with a
plan-citation comment (no silent weakening):

  * hooks/scripts byte-identical with templates/ → the bundle byte-copies them.
  * settings.json rendered from the OS-active template → bundle settings merge.
  * idempotence on --update → the bundle path is idempotent for untouched files.
  * soft-fail on missing template dir → bundle soft-fails per-file.
  * exec bit present on POSIX hooks → bundle sets 0o700 (S_IXUSR present; the
    SOURCE mode changed 0o755 → 0o700 per D7, exec bit stays PRESENT).
  * --skip-materialize-claude-dir stays a registered flag → mapped to
    --skip-kind hooks/scripts/settings (D5).
  * multi-symlink redirect → ONE consolidated deferral, now emitted BY the
    bundle path (v0.2.70 FIX-3, per the must-not-regress ledger).
"""
from __future__ import annotations

import filecmp
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from vco_lib import self_install  # noqa: E402


def _run_bundle_into(folder: Path, *extra: str) -> subprocess.CompletedProcess:
    """Drive the real install-bundle CLI against `folder` from REPO_ROOT — the
    same argv install.py now emits for the root."""
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


class DelegatedCopiesHooksBytewiseTest(unittest.TestCase):
    """MIGRATED (was MaterializeCopiesHooksBytewiseTest): .claude/hooks/ ends
    up byte-identical with templates/hooks/. The bundle byte-copies hooks."""

    def test_hooks_dir_matches_templates_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            proc = _run_bundle_into(proj)
            self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
            hooks_dst = proj / ".claude" / "hooks"
            self.assertTrue(hooks_dst.is_dir())
            for src in (REPO_ROOT / "templates" / "hooks").iterdir():
                if not src.is_file():
                    continue
                dst = hooks_dst / src.name
                self.assertTrue(dst.is_file(), f"missing hook: {src.name}")
                self.assertTrue(filecmp.cmp(src, dst, shallow=False),
                                f"hook content drift: {src.name}")
            # _lib/ subdir (cross-OS helpers) also present + byte-identical.
            lib_src = REPO_ROOT / "templates" / "hooks" / "_lib"
            if lib_src.is_dir():
                lib_dst = hooks_dst / "_lib"
                self.assertTrue(lib_dst.is_dir(), "_lib/ missing")
                for src in lib_src.iterdir():
                    if not src.is_file():
                        continue
                    dst = lib_dst / src.name
                    self.assertTrue(dst.is_file(), f"missing _lib file: {src.name}")
                    self.assertTrue(filecmp.cmp(src, dst, shallow=False),
                                    f"_lib drift: {src.name}")


class DelegatedCopiesScriptsBytewiseTest(unittest.TestCase):
    """MIGRATED (was MaterializeCopiesScriptsBytewiseTest): every SHIPPED script
    ends up byte-identical with its templates/scripts/ source.

    PLAN-v0285 note: the OLD Step-5b path copied EVERY file in
    templates/scripts/ via iterdir(); the bundle path ships the glob-filtered
    set (``vco_lib.bundle_globs.script_patterns()`` — the same policy the whole
    orchestrator already used since v0.2.54 Track G G-4). So we assert against
    the SHIPPED set (bundle globs), not the raw directory listing — that is the
    accurate contract, not a weakening: non-script files like
    ``launchctl-plist.template`` were never meant to land in .claude/scripts/."""

    def test_shipped_scripts_match_templates_byte_for_byte(self) -> None:
        from vco_lib.bundle_globs import script_patterns  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            proc = _run_bundle_into(proj)
            self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
            scripts_dst = proj / ".claude" / "scripts"
            self.assertTrue(scripts_dst.is_dir())
            scripts_src = REPO_ROOT / "templates" / "scripts"
            shipped: set[str] = set()
            for pattern in script_patterns():
                for src in scripts_src.glob(pattern):
                    if src.is_dir():
                        continue
                    shipped.add(src.name)
            self.assertGreater(len(shipped), 0, "no scripts matched the globs")
            for name in shipped:
                src = scripts_src / name
                dst = scripts_dst / name
                self.assertTrue(dst.is_file(), f"missing shipped script: {name}")
                self.assertTrue(filecmp.cmp(src, dst, shallow=False),
                                f"script content drift: {name}")


class DelegatedRendersSettingsTest(unittest.TestCase):
    """MIGRATED (was MaterializeRendersLinux/WindowsSettingsTest): settings.json
    is rendered from the OS-active template. The bundle's settings merge handles
    the OS dispatch inside the subprocess (which runs on the host OS)."""

    def test_settings_json_written_and_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            proc = _run_bundle_into(proj)
            self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
            settings_path = proj / ".claude" / "settings.json"
            self.assertTrue(settings_path.is_file())
            parsed = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertIn("hooks", parsed)
            self.assertIn("permissions", parsed)
            # Hook commands must reference the host-OS flavour.
            cmds = [
                h.get("command", "")
                for entries in parsed["hooks"].values()
                for entry in entries
                for h in entry.get("hooks", [])
                if h.get("command")
            ]
            if sys.platform.startswith("win"):
                self.assertTrue(any(".ps1" in c for c in cmds),
                                "windows settings should reference .ps1 hooks")
            else:
                self.assertTrue(any("bash " in c for c in cmds),
                                "linux settings should wire hooks via bash")


class DelegatedIsIdempotentTest(unittest.TestCase):
    """MIGRATED (was MaterializeIsIdempotentTest): re-running against an
    untouched install produces no content churn (the --update contract)."""

    def test_second_run_yields_identical_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            _run_bundle_into(proj)

            def _snap() -> dict[str, bytes]:
                out: dict[str, bytes] = {}
                for p in (proj / ".claude").rglob("*"):
                    if p.is_file():
                        out[str(p.relative_to(proj))] = p.read_bytes()
                return out

            first = _snap()
            proc2 = _run_bundle_into(proj, "--update")
            self.assertEqual(proc2.returncode, 0, proc2.stderr[-400:])
            second = _snap()

            # Manifest carries a volatile timestamp; compare it semantically.
            manifest_rel = ".claude/.vco-manifest.json"
            volatile = ("updated_at", "installed_at")
            for rel, first_bytes in first.items():
                if rel not in second:
                    continue  # a new run may add e.g. deferral files — tolerate.
                if rel == manifest_rel:
                    a = json.loads(first_bytes.decode("utf-8"))
                    b = json.loads(second[rel].decode("utf-8"))
                    for f in volatile:
                        a.pop(f, None)
                        b.pop(f, None)
                    # The `files` map (the load-bearing content) must be stable.
                    self.assertEqual(a.get("files"), b.get("files"),
                                     "manifest files map churned between runs")
                else:
                    self.assertEqual(first_bytes, second[rel],
                                     f"content churned between runs: {rel}")


class DelegatedSoftFailsOnMissingTemplatesTest(unittest.TestCase):
    """MIGRATED (was MaterializeSoftFailsOnMissingTemplatesTest): a missing
    templates subtree warns + continues, never aborts. The bundle soft-fails
    per-file (missing template tree ⇒ fewer entries, exit clean)."""

    def test_missing_hooks_dir_does_not_abort(self) -> None:
        # Build a partial fake orchestrator (no hooks tree) and confirm the
        # delegated install still lands scripts/settings and exits clean.
        with tempfile.TemporaryDirectory() as tmp:
            orch = Path(tmp) / "orch"
            orch.mkdir()
            (orch / "vct-module.json").write_text("{}\n", encoding="utf-8")
            scripts = orch / "templates" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "kg-search").write_text("print('s')\n", encoding="utf-8")
            settings = {"permissions": {"allow": ["Bash"]}, "hooks": {}}
            for name in ("settings.json.linux.template",
                         "settings.json.windows.template"):
                (orch / "templates" / name).write_text(
                    json.dumps(settings), encoding="utf-8",
                )
            # No templates/hooks/ at all.
            res = self_install.run_root_bundle_install(orch, update_mode=False)
            # No exception + scripts landed.
            self.assertEqual(res["errors"], [])
            self.assertTrue((orch / ".claude" / "scripts").is_dir(),
                            "scripts should still install when hooks/ missing")


class DelegatedPreservesExecBitTest(unittest.TestCase):
    """MIGRATED (was MaterializePreservesExecBitTest): POSIX hook .sh files keep
    the executable bit. PLAN-v0285 D7: the SOURCE mode moved 0o755 → 0o700, but
    S_IXUSR is PRESENT (the v0.2.53 mode-664 regression stays dead)."""

    def test_executable_bit_present_on_posix_hooks(self) -> None:
        if sys.platform.startswith("win"):
            self.skipTest("POSIX-only: Windows has no +x bit")
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            _run_bundle_into(proj)
            hooks_dst = proj / ".claude" / "hooks"
            checked = 0
            for dst in hooks_dst.glob("*.sh"):
                self.assertTrue(
                    dst.stat().st_mode & stat.S_IXUSR,
                    f"executable bit lost on {dst.name}: {oct(dst.stat().st_mode)}",
                )
                checked += 1
            self.assertGreater(checked, 0,
                               "no .sh hooks installed to check the exec bit")


class SkipFlagRegisteredTest(unittest.TestCase):
    """MIGRATED (was SkipFlagShortCircuitsTest): the
    --skip-materialize-claude-dir CLI flag stays registered. install.py maps it
    to the bundle's --skip-kind hooks/scripts/settings (D5)."""

    def test_skip_flag_is_registered_in_argparse(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(
            result.returncode, 0,
            f"install.py --help exited {result.returncode}; "
            f"stderr: {result.stderr[:400]}",
        )
        self.assertIn("--skip-materialize-claude-dir", result.stdout,
                      "CLI flag missing from --help output")

    def test_skip_flag_maps_to_skip_kinds(self) -> None:
        """PLAN-v0285 D5: the flag resolves to the hooks/scripts/settings
        bundle kinds (agents/skills/knowledge still install)."""
        self.assertEqual(
            self_install.SKIP_MATERIALIZE_CLAUDE_DIR_KINDS,
            ("hooks", "scripts", "settings"),
        )


class DelegatedSymlinkConsolidatedDeferralTest(unittest.TestCase):
    """MIGRATED (was MaterializeSymlinkConsolidatedDeferralTest): 2+ symlink
    redirects collapse to ONE consolidated deferral. install.py's own
    accumulator was DELETED — the consolidated deferral is now emitted BY the
    bundle path (v0.2.70 FIX-3, must-not-regress ledger). The condition id is
    ``symlink_preserved_under_install_path``."""

    def _make_symlink_or_skip(self, target: Path, link: Path) -> None:
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot create symlink: {exc}")

    def test_two_symlinked_targets_collapse_to_one_entry(self) -> None:
        from tests._v0284_bundle_fixtures import make_fake_orchestrator  # noqa: PLC0415
        from vco_lib.deferral_report import DeferralReport  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            make_fake_orchestrator(root)
            claude = root / ".claude"
            claude.mkdir()
            ext_hooks = root / "external-hooks"
            ext_hooks.mkdir()
            ext_scripts = root / "external-scripts"
            ext_scripts.mkdir()
            self._make_symlink_or_skip(ext_hooks, claude / "hooks")
            self._make_symlink_or_skip(ext_scripts, claude / "scripts")

            self_install.run_root_bundle_install(root, update_mode=False)

            report = DeferralReport.read(root)
            symlink_entries = [
                e for e in report.entries
                if e.condition_id == "symlink_preserved_under_install_path"
            ]
            # ONE consolidated entry naming BOTH redirected paths (not
            # one-per-redirect, not last-write-wins to a single pair).
            self.assertEqual(
                len(symlink_entries), 1,
                f"expected ONE consolidated symlink deferral; got "
                f"{len(symlink_entries)}",
            )
            detected = symlink_entries[0].detected
            self.assertIn("hooks", detected)
            self.assertIn("scripts", detected)
            # Originals untouched (still symlinks).
            self.assertTrue((claude / "hooks").is_symlink())
            self.assertTrue((claude / "scripts").is_symlink())


if __name__ == "__main__":
    unittest.main()
