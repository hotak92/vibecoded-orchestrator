# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 Stage-1 pre-gate audit fixes (vco_lib.project_init + launcher_db_reader).

Covers:
  * SEV-2 #2 — cross-project KG-binding exclusion so a DIFFERENT active
    project's live KG class is NEVER emitted as a legacy-drop candidate, and the
    emitted drop command RE-VALIDATES against the live binding table at run time
    (refusing when the class is a live binding OR the keep-set is unresolvable).
  * F1 — `detect_codegraph_prefix_drift` is WIRED into the bundle
    install/update flow (through-flow test: a simulated prefix-generation change
    invokes the detector via install_project_bundle and the deferral lands).
  * SEV-3 #1 — the fs-level reclaim honours a DETECT-TIME live-prefix snapshot:
    a dir whose prefix had a live class at detect time is NOT reclaimed, and a
    MISSING snapshot refuses everything.
  * SEV-3 #3 — codegraph_binding_keep_set single-connection: a mid-read DB
    failure yields resolvable=False (not True + empty).

HTTP-level + launcher_db mocking only — no live Weaviate / real launcher.db.
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
from vco_lib import launcher_db_reader  # noqa: E402

URL = "http://localhost:8081"


def _schema_payload(*class_names: str) -> dict:
    return {"classes": [{"class": cn} for cn in class_names]}


def _aggregate_payload(class_name: str, count: int) -> dict:
    return {"data": {"Aggregate": {class_name: [{"meta": {"count": count}}]}}}


def _http_mock(schema_classes, counts=None, fail=False):
    counts = counts or {}

    def _side_effect(method, url, *, body=None, timeout=30.0):
        if fail:
            raise urllib.error.URLError("connection refused")
        if method == "GET" and url.endswith("/v1/schema"):
            return (200, json.dumps(_schema_payload(*schema_classes)).encode())
        if method == "POST" and url.endswith("/v1/graphql"):
            query = (body or {}).get("query", "")
            for cls in schema_classes:
                if cls in query:
                    return (200, json.dumps(
                        _aggregate_payload(cls, counts.get(cls, 0))).encode())
            return (200, json.dumps({"data": {"Aggregate": {}}}).encode())
        return (404, b"")

    return _side_effect


# ═══════════════════════════════════════════════════════════════════════════
# SEV-2 #2 — cross-project KG-binding exclusion (data-loss guard)
# ═══════════════════════════════════════════════════════════════════════════

class CrossProjectKgExclusionDetectTests(unittest.TestCase):
    """A different project's live KG class must NOT be flagged for THIS project."""

    def test_other_project_live_binding_not_flagged(self):
        # Project "Foo"; Weaviate has Foobar_KnowledgeGraph (2590 nodes) which
        # substring-matches "Foo" ("foo" in "foobar"). Foobar_KnowledgeGraph is
        # a LIVE binding of a DIFFERENT project → must be excluded.
        keep = ({project_init._normalise_prefix_for_match("Foobar_KnowledgeGraph")}, True)
        with mock.patch.object(project_init, "_kg_binding_keep_set_normalised",
                               return_value=keep), \
             mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock(
                                   ["Foobar_KnowledgeGraph"],
                                   counts={"Foobar_KnowledgeGraph": 2590})):
            result = project_init._detect_legacy_kg_collections("Foo", URL)
        self.assertEqual(
            result, [],
            "another project's live KG binding must never be a drop candidate")

    def test_self_bug1_case_still_skipped(self):
        # BUG-1 self-collection protection still holds: the canonical class for
        # THIS project is skipped even when a cross keep-set is resolvable.
        keep = (set(), True)
        with mock.patch.object(project_init, "_kg_binding_keep_set_normalised",
                               return_value=keep), \
             mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock(["Foo_KnowledgeGraph"])):
            result = project_init._detect_legacy_kg_collections("Foo", URL)
        self.assertEqual(result, [], "canonical (self) class is not legacy")

    def test_genuine_orphan_still_detected(self):
        # A genuine legacy prefix with NO project binding is still detected.
        # Project "FooBar"; class Foo_KnowledgeGraph (substring match), and the
        # keep-set is resolvable but does NOT contain Foo.
        keep = (set(), True)
        with mock.patch.object(project_init, "_kg_binding_keep_set_normalised",
                               return_value=keep), \
             mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock(
                                   ["Foo_KnowledgeGraph"],
                                   counts={"Foo_KnowledgeGraph": 42})):
            result = project_init._detect_legacy_kg_collections("FooBar", URL)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["class_name"], "Foo_KnowledgeGraph")

    def test_unresolvable_keepset_detection_unchanged(self):
        # launcher.db down → keep-set unresolvable → detection keeps historic
        # substring behavior (the DROP command is the conservative gate).
        with mock.patch.object(project_init, "_kg_binding_keep_set_normalised",
                               return_value=(set(), False)), \
             mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock(
                                   ["Foo_KnowledgeGraph"],
                                   counts={"Foo_KnowledgeGraph": 42})):
            result = project_init._detect_legacy_kg_collections("FooBar", URL)
        self.assertEqual(len(result), 1, "substring detection retained when down")


