"""Tests for vco_lib.project_init — single source of truth for project
init helpers (PR 2 of project-init/update overhaul).

Covers:
  - sanitize_for_weaviate_class — edge cases (empty, hyphenated, leading
    digit, all-punctuation, non-ASCII).
  - derive_project_collection_names — exact dict shape.
  - Back-compat with install.py path-based helpers.
  - CLI entry point (`python -m vco_lib.project_init derive`).
  - Schema definitions — named-vector slots + indexNullState invariant.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402  — back-compat surface
from vco_lib import project_init  # noqa: E402


class SanitizeForWeaviateClassTests(unittest.TestCase):
    """sanitize_for_weaviate_class — single sanitizer, replaces both Python
    `_derive_project_kg_name` and Rust `sanitize_kg_collection`.
    """

    def test_simple_lowercase(self):
        self.assertEqual(project_init.sanitize_for_weaviate_class("foo"), "Foo")

    def test_space_separated(self):
        # "foo bar" → "FooBar" (each part PascalCased, concatenated).
        self.assertEqual(project_init.sanitize_for_weaviate_class("foo bar"), "FooBar")

    def test_underscore_separated(self):
        # FOO_BAR — splits on `_`, PascalCases each: "FOO" → "FOO" (rest
        # preserved), "BAR" → "BAR". Result: "FOOBAR" (existing behavior;
        # the regex strips on `_` runs but the case-preserve rule keeps
        # uppercase tails intact).
        self.assertEqual(project_init.sanitize_for_weaviate_class("FOO_BAR"), "FOOBAR")

    def test_hyphen_separated(self):
        self.assertEqual(
            project_init.sanitize_for_weaviate_class("my-project"),
            "MyProject",
        )

    def test_leading_digit_falls_back(self):
        # Weaviate class names must start with a letter. A leading-digit
        # name like "123abc" produces "123abc" after split (digits not
        # stripped), which fails the .isalpha() guard → fallback "vct".
        self.assertEqual(project_init.sanitize_for_weaviate_class("123abc"), "vct")

    def test_empty_falls_back(self):
        self.assertEqual(project_init.sanitize_for_weaviate_class(""), "vct")

    def test_all_punctuation_falls_back(self):
        self.assertEqual(project_init.sanitize_for_weaviate_class("---"), "vct")

    def test_non_ascii_letters_are_stripped(self):
        # Policy decision (documented in module docstring): the regex
        # `[^A-Za-z0-9]+` matches anything NOT in ASCII A-Z/a-z/0-9, so
        # non-ASCII letters like `é` are treated as separators (stripped).
        # "étude" → split → ["tude"] → "Tude". This matches existing
        # install.py behavior exactly — PR 2 did not introduce new
        # normalization. If we ever want unicode-aware sanitization, that
        # would be a separate behavior change with migration plan.
        self.assertEqual(project_init.sanitize_for_weaviate_class("étude"), "Tude")

    def test_already_pascalcase_preserved(self):
        self.assertEqual(
            project_init.sanitize_for_weaviate_class("VideoFrames"),
            "VideoFrames",
        )


class DeriveProjectCollectionNamesTests(unittest.TestCase):
    """derive_project_collection_names — canonical dict shape."""

    def test_videoframes_exact_dict(self):
        result = project_init.derive_project_collection_names("VideoFrames")
        self.assertEqual(
            result,
            {
                "kg_collection": "VideoFrames_KnowledgeGraph",
                "development_collection": "VideoFrames_Development",
                # Phase 1.5 (Diagrams Integration, 2026-05-24): the
                # per-project diagrams collection is auto-paired with
                # the KG collection on derive. Same `<basename>_Diagrams`
                # convention as `_Development` / `_KnowledgeGraph`.
                "diagrams_collection": "VideoFrames_Diagrams",
                "project_name": "VideoFrames",
                # Canonical shared-KG name. v0.2.23 B1 (2026-05-21):
                # casing flipped to capital-C "VibeCoded" to match the
                # brand spelling (was lowercase-c "Vibecoded" v0.2.12–
                # v0.2.22, itself renamed from "VibeCodedTools_
                # KnowledgeGraph" in v0.2.12 PR-26 / Group E). Cross-
                # language invariant pinned by
                # tests/test_shared_kg_constant_consistency.py.
                "shared_kg_collection": "VibeCodedOrchestrator_KnowledgeGraph",
                "shared_kg_write_disabled": "false",
                "kg_basename": "VideoFrames",
                # v0.2.73 GAP-1: the 5 code-graph collection names, so a
                # consented project-unregister reclaims them too instead of
                # minting fresh orphans. Code prefix uses the underscore-
                # preserving canonical_class_prefix (VideoFrames has no
                # underscores, so it equals the KG basename here).
                "code_collections": [
                    "VideoFrames_CodeFunction",
                    "VideoFrames_CodeModule",
                    "VideoFrames_CodeClass",
                    "VideoFrames_CodeAPI",
                    "VideoFrames_CodeInteraction",
                ],
            },
        )

    def test_shared_kg_write_disabled_default_false(self):
        """The per-project shared-KG write gate defaults to 'false' (writes
        allowed). Spec'd as a string because the value is propagated through
        4 env surfaces verbatim — keep one canonical type."""
        for name in ("VideoFrames", "my-project", "Acme Corp"):
            self.assertEqual(
                project_init.derive_project_collection_names(name)[
                    "shared_kg_write_disabled"
                ],
                "false",
            )

    def test_project_name_is_raw_not_sanitized(self):
        # project_name field must preserve the user's input verbatim,
        # NOT the sanitized form. This is what 4 surfaces want for the
        # PROJECT_NAME env var.
        result = project_init.derive_project_collection_names("my-project")
        self.assertEqual(result["project_name"], "my-project")
        self.assertEqual(result["kg_basename"], "MyProject")
        self.assertEqual(result["kg_collection"], "MyProject_KnowledgeGraph")

    def test_dev_collection_uses_uppercase_d(self):
        # Drift bug B1 mitigation: "_Development" not "_development".
        result = project_init.derive_project_collection_names("anything")
        self.assertTrue(result["development_collection"].endswith("_Development"))

    def test_shared_kg_collection_constant(self):
        # The shared cross-project KG name is fixed (renamed in v0.2.12 PR-26;
        # cross-language invariant in tests/test_shared_kg_constant_consistency.py).
        for name in ("foo", "MyTest", "VideoFrames"):
            self.assertEqual(
                project_init.derive_project_collection_names(name)[
                    "shared_kg_collection"
                ],
                "VibeCodedOrchestrator_KnowledgeGraph",
            )


class BackCompatWithInstallPyTests(unittest.TestCase):
    """The path-based helpers in install.py must still produce the same
    output after the refactor — existing tests rely on this.
    """

    def test_kg_name_back_compat_mytest(self):
        # install.py's `_derive_project_kg_name(Path("/x/y/MyTest"))`
        # historically returned "MyTest_KnowledgeGraph". The new module
        # must match.
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/MyTest")),
            "MyTest_KnowledgeGraph",
        )
        # Same value via the public name-based API.
        self.assertEqual(
            project_init.derive_project_kg_name("MyTest"),
            "MyTest_KnowledgeGraph",
        )

    def test_kg_name_back_compat_hyphens(self):
        # The sanitize function splits on `-` and uppercases the first char of
        # each part, so "vibecoded-orchestrator" → "Vibecoded" + "Orchestrator"
        # = "VibecodedOrchestrator" (lowercase c in the middle — sanitize only
        # touches the leading char per part). This is INTENTIONALLY different
        # from the canonical shared-KG casing "VibeCodedOrchestrator" (capital
        # C, brand spelling) since v0.2.23 B1; the per-project derive function
        # has no special-case for "vibecoded-orchestrator".
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/vibecoded-orchestrator")),
            "VibecodedOrchestrator_KnowledgeGraph",
        )

    def test_dev_name_back_compat(self):
        self.assertEqual(
            install._derive_project_dev_name(Path("/x/y/MyTest")),
            "MyTest_Development",
        )

    def test_fallback_path_back_compat(self):
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/...")),
            "vct_KnowledgeGraph",
        )
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/1foo")),
            "vct_KnowledgeGraph",
        )

    def test_safe_class_re_pattern_unchanged(self):
        # Tests in test_install_shared_containers.py reach into
        # install._SAFE_CLASS_RE — keep the pattern stable.
        self.assertEqual(install._SAFE_CLASS_RE.pattern, r"[^A-Za-z0-9]+")
        self.assertIs(install._SAFE_CLASS_RE, project_init._SAFE_CLASS_RE)


class CliEntryPointTests(unittest.TestCase):
    """`python -m vco_lib.project_init derive --name <n> --json` — Rust
    subprocess interface.
    """

    def test_derive_json_output_parseable(self):
        result = subprocess.run(
            [sys.executable, "-m", "vco_lib.project_init",
             "derive", "--name", "VideoFrames", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, msg=f"stderr={result.stderr}")
        # Stdout must be parseable JSON (Rust does serde_json::from_str).
        payload = json.loads(result.stdout)
        self.assertEqual(payload["kg_collection"], "VideoFrames_KnowledgeGraph")
        self.assertEqual(payload["development_collection"], "VideoFrames_Development")
        self.assertEqual(payload["project_name"], "VideoFrames")
        self.assertEqual(payload["shared_kg_collection"], "VibeCodedOrchestrator_KnowledgeGraph")
        self.assertEqual(payload["shared_kg_write_disabled"], "false")
        self.assertEqual(payload["kg_basename"], "VideoFrames")

    def test_derive_human_readable_without_json(self):
        result = subprocess.run(
            [sys.executable, "-m", "vco_lib.project_init",
             "derive", "--name", "my-project"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        # Human form is `key=value` lines.
        self.assertIn("kg_collection=MyProject_KnowledgeGraph", result.stdout)
        self.assertIn("project_name=my-project", result.stdout)

    def test_derive_requires_name(self):
        result = subprocess.run(
            [sys.executable, "-m", "vco_lib.project_init", "derive", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        # argparse exits with code 2 on missing required arg.
        self.assertEqual(result.returncode, 2)


class SchemaDefinitionTests(unittest.TestCase):
    """Schema invariants required by detect_kg_schema_drift."""

    def test_kg_class_has_legacy_v0217_named_vectors(self):
        """Legacy v0.2.17 trio must remain present in the KG definition.

        v0.2.18 adds 2 more slots on top (arctic2_embed +
        openai_text_embed) but the legacy trio stays — that's the
        data-preservation invariant. Tested separately at
        `test_kg_class_has_v0218_named_vectors` below.
        """
        schema = project_init.kg_class_definition("Foo")
        vec_config = schema["vectorConfig"]
        self.assertIn("qwen3_embed", vec_config)
        self.assertIn("ollama_embed", vec_config)
        self.assertIn("openai_embed", vec_config)
        for slot in ("qwen3_embed", "ollama_embed", "openai_embed"):
            self.assertEqual(vec_config[slot]["vectorIndexType"], "hnsw")

    def test_kg_class_has_v0218_named_vectors(self):
        """v0.2.18: KG class adds arctic2_embed + openai_text_embed to
        the legacy v0.2.17 trio. See `vco_lib.weaviate_schema` for the
        full catalog + rationale.
        """
        schema = project_init.kg_class_definition("Foo")
        vec_config = schema["vectorConfig"]
        self.assertEqual(
            set(vec_config.keys()),
            {
                "qwen3_embed",
                "ollama_embed",
                "openai_embed",
                "arctic2_embed",
                "openai_text_embed",
            },
        )
        for slot in vec_config:
            self.assertEqual(vec_config[slot]["vectorIndexType"], "hnsw")

    def test_kg_class_has_index_null_state(self):
        # PR 2 surprise: existing install.py schema definitions did NOT
        # set indexNullState=True even though the drift detector
        # required it. Adding it here closes that loop on fresh installs.
        schema = project_init.kg_class_definition("Foo")
        self.assertEqual(
            schema["invertedIndexConfig"],
            {"indexNullState": True},
        )

    def test_development_class_has_v0218_named_vectors(self):
        """v0.2.18: Dev (and KG) class definitions ship with the 5-slot
        catalog from `vco_lib.weaviate_schema.KG_NAMED_VECTORS`.

        Pre-v0.2.18 this was 3 slots (qwen3 + ollama + openai). v0.2.18
        retains the 3 legacy slots for data-preservation and adds
        arctic2_embed + openai_text_embed for the new EmbeddingService
        targets.
        """
        schema = project_init.development_class_definition("FooDev")
        vec_config = schema["vectorConfig"]
        self.assertEqual(
            set(vec_config.keys()),
            {
                "qwen3_embed",
                "ollama_embed",
                "openai_embed",
                "arctic2_embed",
                "openai_text_embed",
            },
        )

    def test_development_class_has_index_null_state(self):
        schema = project_init.development_class_definition("FooDev")
        self.assertEqual(
            schema["invertedIndexConfig"],
            {"indexNullState": True},
        )

    def test_kg_class_required_properties_present(self):
        schema = project_init.kg_class_definition("Foo")
        prop_names = {p["name"] for p in schema["properties"]}
        # Spot-check the load-bearing properties (full set is fine to
        # add later; we mainly want regression detection on names).
        self.assertIn("title", prop_names)
        self.assertIn("content", prop_names)
        self.assertIn("file_path", prop_names)
        self.assertIn("typed_links", prop_names)
        self.assertIn("status", prop_names)

    def test_development_class_required_properties_present(self):
        # PR-24 (2026-05-16): Development schema gained the four
        # canonical temporal properties so the MCP `_stale_filter`
        # (valid_until is_none(True) | valid_until > now) doesn't fail
        # with "no such prop with name 'valid_until'" on Development
        # collections. Names + dataType must mirror the KG schema.
        schema = project_init.development_class_definition("FooDev")
        props = {p["name"]: p for p in schema["properties"]}
        # Base content fields preserved.
        self.assertIn("title", props)
        self.assertIn("content", props)
        self.assertIn("file_path", props)
        # New temporal fields present with date dataType.
        for prop_name in ("created", "updated", "valid_from", "valid_until"):
            self.assertIn(prop_name, props,
                          f"Development schema missing temporal '{prop_name}'")
            self.assertEqual(
                props[prop_name]["dataType"], ["date"],
                f"Development.{prop_name} must be dataType=['date'] "
                f"to align with KG schema (was {props[prop_name]['dataType']!r})",
            )

    def test_development_temporal_props_use_date_dataType(self):
        # The KG class definition does NOT statically declare temporal
        # properties — they are added at sync time by
        # `.claude/scripts/add_temporal_metadata.py`. The Development
        # class DOES declare them statically (Development docs don't go
        # through the per-node frontmatter path). The shared invariant
        # is that whenever temporal props ARE present, they use the
        # `date` dataType, matching what MCP filters expect.
        dev = project_init.development_class_definition("FooDev")
        dev_props = {p["name"]: p["dataType"] for p in dev["properties"]}
        for prop_name in ("created", "updated", "valid_from", "valid_until"):
            self.assertEqual(
                dev_props.get(prop_name), ["date"],
                f"Development.{prop_name} must be ['date'] (got "
                f"{dev_props.get(prop_name)!r}); MCP `_stale_filter` "
                f"requires date-typed properties for is_none/comparison."
            )

    def test_kg_class_name_threaded_through(self):
        # The `class` field carries the caller-supplied name verbatim.
        self.assertEqual(
            project_init.kg_class_definition("MyTest_KnowledgeGraph")["class"],
            "MyTest_KnowledgeGraph",
        )

    def test_install_py_shims_match_module(self):
        # The install.py shims must produce exactly the same dict.
        self.assertEqual(
            install._kg_class_definition("Foo"),
            project_init.kg_class_definition("Foo"),
        )
        self.assertEqual(
            install._development_class_definition("FooDev"),
            project_init.development_class_definition("FooDev"),
        )


class SchemaIncompatibleTests(unittest.TestCase):
    """Bug-1 v0.2.4 (2026-05-12): _schema_incompatible — detect pre-existing
    collections whose schema diverges from the current spec in non-additive
    ways. Drives the bootstrap-collections regen path."""

    def _at_target(self) -> dict:
        return project_init.kg_class_definition("ClaudeKnowledgeGraph")

    def test_target_schema_is_compatible(self):
        actual = self._at_target()
        incompatible, reason = project_init._schema_incompatible(
            actual, project_init.kg_class_definition, "ClaudeKnowledgeGraph",
        )
        self.assertFalse(incompatible, msg=f"unexpected: {reason}")
        self.assertEqual(reason, "")

    def test_legacy_single_vector_is_incompatible(self):
        # ArcAgi-style: no vectorConfig at all.
        actual = {
            "class": "ClaudeKnowledgeGraph",
            "invertedIndexConfig": {"indexNullState": False},
            "properties": [],
        }
        incompatible, reason = project_init._schema_incompatible(
            actual, project_init.kg_class_definition, "ClaudeKnowledgeGraph",
        )
        self.assertTrue(incompatible)
        self.assertIn("single-vector", reason)

    def test_missing_named_vector_slot_is_incompatible(self):
        # MyProject_KnowledgeGraph case: pre-2026-04 schema has ollama_embed +
        # qwen3_embed but missing openai_embed slot. New code's
        # sync_knowledge_graph.py writes a single named vector and Weaviate
        # rejects mismatched slot sets with HTTP 422.
        actual = self._at_target()
        del actual["vectorConfig"]["openai_embed"]
        incompatible, reason = project_init._schema_incompatible(
            actual, project_init.kg_class_definition, "ClaudeKnowledgeGraph",
        )
        self.assertTrue(incompatible)
        self.assertIn("named-vector mismatch", reason)
        self.assertIn("openai_embed", reason)

    def test_extra_named_vector_slot_is_tolerated_v0218(self):
        """v0.2.18: extra slots in `actual` no longer flag as incompatible.

        Pre-v0.2.18 the rule was "any slot-set mismatch (missing OR
        extra) -> REGEN". v0.2.18 narrows this to "missing CORE
        legacy-v0.2.17 slots -> REGEN; extras are tolerated".

        Rationale: data preservation > schema strictness. A future
        catalog rev that drops legacy slots from the target shouldn't
        retroactively flag every existing collection as needing a
        destructive rebuild.
        """
        actual = self._at_target()
        actual["vectorConfig"]["snowflake_legacy"] = {
            "vectorizer": {"none": {}}, "vectorIndexType": "hnsw",
        }
        incompatible, reason = project_init._schema_incompatible(
            actual, project_init.kg_class_definition, "ClaudeKnowledgeGraph",
        )
        # No longer flagged: extra slots are benign in v0.2.18.
        self.assertFalse(incompatible, msg=f"unexpected REGEN: {reason}")

    def test_missing_index_null_state_is_incompatible(self):
        actual = self._at_target()
        actual["invertedIndexConfig"] = {"indexNullState": False}
        incompatible, reason = project_init._schema_incompatible(
            actual, project_init.kg_class_definition, "ClaudeKnowledgeGraph",
        )
        self.assertTrue(incompatible)
        self.assertIn("indexNullState", reason)

    def test_additive_property_drift_alone_is_compatible(self):
        # Properties missing from actual are not a regen trigger — the
        # smart migrate's patch_props branch fixes those in-place, and
        # the bootstrap-then-sync path re-ingests anyway. Reserve regen
        # for the changes that genuinely cannot be patched.
        actual = self._at_target()
        actual["properties"] = actual["properties"][:-2]  # drop last 2
        incompatible, reason = project_init._schema_incompatible(
            actual, project_init.kg_class_definition, "ClaudeKnowledgeGraph",
        )
        self.assertFalse(incompatible, msg=f"unexpected: {reason}")


class ExtractSimilarClassNameTests(unittest.TestCase):
    """Bug-1 v0.2.4 (2026-05-12): _extract_similar_class_name — recover the
    actual case from Weaviate's `found similar class "X"` HTTP 422 response
    so we can drop the old-cased collection."""

    def test_extracts_from_canonical_422(self):
        body = (
            'POST /v1/schema (MyProject_Development) → HTTP 422: '
            '{"error":[{"message":"class already exists: found similar '
            'class \\"MyProject_development\\""}]}'
        )
        self.assertEqual(
            project_init._extract_similar_class_name(body),
            "MyProject_development",
        )

    def test_extracts_with_dotted_names(self):
        body = 'found similar class "my_old.Project_KnowledgeGraph"'
        self.assertEqual(
            project_init._extract_similar_class_name(body),
            "my_old.Project_KnowledgeGraph",
        )

    def test_extracts_from_bytes_repr_wrapped_422(self):
        """The shape Weaviate's Python SDK actually produces in the wild:
        the response BYTES are wrapped in a `b'...'` repr and embedded in
        a RuntimeError string. That means the inner JSON's `\\"` escape
        becomes `\\\\"` in the final str — TWO backslashes before the quote.

        Regression test for v0.2.4 first ship: the single-backslash regex
        matched the canonical_422 test above but missed the bytes-repr
        form, so case-conflict recovery silently failed for the real bug
        it was written to fix. Lesson: tests should match the EXACT
        error string produced by the runtime, not the simplified form.
        """
        body = (
            "RuntimeError: POST /v1/schema (MyProject_Development) → HTTP 422: "
            "b'{\"error\":[{\"message\":\"class already exists: found similar "
            "class \\\\\"MyProject_development\\\\\"\"}]}\\n'"
        )
        self.assertEqual(
            project_init._extract_similar_class_name(body),
            "MyProject_development",
        )

    def test_returns_none_on_unrelated_message(self):
        self.assertIsNone(
            project_init._extract_similar_class_name(
                "POST /v1/schema (Foo) → HTTP 500: server exploded",
            ),
        )

    def test_returns_none_on_empty(self):
        self.assertIsNone(project_init._extract_similar_class_name(""))
        self.assertIsNone(project_init._extract_similar_class_name(None))  # type: ignore[arg-type]


class RegenReasonTagTests(unittest.TestCase):
    """Bug-1 v0.2.4 (2026-05-12): _regen_reason_tag — keep the JSON
    envelope's ``reason`` field finite for UI banner text."""

    def test_case_conflict(self):
        self.assertEqual(
            project_init._regen_reason_tag(
                "case-only name conflict ('foo' → 'Foo')",
            ),
            "case-conflict",
        )

    def test_legacy_single_vector(self):
        self.assertEqual(
            project_init._regen_reason_tag(
                "legacy single-vector schema (no vectorConfig)",
            ),
            "legacy-single-vector",
        )

    def test_multi_vector(self):
        self.assertEqual(
            project_init._regen_reason_tag(
                "named-vector mismatch (missing slots: openai_embed)",
            ),
            "multi-vector",
        )

    def test_index_null_state(self):
        self.assertEqual(
            project_init._regen_reason_tag(
                "indexNullState=True required but not set",
            ),
            "index-null-state",
        )

    def test_unknown_falls_back(self):
        self.assertEqual(
            project_init._regen_reason_tag("something arbitrary"),
            "schema-mismatch",
        )


