# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for PR-41: _classify_weaviate_failure() refined classification.

Covers Issues A + F from
.claude/context/mcp-instability-vs-public-repo-2026-05-16.md:

- Issue A: schema-cache-stale not detected → manual `pkill -f
  weaviate_mcp` needed after schema migrations. Fix: schema-shaped
  errors now classify as WeaviateSchemaError, callers reset the
  cached client on that branch.

- Issue F: false-positive WeaviateUnreachable misclassification of
  schema/auth errors. Fix: detection order is now schema → auth →
  connection, with each class carrying a targeted recovery hint.

The classifier must:
  - Return WeaviateUnreachable for actual connection-class signals
    (connection refused, unavailable, failed to connect, grpc).
  - Return WeaviateSchemaError for schema-shaped messages with hints
    pointing at the right migration script.
  - Return WeaviateAuthError for auth-shaped messages (401, 403,
    invalid api key) WITHOUT cache-reset hints.
  - Pass-through (None) for generic payload errors / real bugs.
  - Preserve loud-fail-v2 behaviour for legitimate connection
    failures (the existing patterns must still match).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import claude_mcp_servers.weaviate_mcp.server as srv  # noqa: E402

# v0.2.92 hint-safety fakes live with the primary suite for that fix (repo
# convention: see tests/test_install_bundle.py's _make_fake_orchestrator,
# cross-imported by four other test modules).
from tests.test_v0292_mcp_query_structure import (  # noqa: E402
    DESTRUCTIVE_MARKERS,
    FAKE_SCHEMA_CLASSES,
    _FakeSchemaClient,
    _nested_query_error,
)

_classify = srv._classify_weaviate_failure
WeaviateUnreachable = srv.WeaviateUnreachable
WeaviateSchemaError = srv.WeaviateSchemaError
WeaviateAuthError = srv.WeaviateAuthError


class ConnectionUnreachableTests(unittest.TestCase):
    """Preserve loud-fail-v2 behaviour for actual outages."""

    def test_connection_refused_string(self):
        result = _classify(Exception("connection refused"))
        self.assertIsInstance(result, WeaviateUnreachable)

    def test_failed_to_connect_string(self):
        result = _classify(Exception("Failed to connect to Weaviate at :8081"))
        self.assertIsInstance(result, WeaviateUnreachable)

    def test_unavailable_grpc_string(self):
        result = _classify(Exception("UNAVAILABLE: grpc transport closed"))
        self.assertIsInstance(result, WeaviateUnreachable)

    def test_weaviate_connection_error_class(self):
        """weaviate.exceptions.WeaviateConnectionError → Unreachable."""
        try:
            from weaviate.exceptions import WeaviateConnectionError
        except ImportError:
            self.skipTest("weaviate-client not installed")
        exc = WeaviateConnectionError("Could not connect to gRPC port")
        result = _classify(exc)
        self.assertIsInstance(result, WeaviateUnreachable)

    def test_weaviate_grpc_unavailable_class(self):
        try:
            from weaviate.exceptions import WeaviateGRPCUnavailableError
        except ImportError:
            self.skipTest("weaviate-client not installed")
        exc = WeaviateGRPCUnavailableError("gRPC server is down")
        result = _classify(exc)
        self.assertIsInstance(result, WeaviateUnreachable)


