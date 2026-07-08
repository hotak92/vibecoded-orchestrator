# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P2c-b (v0.2.75): InstallDeferralFlow — the extracted deferral choreography.

The A-2 seed / P1 pre-write late-merge / A-11 single-final-write lifecycle
moved from install.py main() into
``vco_lib.install_deferral_flow.InstallDeferralFlow``. These tests pin the
flow object's semantics (the end-to-end choreography tests against
install.py's owned sets live in ``tests/test_deferral_toctou_v0275.py``;
the install.py call-site structural guards live there and in
``tests/test_deferral_foreign_preservation_v0273.py``).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.deferral_report import (  # noqa: E402
    DeferralEntry,
    DeferralReport,
)
from vco_lib.install_deferral_flow import InstallDeferralFlow  # noqa: E402


def _entry(cid: str, title: str = "T") -> DeferralEntry:
    return DeferralEntry(
        condition_id=cid,
        title=title,
        detected="detected text",
        why_deferred="needs consent",
        command_to_apply="some-command --apply",
        severity="warning",
    )


class _FlowCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def flow(self, owned=("owned_thing",), prefixes=("owned_family_",)):
        return InstallDeferralFlow(
            folder=self.folder, owned_ids=owned, owned_prefixes=prefixes
        )

    def _persist(self, *cids: str) -> None:
        prior = DeferralReport()
        for cid in cids:
            prior.add_entry(_entry(cid))
        prior.write(self.folder)


class SeedTests(_FlowCase):
    def test_seed_imports_foreign_excludes_owned(self):
        self._persist("owned_thing", "owned_family_x", "foreign_thing")
        f = self.flow()
        self.assertEqual(f.seed(), 1)
        self.assertTrue(f.report.has_condition("foreign_thing"))
        self.assertFalse(f.report.has_condition("owned_thing"))
        self.assertFalse(f.report.has_condition("owned_family_x"))

    def test_seed_empty_disk_merges_nothing(self):
        self.assertEqual(self.flow().seed(), 0)


class FinalizeTests(_FlowCase):
    def test_finalize_is_late_merge_then_write(self):
        """The TOCTOU shape through the flow: a child entry written AFTER
        seed must survive the final write."""
        f = self.flow()
        f.seed()
        f.report.add_entry(_entry("owned_thing"))

        # Mid-run: detached child records a NEW foreign entry on disk.
        child = DeferralReport.read(self.folder)
        child.add_entry(_entry("codegraph_embed_resync_pending"))
        child.write(self.folder)

        result = f.finalize()
        self.assertEqual(result.late_merged, 1)
        self.assertIsNone(result.merge_error)
        self.assertTrue(result.wrote_entries)

        after = DeferralReport.read(self.folder)
        cids = {e.condition_id for e in after.entries}
        self.assertEqual(cids, {"owned_thing", "codegraph_embed_resync_pending"})

    def test_finalize_honors_tombstones(self):
        """A probe-resolved entry (mark_resolved on the flow's report;
        stale copy still on disk) must NOT resurrect through finalize."""
        self._persist("codegraph_embed_resync_pending")
        f = self.flow()
        f.seed()
        f.report.mark_resolved("codegraph_embed_resync_pending")
        f.report.add_entry(_entry("owned_thing"))

        result = f.finalize()
        self.assertTrue(result.wrote_entries)
        after = DeferralReport.read(self.folder)
        self.assertFalse(after.has_condition("codegraph_embed_resync_pending"))
        self.assertTrue(after.has_condition("owned_thing"))

    def test_finalize_owned_drop_when_absent(self):
        """An owned entry on disk that the run did NOT re-detect stays
        dropped through both the seed and the finalize merge."""
        self._persist("owned_thing", "foreign_thing")
        f = self.flow()
        f.seed()
        f.finalize()
        after = DeferralReport.read(self.folder)
        self.assertFalse(after.has_condition("owned_thing"))
        self.assertTrue(after.has_condition("foreign_thing"))

    def test_finalize_empty_report_deletes_file_and_reports_no_write(self):
        self._persist("owned_thing")  # only an owned entry on disk
        f = self.flow()
        f.seed()  # imports nothing
        result = f.finalize()
        self.assertFalse(result.wrote_entries)
        self.assertFalse(
            (self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md").exists()
        )
        self.assertFalse(
            (self.folder / ".claude" / "context" / "UPDATE_DEFERRED.json").exists()
        )

    def test_finalize_merge_soft_fails_but_write_proceeds(self):
        """A late-merge failure lands in merge_error; the run's own entries
        are STILL written (a merge failure must not lose them too)."""
        f = self.flow()
        f.report.add_entry(_entry("owned_thing"))
        with mock.patch.object(
            f.report, "merge_from_disk", side_effect=RuntimeError("disk gone")
        ):
            result = f.finalize()
        self.assertEqual(result.merge_error, "disk gone")
        self.assertEqual(result.late_merged, 0)
        self.assertTrue(result.wrote_entries)
        after = DeferralReport.read(self.folder)
        self.assertTrue(after.has_condition("owned_thing"))

    def test_finalize_write_failure_propagates(self):
        """The caller owns the final-write soft-fail logging — finalize
        must let the write exception through."""
        f = self.flow()
        f.report.add_entry(_entry("owned_thing"))
        with mock.patch.object(
            f.report, "write", side_effect=OSError("read-only fs")
        ):
            with self.assertRaises(OSError):
                f.finalize()


class FlowModuleStructureTests(unittest.TestCase):
    """Structural guards on the flow module itself: exactly ONE write call,
    and finalize merges BEFORE it (the install.py-side guards — one seed
    call, one finalize call, no direct report I/O — live in the deferral
    suites)."""

    @classmethod
    def setUpClass(cls):
        cls.source = (
            REPO_ROOT / "vco_lib" / "install_deferral_flow.py"
        ).read_text(encoding="utf-8")

    def _code_lines(self, needle: str) -> list:
        return [
            ln for ln in self.source.splitlines()
            if needle in ln
            and not ln.lstrip().startswith("#")
            and "``" not in ln
        ]

    def test_exactly_one_write_call_in_flow(self):
        lines = self._code_lines(".write(")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("wrote_entries = self.report.write(", lines[0])

    def test_finalize_merges_before_the_single_write(self):
        merge_idx = self.source.index(
            "late_merged = self._merge_foreign_from_disk()"
        )
        write_idx = self.source.index("wrote_entries = self.report.write(")
        self.assertLess(merge_idx, write_idx)


if __name__ == "__main__":
    unittest.main()
