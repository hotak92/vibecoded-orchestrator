# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for vco_lib.embedding_enrichment (v0.2.18 Commit 9).

Coverage:
  * Empty collection → zero report, no embed calls.
  * All objects already have target slot → all skipped, no embed calls.
  * Some objects missing slot → those get enriched, others skipped.
  * Idempotency — second run on same data is full-skipped.
  * Per-object embed failure → continues + logs, doesn't abort.
  * Per-object write failure → continues + logs.
  * Dry-run mode → no writes, reports the would-have-enriched count.
  * Pre-flight errors — UnknownSlot, SlotNotInSchema, NoEmbeddingBackend.
  * No-op fast path when new_slot == active_slot.
  * KG-shaped uses `content` property; code-shaped uses
    `function_body` / `class_body` / `module_summary` / etc.
  * progress_callback invoked per batch.
  * Batch size honoured at BATCH_SIZE=100.
  * Failure detail list capped at MAX_FAILURE_DETAILS.

All tests run with the Weaviate client mocked at the v4-collection level
(`collections.get`) — no live Weaviate needed. EmbeddingService is also
mocked so tests don't touch Ollama / CodeEmbed / OpenAI.

A `LiveEnrichmentTest` block exercises the full path against a running
Weaviate; self-skips when unreachable, same pattern as
`test_weaviate_schema.py::LiveSchemaMigrationTest`.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import embedding_enrichment as ee  # noqa: E402
from vco_lib.embedding_service import NoEmbeddingBackendError  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeObject:
    """Stand-in for a v4 Weaviate ``DataObject``."""

    def __init__(
        self,
        uuid: str,
        properties: dict,
        vector: Optional[dict] = None,
    ):
        self.uuid = uuid
        self.properties = properties
        self.vector = vector or {}


class _FakeCollection:
    """Stand-in for ``client.collections.get(name)`` return value.

    Exposes ``iterator(include_vector=True)`` and ``data.update(...)``.
    Records every update so tests can assert the slot-specific write.
    """

    def __init__(self, objects: list[_FakeObject]):
        # We don't mutate the input list — copy + snapshot.
        self._objects = {o.uuid: o for o in objects}
        self.updates: list[dict] = []
        self.update_should_fail_for_uuid: set[str] = set()
        # The DataAccessor sub-namespace
        self.data = self  # so `.data.update(...)` works

    # ── iterator API (mimics v4) ────────────────────────────────────
    def iterator(self, include_vector: bool = False):
        # Always emit copies so a per-batch mutation doesn't surprise
        # the next iteration. The real v4 iterator is similarly fresh.
        for obj in self._objects.values():
            yield _FakeObject(
                uuid=obj.uuid,
                properties=dict(obj.properties),
                vector=dict(obj.vector) if obj.vector else {},
            )

    # ── data.update API ─────────────────────────────────────────────
    def update(self, *, uuid, vector):
        if uuid in self.update_should_fail_for_uuid:
            raise RuntimeError("simulated write failure")
        # Merge the vector dict; never delete other slots. Matches
        # Weaviate's documented "partial update" semantics.
        existing = self._objects[uuid].vector or {}
        merged = dict(existing)
        merged.update(vector)
        self._objects[uuid].vector = merged
        self.updates.append({"uuid": uuid, "vector": dict(vector)})

    # ── close (called by the enrichment loop's finally) ─────────────
    def close(self):
        pass


class _FakeClient:
    """Stand-in for the v4 WeaviateClient."""

    def __init__(self, collections: dict[str, _FakeCollection]):
        self._collections = collections
        self.collections = self  # so `.collections.get(...)` works

    def get(self, name: str) -> _FakeCollection:
        return self._collections[name]

    def close(self):
        pass


class _FakeEmbeddingService:
    """Mocked EmbeddingService with scriptable text/code batch returns.

    Records every batch call. Tests inject a ``response_factory`` to
    control what each batch returns, including raising for error-path
    coverage.
    """

    def __init__(
        self,
        *,
        text_vector_slot: str = "qwen3_embed",
        code_vector_slot: str = "codesage_embed",
        text_ready: bool = True,
        code_ready: bool = True,
        embed_dim: int = 8,
        text_response_factory=None,
        code_response_factory=None,
        project_root: Optional[Path] = None,
    ):
        self.text_vector_slot = text_vector_slot
        self.code_vector_slot = code_vector_slot
        self._text_ready = text_ready
        self._code_ready = code_ready
        self._embed_dim = embed_dim
        self._text_response_factory = text_response_factory
        self._code_response_factory = code_response_factory
        self.project_root = project_root
        # Records: list[list[str]] — one entry per batch call.
        self.text_batch_calls: list[list[str]] = []
        self.code_batch_calls: list[list[str]] = []
        self.closed = False

    def text_backend_ready(self) -> bool:
        return self._text_ready

    def code_backend_ready(self) -> bool:
        return self._code_ready

    def embed_text_batch(self, texts: list[str]) -> list[list[float]]:
        self.text_batch_calls.append(list(texts))
        if self._text_response_factory is not None:
            return self._text_response_factory(texts)
        return [[float(i)] * self._embed_dim for i, _ in enumerate(texts)]

    def embed_code_batch(self, codes: list[str]) -> list[list[float]]:
        self.code_batch_calls.append(list(codes))
        if self._code_response_factory is not None:
            return self._code_response_factory(codes)
        return [[float(i)] * self._embed_dim for i, _ in enumerate(codes)]

    def close(self) -> None:
        self.closed = True


