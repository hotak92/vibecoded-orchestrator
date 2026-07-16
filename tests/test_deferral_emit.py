# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""WP-B1 (v0.2.83): vco_lib.deferral_emit — the ONE emitter home.

Covers the public API contract (D7):

  * ``emit`` / ``emit_entries`` add entries under the shared lock and PRESERVE
    pre-existing FOREIGN entries (a writer must never clobber another writer's
    entries).
  * ``resolve_conditions`` drops the given conditions, returns how many were
    present, and DELETES ``UPDATE_DEFERRED.{md,json}`` when the report is
    emptied.
  * ``record_auto_resolution`` emits a loud log line AND appends a parseable
    JSONL row to ``.claude/logs/auto-resolutions.jsonl`` (B-F9: no silent
    mutations).
  * ``exclusive_file_lock`` degrades to best-effort no-lock when ``fcntl`` is
    unavailable (Windows) — the block still runs and writes.
  * Structural ratchet: no ``DeferralReport.read(`` triplet remains in the
    migrated writer modules (hard_cut / embedding_service / codegraph_resync).

The concurrent-writer serialization REGRESSION PIN lives in the companion
``tests/test_deferral_emit_concurrency.py`` (it needs real processes).
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.deferral_emit import (  # noqa: E402
    DeferralEntry,
    LOCK_REL,
    emit,
    emit_entries,
    locked_report,
    record_auto_resolution,
    resolve_conditions,
)
from vco_lib.deferral_report import (  # noqa: E402
    _DEFERRED_JSON_REL,
    _DEFERRED_REL,
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


class _TmpFolder(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def _persist(self, *cids: str) -> None:
        rep = DeferralReport()
        for cid in cids:
            rep.add_entry(_entry(cid))
        rep.write(self.folder)

    def _cids_on_disk(self) -> set:
        return {e.condition_id for e in DeferralReport.read(self.folder).entries}


class EmitTests(_TmpFolder):
    def test_emit_single_writes_entry(self):
        wrote = emit(self.folder, _entry("cond_a"))
        self.assertTrue(wrote)
        self.assertEqual(self._cids_on_disk(), {"cond_a"})
        self.assertTrue((self.folder / _DEFERRED_REL).exists())
        self.assertTrue((self.folder / _DEFERRED_JSON_REL).exists())

    def test_emit_entries_preserves_pre_existing_foreign(self):
        # A prior writer family left a foreign entry on disk.
        self._persist("foreign_thing")
        # A different writer emits its own two entries.
        wrote = emit_entries(
            self.folder, (_entry("mine_1"), _entry("mine_2"))
        )
        self.assertTrue(wrote)
        # BOTH the foreign entry and the new ones survive.
        self.assertEqual(
            self._cids_on_disk(), {"foreign_thing", "mine_1", "mine_2"}
        )

    def test_emit_entries_empty_is_noop_but_preserves(self):
        self._persist("foreign_thing")
        wrote = emit_entries(self.folder, ())
        # Report still holds the foreign entry ⇒ True (file present).
        self.assertTrue(wrote)
        self.assertEqual(self._cids_on_disk(), {"foreign_thing"})

    def test_emit_last_write_wins_per_condition(self):
        emit(self.folder, _entry("dup", title="first"))
        emit(self.folder, _entry("dup", title="second"))
        after = DeferralReport.read(self.folder)
        titles = [e.title for e in after.entries if e.condition_id == "dup"]
        self.assertEqual(titles, ["second"])


class ResolveConditionsTests(_TmpFolder):
    def test_resolve_removes_present_and_reports_count(self):
        self._persist("cond_a", "cond_b", "cond_c")
        removed = resolve_conditions(self.folder, ("cond_a", "cond_c"))
        self.assertEqual(removed, 2)
        self.assertEqual(self._cids_on_disk(), {"cond_b"})

    def test_resolve_absent_condition_counts_zero(self):
        self._persist("cond_a")
        removed = resolve_conditions(self.folder, ("not_here",))
        self.assertEqual(removed, 0)
        self.assertEqual(self._cids_on_disk(), {"cond_a"})

    def test_resolve_emptied_report_deletes_both_files(self):
        self._persist("only_one")
        self.assertTrue((self.folder / _DEFERRED_REL).exists())
        self.assertTrue((self.folder / _DEFERRED_JSON_REL).exists())
        removed = resolve_conditions(self.folder, ("only_one",))
        self.assertEqual(removed, 1)
        self.assertFalse((self.folder / _DEFERRED_REL).exists())
        self.assertFalse((self.folder / _DEFERRED_JSON_REL).exists())

    def test_resolve_preserves_foreign_entries(self):
        self._persist("mine", "foreign_thing")
        resolve_conditions(self.folder, ("mine",))
        self.assertEqual(self._cids_on_disk(), {"foreign_thing"})


class LockedReportTests(_TmpFolder):
    def test_locked_report_reads_yields_writes(self):
        self._persist("foreign_thing")
        with locked_report(self.folder) as report:
            self.assertTrue(report.has_condition("foreign_thing"))
            report.add_entry(_entry("added_inside"))
        self.assertEqual(self._cids_on_disk(), {"foreign_thing", "added_inside"})

    def test_locked_report_creates_lockfile_under_context(self):
        with locked_report(self.folder):
            pass
        # The lock token lives beside UPDATE_DEFERRED.* under .claude/context.
        self.assertEqual(
            LOCK_REL, Path(".claude") / "context" / ".update-deferred.lock"
        )
        self.assertTrue((self.folder / LOCK_REL).exists())


class RecordAutoResolutionTests(_TmpFolder):
    _JSONL_REL = Path(".claude") / "logs" / "auto-resolutions.jsonl"

    def test_appends_parseable_jsonl_row(self):
        record_auto_resolution(
            self.folder, "compose_override_filename_conflict",
            "re-mirrored legacy override", "byte-identical to canonical",
        )
        jsonl = self.folder / self._JSONL_REL
        self.assertTrue(jsonl.exists())
        lines = [ln for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["condition_id"], "compose_override_filename_conflict")
        self.assertEqual(row["action"], "re-mirrored legacy override")
        self.assertEqual(row["detail"], "byte-identical to canonical")
        self.assertIn("ts", row)

    def test_multiple_records_append(self):
        record_auto_resolution(self.folder, "c1", "a1", "d1")
        record_auto_resolution(self.folder, "c2", "a2", "d2")
        jsonl = self.folder / self._JSONL_REL
        lines = [ln for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        cids = [json.loads(ln)["condition_id"] for ln in lines]
        self.assertEqual(cids, ["c1", "c2"])

    def test_emits_loud_log_line(self):
        log = logging.getLogger("test_auto_resolution")
        with self.assertLogs(log, level="INFO") as cm:
            record_auto_resolution(
                self.folder, "searxng_removed_from_default_install",
                "removed searxng dir", "matched pinned hashes", log=log,
            )
        joined = "\n".join(cm.output)
        self.assertIn("auto-resolved: searxng_removed_from_default_install", joined)
        self.assertIn("removed searxng dir", joined)
        self.assertIn("matched pinned hashes", joined)

    def test_jsonl_write_failure_is_soft(self):
        # An unwritable target must be swallowed (observability, not a gate).
        log = logging.getLogger("test_auto_resolution_softfail")
        with mock.patch(
            "vco_lib.deferral_emit.open", side_effect=OSError("disk full")
        ):
            # Must not raise.
            record_auto_resolution(self.folder, "c", "a", "d", log=log)


class WindowsDegradationTests(_TmpFolder):
    """exclusive_file_lock must still yield + write when fcntl is unavailable
    (Windows / a filesystem without flock) — best-effort no-lock."""

    def test_emit_works_without_fcntl(self):
        real_import = __import__

        def _no_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                raise ImportError("no fcntl on this platform (simulated)")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=_no_fcntl):
            wrote = emit(self.folder, _entry("cond_no_lock"))
        self.assertTrue(wrote)
        self.assertEqual(self._cids_on_disk(), {"cond_no_lock"})

    def test_exclusive_file_lock_yields_without_fcntl(self):
        from vco_lib.atomic import exclusive_file_lock

        real_import = __import__

        def _no_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        ran = []
        lock_path = self.folder / LOCK_REL
        with mock.patch("builtins.__import__", side_effect=_no_fcntl):
            with exclusive_file_lock(lock_path):
                ran.append(True)
        self.assertEqual(ran, [True])
        self.assertTrue(lock_path.exists())


class StructuralRatchetTests(unittest.TestCase):
    """No ``DeferralReport.read(`` triplet may remain in the migrated writer
    modules — they route through deferral_emit now (WP-B1 ratchet). Guards
    against a future editor re-introducing an un-locked read-modify-write."""

    _MIGRATED = (
        "vco_lib/hard_cut.py",
        "vco_lib/embedding_service.py",
        "vco_lib/codegraph_resync.py",
    )

    def test_no_deferralreport_read_in_migrated_modules(self):
        for rel in self._MIGRATED:
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            offenders = [
                ln for ln in src.splitlines()
                if "DeferralReport.read(" in ln
                and not ln.lstrip().startswith("#")
            ]
            self.assertEqual(
                offenders, [],
                f"{rel} still holds a DeferralReport.read( triplet — it must "
                f"route through vco_lib.deferral_emit (WP-B1).",
            )

    def test_migrated_modules_import_deferral_emit(self):
        for rel in self._MIGRATED:
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(
                "deferral_emit", src,
                f"{rel} should import from vco_lib.deferral_emit after WP-B1.",
            )


if __name__ == "__main__":
    unittest.main()
