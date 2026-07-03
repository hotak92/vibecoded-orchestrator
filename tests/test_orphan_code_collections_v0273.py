# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 FIX-C / FIX-C-RECUR / FIX-D4 / GAP-1 tests.

Covers:
  * GAP-1 — drop-collections is code-aware: derive_project_collection_names
    returns code_collections; _cmd_drop_collections drops them (drop-the-code-
    class case) and leaves shared alone (leave-alone case).
  * FIX-C — two-source orphan detector with BINDING-EXCLUSION seed + the hard
    data-safety guards (case-insensitive live-binding exclusion; unresolvable
    keep-set flags nothing; run-time re-validation).
  * FIX-C-RECUR — prefix-drift forward-guard (drift → deferral once; no change
    → silent).
  * FIX-D4 — SchemaDelta vectorIndexType change → copy; to_weaviate_config
    hfresh/RQ/distance guards; code_class_definitions hfresh; _build_plan
    code-aware gating (default hnsw adds nothing; hfresh enumerates code
    collections).

HTTP-level mocking only — no live Weaviate / launcher.db required.
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
from vco_lib.weaviate_schema import NamedVectorSlot  # noqa: E402

URL = "http://localhost:8081"


def _schema_payload(*class_names: str) -> dict:
    return {"classes": [{"class": cn} for cn in class_names]}


# ═══════════════════════════════════════════════════════════════════════════
# GAP-1 — code-aware drop-collections
# ═══════════════════════════════════════════════════════════════════════════

class Gap1CodeAwareDropTests(unittest.TestCase):
    def test_derive_returns_code_collections(self):
        d = project_init.derive_project_collection_names("Foo")
        self.assertIn("code_collections", d)
        self.assertEqual(
            sorted(d["code_collections"]),
            sorted([
                "Foo_CodeFunction", "Foo_CodeModule", "Foo_CodeClass",
                "Foo_CodeAPI", "Foo_CodeInteraction",
            ]),
        )

    def test_code_prefix_preserves_underscores(self):
        # code prefix uses canonical_class_prefix (underscore-preserving),
        # NOT the KG sanitizer (which collapses underscores).
        names = project_init.derive_project_code_collection_names(
            "vibecoded-orchestrator"
        )
        self.assertTrue(all(n.startswith("Vibecoded_orchestrator_") for n in names))

    def test_degenerate_name_yields_no_code_collections(self):
        self.assertEqual(project_init.derive_project_code_collection_names(""), [])

    def test_drop_the_code_class_case(self):
        """drop-collections drops the 5 code classes (the decision: DROP)."""
        dropped: list[str] = []

        def _fake_delete(name, weaviate_url=None):
            dropped.append(name)

        args = argparse.Namespace(name="Foo", weaviate_url=URL, json=True)
        with mock.patch.object(project_init, "_delete_class", _fake_delete):
            with mock.patch("builtins.print"):
                rc = project_init._cmd_drop_collections(args)
        self.assertEqual(rc, 0)
        for suffix in ("_CodeFunction", "_CodeModule", "_CodeClass",
                       "_CodeAPI", "_CodeInteraction"):
            self.assertIn(f"Foo{suffix}", dropped)
        # KG/DEV/DIAGRAMS still dropped.
        self.assertIn("Foo_KnowledgeGraph", dropped)

    def test_leave_alone_shared_case(self):
        """The shared KG is NEVER a drop target (the decision: LEAVE ALONE)."""
        dropped: list[str] = []
        with mock.patch.object(
            project_init, "_delete_class",
            side_effect=lambda n, weaviate_url=None: dropped.append(n),
        ):
            with mock.patch("builtins.print"):
                args = argparse.Namespace(name="Foo", weaviate_url=URL, json=True)
                project_init._cmd_drop_collections(args)
        self.assertNotIn(project_init._SHARED_KG_NAME, dropped)


# ═══════════════════════════════════════════════════════════════════════════
# FIX-C — orphan detector (binding-exclusion + hard guards)
# ═══════════════════════════════════════════════════════════════════════════