def _fake_schema(slot_names: list[str], class_name: str) -> dict:
    """Build the dict returned by ``_http_get_schema``."""
    return {
        "class": class_name,
        "vectorConfig": {
            n: {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"}
            for n in slot_names
        },
        "properties": [],
    }


# ---------------------------------------------------------------------------
# Patch helpers — common setup. Pre-flight always touches the live HTTP
# schema + GraphQL count endpoints; we stub both to avoid network calls.
# ---------------------------------------------------------------------------


class _BaseEnrichmentTest(unittest.TestCase):
    """Mixin that stubs the HTTP probes used by the pre-flight phase."""

    KG_SLOTS = ["qwen3_embed", "arctic2_embed", "openai_text_embed"]
    CODE_SLOTS = ["codesage_embed", "jina_embed", "openai_code_embed", "qwen3_embed"]
    KG_COLL = "TestKG"
    CODE_COLL = "TestCodeFunction"

    def setUp(self):
        # Patch the schema fetch + count estimator.
        self._schema_patch = mock.patch.object(
            ee, "_http_get_schema",
        )
        self._count_patch = mock.patch.object(
            ee, "_estimate_object_count", return_value=0,
        )
        self._schema_mock = self._schema_patch.start()
        self._schema_mock.side_effect = lambda c, base_url: _fake_schema(
            self.CODE_SLOTS if c == self.CODE_COLL else self.KG_SLOTS,
            class_name=c,
        )
        self._count_mock = self._count_patch.start()

    def tearDown(self):
        self._schema_patch.stop()
        self._count_patch.stop()


# ---------------------------------------------------------------------------
# Empty / no-op paths
# ---------------------------------------------------------------------------


class EmptyAndNoopTests(_BaseEnrichmentTest):
    def test_empty_collection_returns_zero_report(self):
        client = _FakeClient({self.KG_COLL: _FakeCollection([])})
        svc = _FakeEmbeddingService()

        report = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )

        self.assertEqual(report.total, 0)
        self.assertEqual(report.enriched, 0)
        self.assertEqual(report.skipped, 0)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.failures, [])
        # Zero embed calls — empty collection means nothing to embed.
        self.assertEqual(svc.text_batch_calls, [])

    def test_all_objects_have_slot_returns_skipped_total(self):
        objs = [
            _FakeObject(
                uuid=f"u{i}",
                properties={"content": f"body-{i}"},
                vector={
                    "qwen3_embed": [0.1] * 4,
                    "arctic2_embed": [0.2] * 4,
                },
            )
            for i in range(5)
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.KG_COLL: collection})
        svc = _FakeEmbeddingService()

        report = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )

        self.assertEqual(report.total, 5)
        self.assertEqual(report.enriched, 0)
        self.assertEqual(report.skipped, 5)
        self.assertEqual(report.failed, 0)
        # No embed calls — every object already had the slot.
        self.assertEqual(svc.text_batch_calls, [])
        # No writes either.
        self.assertEqual(collection.updates, [])

    def test_no_op_when_slot_already_active(self):
        """When new_slot matches active text slot, the walk runs but the
        report carries a clear no-op message; enriched=0 because all
        objects already have the slot (a real machine-state of "user
        clicked Save without actually changing the model")."""
        objs = [
            _FakeObject(
                uuid=f"u{i}",
                properties={"content": f"body-{i}"},
                vector={"qwen3_embed": [0.1] * 4},
            )
            for i in range(3)
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.KG_COLL: collection})
        # The KG_COLL schema has qwen3_embed → use it as new_slot AND
        # mark it as active in the service.
        svc = _FakeEmbeddingService(text_vector_slot="qwen3_embed")

        report = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="qwen3_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        # Every object already has qwen3_embed, so every one skips.
        self.assertEqual(report.skipped, 3)
        self.assertEqual(report.enriched, 0)
        self.assertEqual(report.total, 3)


# ---------------------------------------------------------------------------
# Happy path / idempotency
# ---------------------------------------------------------------------------


