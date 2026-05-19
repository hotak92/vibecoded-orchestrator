# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.18 Development-collection KG-parity (Wave-C addendum,
2026-05-19).

Covers two related changes:

  1. Schema parity — `vco_lib.project_init.development_class_definition`
     and `templates/scripts/sync_knowledge_graph.py::ensure_dev_collection_exists`
     both gain the `status` and `content_hash` properties (KG-parity for
     archived-doc filtering + embed-skip fast-path) and the 5-slot
     named-vector catalog from `vco_lib.weaviate_schema.KG_NAMED_VECTORS`.

  2. Sync-side embed-skip — `sync_doc` now mirrors `sync_node`'s v0.2.17
     content_hash fast-path: a re-sync of an unchanged file skips the
     delete-and-re-embed pipeline entirely.

Anti-tests assert that `tags`, `links`, `typed_links`, and `node_type`
are explicitly NOT mirrored — user direction 2026-05-19 (KG-only graph
metadata; Dev rows are unambiguously docs by virtue of the collection
name).

Run: pytest tests/test_dev_collection_v0218_parity.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402
from vco_lib.weaviate_schema import KG_NAMED_VECTORS  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_sync_module(test_id: int):
    """Load `templates/scripts/sync_knowledge_graph.py` as a module without
    executing its __main__ guard. Each test gets a unique sys.modules
    entry so module-level mutations don't leak between tests.
    """
    os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    script_path = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
    mod_name = f"_test_dev_parity_sync_{test_id}"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeWrapper:
    """Minimal WeaviateMCPServer stand-in for sync_doc tests.

    Mocks the .client.collections.get(...) -> coll chain and the
    .text_vector_slot / ._get_all_kg_embeddings / ._get_embedding
    EmbeddingService delegation.
    """

    def __init__(
        self,
        *,
        text_slot: str = "qwen3_embed",
        all_slots: dict | None = None,
    ) -> None:
        self.text_vector_slot = text_slot
        self._all_slots = all_slots or {
            text_slot: [0.1] * 1024,
        }
        # Track calls for assertions.
        self.embed_calls: list[str] = []
        self.client = MagicMock()

    def _get_all_kg_embeddings(self, text: str) -> dict:
        self.embed_calls.append(text)
        return self._all_slots

    def _get_embedding(self, text: str) -> list:
        self.embed_calls.append(text)
        return self._all_slots[self.text_vector_slot]


def _fake_obj(
    *,
    uuid_: str = "00000000-0000-0000-0000-000000000001",
    content_hash: str = "",
    total_chunks: int = 1,
    chunk_num: int = 1,
    vector_slots: dict | None = None,
) -> MagicMock:
    """Build a stand-in for a `weaviate.collections.classes.data.DataObject`."""
    obj = MagicMock()
    obj.uuid = uuid_
    obj.properties = {
        "content_hash": content_hash,
        "total_chunks": total_chunks,
        "chunk_num": chunk_num,
    }
    # Named-vector collections return a dict {slot: list[float]} on
    # obj.vector when include_vector=True.
    obj.vector = vector_slots if vector_slots is not None else {}
    return obj


# ---------------------------------------------------------------------------
# Site 1 — development_class_definition (vco_lib/project_init.py)
# ---------------------------------------------------------------------------


