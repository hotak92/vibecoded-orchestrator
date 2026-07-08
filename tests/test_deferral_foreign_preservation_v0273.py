# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A-2 (v0.2.73): install.py's deferral write must not clobber FOREIGN entries.

``UPDATE_DEFERRED.md`` has multiple writer families (install.py,
``vco_lib.project_init``, Rust emitters, background resync children).
install.py rebuilds the file from a fresh in-memory ``DeferralReport`` at end
of run — pre-A-2 that silently DELETED any persisted entry whose condition
install.py did not re-detect that run (e.g. a pending codegraph-resync
deferral, a chunker-preset overhaul entry).

Covers:
  * ``condition_is_owned`` — exact IDs + prefix families.
  * ``DeferralReport.merge_from_disk`` — foreign preserved, owned excluded,
    in-memory entry wins over the disk copy, missing/corrupt file → 0 merged.
  * end-to-end: seed → run adds its own entries → write → FOREIGN entry
    survives an update that did not re-detect it (the A-2 acceptance test).
  * install.py's ownership set does NOT claim the resync ledger entry.
"""

from __future__ import annotations

import sys
import unittest
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.deferral_report import (  # noqa: E402
    DeferralEntry,
    DeferralReport,
    condition_is_owned,
)


def _entry(cid: str, title: str = "T") -> DeferralEntry:
    return DeferralEntry(
        condition_id=cid,
        title=title,
        detected="detected text",
        why_deferred="needs consent",
        command_to_apply="some-command --apply",
        severity="warning",
    )


class TestConditionIsOwned(unittest.TestCase):
    def test_exact_id_match(self):
        self.assertTrue(condition_is_owned("a_cond", {"a_cond", "b_cond"}))
        self.assertFalse(condition_is_owned("c_cond", {"a_cond", "b_cond"}))

    def test_prefix_match(self):
        self.assertTrue(
            condition_is_owned(
                "schema_migration_failed_xyz", set(),
                ("schema_migration_failed_",),
            )
        )
        self.assertFalse(
            condition_is_owned(
                "chunker_preset_overhaul", set(),
                ("schema_migration_failed_",),
            )
        )

    def test_empty_prefix_never_matches_everything(self):
        # A stray "" in the prefix tuple must not claim every condition.
        self.assertFalse(condition_is_owned("anything", set(), ("",)))


class TestMergeFromDisk(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _persist(self, *entries: DeferralEntry) -> None:
        rep = DeferralReport()
        for e in entries:
            rep.add_entry(e)
        rep.write(self.folder)

    def test_foreign_entry_merged(self):
        self._persist(_entry("chunker_preset_overhaul"))
        rep = DeferralReport()
        merged = rep.merge_from_disk(self.folder, exclude_ids={"owned_thing"})
        self.assertEqual(merged, 1)
        self.assertTrue(rep.has_condition("chunker_preset_overhaul"))

    def test_owned_entry_excluded(self):
        self._persist(_entry("owned_thing"), _entry("foreign_thing"))
        rep = DeferralReport()
        merged = rep.merge_from_disk(self.folder, exclude_ids={"owned_thing"})
        self.assertEqual(merged, 1)
        self.assertFalse(rep.has_condition("owned_thing"))
        self.assertTrue(rep.has_condition("foreign_thing"))

    def test_owned_prefix_excluded(self):
        self._persist(_entry("dyn_family_abc"), _entry("foreign_thing"))
        rep = DeferralReport()
        merged = rep.merge_from_disk(
            self.folder, exclude_ids=set(), exclude_prefixes=("dyn_family_",)
        )
        self.assertEqual(merged, 1)
        self.assertFalse(rep.has_condition("dyn_family_abc"))

    def test_in_memory_entry_wins_over_disk(self):
        self._persist(_entry("shared_cid", title="stale disk copy"))
        rep = DeferralReport()
        rep.add_entry(_entry("shared_cid", title="fresh in-memory"))
        merged = rep.merge_from_disk(self.folder, exclude_ids=set())
        self.assertEqual(merged, 0)
        titles = [e.title for e in rep.entries if e.condition_id == "shared_cid"]
        self.assertEqual(titles, ["fresh in-memory"])

    def test_missing_file_merges_nothing(self):
        rep = DeferralReport()
        self.assertEqual(rep.merge_from_disk(self.folder, exclude_ids=set()), 0)
        self.assertEqual(len(rep), 0)

    def test_corrupt_file_soft_fails(self):
        target = self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"\x00\xff not markdown \x00")
        rep = DeferralReport()
        # Must not raise; unparseable content merges nothing.
        self.assertEqual(rep.merge_from_disk(self.folder, exclude_ids=set()), 0)


class TestForeignSurvivesUpdateWrite(unittest.TestCase):
    """The A-2 acceptance test: a FOREIGN entry survives a full
    seed → detect-own-conditions → write cycle that does not re-detect it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_foreign_survives_write_owned_drops(self):
        # Previous state on disk: one foreign entry (another writer family's)
        # + one owned entry whose condition is now resolved (not re-detected).
        prior = DeferralReport()
        prior.add_entry(_entry("codegraph_embed_resync_pending"))
        prior.add_entry(_entry("owned_resolved_thing"))
        prior.write(self.folder)

        # The update run: fresh report, seeded from disk, re-detects only its
        # own NEW condition; the owned_resolved_thing is NOT re-detected.
        run = DeferralReport()
        run.merge_from_disk(self.folder, exclude_ids={"owned_resolved_thing"})
        run.add_entry(_entry("newly_detected_owned"))
        run.write(self.folder)

        after = DeferralReport.read(self.folder)
        cids = {e.condition_id for e in after.entries}
        self.assertIn("codegraph_embed_resync_pending", cids,
                      "foreign entry must survive the update write")
        self.assertIn("newly_detected_owned", cids)
        self.assertNotIn("owned_resolved_thing", cids,
                         "owned entries keep drop-when-absent semantics")

    def test_only_foreign_entries_still_written(self):
        prior = DeferralReport()
        prior.add_entry(_entry("foreign_only"))
        prior.write(self.folder)

        run = DeferralReport()
        run.merge_from_disk(self.folder, exclude_ids={"anything_owned"})
        wrote = run.write(self.folder)
        self.assertTrue(wrote, "a foreign-only report must still write the file")
        after = DeferralReport.read(self.folder)
        self.assertTrue(after.has_condition("foreign_only"))