class SchemaErrorTests(unittest.TestCase):
    """PR-41 Issue A: schema-shaped errors get their own class + hint."""

    def test_class_not_found(self):
        result = _classify(
            Exception("could not find class VCODev_KnowledgeGraph in schema")
        )
        self.assertIsInstance(result, WeaviateSchemaError)
        # Hint should point at install.py --update OR the launcher picker
        hint = result.user_msg.lower()
        self.assertTrue(
            "install.py --update" in hint or "manage shared kg collection" in hint,
            f"hint missing migration pointer: {result.user_msg}",
        )

    def test_class_not_found_alt_phrasing(self):
        result = _classify(Exception("class not found: VCODev_Development"))
        self.assertIsInstance(result, WeaviateSchemaError)

    def test_no_such_prop_valid_until(self):
        """Issue C symptom: Development missing valid_until property."""
        result = _classify(
            Exception(
                "no such prop with name 'valid_until' found in class "
                "'VCODev_Development' in the schema"
            )
        )
        self.assertIsInstance(result, WeaviateSchemaError)
        self.assertIn(
            "migrate-development-temporal-props.sh",
            result.user_msg,
            f"hint missing dev migration script: {result.user_msg}",
        )

    def test_no_such_property_phrasing(self):
        result = _classify(
            Exception("no such property 'valid_from' on class 'VCODev_Development'")
        )
        self.assertIsInstance(result, WeaviateSchemaError)
        self.assertIn("migrate-development-temporal-props.sh", result.user_msg)

    def test_nested_query_index_null_state(self):
        """Issue D symptom: collection lacks indexNullState=True.

        CLASSIFICATION only. Until v0.2.92 this test also asserted that the
        hint recommended ``migrate-shared-kg-schema.sh``; that assertion
        pinned a data-loss bug and was moved (not deleted) into
        :class:`IndexNullStateHintSafetyTests` below, which pins the
        verified-premise contract that replaced it. Do not restore the old
        assertion here — see that class's docstring for why.
        """
        result = _classify(
            Exception(
                "build inverted filter allow list: fetch doc ids for "
                "prop/value pair: nested query: schema not configured"
            )
        )
        self.assertIsInstance(result, WeaviateSchemaError)

    def test_build_inverted_filter_alone(self):
        result = _classify(Exception("build inverted filter allow list: failed"))
        self.assertIsInstance(result, WeaviateSchemaError)

    def test_nested_query_alone(self):
        result = _classify(Exception("nested query error during is_null filter"))
        self.assertIsInstance(result, WeaviateSchemaError)

    def test_schema_error_takes_precedence_over_query_error_class(self):
        """A WeaviateQueryError whose message looks like schema must be
        classified as Schema, not as Unreachable (the bug behind Issue F).
        """
        try:
            from weaviate.exceptions import WeaviateQueryError
        except ImportError:
            self.skipTest("weaviate-client not installed")
        exc = WeaviateQueryError(
            "could not find class VibeCodedOrchestrator_KnowledgeGraph "
            "in schema",
            "GRPC search",
        )
        result = _classify(exc)
        self.assertIsInstance(
            result,
            WeaviateSchemaError,
            f"schema-shaped WeaviateQueryError must be Schema not Unreachable; "
            f"got {type(result).__name__}",
        )


class AuthErrorTests(unittest.TestCase):
    """PR-41 Issue F: auth errors get their own class, NO cache reset."""

    def test_401_unauthorized(self):
        result = _classify(Exception("401 Unauthorized: invalid token"))
        self.assertIsInstance(result, WeaviateAuthError)

    def test_403_forbidden(self):
        result = _classify(Exception("403 Forbidden: insufficient permissions"))
        self.assertIsInstance(result, WeaviateAuthError)

    def test_invalid_api_key(self):
        result = _classify(Exception("invalid api key supplied"))
        self.assertIsInstance(result, WeaviateAuthError)

    def test_auth_error_hint_mentions_api_key_setting(self):
        """The hint should point at WEAVIATE_API_KEY in settings,
        NOT at container restart commands.
        """
        result = _classify(Exception("401 Unauthorized"))
        self.assertIsInstance(result, WeaviateAuthError)
        self.assertIn("WEAVIATE_API_KEY", result.user_msg)
        # Must NOT contain unreachable hints
        self.assertNotIn(
            "podman rm",
            result.user_msg.lower(),
            "auth-error hint must not suggest container restart",
        )


class PassThroughTests(unittest.TestCase):
    """Generic payload errors / real bugs must NOT be wrapped."""

    def test_generic_value_error(self):
        result = _classify(ValueError("payload too large"))
        self.assertIsNone(
            result,
            "generic ValueError must pass through (None) rather than be "
            "wrapped as WeaviateUnreachable (the Issue F bug)",
        )

    def test_generic_query_error_passes_through(self):
        """WeaviateQueryError without schema/auth/connection shape →
        pass-through. This is the key Issue F regression test: the old
        classifier wrapped EVERY WeaviateQueryError as Unreachable.
        """
        try:
            from weaviate.exceptions import WeaviateQueryError
        except ImportError:
            self.skipTest("weaviate-client not installed")
        exc = WeaviateQueryError(
            "Vector dimension mismatch: expected 1024, got 1536",
            "GRPC search",
        )
        result = _classify(exc)
        self.assertIsNone(
            result,
            "WeaviateQueryError with a real query bug must NOT be wrapped "
            "as WeaviateUnreachable",
        )

    def test_random_exception_passes_through(self):
        result = _classify(RuntimeError("internal MCP bug"))
        self.assertIsNone(result)


