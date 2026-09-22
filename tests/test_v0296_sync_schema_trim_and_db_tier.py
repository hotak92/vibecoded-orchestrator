# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 WP-2 — old-schema tolerance (trim, never skip) + the hub-less
launcher.db middle tier for the child's SHARED collection resolution.

Register issue 2 (the 2026-09-20 GUI-update hang): with the hub down —
which every update guarantees — the sync child resolved SHARED_KG_COLLECTION
from the env DEFAULT, which on machines with a pre-0.2.74 history names a
stale OLD-SCHEMA class (no ``chunk_num`` / ``total_chunks``; verified field
props listed in ``_OLD_SCHEMA_PROPS`` below). Every existing-row lookup then
errored on all three ladder rungs, and the raised third became the per-node
traceback storm that filled the 64 KiB launcher pipe.

Two fixes under test:

  (a) OLD-SCHEMA TOLERANCE — the lookup TRIMS its ask to the declared
      properties (it never SKIPS the lookup leg), the chunk-count gate the
      missing props feed is skipped, and the hash-gated embed-skip fast
      path stays LIVE: zero embeds on a fully-current tree over an
      old-schema class (the zero-re-embed pin, owner-verified
      load-bearing 2026-09-20).
  (b) THE HUB-LESS MIDDLE TIER — the CHILD's own SHARED resolution chain
      becomes hub → launcher.db (read-only) → env. install.py threads
      KG_COLLECTION and never SHARED_KG_COLLECTION (its explicit overlay
      never sets it — a SHARED value reaches the child only via install.py's
      the wholesale os.environ copy), so
      the DB tier is the only thing standing between a hub-down update
      window and the stale default name.

Hermetic: in-memory fake Weaviate (the ``test_v0295_kg_metadata_repair_on_skip``
fake family), ``WEAVIATE_URL`` pinned at the unroutable sentinel by
``tests/conftest.py``, ``VCT_STATE_DIR`` + ``VCT_LAUNCHER_DB_PATH`` redirected
into per-test temp dirs, real-schema launcher.db fixtures from
``tests/common/launcher_db_fixture.py``. No live Weaviate, no live launcher,
no live hub.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import add_project, make_launcher_db  # noqa: E402
from vco_lib.project_config import HubUnreachable  # noqa: E402

PROJECT_KG = "V0296T_KnowledgeGraph"
SHARED_KG = "V0296T_Shared_KnowledgeGraph"
DEV_COLL = "V0296T_Development"
ACTIVE_SLOT = "qwen3_embed"

#: The field-verified prop list of the stale pre-0.2.74 shared class
#: (register issue 2). No ``chunk_num``, no ``total_chunks``, no
#: ``external_links`` — everything the modern writer stores is here.
_OLD_SCHEMA_PROPS = (
    "title", "content", "file_path", "node_type", "tags", "links",
    "typed_links", "status", "content_hash", "created_at", "updated_at",
    "valid_from", "valid_until", "created", "updated",
)

#: The modern class shape (chunk props + external_links declared).
_MODERN_EXTRA_PROPS = ("chunk_num", "total_chunks", "external_links")

_NODE_A = """---
title: wp-two-node-a
type: concept
tags: [v0296, schema-trim]
---

Body A — references issue #2 of the register.
"""

_NODE_B = """---
title: wp-two-node-b
type: concept
tags: [v0296, db-tier]
---

Body B — references issue #2 of the register.
"""