class BootstrapCollectionsRegenTests(unittest.TestCase):
    """Bug-1 v0.2.4 (2026-05-12): bootstrap_collections schema-regen and
    case-conflict paths. Drives the JSON envelope with monkey-patched
    HTTP helpers so no live Weaviate is required."""

    def setUp(self):
        # Save originals; per-test patches are applied via mock.patch.
        self._orig_reachable = project_init._is_weaviate_reachable
        # Always pretend Weaviate is up so we exercise the iteration loop.
        project_init._is_weaviate_reachable = lambda url, timeout=5.0: True

    def tearDown(self):
        project_init._is_weaviate_reachable = self._orig_reachable

    def test_existing_compatible_collection_marked_as_exists(self):
        target = project_init.kg_class_definition("VideoFrames_KnowledgeGraph")
        # Fetcher returns the target schema verbatim for every name → all
        # collections are at-target.
        with mock.patch.object(project_init, "_fetch_schema", return_value=target):
            with mock.patch.object(project_init, "_create_class"):
                with mock.patch.object(project_init, "_delete_class"):
                    result = project_init.bootstrap_collections("VideoFrames")
        actions = {a["collection"]: a for a in result["actions"]}
        # KG, Dev, Shared all "exists" (the same compatible target is returned
        # for every fetch).
        self.assertTrue(all(a["action"] == "exists" and a["ok"] for a in actions.values()),
                        msg=f"actions: {actions}")
        self.assertEqual(result["regenerated"], [])
        self.assertEqual(result["errors"], [])

    def test_existing_incompatible_collection_triggers_regen(self):
        # Legacy single-vector schema → regen.
        legacy = {
            "class": "VideoFrames_KnowledgeGraph",
            "invertedIndexConfig": {"indexNullState": False},
            "properties": [],
        }
        # Fetcher returns the legacy schema only for the KG; the other
        # two collections come back as None (don't exist) so they
        # follow the create path.
        def fetcher(name, weaviate_url=None):
            if name == "VideoFrames_KnowledgeGraph":
                return legacy
            return None

        with mock.patch.object(project_init, "_fetch_schema", side_effect=fetcher):
            with mock.patch.object(project_init, "_create_class") as create_mock:
                with mock.patch.object(project_init, "_delete_class") as delete_mock:
                    with mock.patch.object(
                        project_init, "_snapshot_collection_for_rebuild",
                        return_value={"object_count": 42, "sample_uuids": []},
                    ):
                        result = project_init.bootstrap_collections("VideoFrames")

        # Regen surfaces in the envelope.
        regens = {r["collection"]: r for r in result["regenerated"]}
        self.assertIn("VideoFrames_KnowledgeGraph", regens)
        self.assertEqual(regens["VideoFrames_KnowledgeGraph"]["reason"],
                         "legacy-single-vector")
        # The dropped name matches the canonical (not a case-conflict).
        self.assertEqual(regens["VideoFrames_KnowledgeGraph"]["dropped_name"],
                         "VideoFrames_KnowledgeGraph")
        # delete_class invoked for the regenerated collection.
        delete_calls = [c.args[0] for c in delete_mock.call_args_list]
        self.assertIn("VideoFrames_KnowledgeGraph", delete_calls)
        # create_class invoked at least for the regen + the 2 missing.
        create_calls = [c.args[0]["class"] for c in create_mock.call_args_list]
        self.assertIn("VideoFrames_KnowledgeGraph", create_calls)
        self.assertEqual(result["errors"], [])

    def test_case_only_conflict_drops_old_and_creates_new(self):
        # MyProject case: existence-probe says None (no exact-name match),
        # but POST returns 422 "found similar class 'MyProject_development'".
        # Recovery: drop the old-cased class, recreate with the target name.
        target_kg = project_init.kg_class_definition("MyProject_KnowledgeGraph")
        # Fetcher: KG already exists at-target so it's marked exists; Dev
        # doesn't exist (None); Shared also exists at-target.
        def fetcher(name, weaviate_url=None):
            if name == "MyProject_KnowledgeGraph":
                return target_kg
            if name == "MyProject_Development":
                return None  # We pretend it's missing, then 422 on POST.
            # Shared KG exists at-target.
            if name == project_init._SHARED_KG_NAME:
                return project_init.kg_class_definition(project_init._SHARED_KG_NAME)
            return None

        # First call to _create_class for MyProject_Development raises a
        # "similar class" error; the recovery call for MyProject_development
        # then succeeds.
        create_calls: list[str] = []
        def create_side_effect(payload, weaviate_url=None):
            create_calls.append(payload["class"])
            if payload["class"] == "MyProject_Development" and len(create_calls) == 1:
                raise RuntimeError(
                    'POST /v1/schema (MyProject_Development) → HTTP 422: '
                    '{"error":[{"message":"class already exists: found '
                    'similar class \\"MyProject_development\\""}]}'
                )
            # Subsequent calls (recovery + others) succeed silently.
            return None

        delete_targets: list[str] = []
        def delete_side_effect(name, weaviate_url=None):
            delete_targets.append(name)

        with mock.patch.object(project_init, "_fetch_schema", side_effect=fetcher):
            with mock.patch.object(project_init, "_create_class",
                                   side_effect=create_side_effect):
                with mock.patch.object(project_init, "_delete_class",
                                       side_effect=delete_side_effect):
                    with mock.patch.object(
                        project_init, "_snapshot_collection_for_rebuild",
                        return_value={"object_count": 0, "sample_uuids": []},
                    ):
                        result = project_init.bootstrap_collections("MyProject")

        # Regen envelope flags case-conflict with the lowercase old name.
        regens = {r["collection"]: r for r in result["regenerated"]}
        self.assertIn("MyProject_Development", regens)
        self.assertEqual(regens["MyProject_Development"]["reason"], "case-conflict")
        self.assertEqual(regens["MyProject_Development"]["dropped_name"],
                         "MyProject_development")
        # The lowercase collection got dropped during recovery.
        self.assertIn("MyProject_development", delete_targets)
        # create_class was retried with the target name after the drop.
        # The first attempt failed with 422; the second (post-drop) succeeded.
        self.assertGreaterEqual(create_calls.count("MyProject_Development"), 2)
        self.assertEqual(result["errors"], [])

    def test_dry_run_reports_would_regenerate_without_mutating(self):
        legacy = {
            "class": "VideoFrames_KnowledgeGraph",
            "invertedIndexConfig": {"indexNullState": False},
            "properties": [],
        }
        def fetcher(name, weaviate_url=None):
            if name == "VideoFrames_KnowledgeGraph":
                return legacy
            return None

        with mock.patch.object(project_init, "_fetch_schema", side_effect=fetcher):
            with mock.patch.object(project_init, "_create_class") as create_mock:
                with mock.patch.object(project_init, "_delete_class") as delete_mock:
                    result = project_init.bootstrap_collections(
                        "VideoFrames", dry_run=True,
                    )

        # Dry-run: regen surfaced as would-regenerate; no destructive ops.
        actions = [a for a in result["actions"]
                   if a["collection"] == "VideoFrames_KnowledgeGraph"]
        self.assertEqual(actions[0]["action"], "would-regenerate")
        # Nothing mutated.
        delete_mock.assert_not_called()
        create_mock.assert_not_called()
        # Regen entry still recorded so the caller can preview.
        regens = {r["collection"]: r for r in result["regenerated"]}
        self.assertIn("VideoFrames_KnowledgeGraph", regens)