class IdempotencyTests(unittest.TestCase):
    """Passing an already-classified exception in returns it as-is."""

    def test_weaviate_unreachable_passthrough(self):
        original = WeaviateUnreachable("connection refused", "hint")
        result = _classify(original)
        self.assertIs(result, original)

    def test_weaviate_schema_error_passthrough(self):
        original = WeaviateSchemaError("could not find class X", "hint")
        result = _classify(original)
        self.assertIs(result, original)

    def test_weaviate_auth_error_passthrough(self):
        original = WeaviateAuthError("401", "hint")
        result = _classify(original)
        self.assertIs(result, original)


class DetectionOrderTests(unittest.TestCase):
    """Order: schema → auth → connection. Schema must win when message
    has overlapping signals (a regression here is the Issue F bug).
    """

    def test_schema_wins_over_grpc_keyword(self):
        """Some Weaviate error strings include "gRPC" as a transport
        prefix even when the actual problem is schema. Schema patterns
        must still win.
        """
        exc = Exception(
            "gRPC error in search: could not find class FooBar in schema"
        )
        result = _classify(exc)
        self.assertIsInstance(
            result,
            WeaviateSchemaError,
            "schema patterns must match before connection patterns",
        )


class HintBuildersTests(unittest.TestCase):
    """Confirm the structured response helpers exist and emit the right
    error_class string (downstream agents may parse it).
    """

    def test_schema_response_helper_exists(self):
        self.assertTrue(hasattr(srv, "_weaviate_schema_error_response"))

    def test_auth_response_helper_exists(self):
        self.assertTrue(hasattr(srv, "_weaviate_auth_error_response"))

    def test_schema_response_emits_class(self):
        import json
        exc = WeaviateSchemaError("could not find class X", "hint")
        body = srv._weaviate_schema_error_response(exc, query="test")
        data = json.loads(body)
        self.assertFalse(data["success"])
        self.assertEqual(data["error_class"], "WeaviateSchemaError")
        self.assertEqual(data["query"], "test")
        self.assertEqual(data["hint"], "hint")

    def test_auth_response_emits_class(self):
        import json
        exc = WeaviateAuthError("401", "check key")
        body = srv._weaviate_auth_error_response(exc, query="test")
        data = json.loads(body)
        self.assertFalse(data["success"])
        self.assertEqual(data["error_class"], "WeaviateAuthError")
        self.assertEqual(data["hint"], "check key")


