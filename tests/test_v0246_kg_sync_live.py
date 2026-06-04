# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Live integration tests for ``vco_lib.kg_sync.batch_query_content_hashes``
against a real Weaviate. v0.2.46 KG-AUTO-HEAL-E.

WHY THIS TEST EXISTS:
The v0.2.46 refactor extracted the V46-A-hardened hash-fetch path from
``install.py::_batch_query_weaviate_content_hashes`` into
``vco_lib.kg_sync.batch_query_content_hashes``. Every unit test for the
new helper mocks ``post_graphql_safe``, so they're network-free but
also don't catch "I broke the wire format when going from f-string to
``vco_lib.weaviate_helpers``" class of bugs.

The V46-B sibling test (``test_v0246_v46b_live_ci10_diff_gate.py``)
does exactly this for the LEGACY function. This test mirrors that
shape for the NEW one — they will catch the same class of bug.

Skips cleanly when Weaviate is unreachable (CI without infrastructure).
On a developer machine with Weaviate running, exercises the full
round-trip:

  1. Create a test collection with the canonical schema.
  2. Insert known objects with known ``content_hash`` values.
  3. Call ``vco_lib.kg_sync.batch_query_content_hashes`` (the NEW path).
  4. Assert the returned ``{file_path -> content_hash}`` matches what
     we inserted.

Cross-references:
- ``.claude/context/audits/v0.2.46-compat-V46-2026-06-04.md`` watch-out #2
- ``vco_lib/kg_sync.py`` (the function under test)
- ``knowledge/concepts/silent-zero-fallback-antipattern.md`` § "Real instance #3"
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vco_lib.kg_sync import batch_query_content_hashes  # noqa: E402

WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "http://localhost:8081")


def _weaviate_reachable() -> bool:
    """Returns True if Weaviate is reachable on the configured URL."""
    try:
        req = urllib.request.Request(f"{WEAVIATE_URL}/v1/meta")
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError):
        return False


def _create_test_collection(name: str) -> None:
    """Create a minimal-schema Weaviate class for the test."""
    schema = {
        "class": name,
        "vectorizer": "none",
        "properties": [
            {"name": "file_path", "dataType": ["text"]},
            {"name": "content_hash", "dataType": ["text"]},
        ],
    }
    req = urllib.request.Request(
        f"{WEAVIATE_URL}/v1/schema",
        data=json.dumps(schema).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            assert resp.status == 200, f"create({name}): {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code == 422:
            # Already exists; recreate.
            _delete_test_collection(name)
            time.sleep(0.2)
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                assert resp.status == 200, f"recreate({name}): {resp.status}"
        else:
            raise


def _delete_test_collection(name: str) -> None:
    """Drop the test class. Idempotent: 404 is treated as success."""
    req = urllib.request.Request(
        f"{WEAVIATE_URL}/v1/schema/{name}",
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            assert resp.status in (200, 204), f"delete({name}): {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


def _insert_object(collection: str, file_path: str, content_hash: str) -> None:
    obj = {
        "class": collection,
        "properties": {"file_path": file_path, "content_hash": content_hash},
    }
    req = urllib.request.Request(
        f"{WEAVIATE_URL}/v1/objects",
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        assert resp.status == 200, f"insert({collection}/{file_path}): {resp.status}"


@unittest.skipUnless(
    _weaviate_reachable(),
    f"Weaviate not reachable at {WEAVIATE_URL}; live test skipped",
)
class KgSyncLiveDiffGateTest(unittest.TestCase):
    """Live tests for ``vco_lib.kg_sync.batch_query_content_hashes``.

    Same shape as ``test_v0246_v46b_live_ci10_diff_gate.py``, but
    exercises the new vco_lib helper. Both must pass on any post-v0.2.46
    machine with Weaviate running.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.collection = f"VCOTest_kg_sync_live_{uuid.uuid4().hex[:8]}"
        _create_test_collection(cls.collection)
        _insert_object(cls.collection, "a.md", "hash_a")
        _insert_object(cls.collection, "b.md", "hash_b")
        _insert_object(cls.collection, "c.md", "hash_c")
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls) -> None:
        _delete_test_collection(cls.collection)

    def test_live_round_trip_returns_known_hashes(self) -> None:
        """Insert 3 rows; the helper returns all 3 mappings intact."""
        result = batch_query_content_hashes(WEAVIATE_URL, self.collection)
        self.assertEqual(result.get("a.md"), "hash_a")
        self.assertEqual(result.get("b.md"), "hash_b")
        self.assertEqual(result.get("c.md"), "hash_c")
        self.assertEqual(
            len(result),
            3,
            f"expected exactly 3 entries, got {len(result)}: {result}",
        )

    def test_live_unknown_collection_returns_empty_dict(self) -> None:
        """Unknown class -> {} (full-sync semantic), never raises."""
        unknown = f"NoSuch_{uuid.uuid4().hex[:8]}"
        result = batch_query_content_hashes(WEAVIATE_URL, unknown)
        self.assertEqual(result, {})

    def test_live_warn_callback_fires_on_unknown_collection(self) -> None:
        """on_warn must fire so the caller can route to a deferral path."""
        unknown = f"NoSuch_{uuid.uuid4().hex[:8]}"
        warns: list[tuple[str, dict]] = []
        result = batch_query_content_hashes(
            WEAVIATE_URL,
            unknown,
            on_warn=lambda c, d: warns.append((c, d)),
        )
        self.assertEqual(result, {})
        self.assertTrue(
            warns,
            "on_warn must fire for unknown collections; got no callbacks",
        )

    def test_live_match_parity_with_legacy_install_wrapper(self) -> None:
        """install.py wrapper and new helper must return identical results."""
        import install
        new_helper = batch_query_content_hashes(WEAVIATE_URL, self.collection)
        legacy_wrapper = install._batch_query_weaviate_content_hashes(
            self.collection, WEAVIATE_URL,
        )
        self.assertEqual(
            new_helper,
            legacy_wrapper,
            "Helper and wrapper must return identical results; the "
            "wrapper is supposed to be a thin pass-through.",
        )


if __name__ == "__main__":
    unittest.main()
