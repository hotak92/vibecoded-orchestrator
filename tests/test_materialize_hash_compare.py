# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.54 Track D (Theme 5) regression tests: orchestrator-self
materialize hash-compare + preserve-and-defer.

Pre-fix, ``install._materialize_orchestrator_self_claude_dir``
unconditionally ``shutil.copy2``-ed every template over the runtime
``.claude/{hooks,scripts}`` copies — silently destroying local edits
(the field-observed instance: a 118-line ``agent-skill-keyword-match.py``
divergence would have been reverted by the next ``--update``). The
per-project bundle path (``project_init._file_action``) had hash-compare
+ preserve + deferral semantics since v0.2.x; this backports them.

Contract under test:
  * create: target missing → copied (unchanged behaviour).
  * noop: target identical → untouched.
  * overwrite: target matches the manifest's prior-shipped hash OR any
    historical shipped version (git heal) → updated.
  * preserve: target diverges from every shipped version → left on
    disk; listed in an ``orchestrator_self_user_modified_preserved``
    deferral entry.
  * --force-materialize-claude-dir: preserved files are overwritten.
  * the V0243-4 manifest refresh records TEMPLATE hashes (prior-shipped
    semantics), never destination hashes — otherwise the SECOND update
    would destroy the file the first one preserved.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import install  # type: ignore  # noqa: E402
from vco_lib import project_init as _pi  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


def _stage_install_root(*, git: bool = False) -> Path:
    """Minimal fake install_root: 2 hook templates + 2 script templates +
    the two settings templates (tiny synthetic ones — we don't need the
    production templates for hash-compare semantics)."""
    tmp = Path(tempfile.mkdtemp(prefix="trackd-materialize-"))
    hooks = tmp / "templates" / "hooks"
    scripts = tmp / "templates" / "scripts"
    hooks.mkdir(parents=True)
    scripts.mkdir(parents=True)
    (hooks / "sample-hook.sh").write_text("#!/bin/bash\necho v2\n")
    (scripts / "sample-script.py").write_text("print('v2')\n")
    for tname in ("settings.json.linux.template",
                  "settings.json.windows.template"):
        (tmp / "templates" / tname).write_text("{}\n")
    if git:
        subprocess.run(["git", "init", "-q", str(tmp)], check=True)
        subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp), "-c", "user.email=t@t", "-c",
             "user.name=t", "commit", "-qm", "ship v2"],
            check=True,
        )
    return tmp


def _manifest_with(install_root: Path, entries: dict) -> None:
    """Write a minimal .vco-manifest.json with given {rel: sha256}."""
    target = install_root / ".claude" / ".vco-manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "schema_version": 2,
        "files": {k: {"sha256": v, "source": ""} for k, v in entries.items()},
    }))


