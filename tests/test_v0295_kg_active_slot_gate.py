# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 — the KG embed-skip active-slot gate (WP-0(a) + WP-5).

Two defects, one mechanism.

**WP-0(a), the false promise.** ``sync_doc``'s fast-path comment claimed
that ``sync_node``'s fast path "also relies on [the active-slot check]
implicitly via the chunk_count gate". It never did: ``chunk_count_ok``
compares each row's stored ``total_chunks`` against the number of rows
returned and inspects no vector, and ``sync_node``'s fetch omitted
``include_vector`` entirely. A comment describing a guard is part of that
guard, so the sentence was the defect — this module pins its removal.

**WP-5, the gap it hid.** A KG node whose ``content_hash`` matches while
the ACTIVE named-vector slot is empty was skipped by ``sync_node``
forever. That is exactly what an aborted embedding-model change leaves
behind, so the one repair path a wrongly-advanced
``last_installed_active_embedding`` could still have had did not exist.

The gate WP-5 adds is deliberately NARROWER than a verbatim mirror of
``sync_doc`` would be, and the narrowing is the safety property under
test: it engages only when the collection's schema is READABLE and
actually declares the active slot. Every other outcome — schema
unreadable, legacy single-unnamed-vector class, slot absent from the
class, vectors never requested — is INCONCLUSIVE and preserves the
pre-existing (hash + chunk-count) skip semantics. A gate that guessed
"unknown ⇒ re-embed" would re-embed an entire knowledge graph on one
degraded schema read, and on a legacy class it would do so on every run
forever, since a re-embed cannot add a slot to a schema.

No live Weaviate anywhere: an in-memory fake, ``WEAVIATE_URL`` pinned at
an unroutable port, ``VCT_STATE_DIR`` redirected into the temp root.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
sys.path.insert(0, str(REPO_ROOT))

PROJECT_KG = "V0295T_KnowledgeGraph"
SHARED_KG = "V0295T_Shared_KnowledgeGraph"
DEV_COLL = "V0295T_Development"
ACTIVE_SLOT = "qwen3_embed"
OTHER_SLOT = "arctic2_embed"

#: The retired claim, verbatim (modulo line wrapping, which is why this is
#: matched against whitespace-collapsed text).
FALSE_CLAIM = (
    "which sync_node's fast-path also relies on implicitly via the "
    "chunk_count gate"
)


def _collapsed(text: str) -> str:
    """Whitespace-collapsed source, so a re-wrap cannot hide a claim."""
    return " ".join(text.replace("#", " ").split())


# ─── In-memory fake Weaviate client ──────────────────────────────────────
# Same fake family as tests/test_v0292_kg_chunk_plan_transition.py and
# tests/test_v0289_kg_sync_scope_routing.py, plus the two things this
# module's subject needs and they do not have: a readable `config` (the
# named-vector schema probe the gate consults) and vectors that survive
# insert → fetch.


class _FakeProp:
    def __init__(self, name: str):
        self.name = name

    def equal(self, value):
        return _FakeFilter([(self.name, value)])


class _FakeFilter:
    def __init__(self, matchers=None, subfilters=None):
        self.matchers = matchers or []
        self.subfilters = subfilters

    @staticmethod
    def by_property(name: str) -> "_FakeProp":
        return _FakeProp(name)

    @staticmethod
    def any_of(filters) -> "_FakeFilter":
        f = _FakeFilter()
        f.subfilters = list(filters)
        return f

    def __and__(self, other: "_FakeFilter") -> "_FakeFilter":
        return _FakeFilter(self.matchers + other.matchers)

    def matches(self, props: dict) -> bool:
        if self.subfilters is not None:
            return any(sf.matches(props) for sf in self.subfilters)
        return all(props.get(name) == value for name, value in self.matchers)


class _FakeObj:
    def __init__(self, uid, props, vector=None):
        self.uuid = uid
        self.properties = props
        self.vector = vector if vector is not None else {}


class _FakeQueryResult:
    def __init__(self, objects):
        self.objects = objects


