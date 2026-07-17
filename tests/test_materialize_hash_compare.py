# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Root runtime-copy drift handling — v0.2.85 MIGRATION.

Originally (v0.2.54 Track D) these tests pinned install.py's
``_materialize_orchestrator_self_claude_dir`` hash-compare + preserve-and-
defer semantics and the ``_refresh_orchestrator_self_vco_manifest`` template-
hash recording. v0.2.85 (PLAN-v0285 WP-1) DELETED both functions: the root now
delegates to the ``install-bundle`` engine.

The pinned CONTRACT MIGRATES (PLAN-v0285 D3): a runtime copy that diverges
from the shipped template is no longer PRESERVED + eternally deferred — it is
ADOPTED with a timestamped backup (the same policy the launcher already
applied to the root folder since v0.2.84, ending the asymmetry R-A bans). The
hash-heal cases (manifest-matched / git-history-matched → overwrite) carry
over because the bundle's ``_file_action`` implements the SAME heal. Each
migrated assertion carries a plan-citation comment; none is silently weakened.
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
from tests._v0284_bundle_fixtures import make_fake_orchestrator  # noqa: E402
from vco_lib import self_install  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


def _stage_root() -> Path:
    """Fake orchestrator ROOT with templates (the delegated-install target)."""
    root = Path(tempfile.mkdtemp(prefix="v0285-materialize-migrate-"))
    make_fake_orchestrator(root)
    return root


def _manifest(root: Path) -> dict:
    return json.loads(
        (root / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
    )


def _write_manifest(root: Path, data: dict) -> None:
    (root / ".claude" / ".vco-manifest.json").write_text(
        json.dumps(data), encoding="utf-8",
    )


class RootRuntimeCopyDriftTests(unittest.TestCase):
    def setUp(self):
        self.root = _stage_root()

    def tearDown(self):
        shutil.rmtree(str(self.root), ignore_errors=True)

    def _script(self) -> Path:
        return self.root / ".claude" / "scripts" / "kg-search"

    def test_create_and_noop_paths_unchanged(self):
        """MIGRATED (was test_create_and_noop_paths_unchanged): fresh install
        ships the template; a second identical run is a noop (stable content).
        Contract preserved — now via the delegated bundle path."""
        self_install.run_root_bundle_install(self.root, update_mode=False)
        shipped = self._script().read_text(encoding="utf-8")
        # Second run: noop, content stable.
        self_install.run_root_bundle_install(self.root, update_mode=True)
        self.assertEqual(self._script().read_text(encoding="utf-8"), shipped,
                         "re-run must leave an untouched shipped copy stable")

    def test_user_modified_runtime_copy_is_ADOPTED_not_preserved(self):
        """MIGRATED + CONTRACT CHANGED (was
        test_user_modified_runtime_copy_is_preserved_and_deferred).

        PLAN-v0285 D3: a KNOWN user-modified runtime copy (manifest-hash-
        mismatch) is now ADOPTED with a backup — NOT preserved + deferred. The
        original ``orchestrator_self_user_modified_preserved`` producer was
        deleted; this is the launcher-parity behaviour."""
        self_install.run_root_bundle_install(self.root, update_mode=False)
        script = self._script()
        user_content = "print('v2 + local dogfooded feature')\n"
        script.write_text(user_content, encoding="utf-8")
        # Bump the template so the update has shipped bytes to adopt to.
        (self.root / "templates" / "scripts" / "kg-search").write_text(
            "print('v3 shipped')\n", encoding="utf-8",
        )

        res = self_install.run_root_bundle_install(self.root, update_mode=True)
        rel = ".claude/scripts/kg-search"
        # Adopted (was: preserved).
        self.assertIn(rel, res["actions"]["adopt"],
                      "known user-modified runtime copy must be ADOPTED (D3)")
        # Shipped bytes on disk (was: user_content preserved).
        self.assertEqual(script.read_text(encoding="utf-8"), "print('v3 shipped')\n")
        # Backup carries the ORIGINAL user bytes (the safety net that replaces
        # preserve).
        backup = self.root / res["adopt_backup_dir"] / rel
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), user_content)
        # NO orchestrator_self_user_modified_preserved deferral (retired).
        report = DeferralReport.read(self.root)
        self.assertFalse(
            report.has_condition("orchestrator_self_user_modified_preserved"),
            "the retired preserve-deferral must not fire (D3)",
        )

    def test_force_flag_accepts_template_versions(self):
        """MIGRATED (was test_force_flag_accepts_template_versions):
        --force-materialize-claude-dir → --force overwrites user-modified
        files with the template version. Contract preserved."""
        self_install.run_root_bundle_install(self.root, update_mode=False)
        script = self._script()
        script.write_text("print('local edit')\n", encoding="utf-8")
        (self.root / "templates" / "scripts" / "kg-search").write_text(
            "print('shipped')\n", encoding="utf-8",
        )
        self_install.run_root_bundle_install(
            self.root, update_mode=True, force=True,
        )
        # PLAN-v0285 D5: --force accepts the template version.
        self.assertEqual(script.read_text(encoding="utf-8"), "print('shipped')\n",
                         "--force must overwrite with the template version")

    def test_untouched_but_stale_copy_overwritten_via_manifest(self):
        """MIGRATED (was test_untouched_but_stale_copy_overwritten_via_
        manifest): a runtime copy whose bytes match the manifest's prior-
        shipped hash (user-untouched) is OVERWRITTEN. The bundle's _file_action
        implements the SAME manifest-hash heal."""
        # Ship v1, capture its manifest entry, then bump the template to v2.
        self_install.run_root_bundle_install(self.root, update_mode=False)
        script = self._script()
        v1 = script.read_text(encoding="utf-8")
        (self.root / "templates" / "scripts" / "kg-search").write_text(
            "print('v2')\n", encoding="utf-8",
        )
        # Runtime copy is still v1 == manifest prior-shipped hash (untouched).
        res = self_install.run_root_bundle_install(self.root, update_mode=True)
        rel = ".claude/scripts/kg-search"
        self.assertIn(rel, res["actions"]["overwrite"],
                      "manifest-matched untouched copy must OVERWRITE, not adopt")
        self.assertEqual(script.read_text(encoding="utf-8"), "print('v2')\n")
        self.assertNotEqual(script.read_text(encoding="utf-8"), v1)

    def test_untouched_but_stale_copy_overwritten_via_git_heal(self):
        """MIGRATED (was the deleted test_untouched_but_stale_copy_overwritten_
        via_git_heal): a MANIFEST-LESS runtime copy whose bytes match a
        HISTORICAL shipped template version (git history) is a stale VCO copy,
        NOT a user edit — it heals to the current template via a plain
        `overwrite` (NO adoption backup, NO preserve). The bundle's _file_action
        `_installed_matches_template_history` branch implements the SAME v0.2.31
        heal the deleted install.py path did; re-pinned here on the delegated
        bundle path (must-not-regress ledger: "v0.2.31 + PR-2 heal paths stay
        plain overwrite, no backup"). Fable v0.2.85 M-5 caught the dropped pin.
        """
        script = self._script()
        tpl = self.root / "templates" / "scripts" / "kg-search"

        def _git(*args: str) -> None:
            subprocess.run(
                ["git", "-C", str(self.root), "-c", "user.email=t@t",
                 "-c", "user.name=t", *args],
                check=True, capture_output=True,
            )

        _git("init", "-q")
        # History: ship v1, then bump the template to v2 (both committed).
        tpl.write_text("print('v1 historical')\n", encoding="utf-8")
        _git("add", "-A"); _git("commit", "-qm", "ship v1")
        tpl.write_text("print('v2 current')\n", encoding="utf-8")
        _git("add", "-A"); _git("commit", "-qm", "ship v2")

        # Runtime copy = the HISTORICAL v1 bytes, with NO manifest entry (the
        # pre-manifest / manifest-less install shape).
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("print('v1 historical')\n", encoding="utf-8")
        manifest = _manifest(self.root) if (
            self.root / ".claude" / ".vco-manifest.json").exists() else {"files": {}}
        manifest.setdefault("files", {}).pop(".claude/scripts/kg-search", None)
        _write_manifest(self.root, manifest)

        res = self_install.run_root_bundle_install(self.root, update_mode=True)
        rel = ".claude/scripts/kg-search"
        # HEAL → plain overwrite (NOT adopt, NOT preserve).
        self.assertIn(rel, res["actions"]["overwrite"],
                      "manifest-less stale-but-shipped copy must HEAL via overwrite")
        self.assertNotIn(rel, res["actions"].get("adopt", []),
                         "git-history heal must NOT adopt (no backup path)")
        self.assertNotIn(rel, res["actions"].get("preserve", []))
        # Current template bytes on disk.
        self.assertEqual(script.read_text(encoding="utf-8"), "print('v2 current')\n")
        # NO adoption backup was taken (heal is plain overwrite).
        self.assertFalse(res.get("adopt_backup_dir"),
                         "git-history heal must not create an adoption backup")


