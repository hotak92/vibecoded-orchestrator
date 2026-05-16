"""Tests for PR-10B legacy collection detection (vco_lib.project_init).

Covers:
  - _detect_legacy_kg_collections: empty Weaviate → []
  - No matching suffix → []
  - Single legacy candidate matches THIS project → 1 entry
  - Multiple candidates → all returned, deterministic order
  - Different-project class is filtered out (Foo project + Agape class)
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
        self.assertLessEqual(project_init._levenshtein("Artup", "ARTup"), 3)


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
            project_init._is_similar_prefix("Foo", "Agape"),
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
                ["RandomCollection", "AnotherThing", "VibeCodedTools_KnowledgeGraph"],
            ),
        ):
            # VibeCodedTools_KnowledgeGraph is the shared KG; it has the
            # KG suffix but the prefix has zero similarity to "Foo" so it
            # must NOT be returned.
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
        # CRITICAL: project "Foo" + Weaviate has Agape_KnowledgeGraph,
        # ARTup_KnowledgeGraph, SD15_KnowledgeGraph from other projects.
        # Must return EMPTY — never claim other-project data is "legacy".
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                [
                    "Agape_KnowledgeGraph",
                    "ARTup_KnowledgeGraph",
                    "SD15_KnowledgeGraph",
                    "Foo_KnowledgeGraph",  # canonical for Foo
                ],
                counts={
                    "Agape_KnowledgeGraph": 999,
                    "ARTup_KnowledgeGraph": 999,
                    "SD15_KnowledgeGraph": 999,
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
                ["Agape_CodeFunction", "ARTup_CodeModule"],
                counts={"Agape_CodeFunction": 50, "ARTup_CodeModule": 50},
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


if __name__ == "__main__":
    unittest.main()