class EnrichAndIdempotencyTests(_BaseEnrichmentTest):
    def test_objects_missing_slot_get_enriched(self):
        objs = [
            _FakeObject(
                uuid=f"u{i}",
                properties={"content": f"body-{i}"},
                vector={"qwen3_embed": [0.1] * 4},
            )
            for i in range(5)
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.KG_COLL: collection})
        svc = _FakeEmbeddingService()

        report = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )

        self.assertEqual(report.total, 5)
        self.assertEqual(report.enriched, 5)
        self.assertEqual(report.skipped, 0)
        self.assertEqual(report.failed, 0)
        # One batch of 5 → one embed call.
        self.assertEqual(len(svc.text_batch_calls), 1)
        self.assertEqual(len(svc.text_batch_calls[0]), 5)
        # 5 writes, each targeting the new slot only.
        self.assertEqual(len(collection.updates), 5)
        for upd in collection.updates:
            self.assertEqual(list(upd["vector"].keys()), ["arctic2_embed"])

    def test_idempotency_second_run_does_nothing(self):
        objs = [
            _FakeObject(
                uuid=f"u{i}",
                properties={"content": f"body-{i}"},
                vector={"qwen3_embed": [0.1] * 4},
            )
            for i in range(3)
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.KG_COLL: collection})
        svc = _FakeEmbeddingService()

        # First run: enriches all 3.
        ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        self.assertEqual(len(collection.updates), 3)

        # Second run: every object now has arctic2_embed → all skipped.
        svc2 = _FakeEmbeddingService()
        report2 = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc2,
            weaviate_client_factory=lambda: client,
        )
        self.assertEqual(report2.skipped, 3)
        self.assertEqual(report2.enriched, 0)
        self.assertEqual(svc2.text_batch_calls, [])

    def test_mixed_skip_and_enrich(self):
        """Some objects already have the slot; others don't."""
        objs = [
            _FakeObject(
                uuid="u0",
                properties={"content": "alpha"},
                vector={"qwen3_embed": [0.1] * 4},
            ),
            _FakeObject(
                uuid="u1",
                properties={"content": "beta"},
                vector={
                    "qwen3_embed": [0.1] * 4,
                    "arctic2_embed": [0.5] * 4,
                },
            ),
            _FakeObject(
                uuid="u2",
                properties={"content": "gamma"},
                vector={"qwen3_embed": [0.1] * 4},
            ),
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.KG_COLL: collection})
        svc = _FakeEmbeddingService()

        report = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        self.assertEqual(report.total, 3)
        self.assertEqual(report.enriched, 2)  # u0, u2
        self.assertEqual(report.skipped, 1)   # u1
        self.assertEqual(report.failed, 0)


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class FailurePathTests(_BaseEnrichmentTest):
    def test_per_object_embed_failure_continues_and_logs(self):
        """When the batch embed call raises, every uuid in the batch
        gets marked failed; the next batch still runs."""
        objs = [
            _FakeObject(
                uuid=f"u{i}",
                properties={"content": f"body-{i}"},
                vector={"qwen3_embed": [0.1] * 4},
            )
            for i in range(3)
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.KG_COLL: collection})

        def _factory(texts):
            raise RuntimeError("synthetic embed failure")

        svc = _FakeEmbeddingService(text_response_factory=_factory)
        report = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        self.assertEqual(report.total, 3)
        self.assertEqual(report.failed, 3)
        self.assertEqual(report.enriched, 0)
        # Failure rows captured (within cap).
        self.assertEqual(len(report.failures), 3)
        for f in report.failures:
            self.assertIn("synthetic embed failure", f["error"])
            self.assertIn("uuid", f)
        # No writes happened.
        self.assertEqual(collection.updates, [])

    def test_per_object_write_failure_continues_and_logs(self):
        """When ``data.update`` raises for one uuid, the others still
        get written."""
        objs = [
            _FakeObject(
                uuid=f"u{i}",
                properties={"content": f"body-{i}"},
                vector={"qwen3_embed": [0.1] * 4},
            )
            for i in range(3)
        ]
        collection = _FakeCollection(objs)
        collection.update_should_fail_for_uuid = {"u1"}
        client = _FakeClient({self.KG_COLL: collection})
        svc = _FakeEmbeddingService()

        report = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        self.assertEqual(report.enriched, 2)
        self.assertEqual(report.failed, 1)
        # The successful writes are the two non-u1 objects.
        written_uuids = {u["uuid"] for u in collection.updates}
        self.assertEqual(written_uuids, {"u0", "u2"})

    def test_empty_content_counts_as_skipped(self):
        """Objects with empty content can't be embedded — they skip."""
        objs = [
            _FakeObject(
                uuid="u0",
                properties={"content": ""},
                vector={"qwen3_embed": [0.1] * 4},
            ),
            _FakeObject(
                uuid="u1",
                properties={"content": "   "},  # whitespace only
                vector={"qwen3_embed": [0.1] * 4},
            ),
            _FakeObject(
                uuid="u2",
                properties={"content": "real content"},
                vector={"qwen3_embed": [0.1] * 4},
            ),
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.KG_COLL: collection})
        svc = _FakeEmbeddingService()
        report = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        self.assertEqual(report.skipped, 2)   # u0, u1
        self.assertEqual(report.enriched, 1)  # u2
        self.assertEqual(report.failed, 0)

    def test_failure_details_capped_at_max(self):
        """When failures exceed MAX_FAILURE_DETAILS, the integer count
        keeps growing but the detail list stops."""
        count = ee.MAX_FAILURE_DETAILS + 5
        objs = [
            _FakeObject(
                uuid=f"u{i}",
                properties={"content": f"body-{i}"},
                vector={"qwen3_embed": [0.1] * 4},
            )
            for i in range(count)
        ]
        collection = _FakeCollection(objs)
        # Every write fails.
        collection.update_should_fail_for_uuid = {o.uuid for o in objs}
        client = _FakeClient({self.KG_COLL: collection})
        svc = _FakeEmbeddingService()
        report = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        self.assertEqual(report.failed, count)
        self.assertLessEqual(len(report.failures), ee.MAX_FAILURE_DETAILS)


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


class DryRunTests(_BaseEnrichmentTest):
    def test_dry_run_does_not_write(self):
        objs = [
            _FakeObject(
                uuid=f"u{i}",
                properties={"content": f"body-{i}"},
                vector={"qwen3_embed": [0.1] * 4},
            )
            for i in range(4)
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.KG_COLL: collection})
        svc = _FakeEmbeddingService()
        report = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
            dry_run=True,
        )
        self.assertEqual(report.total, 4)
        self.assertEqual(report.enriched, 0)
        # Failures[0] is the dry-run sentinel.
        self.assertEqual(report.failures[0]["dry_run_count"], 4)
        # No writes, no embed calls.
        self.assertEqual(collection.updates, [])
        self.assertEqual(svc.text_batch_calls, [])


# ---------------------------------------------------------------------------
# Pre-flight error paths
# ---------------------------------------------------------------------------