# ─── In-memory fake Weaviate client ──────────────────────────────────────
# The test_v0295_kg_metadata_repair_on_skip.py fake family, extended with
# the two things WP-2(a) needs: a schema payload that DECLARES properties
# (the probe reads `config.properties`), and a fetch that ERRORS on any
# UNdeclared prop the way real Weaviate does ("no such prop with name …")
# — the exact error the incident's traceback storm was made of.


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
    def __init__(self, store: dict, opts: "_FakeOptions"):
        self._store = store
        self._opts = opts

    def fetch_objects(self, filters=None, limit=100, return_properties=None,
                      include_vector=False):
        asked = list(return_properties or [])
        for p in asked:
            if p not in self._opts.declared_props:
                # Real Weaviate errors the READ, it does not omit the field
                # — this is the error the pre-fix ladder died on, on every
                # rung, for every node.
                self._opts.fetch_prop_errors += 1
                raise RuntimeError(f"no such prop with name '{p}' found")
        self._opts.fetch_props_seen.append(tuple(asked))
        objs = [o for o in self._store.values()
                if filters is None or filters.matches(o.properties)][:limit]
        out = []
        for o in objs:
            props = (
                {k: v for k, v in o.properties.items() if k in asked}
                if asked else dict(o.properties)
            )
            out.append(_FakeObj(o.uuid, props, o.vector if include_vector else {}))
        return _FakeQueryResult(out)


class _FakeData:
    def __init__(self, store: dict, opts: "_FakeOptions"):
        self._store = store
        self._opts = opts

    def insert(self, properties=None, vector=None):  # noqa: ARG002
        uid = str(uuid.uuid4())
        self._opts.inserts += 1
        self._store[uid] = _FakeObj(uid, dict(properties or {}), vector)
        return uid

    def delete_by_id(self, uid):
        self._opts.deletes += 1
        self._store.pop(str(uid), None)

    def update(self, uuid=None, properties=None):  # noqa: A002 — client kwarg
        self._opts.updates.append((str(uuid), dict(properties or {})))
        obj = self._store.get(str(uuid))
        if obj is not None:
            obj.properties.update(properties or {})

    def reference_add(self, **kwargs):  # noqa: ARG002
        pass


class _FakeConfigPayload:
    def __init__(self, vector_config, properties):
        self.vector_config = vector_config
        self.properties = properties


class _FakeConfig:
    def __init__(self, opts: "_FakeOptions"):
        self._opts = opts

    def get(self):
        self._opts.config_get_calls += 1
        if self._opts.raise_config_get:
            raise RuntimeError("schema read failed (simulated)")
        return _FakeConfigPayload(
            self._opts.vector_config,
            [type("P", (), {"name": n})() for n in self._opts.declared_props],
        )


class _FakeCollection:
    def __init__(self, store, opts):
        self._store = store
        self._opts = opts
        self.query = _FakeQuery(store, opts)
        self.data = _FakeData(store, opts)
        self.config = _FakeConfig(opts)


class _FakeCollections:
    def __init__(self, opts: "_FakeOptions"):
        self._stores: "dict[str, dict]" = {}
        self._opts = opts

    def _store_for(self, name: str) -> dict:
        return self._stores.setdefault(name, {})

    def get(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._store_for(name), self._opts)

    def exists(self, name: str) -> bool:  # noqa: ARG001
        return True

    def create(self, **kwargs):  # noqa: ARG002
        pass


class _FakeClient:
    def __init__(self, opts: "_FakeOptions"):
        self.collections = _FakeCollections(opts)


class _FakeEmbeddingService:
    text_model_id = "qwen3-embedding:0.6b"


class _CountingServer:
    """Fake server whose embed calls are COUNTED — the load-bearing metric."""

    def __init__(self, **opt_kw):
        self.opts = _FakeOptions(**opt_kw)
        self.client = _FakeClient(self.opts)
        self.embedding_service = _FakeEmbeddingService()
        self.text_vector_slot = ACTIVE_SLOT
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


class _FakeOptions:
    """Per-client knobs + the ledgers the assertions read."""

    def __init__(self, *, declared_props=_OLD_SCHEMA_PROPS,
                 vector_config=None, raise_config_get=False):
        self.declared_props = tuple(declared_props)
        # None → a pre-named-vector class (one UNNAMED vector), the real
        # stale class's shape; the slot gate then does not apply.
        self.vector_config = vector_config
        self.raise_config_get = raise_config_get
        self.updates: list = []
        self.inserts = 0
        self.deletes = 0
        self.embed_calls = 0
        self.fetch_props_seen: list = []
        self.fetch_prop_errors = 0
        self.config_get_calls = 0