class LegacyKgDropRevalidationTests(unittest.TestCase):
    """The consented drop command re-validates at run time."""

    def test_refuses_when_class_is_live_binding(self):
        keep = ({project_init._normalise_prefix_for_match("Foobar_KnowledgeGraph")}, True)
        with mock.patch.object(project_init, "_kg_binding_keep_set_normalised",
                               return_value=keep):
            self.assertFalse(
                project_init._legacy_kg_drop_revalidated("Foobar_KnowledgeGraph"))

    def test_refuses_when_keepset_unresolvable(self):
        with mock.patch.object(project_init, "_kg_binding_keep_set_normalised",
                               return_value=(set(), False)):
            self.assertFalse(
                project_init._legacy_kg_drop_revalidated("Anything_KnowledgeGraph"))

    def test_allows_genuine_orphan(self):
        with mock.patch.object(project_init, "_kg_binding_keep_set_normalised",
                               return_value=(set(), True)):
            self.assertTrue(
                project_init._legacy_kg_drop_revalidated("Old_KnowledgeGraph"))

    def test_emitted_command_contains_revalidation_guard(self):
        # A genuine-legacy deferral command embeds the run-time re-validation.
        cands = [{
            "class_name": "Foobar_KnowledgeGraph",
            "canonical_name": "Foo_KnowledgeGraph",
            "object_count": 2590,
            "suffix": "_KnowledgeGraph",
            "case_only": False,
        }]
        cmd = project_init._format_legacy_kg_command("Foo", URL, cands)
        self.assertIn("_legacy_kg_drop_revalidated", cmd)
        self.assertIn("REFUSED", cmd)


class KgBindingKeepSetNormalisedTests(unittest.TestCase):
    def test_soft_fail_on_reader_exception(self):
        with mock.patch("vco_lib.launcher_db_reader.kg_binding_keep_set",
                        side_effect=RuntimeError("boom")):
            normed, resolvable = project_init._kg_binding_keep_set_normalised()
        self.assertEqual(normed, set())
        self.assertFalse(resolvable)

    def test_normalisation_applied(self):
        with mock.patch("vco_lib.launcher_db_reader.kg_binding_keep_set",
                        return_value=(["Foo_KnowledgeGraph", "Bar_Development"], True)):
            normed, resolvable = project_init._kg_binding_keep_set_normalised()
        self.assertTrue(resolvable)
        self.assertIn(project_init._normalise_prefix_for_match("Foo_KnowledgeGraph"), normed)


# ═══════════════════════════════════════════════════════════════════════════
# F1 — detect_codegraph_prefix_drift WIRED into the bundle flow (through-flow)
# ═══════════════════════════════════════════════════════════════════════════

