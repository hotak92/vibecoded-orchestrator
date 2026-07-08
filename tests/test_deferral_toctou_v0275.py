# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P1 (v0.2.75): close the deferral-write TOCTOU without resurrecting
resolved entries.

install.py seeds its run report from disk at t0 (A-2) and performs a
SINGLE rebuild-from-memory write at end of run (A-11). Between the two,
detached children (the P7 resync driver, MCP emitters) can read-merge-write
NEW foreign entries to disk — pre-P1 the final write clobbered them.

The fix is a second ``merge_from_disk`` immediately before the final write.
THE TRAP that made a naive merge wrong: entries this run EXPLICITLY
resolved via ``mark_resolved`` (the R-6 not_owed probe clearing the FOREIGN
``codegraph_embed_resync_pending`` ledger entry) are cleared from MEMORY
only — their on-disk copy still exists until the final write lands, so a
plain merge re-imports them and the ledger never clears. ``mark_resolved``
therefore tombstones the condition ID for the run, and ``merge_from_disk``
skips tombstoned IDs.

Covers:
  * mid-run foreign ADD survives the final write (the TOCTOU itself);
  * probe-resolved entry does NOT resurrect through the pre-write merge;
  * tombstone unit semantics (mark_resolved → merge skips; re-add revives);
  * --apply-deferred resolved arms actually clear a FOREIGN entry
    end-to-end (seed → apply → re-merge → write) — pre-fix, the
    'do NOT re-add' resolution was inert for foreign cids because the
    A-2 seed had already imported the on-disk copy;
  * structural guard: install.py carries BOTH merge_from_disk call sites
    (A-2 seed + P1 pre-write re-merge) and still exactly ONE write.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # type: ignore  # noqa: E402