_ENV_KEYS = (
    "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
    "VCT_DISABLE_HUB_RESOLVER", "SHARED_KG_WRITE_DISABLED",
    "SHARED_KG_OPT_OUT", "VCT_PROJECT_ID", "KG_SYNC_PROJECT_ROOT",
    "VCT_STATE_DIR", "WEAVIATE_URL", "VCT_ORCHESTRATOR_ROOT",
    "VCT_LAUNCHER_DB_PATH",
)


def _load_sync_module(project_root: Path):
    """A FRESH sync module instance bound to *project_root*.

    Fresh per test so the module-level probe caches (`_PROP_SCHEMA_CACHE`,
    `_OLD_SCHEMA_TRIM_WARNED`, the counters) start empty — the once-per-run
    deductions are only observable against a fresh run.
    """
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = PROJECT_KG
    os.environ["SHARED_KG_COLLECTION"] = SHARED_KG
    os.environ["DEVELOPMENT_COLLECTION"] = DEV_COLL
    os.environ["DUAL_EMBEDDING_ENABLED"] = "true"
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    os.environ["VCT_STATE_DIR"] = str(project_root / ".vct-state-disposable")
    os.environ["WEAVIATE_URL"] = "http://127.0.0.1:9"
    os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    for gone in ("KG_SYNC_PROJECT_ROOT", "SHARED_KG_WRITE_DISABLED",
                 "SHARED_KG_OPT_OUT", "VCT_PROJECT_ID",
                 "VCT_LAUNCHER_DB_PATH"):
        os.environ.pop(gone, None)

    mod_name = f"_sync_kg_v0296_{uuid.uuid4().hex}"
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


