# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.83 PLAN-v0283 B-F5: orphan auto-keep + manifest-entry retirement.

When the orchestrator stops shipping a file the user MODIFIED (upstream
deletion + hash-diverged local copy), pre-.83 emitted a
`bundle_user_modified_deletion_preserved` deferral and kept the manifest entry
forever. B-F5 replaces that with AUTO-KEEP + RETIRE:

  * ACT: the file stays on disk (NEVER deleted), the manifest entry is RETIRED
    (dropped), it lands in `orphan-retired`, NO deferral is emitted, and an
    auto-resolutions.jsonl line is recorded.
  * LEAVE-ALONE: a file the user modified that is STILL shipped (not an orphan)
    still produces `bundle_user_modified_preserved` — B-F5 does not touch that
    path.
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

from tests._v0283_deferral_emit_fake import (  # noqa: E402
    install_fake_deferral_emit,
    read_auto_resolutions,
)

install_fake_deferral_emit()

from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402
from tests.test_install_bundle import _make_fake_orchestrator  # noqa: E402


class OrphanAutoKeepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-b2-orphan-"))
        self.orch = self.tmp / "orchestrator"
        self.proj = self.tmp / "project"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)
        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=False,
        )
        self.assertEqual(result["errors"], [], f"first install failed: {result}")

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _foo_hook_path(self) -> Path:
        ext = "ps1" if platform.system() == "Windows" else "sh"
        return self.proj / ".claude" / "hooks" / f"foo.{ext}"

    def _delete_foo_from_orchestrator(self) -> None:
        for ext in ("sh", "ps1"):
            tmpl = self.orch / "templates" / "hooks" / f"foo.{ext}"
            if tmpl.exists():
                tmpl.unlink()

    def _rel(self) -> str:
        ext = "ps1" if platform.system() == "Windows" else "sh"
        return str(Path(".claude") / "hooks" / f"foo.{ext}")

    # -- ACT ----------------------------------------------------------------

    def test_act_user_modified_orphan_kept_manifest_retired_no_deferral(self):
        installed = self._foo_hook_path()
        installed.write_text("# USER CUSTOM\n", encoding="utf-8")
        self._delete_foo_from_orchestrator()

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        rel = self._rel()

        # Recorded as orphan-retired (not orphan-preserved).
        self.assertIn(rel, result["actions"]["orphan-retired"])
        self.assertNotIn(rel, result["actions"]["orphan-preserved"])
        # File kept on disk, content untouched.
        self.assertTrue(installed.exists())
        self.assertEqual(installed.read_text(encoding="utf-8"), "# USER CUSTOM\n")
        # Manifest entry retired.
        manifest = json.loads(
            (self.proj / ".claude" / ".vco-manifest.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(rel, manifest["files"])
        # NO deferral.
        report = DeferralReport.read(self.proj)
        self.assertFalse(
            report.has_condition("bundle_user_modified_deletion_preserved")
        )
        # Auto-resolution recorded.
        rows = read_auto_resolutions(self.proj)
        self.assertTrue(
            any(
                r["condition_id"] == "bundle_user_modified_deletion_preserved"
                and r["action"] == "retired_orphan_manifest_entry"
                for r in rows
            ),
            f"expected retire auto-resolution row; got {rows}",
        )

    def test_act_clears_preexisting_stale_deletion_deferral(self):
        """A pre-.83 stale deletion-preserved entry on disk self-clears on the
        retire run (still_orphan_preserved becomes False)."""
        from vco_lib.deferral_report import DeferralEntry
        seed = DeferralReport.read(self.proj)
        seed.add_entry(DeferralEntry(
            condition_id="bundle_user_modified_deletion_preserved",
            title="stale", detected="stale", why_deferred="stale",
            command_to_apply="noop", severity="info",
        ))
        seed.write(self.proj)

        installed = self._foo_hook_path()
        installed.write_text("# USER CUSTOM\n", encoding="utf-8")
        self._delete_foo_from_orchestrator()

        project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        self.assertFalse(
            DeferralReport.read(self.proj)
            .has_condition("bundle_user_modified_deletion_preserved"),
            "retire run must clear the pre-existing stale deferral",
        )

    # -- LEAVE-ALONE --------------------------------------------------------

    def test_leave_alone_still_shipped_user_modified_file_preserved(self):
        """A user-modified file that is STILL shipped (NOT an orphan) still
        emits `bundle_user_modified_preserved` — B-F5 must not touch this."""
        installed = self._foo_hook_path()
        installed.write_text("# USER EDIT\n", encoding="utf-8")
        # NOTE: we do NOT delete foo from the orchestrator — it is still shipped,
        # so it is user-modified-but-not-orphan.

        result = project_init.install_project_bundle(
            self.proj, orchestrator_root=self.orch, update_mode=True,
        )
        rel = self._rel()
        # Preserve action + the DIFFERENT deferral.
        self.assertIn(rel, result["actions"]["preserve"])
        self.assertNotIn(rel, result["actions"]["orphan-retired"])
        report = DeferralReport.read(self.proj)
        self.assertTrue(report.has_condition("bundle_user_modified_preserved"))
        self.assertFalse(
            report.has_condition("bundle_user_modified_deletion_preserved")
        )


if __name__ == "__main__":
    unittest.main()