class DevelopmentClassDefinitionV0218Tests(unittest.TestCase):
    """v0.2.18 (2026-05-19): Development class definition KG parity.

    Adds `status` + `content_hash`. Anti-tests assert the explicit
    NOT-mirrored set (`tags`, `links`, `typed_links`, `node_type`).
    """

    def test_development_class_definition_includes_content_hash(self):
        schema = project_init.development_class_definition("FooDev")
        props = {p["name"]: p for p in schema["properties"]}
        self.assertIn(
            "content_hash", props,
            "Development schema must include `content_hash` for KG parity "
            "with the v0.2.17 embed-skip fast-path",
        )
        self.assertEqual(
            props["content_hash"]["dataType"], ["text"],
            "content_hash must be text dataType (SHA-256 hex digest)",
        )

    def test_development_class_definition_includes_status(self):
        schema = project_init.development_class_definition("FooDev")
        props = {p["name"]: p for p in schema["properties"]}
        self.assertIn(
            "status", props,
            "Development schema must include `status` so archived docs "
            "can be filtered out by `hybrid_search` (KG parity)",
        )
        self.assertEqual(
            props["status"]["dataType"], ["text"],
            "status must be text dataType (e.g. 'active', 'archived')",
        )

    def test_development_class_definition_does_NOT_include_tags_links_typed_links_node_type(
        self,
    ):
        """Anti-test for the user's explicit exclusion list (2026-05-19).

        These four are KG-only graph metadata:
          - `tags`, `links`, `typed_links` — WikiLink graph structure;
            Dev rows have no WikiLinks, no typed relationships, no
            tag-from-typed-link inference.
          - `node_type` — redundant: every row in a Dev collection is
            unambiguously a "doc" by virtue of the collection name.

        If a future PR adds any of these back, this test fails LOUDLY —
        the user wants the exclusion preserved.
        """
        schema = project_init.development_class_definition("FooDev")
        prop_names = {p["name"] for p in schema["properties"]}
        forbidden = {"tags", "links", "typed_links", "node_type"}
        intersection = prop_names & forbidden
        self.assertEqual(
            intersection, set(),
            f"Development schema must NOT include {sorted(forbidden)}; "
            f"saw {sorted(intersection)}. Re-read the v0.2.18 docstring on "
            "`development_class_definition` before adding any of these — "
            "they were explicitly ruled out by the user on 2026-05-19.",
        )

    def test_development_class_definition_preserves_temporal_props(self):
        """Adding `status`/`content_hash` must not regress PR-24's
        temporal-property set."""
        schema = project_init.development_class_definition("FooDev")
        prop_names = {p["name"] for p in schema["properties"]}
        for required in ("created", "updated", "valid_from", "valid_until"):
            self.assertIn(
                required, prop_names,
                f"Development schema lost `{required}` temporal prop — "
                "regression of PR-24 (2026-05-16)",
            )

    def test_development_class_at_target_yields_noop_delta(self):
        """End-to-end: a Dev collection at the post-v0.2.18 target shape
        must produce a noop delta when fed back through `_schema_delta` +
        `_classify_action`. Catches the case where Site 1 + the test
        fixture in `test_vco_lib_migrate.py::_at_target_dev` drift.
        """
        target = project_init.development_class_definition("FooDev")
        # Round-trip: feed the target schema back as the "actual" — must
        # show no delta (modulo dataType list-vs-string normalization
        # which Weaviate handles server-side).
        delta = project_init._schema_delta(target, target)
        self.assertFalse(
            delta.any(),
            f"target-as-actual produced spurious delta: {delta}",
        )
        self.assertEqual(project_init._classify_action(delta), "noop")


# ---------------------------------------------------------------------------
# Site 2 — ensure_dev_collection_exists (templates/scripts/sync_knowledge_graph.py)
# ---------------------------------------------------------------------------


