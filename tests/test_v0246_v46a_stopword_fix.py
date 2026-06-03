# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.46 V46-A: stopword-fix tests for ``_batch_query_weaviate_content_hashes``.

The pre-v0.2.46 implementation shipped a GraphQL filter
``where: {path: ["file_path"], operator: Like, valueText: "%"}``. ``%`` is
SQL-wildcard convention; Weaviate uses ``*`` and rejects ``%`` as "only
stopwords provided", returning HTTP 200 with::

    {"data": {"Get": {"<collection>": null}}, "errors": [...]}

Python code at the call-site silently coalesced ``null → []`` via
``.get(..., []) or []`` and the function returned ``{}`` on every call.
The diff-gate caller treated empty-dict as "no stored hashes" and triggered
a full re-embed of every KG file on every ``install.py --update`` across
v0.2.42 → v0.2.45.

These unit tests assert the v0.2.46 V46-A fix:

  1. ``errors`` array triggers a WARN-level log + returns ``{}`` (loud fail).
  2. Legitimate empty collection returns ``{}`` with NO warn (silent OK).
  3. Populated collection returns the expected ``{file_path: content_hash}``.
  4. Saturation warning fires at ``len(objects) >= 10000``.
  5. The query string no longer contains the broken ``Like`` / ``%`` filter
     (regression guard).

Live integration test (real Weaviate instance) is V46-B's scope, NOT this
file — pure unit tests with mocked urlopen run here.

See ``knowledge/concepts/silent-zero-fallback-antipattern.md`` instance #3
and ``.claude/context/plans/v0.2.46-design-2026-06-03.md`` § V46-A.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402


# ─── Mock-urlopen helpers ─────────────────────────────────────────────────────

class _FakeResponse:
    """Minimal urlopen() response object for json.loads(resp.read())."""

    def __init__(self, body: dict) -> None:
        self._payload = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: D401
        return None


