"""Tests for PR-10B legacy collection detection (vco_lib.project_init).

Covers:
  - _detect_legacy_kg_collections: empty Weaviate → []
  - No matching suffix → []
  - Single legacy candidate matches THIS project → 1 entry
  - Multiple candidates → all returned, deterministic order
  - Different-project class is filtered out (Foo project + Quux class)
  - Weaviate unreachable → [] (no exception)
  - Substring-prefix similarity (FooBar project + Foo_KnowledgeGraph)
  - Exact canonical class is NOT a candidate
  - _detect_legacy_codegraph_collections same conservative filter
  - HTTP-level mocking only — no live Weaviate required
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _schema_payload(*class_names: str) -> dict:
    """Build a Weaviate /v1/schema response with the given class names."""
    return {"classes": [{"class": cn} for cn in class_names]}


def _aggregate_payload(class_name: str, count: int) -> dict:
    """Build a Weaviate /v1/graphql Aggregate response for one class."""
    return {
        "data": {
            "Aggregate": {
                class_name: [{"meta": {"count": count}}],
            }
        }
    }


def _make_http_request_mock(
    schema_classes: list[str],
    counts: dict[str, int] | None = None,
    fail: bool = False,
):
    """Build a side_effect for project_init._http_request that responds to
    GET /v1/schema with `schema_classes` and POST /v1/graphql with the
    matching count from `counts` (default 0).

    `fail=True` → all requests raise URLError (Weaviate unreachable).
    """
    counts = counts or {}

    def _side_effect(method, url, *, body=None, timeout=30.0):
        if fail:
            raise urllib.error.URLError("connection refused")

        if method == "GET" and url.endswith("/v1/schema"):
            payload = _schema_payload(*schema_classes)
            return (200, json.dumps(payload).encode("utf-8"))

        if method == "POST" and url.endswith("/v1/graphql"):
            # Extract the class name from the GraphQL query body.
            query = (body or {}).get("query", "")
            for cls in schema_classes:
                if cls in query:
                    cnt = counts.get(cls, 0)
                    return (200, json.dumps(_aggregate_payload(cls, cnt)).encode("utf-8"))
            # Unknown class → empty Aggregate (returns count 0).
            return (200, json.dumps({"data": {"Aggregate": {}}}).encode("utf-8"))

        return (404, b"")

    return _side_effect


URL = "http://localhost:8081"


# ---------------------------------------------------------------------------
# _levenshtein
# ---------------------------------------------------------------------------

class LevenshteinTests(unittest.TestCase):

    def test_identical_strings(self):
        self.assertEqual(project_init._levenshtein("foo", "foo"), 0)

    def test_empty_strings(self):
        self.assertEqual(project_init._levenshtein("", ""), 0)
        self.assertEqual(project_init._levenshtein("foo", ""), 3)
        self.assertEqual(project_init._levenshtein("", "foo"), 3)

    def test_single_substitution(self):
        self.assertEqual(project_init._levenshtein("foo", "fox"), 1)

    def test_transposition_counts_as_two(self):
        # Pure Levenshtein, not Damerau — "ab" → "ba" is two substitutions.
        self.assertEqual(project_init._levenshtein("ab", "ba"), 2)

    def test_close_project_names(self):
        self.assertLessEqual(project_init._levenshtein("Foo", "FoO"), 1)
        self.assertLessEqual(project_init._levenshtein("Quux", "QUUX"), 3)


# ---------------------------------------------------------------------------
# _is_similar_prefix
# ---------------------------------------------------------------------------

class SimilarPrefixTests(unittest.TestCase):

    def test_exact_match(self):
        self.assertTrue(project_init._is_similar_prefix("Foo", "Foo"))

    def test_case_insensitive_match(self):
        self.assertTrue(project_init._is_similar_prefix("foo", "FOO"))

    def test_substring_match(self):
        self.assertTrue(project_init._is_similar_prefix("Foo", "FooBar"))
        self.assertTrue(project_init._is_similar_prefix("FooBar", "Foo"))

    def test_levenshtein_within_threshold(self):
        # 3-char distance between "Foo" and "Bar" — false (distance is 3
        # but they share zero substring → falls back to Levenshtein).
        self.assertTrue(
            project_init._is_similar_prefix("Foo", "Foa"),
            "single char substitution should match",
        )

    def test_levenshtein_above_threshold(self):
        self.assertFalse(
            project_init._is_similar_prefix("Foo", "Quux"),
            "totally different names should not match",
        )

    def test_empty_inputs(self):
        self.assertFalse(project_init._is_similar_prefix("", "Foo"))
        self.assertFalse(project_init._is_similar_prefix("Foo", ""))


# ---------------------------------------------------------------------------
# _strip_known_suffix
# ---------------------------------------------------------------------------

class StripSuffixTests(unittest.TestCase):

    def test_kg_suffix(self):
        self.assertEqual(
            project_init._strip_known_suffix(
                "Foo_KnowledgeGraph", project_init._KG_SUFFIXES,
            ),
            ("Foo", "_KnowledgeGraph"),
        )

    def test_dev_suffix(self):
        self.assertEqual(
            project_init._strip_known_suffix(
                "Foo_Development", project_init._KG_SUFFIXES,
            ),
            ("Foo", "_Development"),
        )

    def test_no_match(self):
        self.assertIsNone(
            project_init._strip_known_suffix(
                "RandomThing", project_init._KG_SUFFIXES,
            )
        )

    def test_codegraph_suffix(self):
        self.assertEqual(
            project_init._strip_known_suffix(
                "Foo_CodeFunction", project_init._CODEGRAPH_SUFFIXES,
            ),
            ("Foo", "_CodeFunction"),
        )

    def test_suffix_only_no_prefix_returns_none(self):
        # Class named exactly "_KnowledgeGraph" with no prefix → reject.
        self.assertIsNone(
            project_init._strip_known_suffix(
                "_KnowledgeGraph", project_init._KG_SUFFIXES,
            )
        )


# ---------------------------------------------------------------------------
# _detect_legacy_kg_collections
# ---------------------------------------------------------------------------

class DetectLegacyKgTests(unittest.TestCase):

    def test_empty_weaviate_returns_empty(self):
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock([]),
        ):
            result = project_init._detect_legacy_kg_collections("Foo", URL)
        self.assertEqual(result, [])

    def test_no_kg_suffix_classes_returns_empty(self):
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                # Legacy-detection: VibeCodedTools_KnowledgeGraph is the
                # pre-v0.2.12 PR-26 shared-KG name. Keep the literal here
                # to exercise the "wrong prefix despite KG suffix" path
                # against a name that actually appears on legacy installs.
                ["RandomCollection", "AnotherThing", "VibeCodedTools_KnowledgeGraph"],
            ),
        ):
            # The shared KG (legacy name) has the KG suffix but the prefix
            # has zero similarity to "Foo" so it must NOT be returned.
            result = project_init._detect_legacy_kg_collections("Foo", URL)
        self.assertEqual(result, [])

    def test_canonical_class_alone_is_not_legacy(self):
        # Project "Foo" has only its canonical Foo_KnowledgeGraph → no
        # legacy candidates (the fresh-install case).
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(["Foo_KnowledgeGraph"]),
        ):
            result = project_init._detect_legacy_kg_collections("Foo", URL)
        self.assertEqual(result, [])

    def test_single_candidate_same_suffix_different_prefix(self):
        # Project "FooBar"; legacy class "Foo_KnowledgeGraph" (substring).
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                ["Foo_KnowledgeGraph"],
                counts={"Foo_KnowledgeGraph": 42},
            ),
        ):
            result = project_init._detect_legacy_kg_collections("FooBar", URL)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["class_name"], "Foo_KnowledgeGraph")
        self.assertEqual(result[0]["suffix"], "_KnowledgeGraph")
        self.assertEqual(result[0]["object_count"], 42)
        self.assertEqual(result[0]["canonical_name"], "FooBar_KnowledgeGraph")

    def test_multiple_candidates_all_returned_sorted(self):
        # Project "FooBar"; both KG and Dev legacies.
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                ["Foo_KnowledgeGraph", "Foo_Development"],
                counts={"Foo_KnowledgeGraph": 100, "Foo_Development": 5},
            ),
        ):
            result = project_init._detect_legacy_kg_collections("FooBar", URL)
        self.assertEqual(len(result), 2)
        # Sorted by suffix then class_name → _Development first.
        self.assertEqual(result[0]["class_name"], "Foo_Development")
        self.assertEqual(result[0]["suffix"], "_Development")
        self.assertEqual(result[1]["class_name"], "Foo_KnowledgeGraph")
        self.assertEqual(result[1]["suffix"], "_KnowledgeGraph")

    def test_other_project_classes_filtered_out(self):
        # CRITICAL: project "Foo" + Weaviate has Quux_KnowledgeGraph,
        # Bazquux_KnowledgeGraph, Quuux_KnowledgeGraph from other projects.
        # Must return EMPTY — never claim other-project data is "legacy".
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                [
                    "Quux_KnowledgeGraph",
                    "Bazquux_KnowledgeGraph",
                    "Quuux_KnowledgeGraph",
                    "Foo_KnowledgeGraph",  # canonical for Foo
                ],
                counts={
                    "Quux_KnowledgeGraph": 999,
                    "Bazquux_KnowledgeGraph": 999,
                    "Quuux_KnowledgeGraph": 999,
                },
            ),
        ):
            result = project_init._detect_legacy_kg_collections("Foo", URL)
        self.assertEqual(result, [], "must not surface other-project KGs")

    def test_substring_match_VCO_to_FooBar(self):
        # Spec example: project "FooBar" + class "Foo_KnowledgeGraph"
        # → similarity match (substring), returned.
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                ["Foo_KnowledgeGraph"],
                counts={"Foo_KnowledgeGraph": 7},
            ),
        ):
            result = project_init._detect_legacy_kg_collections("FooBar", URL)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["class_name"], "Foo_KnowledgeGraph")

    def test_weaviate_unreachable_returns_empty(self):
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock([], fail=True),
        ):
            result = project_init._detect_legacy_kg_collections("Foo", URL)
        self.assertEqual(result, [])

    def test_weaviate_500_returns_empty(self):
        with mock.patch.object(
            project_init, "_http_request",
            return_value=(500, b"internal error"),
        ):
            result = project_init._detect_legacy_kg_collections("Foo", URL)
        self.assertEqual(result, [])

    def test_malformed_json_returns_empty(self):
        with mock.patch.object(
            project_init, "_http_request",
            return_value=(200, b"not valid json"),
        ):
            result = project_init._detect_legacy_kg_collections("Foo", URL)
        self.assertEqual(result, [])

    def test_empty_project_name_returns_empty(self):
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(["Foo_KnowledgeGraph"]),
        ):
            result = project_init._detect_legacy_kg_collections("", URL)
        self.assertEqual(result, [])

    def test_count_unknown_when_aggregate_fails(self):
        # GET /v1/schema succeeds but POST /v1/graphql fails for one class
        # → object_count is None, but the candidate is still returned.
        call_count = {"n": 0}

        def _side(method, url, *, body=None, timeout=30.0):
            call_count["n"] += 1
            if method == "GET" and url.endswith("/v1/schema"):
                return (
                    200,
                    json.dumps(_schema_payload("Foo_KnowledgeGraph")).encode("utf-8"),
                )
            if method == "POST" and url.endswith("/v1/graphql"):
                # Simulate transient graphql failure.
                return (503, b"unavailable")
            return (404, b"")

        with mock.patch.object(project_init, "_http_request", side_effect=_side):
            result = project_init._detect_legacy_kg_collections("FooBar", URL)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["object_count"])


# ---------------------------------------------------------------------------
# _detect_legacy_codegraph_collections
# ---------------------------------------------------------------------------

class DetectLegacyCodegraphTests(unittest.TestCase):

    def test_no_codegraph_classes_returns_empty(self):
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(["Foo_KnowledgeGraph"]),
        ):
            result = project_init._detect_legacy_codegraph_collections("Foo", URL)
        self.assertEqual(result, [])

    def test_all_5_codegraph_suffixes_detected(self):
        legacy_classes = [
            "Foo_CodeFunction",
            "Foo_CodeModule",
            "Foo_CodeClass",
            "Foo_CodeAPI",
            "Foo_CodeInteraction",
        ]
        counts = {c: 10 for c in legacy_classes}
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(legacy_classes, counts=counts),
        ):
            result = project_init._detect_legacy_codegraph_collections("FooBar", URL)
        self.assertEqual(len(result), 5)
        suffixes_seen = {c["suffix"] for c in result}
        self.assertEqual(
            suffixes_seen,
            set(project_init._CODEGRAPH_SUFFIXES),
        )

    def test_other_project_codegraph_filtered_out(self):
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                ["Quux_CodeFunction", "Bazquux_CodeModule"],
                counts={"Quux_CodeFunction": 50, "Bazquux_CodeModule": 50},
            ),
        ):
            result = project_init._detect_legacy_codegraph_collections("Foo", URL)
        self.assertEqual(result, [])

    def test_canonical_codegraph_not_legacy(self):
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                ["Foo_CodeFunction", "Foo_CodeModule"],
                counts={"Foo_CodeFunction": 999, "Foo_CodeModule": 999},
            ),
        ):
            result = project_init._detect_legacy_codegraph_collections("Foo", URL)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# _emit_legacy_kg_deferral / _emit_legacy_codegraph_deferral end-to-end
# ---------------------------------------------------------------------------

class EmitDeferralTests(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="vco-pr10b-")
        self.folder = Path(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kg_deferral_writes_update_deferred_md(self):
        candidates = [{
            "class_name": "Foo_KnowledgeGraph",
            "suffix": "_KnowledgeGraph",
            "object_count": 42,
            "embedding_dim": 1024,
            "canonical_name": "FooBar_KnowledgeGraph",
        }]
        project_init._emit_legacy_kg_deferral(
            self.folder,
            project_name="FooBar",
            weaviate_url="http://localhost:8081",
            candidates=candidates,
        )
        deferral_path = self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertTrue(deferral_path.exists())
        text = deferral_path.read_text(encoding="utf-8")
        self.assertIn("kg_collection_legacy_candidates", text)
        self.assertIn("Foo_KnowledgeGraph", text)
        self.assertIn("FooBar_KnowledgeGraph", text)
        self.assertIn("42", text)

    def test_kg_deferral_empty_candidates_noop(self):
        project_init._emit_legacy_kg_deferral(
            self.folder,
            project_name="FooBar",
            weaviate_url="http://localhost:8081",
            candidates=[],
        )
        deferral_path = self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertFalse(deferral_path.exists())

    def test_codegraph_deferral_severity_info(self):
        candidates = [{
            "class_name": "Foo_CodeFunction",
            "suffix": "_CodeFunction",
            "object_count": 1234,
            "embedding_dim": None,
            "canonical_name": "FooBar_CodeFunction",
        }]
        project_init._emit_legacy_codegraph_deferral(
            self.folder,
            project_name="FooBar",
            weaviate_url="http://localhost:8081",
            candidates=candidates,
        )
        deferral_path = self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
        self.assertTrue(deferral_path.exists())
        text = deferral_path.read_text(encoding="utf-8")
        self.assertIn("codegraph_collection_legacy_candidates", text)
        self.assertIn("(info)", text)
        self.assertIn("code-graph-analyze", text)


# ---------------------------------------------------------------------------
# BUG-1 (v0.2.73) — DATA-LOSS: active canonical must never be flagged legacy
# ---------------------------------------------------------------------------

class Bug1ActiveCanonicalNeverLegacyTests(unittest.TestCase):
    """The legacy detector must never propose dropping the collection the
    project is actively bound to, even when the case-lossy sanitizer yields a
    different casing than the real stored class name.
    """

    def test_case_variant_active_canonical_is_not_legacy(self):
        # Sanitizer for an all-lowercase project name yields a lowercase-c
        # canonical, but the REAL active class is uppercase-C. The active
        # class must NOT be flagged as a legacy drop target (case-insensitive
        # canonical skip).
        #
        # Use generic example names: project "prefixexample-orchestrator"
        # sanitizes to "PrefixexampleOrchestrator" (lowercase-e in the middle),
        # while the real stored class is "PrefixExampleOrchestrator_..."
        project = "prefixexample-orchestrator"
        real_class = "PrefixExampleOrchestrator_KnowledgeGraph"
        # Sanity: the sanitizer's casing differs from the real class casing
        # (this is the exact BUG-1 condition — case-lossy sanitizer).
        sanitized = project_init.sanitize_for_weaviate_class(project)
        self.assertNotEqual(
            sanitized + "_KnowledgeGraph", real_class,
            "test premise: sanitizer casing must differ from the real class",
        )
        self.assertEqual(sanitized.lower(), "prefixexampleorchestrator")

        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                [real_class],
                counts={real_class: 2590},
            ),
        ):
            # No live binding injected → skip must still fire via the
            # case-insensitive canonical match alone.
            result = project_init._detect_legacy_kg_collections(project, URL)
        self.assertEqual(
            result, [],
            "case-variant of the active canonical must never be flagged legacy",
        )

    def test_live_binding_class_is_never_legacy(self):
        # Even if the class name were somehow prefix-similar but NOT a case
        # match of the sanitizer canonical, an explicit live KG_COLLECTION
        # binding must exclude it as a drop target.
        project = "FooBar"
        live_class = "Foo_KnowledgeGraph"  # what the project actually reads
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                [live_class],
                counts={live_class: 999},
            ),
        ):
            result = project_init._detect_legacy_kg_collections(
                project, URL, live_binding=live_class,
            )
        self.assertEqual(
            result, [],
            "the live KG_COLLECTION binding is never a legacy drop target",
        )

    def test_live_binding_resolved_from_env(self):
        # When live_binding is not passed, it is resolved from the
        # KG_COLLECTION env var.
        project = "FooBar"
        live_class = "Foo_KnowledgeGraph"
        with mock.patch.dict("os.environ", {"KG_COLLECTION": live_class}):
            with mock.patch.object(
                project_init, "_http_request",
                side_effect=_make_http_request_mock(
                    [live_class], counts={live_class: 5},
                ),
            ):
                result = project_init._detect_legacy_kg_collections(project, URL)
        self.assertEqual(result, [])

    def test_genuine_legacy_still_detected(self):
        # Guard against over-correction: a TRULY different prefix (real
        # drop target) must STILL be detected. Project "FooBar" with a
        # genuine legacy "Foo_KnowledgeGraph" that is neither a case-variant
        # of the canonical (FooBar_KnowledgeGraph) nor the live binding.
        with mock.patch.dict("os.environ", {"KG_COLLECTION": "FooBar_KnowledgeGraph"}):
            with mock.patch.object(
                project_init, "_http_request",
                side_effect=_make_http_request_mock(
                    ["Foo_KnowledgeGraph"],
                    counts={"Foo_KnowledgeGraph": 42},
                ),
            ):
                result = project_init._detect_legacy_kg_collections("FooBar", URL)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["class_name"], "Foo_KnowledgeGraph")
        self.assertFalse(
            result[0]["case_only"],
            "a different-prefix legacy is NOT a case-only pair",
        )


# ---------------------------------------------------------------------------
# BUG-1 hard guard — rendered command never drops a case-only pair
# ---------------------------------------------------------------------------

class Bug1CommandGuardTests(unittest.TestCase):

    def test_case_only_pair_renders_no_destructive_ops(self):
        # A case-only candidate (old.lower() == new.lower()) must render a
        # case-REBIND instruction — never _delete_class or
        # _copy_collection_with_vectors.
        candidates = [{
            "class_name": "PrefixExampleOrchestrator_KnowledgeGraph",
            "suffix": "_KnowledgeGraph",
            "object_count": 2590,
            "embedding_dim": 1024,
            "canonical_name": "Prefixexampleorchestrator_KnowledgeGraph",
            "case_only": True,
        }]
        cmd = project_init._format_legacy_kg_command(
            "prefixexample-orchestrator",
            "http://localhost:8081",
            candidates,
        )
        self.assertNotIn("_delete_class", cmd)
        self.assertNotIn("_copy_collection_with_vectors", cmd)
        self.assertIn("case-REBIND", cmd)
        self.assertIn("bootstrap-collections", cmd)

    def test_case_only_guard_fires_even_without_flag(self):
        # Belt-and-suspenders: even if a candidate omits/misreports case_only,
        # the name comparison in the renderer catches the case-only pair.
        candidates = [{
            "class_name": "Foo_KnowledgeGraph",
            "suffix": "_KnowledgeGraph",
            "object_count": 100,
            "embedding_dim": None,
            "canonical_name": "FOO_KnowledgeGraph",  # case-only vs class_name
            # case_only intentionally omitted
        }]
        cmd = project_init._format_legacy_kg_command(
            "FOO", "http://localhost:8081", candidates,
        )
        self.assertNotIn("_delete_class", cmd)
        self.assertNotIn("_copy_collection_with_vectors", cmd)
        self.assertIn("case-REBIND", cmd)


# ---------------------------------------------------------------------------
# BUG-2 — genuine legacy migration uses re-embed-from-.md, not copy-vectors
# ---------------------------------------------------------------------------

class Bug2ReEmbedRemediationTests(unittest.TestCase):

    def test_genuine_legacy_command_uses_reembed_not_copy(self):
        candidates = [{
            "class_name": "Foo_KnowledgeGraph",
            "suffix": "_KnowledgeGraph",
            "object_count": 42,
            "embedding_dim": 1024,
            "canonical_name": "FooBar_KnowledgeGraph",
            "case_only": False,
        }]
        cmd = project_init._format_legacy_kg_command(
            "FooBar", "http://localhost:8081", candidates,
        )
        # RE-EMBED sequence: bootstrap → kg-sync --all → drop legacy.
        self.assertIn("bootstrap-collections", cmd)
        self.assertIn("kg-sync --all", cmd)
        self.assertIn("_delete_class", cmd)  # drop of the GENUINE legacy is OK
        # Must NOT use the vector-copy helper (422 on shape mismatch).
        self.assertNotIn("_copy_collection_with_vectors", cmd)


class Bug2CopyShapeGuardTests(unittest.TestCase):
    """`_copy_collection_with_vectors` must fail CLEARLY, not with an opaque
    422, when the destination is single-vector while the source is named.
    """

    def test_single_vector_dst_raises_clear_error(self):
        # Mock _fetch_schema(dst) → single-vector (no vectorConfig).
        def _fake_fetch(name, weaviate_url=None):
            # Destination has NO vectorConfig → single-vector.
            return {"class": name, "properties": []}

        with mock.patch.object(project_init, "_fetch_schema", side_effect=_fake_fetch):
            with self.assertRaises(ValueError) as ctx:
                project_init._copy_collection_with_vectors(
                    "Src_KnowledgeGraph",
                    "Dst_KnowledgeGraph",
                    weaviate_url=URL,
                )
        msg = str(ctx.exception)
        self.assertIn("single-vector", msg)
        self.assertIn("re-embed", msg.lower())

    def test_schema_probe_soft_fails_open(self):
        # If the dst schema probe raises (transient network), the guard must
        # NOT block — it falls through to the copy path (which will surface
        # any real failure). We assert the ValueError shape-guard does NOT
        # fire; the copy then fails on the v4 client connect (expected), which
        # is a DIFFERENT error than the shape ValueError.
        def _raise_fetch(name, weaviate_url=None):
            raise RuntimeError("transient network")

        with mock.patch.object(project_init, "_fetch_schema", side_effect=_raise_fetch):
            with mock.patch.object(
                project_init, "_connect_v4_client",
                side_effect=RuntimeError("v4-connect-unavailable"),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    project_init._copy_collection_with_vectors(
                        "Src_KnowledgeGraph", "Dst_KnowledgeGraph",
                        weaviate_url=URL,
                    )
        # It fell through to the copy path (connect error), NOT the shape guard.
        self.assertIn("v4-connect-unavailable", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
