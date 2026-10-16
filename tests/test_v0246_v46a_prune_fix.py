# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.46 V46-A: stopword-fix tests for ``_prune_stale_kg_rows``.

Mirrors the test surface of ``test_v0246_v46a_stopword_fix.py`` but targets
``_prune_stale_kg_rows`` (V0243-6). Same root cause — the function shipped
the same broken ``where: Like "%"`` GraphQL filter, silently returning an
empty result on every call, meaning orphan KG rows for files-deleted-from-disk
never got pruned.

v0.2.103: the prune's enumerate → classify → delete pipeline moved OUT of
``install_weaviate._prune_stale_kg_rows`` (it was a second implementation
beside ``vco_lib.collection_repair`` and lacked the shared-identity guard —
a root ``--update`` could delete sibling projects' shared rows). The tests
below keep every V46-A intent, driven through the ONE home at its
``collection_repair._http_request`` mock seam (the install adapter delegates
there), with a NON-root ``project_root`` (a markerless tmp dir — v0.2.103
R2, review H-2: the shared-identity predicate's root leg fires on any
orchestrator clone, and this checkout IS one) so the adapter reaches the
delegation instead of the (correct, tested elsewhere) shared-identity skip.
The shared-identity guard itself is covered by
``tests/test_v02103_install_prune_shared_guard.py``.

See ``knowledge/concepts/silent-zero-fallback-antipattern.md`` instance #3
and ``.claude/context/plans/v0.2.46-design-2026-06-03.md`` § V46-A.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from vco_lib import collection_repair  # noqa: E402
from vco_lib import install_weaviate  # noqa: E402


# ─── collection_repair HTTP-seam fake ────────────────────────────────────────

class _FakeRepairHTTP:
    """Recording fake for ``collection_repair._http_request``.

    Serves the GraphQL enumerate response built from *rows*
    ``[(uuid, file_path, title), ...]`` (or an errors[] body when
    *graphql_errors*), and records batch-delete uuids.
    """

    def __init__(self, rows, *, graphql_errors: bool = False) -> None:
        self.rows = list(rows)
        self.graphql_errors = graphql_errors
        self.deleted_uuids: list[str] = []
        self.queries: list[str] = []
        self.calls: list = []

    def __call__(self, method, url, *, body=None, timeout=30.0):  # noqa: ARG002
        self.calls.append((method, url, body))
        if self.graphql_errors:
            return (200, json.dumps(
                {"errors": [{"message": "only stopwords provided"}]}
            ).encode())
        if method == "POST" and url.endswith("/v1/graphql"):
            assert body is not None  # GraphQL branch always carries a body
            self.queries.append(body["query"])
            objs = [
                {"_additional": {"id": u}, "file_path": fp, "title": t}
                for u, fp, t in self.rows
            ]
            coll = body["query"].split("{ Get { ")[1].split("(")[0]
            return (200, json.dumps(
                {"data": {"Get": {coll: objs}}}
            ).encode())
        if method == "DELETE" and url.endswith("/v1/batch/objects"):
            assert body is not None  # DELETE branch always carries a body
            chunk = body["match"]["where"]["valueTextArray"]
            self.deleted_uuids.extend(chunk)
            return (200, json.dumps(
                {"results": {"successful": len(chunk), "failed": 0}}
            ).encode())
        return (404, b"")


def _run_prune(fake: _FakeRepairHTTP, *, dry_run: bool = True,
               path_check=None) -> mock.MagicMock:
    """Drive ``install_weaviate._prune_stale_kg_rows`` with the fake seam.

    ``project_root`` is a markerless tmp dir: the repo checkout these tests
    run from IS the orchestrator clone, and the home's shared-identity
    predicate (v0.2.103 R2, review H-2) would correctly skip the prune on
    it. The existence check is faked at the home's seam
    (``_stored_file_exists_for_prune``, the v0.2.103 R2 / H-1 name) so the
    tmp tree needs no fixture files.
    """
    log_mock = mock.MagicMock()
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(collection_repair, "_http_request", fake), \
         mock.patch.object(install, "_log_install_event", log_mock), \
         mock.patch.object(
             collection_repair, "_stored_file_exists_for_prune",
             path_check or (lambda fp, root: False),
         ):
        install_weaviate._prune_stale_kg_rows(
            "TestCollection", "http://localhost:8081", dry_run=dry_run,
            project_root=Path(tmp),
            log_event=install._log_install_event,
        )
    return log_mock


def _warn_details(log_mock: mock.MagicMock) -> list[str]:
    return [
        c.args[2] if len(c.args) >= 3 else ""
        for c in log_mock.call_args_list
        if len(c.args) >= 2 and c.args[1] == "warn"
    ]


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestV46APruneFix(unittest.TestCase):
    """v0.2.46 V46-A fetch-time semantics, via the ONE prune home."""

    def setUp(self) -> None:
        self._orig_pending = install._PENDING_EVENTS[:]
        install._PENDING_EVENTS.clear()

    def tearDown(self) -> None:
        install._PENDING_EVENTS.clear()
        install._PENDING_EVENTS.extend(self._orig_pending)

    # ── 1. errors array → WARN + no destructive action ─────────────────────

    def test_v46a_prune_errors_array_triggers_warn_log(self) -> None:
        """Non-empty ``errors[]`` MUST log WARN and skip the leg.

        Skipping (rather than processing a possibly-truncated result set) is
        the safer default: better to skip prune than to delete rows based on
        an incomplete stored-rows view.
        """
        fake = _FakeRepairHTTP([], graphql_errors=True)
        log_mock = _run_prune(fake, dry_run=False)

        warns = _warn_details(log_mock)
        self.assertEqual(len(warns), 1, f"expected one warn; got {warns!r}")
        self.assertIn("enumerate", warns[0])
        self.assertIn("TestCollection", warns[0])
        # No destructive action without an authoritative row list.
        self.assertEqual(fake.deleted_uuids, [])

    # ── 2. legitimate empty collection → no warn, no delete attempt ─────────

    def test_v46a_prune_empty_collection_no_warn(self) -> None:
        """An empty (but valid) collection MUST NOT emit a warn."""
        fake = _FakeRepairHTTP([])
        log_mock = _run_prune(fake, dry_run=False)

        self.assertEqual(_warn_details(log_mock), [])
        self.assertEqual(fake.deleted_uuids, [])

    # ── 3. populated collection → rows are inspected ────────────────────────

    def test_v46a_prune_populated_collection_evaluates_rows(self) -> None:
        """When Weaviate returns rows, prune MUST evaluate each one against
        the on-disk file-existence check.

        ``_stored_file_exists_for_prune`` is faked to return False for every
        row so the rows are flagged as stale; with ``dry_run=True`` no delete
        fires.
        """
        fake = _FakeRepairHTTP([
            ("uuid-a", "knowledge/orphan-a.md", "A"),
            ("uuid-b", "knowledge/orphan-b.md", "B"),
        ])
        log_mock = _run_prune(fake, dry_run=True)

        infoish = [
            c.args[2] if len(c.args) >= 3 else ""
            for c in log_mock.call_args_list
            if len(c.args) >= 2 and c.args[1] in ("info", "ok")
        ]
        self.assertTrue(
            any("stale" in d or "foreign" in d for d in infoish),
            f"expected a log mentioning stale/foreign rows; got "
            f"{log_mock.call_args_list!r}",
        )
        self.assertEqual(_warn_details(log_mock), [])
        self.assertEqual(fake.deleted_uuids, [])

    # ── 4. saturation at the 10k cap: truncated view proceeds safely ───────

    def test_v46a_prune_saturation_warns_and_proceeds_per_row(self) -> None:
        """At ``QUERY_MAXIMUM_RESULTS`` the prune MUST warn — and, since
        v0.2.103 unified the homes, PROCEED on the truncated view.

        The old install-local implementation aborted at the cap on the theory
        that a truncated view could mark live rows stale; that theory was
        wrong (staleness is a per-row decision made from the row's OWN path,
        not from the completeness of the view), and
        ``vco_lib.collection_repair`` — the one home, reviewed for exactly
        this — deliberately proceeds and marks ``saturated``. This test pins
        the unified policy at the install surface.
        """
        rows = [
            (f"uuid-{i}", f"knowledge/file-{i}.md", None)
            for i in range(collection_repair.QUERY_MAX_LIMIT)
        ]
        fake = _FakeRepairHTTP(rows)
        log_mock = _run_prune(fake, dry_run=True)

        warns = _warn_details(log_mock)
        self.assertEqual(len(warns), 1, f"expected one warn; got {warns!r}")
        self.assertIn("10000", warns[0])
        self.assertIn("QUERY_MAXIMUM_RESULTS", warns[0])
        # Per-row policy: the truncated view IS evaluated (dry-run ⇒ the
        # would-be deletions are reported, none executed).
        self.assertEqual(fake.deleted_uuids, [])
        dry_run_logged = any(
            "would be deleted" in (c.args[2] if len(c.args) >= 3 else "")
            for c in log_mock.call_args_list
        )
        self.assertTrue(
            dry_run_logged,
            "a saturated dry-run must still report the would-be deletions",
        )

    # ── 5. regression guard: no more Like/% filter ─────────────────────────

    def test_v46a_prune_filter_dropped_no_more_where_clause(self) -> None:
        """Prune's GraphQL query MUST NOT contain ``Like`` / ``%`` /
        ``valueText``."""
        fake = _FakeRepairHTTP([])
        _run_prune(fake, dry_run=True)

        self.assertEqual(len(fake.queries), 1)
        sent_query = fake.queries[0]
        self.assertNotIn("Like", sent_query)
        self.assertNotIn("valueText", sent_query)
        self.assertNotIn("%", sent_query)
        # Sanity: still selects the right fields.
        self.assertIn("limit: 10000", sent_query)
        self.assertIn("_additional", sent_query)
        self.assertIn("file_path", sent_query)

    # ── 6. prune-specific: GraphQL braces are balanced ─────────────────────

    def test_v46a_prune_query_has_balanced_braces(self) -> None:
        """The GraphQL query MUST be syntactically balanced (no "Expected
        Name, found EOF" from Weaviate)."""
        fake = _FakeRepairHTTP([])
        _run_prune(fake, dry_run=True)

        sent_query = fake.queries[0]
        open_count = sent_query.count("{")
        close_count = sent_query.count("}")
        self.assertEqual(
            open_count, close_count,
            f"GraphQL braces unbalanced: opens={open_count} "
            f"closes={close_count} query={sent_query!r}",
        )
        self.assertGreaterEqual(open_count, 4)


if __name__ == "__main__":
    unittest.main()