class IndexNullStateHintSafetyTests(unittest.TestCase):
    """v0.2.92 — the indexNullState hint must VERIFY before it recommends.

    WHY THE OLD ASSERTIONS ARE GONE. Until v0.2.92 three tests in
    :class:`SchemaErrorTests` asserted that ANY message containing "nested
    query" or "build inverted filter" produced a hint recommending
    ``scripts/migrate-shared-kg-schema.sh``. That script ``DELETE``s
    ``$SHARED_KG_COLLECTION``. The hint fired on a substring match alone, so
    a purely client-side query-construction bug in ``query_code_structure``
    — filtering the ``imports`` CROSS-REFERENCE on a per-project
    ``*_CodeModule`` with ``Filter.by_property(...).contains_any([...])``,
    whose GRPC rejection happens to contain the words "nested query" — told
    the user to drop an unrelated, populated shared KG (738 nodes on the
    machine where this was found). The premise was false too: every
    collection involved had ``indexNullState=true``.

    Those tests therefore PINNED a data-loss instruction. They are replaced,
    not deleted: the assertions below pin the guarantee that replaced them,
    so the fix stays testable and a regression is loud.

    The contract, in three parts:
      1. VERIFY the premise at emit time — probe the live schema of the
         collection that actually failed before asserting it is defective.
      2. NEVER name a collection other than the one at fault, and never
         redirect from a code-graph class to the shared KG.
      3. FAIL SAFE — when the premise cannot be confirmed (no client, no
         parseable collection name), describe what to check and emit NOTHING
         destructive.
    """

    # Messages that name NO collection — the shapes the pre-v0.2.92 tests used.
    UNIDENTIFIABLE = (
        "build inverted filter allow list: fetch doc ids for prop/value "
        "pair: nested query: schema not configured",
        "build inverted filter allow list: failed",
        "nested query error during is_null filter",
    )

    def _hint_for(self, message, *, classes=None, shared="Fake_SharedKnowledgeGraph"):
        client = _FakeSchemaClient(classes or FAKE_SCHEMA_CLASSES)
        with mock.patch.object(srv, "get_weaviate_client", return_value=client), \
             mock.patch.object(srv, "SHARED_KG_COLLECTION", shared):
            result = _classify(Exception(message))
        self.assertIsInstance(result, WeaviateSchemaError)
        return result.user_msg

    def _assert_nothing_destructive(self, hint):
        for marker in DESTRUCTIVE_MARKERS:
            self.assertNotIn(
                marker, hint,
                f"hint offered the destructive '{marker}' — {hint}",
            )

    # ---- FAIL SAFE: premise cannot be confirmed ----------------------

    def test_unidentifiable_collection_emits_nothing_destructive(self):
        for message in self.UNIDENTIFIABLE:
            with self.subTest(message=message):
                hint = self._hint_for(message)
                self._assert_nothing_destructive(hint)
                self.assertIn("Could not verify", hint)

    def test_unidentifiable_collection_never_touches_weaviate(self):
        """Hermeticity: a message naming no collection must not attempt a
        connection — otherwise every CI run without Weaviate pays a connect
        timeout inside a unit test."""
        for message in self.UNIDENTIFIABLE:
            with self.subTest(message=message):
                with mock.patch.object(srv, "get_weaviate_client") as get_client:
                    _classify(Exception(message))
                get_client.assert_not_called()

    def test_probe_failure_emits_nothing_destructive(self):
        client = _FakeSchemaClient(FAKE_SCHEMA_CLASSES, get_raises=True)
        with mock.patch.object(srv, "get_weaviate_client", return_value=client):
            result = _classify(Exception(_nested_query_error("fakeproject_codeclass")))
        self._assert_nothing_destructive(result.user_msg)
        self.assertIn("Could not verify", result.user_msg)

    # ---- VERIFY: premise refuted by the probe ------------------------

    def test_present_index_null_state_emits_nothing_destructive(self):
        hint = self._hint_for(_nested_query_error("fakeproject_codemodule"))
        self._assert_nothing_destructive(hint)
        self.assertIn("already True", hint)
        self.assertIn("FakeProject_CodeModule", hint)

    def test_code_graph_failure_never_points_at_the_shared_kg(self):
        """The v0.2.92 BLOCKER, pinned at the classifier boundary."""
        hint = self._hint_for(_nested_query_error("fakeproject_codemodule"))
        self.assertNotIn("Fake_SharedKnowledgeGraph", hint)
        self.assertNotIn("migrate-shared-kg-schema", hint)

    # ---- ACT: premise CONFIRMED by the probe -------------------------

    def test_confirmed_absent_on_the_shared_kg_does_offer_the_migration(self):
        """The leave-alone cases above are only meaningful if the act case
        still fires: when the failing collection IS the configured shared KG
        and the probe confirms its null index is gone, the migration script
        is exactly right and must still be offered."""
        classes = dict(FAKE_SCHEMA_CLASSES, Fake_SharedKnowledgeGraph=False)
        hint = self._hint_for(
            _nested_query_error("fake_sharedknowledgegraph"), classes=classes
        )
        self.assertIn("migrate-shared-kg-schema.sh", hint)
        self.assertIn("Fake_SharedKnowledgeGraph", hint)
        self.assertIn("is absent", hint)

    def test_confirmed_absent_elsewhere_offers_no_paste_ready_drop(self):
        hint = self._hint_for(_nested_query_error("fakeproject_codeclass"))
        self.assertIn("FakeProject_CodeClass", hint)
        self.assertIn("is absent", hint)
        self._assert_nothing_destructive(hint)
        self.assertIn("do not migrate any other collection", hint)

    # ---- NEVER misdirect --------------------------------------------

    def test_no_hint_names_a_collection_other_than_the_failing_one(self):
        cases = (
            ("fakeproject_codemodule", "FakeProject_CodeModule", None),
            ("fakeproject_codeclass", "FakeProject_CodeClass", None),
            (
                "fake_sharedknowledgegraph",
                "Fake_SharedKnowledgeGraph",
                dict(FAKE_SCHEMA_CLASSES, Fake_SharedKnowledgeGraph=False),
            ),
        )
        for index_name, failing, classes in cases:
            with self.subTest(failing=failing):
                schema = classes or FAKE_SCHEMA_CLASSES
                hint = self._hint_for(
                    _nested_query_error(index_name), classes=schema
                )
                for other in schema:
                    if other == failing:
                        continue
                    self.assertNotIn(
                        other, hint,
                        f"hint for {failing} named unrelated collection {other}",
                    )


if __name__ == "__main__":
    unittest.main()