class _V0296TestBase(unittest.TestCase):
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

    def _node_path(self, name: str, text: str) -> Path:
        path = self.root / "knowledge" / "concepts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _seed_old_schema_row(self, mod, server, path: Path,
                             stored_props: dict | None = None):
        """Insert the row a pre-0.2.74 writer left: matching hash, NO chunk
        props (the class would have rejected them), no vector slots."""
        text = path.read_text(encoding="utf-8")
        content_hash = mod._content_signature_excluding_updated(text)
        store = server.client.collections._store_for(PROJECT_KG)
        uid = f"row-{path.stem}"
        props = {
            "file_path": mod._canonical_file_path(path),
            "content_hash": content_hash,
            "content": text,
            "links": [],
        }
        props.update(stored_props or {})
        store[uid] = _FakeObj(uid, props, {})
        return uid

    def _sync(self, mod, server, path: Path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = mod.sync_node(server, path)
        return outcome, buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────
# (a) OLD-SCHEMA TOLERANCE — trim, never skip.
# ─────────────────────────────────────────────────────────────────────────


class OldSchemaTrimTests(_V0296TestBase):
    """The stale pre-0.2.74 class: trimmed ask, live fast path, one line."""

    def test_old_schema_class_skips_with_zero_embeds_and_one_warning(self):
        """THE pin: the zero-re-embed promise holds on an old-schema class.

        Two nodes prove the warning dedup is per run, not per node. Every
        assertion that could pass on a re-embed path is paired with the
        embed/delete/insert counters.
        """
        mod = _load_sync_module(self.root)
        server = _CountingServer()  # declared_props = _OLD_SCHEMA_PROPS
        path_a = self._node_path("wp-two-node-a.md", _NODE_A)
        path_b = self._node_path("wp-two-node-b.md", _NODE_B)
        self._seed_old_schema_row(
            mod, server, path_a,
            {"title": "wp-two-node-a", "node_type": "concept",
             "tags": ["v0296", "schema-trim"]},
        )
        self._seed_old_schema_row(
            mod, server, path_b,
            {"title": "wp-two-node-b", "node_type": "concept",
             "tags": ["v0296", "db-tier"]},
        )

        outcome_a, out_a = self._sync(mod, server, path_a)
        outcome_b, out_b = self._sync(mod, server, path_b)
        out = out_a + out_b

        self.assertEqual(outcome_a.status, mod.OUTCOME_EMBED_SKIPPED)
        self.assertEqual(outcome_b.status, mod.OUTCOME_EMBED_SKIPPED)
        self.assertEqual(
            server.embed_calls, 0,
            "content unchanged on an old-schema class — the trimmed lookup "
            "must keep the hash-gated skip alive, never re-embed the class",
        )
        self.assertEqual(server.opts.deletes, 0)
        self.assertEqual(server.opts.inserts, 0)
        self.assertEqual(
            server.opts.updates, [],
            "external_links is undeclared on the stale class, so the "
            "metadata repair must read 'no information', never a difference",
        )
        self.assertEqual(
            out.count("Old schema on"), 1,
            "exactly ONE warning line per run when the trim activates — "
            "two synced nodes must not double it",
        )
        self.assertIn(PROJECT_KG, out)
        # No per-node error churn: the first ask already fits the schema.
        self.assertEqual(
            server.opts.fetch_prop_errors, 0,
            "the trimmed ask must SUCCEED on the first rung — a raised "
            "fetch per node is the traceback storm this WP exists to kill",
        )

    def test_trimmed_ask_is_the_declared_intersection(self):
        """The ask keeps file_path + content_hash and drops the undeclared."""
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        path = self._node_path("wp-two-node-a.md", _NODE_A)
        self._seed_old_schema_row(mod, server, path)
        self._sync(mod, server, path)

        first = server.opts.fetch_props_seen[0]
        self.assertIn("file_path", first)
        self.assertIn("content_hash", first)
        self.assertNotIn("chunk_num", first)
        self.assertNotIn("total_chunks", first)
        self.assertNotIn("external_links", first)
        # Declared repairables still ride the first fetch (title IS declared
        # on the stale class) — the repair arm is trimmed, not amputated.
        self.assertIn("title", first)
        self.assertIn("node_type", first)
        self.assertIn("tags", first)

    def test_schema_probe_is_once_per_run(self):
        """Two nodes, two probes total (props + slots) — not one per node."""
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        path_a = self._node_path("wp-two-node-a.md", _NODE_A)
        path_b = self._node_path("wp-two-node-b.md", _NODE_B)
        self._seed_old_schema_row(mod, server, path_a)
        self._seed_old_schema_row(mod, server, path_b)
        self._sync(mod, server, path_a)
        self._sync(mod, server, path_b)
        # 1 = _collection_declared_props (WP-2(a)); 1 = the v0.2.95 slot
        # probe for _active_slot_gate_ok — both cached per collection name.
        self.assertEqual(
            server.opts.config_get_calls, 2,
            "the schema probe is ONCE per run per class — per-node probes "
            "would re-roundtrip the schema for every file in the tree",
        )

    def test_changed_content_still_reembeds_on_old_schema(self):
        """The trim must not become a way to AVOID a needed re-embed."""
        mod = _load_sync_module(self.root)
        server = _CountingServer(declared_props=_OLD_SCHEMA_PROPS)
        path = self._node_path("wp-two-node-a.md", _NODE_A)
        self._seed_old_schema_row(mod, server, path)
        path.write_text(_NODE_A + "\nA genuinely new paragraph.\n",
                        encoding="utf-8")

        outcome, _ = self._sync(mod, server, path)

        self.assertEqual(outcome.status, mod.OUTCOME_SYNCED)
        self.assertGreater(server.embed_calls, 0)
        self.assertEqual(server.opts.deletes, 1)
        self.assertEqual(server.opts.inserts, 1)


class ModernSchemaUntrimmedTests(_V0296TestBase):
    """The negative: a MODERN class keeps today's ask and gates exactly."""

    def test_modern_class_asks_chunk_props_and_never_trims(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer(
            declared_props=_OLD_SCHEMA_PROPS + _MODERN_EXTRA_PROPS,
            vector_config={ACTIVE_SLOT: object()},
        )
        path = self._node_path("wp-two-node-a.md", _NODE_A)
        text = path.read_text(encoding="utf-8")
        store = server.client.collections._store_for(PROJECT_KG)
        store["row-1"] = _FakeObj("row-1", {
            "file_path": mod._canonical_file_path(path),
            "content_hash": mod._content_signature_excluding_updated(text),
            "chunk_num": 1,
            "total_chunks": 1,
            "content": text,
            "links": [],
            "title": "wp-two-node-a",
            "node_type": "concept",
            "tags": ["v0296", "schema-trim"],
            "external_links": "",
        }, {ACTIVE_SLOT: [0.1, 0.2, 0.3]})

        outcome, out = self._sync(mod, server, path)

        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED)
        self.assertEqual(server.embed_calls, 0)
        first = server.opts.fetch_props_seen[0]
        self.assertIn("chunk_num", first, "a modern class keeps the full ask")
        self.assertIn("total_chunks", first)
        self.assertIn("external_links", first)
        self.assertNotIn("Old schema on", out)

    def test_probe_failure_keeps_the_hard_ask(self):
        """Undeterminable schema (probe raises) → NO trim: today's ladder.

        A probe that cannot CONFIRM an old schema must not guess a trim —
        the conservative default is the pre-v0.2.96 ask, whose failure mode
        is the loud per-node error (OUTCOME_FAILED + the chained traceback
        printed to stderr — the register's storm), not a silently narrowed
        read that silently drops rows from the lookup.
        """
        mod = _load_sync_module(self.root)
        server = _CountingServer(raise_config_get=True)
        path = self._node_path("wp-two-node-a.md", _NODE_A)
        self._seed_old_schema_row(mod, server, path)

        outcome = None
        err_buf = io.StringIO()
        out_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), \
                contextlib.redirect_stderr(err_buf):
            outcome = mod.sync_node(server, path)

        self.assertEqual(outcome.status, mod.OUTCOME_FAILED)
        self.assertIn("no such prop with name 'chunk_num' found", err_buf.getvalue())
        self.assertGreater(
            server.opts.fetch_prop_errors, 0,
            "the hard ask names undeclared props — that is the pre-fix "
            "behaviour a failed probe must preserve",
        )