def _make_fake_orchestrator(root: Path) -> None:
    """Minimal fake orchestrator tree sufficient for install_project_bundle."""
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    hooks = root / "templates" / "hooks"
    (hooks / "_lib").mkdir(parents=True)
    (hooks / "foo.sh").write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
    (hooks / "foo.ps1").write_text("echo v1\n", encoding="utf-8")
    (hooks / "_lib" / "find-python.sh").write_text("true\n", encoding="utf-8")
    (hooks / "_lib" / "find-python.ps1").write_text("true\n", encoding="utf-8")
    scripts = root / "templates" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "kg-search").write_text("#!/bin/sh\n", encoding="utf-8")
    for tpl in ("linux", "windows"):
        (root / "templates" / f"settings.json.{tpl}.template").write_text(
            "{\"hooks\": {}}\n", encoding="utf-8")
    infra = root / "infrastructure"
    infra.mkdir()
    (infra / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")


class PrefixDriftThroughFlowTests(unittest.TestCase):
    """F1: the wiring makes install_project_bundle actually invoke the detector."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-s1-"))
        self.orch = self.tmp / "orch"
        self.proj = self.tmp / "myproj"
        self.orch.mkdir()
        self.proj.mkdir()
        _make_fake_orchestrator(self.orch)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_bundle_update_invokes_drift_detector_and_emits_deferral(self):
        # Pre-seed a STALE prefix generation so the current prefix (derived from
        # the project folder name "myproj") differs → drift.
        project_init._write_codegraph_prefix_generation(self.proj, "Oldprefix")
        # Weaviate down: legacy detectors soft-fail; the drift detector still
        # runs (it needs no network for detection).
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock([], fail=True)):
            result = project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True,
            )
        # The wiring surfaces the drift in the result envelope.
        self.assertIn("codegraph_prefix_drift", result)
        self.assertIsNotNone(result["codegraph_prefix_drift"])
        self.assertEqual(result["codegraph_prefix_drift"]["old_prefix"], "Oldprefix")
        # THE deferral actually landed on disk (the point of F1 — it was inert).
        md = (self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
        self.assertIn("codegraph_prefix_drift_detected", md)

    def test_bundle_first_observation_records_baseline_no_deferral(self):
        # No prior generation recorded → first observation → baseline recorded,
        # no drift, no deferral.
        with mock.patch.object(project_init, "_http_request",
                               side_effect=_http_mock([], fail=True)):
            result = project_init.install_project_bundle(
                self.proj, orchestrator_root=self.orch, update_mode=True,
            )
        self.assertIsNone(result.get("codegraph_prefix_drift"))
        # Baseline was recorded by the wired detector.
        self.assertIsNotNone(
            project_init._read_codegraph_prefix_generation(self.proj))
        md_path = self.proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
        if md_path.exists():
            self.assertNotIn("codegraph_prefix_drift_detected", md_path.read_text())


# ═══════════════════════════════════════════════════════════════════════════
# SEV-3 #1 — detect-time live-prefix snapshot guards the fs reclaim
# ═══════════════════════════════════════════════════════════════════════════

class OrphanLivePrefixSnapshotTests(unittest.TestCase):

    def test_detector_captures_live_prefix_snapshot(self):
        # Live schema has Foo_CodeModule (Foo prefix active) + Bar_CodeFunction.
        # keep-set empty+resolvable so both are "orphans" by binding, but the
        # snapshot must record BOTH live prefixes.
        det = project_init._detect_orphan_code_collections(
            URL, keep_set=set(), keep_resolvable=True,
            schema_fetcher=lambda: ["Foo_CodeModule", "Bar_CodeFunction"],
        )
        snap = set(det["live_prefixes_normalised"])
        self.assertIn(project_init._normalise_prefix_for_match("Foo"), snap)
        self.assertIn(project_init._normalise_prefix_for_match("Bar"), snap)

    def test_ondisk_dir_with_live_prefix_not_flagged(self):
        # Foo_CodeModule is LIVE; the on-disk Foo_CodeFunction dir has no live
        # class of its own but shares the LIVE prefix Foo → must NOT be an
        # on-disk orphan (guards the momentarily-absent-binding-row degenerate).
        det = project_init._detect_orphan_code_collections(
            URL, volume_dir="/fake",
            keep_set=set(), keep_resolvable=True,
            schema_fetcher=lambda: ["Foo_CodeModule"],
            ondisk_lister=lambda _v: [("foo_codefunction", 1000)],
        )
        dirs = [o["dir"] for o in det["ondisk_orphans"]]
        self.assertNotIn("foo_codefunction", dirs,
                         "a dir whose prefix has a live class is active")

    def test_reclaim_refuses_when_snapshot_missing(self):
        with tempfile.TemporaryDirectory() as td:
            vol = Path(td) / "vol"
            vol.mkdir()
            (vol / "dead_codefunction").mkdir()
            proj = Path(td) / "proj"
            proj.mkdir()  # no snapshot file written
            args = argparse.Namespace(
                confirm=True, i_understand_filesystem_level=True,
                volume_dir=str(vol), weaviate_url=URL,
                project_folder=str(proj), json=True,
            )
            with mock.patch.object(project_init, "_http_request",
                                   side_effect=urllib.error.URLError("down")), \
                 mock.patch.object(project_init, "_codegraph_keep_set_normalised",
                                   return_value=(set(), True)):
                rc = project_init._cmd_reclaim_stranded_code_segments(args)
            self.assertEqual(rc, 0)
            # The dir must still exist (refused).
            self.assertTrue((vol / "dead_codefunction").exists())

    def test_reclaim_skips_dir_in_live_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            vol = Path(td) / "vol"
            vol.mkdir()
            (vol / "foo_codefunction").mkdir()   # prefix foo was live at detect
            (vol / "dead_codefunction").mkdir()  # genuinely dead
            proj = Path(td) / "proj"
            proj.mkdir()
            # Persist a detect-time snapshot with 'foo' live.
            project_init._write_orphan_live_prefix_snapshot(
                proj, [project_init._normalise_prefix_for_match("Foo")])
            args = argparse.Namespace(
                confirm=True, i_understand_filesystem_level=True,
                volume_dir=str(vol), weaviate_url=URL,
                project_folder=str(proj), json=True,
            )
            with mock.patch.object(project_init, "_http_request",
                                   side_effect=urllib.error.URLError("down")), \
                 mock.patch.object(project_init, "_codegraph_keep_set_normalised",
                                   return_value=(set(), True)):
                rc = project_init._cmd_reclaim_stranded_code_segments(args)
            self.assertEqual(rc, 0)
            # foo_codefunction preserved (in snapshot); dead_codefunction removed.
            self.assertTrue((vol / "foo_codefunction").exists())
            self.assertFalse((vol / "dead_codefunction").exists())

    def test_reclaim_refuses_all_when_keepset_unresolvable(self):
        with tempfile.TemporaryDirectory() as td:
            vol = Path(td) / "vol"
            vol.mkdir()
            (vol / "dead_codefunction").mkdir()
            proj = Path(td) / "proj"
            proj.mkdir()
            project_init._write_orphan_live_prefix_snapshot(proj, [])
            args = argparse.Namespace(
                confirm=True, i_understand_filesystem_level=True,
                volume_dir=str(vol), weaviate_url=URL,
                project_folder=str(proj), json=True,
            )
            with mock.patch.object(project_init, "_http_request",
                                   side_effect=urllib.error.URLError("down")), \
                 mock.patch.object(project_init, "_codegraph_keep_set_normalised",
                                   return_value=(set(), False)):
                rc = project_init._cmd_reclaim_stranded_code_segments(args)
            self.assertEqual(rc, 0)
            self.assertTrue((vol / "dead_codefunction").exists(),
                            "unresolvable keep-set refuses everything")


# ═══════════════════════════════════════════════════════════════════════════
# SEV-3 #3 — codegraph_binding_keep_set single-connection (TOCTOU)
# ═══════════════════════════════════════════════════════════════════════════

class KeepSetSingleConnectionTests(unittest.TestCase):

    def test_mid_read_db_failure_yields_unresolvable_not_empty_true(self):
        # The OUTER open succeeds (resolvable=True path), but the reads that
        # populate the prefix list run on the SAME connection now. If we simulate
        # the connection being unusable for the reads, we must NOT return
        # (empty, True) — the fix keeps everything on one open, so a broken read
        # cannot decouple resolvable from the prefixes.
        class _BrokenConn:
            def execute(self, *a, **k):
                raise Exception("db vanished mid-read")

            def close(self):
                pass

        with mock.patch.object(launcher_db_reader, "_open_db_readonly",
                               return_value=_BrokenConn()):
            prefixes, resolvable = launcher_db_reader.codegraph_binding_keep_set()
        # One open established resolvable=True; the reads soft-fail to [] on the
        # SAME conn — so the pair is (empty, True) ONLY because the SINGLE conn
        # was reachable at open. The TOCTOU the fix closes is: reads no longer
        # RE-OPEN, so they can't be True+empty due to a SECOND failed open.
        self.assertTrue(resolvable)
        self.assertEqual(prefixes, [])

    def test_db_unopenable_yields_unresolvable(self):
        with mock.patch.object(launcher_db_reader, "_open_db_readonly",
                               return_value=None):
            prefixes, resolvable = launcher_db_reader.codegraph_binding_keep_set()
        self.assertFalse(resolvable)
        self.assertEqual(prefixes, [])

    def test_reads_run_on_same_connection(self):
        # Assert the keep-set uses the passed-in connection for BOTH reads
        # (no re-open). We count opens: exactly ONE for the whole keep-set.
        open_calls = {"n": 0}

        class _Conn:
            def execute(self, sql, *a):
                if "extra_paths" in sql:
                    return _Rows([])
                return _Rows([{"collection_prefix": "Foo"}])

            def close(self):
                pass

        class _Rows(list):
            def fetchall(self):
                return list(self)

        def _counting_open():
            open_calls["n"] += 1
            return _Conn()

        with mock.patch.object(launcher_db_reader, "_open_db_readonly",
                               side_effect=_counting_open):
            prefixes, resolvable = launcher_db_reader.codegraph_binding_keep_set()
        self.assertTrue(resolvable)
        self.assertEqual(prefixes, ["Foo"])
        self.assertEqual(open_calls["n"], 1,
                         "keep-set must open the DB exactly ONCE (SEV-3 #3)")

    def test_kg_binding_keep_set_single_open(self):
        open_calls = {"n": 0}

        class _Rows(list):
            def fetchall(self):
                return list(self)

        class _Conn:
            def execute(self, sql, *a):
                return _Rows([{"collection_name": "Foo_KnowledgeGraph"}])

            def close(self):
                pass

        def _counting_open():
            open_calls["n"] += 1
            return _Conn()

        with mock.patch.object(launcher_db_reader, "_open_db_readonly",
                               side_effect=_counting_open):
            names, resolvable = launcher_db_reader.kg_binding_keep_set()
        self.assertTrue(resolvable)
        self.assertEqual(names, ["Foo_KnowledgeGraph"])
        self.assertEqual(open_calls["n"], 1)

    def test_kg_binding_keep_set_unopenable(self):
        with mock.patch.object(launcher_db_reader, "_open_db_readonly",
                               return_value=None):
            names, resolvable = launcher_db_reader.kg_binding_keep_set()
        self.assertFalse(resolvable)
        self.assertEqual(names, [])


if __name__ == "__main__":
    unittest.main()
