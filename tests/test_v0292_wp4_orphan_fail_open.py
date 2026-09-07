# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 W18 — the orphan detector must not FAIL OPEN when Weaviate is down.

THE BUG (verified in source, not hypothesised)
----------------------------------------------
``_detect_orphan_code_collections`` consumed the live class list as an
EXCLUSION set: a segment dir matching a live class was skipped, and a dir whose
prefix had any live class was skipped. ``_list_classes`` returned ``[]`` on
transport failure, so with Weaviate merely DOWN the exclusion set was EMPTY,
nothing was excluded, and ``ondisk_orphans`` got **WIDER** — while the emitted
command is a filesystem-level ``rm`` the user pastes.

It then compounded. ``_emit_orphan_code_collections_deferral`` persisted that
empty set as the DETECT-TIME snapshot; the later reclaim (which runs with
Weaviate down and cannot re-fetch) read it back as "nothing was live" and its
own re-check PASSED. The safety net removed itself.
``cross_project_keep_resolvable`` did not help: the keep-set comes from
launcher.db, so Weaviate-down + launcher.db-up sailed straight through.

WHAT IS ASSERTED HERE
---------------------
Every destructive decision gets BOTH branches — the ACT case (the feature still
works when the evidence is there) and the LEAVE-ALONE case (nothing happens
when it is not) — because a fix that only proves the refusal has not shown the
detector still detects.

* the detector: schema readable → planted orphan surfaced (act);
  schema unreadable → zero orphans, ``live_schema_resolvable`` False, and NO
  snapshot file written, proven with a WRITE SPY rather than by inspection.
* the emitter: refuses an unreadable-schema detection outright.
* the run-time re-probe: refuses (exit 2) instead of answering "0 orphans".
* the staging sweep: skips and says why, dropping nothing either way.
* the ALREADY-DAMAGED install: a snapshot already written during an outage
  carries no ``live_schema_resolvable`` marker, so the reclaim refuses it.

No live Weaviate and no ambient launcher.db: every probe is mocked at
``project_init._http_request`` or injected.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402
from vco_lib import weaviate_helpers as wh  # noqa: E402

URL = "http://localhost:8081"


def _schema_ok(*class_names: str):
    """``_http_request`` mock: /v1/schema answers 200 with ``class_names``."""
    def _side_effect(method, url, *, body=None, timeout=30.0):
        if method == "GET" and url.endswith("/v1/schema"):
            payload = {"classes": [{"class": c} for c in class_names]}
            return (200, json.dumps(payload).encode())
        if method == "POST" and url.endswith("/v1/graphql"):
            return (200, json.dumps({"data": {"Aggregate": {}}}).encode())
        return (404, b"")
    return _side_effect


def _schema_down(exc=None):
    """``_http_request`` mock: the transport fails (Weaviate is not running)."""
    def _side_effect(method, url, *, body=None, timeout=30.0):
        raise exc or urllib.error.URLError("connection refused")
    return _side_effect


def _schema_http_500():
    def _side_effect(method, url, *, body=None, timeout=30.0):
        return (500, b"upstream boom")
    return _side_effect


def _no_collection_env():
    """Hermeticity: `migrate_collections` reads KG/DEV/DIAGRAMS_COLLECTION from
    the AMBIENT environment for its per-collection recovery sweep. This machine
    exports them, so without this the test would exercise the developer's own
    collection names (and behave differently in CI)."""
    import os
    return mock.patch.dict(
        os.environ,
        {"KG_COLLECTION": "", "DEVELOPMENT_COLLECTION": "",
         "DIAGRAMS_COLLECTION": ""},
    )


# ═══════════════════════════════════════════════════════════════════════════
# `_list_classes` — the tri-state itself
# ═══════════════════════════════════════════════════════════════════════════

