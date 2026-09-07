# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — "VCO retries this itself" must survive contact with reality.

THE PROMISE THIS CLOSES (WFT C7, found-along-the-way register item 7)
--------------------------------------------------------------------
``codegraph_embed_resync_pending`` is classed ``auto_retryable``, and every
surface renders that tier as *"VCO retries this itself"* — the ledger's
disposition line, the launcher panel, ``vco doctor``'s owed-work finding. The
sentence is true of the CLASSIFICATION and can be false of the last three
weeks: the retry is gated on POSITIVE evidence that the code-embed backend is
reachable, so on a machine where that service stays down, VCO never retries at
all and nothing anywhere says so.

**The plan assumed the count already existed.** It did not. Measured before
writing a line of this: after five dispatch passes with the backend down,
``attempts_path`` did not exist and ``attempt_count`` returned 0 — the backend
gate returned early WITHOUT recording anything durable, so "VCO has retried N
times and the service was down each time" was not a sentence the code could
form. The fix is therefore two-part: record the fact, then render it.

A LEDGER DISPOSITION IS USER-FACING TEXT AND IS SHIPPED CODE (R16 category 3).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import deferral_retry, doctor  # noqa: E402

CID = "codegraph_embed_resync_pending"
KG_CID = "kg_sync_no_embedding_backend"


def _runner(rc: int = 0):
    def _run(argv, cwd):
        return rc
    return _run


def _passes(folder: Path, n: int, *, backend, cid=CID, rc=0):
    for _ in range(n):
        deferral_retry.dispatch(
            folder, condition_ids=[cid],
            backend_probe=lambda f, k: backend, runner=_runner(rc),
            single_instance=False,
        )