class PreflightTests(_BaseEnrichmentTest):
    def test_unknown_slot_raises_UnknownSlotError(self):
        client = _FakeClient({self.KG_COLL: _FakeCollection([])})
        svc = _FakeEmbeddingService()
        with self.assertRaises(ee.UnknownSlotError) as ctx:
            ee.enrich_collection_vectors(
                collection_name=self.KG_COLL,
                new_slot="not_a_real_slot",
                embedding_service=svc,
                weaviate_client_factory=lambda: client,
            )
        self.assertIn("not in the kg catalog", str(ctx.exception))

    def test_slot_not_in_schema_raises_SlotNotInSchemaError(self):
        # Server returns a schema lacking arctic2_embed. The catalog
        # knows arctic2_embed exists; the live schema doesn't.
        self._schema_mock.side_effect = lambda c, base_url: _fake_schema(
            ["qwen3_embed"], class_name=c,
        )
        client = _FakeClient({self.KG_COLL: _FakeCollection([])})
        svc = _FakeEmbeddingService()
        with self.assertRaises(ee.SlotNotInSchemaError) as ctx:
            ee.enrich_collection_vectors(
                collection_name=self.KG_COLL,
                new_slot="arctic2_embed",
                embedding_service=svc,
                weaviate_client_factory=lambda: client,
            )
        self.assertIn("migrate-collections", str(ctx.exception))

    def test_collection_not_found_raises_CollectionNotFoundError(self):
        self._schema_mock.side_effect = lambda c, base_url: None
        client = _FakeClient({})
        svc = _FakeEmbeddingService()
        with self.assertRaises(ee.CollectionNotFoundError):
            ee.enrich_collection_vectors(
                collection_name="NotThere",
                new_slot="arctic2_embed",
                embedding_service=svc,
                weaviate_client_factory=lambda: client,
            )

    def test_no_embedding_backend_raises_NoEmbeddingBackendError(self):
        client = _FakeClient({self.KG_COLL: _FakeCollection([])})
        svc = _FakeEmbeddingService(text_ready=False)
        with self.assertRaises(NoEmbeddingBackendError):
            ee.enrich_collection_vectors(
                collection_name=self.KG_COLL,
                new_slot="arctic2_embed",
                embedding_service=svc,
                weaviate_client_factory=lambda: client,
            )


# ---------------------------------------------------------------------------
# Content-property resolution by collection shape
# ---------------------------------------------------------------------------


