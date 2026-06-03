# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.46 V46-A: stopword-fix tests for ``_prune_stale_kg_rows``.

Mirrors the test surface of ``test_v0246_v46a_stopword_fix.py`` but targets
``_prune_stale_kg_rows`` (V0243-6). Same root cause — the function shipped
the same broken ``where: Like "%"`` GraphQL filter at ``install.py:6135``,
silently returning empty result on every call, meaning orphan KG rows for
files-deleted-from-disk never got pruned.

Plus one extra test specific to prune: the pre-v0.2.46 query string had a
secondary missing-closing-brace issue that Investigator 1 reproduced live
as ``"Expected Name, found EOF"``. The v0.2.46 V46-A rewrite drops the
``where`` clause entirely and the brace balance is now trivially correct;
this test guards against regression.

See ``knowledge/concepts/silent-zero-fallback-antipattern.md`` instance #3
and ``.claude/context/plans/v0.2.46-design-2026-06-03.md`` § V46-A.
"""

from __future__ import annotations

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


def _make_urlopen_returning(body: dict) -> mock.MagicMock:
    """Build a urlopen mock that returns a single canned response body."""
    return mock.MagicMock(return_value=_FakeResponse(body))


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestV46APruneFix(unittest.TestCase):
    """v0.2.46 V46-A: ``_prune_stale_kg_rows`` fix.

    All tests run with ``dry_run=True`` to avoid the post-fetch DELETE path
    (that's covered by the V0243-6 existing tests). We're only validating
    the FETCH-time fix here.
    """

    def setUp(self) -> None:
        self._orig_pending = install._PENDING_EVENTS[:]
        install._PENDING_EVENTS.clear()

    def tearDown(self) -> None:
        install._PENDING_EVENTS.clear()
        install._PENDING_EVENTS.extend(self._orig_pending)

    # ── 1. errors array → WARN + early return ───────────────────────────────

    def test_v46a_prune_errors_array_triggers_warn_log(self) -> None:
        """Non-empty ``errors[]`` MUST log WARN and abort the prune.

        Aborting (rather than processing a possibly-truncated result set) is
        the safer default: better to skip prune than to delete rows based on
        an incomplete stored-rows view.
        """
        body = {
            "data": {"Get": {"TestCollection": None}},
            "errors": [{"message": "only stopwords provided in search"}],
        }
        urlopen_mock = _make_urlopen_returning(body)

        with mock.patch("urllib.request.urlopen", urlopen_mock), \
             mock.patch.object(install, "_log_install_event") as log_mock:
            install._prune_stale_kg_rows(
                "TestCollection", "http://localhost:8081", dry_run=True,
            )

        warn_calls = [
            c for c in log_mock.call_args_list
            if len(c.args) >= 2 and c.args[1] == "warn"
        ]
        self.assertEqual(
            len(warn_calls), 1,
            f"expected exactly one warn log; got: {log_mock.call_args_list!r}",
        )
        detail = warn_calls[0].args[2] if len(warn_calls[0].args) >= 3 else ""
        self.assertIn("V0243-6", detail)
        self.assertIn("GraphQL errors", detail)
        self.assertIn("TestCollection", detail)

    # ── 2. legitimate empty collection → no warn, no delete attempt ─────────

    def test_v46a_prune_empty_collection_no_warn(self) -> None:
        """An empty (but valid) collection MUST log an OK-level event
        ("no stale rows") and NOT emit a warn."""
        body = {"data": {"Get": {"TestCollection": []}}}
        urlopen_mock = _make_urlopen_returning(body)

        with mock.patch("urllib.request.urlopen", urlopen_mock), \
             mock.patch.object(install, "_log_install_event") as log_mock:
            install._prune_stale_kg_rows(
                "TestCollection", "http://localhost:8081", dry_run=True,
            )

        warn_calls = [
            c for c in log_mock.call_args_list
            if len(c.args) >= 2 and c.args[1] == "warn"
        ]
        self.assertEqual(
            warn_calls, [],
            f"expected NO warn log for empty prune; got: {warn_calls!r}",
        )

    # ── 3. populated collection → rows are inspected ────────────────────────

    def test_v46a_prune_populated_collection_evaluates_rows(self) -> None:
        """When Weaviate returns rows, prune MUST evaluate each one against
        the on-disk file-existence check.

        We mock ``_path_resolves_on_disk`` to return False for every row so
        the rows are flagged as stale; with ``dry_run=True`` no delete fires.
        """
        body = {
            "data": {
                "Get": {
                    "TestCollection": [
                        {"_additional": {"id": "uuid-a"},
                         "file_path": "knowledge/orphan-a.md"},
                        {"_additional": {"id": "uuid-b"},
                         "file_path": "knowledge/orphan-b.md"},
                    ]
                }
            }
        }
        urlopen_mock = _make_urlopen_returning(body)

        with mock.patch("urllib.request.urlopen", urlopen_mock), \
             mock.patch.object(install, "_path_resolves_on_disk", return_value=False), \
             mock.patch.object(install, "_log_install_event") as log_mock:
            install._prune_stale_kg_rows(
                "TestCollection", "http://localhost:8081", dry_run=True,
            )

        # Should have logged an INFO-level "N stale row(s)" event (and possibly
        # nothing else, since dry_run=True short-circuits before the DELETE).
        info_calls = [
            c for c in log_mock.call_args_list
            if len(c.args) >= 2 and c.args[1] == "info"
        ]
        # At least one info event referencing the stale count.
        self.assertTrue(
            any("stale" in (c.args[2] if len(c.args) >= 3 else "") for c in info_calls),
            f"expected an info log mentioning stale rows; got: {log_mock.call_args_list!r}",
        )
        # No warn from a healthy populated query.
        warn_calls = [
            c for c in log_mock.call_args_list
            if len(c.args) >= 2 and c.args[1] == "warn"
        ]
        self.assertEqual(warn_calls, [])

    # ── 4. saturation warning at 10k rows + aborts prune ────────────────────

    def test_v46a_prune_saturation_warning_aborts(self) -> None:
        """At ``QUERY_MAXIMUM_RESULTS`` cap, prune MUST log a warn and abort.

        Unlike the diff gate (where a truncated stored-set just means "more
        files look stale than necessary; full sync still safe"), the prune
        operation must NOT run on a truncated view — rows that fell past
        the limit would be wrongly flagged as orphans.
        """
        rows = [
            {"_additional": {"id": f"uuid-{i}"},
             "file_path": f"knowledge/file-{i}.md"}
            for i in range(10000)
        ]
        body = {"data": {"Get": {"TestCollection": rows}}}
        urlopen_mock = _make_urlopen_returning(body)

        # _path_resolves_on_disk should NOT be called if prune aborts; make
        # it raise so any accidental call fails the test loudly.
        path_check_mock = mock.MagicMock(side_effect=AssertionError(
            "_path_resolves_on_disk must not be called when saturation aborts"
        ))

        with mock.patch("urllib.request.urlopen", urlopen_mock), \
             mock.patch.object(install, "_path_resolves_on_disk", path_check_mock), \
             mock.patch.object(install, "_log_install_event") as log_mock:
            install._prune_stale_kg_rows(
                "TestCollection", "http://localhost:8081", dry_run=True,
            )

        warn_calls = [
            c for c in log_mock.call_args_list
            if len(c.args) >= 2 and c.args[1] == "warn"
        ]
        self.assertEqual(len(warn_calls), 1)
        detail = warn_calls[0].args[2] if len(warn_calls[0].args) >= 3 else ""
        self.assertIn("10000", detail)
        self.assertIn("QUERY_MAXIMUM_RESULTS", detail)
        # And confirm _path_resolves_on_disk was indeed not called.
        self.assertEqual(path_check_mock.call_count, 0)

    # ── 5. regression guard: no more Like/% filter ──────────────────────────

    def test_v46a_prune_filter_dropped_no_more_where_clause(self) -> None:
        """Prune's GraphQL query MUST NOT contain ``Like`` / ``%`` / ``valueText``."""
        body = {"data": {"Get": {"TestCollection": []}}}
        urlopen_mock = _make_urlopen_returning(body)

        with mock.patch("urllib.request.urlopen", urlopen_mock), \
             mock.patch.object(install, "_log_install_event"):
            install._prune_stale_kg_rows(
                "TestCollection", "http://localhost:8081", dry_run=True,
            )

        self.assertEqual(urlopen_mock.call_count, 1)
        req = urlopen_mock.call_args[0][0]
        sent_payload = req.data.decode("utf-8") if isinstance(req.data, bytes) else str(req.data)
        sent_query = json.loads(sent_payload).get("query", "")

        self.assertNotIn("Like", sent_query)
        self.assertNotIn("valueText", sent_query)
        self.assertNotIn("%", sent_query)
        # Sanity: still selects the right fields.
        self.assertIn("limit: 10000", sent_query)
        self.assertIn("_additional", sent_query)
        self.assertIn("file_path", sent_query)

    # ── 6. prune-specific: GraphQL braces are balanced ──────────────────────

    def test_v46a_prune_query_has_balanced_braces(self) -> None:
        """The v0.2.46 V46-A rewrite MUST emit a syntactically balanced
        GraphQL query (no "Expected Name, found EOF" from Weaviate).

        Pre-v0.2.46, Investigator 1 reproduced this error live against a
        real Weaviate. The simplified post-V46-A query has 4 ``{`` and
        4 ``}`` (Get + selector + _additional + outer wrapper).
        """
        body = {"data": {"Get": {"TestCollection": []}}}
        urlopen_mock = _make_urlopen_returning(body)

        with mock.patch("urllib.request.urlopen", urlopen_mock), \
             mock.patch.object(install, "_log_install_event"):
            install._prune_stale_kg_rows(
                "TestCollection", "http://localhost:8081", dry_run=True,
            )

        req = urlopen_mock.call_args[0][0]
        sent_payload = req.data.decode("utf-8") if isinstance(req.data, bytes) else str(req.data)
        sent_query = json.loads(sent_payload).get("query", "")

        open_count = sent_query.count("{")
        close_count = sent_query.count("}")
        self.assertEqual(
            open_count, close_count,
            f"GraphQL braces unbalanced: opens={open_count} closes={close_count} "
            f"query={sent_query!r}",
        )
        # Sanity: at least 4 each (Get-block + selector + _additional + outer).
        self.assertGreaterEqual(open_count, 4)


if __name__ == "__main__":
    unittest.main()