class TestInstallOwnershipSet(unittest.TestCase):
    """Guards on install.py's ownership constants (import-shape only —
    running main() is out of scope for a unit test)."""

    @classmethod
    def setUpClass(cls):
        # install.py executes heavy top-level code on import; parse the
        # constants textually instead (stable anchors).
        cls.source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")

    def test_resync_ledger_cid_not_owned(self):
        """`codegraph_embed_resync_pending` must NEVER enter the owned set —
        it is an owed-work ledger entry resolved explicitly by the probe."""
        start = self.source.index("_INSTALL_OWNED_CONDITION_IDS = frozenset({")
        end = self.source.index("})", start)
        block = self.source[start:end]
        self.assertNotIn("codegraph_embed_resync_pending", block)

    def test_owned_set_and_prefixes_exist(self):
        self.assertIn("_INSTALL_OWNED_CONDITION_IDS = frozenset({", self.source)
        self.assertIn("_INSTALL_OWNED_CONDITION_PREFIXES = (", self.source)
        # P2c-b (v0.2.75): the seed/finalize choreography moved to
        # vco_lib.install_deferral_flow — main() must hold the A-2 seed
        # call site (the flow is constructed with the owned sets above).
        self.assertIn("_deferral_flow.seed(", self.source)

    def test_mid_run_write_removed(self):
        """A-11: exactly ONE write moment remains — P2c-b moved it into
        `InstallDeferralFlow.finalize()` (late-merge + single write), so
        install.py must hold exactly ONE `_deferral_flow.finalize(` call
        site and ZERO direct `_deferral_report.write(` calls. The
        flow-module-side single-write guard lives in
        tests/test_install_deferral_flow_v0275.py. Comment/docstring
        mentions are excluded by matching code lines only."""
        def _code_lines(needle):
            return [
                ln for ln in self.source.splitlines()
                if needle in ln
                and not ln.lstrip().startswith("#")
                and "``" not in ln
            ]

        self.assertEqual(_code_lines("_deferral_report.write("), [],
                         "no direct report write may remain in install.py")
        finalize_lines = _code_lines("_deferral_flow.finalize(")
        self.assertEqual(len(finalize_lines), 1, finalize_lines)
        self.assertIn("_final = _deferral_flow.finalize(", finalize_lines[0])


if __name__ == "__main__":
    unittest.main()