class EnsureDevCollectionV0218Tests(unittest.TestCase):
    """Runtime fallback creator must match Site 1 exactly."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._test_id = id(cls)
        cls._mod = _load_sync_module(cls._test_id)

    def _make_server_with_no_existing_collection(
        self, dev_name: str = "Foo_Development",
    ):
        """Build a server stand-in where `collections.exists(...)` returns
        False (so the create branch fires)."""
        srv = MagicMock()
        srv.client.collections.exists.return_value = False
        srv.client.collections.create = MagicMock()
        # Module reads DEV_COLLECTION_NAME at module-level; we patch it
        # for the call via the loaded module's namespace.
        self._mod.DEV_COLLECTION_NAME = dev_name
        return srv

    def test_ensure_dev_collection_includes_content_hash_status(self):
        """The runtime fallback creator passes `status` + `content_hash`
        in the `properties=` kwarg to `collections.create(...)`."""
        srv = self._make_server_with_no_existing_collection()
        ok = self._mod.ensure_dev_collection_exists(srv)
        self.assertTrue(ok)

        srv.client.collections.create.assert_called_once()
        kwargs = srv.client.collections.create.call_args.kwargs
        props = kwargs.get("properties", [])
        prop_names = {p.name for p in props}
        self.assertIn(
            "status", prop_names,
            "ensure_dev_collection_exists must pass `status` to create()",
        )
        self.assertIn(
            "content_hash", prop_names,
            "ensure_dev_collection_exists must pass `content_hash` to create()",
        )
        # Anti-test: still don't mirror KG-only graph metadata.
        forbidden = {"tags", "links", "typed_links", "external_links", "node_type"}
        intersection = prop_names & forbidden
        self.assertEqual(
            intersection, set(),
            f"runtime-fallback Dev collection must NOT include "
            f"{sorted(forbidden)}; saw {sorted(intersection)}",
        )

    def test_ensure_dev_collection_uses_KG_NAMED_VECTORS_slots(self):
        """All 5 v0.2.18 slots from `KG_NAMED_VECTORS` must be configured.

        Pre-v0.2.18 this hardcoded 3 slots (qwen3 + ollama + openai).
        v0.2.18 sources from the canonical catalog so the runtime
        fallback matches `project_init.development_class_definition`
        exactly.
        """
        srv = self._make_server_with_no_existing_collection()
        ok = self._mod.ensure_dev_collection_exists(srv)
        self.assertTrue(ok)

        kwargs = srv.client.collections.create.call_args.kwargs
        vec_config = kwargs.get("vectorizer_config", [])
        # Each Configure.NamedVectors.none(name=X) has a `.name` attr in
        # weaviate-py v4. Extract names defensively.
        configured = set()
        for entry in vec_config:
            # weaviate.classes.config._NamedVectorConfigCreate carries
            # the slot name as `.name`.
            n = getattr(entry, "name", None)
            if n is not None:
                configured.add(n)
            else:
                # Fallback: inspect repr/str for the slot name.
                configured.add(str(entry))

        expected = {slot.name for slot in KG_NAMED_VECTORS}
        # Defensive: at minimum all 5 v0.2.18 slots must be present.
        # We don't insist on no-extras because future v0.2.19 may add
        # slots that this commit's tests legitimately don't know about.
        for slot_name in expected:
            self.assertTrue(
                any(slot_name in c for c in (configured if all(isinstance(x, str) for x in configured) else [s for s in configured])),
                f"missing slot '{slot_name}' in ensure_dev_collection_exists "
                f"vectorizer_config; saw: {sorted(configured)}",
            )
        # Stronger assertion when the introspection worked cleanly:
        if all(isinstance(x, str) for x in configured):
            self.assertGreaterEqual(
                len(configured), 5,
                f"v0.2.18 expects >= 5 named-vector slots; saw "
                f"{len(configured)}: {sorted(configured)}",
            )


# ---------------------------------------------------------------------------
# Site 3 — sync_doc embed-skip + content_hash persistence
# ---------------------------------------------------------------------------


class SyncDocEmbedSkipTests(unittest.TestCase):
    """v0.2.18 sync_doc gains the v0.2.17 KG embed-skip fast-path."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._test_id = id(cls)
        cls._mod = _load_sync_module(cls._test_id)

    def _make_wrapper_with_coll(
        self,
        *,
        existing_objs: list | None = None,
        fetch_supports_include_vector: bool = True,
    ):
        """Build a `_FakeWrapper` whose `client.collections.get(name)`
        returns a stand-in collection with the supplied `existing_objs`."""
        wrapper = _FakeWrapper()
        coll = MagicMock()

        existing_result = MagicMock()
        existing_result.objects = existing_objs or []

        def _fetch_objects(*args, **kwargs):
            if "include_vector" in kwargs and not fetch_supports_include_vector:
                raise TypeError(
                    "fetch_objects() got an unexpected keyword "
                    "'include_vector' (simulating old client)"
                )
            return existing_result

        coll.query.fetch_objects = _fetch_objects
        coll.data.insert = MagicMock(return_value="inserted-uuid")
        coll.data.delete_by_id = MagicMock()

        wrapper.client.collections.get = MagicMock(return_value=coll)
        wrapper.client.collections.exists = MagicMock(return_value=True)
        return wrapper, coll

    def _write_doc(self, tmpdir: Path, content: str) -> Path:
        """Write a docs/ file inside `tmpdir` and return its path."""
        doc = tmpdir / "test_doc.md"
        doc.write_text(content, encoding="utf-8")
        return doc

    def _set_dev_collection(self, name: str = "Foo_Development") -> None:
        self._mod.DEV_COLLECTION_NAME = name

    def test_sync_doc_writes_content_hash_on_insert(self):
        """On first sync (no existing objects), sync_doc must write
        `content_hash` in the `data_obj` passed to collection.data.insert.
        """
        self._set_dev_collection()
        wrapper, coll = self._make_wrapper_with_coll(existing_objs=[])

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            doc = self._write_doc(tmpdir, "# Title\n\nHello world.\n")
            ok = self._mod.sync_doc(wrapper, doc)
            self.assertTrue(ok)

        coll.data.insert.assert_called()
        call_kwargs = coll.data.insert.call_args.kwargs
        properties = call_kwargs["properties"]
        self.assertIn(
            "content_hash", properties,
            "sync_doc must write `content_hash` into the insert properties",
        )
        # SHA-256 hex digest is 64 lowercase-hex chars.
        self.assertEqual(len(properties["content_hash"]), 64)
        self.assertTrue(
            all(c in "0123456789abcdef" for c in properties["content_hash"]),
            "content_hash must be a SHA-256 hex digest",
        )

    def test_sync_doc_skips_reembed_when_content_hash_matches(self):
        """When existing objects all have matching content_hash AND active
        slot populated AND chunk-count matches → sync_doc must NOT call
        delete_by_id and NOT call the embed backend.
        """
        self._set_dev_collection()
        # Compute the expected hash so we can mock it.
        content = "# Hello\n\nUnchanged body.\n"

        # Use the module's own hash function so the test stays in lockstep
        # with the implementation.
        expected_hash = self._mod._content_signature_excluding_updated(content)

        existing = [
            _fake_obj(
                content_hash=expected_hash,
                total_chunks=1,
                chunk_num=1,
                vector_slots={"qwen3_embed": [0.1, 0.2, 0.3]},
            )
        ]
        wrapper, coll = self._make_wrapper_with_coll(existing_objs=existing)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            doc = self._write_doc(tmpdir, content)
            ok = self._mod.sync_doc(wrapper, doc)
            self.assertTrue(ok)

        # Fast-path assertions: zero embed calls, zero delete calls,
        # zero insert calls.
        self.assertEqual(
            wrapper.embed_calls, [],
            "Fast-path must NOT call the embedding backend on unchanged content",
        )
        coll.data.delete_by_id.assert_not_called()
        coll.data.insert.assert_not_called()

    def test_sync_doc_reembeds_when_content_hash_mismatches(self):
        """When existing hash differs from the current file's hash → fall
        through to delete-and-re-embed."""
        self._set_dev_collection()
        existing = [
            _fake_obj(
                content_hash="0" * 64,  # stale hash
                total_chunks=1,
                chunk_num=1,
                vector_slots={"qwen3_embed": [0.1, 0.2, 0.3]},
            )
        ]
        wrapper, coll = self._make_wrapper_with_coll(existing_objs=existing)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            doc = self._write_doc(
                tmpdir, "# Title\n\nDifferent content from before.\n"
            )
            ok = self._mod.sync_doc(wrapper, doc)
            self.assertTrue(ok)

        # Should have hit the slow path: delete + embed + insert.
        coll.data.delete_by_id.assert_called()
        self.assertEqual(
            len(wrapper.embed_calls), 1,
            "Slow path must call embed exactly once for the single-chunk doc",
        )
        coll.data.insert.assert_called_once()
        # And persists the new hash.
        new_props = coll.data.insert.call_args.kwargs["properties"]
        self.assertIn("content_hash", new_props)
        self.assertNotEqual(new_props["content_hash"], "0" * 64)

    def test_sync_doc_reembeds_when_active_slot_missing_vector(self):
        """v0.2.17 -> v0.2.18 warm-up case: existing object has matching
        content_hash but the active named-vector slot is empty (e.g. user
        just switched from qwen3 to openai_text and the new slot hasn't
        been populated yet). MUST re-embed to populate the new slot.
        """
        self._set_dev_collection()
        content = "# Warm-up\n\nThis triggers the slot-missing case.\n"
        expected_hash = self._mod._content_signature_excluding_updated(content)

        # Existing object has matching hash but a DIFFERENT slot populated.
        # The wrapper says the active slot is qwen3_embed; the existing
        # row has openai_text_embed but no qwen3_embed → re-embed.
        existing = [
            _fake_obj(
                content_hash=expected_hash,
                total_chunks=1,
                chunk_num=1,
                vector_slots={"openai_text_embed": [0.5] * 1536},
            )
        ]
        wrapper, coll = self._make_wrapper_with_coll(existing_objs=existing)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            doc = self._write_doc(tmpdir, content)
            ok = self._mod.sync_doc(wrapper, doc)
            self.assertTrue(ok)

        # Slow path: delete + embed + insert.
        coll.data.delete_by_id.assert_called()
        self.assertEqual(
            len(wrapper.embed_calls), 1,
            "Warm-up case must re-embed to populate the new active slot",
        )
        coll.data.insert.assert_called_once()

    def test_sync_doc_reembeds_when_chunk_count_mismatches(self):
        """If existing objects' total_chunks (e.g. 3) disagrees with the
        number of existing chunks Weaviate returned (e.g. 2 — partial
        write from a prior crash) the fast-path MUST fall through to
        re-embed.
        """
        self._set_dev_collection()
        content = "# Crashy\n\nNeeds re-embed because of partial state.\n"
        expected_hash = self._mod._content_signature_excluding_updated(content)

        # Returns 2 chunks but each claims total_chunks=3 → mismatch.
        existing = [
            _fake_obj(
                content_hash=expected_hash,
                total_chunks=3,
                chunk_num=1,
                vector_slots={"qwen3_embed": [0.1] * 1024},
            ),
            _fake_obj(
                content_hash=expected_hash,
                total_chunks=3,
                chunk_num=2,
                uuid_="00000000-0000-0000-0000-000000000002",
                vector_slots={"qwen3_embed": [0.1] * 1024},
            ),
        ]
        wrapper, coll = self._make_wrapper_with_coll(existing_objs=existing)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            doc = self._write_doc(tmpdir, content)
            ok = self._mod.sync_doc(wrapper, doc)
            self.assertTrue(ok)

        # Slow path: delete + embed.
        coll.data.delete_by_id.assert_called()
        self.assertGreater(len(wrapper.embed_calls), 0)

    def test_sync_doc_reembeds_when_existing_hash_is_empty(self):
        """Warm-up case 2: the v0.2.17 -> v0.2.18 migration adds the
        `content_hash` property to existing Dev collections via
        patch_props, but the rows have empty values until the first
        re-sync. The fast-path must NOT fire when any row has empty hash.
        """
        self._set_dev_collection()
        content = "# Empty-hash\n\nFirst re-sync after the v0.2.18 upgrade.\n"

        existing = [
            _fake_obj(
                content_hash="",  # patch_props added prop, no value yet
                total_chunks=1,
                chunk_num=1,
                vector_slots={"qwen3_embed": [0.1] * 1024},
            )
        ]
        wrapper, coll = self._make_wrapper_with_coll(existing_objs=existing)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            doc = self._write_doc(tmpdir, content)
            ok = self._mod.sync_doc(wrapper, doc)
            self.assertTrue(ok)

        # Slow path: re-embed to backfill the hash. Subsequent run will
        # then hit the fast path.
        coll.data.delete_by_id.assert_called()
        self.assertEqual(len(wrapper.embed_calls), 1)
        # And the new hash is non-empty.
        new_props = coll.data.insert.call_args.kwargs["properties"]
        self.assertTrue(new_props["content_hash"])

    def test_sync_doc_fast_path_falls_through_when_include_vector_unsupported(
        self,
    ):
        """Defensive: older Weaviate clients may not accept
        `include_vector=True`. The fast-path must gracefully fall back
        (the second fetch_objects call without the kwarg) and still skip
        re-embed when hash + chunk-count match (skipping the slot check).
        """
        self._set_dev_collection()
        content = "# Compat\n\nOlder client path.\n"
        expected_hash = self._mod._content_signature_excluding_updated(content)
        existing = [
            _fake_obj(
                content_hash=expected_hash,
                total_chunks=1,
                chunk_num=1,
                # No vector dict (older client).
                vector_slots=None,
            )
        ]
        wrapper, coll = self._make_wrapper_with_coll(
            existing_objs=existing,
            fetch_supports_include_vector=False,
        )
        # Override obj.vector to be None (no include_vector path).
        for obj in existing:
            obj.vector = None
        # On the fallback path, we want vector_slot inspection to be
        # treated as "active_slot empty" so the slot gate is bypassed.
        # Set wrapper's text_vector_slot to empty string to take the
        # gate-bypass branch.
        wrapper.text_vector_slot = ""

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            doc = self._write_doc(tmpdir, content)
            ok = self._mod.sync_doc(wrapper, doc)
            self.assertTrue(ok)

        # Still hits the fast path (no slot check needed).
        coll.data.delete_by_id.assert_not_called()
        self.assertEqual(wrapper.embed_calls, [])