class BlockedPassesAreRecordedTests(unittest.TestCase):
    """Part one: the fact has to exist before it can be told."""

    def test_a_blocked_pass_now_leaves_a_durable_row(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            with mock.patch.object(deferral_retry, "trail"):
                _passes(folder, 3, backend=False)
            rows = [
                json.loads(line)
                for line in deferral_retry.attempts_path(folder)
                .read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual([r["status"] for r in rows], [deferral_retry.BLOCKED] * 3)
        self.assertTrue(all(r["condition_id"] == CID for r in rows))
        self.assertIn("backend", rows[0]["detail"])

    def test_a_blocked_pass_does_not_burn_the_attempt_cap(self):
        """The property `test_skips_do_not_burn_the_cap` protects: a machine
        whose backend is down for a week must still retry on the day it comes
        back. Recording the block must not change that.

        Measured through the cap's OWN input (`attempt_count`, which counts
        STARTED rows), not through the returned status — the handler in a bare
        temp folder finds no analyzer and skips, and that is downstream of the
        gate this test is about.
        """
        with TemporaryDirectory() as td:
            folder = Path(td)
            with mock.patch.object(deferral_retry, "trail"):
                _passes(folder, 10, backend=False)
                self.assertEqual(
                    deferral_retry.attempt_count(folder, CID), 0,
                    "ten blocked passes must consume no attempts")
                deferral_retry.dispatch(
                    folder, condition_ids=[CID],
                    backend_probe=lambda f, k: True, runner=_runner(0),
                    single_instance=False,
                )
                self.assertEqual(
                    deferral_retry.attempt_count(folder, CID), 1,
                    "the pass after the backend returns must still dispatch")
                history = deferral_retry.retry_history(folder, CID)
        self.assertEqual(history.blocked, 10)
        self.assertEqual(history.blocked_streak, 0,
                         "the run ended when a real attempt happened")

    def test_the_returned_status_is_still_SKIPPED(self):
        """BLOCKED is a TRAIL status only. Callers and their tests depend on
        the backend gate returning SKIPPED, and this change must not rename a
        result anybody reads."""
        with TemporaryDirectory() as td:
            with mock.patch.object(deferral_retry, "trail"):
                results = deferral_retry.dispatch(
                    Path(td), condition_ids=[CID],
                    backend_probe=lambda f, k: False, runner=_runner(0),
                    single_instance=False,
                )
        self.assertEqual([r.status for r in results], [deferral_retry.SKIPPED])
        self.assertNotEqual(deferral_retry.BLOCKED, deferral_retry.SKIPPED)

    def test_an_unknown_backend_is_recorded_as_blocked_too(self):
        """"Could not tell" is treated as "no" by the gate, so it is the same
        user-visible non-event and belongs in the same history."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            with mock.patch.object(deferral_retry, "trail"):
                _passes(folder, 2, backend=None)
            history = deferral_retry.retry_history(folder, CID)
            rows = [
                json.loads(line)
                for line in deferral_retry.attempts_path(folder)
                .read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(history.blocked, 2)
        self.assertEqual(history.last_status, deferral_retry.BLOCKED)
        self.assertIn("reachability unknown", rows[0]["detail"],
                      "the row must say WHICH non-answer blocked the pass")

    def test_a_cap_reached_skip_is_not_recorded_as_blocked(self):
        """The cap arm is not evidence about the backend, and re-recording it
        every pass would grow the trail forever while saying nothing new."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            for _ in range(deferral_retry.MAX_ATTEMPTS):
                deferral_retry.record_attempt(
                    folder,
                    deferral_retry.RetryResult(CID, deferral_retry.STARTED, "x"),
                )
            with mock.patch.object(deferral_retry, "trail"):
                _passes(folder, 3, backend=False)
            history = deferral_retry.retry_history(folder, CID)
        self.assertEqual(history.blocked, 0)
        self.assertTrue(history.cap_reached)

    def test_an_unwritable_trail_never_breaks_a_dispatch(self):
        """Observability never gates work — the pre-existing contract, now
        exercised on the new row type too."""
        deferral_retry.record_attempt(
            Path("/proc/definitely/not/writable"),
            deferral_retry.RetryResult(CID, deferral_retry.BLOCKED, "x"),
        )
        with mock.patch.object(deferral_retry, "trail"):
            results = deferral_retry.dispatch(
                Path("/proc/definitely/not/writable"), condition_ids=[CID],
                backend_probe=lambda f, k: False, runner=_runner(0),
                single_instance=False,
            )
        self.assertEqual([r.status for r in results], [deferral_retry.SKIPPED])


class RetryHistoryTests(unittest.TestCase):
    def _trail(self, folder: Path, rows):
        path = deferral_retry.attempts_path(folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                json.dumps({"ts": ts, "condition_id": cid, "status": st,
                            "detail": ""}) + "\n"
                for ts, cid, st in rows
            ),
            encoding="utf-8",
        )

    def test_an_absent_trail_is_an_empty_history_not_an_error(self):
        with TemporaryDirectory() as td:
            h = deferral_retry.retry_history(Path(td), CID)
        self.assertEqual((h.attempts, h.blocked, h.blocked_streak), (0, 0, 0))
        self.assertEqual(deferral_retry.retry_disposition_note(Path("/nope"), CID), "")

    def test_the_streak_counts_only_TRAILING_consecutive_blocks(self):
        """"down each time" is only true of an unbroken run; a run that was
        interrupted by an attempt that DID happen must not be described that
        way."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            self._trail(folder, [
                ("t1", CID, deferral_retry.BLOCKED),
                ("t2", CID, deferral_retry.BLOCKED),
                ("t3", CID, deferral_retry.STARTED),
                ("t4", CID, deferral_retry.INCONCLUSIVE),
                ("t5", CID, deferral_retry.BLOCKED),
                ("t6", CID, deferral_retry.BLOCKED),
            ])
            h = deferral_retry.retry_history(folder, CID)
        self.assertEqual(h.blocked, 4)
        self.assertEqual(h.blocked_streak, 2)
        self.assertEqual(h.streak_first_ts, "t5")
        self.assertEqual(h.streak_last_ts, "t6")

    def test_other_conditions_do_not_contaminate_the_history(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            self._trail(folder, [
                ("t1", KG_CID, deferral_retry.BLOCKED),
                ("t2", KG_CID, deferral_retry.BLOCKED),
                ("t3", CID, deferral_retry.BLOCKED),
            ])
            self.assertEqual(
                deferral_retry.retry_history(folder, CID).blocked_streak, 1)
            self.assertEqual(
                deferral_retry.retry_history(folder, KG_CID).blocked_streak, 2)

    def test_corrupt_lines_are_ignored(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            path = deferral_retry.attempts_path(folder)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "not json\n"
                + json.dumps({"condition_id": CID, "status": "blocked"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(deferral_retry.retry_history(folder, CID).blocked, 1)


class DispositionNoteTests(unittest.TestCase):
    def _trail_blocked(self, folder: Path, n: int):
        path = deferral_retry.attempts_path(folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                json.dumps({"ts": f"2026-08-{i + 1:02d}T00:00:00Z",
                            "condition_id": CID,
                            "status": deferral_retry.BLOCKED,
                            "detail": "no code embedding backend reachable"}) + "\n"
                for i in range(n)
            ),
            encoding="utf-8",
        )

    def test_a_short_run_keeps_the_generic_disposition(self):
        """Below the threshold the condition is plausibly transient and
        "VCO retries this itself" is still the most accurate thing to say. An
        empty note is a real answer."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            self._trail_blocked(folder, deferral_retry.BLOCKED_NOTE_THRESHOLD - 1)
            self.assertEqual(deferral_retry.retry_disposition_note(folder, CID), "")

    def test_a_sustained_run_says_what_actually_happened(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            self._trail_blocked(folder, 12)
            note = deferral_retry.retry_disposition_note(folder, CID)
        self.assertIn("12", note)
        self.assertIn("unreachable every time", note)
        self.assertIn("2026-08-01", note, "the note dates the run it describes")
        self.assertIn("nothing changes here until the service is back", note)

    def test_a_spent_cap_says_VCO_has_STOPPED(self):
        """The strongest correction: past the cap the dispatcher would SKIP
        the condition, so "VCO retries this itself" describes something that
        will provably not happen again."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            for _ in range(deferral_retry.MAX_ATTEMPTS):
                deferral_retry.record_attempt(
                    folder,
                    deferral_retry.RetryResult(CID, deferral_retry.STARTED, "x"))
            note = deferral_retry.retry_disposition_note(folder, CID)
        self.assertIn("STOPPED", note)
        self.assertIn("manual work", note)

    def test_the_note_never_promises_something_untrue(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            self._trail_blocked(folder, 9)
            note = deferral_retry.retry_disposition_note(folder, CID)
        self.assertNotIn("VCO retries this itself", note)
        self.assertIn("will retry by itself the moment that backend answers", note)

    def test_the_reader_never_writes_to_the_ledger(self):
        """It must not: `vco_lib.codegraph_resync` owns this entry and
        re-emits it on every deferred run (`add_entry` is last-write-wins), so
        an explicit `disposition` written from here would be overwritten by
        the next update and revert in silence. Forking another component's
        lifecycle to correct its text is not a fix."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            self._trail_blocked(folder, 5)
            ctx_dir = folder / ".claude" / "context"
            ctx_dir.mkdir(parents=True)
            before = sorted(p.name for p in ctx_dir.iterdir())
            deferral_retry.retry_disposition_note(folder, CID)
            self.assertEqual(before, sorted(p.name for p in ctx_dir.iterdir()))

    def test_the_registry_row_still_classes_the_condition_auto_retryable(self):
        """The tier is CORRECT — the retry is wired and does run when the
        backend is up. What was missing was the ability to say that it has not
        been able to. This pins that the fix did not silently re-tier the row
        (which would stop the retry from ever being dispatched)."""
        toml = (REPO_ROOT / "vco_lib" / "deferral_conditions.toml").read_text(
            encoding="utf-8")
        block = toml[toml.index(f"[conditions.{CID}]"):]
        block = block[: block.index("[conditions.", 10)]
        self.assertIn('class = "auto_retryable"', block)
        self.assertIn('retry_action = "retry:py:codegraph_resync"', block)


class DoctorRendersTheNoteTests(unittest.TestCase):
    """The LIVE consumer. A reader with no renderer is the same defect."""

    class _Entry:
        def __init__(self, cid):
            self.condition_id = cid
            self.title = cid
            self.detected = ""
            self.command_to_apply = ""
            self.severity = "warning"

    class _Report:
        def __init__(self, cids):
            self.entries = [DoctorRendersTheNoteTests._Entry(c) for c in cids]

    def _findings(self, folder):
        res = doctor.DoctorResolvers(
            deferral_report=lambda f: self._Report([CID]),
            npx_probe=lambda names: {"npx_present": True, "npx_path": "/b/npx",
                                     "npm_present": True, "commands": {}},
            mcp_entries=lambda: {},
            pin_rows=lambda: [],
        )
        return doctor.probe_deferral_ledger(folder, res, {})

    def test_a_blocked_condition_is_annotated_in_the_owed_work_finding(self):
        with TemporaryDirectory() as td:
            folder = Path(td)
            path = deferral_retry.attempts_path(folder)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "".join(
                    json.dumps({"ts": f"2026-08-0{i + 1}T00:00:00Z",
                                "condition_id": CID,
                                "status": deferral_retry.BLOCKED,
                                "detail": "no code embedding backend reachable"})
                    + "\n" for i in range(6)
                ),
                encoding="utf-8",
            )
            findings = self._findings(folder)
        owed = next(f for f in findings if f.probe == "owed_retryable_work")
        self.assertIn("VCO can retry itself", owed.summary)
        self.assertIn("unreachable every time", owed.summary,
                      "the tier and the history must appear together")
        self.assertIn(CID, owed.detail["retry_notes"])

    def test_an_untried_condition_carries_no_note(self):
        with TemporaryDirectory() as td:
            findings = self._findings(Path(td))
        owed = next(f for f in findings if f.probe == "owed_retryable_work")
        self.assertEqual(owed.detail["retry_notes"], {})
        self.assertNotIn("unreachable", owed.summary)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