class ListClassesTriStateTests(unittest.TestCase):

    def test_readable_schema_returns_the_names(self):
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_schema_ok("A_CodeModule", "B")):
            self.assertEqual(project_init._list_classes(URL),
                             ["A_CodeModule", "B"])

    def test_empty_server_is_a_real_answer(self):
        """A READ schema with no classes still returns ``[]`` — the tri-state
        must not turn a genuinely empty server into a refusal."""
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_schema_ok()):
            self.assertEqual(project_init._list_classes(URL), [])

    def test_transport_failure_raises_instead_of_returning_empty(self):
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_schema_down()):
            with self.assertRaises(wh.ProbeUnavailable):
                project_init._list_classes(URL)

    def test_non_200_raises(self):
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_schema_http_500()):
            with self.assertRaises(wh.ProbeUnavailable):
                project_init._list_classes(URL)

    def test_the_raise_names_the_question_and_the_reason(self):
        """The message a user eventually sees must say what could not be
        determined and why — the whole point of not returning ``[]``."""
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_schema_down()):
            with self.assertRaises(wh.ProbeUnavailable) as ctx:
                project_init._list_classes(URL)
        self.assertIn("which classes exist", str(ctx.exception))
        self.assertIn("NOT evidence of absence", str(ctx.exception))


# ═══════════════════════════════════════════════════════════════════════════
# THE FIX — detector both branches
# ═══════════════════════════════════════════════════════════════════════════

class OrphanDetectorFailOpenTests(unittest.TestCase):
    """The one that matters: a transient outage must not WIDEN the reclaim."""

    ONDISK = [
        ("live_codefunction", 4_000_000),   # a LIVE project's segment dir
        ("dead_codeclass", 7_000_000),      # genuinely stranded
    ]

    def _detect(self, http_side_effect, **kw):
        with mock.patch.object(project_init, "_http_request",
                               side_effect=http_side_effect):
            return project_init._detect_orphan_code_collections(
                URL, volume_dir="/vol",
                keep_set=set(), keep_resolvable=True,
                ondisk_lister=lambda vd: list(self.ONDISK),
                **kw,
            )

    # ── ACT: the evidence is there, the feature still works ───────────────

    def test_schema_readable_surfaces_the_planted_orphan(self):
        det = self._detect(_schema_ok("Live_CodeFunction"))
        self.assertTrue(det["live_schema_resolvable"])
        dirs = {o["dir"] for o in det["ondisk_orphans"]}
        self.assertEqual(dirs, {"dead_codeclass"},
                         "the genuinely stranded dir must still be found")
        self.assertEqual(det["total_reclaim_bytes"], 7_000_000)

    def test_schema_readable_excludes_the_live_projects_dir(self):
        """The exclusion guard the outage was disabling: `live_codefunction`
        matches a live class, so it is NOT an orphan."""
        det = self._detect(_schema_ok("Live_CodeFunction"))
        self.assertNotIn("live_codefunction",
                         {o["dir"] for o in det["ondisk_orphans"]})

    def test_schema_readable_excludes_a_sibling_suffix_of_a_live_prefix(self):
        """A dir whose prefix has ANY live class is active even without a
        binding row — the SEV-3 #1 degenerate. Still honoured."""
        det = self._detect(_schema_ok("Live_CodeModule"))  # note: not Function
        self.assertNotIn("live_codefunction",
                         {o["dir"] for o in det["ondisk_orphans"]})
        self.assertEqual(det["live_prefixes_normalised"], ["live"])

    # ── LEAVE ALONE: the evidence is missing, nothing is claimed ──────────

    def test_schema_unreadable_flags_nothing(self):
        det = self._detect(_schema_down())
        self.assertFalse(det["live_schema_resolvable"])
        self.assertEqual(det["live_orphans"], [])
        self.assertEqual(det["ondisk_orphans"], [],
                         "an outage must NEVER widen the reclaim list")
        self.assertEqual(det["total_reclaim_bytes"], 0)

    def test_schema_unreadable_records_why(self):
        det = self._detect(_schema_down())
        self.assertTrue(det["live_schema_unresolvable_reason"],
                        "the run must be able to say it could not check")

    def test_schema_http_500_flags_nothing(self):
        det = self._detect(_schema_http_500())
        self.assertFalse(det["live_schema_resolvable"])
        self.assertEqual(det["ondisk_orphans"], [])

    def test_schema_unreadable_persists_no_live_prefix_snapshot_value(self):
        """The compounding half: the empty set must never be published as a
        detect-time fact."""
        det = self._detect(_schema_down())
        self.assertEqual(det["live_prefixes_normalised"], [])
        self.assertFalse(det["live_schema_resolvable"],
                         "an empty snapshot is only meaningful next to the "
                         "flag saying the schema WAS read")

    def test_a_raising_schema_fetcher_is_treated_as_unreadable(self):
        """The injection seam must not be a way around the guard."""
        def boom():
            raise RuntimeError("injected")
        det = project_init._detect_orphan_code_collections(
            URL, volume_dir="/vol", keep_set=set(), keep_resolvable=True,
            schema_fetcher=boom,
            ondisk_lister=lambda vd: list(self.ONDISK),
        )
        self.assertFalse(det["live_schema_resolvable"])
        self.assertEqual(det["ondisk_orphans"], [])

    # ── the OTHER precondition still behaves as before ────────────────────

    def test_unresolvable_keepset_returns_before_the_schema_is_asked_for(self):
        """The two guards are independent; neither may mask the other, and
        neither may CLAIM the other's evidence. With the keep-set unresolvable
        the schema is never read, so the flag is None ("not attempted") — not
        True (which would assert a read that never happened) and not False
        (which would report a failure that never occurred)."""
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_schema_ok("Live_CodeFunction")):
            det = project_init._detect_orphan_code_collections(
                URL, volume_dir="/vol", keep_set=set(), keep_resolvable=False,
                ondisk_lister=lambda vd: list(self.ONDISK),
            )
        self.assertFalse(det["keep_resolvable"])
        self.assertIsNone(det["live_schema_resolvable"])
        self.assertEqual(det["ondisk_orphans"], [])

    def test_detection_always_carries_the_flag(self):
        """`_emit_orphan_code_collections_deferral` refuses only on an explicit
        False, so the producer must never omit the key."""
        for side_effect in (_schema_ok("X_CodeModule"), _schema_down()):
            with self.subTest(side_effect=side_effect):
                det = self._detect(side_effect)
                self.assertIn("live_schema_resolvable", det)