def _capture_query_via_urlopen(body: dict) -> tuple[mock.MagicMock, _FakeResponse]:
    """Build a mock urlopen patcher that records the request body it receives.

    Returns (urlopen_mock, response). The caller wires up the patch and reads
    ``urlopen_mock.call_args[0][0].data`` to assert query-string contents.
    """
    response = _FakeResponse(body)
    urlopen_mock = mock.MagicMock(return_value=response)
    return urlopen_mock, response


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestV46AStopwordFix(unittest.TestCase):
    """v0.2.46 V46-A: ``_batch_query_weaviate_content_hashes`` fix."""

    def setUp(self) -> None:
        # Don't leak _PENDING_EVENTS between tests in case some envs lack the
        # log directory and buffer through.
        self._orig_pending = install._PENDING_EVENTS[:]
        install._PENDING_EVENTS.clear()

    def tearDown(self) -> None:
        install._PENDING_EVENTS.clear()
        install._PENDING_EVENTS.extend(self._orig_pending)

    # ── 1. errors array → WARN + return {} ──────────────────────────────────

    def test_v46a_errors_array_triggers_warn_log(self) -> None:
        """Non-empty ``errors[]`` MUST log WARN and return ``{}``.

        This is THE bug — pre-v0.2.46, the same Weaviate response was
        silently treated as "empty result" and triggered full re-embed.
        """
        body = {
            "data": {"Get": {"TestCollection": None}},
            "errors": [{"message": "only stopwords provided in search"}],
        }
        urlopen_mock, _ = _capture_query_via_urlopen(body)

        with mock.patch("urllib.request.urlopen", urlopen_mock), \
             mock.patch.object(install, "_log_install_event") as log_mock:
            result = install._batch_query_weaviate_content_hashes(
                "TestCollection", "http://localhost:8081"
            )

        self.assertEqual(result, {})
        # Assert a WARN-level event with the right shape was logged.
        warn_calls = [
            c for c in log_mock.call_args_list
            if len(c.args) >= 2 and c.args[1] == "warn"
        ]
        self.assertEqual(len(warn_calls), 1,
                         f"expected exactly one warn log; got: {log_mock.call_args_list!r}")
        # Detail mentions GraphQL errors + the collection name.
        detail = warn_calls[0].args[2] if len(warn_calls[0].args) >= 3 else ""
        self.assertIn("GraphQL errors", detail)
        self.assertIn("TestCollection", detail)
        # Structured data carries the errors list.
        data = warn_calls[0].kwargs.get("data") or {}
        self.assertEqual(data.get("collection"), "TestCollection")
        self.assertIsInstance(data.get("errors"), list)
        self.assertGreaterEqual(len(data["errors"]), 1)

    # ── 2. legitimate empty collection → {} with no warn ────────────────────

    def test_v46a_empty_collection_returns_empty_dict_no_warn(self) -> None:
        """An empty (but valid) collection MUST return ``{}`` and log NO warn.

        Distinguishes "Weaviate said 0 rows" (silent OK; means full sync) from
        "Weaviate said error" (loud WARN; means investigate).
        """
        body = {"data": {"Get": {"TestCollection": []}}}
        urlopen_mock, _ = _capture_query_via_urlopen(body)

        with mock.patch("urllib.request.urlopen", urlopen_mock), \
             mock.patch.object(install, "_log_install_event") as log_mock:
            result = install._batch_query_weaviate_content_hashes(
                "TestCollection", "http://localhost:8081"
            )

        self.assertEqual(result, {})
        warn_calls = [
            c for c in log_mock.call_args_list
            if len(c.args) >= 2 and c.args[1] == "warn"
        ]
        self.assertEqual(
            warn_calls, [],
            f"expected NO warn log for legitimate empty collection; got: {warn_calls!r}",
        )

    # ── 3. populated collection → expected dict ─────────────────────────────

    def test_v46a_populated_collection_returns_dict(self) -> None:
        """Three rows in → three (path, hash) pairs out."""
        body = {
            "data": {
                "Get": {
                    "TestCollection": [
                        {"file_path": "knowledge/a.md", "content_hash": "h-a"},
                        {"file_path": "knowledge/b.md", "content_hash": "h-b"},
                        {"file_path": "knowledge/c.md", "content_hash": "h-c"},
                    ]
                }
            }
        }
        urlopen_mock, _ = _capture_query_via_urlopen(body)

        with mock.patch("urllib.request.urlopen", urlopen_mock), \
             mock.patch.object(install, "_log_install_event") as log_mock:
            result = install._batch_query_weaviate_content_hashes(
                "TestCollection", "http://localhost:8081"
            )

        self.assertEqual(result, {
            "knowledge/a.md": "h-a",
            "knowledge/b.md": "h-b",
            "knowledge/c.md": "h-c",
        })
        # No warnings on a healthy populated query.
        warn_calls = [
            c for c in log_mock.call_args_list
            if len(c.args) >= 2 and c.args[1] == "warn"
        ]
        self.assertEqual(warn_calls, [])

    # ── 4. saturation warning at 10k rows ───────────────────────────────────

    def test_v46a_saturation_warning_at_10k_rows(self) -> None:
        """``len(objects) >= 10000`` MUST emit a saturation warning.

        Signals that the result set was truncated by Weaviate's
        ``QUERY_MAXIMUM_RESULTS`` cap. Future enhancement is cursor pagination.
        """
        rows = [
            {"file_path": f"knowledge/file-{i}.md", "content_hash": f"h-{i}"}
            for i in range(10000)
        ]
        body = {"data": {"Get": {"TestCollection": rows}}}
        urlopen_mock, _ = _capture_query_via_urlopen(body)

        with mock.patch("urllib.request.urlopen", urlopen_mock), \
             mock.patch.object(install, "_log_install_event") as log_mock:
            result = install._batch_query_weaviate_content_hashes(
                "TestCollection", "http://localhost:8081"
            )

        self.assertEqual(len(result), 10000)
        # Saturation warn must fire.
        warn_calls = [
            c for c in log_mock.call_args_list
            if len(c.args) >= 2 and c.args[1] == "warn"
        ]
        self.assertEqual(len(warn_calls), 1)
        detail = warn_calls[0].args[2] if len(warn_calls[0].args) >= 3 else ""
        self.assertIn("10000", detail)
        self.assertIn("QUERY_MAXIMUM_RESULTS", detail)

    # ── 5. regression guard: no more Like/% filter ──────────────────────────

    def test_v46a_filter_dropped_no_more_where_clause(self) -> None:
        """The GraphQL query string MUST NOT contain ``Like`` / ``%`` / ``valueText``.

        This is the regression guard. If anyone re-adds the broken filter, this
        test fails loudly. The earlier behavioural tests would still catch the
        runtime bug, but this catches the SOURCE-LEVEL reintroduction.
        """
        body = {"data": {"Get": {"TestCollection": []}}}
        urlopen_mock, _ = _capture_query_via_urlopen(body)

        with mock.patch("urllib.request.urlopen", urlopen_mock), \
             mock.patch.object(install, "_log_install_event"):
            install._batch_query_weaviate_content_hashes(
                "TestCollection", "http://localhost:8081"
            )

        # Inspect the Request that urlopen was called with.
        self.assertEqual(urlopen_mock.call_count, 1)
        req = urlopen_mock.call_args[0][0]
        # urllib.request.Request stores body bytes in .data
        sent_payload = req.data.decode("utf-8") if isinstance(req.data, bytes) else str(req.data)
        sent_query = json.loads(sent_payload).get("query", "")

        self.assertNotIn("Like", sent_query,
                         f"GraphQL query still contains the broken Like operator: {sent_query!r}")
        self.assertNotIn("valueText", sent_query,
                         f"GraphQL query still references valueText: {sent_query!r}")
        self.assertNotIn("%", sent_query,
                         f"GraphQL query still contains the SQL-wildcard %: {sent_query!r}")
        # Sanity: the query DOES contain the new limit + content_hash selection.
        self.assertIn("limit: 10000", sent_query)
        self.assertIn("content_hash", sent_query)
        self.assertIn("file_path", sent_query)


if __name__ == "__main__":
    unittest.main()