class DetectAndRenameLegacyComposeOverrideTests(unittest.TestCase):
    """PR-22 (v0.2.12, 2026-05-16): legacy `docker-compose.override.yml`
    rename helper exercised at `install.py --update` time.

    Covers:
      - No legacy file → returns None (silent no-op).
      - Legacy file present in `infrastructure/` → renamed, deferral emitted.
      - Legacy file present in `claude_mcp_servers/` → renamed.
      - Both legacy and canonical present → no rename, conflict deferral.
      - Mixed: rename one location, conflict in the other.
      - Permission failure → deferral with severity=warning, no raise.
    """

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="pr22-rename-test-")
        self.install_root = Path(self._tmp)
        (self.install_root / "infrastructure").mkdir()
        (self.install_root / "claude_mcp_servers").mkdir()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _read_deferral_ids(self) -> list[str]:
        """Read the deferral report and return condition_ids in order."""
        from vco_lib.deferral_report import DeferralReport
        report = DeferralReport.read(self.install_root)
        return [e.condition_id for e in report.entries]

    def test_no_legacy_returns_none(self):
        """Empty tree → returns None, no deferral emitted."""
        result = project_init._detect_and_rename_legacy_compose_override(
            self.install_root,
        )
        self.assertIsNone(result)
        # No deferral report should have been created either.
        self.assertEqual(self._read_deferral_ids(), [])

    def test_legacy_in_infrastructure_renamed(self):
        """Legacy file under `infrastructure/` is renamed in place,
        deferral entry recorded with condition_id=compose_override_renamed."""
        legacy = self.install_root / "infrastructure" / "docker-compose.override.yml"
        legacy.write_text("services: {}\n")

        result = project_init._detect_and_rename_legacy_compose_override(
            self.install_root,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "renamed")
        self.assertEqual(len(result["renamed"]), 1)
        # Legacy gone, canonical present.
        self.assertFalse(legacy.exists())
        canonical = self.install_root / "infrastructure" / "compose.override.yaml"
        self.assertTrue(canonical.exists())
        self.assertEqual(canonical.read_text(), "services: {}\n")
        # Deferral emitted.
        self.assertIn("compose_override_renamed", self._read_deferral_ids())

    def test_legacy_in_claude_mcp_servers_renamed(self):
        """Legacy file under `claude_mcp_servers/` (alternate location
        some users have) is also renamed."""
        legacy = self.install_root / "claude_mcp_servers" / "docker-compose.override.yml"
        legacy.write_text("services:\n  ollama:\n    image: ollama/ollama\n")

        result = project_init._detect_and_rename_legacy_compose_override(
            self.install_root,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "renamed")
        canonical = self.install_root / "claude_mcp_servers" / "compose.override.yaml"
        self.assertTrue(canonical.exists())
        self.assertFalse(legacy.exists())

    def test_both_present_conflict_no_rename(self):
        """When BOTH legacy and canonical exist in the same directory,
        the function emits a conflict deferral and does NOT rename
        (the files may have diverged)."""
        legacy = self.install_root / "infrastructure" / "docker-compose.override.yml"
        canonical = self.install_root / "infrastructure" / "compose.override.yaml"
        legacy.write_text("# legacy\n")
        canonical.write_text("# canonical\n")

        result = project_init._detect_and_rename_legacy_compose_override(
            self.install_root,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "conflict")
        # Both files survive.
        self.assertTrue(legacy.exists())
        self.assertTrue(canonical.exists())
        self.assertEqual(legacy.read_text(), "# legacy\n")
        self.assertEqual(canonical.read_text(), "# canonical\n")
        self.assertIn(
            "compose_override_filename_conflict",
            self._read_deferral_ids(),
        )

    def test_idempotent_after_rename(self):
        """Second call on a tree with no legacy files returns None."""
        legacy = self.install_root / "infrastructure" / "docker-compose.override.yml"
        legacy.write_text("services: {}\n")
        project_init._detect_and_rename_legacy_compose_override(self.install_root)
        # Second pass: no-op.
        result = project_init._detect_and_rename_legacy_compose_override(
            self.install_root,
        )
        self.assertIsNone(result)

    def test_mixed_rename_and_conflict(self):
        """One legacy in `infrastructure/` (renamed), one legacy+canonical
        pair in `claude_mcp_servers/` (conflict). Both deferral entries
        should be emitted."""
        # Will be renamed.
        (self.install_root / "infrastructure" / "docker-compose.override.yml"
         ).write_text("# infra legacy\n")
        # Conflict pair.
        (self.install_root / "claude_mcp_servers" / "docker-compose.override.yml"
         ).write_text("# mcp legacy\n")
        (self.install_root / "claude_mcp_servers" / "compose.override.yaml"
         ).write_text("# mcp canonical\n")

        result = project_init._detect_and_rename_legacy_compose_override(
            self.install_root,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "mixed")
        self.assertEqual(len(result["renamed"]), 1)
        self.assertEqual(len(result["conflicts"]), 1)

        ids = self._read_deferral_ids()
        self.assertIn("compose_override_renamed", ids)
        self.assertIn("compose_override_filename_conflict", ids)

    def test_legacy_outside_known_dirs_ignored(self):
        """Legacy files in unrelated subdirs should not be touched —
        only `infrastructure/` and `claude_mcp_servers/` are scanned."""
        (self.install_root / "other").mkdir()
        unrelated = self.install_root / "other" / "docker-compose.override.yml"
        unrelated.write_text("# unrelated\n")

        result = project_init._detect_and_rename_legacy_compose_override(
            self.install_root,
        )
        self.assertIsNone(result)
        self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