class OrphanDetectorTests(unittest.TestCase):
    def test_binding_exclusion_skips_live_prefix(self):
        det = project_init._detect_orphan_code_collections(
            URL, keep_set={"foo"}, keep_resolvable=True,
            schema_fetcher=lambda: [
                "Foo_CodeFunction", "Bar_CodeFunction", "Baz_KnowledgeGraph",
            ],
        )
        names = [o["class_name"] for o in det["live_orphans"]]
        self.assertNotIn("Foo_CodeFunction", names)   # in keep-set → active
        self.assertIn("Bar_CodeFunction", names)      # no binding → orphan
        self.assertNotIn("Baz_KnowledgeGraph", names)  # not a code class

    def test_case_insensitive_binding_exclusion(self):
        """A case-only variant of a live binding is NEVER flagged (BLOCKER-1)."""
        # keep-set is normalised (lowercased, alnum-only). A live class whose
        # prefix differs only by case must be excluded.
        det = project_init._detect_orphan_code_collections(
            URL, keep_set={"vibecodedorchestrator"}, keep_resolvable=True,
            schema_fetcher=lambda: [
                "VibeCodedOrchestrator_CodeFunction",   # canonical (87GB)
                "Vibecodedorchestrator_CodeFunction",   # lowercase-o variant
            ],
        )
        self.assertEqual(det["live_orphans"], [],
                         "case-variant of a live binding must never be flagged")

    def test_unresolvable_keep_set_flags_nothing(self):
        det = project_init._detect_orphan_code_collections(
            URL, keep_set=set(), keep_resolvable=False,
            schema_fetcher=lambda: ["Bar_CodeFunction"],
        )
        self.assertEqual(det["live_orphans"], [])
        self.assertEqual(det["ondisk_orphans"], [])
        self.assertFalse(det["keep_resolvable"])

    def test_ondisk_source_flags_stranded_dirs(self):
        det = project_init._detect_orphan_code_collections(
            URL, keep_set={"foo"}, keep_resolvable=True,
            schema_fetcher=lambda: ["Foo_CodeFunction"],
            volume_dir="/vol",
            ondisk_lister=lambda vd: [
                ("foo_codefunction", 100),        # live (keep-set) → skip
                ("zombie_codefunction", 5_000_000),  # stranded → flag
                ("raft", 999),                    # internal, not code → skip
                ("bar_codeclass", 3000),          # stranded → flag
            ],
        )
        dirs = {o["dir"] for o in det["ondisk_orphans"]}
        self.assertEqual(dirs, {"zombie_codefunction", "bar_codeclass"})
        self.assertEqual(det["total_reclaim_bytes"], 5_003_000)

    def test_ondisk_skips_dir_matching_live_class(self):
        det = project_init._detect_orphan_code_collections(
            URL, keep_set=set(), keep_resolvable=True,
            schema_fetcher=lambda: ["Live_CodeFunction"],
            volume_dir="/vol",
            ondisk_lister=lambda vd: [("live_codefunction", 12345)],
        )
        # dir matches a LIVE class (case-insensitively) → not stranded.
        self.assertEqual(det["ondisk_orphans"], [])

    def test_normalise_mirrors_rust(self):
        n = project_init._normalise_prefix_for_match
        canon = n("VibeCodedOrchestrator")
        self.assertEqual(n("VibeCoded_Orchestrator"), canon)
        self.assertEqual(n("vibecoded_orchestrator"), canon)
        self.assertEqual(n("Vibecodedorchestrator"), canon)
        self.assertEqual(n("VibeCoded Orchestrator"), canon)


class OrphanDropRevalidationTests(unittest.TestCase):
    def test_revalidated_drop_reprobes_bindings(self):
        """The consented drop RE-VALIDATES against current bindings, not a
        stale snapshot — a prefix that gained a binding is spared."""
        # Live schema has Bar_* + Foo_*; keep-set now has 'bar' (re-added).
        with mock.patch.object(
            project_init, "_codegraph_keep_set_normalised",
            return_value=({"bar"}, True),
        ), mock.patch.object(
            project_init, "_list_classes",
            return_value=["Bar_CodeFunction", "Foo_CodeFunction"],
        ):
            out = project_init._revalidated_orphan_live_classes(URL)
        self.assertIn("Foo_CodeFunction", out)
        self.assertNotIn("Bar_CodeFunction", out)  # re-added → spared

    def test_revalidated_drop_unresolvable_drops_nothing(self):
        with mock.patch.object(
            project_init, "_codegraph_keep_set_normalised",
            return_value=(set(), False),
        ), mock.patch.object(
            project_init, "_list_classes",
            return_value=["Foo_CodeFunction"],
        ):
            self.assertEqual(project_init._revalidated_orphan_live_classes(URL), [])

    def test_drop_orphan_cmd_requires_confirm(self):
        args = argparse.Namespace(weaviate_url=URL, confirm=False, json=True)
        with mock.patch("builtins.print"):
            rc = project_init._cmd_drop_orphan_code_collections(args)
        self.assertEqual(rc, 2)

    def test_reclaim_requires_both_flags(self):
        args = argparse.Namespace(
            volume_dir="/vol", weaviate_url=URL, confirm=True,
            i_understand_filesystem_level=False, json=True,
        )
        with mock.patch("builtins.print"):
            rc = project_init._cmd_reclaim_stranded_code_segments(args)
        self.assertEqual(rc, 2)

    def test_reclaim_refuses_while_weaviate_up(self):
        with tempfile.TemporaryDirectory() as td:
            args = argparse.Namespace(
                volume_dir=td, weaviate_url=URL, confirm=True,
                i_understand_filesystem_level=True, json=True,
            )
            # /v1/meta answers 200 → Weaviate up → refuse.
            with mock.patch.object(
                project_init, "_http_request", return_value=(200, b"{}"),
            ), mock.patch("builtins.print"):
                rc = project_init._cmd_reclaim_stranded_code_segments(args)
            self.assertEqual(rc, 2)