# ---------------------------------------------------------------------------
# Cross-site: parity assertion between Site 1 and Site 2
# ---------------------------------------------------------------------------


class SitesAgreeOnPropSetTests(unittest.TestCase):
    """The two write sites MUST stay in lockstep. If they drift the
    migrate dispatcher's additive patch_props diff will produce phantom
    missing-prop loops.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._mod = _load_sync_module(id(cls))

    def test_site1_and_site2_agree_on_status_content_hash(self):
        """Both write paths must include `status` + `content_hash`."""
        # Site 1 — Python definition.
        site1_props = {
            p["name"]
            for p in project_init.development_class_definition("FooDev")["properties"]
        }

        # Site 2 — capture the Property names the runtime fallback would
        # pass to collections.create().
        srv = MagicMock()
        srv.client.collections.exists.return_value = False
        srv.client.collections.create = MagicMock()
        self._mod.DEV_COLLECTION_NAME = "Foo_Development"
        self._mod.ensure_dev_collection_exists(srv)
        kwargs = srv.client.collections.create.call_args.kwargs
        site2_props = {p.name for p in kwargs["properties"]}

        for required in ("status", "content_hash"):
            self.assertIn(required, site1_props)
            self.assertIn(required, site2_props)


if __name__ == "__main__":
    unittest.main()