# ═══════════════════════════════════════════════════════════════════════════
# THE EMITTER — no entry, and specifically NO SNAPSHOT, proven with a spy
# ═══════════════════════════════════════════════════════════════════════════

class EmitterRefusesUnreadableSchemaTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _snapshot(self) -> Path:
        return project_init._orphan_live_prefix_snapshot_path(self.folder)

    def _deferral(self) -> Path:
        return self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md"

    ORPHANS = {
        "live_orphans": [],
        "ondisk_orphans": [{"dir": "zombie_codefunction",
                            "size_bytes": 5_000_000,
                            "prefix_normalised": "zombie"}],
        "total_reclaim_bytes": 5_000_000,
        "volume_dir": "/vol",
        "live_prefixes_normalised": [],
    }

    # ── ACT ───────────────────────────────────────────────────────────────

    def test_readable_schema_emits_and_writes_the_snapshot(self):
        det = dict(self.ORPHANS, live_schema_resolvable=True,
                   live_prefixes_normalised=["live"])
        self.assertTrue(
            project_init._emit_orphan_code_collections_deferral(
                self.folder, URL, det))
        self.assertTrue(self._snapshot().is_file())
        self.assertIn("orphan_code_collections_detected",
                      self._deferral().read_text(encoding="utf-8"))

    # ── LEAVE ALONE, with a WRITE SPY (not an inspection) ─────────────────

    def test_unreadable_schema_emits_nothing_and_writes_no_snapshot(self):
        det = dict(self.ORPHANS, live_schema_resolvable=False)
        spy = mock.Mock(wraps=project_init._write_orphan_live_prefix_snapshot)
        with mock.patch.object(project_init,
                               "_write_orphan_live_prefix_snapshot", spy):
            emitted = project_init._emit_orphan_code_collections_deferral(
                self.folder, URL, det)
        self.assertFalse(emitted)
        spy.assert_not_called()
        self.assertFalse(self._snapshot().exists(),
                         "no snapshot file may exist on disk either")
        self.assertFalse(self._deferral().exists())

    def test_no_orphans_still_writes_no_snapshot(self):
        det = {"live_orphans": [], "ondisk_orphans": [],
               "live_schema_resolvable": True,
               "live_prefixes_normalised": ["live"]}
        spy = mock.Mock(wraps=project_init._write_orphan_live_prefix_snapshot)
        with mock.patch.object(project_init,
                               "_write_orphan_live_prefix_snapshot", spy):
            self.assertFalse(
                project_init._emit_orphan_code_collections_deferral(
                    self.folder, URL, det))
        spy.assert_not_called()

    def test_the_written_snapshot_records_that_the_schema_was_read(self):
        det = dict(self.ORPHANS, live_schema_resolvable=True,
                   live_prefixes_normalised=["live"])
        project_init._emit_orphan_code_collections_deferral(
            self.folder, URL, det)
        payload = json.loads(self._snapshot().read_text(encoding="utf-8"))
        self.assertIs(payload.get("live_schema_resolvable"), True)


