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

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import claude_mcp_servers.weaviate_mcp.server as srv  # noqa: E402

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
        """Issue D symptom: collection lacks indexNullState=True."""
        result = _classify(
            Exception(
                "build inverted filter allow list: fetch doc ids for "
                "prop/value pair: nested query: schema not configured"
            )
        )
        self.assertIsInstance(result, WeaviateSchemaError)
        self.assertIn(
            "migrate-shared-kg-schema.sh",
            result.user_msg,
            f"hint missing shared-kg migration script: {result.user_msg}",
        )

    def test_build_inverted_filter_alone(self):
        result = _classify(Exception("build inverted filter allow list: failed"))
        self.assertIsInstance(result, WeaviateSchemaError)
        self.assertIn("migrate-shared-kg-schema.sh", result.user_msg)

    def test_nested_query_alone(self):
        result = _classify(Exception("nested query error during is_null filter"))
        self.assertIsInstance(result, WeaviateSchemaError)
        self.assertIn("migrate-shared-kg-schema.sh", result.user_msg)

    def test_schema_error_takes_precedence_over_query_error_class(self):
        """A WeaviateQueryError whose message looks like schema must be
        classified as Schema, not as Unreachable (the bug behind Issue F).
        """
        try:
            from weaviate.exceptions import WeaviateQueryError
        except ImportError:
            self.skipTest("weaviate-client not installed")
        exc = WeaviateQueryError(
            "could not find class VibecodedOrchestrator_KnowledgeGraph "
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


if __name__ == "__main__":
    unittest.main()