# ─────────────────────────────────────────────────────────────────────────
# (a-bis) THE SAME TOLERANCE ON THE **DEVELOPMENT** COLLECTION.
#
# v0.2.96 ship-gate F-L5: WP-2(a) fixed the KG collection and left `sync_doc`
# ending its ladder in `existing = None  # forces fall-through to re-embed`,
# so a pre-0.2.74 `<X>_Development` class re-embedded every unchanged doc on
# every sync — the same anti-promise, on the other collection. These pins are
# the doc-side counterparts of `OldSchemaTrimTests` /
# `ModernSchemaUntrimmedTests` above.
# ─────────────────────────────────────────────────────────────────────────


class DocOldSchemaTrimTests(_V0296TestBase):
    """`sync_doc` on a stale DEVELOPMENT class: trimmed ask, live fast path."""

    def _doc_path(self, name: str, text: str) -> Path:
        path = self.root / "docs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _seed_doc_row(self, mod, server, path: Path):
        """The row a pre-0.2.74 DEV writer left: matching hash, NO chunk
        props (the class would have rejected them), no named-vector slots."""
        text = path.read_text(encoding="utf-8")
        store = server.client.collections._store_for(DEV_COLL)
        uid = f"doc-{path.stem}"
        store[uid] = _FakeObj(uid, {
            "file_path": mod._canonical_file_path(path),
            "content_hash": mod._content_signature_excluding_updated(text),
            "content": text,
        }, {})
        return uid

    def _sync_doc(self, mod, server, path: Path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = mod.sync_doc(server, path)
        return outcome, buf.getvalue()

    def test_old_schema_dev_class_skips_with_zero_embeds_and_one_warning(self):
        """THE pin: the zero-re-embed promise holds for `docs/` too.

        Two docs prove the warning dedup is per (run, collection), not per
        file. Every assertion that a re-embed path could also satisfy is
        paired with the embed / delete / insert counters.
        """
        mod = _load_sync_module(self.root)
        server = _CountingServer()  # declared_props = _OLD_SCHEMA_PROPS
        doc_a = self._doc_path("guide-a.md", "# Guide A\n\nBody A.\n")
        doc_b = self._doc_path("guide-b.md", "# Guide B\n\nBody B.\n")
        self._seed_doc_row(mod, server, doc_a)
        self._seed_doc_row(mod, server, doc_b)

        outcome_a, out_a = self._sync_doc(mod, server, doc_a)
        outcome_b, out_b = self._sync_doc(mod, server, doc_b)
        out = out_a + out_b

        self.assertEqual(outcome_a.status, mod.OUTCOME_EMBED_SKIPPED)
        self.assertEqual(outcome_b.status, mod.OUTCOME_EMBED_SKIPPED)
        self.assertEqual(
            server.embed_calls, 0,
            "content unchanged on an old-schema DEVELOPMENT class — the "
            "trimmed lookup must keep the hash-gated skip alive",
        )
        self.assertEqual(server.opts.deletes, 0)
        self.assertEqual(server.opts.inserts, 0)
        self.assertEqual(
            out.count("Old schema on"), 1,
            "exactly ONE warning line per run per collection",
        )
        self.assertIn(DEV_COLL, out)
        self.assertEqual(
            server.opts.fetch_prop_errors, 0,
            "the trimmed ask must SUCCEED on the first rung",
        )

    def test_trimmed_doc_ask_is_the_declared_intersection(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        doc = self._doc_path("guide-a.md", "# Guide A\n\nBody A.\n")
        self._seed_doc_row(mod, server, doc)
        self._sync_doc(mod, server, doc)

        first = server.opts.fetch_props_seen[0]
        self.assertIn("file_path", first)
        self.assertIn("content_hash", first)
        self.assertNotIn("chunk_num", first)
        self.assertNotIn("total_chunks", first)

    def test_doc_schema_probe_is_once_per_run(self):
        """Two docs, two probes total (props + slots) — not one per file."""
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        doc_a = self._doc_path("guide-a.md", "# Guide A\n\nBody A.\n")
        doc_b = self._doc_path("guide-b.md", "# Guide B\n\nBody B.\n")
        self._seed_doc_row(mod, server, doc_a)
        self._seed_doc_row(mod, server, doc_b)
        self._sync_doc(mod, server, doc_a)
        self._sync_doc(mod, server, doc_b)
        self.assertEqual(
            server.opts.config_get_calls, 2,
            "the schema probe is ONCE per run per class",
        )

    def test_changed_doc_still_reembeds_on_old_schema(self):
        """The trim must not become a way to AVOID a needed re-embed."""
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        doc = self._doc_path("guide-a.md", "# Guide A\n\nBody A.\n")
        self._seed_doc_row(mod, server, doc)
        doc.write_text("# Guide A\n\nA genuinely new paragraph.\n",
                       encoding="utf-8")

        outcome, _ = self._sync_doc(mod, server, doc)

        self.assertEqual(outcome.status, mod.OUTCOME_SYNCED)
        self.assertGreater(server.embed_calls, 0)
        self.assertEqual(server.opts.deletes, 1)


class DocModernSchemaUntrimmedTests(_V0296TestBase):
    """The negative: a MODERN DEVELOPMENT class keeps today's ask."""

    def _doc_path(self, name: str, text: str) -> Path:
        path = self.root / "docs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_modern_dev_class_asks_chunk_props_and_never_trims(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer(
            declared_props=_OLD_SCHEMA_PROPS + _MODERN_EXTRA_PROPS,
            vector_config={ACTIVE_SLOT: object()},
        )
        doc = self._doc_path("guide-a.md", "# Guide A\n\nBody A.\n")
        text = doc.read_text(encoding="utf-8")
        store = server.client.collections._store_for(DEV_COLL)
        store["doc-1"] = _FakeObj("doc-1", {
            "file_path": mod._canonical_file_path(doc),
            "content_hash": mod._content_signature_excluding_updated(text),
            "chunk_num": 1,
            "total_chunks": 1,
            "content": text,
        }, {ACTIVE_SLOT: [0.1, 0.2, 0.3]})

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = mod.sync_doc(server, doc)
        out = buf.getvalue()

        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED)
        self.assertEqual(server.embed_calls, 0)
        first = server.opts.fetch_props_seen[0]
        self.assertIn("chunk_num", first, "a modern class keeps the full ask")
        self.assertIn("total_chunks", first)
        self.assertNotIn("Old schema on", out)

    def test_doc_probe_failure_keeps_the_hard_ask(self):
        """Undeterminable schema (probe raises) → NO trim: today's ladder.

        A probe that cannot CONFIRM an old schema must not guess a trim. On
        the doc side the pre-existing failure mode is the ladder's
        `existing = None` fall-through, i.e. a re-embed — loud in the
        counters, never a silently narrowed read.
        """
        mod = _load_sync_module(self.root)
        server = _CountingServer(raise_config_get=True)
        doc = self._doc_path("guide-a.md", "# Guide A\n\nBody A.\n")
        text = doc.read_text(encoding="utf-8")
        store = server.client.collections._store_for(DEV_COLL)
        store["doc-1"] = _FakeObj("doc-1", {
            "file_path": mod._canonical_file_path(doc),
            "content_hash": mod._content_signature_excluding_updated(text),
            "content": text,
        }, {})

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = mod.sync_doc(server, doc)

        self.assertEqual(outcome.status, mod.OUTCOME_SYNCED)
        self.assertGreater(
            server.opts.fetch_prop_errors, 0,
            "the hard ask names undeclared props — that is the pre-fix "
            "behaviour a failed probe must preserve",
        )
        self.assertNotIn("Old schema on", buf.getvalue())


