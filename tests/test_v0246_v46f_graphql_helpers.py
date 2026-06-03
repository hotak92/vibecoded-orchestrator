# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``vco_lib.weaviate_helpers`` — the reusable GraphQL safety helpers.

Covers V46-F's helper module:
- ``check_graphql_errors`` — errors-array gate
- ``post_graphql_safe`` — one-call wrapper (POST + parse + gate)
- ``WeaviateGraphQLError`` — exception class
"""
from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

from vco_lib.weaviate_helpers import (
    WeaviateGraphQLError,
    check_graphql_errors,
    post_graphql_safe,
)


# --- check_graphql_errors --------------------------------------------------


def test_check_graphql_errors_returns_false_on_no_errors():
    body = {"data": {"Get": {"Foo": [{"file_path": "a"}]}}}
    assert check_graphql_errors(body, ctx="test") is False


def test_check_graphql_errors_returns_true_on_errors_present():
    body = {
        "data": None,
        "errors": [{"message": "Cannot query field 'bogus' on type 'Foo'"}],
    }
    assert check_graphql_errors(body, ctx="test") is True


def test_check_graphql_errors_calls_on_error_callback():
    received: list[list[dict[str, Any]]] = []

    body = {
        "data": None,
        "errors": [{"message": "bad query"}, {"message": "second error"}],
    }

    def callback(errs: list[dict[str, Any]]) -> None:
        received.append(errs)

    result = check_graphql_errors(body, ctx="cb-test", on_error=callback)

    assert result is True
    assert len(received) == 1
    assert received[0] == body["errors"]
    assert received[0][0]["message"] == "bad query"


def test_check_graphql_errors_swallows_callback_exceptions():
    """Callback exceptions must not propagate — logging failure never blocks
    the caller's recovery path."""
    body = {"errors": [{"message": "x"}]}

    def boom(_errs: list[dict[str, Any]]) -> None:
        raise RuntimeError("logger died")

    # Must still return True; must not raise.
    result = check_graphql_errors(body, ctx="boom-test", on_error=boom)
    assert result is True


# --- post_graphql_safe -----------------------------------------------------


def _fake_urlopen_response(payload: dict[str, Any]) -> MagicMock:
    """Build a context-manager-shaped mock that yields ``payload`` as JSON
    bytes when ``.read()`` is called."""
    body_bytes = json.dumps(payload).encode("utf-8")
    response = MagicMock()
    response.read.return_value = body_bytes
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_post_graphql_safe_returns_data_on_success():
    payload = {"data": {"Get": {"Foo": [{"file_path": "alpha"}]}}}

    with patch(
        "vco_lib.weaviate_helpers.urllib.request.urlopen",
        return_value=_fake_urlopen_response(payload),
    ):
        result = post_graphql_safe(
            "http://localhost:8081",
            {"query": "{ Get { Foo { file_path } } }"},
            ctx="success-test",
        )

    assert result == payload["data"]
    assert result is not None
    assert result["Get"]["Foo"][0]["file_path"] == "alpha"


def test_post_graphql_safe_returns_none_on_errors_array():
    """HTTP 200 + errors[] must coalesce to None — that's the entire point
    of this helper."""
    payload = {
        "data": None,
        "errors": [{"message": "field 'content_hash' undefined"}],
    }

    with patch(
        "vco_lib.weaviate_helpers.urllib.request.urlopen",
        return_value=_fake_urlopen_response(payload),
    ):
        result = post_graphql_safe(
            "http://localhost:8081",
            {"query": "{ Get { Foo { content_hash } } }"},
            ctx="errors-array-test",
        )

    assert result is None


def test_post_graphql_safe_returns_none_on_http_failure():
    """Transport-layer exception (timeout, connection refused, etc.) must
    return None — caller decides recovery."""
    with patch(
        "vco_lib.weaviate_helpers.urllib.request.urlopen",
        side_effect=ConnectionRefusedError("nothing on 8081"),
    ):
        result = post_graphql_safe(
            "http://localhost:8081",
            {"query": "{ Get { Foo { file_path } } }"},
            ctx="transport-fail-test",
        )

    assert result is None


def test_post_graphql_safe_calls_on_error_for_errors_array():
    received: list[list[dict[str, Any]]] = []

    payload = {
        "data": None,
        "errors": [{"message": "schema mismatch"}],
    }

    with patch(
        "vco_lib.weaviate_helpers.urllib.request.urlopen",
        return_value=_fake_urlopen_response(payload),
    ):
        result = post_graphql_safe(
            "http://localhost:8081",
            {"query": "{ Get { Foo { file_path } } }"},
            ctx="errors-cb-test",
            on_error=lambda errs: received.append(errs),
        )

    assert result is None
    assert len(received) == 1
    assert received[0][0]["message"] == "schema mismatch"


def test_post_graphql_safe_calls_on_error_for_transport_failure():
    """Transport failure should also fire on_error so callers have ONE
    observability path. The synthetic error has shape
    ``[{"message": "transport: <details>"}]``."""
    received: list[list[dict[str, Any]]] = []

    with patch(
        "vco_lib.weaviate_helpers.urllib.request.urlopen",
        side_effect=TimeoutError("read timed out after 30s"),
    ):
        result = post_graphql_safe(
            "http://localhost:8081",
            {"query": "{ Get { Foo { file_path } } }"},
            ctx="transport-cb-test",
            on_error=lambda errs: received.append(errs),
        )

    assert result is None
    assert len(received) == 1
    assert len(received[0]) == 1
    msg = received[0][0]["message"]
    assert msg.startswith("transport: ")
    assert "read timed out" in msg


# --- WeaviateGraphQLError --------------------------------------------------


def test_weaviate_graphql_error_exception_carries_errors():
    errs = [
        {"message": "first"},
        {"message": "second"},
    ]
    exc = WeaviateGraphQLError(errs, ctx="ctxX")

    # The full errors list is preserved on the exception.
    assert exc.errors == errs
    assert exc.errors[0]["message"] == "first"
    assert exc.errors[1]["message"] == "second"

    # Context string is preserved.
    assert exc.ctx == "ctxX"

    # str(exc) includes ctx + first message (truncated to 200 chars).
    rendered = str(exc)
    assert "ctxX" in rendered
    assert "first" in rendered


def test_weaviate_graphql_error_handles_empty_errors_list():
    """Defensive: caller may construct with [] (e.g., for synthetic raises).
    Should not crash; renders 'unknown' as the first-message stand-in."""
    exc = WeaviateGraphQLError([], ctx="empty")
    assert exc.errors == []
    assert exc.ctx == "empty"
    rendered = str(exc)
    assert "empty" in rendered
    assert "unknown" in rendered


def test_weaviate_graphql_error_truncates_long_messages():
    """First message rendering caps at 200 chars to keep exception text
    manageable in logs."""
    long_msg = "x" * 500
    exc = WeaviateGraphQLError([{"message": long_msg}], ctx="trunc")
    rendered = str(exc)
    # The truncated portion (200 chars of x) must be present.
    assert "x" * 200 in rendered
    # But the full 500 must not (rendered should be shorter overall).
    assert "x" * 201 not in rendered