class _FakeQuery:
    """``include_vector`` is honoured, and optionally REJECTED.

    A real weaviate-client v4 returns the named-vector map only when the
    caller asks for it; an older/mocked client raises on the kwarg. Both
    shapes matter here — the second is the "vectors never requested" row
    of the gate's decision table.
    """

    def __init__(self, store: dict, *, reject_include_vector: bool = False):
        self._store = store
        self._reject = reject_include_vector

    def fetch_objects(self, filters=None, limit=100, return_properties=None,
                      include_vector=False):
        if include_vector and self._reject:
            raise TypeError(
                "fetch_objects() got an unexpected keyword argument "
                "'include_vector'"
            )
        objs = list(self._store.values())
        if filters is not None:
            objs = [o for o in objs if filters.matches(o.properties)]
        objs = objs[:limit]
        if include_vector:
            return _FakeQueryResult(objs)
        # Weaviate returns no vector payload unless asked.
        return _FakeQueryResult(
            [_FakeObj(o.uuid, o.properties, {}) for o in objs]
        )


class _FakeData:
    def __init__(self, store: dict):
        self._store = store

    def insert(self, properties=None, vector=None):
        uid = str(uuid.uuid4())
        self._store[uid] = _FakeObj(uid, dict(properties or {}), vector)
        return uid

    def delete_by_id(self, uid):
        self._store.pop(str(uid), None)

    def reference_add(self, **kwargs):  # noqa: ARG002
        pass


class _FakeConfigPayload:
    def __init__(self, vector_config):
        self.vector_config = vector_config


class _FakeConfig:
    def __init__(self, vector_config, *, raises: bool = False):
        self._vc = vector_config
        self._raises = raises

    def get(self):
        if self._raises:
            raise RuntimeError("schema read failed (simulated)")
        return _FakeConfigPayload(self._vc)


class _FakeCollection:
    def __init__(self, store, config, *, reject_include_vector=False):
        self._store = store
        self.query = _FakeQuery(
            store, reject_include_vector=reject_include_vector
        )
        self.data = _FakeData(store)
        self.config = config


class _FakeCollections:
    def __init__(self, *, vector_config, schema_raises=False,
                 reject_include_vector=False):
        self._stores: dict[str, dict] = {}
        self._vector_config = vector_config
        self._schema_raises = schema_raises
        self._reject_include_vector = reject_include_vector

    def _store_for(self, name: str) -> dict:
        return self._stores.setdefault(name, {})

    def get(self, name: str) -> _FakeCollection:
        return _FakeCollection(
            self._store_for(name),
            _FakeConfig(self._vector_config, raises=self._schema_raises),
            reject_include_vector=self._reject_include_vector,
        )

    def exists(self, name: str) -> bool:  # noqa: ARG002
        return True

    def create(self, **kwargs):  # noqa: ARG002
        pass


class _FakeClient:
    def __init__(self, **kw):
        self.collections = _FakeCollections(**kw)


class _FakeEmbeddingService:
    text_model_id = "qwen3-embedding:0.6b"


class _CountingServer:
    """Fake server whose embed calls are COUNTED — the fast-path metric."""

    def __init__(self, slot: str = ACTIVE_SLOT, **client_kw):
        self.client = _FakeClient(**client_kw)
        self.embedding_service = _FakeEmbeddingService()
        self.text_vector_slot = slot
        self.embed_calls = 0

    def _get_embedding(self, text):  # noqa: ARG002
        self.embed_calls += 1
        return [0.9, 0.9, 0.9]

    def _get_all_kg_embeddings(self, text):  # noqa: ARG002
        self.embed_calls += 1
        return {self.text_vector_slot: [0.9, 0.9, 0.9]}

    def _get_all_kg_embeddings_tagged(self, text):  # noqa: ARG002
        self.embed_calls += 1
        return {self.text_vector_slot: [0.9, 0.9, 0.9]}, []