# ─────────────────────────────────────────────────────────────────────────
# (b) THE HUB-LESS MIDDLE TIER — hub → launcher.db (read-only) → env.
# ─────────────────────────────────────────────────────────────────────────


class DbMiddleTierTests(_V0296TestBase):
    """The CHILD's own SHARED resolution, with the hub deliberately down."""

    DB_SHARED = "MachineCanonical_Shared_KG"
    ENV_DEFAULT_SHARED = "StaleDefault_KnowledgeGraph"

    def _setUp_db(self, *, register_self: bool = True, root_shared=None):
        """Fixture launcher.db (REAL schema) + env shaped like the update
        window: hub down, env SHARED carrying the stale default."""
        mod = _load_sync_module(self.root)
        db_path = make_launcher_db(self.root / "db")
        if register_self:
            add_project(
                db_path,
                project_id="p1",
                name="V0296 Proj",
                folder_path=str(self.root),
                host="base",
                kg_primary=PROJECT_KG,
                kg_shared=self.DB_SHARED,
            )
        if root_shared is not None:
            add_project(
                db_path,
                project_id="root-pid",
                name="orchestrator-root",
                folder_path=str(self.root / "elsewhere"),
                host="orchestrator_root",
                kg_primary="RootCanonical_KG",
                kg_shared=root_shared,
            )
        os.environ["VCT_LAUNCHER_DB_PATH"] = str(db_path)
        os.environ["SHARED_KG_COLLECTION"] = self.ENV_DEFAULT_SHARED
        return mod

    def _hub_down(self):
        """Pop the test gate and make the hub resolver positively fail."""
        patcher = mock.patch(
            "vco_lib.project_config.resolve",
            side_effect=HubUnreachable("hub down (update window)"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("VCT_DISABLE_HUB_RESOLVER", None)
        return patcher

    def test_hub_down_db_binding_beats_the_env_default(self):
        """THE pin: the DB's shared-collection name, not the env default."""
        mod = self._setUp_db()
        self._hub_down()

        kg, dev, shared, resolved = mod._resolve_collections()

        self.assertEqual(
            shared, self.DB_SHARED,
            "hub down + launcher.db binding present → the child resolves "
            "the DB's shared-collection name (install.py threads "
            "KG_COLLECTION and never SHARED_KG_COLLECTION — "
            "install.py:15088)",
        )
        self.assertNotEqual(shared, self.ENV_DEFAULT_SHARED)
        self.assertEqual(kg, PROJECT_KG)
        self.assertTrue(resolved)

    def test_hub_down_busy_db_soft_fails_to_env_without_error(self):
        """A locked launcher.db → env default, silently, no exception."""
        mod = self._setUp_db()
        self._hub_down()
        lock = mock.patch(
            "sqlite3.connect",
            side_effect=sqlite3.OperationalError("database is locked"),
        )
        lock.start()
        self.addCleanup(lock.stop)

        kg, dev, shared, resolved = mod._resolve_collections()

        self.assertEqual(
            shared, self.ENV_DEFAULT_SHARED,
            "busy/locked DB soft-fails to the next tier — the update "
            "window's real DB state is the normal case, not an error",
        )
        self.assertEqual(kg, PROJECT_KG)
        self.assertTrue(resolved)

    def test_child_shared_path_consults_the_db_tier(self):
        """The PIN: the CHILD's own resolution calls the reader (role=shared).

        Not only an install-side caller — the exact hazard (register issue
        2 REFINEMENT) is the child's runtime resolution with the hub down.
        """
        mod = self._setUp_db()
        self._hub_down()
        calls: list[tuple[str, str]] = []
        real_get_kg_binding = __import__(
            "vco_lib.launcher_db_reader", fromlist=["get_kg_binding"]
        ).get_kg_binding

        def _spy(project_id, role):
            calls.append((project_id, role))
            return real_get_kg_binding(project_id, role)

        with mock.patch(
            "vco_lib.launcher_db_reader.get_kg_binding", side_effect=_spy
        ):
            _kg, _dev, shared, _resolved = mod._resolve_collections()

        self.assertEqual(shared, self.DB_SHARED)
        self.assertEqual(
            calls, [("p1", "shared")],
            "the child's SHARED resolution must consult the DB tier itself, "
            "asking for the shared role of the registered project",
        )

    def test_unregistered_root_falls_back_to_the_orchestrator_root_row(self):
        """No folder match → the host-keyed root row (get_orchestrator_root_
        bindings), the same read install.py's V44-G1 chain performs."""
        mod = self._setUp_db(register_self=False, root_shared="RootShared_KG")
        self._hub_down()

        _kg, _dev, shared, _resolved = mod._resolve_collections()

        self.assertEqual(shared, "RootShared_KG")

    def test_hub_up_wins_and_the_db_tier_never_runs(self):
        """Tier ordering: the hub stays authoritative; the DB is middle."""
        mod = self._setUp_db()
        os.environ.pop("VCT_DISABLE_HUB_RESOLVER", None)
        hub_cfg = type("Cfg", (), {
            "kg_collection": "Hub_KG",
            "development_collection": "Hub_Dev",
            "shared_kg_collection": "Hub_Shared",
        })()
        bomb = mock.patch(
            "vco_lib.launcher_db_reader.get_kg_binding",
            side_effect=AssertionError("DB tier must not run when hub is up"),
        )
        bomb.start()
        self.addCleanup(bomb.stop)

        with mock.patch(
            "vco_lib.project_config.resolve", return_value=hub_cfg
        ):
            kg, dev, shared, resolved = mod._resolve_collections()

        self.assertEqual(kg, "Hub_KG")
        self.assertEqual(dev, "Hub_Dev")
        self.assertEqual(shared, "Hub_Shared")
        self.assertTrue(resolved)

    def test_absent_db_soft_fails_to_env(self):
        """No launcher.db at all (free-tier install) → env, silently."""
        mod = self._load_module_no_db()
        self._hub_down()

        _kg, _dev, shared, _resolved = mod._resolve_collections()

        self.assertEqual(shared, self.ENV_DEFAULT_SHARED)

    def _load_module_no_db(self):
        os.environ["KG_BASE_DIR"] = str(self.root)
        os.environ["KG_COLLECTION"] = PROJECT_KG
        os.environ["SHARED_KG_COLLECTION"] = self.ENV_DEFAULT_SHARED
        os.environ["VCT_STATE_DIR"] = str(self.root / ".vct-none")
        os.environ.pop("VCT_LAUNCHER_DB_PATH", None)
        mod = _load_sync_module(self.root)
        os.environ["SHARED_KG_COLLECTION"] = self.ENV_DEFAULT_SHARED
        return mod


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