from vco_lib.deferral_report import (  # noqa: E402
    DeferralEntry,
    DeferralReport,
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


class TestMidRunForeignAddSurvives(unittest.TestCase):
    """(a) A child writing a NEW foreign entry between seed and final
    write must survive the final rebuild-from-memory write."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_child_entry_written_mid_run_survives_final_write(self):
        owned = {"owned_thing"}

        # t0: install.py's run report, seeded from (empty) disk.
        run = DeferralReport()
        run.merge_from_disk(self.folder, exclude_ids=owned)
        run.add_entry(_entry("owned_thing"))

        # t1 (mid-run): a detached child records a NEW foreign entry on
        # disk via the standard read-merge-write (codegraph_resync.py's
        # _record_unconverged_deferral shape).
        child = DeferralReport.read(self.folder)
        child.add_entry(_entry("codegraph_embed_resync_pending"))
        child.write(self.folder)

        # t2 (end of run): P1 pre-write re-merge + the single final write.
        run.merge_from_disk(self.folder, exclude_ids=owned)
        run.write(self.folder)

        after = DeferralReport.read(self.folder)
        cids = {e.condition_id for e in after.entries}
        self.assertIn(
            "codegraph_embed_resync_pending", cids,
            "mid-run foreign entry must survive the final write (P1 TOCTOU)",
        )
        self.assertIn("owned_thing", cids)

    def test_owned_ids_still_drop_when_absent_after_late_merge(self):
        """The pre-write merge must not weaken owned drop-when-absent
        semantics: an owned entry present on disk but NOT re-detected this
        run stays dropped even though the merge runs twice."""
        owned = {"owned_resolved_thing"}
        prior = DeferralReport()
        prior.add_entry(_entry("owned_resolved_thing"))
        prior.add_entry(_entry("foreign_thing"))
        prior.write(self.folder)

        run = DeferralReport()
        run.merge_from_disk(self.folder, exclude_ids=owned)   # A-2 seed
        run.merge_from_disk(self.folder, exclude_ids=owned)   # P1 re-merge
        run.write(self.folder)

        after = DeferralReport.read(self.folder)
        cids = {e.condition_id for e in after.entries}
        self.assertNotIn("owned_resolved_thing", cids)
        self.assertIn("foreign_thing", cids)


class TestResolvedEntryDoesNotResurrect(unittest.TestCase):
    """(b) THE RESURRECT TRAP: a probe-resolved ledger entry (mark_resolved
    called; on-disk copy still present because the single write hasn't
    happened yet) must be ABSENT after the final write."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_probe_resolved_foreign_entry_not_resurrected(self):
        owned = {"owned_thing"}

        # Prior state: the FOREIGN resync ledger entry persisted by an
        # earlier run's child.
        prior = DeferralReport()
        prior.add_entry(_entry("codegraph_embed_resync_pending"))
        prior.write(self.folder)

        # The update run: A-2 seed imports the foreign entry.
        run = DeferralReport()
        run.merge_from_disk(self.folder, exclude_ids=owned)
        self.assertTrue(run.has_condition("codegraph_embed_resync_pending"))

        # R-6 owed-probe returns not_owed → install.py clears it from
        # MEMORY. The on-disk copy still exists (no write happened).
        run.mark_resolved("codegraph_embed_resync_pending")

        # P1 pre-write re-merge + single final write. Without the
        # tombstone the merge would re-import the stale disk copy here.
        run.merge_from_disk(self.folder, exclude_ids=owned)
        run.add_entry(_entry("owned_thing"))
        run.write(self.folder)

        after = DeferralReport.read(self.folder)
        cids = {e.condition_id for e in after.entries}
        self.assertNotIn(
            "codegraph_embed_resync_pending", cids,
            "probe-resolved entry must NOT resurrect through the P1 merge",
        )
        self.assertIn("owned_thing", cids)

    def test_mark_resolved_tombstone_blocks_merge(self):
        """Unit shape: mark_resolved tombstones the ID even when it was
        never in memory (probe settling an on-disk-only ledger entry)."""
        prior = DeferralReport()
        prior.add_entry(_entry("some_ledger_cid"))
        prior.write(self.folder)

        run = DeferralReport()
        run.mark_resolved("some_ledger_cid")  # never seeded into memory
        merged = run.merge_from_disk(self.folder, exclude_ids=set())
        self.assertEqual(merged, 0)
        self.assertFalse(run.has_condition("some_ledger_cid"))

    def test_re_add_after_resolve_revives_condition(self):
        """add_entry after mark_resolved supersedes the tombstone: the
        condition is live again and must be written."""
        run = DeferralReport()
        run.mark_resolved("flappy_cid")
        run.add_entry(_entry("flappy_cid", title="re-detected"))
        run.write(self.folder)

        after = DeferralReport.read(self.folder)
        self.assertTrue(after.has_condition("flappy_cid"))

    def test_tombstone_scoped_to_instance(self):
        """Tombstones are per-run (per-instance): a FRESH report (the next
        install run) merges the entry normally if it is still on disk."""
        prior = DeferralReport()
        prior.add_entry(_entry("sticky_cid"))
        prior.write(self.folder)

        first = DeferralReport()
        first.mark_resolved("sticky_cid")
        self.assertEqual(first.merge_from_disk(self.folder, exclude_ids=set()), 0)

        fresh = DeferralReport()
        self.assertEqual(fresh.merge_from_disk(self.folder, exclude_ids=set()), 1)
        self.assertTrue(fresh.has_condition("sticky_cid"))


class TestApplyDeferredForeignResolveClears(unittest.TestCase):
    """--apply-deferred resolved arms must clear a FOREIGN entry through
    the FULL main() choreography (A-2 seed → apply → P1 re-merge → write).

    Pre-fix, every resolved arm 'cleared' by simply not re-adding the
    entry — inert for `launcher_update_diverged` (Rust-emitted, NOT in
    install.py's owned set): the A-2 seed had already imported the disk
    copy into memory, so the final write persisted it forever. The arms
    now call current_run_report.mark_resolved(cid), which drops the
    seeded copy AND tombstones the cid against the P1 pre-write merge."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_run_report(self) -> DeferralReport:
        run = DeferralReport()
        run.merge_from_disk(
            self.folder,
            exclude_ids=install._INSTALL_OWNED_CONDITION_IDS,
            exclude_prefixes=install._INSTALL_OWNED_CONDITION_PREFIXES,
        )
        return run

    def test_foreign_diverged_entry_clears_end_to_end(self):
        cid = "launcher_update_diverged"
        prior = DeferralReport()
        prior.add_entry(_entry(cid))
        prior.write(self.folder)

        # A-2 seed: the cid is FOREIGN → imported into memory.
        run = self._seed_run_report()
        self.assertTrue(run.has_condition(cid),
                        "premise: launcher_update_diverged must be FOREIGN "
                        "(seeded) — if this fails it was added to the owned "
                        "set and this test needs a different foreign cid")

        # Make the handler's re-probe succeed: on-disk binary metadata at
        # the source version (hub sidecar absent → treated as OK).
        subdir, fname = install._launcher_binary_relative_path()
        dist = self.folder / "launcher" / "dist" / subdir
        dist.mkdir(parents=True)
        (dist / f"{fname}.metadata.json").write_text(
            json.dumps({"launcher_version": "0.2.75"}), encoding="utf-8"
        )
        with mock.patch.object(
            install, "_read_launcher_version", return_value="0.2.75"
        ):
            install._apply_deferred_entries(run, self.folder, args=None)

        self.assertFalse(run.has_condition(cid),
                         "resolved arm must clear the seeded copy")

        # P1 pre-write re-merge (disk copy still present!) + final write.
        run.merge_from_disk(
            self.folder,
            exclude_ids=install._INSTALL_OWNED_CONDITION_IDS,
            exclude_prefixes=install._INSTALL_OWNED_CONDITION_PREFIXES,
        )
        run.write(self.folder)

        after = DeferralReport.read(self.folder)
        self.assertFalse(
            after.has_condition(cid),
            "apply-resolved FOREIGN entry must stay cleared through the "
            "P1 re-merge + final write",
        )

    def test_foreign_diverged_entry_kept_when_probe_fails(self):
        """Keep-side sanity: when the binary has NOT reached the source
        version, the entry survives the full cycle (re-probe discipline:
        keep on unconfirmed premise)."""
        cid = "launcher_update_diverged"
        prior = DeferralReport()
        prior.add_entry(_entry(cid))
        prior.write(self.folder)

        run = self._seed_run_report()
        subdir, fname = install._launcher_binary_relative_path()
        dist = self.folder / "launcher" / "dist" / subdir
        dist.mkdir(parents=True)
        (dist / f"{fname}.metadata.json").write_text(
            json.dumps({"launcher_version": "0.2.60"}), encoding="utf-8"
        )
        with mock.patch.object(
            install, "_read_launcher_version", return_value="0.2.75"
        ):
            install._apply_deferred_entries(run, self.folder, args=None)

        run.merge_from_disk(
            self.folder,
            exclude_ids=install._INSTALL_OWNED_CONDITION_IDS,
            exclude_prefixes=install._INSTALL_OWNED_CONDITION_PREFIXES,
        )
        run.write(self.folder)
        after = DeferralReport.read(self.folder)
        self.assertTrue(after.has_condition(cid),
                        "unresolved entry must be kept (probe not met)")


class TestInstallPreWriteMergeStructure(unittest.TestCase):
    """Structural guards on install.py (import-shape only — running main()
    is out of scope; matches TestInstallOwnershipSet's approach in
    tests/test_deferral_foreign_preservation_v0273.py)."""

    @classmethod
    def setUpClass(cls):
        cls.source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")

    def test_two_merge_from_disk_call_sites(self):
        """A-2 seed + P1 pre-write re-merge: exactly TWO real
        `_deferral_report.merge_from_disk(` call sites."""
        call_lines = [
            ln for ln in self.source.splitlines()
            if "_deferral_report.merge_from_disk(" in ln
            and not ln.lstrip().startswith("#")
            and "``" not in ln
        ]
        self.assertEqual(len(call_lines), 2, call_lines)

    def test_single_write_invariant_still_holds(self):
        """P1 must not add a write call site (A-11 invariant; the canonical
        assertion lives in test_deferral_foreign_preservation_v0273.py —
        re-asserted here so a P1 regression fails close to its cause)."""
        call_lines = [
            ln for ln in self.source.splitlines()
            if "_deferral_report.write(" in ln
            and not ln.lstrip().startswith("#")
            and "``" not in ln
        ]
        self.assertEqual(len(call_lines), 1, call_lines)


if __name__ == "__main__":
    unittest.main()