_ENV_KEYS = (
    "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
    "VCT_DISABLE_HUB_RESOLVER", "SHARED_KG_WRITE_DISABLED",
    "SHARED_KG_OPT_OUT", "VCT_PROJECT_ID", "KG_SYNC_PROJECT_ROOT",
    "VCT_STATE_DIR", "WEAVIATE_URL", "VCT_ORCHESTRATOR_ROOT",
)

#: A multi-slot class that DECLARES the active slot — the only schema
#: shape in which the gate is allowed to force work.
DECLARES_ACTIVE = {ACTIVE_SLOT: object(), OTHER_SLOT: object()}
#: A class with named vectors that do NOT include the active slot.
DECLARES_OTHER_ONLY = {OTHER_SLOT: object()}
#: Pre-named-vector legacy class: ONE unnamed vector. `vector_config` is
#: None on weaviate-client 4.x — verified live, see vco_lib/kg_vector_slot.py.
LEGACY_UNNAMED = None


def _load_sync_module(project_root: Path):
    """Load a FRESH sync module instance bound to *project_root*.

    ``VCT_STATE_DIR`` / ``WEAVIATE_URL`` are pinned to disposable values so
    no run touches live state, and ``VCT_ORCHESTRATOR_ROOT`` to THIS repo so
    the import cannot bind ``weaviate_mcp.*`` from another checkout.
    """
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = PROJECT_KG
    os.environ["SHARED_KG_COLLECTION"] = SHARED_KG
    os.environ["DEVELOPMENT_COLLECTION"] = DEV_COLL
    # The shipped default. The write path then hands Weaviate a
    # {slot: vector} map, which is what a named-vector class stores and
    # returns — the shape the gate reads.
    os.environ["DUAL_EMBEDDING_ENABLED"] = "true"
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    os.environ["VCT_STATE_DIR"] = str(project_root / ".vct-state-disposable")
    os.environ["WEAVIATE_URL"] = "http://127.0.0.1:9"
    os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    for gone in ("KG_SYNC_PROJECT_ROOT", "SHARED_KG_WRITE_DISABLED",
                 "SHARED_KG_OPT_OUT", "VCT_PROJECT_ID"):
        os.environ.pop(gone, None)

    mod_name = f"_sync_kg_slot_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, SYNC_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest(
            f"sync_knowledge_graph.py has runtime deps not installed ({exc})"
        )
    mod.Filter = _FakeFilter
    return mod


def _write_node(path: Path, title: str, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        f"title: {title}\n"
        "type: concept\n"
        "tags: [test]\n"
        "status: active\n"
        "created: 2026-01-01T00:00:00Z\n"
        "updated: 2026-01-01T00:00:00Z\n"
        "---\n"
        f"{body}\n"
    )
    path.write_text(text, encoding="utf-8")
    return text