class ManifestRecordsTemplateHashTests(unittest.TestCase):
    """MIGRATED (was TestManifestRefreshRecordsShippedHashes): the manifest
    records the SHIPPED (template) hash so a user drift after install does not
    make the next update see installed==prior-shipped. install.py's manifest
    writer was DELETED; the bundle's ``_write_manifest_atomic`` records the
    shipped hash — this pins that behaviour via the delegated path."""

    def setUp(self):
        self.root = _stage_root()

    def tearDown(self):
        shutil.rmtree(str(self.root), ignore_errors=True)

    def test_manifest_records_template_hash_not_dest_hash(self):
        import hashlib  # noqa: PLC0415
        self_install.run_root_bundle_install(self.root, update_mode=False)
        rel = ".claude/scripts/kg-search"
        template = self.root / "templates" / "scripts" / "kg-search"
        template_hash = hashlib.sha256(template.read_bytes()).hexdigest()
        recorded = _manifest(self.root)["files"][rel]["sha256"]
        # PLAN-v0284/85: the manifest is the prior-SHIPPED hash.
        self.assertEqual(recorded, template_hash,
                         "manifest must record the SHIPPED (template) hash")

        # User modifies the runtime copy AFTER install — the manifest must
        # still hold the template hash (not the drifted dest hash), or the
        # next update would see installed==prior-shipped and silently lose it.
        script = self.root / ".claude" / "scripts" / "kg-search"
        script.write_text("print('user drift')\n", encoding="utf-8")
        recorded_after = _manifest(self.root)["files"][rel]["sha256"]
        dest_hash = hashlib.sha256(script.read_bytes()).hexdigest()
        self.assertEqual(recorded_after, template_hash)
        self.assertNotEqual(recorded_after, dest_hash)


class ForceFlagRegisteredTest(unittest.TestCase):
    """CARRIED FORWARD (was TestForceFlagRegistered): the
    --force-materialize-claude-dir CLI flag stays registered (install.py maps
    it to the bundle's --force). Black-box --help grep."""

    def test_force_flag_in_argparse(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--force-materialize-claude-dir", result.stdout,
                      "CLI flag missing from --help output")


if __name__ == "__main__":
    unittest.main()
