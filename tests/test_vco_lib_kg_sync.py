# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for ``vco_lib.kg_sync.batch_query_content_hashes``.

The helper extracts the V46-A-hardened content_hash fetch into a reusable
module. These tests pin:

1. The V46-A safety triad: no ``where: Like "%"`` filter, ``limit: 10000``,
   errors-array inspection BEFORE data consumption, saturation warning.
2. Routing via V46-F's ``post_graphql_safe`` (so observability + transport
   handling stays centralised).
3. The same return-shape contract as the legacy
   ``install.py::_batch_query_weaviate_content_hashes`` (which is now a
   thin wrapper around this helper, kept for back-compat with existing
   call sites).

We mock at the ``post_graphql_safe`` layer so the tests are HTTP-free
(no Weaviate process required) and cross-OS by construction.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from vco_lib.kg_sync import QUERY_MAX_LIMIT, batch_query_content_hashes


def _stub_graphql_response(class_name: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    """Build the ``data`` payload shape ``post_graphql_safe`` returns."""
    return {"Get": {class_name: rows}}


class TestHappyPath:
    def test_returns_file_path_to_content_hash_map(self):
        rows = [
            {"file_path": "a.md", "content_hash": "h_a"},
            {"file_path": "b.md", "content_hash": "h_b"},
        ]
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            mock_post.return_value = _stub_graphql_response("FooKG", rows)
            result = batch_query_content_hashes("http://x:8081", "FooKG")
        assert result == {"a.md": "h_a", "b.md": "h_b"}

    def test_returns_empty_dict_when_collection_has_no_rows(self):
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            mock_post.return_value = _stub_graphql_response("EmptyKG", [])
            result = batch_query_content_hashes("http://x:8081", "EmptyKG")
        assert result == {}

    def test_missing_content_hash_yields_empty_string_value(self):
        """Legacy rows missing content_hash get '' — the diff caller treats
        this as 'always stale' for that file. Preserves the v0.2.17
        upgrade-path semantics."""
        rows = [
            {"file_path": "legacy.md"},  # no content_hash key
            {"file_path": "fresh.md", "content_hash": "h_fresh"},
        ]
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            mock_post.return_value = _stub_graphql_response("X", rows)
            result = batch_query_content_hashes("http://x:8081", "X")
        assert result == {"legacy.md": "", "fresh.md": "h_fresh"}

    def test_skips_rows_with_empty_file_path(self):
        rows = [
            {"file_path": "", "content_hash": "orphan"},
            {"file_path": "real.md", "content_hash": "h"},
        ]
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            mock_post.return_value = _stub_graphql_response("X", rows)
            result = batch_query_content_hashes("http://x:8081", "X")
        assert result == {"real.md": "h"}
        assert "" not in result


class TestV46ASafetyTriad:
    """Pin the V46-A safety triad: no Like-%, limit:10000, errors-first
    inspection (via routing through post_graphql_safe), saturation warn."""

    def test_query_string_contains_limit_10000(self):
        """The legacy bug shipped ``limit: 1000`` which silently truncated
        the user's 1193-row collection. V46-A bumped to 10000."""
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            mock_post.return_value = _stub_graphql_response("X", [])
            batch_query_content_hashes("http://x:8081", "MyClass")
        assert mock_post.call_count == 1
        gql_arg = mock_post.call_args.args[1]  # second positional arg is the gql dict
        assert "limit: 10000" in gql_arg["query"]
        assert "MyClass" in gql_arg["query"]

    def test_query_string_does_NOT_contain_where_like_percent(self):
        """The recurring bug was a ``where: {operator: Like, valueText: "%"}``
        filter. V46-A dropped it. This test enforces it stays dropped."""
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            mock_post.return_value = _stub_graphql_response("X", [])
            batch_query_content_hashes("http://x:8081", "X")
        gql_arg = mock_post.call_args.args[1]
        assert "where:" not in gql_arg["query"].lower()
        assert "like" not in gql_arg["query"].lower()
        assert '"%"' not in gql_arg["query"]

    def test_saturation_warning_fires_at_limit(self):
        """When result count hits QUERY_MAX_LIMIT (10000), the helper must
        emit a 'saturation' on_warn callback so the caller can surface
        the truncation risk."""
        rows = [
            {"file_path": f"f_{i}.md", "content_hash": f"h_{i}"}
            for i in range(QUERY_MAX_LIMIT)
        ]
        warns: list[tuple[str, dict[str, Any]]] = []
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            mock_post.return_value = _stub_graphql_response("BigKG", rows)
            result = batch_query_content_hashes(
                "http://x:8081", "BigKG", on_warn=lambda c, d: warns.append((c, d))
            )
        assert len(result) == QUERY_MAX_LIMIT
        assert any(c == "saturation" for c, _ in warns)
        sat = next(d for c, d in warns if c == "saturation")
        assert sat["collection"] == "BigKG"
        assert sat["rows"] == QUERY_MAX_LIMIT

    def test_saturation_warning_does_NOT_fire_below_limit(self):
        rows = [{"file_path": "a.md", "content_hash": "h"}]
        warns: list[tuple[str, dict[str, Any]]] = []
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            mock_post.return_value = _stub_graphql_response("SmallKG", rows)
            batch_query_content_hashes(
                "http://x:8081", "SmallKG", on_warn=lambda c, d: warns.append((c, d))
            )
        assert not any(c == "saturation" for c, _ in warns)


class TestRoutingViaPostGraphqlSafe:
    """Confirm the helper routes via vco_lib.weaviate_helpers.post_graphql_safe
    rather than open-coding a urllib.urlopen call. This is the V46-F
    contract: one transport surface, one errors-array gate."""

    def test_uses_post_graphql_safe_not_raw_urlopen(self):
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            mock_post.return_value = _stub_graphql_response("X", [])
            batch_query_content_hashes("http://x:8081", "X")
        # If the helper bypassed post_graphql_safe, this call would never
        # happen and mock_post.call_count would be 0.
        assert mock_post.call_count == 1
        # Confirm the call signature carries the ctx + on_error callback
        # that V46-F expects.
        call_kwargs = mock_post.call_args.kwargs
        assert "ctx" in call_kwargs
        assert "X" in call_kwargs["ctx"]
        assert "on_error" in call_kwargs

    def test_post_graphql_safe_returning_none_yields_empty_dict(self):
        """V46-F returns None on errors-array OR transport failure. The
        caller (this helper) must coerce to an empty dict so the diff
        gate defaults to 'full sync required'."""
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            mock_post.return_value = None
            result = batch_query_content_hashes("http://x:8081", "X")
        assert result == {}


class TestOnWarnCallback:
    def test_graphql_errors_channel_fires_for_non_transport_errors(self):
        """When V46-F invokes on_error with a graphql-level error, the
        helper relays to on_warn with channel='graphql_errors'."""
        warns: list[tuple[str, dict[str, Any]]] = []
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            # Simulate post_graphql_safe's contract: it calls our on_error
            # with the errors list, then returns None.
            def _fake_post(url, gql, *, ctx, on_error):
                on_error([{"message": "Only stopwords provided", "path": ["Get"]}])
                return None
            mock_post.side_effect = _fake_post
            batch_query_content_hashes(
                "http://x:8081", "BadKG", on_warn=lambda c, d: warns.append((c, d))
            )
        assert any(c == "graphql_errors" for c, _ in warns)
        evt = next(d for c, d in warns if c == "graphql_errors")
        assert evt["collection"] == "BadKG"
        assert "Only stopwords" in evt["errors"][0]

    def test_transport_failure_channel_fires_for_transport_errors(self):
        """V46-F's synthetic ``[{"message": "transport: ..."}]`` payload
        on HTTP failure must be relayed as channel='transport_failure'
        so the caller can distinguish from GraphQL-level errors."""
        warns: list[tuple[str, dict[str, Any]]] = []
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            def _fake_post(url, gql, *, ctx, on_error):
                on_error([{"message": "transport: Connection refused"}])
                return None
            mock_post.side_effect = _fake_post
            batch_query_content_hashes(
                "http://x:8081", "X", on_warn=lambda c, d: warns.append((c, d))
            )
        assert any(c == "transport_failure" for c, _ in warns)

    def test_on_warn_exceptions_are_swallowed(self):
        """Observability failure must NEVER break the caller. A buggy
        on_warn that raises is silently ignored — the helper still
        returns its result normally."""
        rows = [{"file_path": "a.md", "content_hash": "h"}]
        def _exploding_warn(channel, data):
            raise RuntimeError("on_warn is broken")
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            def _fake_post(url, gql, *, ctx, on_error):
                # Trigger a callback so the relay-path runs.
                on_error([{"message": "transport: x"}])
                return None
            mock_post.side_effect = _fake_post
            # Must not raise.
            result = batch_query_content_hashes(
                "http://x:8081", "X", on_warn=_exploding_warn
            )
        assert result == {}

    def test_on_warn_optional(self):
        """Caller may pass None — helper handles it cleanly."""
        with patch("vco_lib.kg_sync.post_graphql_safe") as mock_post:
            mock_post.return_value = _stub_graphql_response("X", [])
            result = batch_query_content_hashes("http://x:8081", "X", on_warn=None)
        assert result == {}