def _write_doc(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"# Doc title\n\n{body}\n"
    path.write_text(text, encoding="utf-8")
    return text


def _all_rows(server: _CountingServer):
    """Every stored object across every collection of the fake client."""
    out = []
    for store in server.client.collections._stores.values():
        out.extend(store.values())
    return out


def _drop_slot(server: _CountingServer, slot: str) -> int:
    """Remove *slot* from every stored object's vector map.

    This is the on-disk state an embedding-model change leaves: content
    untouched (so every ``content_hash`` still matches), the previous
    model's slot populated, the new ACTIVE slot empty.
    """
    touched = 0
    for obj in _all_rows(server):
        if isinstance(obj.vector, dict) and slot in obj.vector:
            obj.vector = {
                k: v for k, v in obj.vector.items() if k != slot
            } or {OTHER_SLOT: [0.5, 0.5, 0.5]}
            touched += 1
    return touched


class _GateTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_snapshot = {k: os.environ.get(k) for k in _ENV_KEYS}
        self.addCleanup(self._restore_env)
        self.addCleanup(self._tmp.cleanup)

    def _restore_env(self):
        for k, v in self._env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _sync_node(self, mod, server, path: Path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = mod.sync_node(server, path)
        return outcome, buf.getvalue()

    def _sync_doc(self, mod, server, path: Path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = mod.sync_doc(server, path)
        return outcome, buf.getvalue()


class SlotPromiseTextTests(unittest.TestCase):
    """WP-0(a): the promise text itself is the artifact under test.

    A source scan is the correct instrument here precisely BECAUSE the
    subject is prose — it is not a wiring assertion standing in for a
    behavioural one (the behaviour is pinned by every class below).
    """

    def setUp(self) -> None:
        self.src = SYNC_SCRIPT.read_text(encoding="utf-8")
        self.flat = _collapsed(self.src)

    def test_false_implicit_reliance_claim_is_gone(self) -> None:
        self.assertNotIn(
            FALSE_CLAIM,
            self.flat,
            "sync_doc's fast-path comment again claims sync_node relies on "
            "the active-slot check implicitly via the chunk_count gate. It "
            "does not — chunk_count_ok counts objects and reads no vector.",
        )

    def test_the_asymmetry_is_stated_where_the_claim_was(self) -> None:
        self.assertIn(
            "chunk_count_ok` counts OBJECTS",
            self.src,
            "the retired claim must be REPLACED by the true asymmetry, not "
            "merely deleted (promises are made true, not dropped)",
        )


class SlotGateDecisionTableTests(_GateTestBase):
    """Unit-level: every row of ``_active_slot_gate_ok``'s decision table.

    Only ONE row may return False. Any other row returning False is an
    unbounded re-embed of a whole collection.
    """

    def setUp(self):
        super().setUp()
        self.mod = _load_sync_module(self.root)

    def _rows(self, vector):
        return [_FakeObj("u1", {}, vector)]

    def _coll(self, vector_config, raises=False):
        return _FakeCollection(
            {}, _FakeConfig(vector_config, raises=raises)
        )

    def test_declared_slot_empty_is_the_only_veto(self):
        ok = self.mod._active_slot_gate_ok(
            self._coll(DECLARES_ACTIVE), "C",
            self._rows({OTHER_SLOT: [0.1]}), ACTIVE_SLOT,
            vectors_requested=True,
        )
        self.assertFalse(ok, "a declared-but-empty active slot must veto")

    def test_declared_slot_populated_permits_skip(self):
        ok = self.mod._active_slot_gate_ok(
            self._coll(DECLARES_ACTIVE), "C",
            self._rows({ACTIVE_SLOT: [0.1], OTHER_SLOT: [0.2]}), ACTIVE_SLOT,
            vectors_requested=True,
        )
        self.assertTrue(ok)

    def test_unreadable_schema_is_inconclusive_not_a_re_embed(self):
        ok = self.mod._active_slot_gate_ok(
            self._coll(DECLARES_ACTIVE, raises=True), "C",
            self._rows({OTHER_SLOT: [0.1]}), ACTIVE_SLOT,
            vectors_requested=True,
        )
        self.assertTrue(
            ok,
            "a schema read that FAILED is the absence of an answer; reading "
            "it as 'slot empty' re-embeds the whole collection",
        )

    def test_legacy_unnamed_vector_class_is_not_applicable(self):
        ok = self.mod._active_slot_gate_ok(
            self._coll(LEGACY_UNNAMED), "C",
            self._rows({}), ACTIVE_SLOT,
            vectors_requested=True,
        )
        self.assertTrue(
            ok,
            "a legacy single-unnamed-vector class has no named slot to "
            "populate; vetoing would re-embed it on every sync forever",
        )

    def test_slot_absent_from_schema_is_a_migration_matter(self):
        ok = self.mod._active_slot_gate_ok(
            self._coll(DECLARES_OTHER_ONLY), "C",
            self._rows({OTHER_SLOT: [0.1]}), ACTIVE_SLOT,
            vectors_requested=True,
        )
        self.assertTrue(
            ok,
            "re-embedding cannot add a slot to a schema — only "
            "migrate-collections can; the sync must not spin on it",
        )

    def test_vectors_never_requested_is_inconclusive(self):
        ok = self.mod._active_slot_gate_ok(
            self._coll(DECLARES_ACTIVE), "C",
            self._rows({}), ACTIVE_SLOT,
            vectors_requested=False,
        )
        self.assertTrue(
            ok,
            "with no vectors asked for, every payload is empty BY "
            "CONSTRUCTION and proves nothing",
        )

    def test_empty_active_slot_name_disables_the_gate(self):
        ok = self.mod._active_slot_gate_ok(
            self._coll(DECLARES_ACTIVE), "C",
            self._rows({}), "",
            vectors_requested=True,
        )
        self.assertTrue(ok)

    def test_schema_probed_once_per_collection(self):
        """The probe is an HTTP round-trip; at tree scale it must not be
        paid per node."""
        calls = {"n": 0}

        class _CountingConfig(_FakeConfig):
            def get(self):
                calls["n"] += 1
                return super().get()

        coll = _FakeCollection({}, _CountingConfig(DECLARES_ACTIVE))
        for _ in range(5):
            self.mod._active_slot_gate_ok(
                coll, "SameCollection",
                self._rows({ACTIVE_SLOT: [0.1]}), ACTIVE_SLOT,
                vectors_requested=True,
            )
        self.assertEqual(calls["n"], 1, "schema probe must be cached by name")


class SyncNodeSlotGateTests(_GateTestBase):
    """End-to-end through the production ``sync_node``."""

    def test_pay_once_survives_populated_slot(self):
        """REGRESSION GUARD: the whole point of the fast path. An unchanged
        node whose ACTIVE slot IS populated must still skip — if this goes
        red the gate re-embeds every knowledge graph on this machine."""
        node = self.root / "knowledge" / "concepts" / "n.md"
        _write_node(node, "N", "A short body sentence.")
        mod = _load_sync_module(self.root)
        server = _CountingServer(vector_config=DECLARES_ACTIVE)

        first, _ = self._sync_node(mod, server, node)
        self.assertEqual(first.status, mod.OUTCOME_SYNCED)
        after_first = server.embed_calls
        self.assertGreater(after_first, 0)

        second, out = self._sync_node(mod, server, node)
        self.assertEqual(
            second.status, mod.OUTCOME_EMBED_SKIPPED,
            f"unchanged node with a populated active slot re-embedded: {out}",
        )
        self.assertEqual(
            server.embed_calls, after_first,
            "the skip must cost ZERO embed calls",
        )

    def test_empty_active_slot_now_re_embeds(self):
        """RED CORE (WP-5): content unchanged, ACTIVE slot emptied — the
        residue of an aborted embedding-model change. Pre-fix sync_node
        skipped this forever and nothing else could see it."""
        node = self.root / "knowledge" / "concepts" / "n.md"
        _write_node(node, "N", "A short body sentence.")
        mod = _load_sync_module(self.root)
        server = _CountingServer(vector_config=DECLARES_ACTIVE)

        self._sync_node(mod, server, node)
        after_first = server.embed_calls
        touched = _drop_slot(server, ACTIVE_SLOT)
        self.assertGreater(touched, 0, "fixture must actually empty the slot")

        second, out = self._sync_node(mod, server, node)
        self.assertNotEqual(
            second.status, mod.OUTCOME_EMBED_SKIPPED,
            "a node whose ACTIVE vector slot is empty was skipped — the "
            f"v0.2.95 WP-5 defect is back. Output:\n{out}",
        )
        self.assertGreater(
            server.embed_calls, after_first,
            "the re-embed must actually happen, not just be announced",
        )

    def test_the_re_embed_repairs_the_slot_so_the_gate_cannot_loop(self):
        """A veto that its own remedy does not clear is an infinite
        re-embed. Third run must skip again."""
        node = self.root / "knowledge" / "concepts" / "n.md"
        _write_node(node, "N", "A short body sentence.")
        mod = _load_sync_module(self.root)
        server = _CountingServer(vector_config=DECLARES_ACTIVE)

        self._sync_node(mod, server, node)
        _drop_slot(server, ACTIVE_SLOT)
        self._sync_node(mod, server, node)
        after_repair = server.embed_calls

        third, out = self._sync_node(mod, server, node)
        self.assertEqual(
            third.status, mod.OUTCOME_EMBED_SKIPPED,
            f"the repair did not satisfy its own gate — loop risk:\n{out}",
        )
        self.assertEqual(server.embed_calls, after_repair)

    def test_unreadable_schema_keeps_pre_existing_skip(self):
        """The catastrophic-direction guard, end to end."""
        node = self.root / "knowledge" / "concepts" / "n.md"
        _write_node(node, "N", "A short body sentence.")
        mod = _load_sync_module(self.root)
        server = _CountingServer(
            vector_config=DECLARES_ACTIVE, schema_raises=True
        )

        self._sync_node(mod, server, node)
        after_first = server.embed_calls
        _drop_slot(server, ACTIVE_SLOT)

        second, out = self._sync_node(mod, server, node)
        self.assertEqual(
            second.status, mod.OUTCOME_EMBED_SKIPPED,
            f"an unreadable schema forced a re-embed: {out}",
        )
        self.assertEqual(server.embed_calls, after_first)

    def test_old_client_rejecting_include_vector_keeps_pre_existing_skip(self):
        """`vectors_requested=False` end to end: the fallback fetch must
        not be read as 'no vectors stored'."""
        node = self.root / "knowledge" / "concepts" / "n.md"
        _write_node(node, "N", "A short body sentence.")
        mod = _load_sync_module(self.root)
        server = _CountingServer(
            vector_config=DECLARES_ACTIVE, reject_include_vector=True
        )

        self._sync_node(mod, server, node)
        after_first = server.embed_calls

        second, out = self._sync_node(mod, server, node)
        self.assertEqual(
            second.status, mod.OUTCOME_EMBED_SKIPPED,
            f"a client that cannot return vectors forced a re-embed: {out}",
        )
        self.assertEqual(server.embed_calls, after_first)


class SyncDocSharedGateTests(_GateTestBase):
    """``sync_doc`` answers this question through the SAME home.

    Its own comment already promised that a fetch which cannot return
    vectors falls back to the hash-only check. The inline loop it used to
    run did the opposite — every row scored "not populated" and the whole
    docs tree re-embedded. That promise is now kept.
    """

    def test_docs_fallback_fetch_no_longer_re_embeds_everything(self):
        doc = self.root / "docs" / "guide.md"
        _write_doc(doc, "Some documentation body.")
        mod = _load_sync_module(self.root)
        server = _CountingServer(
            vector_config=DECLARES_ACTIVE, reject_include_vector=True
        )

        first, _ = self._sync_doc(mod, server, doc)
        self.assertEqual(first.status, mod.OUTCOME_SYNCED)
        after_first = server.embed_calls

        second, out = self._sync_doc(mod, server, doc)
        self.assertEqual(
            second.status, mod.OUTCOME_EMBED_SKIPPED,
            f"docs re-embedded because the vector fetch degraded: {out}",
        )
        self.assertEqual(server.embed_calls, after_first)

    def test_docs_empty_active_slot_still_re_embeds(self):
        """The capability sync_doc already had must survive the move to
        the shared home."""
        doc = self.root / "docs" / "guide.md"
        _write_doc(doc, "Some documentation body.")
        mod = _load_sync_module(self.root)
        server = _CountingServer(vector_config=DECLARES_ACTIVE)

        self._sync_doc(mod, server, doc)
        after_first = server.embed_calls
        self.assertGreater(_drop_slot(server, ACTIVE_SLOT), 0)

        second, out = self._sync_doc(mod, server, doc)
        self.assertNotEqual(
            second.status, mod.OUTCOME_EMBED_SKIPPED,
            f"sync_doc lost its active-slot check in the move: {out}",
        )
        self.assertGreater(server.embed_calls, after_first)


if __name__ == "__main__":
    unittest.main()