class ContentPropertyTests(_BaseEnrichmentTest):
    def test_kg_uses_content_property_for_embed_input(self):
        objs = [
            _FakeObject(
                uuid="u0",
                properties={"content": "the kg body"},
                vector={"qwen3_embed": [0.1] * 4},
            ),
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.KG_COLL: collection})
        svc = _FakeEmbeddingService()
        ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        # The text fed to embed_text_batch is the `content` property.
        self.assertEqual(svc.text_batch_calls, [["the kg body"]])

    def test_code_function_uses_function_body_property(self):
        objs = [
            _FakeObject(
                uuid="u0",
                properties={"function_body": "def foo(): pass"},
                vector={"codesage_embed": [0.1] * 4},
            ),
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.CODE_COLL: collection})
        svc = _FakeEmbeddingService()
        ee.enrich_collection_vectors(
            collection_name=self.CODE_COLL,
            new_slot="jina_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        # Code path → embed_code_batch (not text).
        self.assertEqual(svc.code_batch_calls, [["def foo(): pass"]])
        self.assertEqual(svc.text_batch_calls, [])

    def test_code_class_uses_class_body_property(self):
        coll_name = "TestCodeClass"
        # Patch schema fetch for THIS class name.
        self._schema_mock.side_effect = lambda c, base_url: _fake_schema(
            self.CODE_SLOTS, class_name=c,
        )
        objs = [
            _FakeObject(
                uuid="u0",
                properties={"class_body": "class Foo: pass"},
                vector={"codesage_embed": [0.1] * 4},
            ),
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({coll_name: collection})
        svc = _FakeEmbeddingService()
        ee.enrich_collection_vectors(
            collection_name=coll_name,
            new_slot="jina_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        self.assertEqual(svc.code_batch_calls, [["class Foo: pass"]])

    def test_code_module_uses_module_summary_property(self):
        coll_name = "TestCodeModule"
        self._schema_mock.side_effect = lambda c, base_url: _fake_schema(
            self.CODE_SLOTS, class_name=c,
        )
        objs = [
            _FakeObject(
                uuid="u0",
                properties={"module_summary": "Module summary text"},
                vector={"codesage_embed": [0.1] * 4},
            ),
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({coll_name: collection})
        svc = _FakeEmbeddingService()
        ee.enrich_collection_vectors(
            collection_name=coll_name,
            new_slot="jina_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        self.assertEqual(svc.code_batch_calls, [["Module summary text"]])


# ---------------------------------------------------------------------------
# Progress callback + batch sizing
# ---------------------------------------------------------------------------


class ProgressAndBatchTests(_BaseEnrichmentTest):
    def test_progress_callback_invoked_per_batch(self):
        # 5 objects, BATCH_SIZE = 100 default → one batch → at least one
        # progress callback at the end. Force a small batch size to
        # exercise the per-batch path.
        with mock.patch.object(ee, "BATCH_SIZE", 2):
            objs = [
                _FakeObject(
                    uuid=f"u{i}",
                    properties={"content": f"body-{i}"},
                    vector={"qwen3_embed": [0.1] * 4},
                )
                for i in range(5)
            ]
            self._count_mock.return_value = 5  # estimate available
            collection = _FakeCollection(objs)
            client = _FakeClient({self.KG_COLL: collection})
            svc = _FakeEmbeddingService()
            calls: list[tuple[float, str]] = []
            ee.enrich_collection_vectors(
                collection_name=self.KG_COLL,
                new_slot="arctic2_embed",
                embedding_service=svc,
                weaviate_client_factory=lambda: client,
                progress_callback=lambda p, m: calls.append((p, m)),
            )
            # Three batches (2,2,1) → three flush events + final emit.
            self.assertGreaterEqual(len(calls), 3)
            # Final % should be 1.0 (or very close).
            self.assertAlmostEqual(calls[-1][0], 1.0, places=2)
            # Messages mention "Enriched".
            self.assertIn("Enriched", calls[-1][1])

    def test_batch_size_is_100_by_default(self):
        # Sanity check the constant lives where the plan says it does.
        self.assertEqual(ee.BATCH_SIZE, 100)

    def test_batch_chunks_at_100(self):
        # 250 objects → 3 batches (100,100,50).
        objs = [
            _FakeObject(
                uuid=f"u{i}",
                properties={"content": f"body-{i}"},
                vector={"qwen3_embed": [0.1] * 4},
            )
            for i in range(250)
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.KG_COLL: collection})
        svc = _FakeEmbeddingService()
        ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        # 3 batches.
        self.assertEqual(len(svc.text_batch_calls), 3)
        self.assertEqual(
            [len(c) for c in svc.text_batch_calls], [100, 100, 50],
        )


# ---------------------------------------------------------------------------
# Defensive: embed returns wrong number of vectors
# ---------------------------------------------------------------------------


class DefensiveTests(_BaseEnrichmentTest):
    def test_embed_returns_fewer_vectors_than_inputs(self):
        """Shouldn't happen in practice, but if a buggy adapter returns
        fewer vectors than inputs, we mark the missing-index uuids as
        failed and write the rest."""
        objs = [
            _FakeObject(
                uuid=f"u{i}",
                properties={"content": f"body-{i}"},
                vector={"qwen3_embed": [0.1] * 4},
            )
            for i in range(3)
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.KG_COLL: collection})

        # Adapter returns 2 vectors for 3 inputs → u2 fails.
        svc = _FakeEmbeddingService(
            text_response_factory=lambda texts: [[0.5] * 4 for _ in texts[:-1]],
        )
        report = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        self.assertEqual(report.enriched, 2)
        self.assertEqual(report.failed, 1)

    def test_embed_returns_empty_vector_marks_failed(self):
        """Empty vector returned by adapter — count as failed."""
        objs = [
            _FakeObject(
                uuid="u0",
                properties={"content": "body"},
                vector={"qwen3_embed": [0.1] * 4},
            ),
        ]
        collection = _FakeCollection(objs)
        client = _FakeClient({self.KG_COLL: collection})
        svc = _FakeEmbeddingService(
            text_response_factory=lambda texts: [[] for _ in texts],
        )
        report = ee.enrich_collection_vectors(
            collection_name=self.KG_COLL,
            new_slot="arctic2_embed",
            embedding_service=svc,
            weaviate_client_factory=lambda: client,
        )
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.enriched, 0)


# ---------------------------------------------------------------------------
# Content-property resolution helper
# ---------------------------------------------------------------------------


class ResolveContentPropertyTests(unittest.TestCase):
    """Direct tests of the internal `_resolve_content_property` helper."""

    def test_kg_class_returns_content(self):
        self.assertEqual(
            ee._resolve_content_property("MyProject_KnowledgeGraph"),
            "content",
        )

    def test_bare_kg_returns_content(self):
        self.assertEqual(
            ee._resolve_content_property("VibecodedOrchestrator_KnowledgeGraph"),
            "content",
        )

    def test_code_function_returns_function_body(self):
        self.assertEqual(
            ee._resolve_content_property("MyProj_CodeFunction"),
            "function_body",
        )

    def test_code_class_returns_class_body(self):
        self.assertEqual(
            ee._resolve_content_property("MyProj_CodeClass"),
            "class_body",
        )

    def test_code_module_returns_module_summary(self):
        self.assertEqual(
            ee._resolve_content_property("MyProj_CodeModule"),
            "module_summary",
        )

    def test_code_api_returns_api_description(self):
        self.assertEqual(
            ee._resolve_content_property("MyProj_CodeAPI"),
            "api_description",
        )

    def test_code_interaction_returns_endpoint(self):
        self.assertEqual(
            ee._resolve_content_property("MyProj_CodeInteraction"),
            "endpoint",
        )


# ---------------------------------------------------------------------------
# CLI shape — uses argparse + dataclass serialization
# ---------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    """Smoke-tests the argparser shape without invoking the full enrichment."""

    def test_argparser_accepts_enrich_command(self):
        parser = ee._build_argparser()
        args = parser.parse_args([
            "enrich",
            "--collection", "Foo",
            "--new-slot", "bar_embed",
        ])
        self.assertEqual(args.cmd, "enrich")
        self.assertEqual(args.collection, "Foo")
        self.assertEqual(args.new_slot, "bar_embed")
        self.assertFalse(args.dry_run)
        self.assertFalse(args.stream_progress)

    def test_argparser_accepts_optional_flags(self):
        parser = ee._build_argparser()
        args = parser.parse_args([
            "enrich",
            "--collection", "Foo",
            "--new-slot", "bar_embed",
            "--project-root", "/tmp/foo",
            "--dry-run",
            "--stream-progress",
            "--json",
        ])
        self.assertEqual(args.project_root, "/tmp/foo")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.stream_progress)
        self.assertTrue(args.json)

    def test_cli_preflight_error_returns_1(self):
        """A pre-flight error path should exit 1 with a JSON error
        payload."""
        # Patch the public enrich function to raise.
        with mock.patch.object(
            ee, "enrich_collection_vectors",
        ) as patched:
            patched.side_effect = ee.UnknownSlotError("unknown")

            with mock.patch("sys.stdout", new=mock.MagicMock()) as out:
                rc = ee.main([
                    "enrich",
                    "--collection", "Foo",
                    "--new-slot", "bad",
                ])
            self.assertEqual(rc, 1)
            # Last write was the JSON payload with error.
            written = "".join(c.args[0] for c in out.write.call_args_list)
            self.assertIn("UnknownSlotError", written)

    def test_cli_unexpected_error_returns_2(self):
        with mock.patch.object(
            ee, "enrich_collection_vectors",
        ) as patched:
            patched.side_effect = RuntimeError("kaboom")
            with mock.patch("sys.stdout", new=mock.MagicMock()) as out:
                rc = ee.main([
                    "enrich",
                    "--collection", "Foo",
                    "--new-slot", "qwen3_embed",
                ])
            self.assertEqual(rc, 2)
            written = "".join(c.args[0] for c in out.write.call_args_list)
            self.assertIn("kaboom", written)
            self.assertIn("unexpected", written)

    def test_cli_stream_progress_emits_jsonl(self):
        """--stream-progress prints one JSON line per batch."""
        progress_calls: list[tuple[float, str]] = []

        # The CLI builds its own progress callback that prints jsonl.
        # We simulate one batch via a side-effect that calls back.
        def fake_enrich(*, collection_name, new_slot, project_root,
                        progress_callback, dry_run):
            progress_callback(0.5, "halfway")
            progress_callback(1.0, "done")
            return ee.EnrichmentReport(
                collection=collection_name,
                new_slot=new_slot,
                total=2, enriched=2, skipped=0, failed=0,
            )

        with mock.patch.object(
            ee, "enrich_collection_vectors", side_effect=fake_enrich,
        ):
            from io import StringIO
            buf = StringIO()
            with mock.patch("sys.stdout", new=buf):
                rc = ee.main([
                    "enrich",
                    "--collection", "Foo",
                    "--new-slot", "qwen3_embed",
                    "--stream-progress",
                ])
        self.assertEqual(rc, 0)
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        # Expect two progress lines + one final report.
        self.assertEqual(len(lines), 3)
        # Each progress line parses to a {"progress","message"} dict.
        prog1 = json.loads(lines[0])
        self.assertIn("progress", prog1)
        self.assertIn("message", prog1)
        final = json.loads(lines[-1])
        self.assertEqual(final["enriched"], 2)


# ---------------------------------------------------------------------------
# v0.2.18 Commit 10 — multi-class sibling enrichment (GUI Save sweep).
#
# The codegraph Save flow in the launcher (KgCodegraphTab.svelte) enriches
# all five sibling Code* classes (CodeModule / CodeClass / CodeFunction /
# CodeAPI / CodeInteraction) sequentially by calling the existing single-
# collection `enrich_collection_vectors` once per class. The Python side
# stays single-collection — the multi-class loop is the Svelte modal's
# responsibility. These tests verify the underlying assumption: the
# single-collection enrichment IS independently invocable on every code-
# class suffix variant (including project-prefixed forms), with the
# expected content-property routing per class.
# ---------------------------------------------------------------------------


CODE_COLLECTION_SUFFIXES_ORDERED = (
    "CodeModule",
    "CodeClass",
    "CodeFunction",
    "CodeAPI",
    "CodeInteraction",
)


class MultiClassInvocationTests(_BaseEnrichmentTest):
    """Each of the 5 code-class suffix variants is independently enrichable.

    The Svelte sweep in v0.2.18 Commit 10 expands a project prefix
    (e.g. ``"MyProj_"``) into the 5 sibling Code* class names and invokes
    ``enrich_collection_vectors`` per class. The Python primitive doesn't
    know about the sweep — it just handles one class at a time. These
    tests pin the per-class behaviour the sweep depends on:

      * ``is_code_collection`` returns True for every suffixed form.
      * Enrichment runs end-to-end on each variant with the correct
        content property → code-batch embed call.
      * Sequential invocations don't leave shared mutable state behind
        (running enrichment on Class A doesn't mis-route Class B).
    """

    # The launcher's Svelte sweep order. Mirrors `CODE_COLLECTION_SUFFIXES`
    # in `launcher/src/lib/project-state/KgCodegraphTab.svelte`. Matches
    # the canonical Module/Class/Function/API/Interaction order used in
    # `vco_lib/weaviate_schema.py` docstrings (the underlying frozenset
    # is unordered; we pick a deterministic ordering).
    EXPECTED_ORDER = CODE_COLLECTION_SUFFIXES_ORDERED

    # The content property each class flows into for embed input.
    # Mirror of `vco_lib.embedding_enrichment.CODE_CONTENT_PROPERTY_BY_SUFFIX`.
    PROPERTY_BY_SUFFIX = {
        "CodeModule": "module_summary",
        "CodeClass": "class_body",
        "CodeFunction": "function_body",
        "CodeAPI": "api_description",
        "CodeInteraction": "endpoint",
    }

    def test_is_code_collection_recognises_all_five_suffixes(self):
        """Each project-prefixed sibling form is classified as a code
        collection (which controls slot-catalog selection inside
        enrichment)."""
        from vco_lib.weaviate_schema import (
            is_code_collection,
            _CODE_COLLECTION_SUFFIXES,
        )

        for suffix in self.EXPECTED_ORDER:
            # Bare name.
            self.assertTrue(
                is_code_collection(suffix),
                f"bare {suffix} should be code-shaped",
            )
            # Per-project prefixed.
            for prefix in ("MyProj_", "ARTup_", "VCODev_"):
                name = f"{prefix}{suffix}"
                self.assertTrue(
                    is_code_collection(name),
                    f"prefixed {name} should be code-shaped",
                )
            # And in the schema-level frozenset.
            self.assertIn(suffix, _CODE_COLLECTION_SUFFIXES)

        # Sanity: a non-code name doesn't accidentally match.
        self.assertFalse(is_code_collection("MyProj_KnowledgeGraph"))

    def test_enrich_collection_vectors_invocable_per_class(self):
        """Run `enrich_collection_vectors` once per suffix and verify each
        routes through the code path (code-batch embed call, correct
        property)."""
        for suffix in self.EXPECTED_ORDER:
            with self.subTest(suffix=suffix):
                coll_name = f"MyProj_{suffix}"
                content_prop = self.PROPERTY_BY_SUFFIX[suffix]

                # Schema for THIS class — code slots.
                self._schema_mock.side_effect = (
                    lambda c, base_url: _fake_schema(
                        self.CODE_SLOTS, class_name=c,
                    )
                )

                obj = _FakeObject(
                    uuid="u-only",
                    properties={content_prop: f"snippet for {suffix}"},
                    vector={"codesage_embed": [0.1] * 4},
                )
                collection = _FakeCollection([obj])
                client = _FakeClient({coll_name: collection})
                svc = _FakeEmbeddingService()

                report = ee.enrich_collection_vectors(
                    collection_name=coll_name,
                    new_slot="jina_embed",
                    embedding_service=svc,
                    weaviate_client_factory=lambda: client,
                )

                # Outcome.
                self.assertEqual(
                    report.total, 1,
                    f"{coll_name}: expected one object",
                )
                self.assertEqual(
                    report.enriched, 1,
                    f"{coll_name}: expected single enrichment write",
                )
                self.assertEqual(
                    report.skipped, 0,
                    f"{coll_name}: nothing should be skipped",
                )
                self.assertEqual(
                    report.failed, 0,
                    f"{coll_name}: no failures expected",
                )

                # Routing: code-batch, not text-batch.
                self.assertEqual(
                    len(svc.code_batch_calls), 1,
                    f"{coll_name}: should call embed_code_batch once",
                )
                self.assertEqual(
                    svc.text_batch_calls, [],
                    f"{coll_name}: should NOT touch embed_text_batch",
                )
                # And the content from the right property was fed in.
                self.assertEqual(
                    svc.code_batch_calls[0], [f"snippet for {suffix}"],
                    f"{coll_name}: embed input should come from {content_prop}",
                )

                # Write targets the new slot only (codesage preserved).
                self.assertEqual(len(collection.updates), 1)
                self.assertEqual(
                    list(collection.updates[0]["vector"].keys()),
                    ["jina_embed"],
                    f"{coll_name}: write should touch ONLY jina_embed",
                )

    def test_sequential_sweep_each_class_independently(self):
        """Simulate the Svelte modal's sequential loop: 5 successive
        ``enrich_collection_vectors`` calls, one per sibling class, each
        with its own client/collection/service. Verifies state from one
        call doesn't bleed into the next."""
        results: dict[str, ee.EnrichmentReport] = {}

        # Single shared schema patch — code slots for ALL classes.
        self._schema_mock.side_effect = lambda c, base_url: _fake_schema(
            self.CODE_SLOTS, class_name=c,
        )

        for suffix in self.EXPECTED_ORDER:
            coll_name = f"SweepProj_{suffix}"
            content_prop = self.PROPERTY_BY_SUFFIX[suffix]
            objs = [
                _FakeObject(
                    uuid=f"{suffix}-u{i}",
                    properties={content_prop: f"{suffix}/{i}"},
                    vector={"codesage_embed": [0.1] * 4},
                )
                for i in range(3)
            ]
            client = _FakeClient({coll_name: _FakeCollection(objs)})
            svc = _FakeEmbeddingService()

            report = ee.enrich_collection_vectors(
                collection_name=coll_name,
                new_slot="jina_embed",
                embedding_service=svc,
                weaviate_client_factory=lambda c=client: c,
            )
            results[suffix] = report

        # Every class should have enriched all 3 of its objects.
        for suffix in self.EXPECTED_ORDER:
            r = results[suffix]
            self.assertEqual(
                r.total, 3, f"{suffix}: expected total=3",
            )
            self.assertEqual(
                r.enriched, 3, f"{suffix}: expected enriched=3",
            )
            self.assertEqual(
                r.failed, 0, f"{suffix}: expected no failures",
            )

    def test_per_class_failure_does_not_taint_other_classes(self):
        """Simulate the Svelte modal's soft-fail-per-class contract: one
        sibling raises a pre-flight error, the others still succeed.

        The Python side raises (CollectionNotFoundError); it's the Svelte
        loop that catches + continues. Here we just verify that an error
        on one class doesn't corrupt internal module state such that the
        next call also fails."""

        # Schema lookup returns 404 ONLY for CodeAPI (simulating a class
        # that was never seeded). Other classes return a real schema.
        def selective_schema(c: str, base_url: str):
            if c.endswith("CodeAPI"):
                return None  # → CollectionNotFoundError pre-flight
            return _fake_schema(self.CODE_SLOTS, class_name=c)

        self._schema_mock.side_effect = selective_schema

        # CodeModule should work (run before CodeAPI in EXPECTED_ORDER).
        client_mod = _FakeClient(
            {"SoftFail_CodeModule": _FakeCollection([
                _FakeObject(
                    uuid="mod-0",
                    properties={"module_summary": "mod body"},
                    vector={"codesage_embed": [0.1] * 4},
                ),
            ])},
        )
        r_mod = ee.enrich_collection_vectors(
            collection_name="SoftFail_CodeModule",
            new_slot="jina_embed",
            embedding_service=_FakeEmbeddingService(),
            weaviate_client_factory=lambda: client_mod,
        )
        self.assertEqual(r_mod.enriched, 1)

        # CodeAPI should pre-flight-fail.
        with self.assertRaises(ee.CollectionNotFoundError):
            ee.enrich_collection_vectors(
                collection_name="SoftFail_CodeAPI",
                new_slot="jina_embed",
                embedding_service=_FakeEmbeddingService(),
                weaviate_client_factory=lambda: _FakeClient({}),
            )

        # CodeFunction should STILL work after the CodeAPI failure — no
        # module-level state was poisoned.
        client_fn = _FakeClient(
            {"SoftFail_CodeFunction": _FakeCollection([
                _FakeObject(
                    uuid="fn-0",
                    properties={"function_body": "def f(): pass"},
                    vector={"codesage_embed": [0.1] * 4},
                ),
            ])},
        )
        r_fn = ee.enrich_collection_vectors(
            collection_name="SoftFail_CodeFunction",
            new_slot="jina_embed",
            embedding_service=_FakeEmbeddingService(),
            weaviate_client_factory=lambda: client_fn,
        )
        self.assertEqual(r_fn.enriched, 1)


# ---------------------------------------------------------------------------
# Live integration — skipped when Weaviate is unreachable
# ---------------------------------------------------------------------------


def _weaviate_reachable(url: str) -> bool:
    try:
        req = urllib.request.Request(
            f"{url.rstrip('/')}/v1/.well-known/ready", method="GET",
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


class LiveEnrichmentTest(unittest.TestCase):
    """End-to-end test against a real Weaviate.

    Creates a 3-slot KG-shaped collection, inserts 5 objects with only
    qwen3_embed populated, runs enrichment with new_slot=arctic2_embed,
    verifies all 5 now have both slots populated AND qwen3_embed values
    are unchanged. Self-skips when Weaviate isn't reachable.
    """

    TEST_COLL = "VCO218EnrichmentTest"

    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
        if not _weaviate_reachable(cls.url):
            raise unittest.SkipTest(
                f"Weaviate not reachable at {cls.url} — live test skipped",
            )

    def setUp(self):
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        try:
            req = urllib.request.Request(
                f"{self.url}/v1/schema/{self.TEST_COLL}",
                method="DELETE",
            )
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                pass
        except (urllib.error.URLError, OSError):
            pass

    def _create_collection(self):
        body = {
            "class": self.TEST_COLL,
            "vectorConfig": {
                "qwen3_embed":   {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
                "arctic2_embed": {"vectorizer": {"none": {}}, "vectorIndexType": "hnsw"},
            },
            "invertedIndexConfig": {"indexNullState": True},
            "properties": [
                {"name": "title", "dataType": ["text"]},
                {"name": "content", "dataType": ["text"]},
            ],
        }
        req = urllib.request.Request(
            f"{self.url}/v1/schema",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertIn(r.status, (200, 201))

    def _populate(self, count: int = 5) -> set[str]:
        import uuid as _uuid
        import weaviate  # type: ignore[import-untyped]

        host = self.url.replace("http://", "").replace("https://", "").split(":")[0]
        port = int(self.url.rsplit(":", 1)[-1])
        grpc_port = int(os.environ.get("GRPC_PORT", "50052"))
        client = weaviate.connect_to_custom(
            http_host=host, http_port=port, http_secure=False,
            grpc_host=host, grpc_port=grpc_port, grpc_secure=False,
            skip_init_checks=True,
        )
        uuids = set()
        try:
            col = client.collections.get(self.TEST_COLL)
            with col.batch.dynamic() as bw:
                for i in range(count):
                    uid = str(_uuid.uuid4())
                    uuids.add(uid)
                    bw.add_object(
                        properties={
                            "title": f"node-{i}",
                            "content": f"contents of node {i}",
                        },
                        uuid=uid,
                        vector={"qwen3_embed": [float(i)] * 1024},
                    )
        finally:
            client.close()
        return uuids

    def _read_all(self) -> dict[str, dict]:
        """Return {uuid: vector_dict} for every object in the collection."""
        import weaviate  # type: ignore[import-untyped]
        host = self.url.replace("http://", "").replace("https://", "").split(":")[0]
        port = int(self.url.rsplit(":", 1)[-1])
        grpc_port = int(os.environ.get("GRPC_PORT", "50052"))
        client = weaviate.connect_to_custom(
            http_host=host, http_port=port, http_secure=False,
            grpc_host=host, grpc_port=grpc_port, grpc_secure=False,
            skip_init_checks=True,
        )
        out: dict[str, dict] = {}
        try:
            col = client.collections.get(self.TEST_COLL)
            for obj in col.iterator(include_vector=True):
                out[str(obj.uuid)] = dict(obj.vector or {})
        finally:
            client.close()
        return out

    def test_enrich_preserves_qwen3_adds_arctic2(self):
        """The crown jewel test: data preservation across enrichment.

        Skipped automatically when no EmbeddingService backend is
        reachable (the live path needs Ollama up too). For that case we
        inject a fake service that produces deterministic 1024d vectors
        so the test exercises the Weaviate side without external deps.
        """
        self._create_collection()
        original_uuids = self._populate(count=5)

        # Snapshot original qwen3_embed vectors so we can assert
        # post-enrichment values match.
        before = self._read_all()
        self.assertEqual(set(before.keys()), original_uuids)
        for uid in original_uuids:
            self.assertIn("qwen3_embed", before[uid])
            self.assertEqual(len(before[uid]["qwen3_embed"]), 1024)

        # Run enrichment with a faked EmbeddingService that emits a
        # deterministic 1024d vector (same dim as the slot expects).
        # This bypasses the Ollama-availability requirement and isolates
        # the data-preservation behaviour.
        fake_svc = _FakeEmbeddingService(
            text_vector_slot="qwen3_embed",
            embed_dim=1024,
        )
        report = ee.enrich_collection_vectors(
            collection_name=self.TEST_COLL,
            new_slot="arctic2_embed",
            embedding_service=fake_svc,
        )
        self.assertEqual(report.total, 5)
        self.assertEqual(report.enriched, 5)
        self.assertEqual(report.skipped, 0)
        self.assertEqual(report.failed, 0)

        # Verify qwen3_embed unchanged + arctic2_embed populated.
        after = self._read_all()
        self.assertEqual(set(after.keys()), original_uuids)
        for uid in original_uuids:
            self.assertIn("qwen3_embed", after[uid])
            self.assertIn("arctic2_embed", after[uid])
            # qwen3_embed preserved verbatim.
            self.assertEqual(
                after[uid]["qwen3_embed"], before[uid]["qwen3_embed"],
                msg=f"qwen3_embed mutated for {uid}",
            )
            # arctic2_embed populated with a 1024d vector.
            self.assertEqual(len(after[uid]["arctic2_embed"]), 1024)


if __name__ == "__main__":
    unittest.main()