# ═══════════════════════════════════════════════════════════════════════════
# END-TO-END through the DETECT CLI — the shipped path, not a unit
# ═══════════════════════════════════════════════════════════════════════════

class DetectCommandTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, http_side_effect, ondisk):
        args = argparse.Namespace(
            weaviate_url=URL, volume_dir="/vol",
            project_folder=str(self.folder), json=True,
        )
        buf = []
        with mock.patch.object(project_init, "_http_request",
                               side_effect=http_side_effect), \
             mock.patch.object(project_init, "_codegraph_keep_set_normalised",
                               return_value=(set(), True)), \
             mock.patch.object(project_init, "_list_ondisk_weaviate_dirs",
                               return_value=ondisk), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: buf.append(a)):
            rc = project_init._cmd_detect_orphan_code_collections(args)
        payload = json.loads(buf[0][0]) if buf else {}
        return rc, payload

    def test_act_schema_up_reports_the_orphan_and_emits(self):
        rc, out = self._run(_schema_ok("Live_CodeFunction"),
                            [("dead_codeclass", 3000)])
        self.assertEqual(rc, 0)
        self.assertTrue(out["live_schema_resolvable"])
        self.assertEqual([o["dir"] for o in out["ondisk_orphans"]],
                         ["dead_codeclass"])
        self.assertTrue(out["deferral_emitted"])

    def test_leave_alone_schema_down_reports_nothing_and_emits_nothing(self):
        rc, out = self._run(_schema_down(),
                            [("dead_codeclass", 3000),
                             ("live_codefunction", 9_000_000)])
        self.assertEqual(rc, 0)
        self.assertFalse(out["live_schema_resolvable"])
        self.assertEqual(out["ondisk_orphans"], [])
        self.assertFalse(out["deferral_emitted"])
        self.assertFalse(
            project_init._orphan_live_prefix_snapshot_path(self.folder).exists())
        self.assertFalse(
            (self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md").exists())


# ═══════════════════════════════════════════════════════════════════════════
# THE CONSENTED DROP — refuses instead of answering "0 orphans, done"
# ═══════════════════════════════════════════════════════════════════════════

class DropCommandRefusalTests(unittest.TestCase):

    def _run(self, http_side_effect, keep, json_out=True):
        args = argparse.Namespace(weaviate_url=URL, confirm=True, json=json_out)
        printed = []
        with mock.patch.object(project_init, "_http_request",
                               side_effect=http_side_effect), \
             mock.patch.object(project_init, "_codegraph_keep_set_normalised",
                               return_value=keep), \
             mock.patch.object(project_init, "_delete_class") as delete, \
             mock.patch("builtins.print",
                        side_effect=lambda *a, **k: printed.append(str(a[0]) if a else "")):
            rc = project_init._cmd_drop_orphan_code_collections(args)
        return rc, delete, "\n".join(printed)

    def test_act_drops_a_revalidated_orphan(self):
        rc, delete, _out = self._run(_schema_ok("Zombie_CodeFunction"),
                                     ({"live"}, True))
        self.assertEqual(rc, 0)
        delete.assert_called_once()
        self.assertEqual(delete.call_args[0][0], "Zombie_CodeFunction")

    def test_leave_alone_schema_unreadable_refuses_and_drops_nothing(self):
        rc, delete, out = self._run(_schema_down(), ({"live"}, True))
        self.assertEqual(rc, 2, "could-not-check must REFUSE, not exit 0")
        delete.assert_not_called()
        self.assertIn("refusing", out.lower())
        self.assertIn("live Weaviate schema", out)

    def test_leave_alone_keepset_unreadable_refuses_and_drops_nothing(self):
        rc, delete, out = self._run(_schema_ok("Zombie_CodeFunction"),
                                    (set(), False))
        self.assertEqual(rc, 2)
        delete.assert_not_called()
        self.assertIn("launcher.db", out)

    def test_a_clean_run_no_longer_claims_maybe_already_reclaimed(self):
        """The old message hedged ("keep-set unresolvable, OR all reclaimed
        already") because it could not tell. Both causes now refuse above, so
        the remaining message must be unambiguous."""
        rc, delete, out = self._run(_schema_ok("Live_CodeFunction"),
                                    ({"live"}, True), json_out=False)
        self.assertEqual(rc, 0)
        delete.assert_not_called()
        self.assertNotIn("unresolvable", out)
        self.assertIn("no orphan classes to drop", out)


# ═══════════════════════════════════════════════════════════════════════════
# ALREADY-DAMAGED — a snapshot written during an outage by the OLD code
# ═══════════════════════════════════════════════════════════════════════════

class AlreadyDamagedSnapshotTests(unittest.TestCase):
    """The mechanism that reaches a user whose install is ALREADY broken.

    Fixing the detector stops new poisoned snapshots. It does nothing for the
    file already on disk — and that file is what the reclaim consults. So the
    reader refuses any snapshot that cannot prove the schema was read, which is
    exactly the shape a pre-v0.2.92 outage-time snapshot has.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vol = self.root / "vol"
        self.vol.mkdir()
        self.proj = self.root / "proj"
        self.proj.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_legacy_snapshot(self, prefixes):
        """Exactly what the pre-v0.2.92 writer produced: no marker field."""
        p = project_init._orphan_live_prefix_snapshot_path(self.proj)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "schema": project_init._ORPHAN_LIVE_PREFIX_SNAPSHOT_SCHEMA,
            "live_prefixes_normalised": prefixes,
            "recorded_at": "2026-08-01T00:00:00Z",
        }, indent=2) + "\n", encoding="utf-8")
        return p

    def _reclaim(self):
        args = argparse.Namespace(
            confirm=True, i_understand_filesystem_level=True,
            volume_dir=str(self.vol), weaviate_url=URL,
            project_folder=str(self.proj), json=True,
        )
        printed = []
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_schema_down()), \
             mock.patch.object(project_init, "_codegraph_keep_set_normalised",
                               return_value=(set(), True)), \
             mock.patch("builtins.print",
                        side_effect=lambda *a, **k: printed.append(str(a[0]) if a else "")):
            rc = project_init._cmd_reclaim_stranded_code_segments(args)
        return rc, "\n".join(printed)

    def test_leave_alone_unmarked_legacy_snapshot_is_refused(self):
        (self.vol / "live_codefunction").mkdir()
        self._write_legacy_snapshot([])     # the outage-time empty set
        rc, out = self._reclaim()
        self.assertEqual(rc, 0)
        self.assertTrue((self.vol / "live_codefunction").exists(),
                        "a dir must NOT be removed on the strength of a "
                        "snapshot that cannot prove the schema was read")
        self.assertIn("before v0.2.92", out + json.dumps(out))

    def test_act_a_marked_snapshot_still_reclaims(self):
        """The feature is not neutered: a snapshot from a healthy detect run
        still authorises the reclaim."""
        (self.vol / "dead_codefunction").mkdir()
        project_init._write_orphan_live_prefix_snapshot(self.proj, [])
        rc, _out = self._reclaim()
        self.assertEqual(rc, 0)
        self.assertFalse((self.vol / "dead_codefunction").exists())

    def test_act_a_marked_snapshot_still_protects_a_live_prefix(self):
        (self.vol / "foo_codefunction").mkdir()
        (self.vol / "dead_codefunction").mkdir()
        project_init._write_orphan_live_prefix_snapshot(self.proj, ["foo"])
        rc, _out = self._reclaim()
        self.assertEqual(rc, 0)
        self.assertTrue((self.vol / "foo_codefunction").exists())
        self.assertFalse((self.vol / "dead_codefunction").exists())

    def test_reader_is_tri_state(self):
        missing = project_init._read_orphan_live_prefix_snapshot(self.proj)
        self.assertTrue(missing.is_unknown())

        self._write_legacy_snapshot(["foo"])
        legacy = project_init._read_orphan_live_prefix_snapshot(self.proj)
        self.assertTrue(legacy.is_unknown(),
                        "unmarked == cannot be trusted, even when non-empty")

        project_init._write_orphan_live_prefix_snapshot(self.proj, [])
        empty = project_init._read_orphan_live_prefix_snapshot(self.proj)
        self.assertTrue(empty.is_absent(), "a MARKED empty set is a real answer")
        self.assertEqual(empty.require(), set())

        project_init._write_orphan_live_prefix_snapshot(self.proj, ["foo"])
        full = project_init._read_orphan_live_prefix_snapshot(self.proj)
        self.assertTrue(full.is_present())
        self.assertEqual(full.require(), {"foo"})

    def test_malformed_snapshot_is_unknown_not_empty(self):
        p = project_init._orphan_live_prefix_snapshot_path(self.proj)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        self.assertTrue(
            project_init._read_orphan_live_prefix_snapshot(self.proj).is_unknown())


# ═══════════════════════════════════════════════════════════════════════════
# CALL SITE 1 — the migrate-time orphan-staging sweep
# ═══════════════════════════════════════════════════════════════════════════

class StagingSweepUnreadableSchemaTests(unittest.TestCase):
    """The sweep was already fail-CLOSED (an empty list surfaces nothing and
    drops nothing). What changes is that it now SAYS the schema was unreadable
    instead of implying the server was clean — and, critically, it still
    performs no drop."""

    def test_sweep_drops_nothing_and_logs_the_real_cause(self):
        events = []

        def _log_event(step, level, msg, data=None):
            events.append((step, level, msg, data or {}))

        args = argparse.Namespace(name="Proj", yes=True, dry_run=True)
        with _no_collection_env(), \
             mock.patch.object(project_init, "_list_classes",
                               side_effect=wh.ProbeUnavailable(
                                   "which classes exist in Weaviate",
                                   "GET /v1/schema failed")), \
             mock.patch.object(project_init, "_recover_or_drop_orphan_staging") as rec, \
             mock.patch.object(project_init, "_build_plan", return_value=[]), \
             mock.patch("builtins.print"):
            project_init.migrate_collections(
                args, weaviate_url=URL, log_event=_log_event, dry_run=True,
            )
        rec.assert_not_called()
        msgs = " | ".join(e[2] for e in events)
        self.assertIn("could not read the live schema", msgs)
        self.assertIn("SKIPPED", msgs)

    def test_sweep_still_works_when_the_schema_reads(self):
        args = argparse.Namespace(name="Proj", yes=True, dry_run=True)
        errors = []
        with _no_collection_env(), mock.patch.object(
            project_init, "_list_classes",
            return_value=["Zzz_KnowledgeGraph__staging", "Zzz_KnowledgeGraph"],
        ), mock.patch.object(project_init, "_build_plan", return_value=[]), \
                mock.patch("builtins.print"):
            out = project_init.migrate_collections(
                args, weaviate_url=URL, dry_run=True,
            )
        errors = [e for e in out.get("errors", [])
                  if "orphan staging class" in str(e.get("error", ""))]
        self.assertTrue(errors, "the sweep must still surface a real orphan")


# ═══════════════════════════════════════════════════════════════════════════
# TRI-OS — the shape of every OS-dependent decision in this change
# ═══════════════════════════════════════════════════════════════════════════

class TriOsShapeTests(unittest.TestCase):
    """Windows / macOS / Linux.

    The changed code contains NO ``sys.platform`` / ``os.name`` branch — every
    decision is over HTTP probe states, sqlite outcomes and set membership. The
    two things that DO vary by OS are asserted here: path composition (pathlib
    parts, never an embedded separator, so a Windows install gets the same file)
    and case-insensitive matching (macOS and Windows filesystems fold case;
    Linux does not, and Weaviate lowercases its own dir names on all three).
    """

    def test_snapshot_path_is_composed_from_parts_not_a_separator(self):
        with tempfile.TemporaryDirectory() as td:
            p = project_init._orphan_live_prefix_snapshot_path(Path(td))
        self.assertEqual(
            p.parts[-3:],
            (".claude", "state", "codegraph-orphan-live-prefixes.json"),
            "a hardcoded '.claude/state' would survive on POSIX and produce a "
            "single literal-slash filename on Windows",
        )

    def test_dir_to_live_class_match_is_case_insensitive_both_directions(self):
        """Weaviate lowercases on-disk dir names; the class keeps its case."""
        for dirname, live in (("live_codefunction", "Live_CodeFunction"),
                              ("LIVE_CODEFUNCTION", "live_codefunction"),
                              ("Live_CodeFunction", "LIVE_CODEFUNCTION")):
            with self.subTest(dirname=dirname, live=live):
                det = project_init._detect_orphan_code_collections(
                    URL, volume_dir="/vol", keep_set=set(), keep_resolvable=True,
                    schema_fetcher=lambda live=live: [live],
                    ondisk_lister=lambda vd, d=dirname: [(d, 1000)],
                )
                self.assertEqual(det["ondisk_orphans"], [])

    def test_no_platform_branch_was_introduced_in_the_changed_functions(self):
        import inspect
        for fn in (project_init._detect_orphan_code_collections,
                   project_init._read_orphan_live_prefix_snapshot,
                   project_init._write_orphan_live_prefix_snapshot,
                   project_init._revalidated_orphan_live_classes,
                   project_init._list_classes):
            with self.subTest(fn=fn.__name__):
                src = inspect.getsource(fn)
                self.assertNotIn("sys.platform", src)
                self.assertNotIn("os.name", src)

    def test_write_file_atomic_documents_the_mode_it_actually_ships(self):
        """R16: the docstring said 0o755 while every caller passes 0o700."""
        import inspect
        doc = project_init._write_file_atomic.__doc__ or ""
        self.assertIn("0o700", doc)
        src = inspect.getsource(project_init.install_project_bundle)
        self.assertIn("mode = 0o700", src)
        self.assertNotIn("mode = 0o755", src)
        # Windows: chmod is a documented no-op, and the write must not fail.
        self.assertIn("no-op on Windows",
                      inspect.getsource(project_init._write_file_atomic))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