class OrphanDeferralTests(unittest.TestCase):
    def test_deferral_emitted_only_when_orphans(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            # no orphans → no deferral
            empty = {"live_orphans": [], "ondisk_orphans": [],
                     "total_reclaim_bytes": 0, "volume_dir": None}
            self.assertFalse(
                project_init._emit_orphan_code_collections_deferral(
                    folder, URL, empty)
            )
            # orphans → deferral written
            det = {
                "live_orphans": [{"class_name": "Bar_CodeFunction",
                                  "prefix": "Bar", "suffix": "_CodeFunction",
                                  "object_count": 5}],
                "ondisk_orphans": [{"dir": "zombie_codefunction",
                                    "size_bytes": 5_000_000}],
                "total_reclaim_bytes": 5_000_000,
                "volume_dir": "/vol",
            }
            self.assertTrue(
                project_init._emit_orphan_code_collections_deferral(
                    folder, URL, det)
            )
            md = (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
            self.assertIn("orphan_code_collections_detected", md)
            self.assertIn("drop-orphan-code-collections", md)
            self.assertIn("reclaim-stranded-code-segments", md)
            self.assertIn("--i-understand-filesystem-level", md)


# ═══════════════════════════════════════════════════════════════════════════
# FIX-C-RECUR — prefix-drift forward-guard
# ═══════════════════════════════════════════════════════════════════════════

class PrefixDriftTests(unittest.TestCase):
    def test_first_run_records_baseline_no_drift(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            r = project_init.detect_codegraph_prefix_drift(
                folder, "Foo", emit_deferral=False)
            self.assertIsNone(r)
            self.assertEqual(
                project_init._read_codegraph_prefix_generation(folder), "Foo")

    def test_drift_detected_and_deferral_emitted_once(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            project_init._write_codegraph_prefix_generation(folder, "Oldprefix")
            r = project_init.detect_codegraph_prefix_drift(
                folder, "Foo", emit_deferral=True)
            self.assertEqual(r, {"old_prefix": "Oldprefix", "new_prefix": "Foo"})
            md = (folder / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text()
            self.assertIn("codegraph_prefix_drift_detected", md)
            # Fires once: after recording the new gen, re-run is silent.
            r2 = project_init.detect_codegraph_prefix_drift(
                folder, "Foo", emit_deferral=False)
            self.assertIsNone(r2)

    def test_case_only_change_is_not_drift(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            # record a case-variant of the same logical prefix.
            cur = project_init.derive_project_code_prefix("Foo")
            project_init._write_codegraph_prefix_generation(folder, cur.lower())
            r = project_init.detect_codegraph_prefix_drift(
                folder, "Foo", emit_deferral=False)
            self.assertIsNone(r, "case-only difference is the SAME generation")


# ═══════════════════════════════════════════════════════════════════════════
# FIX-D4 — HFresh wiring
# ═══════════════════════════════════════════════════════════════════════════

class HFreshConfigTests(unittest.TestCase):
    def test_hnsw_default_unchanged(self):
        s = NamedVectorSlot("codesage_embed", 2048)
        self.assertEqual(
            s.to_weaviate_config(),
            {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
        )

    def test_hfresh_emits_index_type_and_rq_config(self):
        s = NamedVectorSlot("codesage_embed", 2048)
        cfg = s.to_weaviate_config(index_type="hfresh")
        self.assertEqual(cfg["vectorIndexType"], "hfresh")
        self.assertEqual(cfg["vectorIndexConfig"]["distance"], "cosine")

    def test_hfresh_rescore_limit(self):
        s = NamedVectorSlot("codesage_embed", 2048)
        cfg = s.to_weaviate_config(index_type="hfresh", rescore_limit=20)
        self.assertEqual(cfg["vectorIndexConfig"]["rescoreLimit"], 20)

    def test_hfresh_refuses_dot_distance(self):
        s = NamedVectorSlot("codesage_embed", 2048)
        with self.assertRaises(ValueError):
            s.to_weaviate_config(index_type="hfresh", distance="dot")

    def test_hfresh_allows_l2_squared(self):
        s = NamedVectorSlot("codesage_embed", 2048)
        cfg = s.to_weaviate_config(index_type="hfresh", distance="l2-squared")
        self.assertEqual(cfg["vectorIndexConfig"]["distance"], "l2-squared")

    def test_unsupported_index_type_raises(self):
        s = NamedVectorSlot("codesage_embed", 2048)
        with self.assertRaises(ValueError):
            s.to_weaviate_config(index_type="flat")

    def test_code_class_definitions_hfresh(self):
        d = project_init.code_class_definitions("Foo_", index_type="hfresh")
        vt = d["CodeFunction"]["vectorConfig"]["codesage_embed"]["vectorIndexType"]
        self.assertEqual(vt, "hfresh")


class SchemaDeltaIndexTypeTests(unittest.TestCase):
    def _shape(self, index_type):
        return {
            "vectorConfig": {"codesage_embed": {"vectorIndexType": index_type}},
            "invertedIndexConfig": {"indexNullState": True},
            "properties": [],
        }

    def test_hnsw_to_hfresh_routes_to_copy(self):
        d = project_init._schema_delta(
            self._shape("hnsw"),
            {
                "vectorConfig": {
                    "codesage_embed": {
                        "vectorIndexType": "hfresh",
                        "vectorIndexConfig": {"distance": "cosine"},
                    }
                },
                "invertedIndexConfig": {"indexNullState": True},
                "properties": [],
            },
        )
        self.assertEqual(d.vector_index_type_change, "hfresh")
        self.assertEqual(project_init._classify_action(d), "copy")

    def test_same_index_type_is_noop(self):
        d = project_init._schema_delta(self._shape("hnsw"), self._shape("hnsw"))
        self.assertIsNone(d.vector_index_type_change)
        self.assertEqual(project_init._classify_action(d), "noop")


class BuildPlanCodeAwareTests(unittest.TestCase):
    def setUp(self):
        # Isolate from an ambient VCO shell env (KG/DEV/DIAGRAMS + code-index
        # env). _build_plan reads these; the test asserts only the code-graph
        # code path, so clear them for a deterministic plan.
        self._env = mock.patch.dict("os.environ", {
            "KG_COLLECTION": "",
            "DEVELOPMENT_COLLECTION": "",
            "DIAGRAMS_COLLECTION": "",
            "VCT_CODEGRAPH_INDEX_TYPE": "",
        }, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _code_schema(self):
        slots = {s: {"vectorIndexType": "hnsw"} for s in (
            "codesage_embed", "ollama_code_embed", "openai_embed",
            "qwen3_embed", "jina_embed", "openai_code_embed",
        )}
        return {
            "vectorConfig": slots,
            "invertedIndexConfig": {"indexNullState": True},
            "properties": [{"name": "language", "dataType": ["text"]}],
        }

    def test_default_hnsw_adds_no_code_collections(self):
        ns = argparse.Namespace(force_rebuild=False, name="Foo", index_type=None)
        plan = project_init._build_plan(ns, schema_fetcher=lambda n: None)
        self.assertEqual([p["collection"] for p in plan], [])

    def test_hfresh_enumerates_existing_code_collections(self):
        def fetch(n):
            if n.endswith(("_CodeFunction", "_CodeModule", "_CodeClass",
                           "_CodeAPI", "_CodeInteraction")):
                return self._code_schema()
            return None
        ns = argparse.Namespace(force_rebuild=False, name="Foo", index_type="hfresh")
        plan = project_init._build_plan(ns, schema_fetcher=fetch)
        self.assertEqual(len(plan), 5)
        self.assertTrue(all(p["action"] == "copy" for p in plan))

    def test_hfresh_skips_absent_code_collections(self):
        ns = argparse.Namespace(force_rebuild=False, name="Foo", index_type="hfresh")
        plan = project_init._build_plan(ns, schema_fetcher=lambda n: None)
        self.assertEqual(plan, [])


if __name__ == "__main__":
    unittest.main()