class TestMaterializeHashCompare(unittest.TestCase):
    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_create_and_noop_paths_unchanged(self):
        self.root = _stage_install_root()
        install._materialize_orchestrator_self_claude_dir(self.root)
        script = self.root / ".claude" / "scripts" / "sample-script.py"
        self.assertEqual(script.read_text(), "print('v2')\n")
        # Second run: noop, content stable.
        install._materialize_orchestrator_self_claude_dir(self.root)
        self.assertEqual(script.read_text(), "print('v2')\n")

    def test_user_modified_runtime_copy_is_preserved_and_deferred(self):
        self.root = _stage_install_root()
        # First materialize ships v2 everywhere.
        install._materialize_orchestrator_self_claude_dir(self.root)
        script = self.root / ".claude" / "scripts" / "sample-script.py"
        # Record the prior-shipped hash the way the V0243-4 refresh does.
        _manifest_with(self.root, {
            str(Path(".claude") / "scripts" / "sample-script.py"):
                _pi._file_sha256(script),
        })
        # User dogfoods a local edit (the field-observed keyword-match shape).
        script.write_text("print('v2 + local dogfooded feature')\n")
        user_content = script.read_text()

        report = DeferralReport()
        install._materialize_orchestrator_self_claude_dir(
            self.root, deferral_report=report,
        )
        self.assertEqual(script.read_text(), user_content,
                         "user-modified runtime copy must be PRESERVED")
        cids = [e.condition_id for e in report.entries]
        self.assertIn("orchestrator_self_user_modified_preserved", cids)
        entry = next(e for e in report.entries
                     if e.condition_id ==
                     "orchestrator_self_user_modified_preserved")
        self.assertIn("scripts/sample-script.py", entry.detected)
        self.assertIn("--force-materialize-claude-dir", entry.command_to_apply)

    def test_force_flag_accepts_template_versions(self):
        self.root = _stage_install_root()
        install._materialize_orchestrator_self_claude_dir(self.root)
        script = self.root / ".claude" / "scripts" / "sample-script.py"
        _manifest_with(self.root, {
            str(Path(".claude") / "scripts" / "sample-script.py"):
                _pi._file_sha256(script),
        })
        script.write_text("print('local edit')\n")

        report = DeferralReport()
        install._materialize_orchestrator_self_claude_dir(
            self.root, deferral_report=report, force_overwrite=True,
        )
        self.assertEqual(script.read_text(), "print('v2')\n",
                         "--force must accept the template version")
        self.assertEqual(report.entries, [])

    def test_untouched_but_stale_copy_overwritten_via_manifest(self):
        self.root = _stage_install_root()
        # Simulate a prior install that shipped v1: runtime copy is v1,
        # manifest records v1 as prior-shipped.
        old = "print('v1')\n"
        script = self.root / ".claude" / "scripts" / "sample-script.py"
        script.parent.mkdir(parents=True)
        script.write_text(old)
        _manifest_with(self.root, {
            str(Path(".claude") / "scripts" / "sample-script.py"):
                _pi._bytes_sha256(old.encode()),
        })
        install._materialize_orchestrator_self_claude_dir(self.root)
        self.assertEqual(script.read_text(), "print('v2')\n",
                         "manifest-matched (user-untouched) copy must update")

    def test_untouched_but_stale_copy_overwritten_via_git_heal(self):
        # Pre-manifest install: no .vco-manifest.json entry, but the
        # runtime copy matches a HISTORICAL shipped version (v1 commit).
        self.root = _stage_install_root(git=True)
        tpl = self.root / "templates" / "scripts" / "sample-script.py"
        # History: v1 was shipped first.
        tpl.write_text("print('v1')\n")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.email=t@t", "-c",
             "user.name=t", "commit", "-qm", "ship v1"],
            check=True,
        )
        tpl.write_text("print('v2')\n")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.email=t@t", "-c",
             "user.name=t", "commit", "-qm", "ship v2 again"],
            check=True,
        )
        # Runtime copy = v1 (stale shipped), NO manifest.
        script = self.root / ".claude" / "scripts" / "sample-script.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('v1')\n")

        install._materialize_orchestrator_self_claude_dir(self.root)
        self.assertEqual(
            script.read_text(), "print('v2')\n",
            "stale-but-shipped copy must heal to current template "
            "(v0.2.31 git-history heal)",
        )

    def test_genuinely_modified_copy_preserved_even_without_manifest(self):
        # No manifest, content matches NO shipped version → preserve.
        self.root = _stage_install_root(git=True)
        script = self.root / ".claude" / "scripts" / "sample-script.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('never shipped, user wrote this')\n")
        report = DeferralReport()
        install._materialize_orchestrator_self_claude_dir(
            self.root, deferral_report=report,
        )
        self.assertEqual(script.read_text(),
                         "print('never shipped, user wrote this')\n")
        cids = [e.condition_id for e in report.entries]
        self.assertIn("orchestrator_self_user_modified_preserved", cids)


class TestManifestRefreshRecordsShippedHashes(unittest.TestCase):
    def test_refresh_records_template_hash_not_dest_hash(self):
        root = _stage_install_root()
        try:
            install._materialize_orchestrator_self_claude_dir(root)
            # User modifies the runtime copy AFTER materialize.
            script = root / ".claude" / "scripts" / "sample-script.py"
            script.write_text("print('local edit')\n")

            install._refresh_orchestrator_self_vco_manifest(root)

            manifest = json.loads(
                (root / ".claude" / ".vco-manifest.json").read_text()
            )
            rel = str(Path(".claude") / "scripts" / "sample-script.py")
            recorded = manifest["files"][rel]["sha256"]
            template_hash = _pi._file_sha256(
                root / "templates" / "scripts" / "sample-script.py")
            dest_hash = _pi._file_sha256(script)
            self.assertEqual(
                recorded, template_hash,
                "manifest must record the SHIPPED (template) hash",
            )
            self.assertNotEqual(
                recorded, dest_hash,
                "recording the dest hash would make the next update see "
                "installed==prior-shipped and destroy the preserved file",
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestForceFlagRegistered(unittest.TestCase):
    def test_force_flag_in_argparse(self):
        # Mirrors test_skip_flag_is_registered_in_argparse in the PR-39
        # test file: dry-parse via --help and grep the flag name.
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--force-materialize-claude-dir", result.stdout,
                      "CLI flag missing from --help output")


if __name__ == "__main__":
    unittest.main()
