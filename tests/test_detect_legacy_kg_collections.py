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


# ---------------------------------------------------------------------------
# _configured_canonical_class_names + folder-name-mismatch false positive
# ---------------------------------------------------------------------------

class ConfiguredCanonicalNamesTests(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="vco-cfgcanon-")
        self.folder = Path(self.tmp)
        (self.folder / ".claude").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_settings(self, env: dict):
        (self.folder / ".claude" / "settings.json").write_text(
            json.dumps({"env": env}), encoding="utf-8",
        )

    def _write_env_file(self, lines: list[str]):
        (self.folder / ".claude" / "env").write_text(
            "\n".join(lines) + "\n", encoding="utf-8",
        )

    def test_empty_folder_returns_empty_set(self):
        self.assertEqual(
            project_init._configured_canonical_class_names(self.folder),
            set(),
        )

    def test_reads_kg_and_dev_from_settings_json(self):
        self._write_settings({
            "KG_COLLECTION": "Test_KnowledgeGraph",
            "DEVELOPMENT_COLLECTION": "Test_Development",
            "DIAGRAMS_COLLECTION": "Test_Diagrams",
        })
        names = project_init._configured_canonical_class_names(self.folder)
        self.assertIn("Test_KnowledgeGraph", names)
        self.assertIn("Test_Development", names)
        self.assertIn("Test_Diagrams", names)

    def test_codegraph_family_expanded_from_prefix(self):
        self._write_settings({"CODE_GRAPH_PROJECT": "Test"})
        names = project_init._configured_canonical_class_names(self.folder)
        for sfx in project_init._CODEGRAPH_SUFFIXES:
            self.assertIn(f"Test{sfx}", names)

    def test_reads_from_env_file_when_no_settings(self):
        self._write_env_file([
            '# comment',
            'export KG_COLLECTION="Test_KnowledgeGraph"',
            'export DEVELOPMENT_COLLECTION="Test_Development"',
        ])
        names = project_init._configured_canonical_class_names(self.folder)
        self.assertIn("Test_KnowledgeGraph", names)
        self.assertIn("Test_Development", names)

    def test_settings_json_wins_over_env_file(self):
        self._write_settings({"KG_COLLECTION": "Test_KnowledgeGraph"})
        self._write_env_file(['export KG_COLLECTION="Other_KnowledgeGraph"'])
        names = project_init._configured_canonical_class_names(self.folder)
        self.assertIn("Test_KnowledgeGraph", names)
        self.assertNotIn("Other_KnowledgeGraph", names)


class FolderNameMismatchFalsePositiveTests(unittest.TestCase):
    """Regression: folder basename 'test_install' derives canonical prefix
    'TestInstall', but the project's configured KG_COLLECTION is
    'Test_KnowledgeGraph' (project name 'test'). 'Test' is a substring of
    'TestInstall', so _is_similar_prefix would mark the real in-use
    collection as a legacy candidate. Passing configured_canonical_names
    must suppress that false positive.
    """

    def test_configured_collection_not_flagged_as_legacy(self):
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                ["Test_KnowledgeGraph", "Test_Development"],
                counts={"Test_KnowledgeGraph": 0, "Test_Development": 0},
            ),
        ):
            result = project_init._detect_legacy_kg_collections(
                "test_install",
                URL,
                configured_canonical_names={
                    "Test_KnowledgeGraph", "Test_Development",
                },
            )
        self.assertEqual(
            result, [],
            "configured in-use collection must not be flagged as legacy",
        )

    def test_without_configured_names_still_false_positive(self):
        # Documents the pre-fix behaviour: without the configured-names
        # guard, the substring rule DOES (wrongly) flag the in-use class.
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                ["Test_KnowledgeGraph"],
                counts={"Test_KnowledgeGraph": 0},
            ),
        ):
            result = project_init._detect_legacy_kg_collections(
                "test_install", URL,
            )
        # 'Test' ⊂ 'TestInstall' → flagged. The guard (configured names)
        # is what suppresses this in the real install path.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["class_name"], "Test_KnowledgeGraph")

    def test_genuine_legacy_still_detected_with_configured_guard(self):
        # The guard must NOT over-suppress: a genuine legacy class (a
        # DIFFERENT name from the configured one) is still detected.
        with mock.patch.object(
            project_init, "_http_request",
            side_effect=_make_http_request_mock(
                ["Foo_KnowledgeGraph", "FooBar_KnowledgeGraph"],
                counts={"Foo_KnowledgeGraph": 50, "FooBar_KnowledgeGraph": 0},
            ),
        ):
            result = project_init._detect_legacy_kg_collections(
                "FooBar",
                URL,
                configured_canonical_names={"FooBar_KnowledgeGraph"},
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["class_name"], "Foo_KnowledgeGraph")


if __name__ == "__main__":
    unittest.main()
