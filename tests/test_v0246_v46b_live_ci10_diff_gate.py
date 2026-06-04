"""Live integration tests for install._batch_query_weaviate_content_hashes
and install._prune_stale_kg_rows against a real Weaviate.

WHY THIS TEST EXISTS (v0.2.46 V46-B):
v0.2.42 CI-10 introduced _batch_query_weaviate_content_hashes with a
broken GraphQL filter (``where: Like "%"`` — SQL wildcard convention
rejected by Weaviate's BM25 tokenizer). v0.2.43 V0243-6 copy-pasted
the same bug into _prune_stale_kg_rows. Both functions silently
returned empty dicts/lists on every call against any populated
collection.

Every existing test mocked the functions at the boundary, so the bug
was invisible to CI for 4 release cycles. The user only noticed on
the 5th post-update re-embed.

This test brings up a real Weaviate (or skips), seeds known rows, and
asserts the production functions return the expected data. It is the
ONLY structural defense against this class of bug.

Cross-references:
- knowledge/concepts/silent-zero-fallback-antipattern.md § "Real instance #3"
- ~/.claude/projects/-home-martino-Desktop-PROGETTI-VCO-dev/memory/feedback_live_smoke_after_mcp_merge.md
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Import the real install module from the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import install  # noqa: E402

WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "http://localhost:8081")


def _weaviate_reachable() -> bool:
    """Returns True if Weaviate is reachable on the configured URL."""
    try:
        req = urllib.request.Request(f"{WEAVIATE_URL}/v1/meta")
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            return resp.status == 200
    except Exception:
        return False


def _create_test_collection(name: str) -> None:
    """Create a minimal test collection with file_path + content_hash properties.

    No vectorizer is configured — embeddings are irrelevant for these tests
    (we only exercise the GraphQL filter path).
    """
    body = {
        "class": name,
        "properties": [
            {"name": "file_path", "dataType": ["text"]},
            {"name": "content_hash", "dataType": ["text"]},
        ],
        "vectorizer": "none",
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{WEAVIATE_URL}/v1/schema",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        resp.read()


def _delete_test_collection(name: str) -> None:
    """Delete the test collection. Soft-fail."""
    try:
        req = urllib.request.Request(
            f"{WEAVIATE_URL}/v1/schema/{name}",
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            resp.read()
    except Exception:
        # Cleanup is best-effort; the next setUp will collide on the random
        # UUID suffix only with vanishingly small probability.
        pass


def _insert_object(
    collection: str,
    file_path: str,
    content_hash: str,
    *,
    object_uuid: str | None = None,
) -> str:
    """Insert one object; return its UUID."""
    body: dict = {
        "class": collection,
        "properties": {"file_path": file_path, "content_hash": content_hash},
    }
    if object_uuid is not None:
        body["id"] = object_uuid
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{WEAVIATE_URL}/v1/objects",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())["id"]


def _count_objects(collection: str) -> int:
    """Return the count of objects currently in ``collection``.

    Uses Weaviate's Aggregate GraphQL endpoint so it doesn't depend on the
    production code's filter logic (which is what we're testing).
    """
    gql = {
        "query": f"{{ Aggregate {{ {collection} {{ meta {{ count }} }} }} }}",
    }
    data = json.dumps(gql).encode("utf-8")
    req = urllib.request.Request(
        f"{WEAVIATE_URL}/v1/graphql",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            body = json.loads(resp.read())
    except urllib.error.URLError:
        return -1
    try:
        return int(
            body["data"]["Aggregate"][collection][0]["meta"]["count"]
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return -1


@unittest.skipUnless(
    _weaviate_reachable(),
    f"Weaviate not reachable at {WEAVIATE_URL}",
)
class V46BLiveDiffGateTest(unittest.TestCase):
    """Live tests for ``install._batch_query_weaviate_content_hashes``.

    These hit a real Weaviate. The test collection is created in setUp
    and torn down in tearDown so concurrent test runs and prior failed
    runs cannot leak.
    """

    def setUp(self) -> None:
        # uuid suffix avoids collision with concurrent test runs.
        self.test_collection = f"V46BTestDiffGate{uuid.uuid4().hex[:8]}"
        _create_test_collection(self.test_collection)
        # Weaviate's schema-create call returns before the class is fully
        # queryable on every shard. Sleep briefly to let it settle.
        time.sleep(0.3)

    def tearDown(self) -> None:
        _delete_test_collection(self.test_collection)

    def test_empty_collection_returns_empty_dict(self) -> None:
        """A freshly-created collection with zero rows should return {}."""
        result = install._batch_query_weaviate_content_hashes(
            self.test_collection, WEAVIATE_URL,
        )
        self.assertEqual(
            result, {},
            "Empty collection should return empty dict, "
            f"got {len(result)} entries: {list(result.keys())[:5]}",
        )

    def test_three_rows_returns_three_entries(self) -> None:
        """3 seeded rows → returns exactly 3 entries with correct hashes.

        THIS IS THE TEST THAT WOULD HAVE CAUGHT THE v0.2.42 BUG.
        Pre-v0.2.46 code would return {} here (because ``Like "%"`` is
        rejected as a stopword by Weaviate's BM25 tokenizer);
        v0.2.46+ returns 3 entries.
        """
        seeds = [
            ("knowledge/a.md", "hash_a_12345"),
            ("knowledge/b.md", "hash_b_67890"),
            ("knowledge/c.md", "hash_c_abcde"),
        ]
        for fp, ch in seeds:
            _insert_object(self.test_collection, fp, ch)
        # Object writes are also eventually-consistent in Weaviate; allow
        # the inverted index to catch up.
        time.sleep(0.6)

        # Sanity-check via Aggregate that the rows really landed. If this
        # is 0 the seed itself failed and the test would be misleading.
        seeded_count = _count_objects(self.test_collection)
        self.assertEqual(
            seeded_count, len(seeds),
            f"Seed sanity check: expected {len(seeds)} rows via Aggregate, "
            f"got {seeded_count}. Test cannot prove anything about the "
            "diff gate if the seed itself didn't land.",
        )

        result = install._batch_query_weaviate_content_hashes(
            self.test_collection, WEAVIATE_URL,
        )

        self.assertEqual(
            len(result), 3,
            f"expected 3 stored hashes from "
            f"_batch_query_weaviate_content_hashes, got {len(result)}. "
            "If 0, the GraphQL filter is broken (the v0.2.42-v0.2.45 bug). "
            f"Returned dict: {result!r}",
        )
        for fp, ch in seeds:
            self.assertIn(
                fp, result,
                f"missing file_path {fp!r} in result; keys: {list(result.keys())}",
            )
            self.assertEqual(
                result[fp], ch,
                f"hash mismatch for {fp!r}: expected {ch!r}, got {result[fp]!r}",
            )


@unittest.skipUnless(
    _weaviate_reachable(),
    f"Weaviate not reachable at {WEAVIATE_URL}",
)
class V46BLivePruneTest(unittest.TestCase):
    """Live tests for ``install._prune_stale_kg_rows``.

    The function fetches all (uuid, file_path) pairs from Weaviate, then
    checks each ``file_path`` against ``_path_resolves_on_disk`` to
    decide whether to prune. We exercise both branches:
      - stale (file_path doesn't exist) → row should be deleted
      - legitimate (file_path is a real file in this repo) → kept
    """

    def setUp(self) -> None:
        # The function is hard-wired to the orchestrator's ``KG_COLLECTION``
        # name (which the production code passes in). We override that
        # by passing our own test collection name, so the function will
        # operate on it instead.
        self.test_collection = f"V46BTestPrune{uuid.uuid4().hex[:8]}"
        _create_test_collection(self.test_collection)
        time.sleep(0.3)

    def tearDown(self) -> None:
        _delete_test_collection(self.test_collection)

    def test_prune_finds_and_deletes_stale_rows(self) -> None:
        """Seed 3 rows whose file_paths don't exist on disk → all 3 deleted.

        With ``dry_run=False`` the function deletes via Weaviate's batch
        delete endpoint. Count via Aggregate should drop to 0.
        """
        # Use paths that definitely don't resolve on disk (under the test
        # collection's namespace + a UUID-derived suffix).
        nonce = uuid.uuid4().hex[:8]
        stale_fps = [
            f"knowledge/_v46b_stale_test_a_{nonce}.md",
            f"knowledge/_v46b_stale_test_b_{nonce}.md",
            f"knowledge/_v46b_stale_test_c_{nonce}.md",
        ]
        for fp in stale_fps:
            _insert_object(self.test_collection, fp, "stale_hash")
        time.sleep(0.6)

        # Sanity-check the seed.
        seeded_count = _count_objects(self.test_collection)
        self.assertEqual(
            seeded_count, len(stale_fps),
            f"Seed sanity check: expected {len(stale_fps)} rows, "
            f"got {seeded_count}",
        )

        # Run the prune with dry_run=False — this is the destructive path.
        install._prune_stale_kg_rows(
            self.test_collection, WEAVIATE_URL, dry_run=False,
        )
        # Batch-delete is also async; allow it to settle.
        time.sleep(0.6)

        # Count should drop to 0.
        remaining = _count_objects(self.test_collection)
        self.assertEqual(
            remaining, 0,
            f"After prune, expected 0 rows; got {remaining}. "
            "If equal to the seed count, the broken filter prevented "
            "fetch_stored_paths from finding any rows to prune (the "
            "v0.2.43 V0243-6 instance of the bug).",
        )

    def test_prune_keeps_legitimate_rows(self) -> None:
        """Seed 1 row whose file_path exists on disk → must NOT be pruned.

        We pick a stable on-disk file (``install.py``) as the legitimate
        path, plus one stale path. After prune, the legitimate row must
        survive; the stale row must be deleted.
        """
        # ``install.py`` is at the project root — _path_resolves_on_disk
        # will find it via strategy 1 (direct relative-to-PROJECT_ROOT).
        legit_fp = "install.py"
        stale_fp = f"knowledge/_v46b_stale_keep_test_{uuid.uuid4().hex[:8]}.md"

        legit_uuid = _insert_object(
            self.test_collection, legit_fp, "legit_hash",
        )
        _insert_object(self.test_collection, stale_fp, "stale_hash")
        time.sleep(0.6)

        seeded_count = _count_objects(self.test_collection)
        self.assertEqual(seeded_count, 2, "Seed sanity check")

        install._prune_stale_kg_rows(
            self.test_collection, WEAVIATE_URL, dry_run=False,
        )
        time.sleep(0.6)

        # The legitimate row must survive.
        remaining = _count_objects(self.test_collection)
        self.assertEqual(
            remaining, 1,
            f"After prune, expected 1 surviving row (the legitimate "
            f"file_path={legit_fp!r}); got {remaining}. "
            "If 0: the prune wrongly deleted the legitimate row. "
            "If 2: the prune failed to delete the stale row (likely "
            "the v0.2.43 broken-filter bug).",
        )

        # Verify it's the legitimate row that survived (not the stale one).
        try:
            req = urllib.request.Request(
                f"{WEAVIATE_URL}/v1/objects/{self.test_collection}/{legit_uuid}",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                obj = json.loads(resp.read())
            self.assertEqual(
                obj.get("properties", {}).get("file_path"), legit_fp,
                "Surviving row should be the legitimate one",
            )
        except urllib.error.HTTPError as e:
            self.fail(
                f"Legitimate row was deleted (HTTP {e.code} fetching "
                f"uuid={legit_uuid}). The prune should never delete rows "
                "whose file_path resolves on disk.",
            )

    def test_prune_dry_run_does_not_delete(self) -> None:
        """``dry_run=True`` must not actually delete any rows.

        Defensive: this protects against future refactors that
        accidentally drop the early-return guard.
        """
        nonce = uuid.uuid4().hex[:8]
        stale_fps = [
            f"knowledge/_v46b_dry_test_{nonce}_a.md",
            f"knowledge/_v46b_dry_test_{nonce}_b.md",
        ]
        for fp in stale_fps:
            _insert_object(self.test_collection, fp, "stale_hash")
        time.sleep(0.6)

        seeded_count = _count_objects(self.test_collection)
        self.assertEqual(seeded_count, len(stale_fps), "Seed sanity check")

        install._prune_stale_kg_rows(
            self.test_collection, WEAVIATE_URL, dry_run=True,
        )
        time.sleep(0.4)

        remaining = _count_objects(self.test_collection)
        self.assertEqual(
            remaining, len(stale_fps),
            f"dry_run=True must not delete; expected {len(stale_fps)} "
            f"rows still present, got {remaining}",
        )


class V46BSourceRegressionGuardTest(unittest.TestCase):
    """Source-inspection regression guards (no Weaviate required).

    These tests run in EVERY environment, including CI machines without
    a live Weaviate. They scan ``install.py``'s source for the literal
    SQL-wildcard filter pattern that caused the v0.2.42-v0.2.45 bug,
    and fail loudly if it's ever reintroduced.

    Why a separate class: the live tests above are gated on Weaviate
    reachability via ``@skipUnless(_weaviate_reachable(), ...)``. The
    source-only guards don't need Weaviate and must run unconditionally,
    otherwise a CI environment that lacks Weaviate would silently skip
    the regression check too — exactly the failure mode this whole
    fanout is designed to prevent.
    """

    @staticmethod
    def _has_broken_filter(source: str) -> bool:
        """Detect ``where: Like "%"`` (raw or backslash-escaped form).

        ``inspect.getsource`` returns pre-evaluation source text, so
        f-string-embedded ``"%"`` looks like ``\\"%\\"`` (escaped
        backslashes) in what we receive. The regex tolerates both.
        """
        import re
        has_like = (
            "operator: Like" in source
            or '"operator": "Like"' in source
        )
        # Match valueText: "%"  or  valueText: \"%\"  (escaped f-string form).
        pct_pattern = re.compile(r'valueText\s*:\s*\\?"%\\?"')
        return has_like and bool(pct_pattern.search(source))

    def test_batch_query_does_not_contain_broken_filter(self) -> None:
        """Defensive: ensure _batch_query_weaviate_content_hashes never
        reintroduces the v0.2.42 bug.

        ``Like`` + ``valueText: "%"`` is the SQL-wildcard convention that
        Weaviate's BM25 tokenizer rejects (returns 0 hits silently).
        Any fix MUST use a different filter — typically no filter at all
        (an unfiltered Get returns every row up to ``limit``) or
        ``IsNotNull``.
        """
        import inspect
        src = inspect.getsource(install._batch_query_weaviate_content_hashes)
        self.assertFalse(
            self._has_broken_filter(src),
            "REGRESSION: _batch_query_weaviate_content_hashes reintroduced "
            "the v0.2.42 SQL-wildcard filter ``where: Like \"%\"``. "
            "This filter silently returns 0 hits against any populated "
            "Weaviate collection (BM25 tokenizer rejects '%' as a "
            "stopword). See knowledge/concepts/"
            "silent-zero-fallback-antipattern.md § \"Real instance #3\".",
        )

    def test_prune_does_not_contain_broken_filter(self) -> None:
        """Defensive: same regression guard as the diff-gate test, for prune.

        v0.2.43 V0243-6 copy-pasted the broken filter from CI-10 into
        _prune_stale_kg_rows. Both must stay clean forever.
        """
        import inspect
        src = inspect.getsource(install._prune_stale_kg_rows)
        self.assertFalse(
            self._has_broken_filter(src),
            "REGRESSION: _prune_stale_kg_rows reintroduced the v0.2.43 "
            "SQL-wildcard filter. This filter silently returns 0 hits "
            "against populated collections, causing prune to be a no-op. "
            "See knowledge/concepts/silent-zero-fallback-antipattern.md "
            "§ \"Real instance #3\".",
        )

    # v0.2.46 KG-AUTO-HEAL-E: extension to cover the refactored helper.
    #
    # After v0.2.46 the V46-A safety triad lives in
    # ``vco_lib.kg_sync.batch_query_content_hashes`` — the install.py
    # function ``_batch_query_weaviate_content_hashes`` is now a 3-line
    # wrapper that delegates to it. The original
    # ``test_batch_query_does_not_contain_broken_filter`` above still
    # runs and passes, BUT it would now pass VACUOUSLY (the wrapper
    # doesn't contain GraphQL at all). Without this extension, a future
    # refactor that drops the safety triad in ``vco_lib/kg_sync.py``
    # would silently re-introduce the v0.2.42 bug.
    #
    # The V46 audit (`.claude/context/audits/v0.2.46-compat-V46-2026-06-04.md`)
    # flagged this as watch-out #2: "the guard passes vacuously while
    # the new helper in vco_lib/kg_sync.py is unguarded". This test
    # closes that gap.

    def test_vco_lib_kg_sync_does_not_contain_broken_filter(self) -> None:
        """v0.2.46 KG-AUTO-HEAL-E: extend the V46-B regression guard to
        cover the new ``vco_lib.kg_sync.batch_query_content_hashes``
        helper. After the v0.2.46 refactor, this helper hosts the V46-A
        safety triad (no Like-% / limit:10000 / errors-before-data /
        saturation warning) that install.py's
        ``_batch_query_weaviate_content_hashes`` used to host inline.

        The old install.py guard above still passes (the wrapper has no
        GraphQL) but is now vacuous — this is the real guard.
        """
        import inspect

        import vco_lib.kg_sync

        src = inspect.getsource(vco_lib.kg_sync.batch_query_content_hashes)
        self.assertFalse(
            self._has_broken_filter(src),
            "REGRESSION: vco_lib.kg_sync.batch_query_content_hashes "
            "reintroduced the v0.2.42 SQL-wildcard filter "
            "``where: Like \"%\"``. This filter silently returns 0 hits "
            "against any populated Weaviate collection (BM25 tokenizer "
            "rejects '%' as a stopword). See knowledge/concepts/"
            "silent-zero-fallback-antipattern.md § \"Real instance #3\".",
        )

    def test_vco_lib_kg_sync_uses_v46f_post_graphql_safe(self) -> None:
        """The helper MUST route via ``vco_lib.weaviate_helpers.post_graphql_safe``
        (V46-F), NOT open-coded urllib.urlopen. The V46-F path inspects
        ``body['errors']`` BEFORE consuming ``data`` — the load-bearing
        safety property. A regression where the helper bypasses V46-F
        and rolls its own ``urlopen`` loop would re-open the silent-zero
        fallback bug class.
        """
        import inspect

        import vco_lib.kg_sync

        src = inspect.getsource(vco_lib.kg_sync.batch_query_content_hashes)
        self.assertIn(
            "post_graphql_safe",
            src,
            "REGRESSION: vco_lib.kg_sync.batch_query_content_hashes no "
            "longer calls post_graphql_safe — the V46-F errors-array "
            "gate has been bypassed. Restore the call OR add an "
            "equivalent inline ``body.get('errors')`` check BEFORE any "
            "``data`` consumption.",
        )
        # Belt-and-suspenders: ensure the legacy raw-urlopen path didn't
        # creep back in.
        self.assertNotIn(
            "urlopen",
            src,
            "REGRESSION: vco_lib.kg_sync.batch_query_content_hashes "
            "appears to use raw urllib.urlopen. This bypasses V46-F's "
            "errors-array gate. Route via post_graphql_safe instead.",
        )

    def test_vco_lib_kg_sync_keeps_limit_10000(self) -> None:
        """Saturation cap: pre-v0.2.46 used ``limit: 1000`` which silently
        truncated the user's 1193-row VCO_dev collection. V46-A bumped
        to 10000 (Weaviate's QUERY_MAXIMUM_RESULTS default). Any value
        smaller than 10000 in this helper's source is a regression.

        v0.2.46 KG-AUTO-HEAL adversarial-review H1 follow-up: the
        previous form of this test (``"10000" in src or "QUERY_MAX_LIMIT"
        in src``) was VACUOUS — the function's docstring mentions both
        magic strings + the saturation check has its own
        ``QUERY_MAX_LIMIT`` reference unrelated to the GraphQL limit
        clause. A regression that dropped the GraphQL ``limit: ...``
        value to 100 passed the old form. This tightened version
        captures the value INSIDE the actual GraphQL query string and
        asserts it's ``>= 10000``.
        """
        import inspect
        import re

        import vco_lib.kg_sync

        src = inspect.getsource(vco_lib.kg_sync.batch_query_content_hashes)

        # Capture the limit value inside the ACTUAL GraphQL query string,
        # not the docstring. The query construction looks like:
        #   gql = {
        #       "query": (
        #           f"{{ Get {{ {collection_name}(limit: {QUERY_MAX_LIMIT}) "
        #           f"{{ file_path content_hash }} }} }}"
        #       ),
        #   }
        # So we narrow to the literal ``(limit: ...)`` pattern — the docstring's
        # plain ``limit: 10000`` reference (without parentheses, without the
        # collection-name preamble) won't match this pattern.
        m = re.search(
            r"\(limit:\s*(?:\{?(?P<const>QUERY_MAX_LIMIT)\}?|(?P<digits>\d+))",
            src,
        )
        self.assertIsNotNone(
            m,
            "REGRESSION: vco_lib.kg_sync.batch_query_content_hashes "
            "no longer emits a ``limit: ...`` clause in its GraphQL "
            "query string. Anything missing this clause silently uses "
            "Weaviate's default limit (100) and truncates collections "
            ">100 objects.",
        )
        # If the captured group is the constant, walk the module to
        # resolve its value at runtime — pins the actual numeric.
        if m.group("const"):
            self.assertEqual(
                vco_lib.kg_sync.QUERY_MAX_LIMIT,
                10000,
                f"REGRESSION: QUERY_MAX_LIMIT = {vco_lib.kg_sync.QUERY_MAX_LIMIT}, "
                "expected 10000 (Weaviate's QUERY_MAXIMUM_RESULTS default). "
                "Anything smaller silently truncates collections >N objects.",
            )
        else:
            value = int(m.group("digits"))
            self.assertGreaterEqual(
                value,
                10000,
                f"REGRESSION: vco_lib.kg_sync.batch_query_content_hashes "
                f"emits ``limit: {value}`` which is < 10000 (Weaviate's "
                f"QUERY_MAXIMUM_RESULTS default). Anything smaller silently "
                f"truncates collections >{value} objects.",
            )


if __name__ == "__main__":
    unittest.main()
