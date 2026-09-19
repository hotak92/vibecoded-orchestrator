# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 — the embed-skip path REPAIRS stale KG metadata, without embedding.

The defect. ``_normalise_frontmatter`` (new in v0.2.95) promotes a nested
``metadata:`` block, reads ``name:`` as ``title:`` and splits a string
``tags:`` — on every NEW write. The embed-skip gate, however, compares the
file's TEXT signature against the stored ``content_hash`` and nothing else,
so a node already stored under the old parse keeps its wrong ``tags`` and
``node_type`` in Weaviate for as long as its text is untouched.
``kg-sync --all`` did not repair it either: the skip returned having written
no property at all, so a bulk run printed its "promoted nested `metadata:`
keys" line over a row it left exactly as wrong as it found it.

The fix under test is a metadata PATCH on the skip path — never a re-embed
and never a hash change, because the hash has readers that are not this gate
(install.py's CI-10 seed-diff gate and ``vco_lib.kg_sync_drift`` RECOMPUTE
it from the file; the shipped-vector sidecar is keyed on it; the curated
provenance registry gates a DELETION on it).

Two properties are asserted together throughout, because either alone would
pass a broken implementation:

  1. the stored row ENDS UP CORRECT, and
  2. the run performed ZERO embeds (asserted on the embed CALL COUNT, not on
     the absence of an error).

No live Weaviate: an in-memory fake, ``WEAVIATE_URL`` pinned at the
unroutable sentinel, ``VCT_STATE_DIR`` redirected into a temp root.
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
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.codegraph_guards import RowAction  # noqa: E402
from vco_lib import kg_metadata_repair as repair  # noqa: E402

PROJECT_KG = "V0295R_KnowledgeGraph"
SHARED_KG = "V0295R_Shared_KnowledgeGraph"
DEV_COLL = "V0295R_Development"
ACTIVE_SLOT = "qwen3_embed"

#: The field-reported nested dialect (same shape as
#: tests/test_v0295_kg_frontmatter_dialects.py's regression fixture).
NESTED_NODE = """---
name: pay-payment-orchestration
description: PAY — payment orchestration module 6.3
metadata:
  type: concept
  tags: [Acme, Acme-PAY, payments]
---

See issues #4 and #12 (also section #14) for the incident history.
"""

#: What a pre-v0.2.95 writer stored for that file: the folder name as the
#: type, prose ``#`` references scraped as tags, the filename stem as title.
STALE_STORED = {
    "title": "pay-payment-orchestration-file",
    "node_type": "concepts",
    "tags": ["4", "12", "14"],
    "external_links": "",
}


# ─── In-memory fake Weaviate client ──────────────────────────────────────
# Same fake family as tests/test_v0295_kg_active_slot_gate.py, plus the two
# things this module's subject needs and it does not have: a `data.update`
# (the metadata patch) and a `fetch_objects` that HONOURS
# `return_properties` — the fallback fetch arm's whole point is that the
# repairable properties are then absent, and a fake that returns them
# anyway could not tell the arms apart.


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
        if self._opts.reject_metadata_props and any(
            p in asked for p in repair.REPAIRABLE_PROPERTIES
        ):
            # A collection whose schema predates one of these properties:
            # Weaviate errors the READ rather than omitting the field.
            raise RuntimeError("no such prop with name 'node_type' found")
        if include_vector and self._opts.reject_include_vector:
            raise TypeError(
                "fetch_objects() got an unexpected keyword argument "
                "'include_vector'"
            )
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

    def insert(self, properties=None, vector=None):
        uid = str(uuid.uuid4())
        self._opts.inserts += 1
        self._store[uid] = _FakeObj(uid, dict(properties or {}), vector)
        return uid

    def delete_by_id(self, uid):
        self._opts.deletes += 1
        self._store.pop(str(uid), None)

    def update(self, uuid=None, properties=None):  # noqa: A002 — client kwarg
        uid = str(uuid)
        self._opts.updates.append((uid, dict(properties or {})))
        if uid in self._opts.fail_update_for:
            raise RuntimeError("simulated patch failure")
        obj = self._store.get(uid)
        if obj is not None:
            obj.properties.update(properties or {})

    def reference_add(self, **kwargs):  # noqa: ARG002
        pass


class _FakeOptions:
    """Per-client knobs + write ledger (what the assertions read)."""

    def __init__(self, *, reject_include_vector=False,
                 reject_metadata_props=False, fail_update_for=()):
        self.reject_include_vector = reject_include_vector
        self.reject_metadata_props = reject_metadata_props
        self.fail_update_for = set(fail_update_for)
        self.updates: list = []
        self.inserts = 0
        self.deletes = 0
        self.fetch_props_seen: list = []


class _FakeConfigPayload:
    def __init__(self, vector_config):
        self.vector_config = vector_config


class _FakeConfig:
    def __init__(self, vector_config):
        self._vc = vector_config

    def get(self):
        return _FakeConfigPayload(self._vc)


class _FakeCollection:
    def __init__(self, store, opts):
        self._store = store
        self.query = _FakeQuery(store, opts)
        self.data = _FakeData(store, opts)
        self.config = _FakeConfig({ACTIVE_SLOT: object()})


class _FakeCollections:
    def __init__(self, opts: _FakeOptions):
        self._stores: "dict[str, dict]" = {}
        self._opts = opts

    def _store_for(self, name: str) -> dict:
        return self._stores.setdefault(name, {})

    def get(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._store_for(name), self._opts)

    def exists(self, name: str) -> bool:  # noqa: ARG002
        return True

    def create(self, **kwargs):  # noqa: ARG002
        pass


class _FakeClient:
    def __init__(self, opts: _FakeOptions):
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


_ENV_KEYS = (
    "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
    "VCT_DISABLE_HUB_RESOLVER", "SHARED_KG_WRITE_DISABLED",
    "SHARED_KG_OPT_OUT", "VCT_PROJECT_ID", "KG_SYNC_PROJECT_ROOT",
    "VCT_STATE_DIR", "WEAVIATE_URL", "VCT_ORCHESTRATOR_ROOT",
)


def _load_sync_module(project_root: Path):
    """A FRESH sync module instance bound to *project_root*.

    Fresh per test so the module-level repair counters start at zero and one
    test's run report cannot be read from another's.
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
                 "SHARED_KG_OPT_OUT", "VCT_PROJECT_ID"):
        os.environ.pop(gone, None)

    mod_name = f"_sync_kg_repair_{uuid.uuid4().hex}"
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


# ─────────────────────────────────────────────────────────────────────────
# PURE layer — the decision, with no backend of any kind in reach.
# ─────────────────────────────────────────────────────────────────────────


class MetadataRepairDecisionTests(unittest.TestCase):
    """Every row of the classifier's table, including every "do nothing"."""

    DESIRED = {
        "title": "PAY payment orchestration",
        "node_type": "concept",
        "tags": ["Acme", "Acme-PAY", "payments"],
        "external_links": "",
    }

    def _plan(self, *rows):
        return repair.plan_metadata_repair(list(rows), self.DESIRED)

    def _stored(self, **over):
        base = dict(self.DESIRED)
        base.update(over)
        return base

    def test_stale_dialect_row_is_a_stamp(self):
        plan = self._plan(("u1", dict(STALE_STORED)))
        self.assertIs(plan.verdict, RowAction.STAMP)
        self.assertEqual(plan.row_count, 1)
        self.assertEqual(
            plan.patches[0][1],
            {
                "title": self.DESIRED["title"],
                "node_type": "concept",
                "tags": self.DESIRED["tags"],
            },
            "only the properties that DIFFER belong in the payload",
        )

    def test_matching_row_writes_nothing(self):
        plan = self._plan(("u1", self._stored()))
        self.assertIs(plan.verdict, RowAction.SKIP)
        self.assertEqual(plan.patches, [])

    def test_tag_order_alone_is_not_a_difference(self):
        plan = self._plan(("u1", self._stored(tags=["payments", "Acme-PAY", "Acme"])))
        self.assertIs(
            plan.verdict, RowAction.SKIP,
            "tags are assembled with list(set(...)) when link inference "
            "contributes, so an order-sensitive comparison would rewrite "
            "those rows on every sync forever",
        )

    def test_absent_property_is_inconclusive_not_a_difference(self):
        props = self._stored()
        props.pop("node_type")
        plan = self._plan(("u1", props))
        self.assertIs(plan.verdict, RowAction.SKIP)
        self.assertIn("not returned", plan.reason)

    def test_unexpected_stored_type_is_inconclusive(self):
        plan = self._plan(("u1", self._stored(tags="Acme,payments")))
        self.assertIs(plan.verdict, RowAction.SKIP)
        self.assertIn("unexpected type", plan.reason)

    def test_unset_stored_value_is_judgeable(self):
        """``None`` is Weaviate's "unset" — a positive fact, not an absence."""
        plan = self._plan(("u1", self._stored(tags=None)))
        self.assertIs(plan.verdict, RowAction.STAMP)
        self.assertEqual(plan.patches[0][1], {"tags": self.DESIRED["tags"]})

    def test_row_without_an_id_stops_the_whole_node(self):
        plan = self._plan((None, dict(STALE_STORED)))
        self.assertIs(plan.verdict, RowAction.SKIP)

    def test_unusable_desired_value_is_never_written(self):
        for bad in ({"title": ""}, {"node_type": "   "}, {"title": 7},
                    {"tags": "a,b"}, {"external_links": None}):
            desired = dict(self.DESIRED)
            desired.update(bad)
            names = repair.comparable_properties(desired)
            self.assertNotIn(
                next(iter(bad)), names,
                f"an unusable desired {next(iter(bad))!r} must be dropped "
                f"from the comparison, never written over stored data",
            )

    def test_one_unjudgeable_chunk_protects_every_other_chunk(self):
        """All-or-nothing: a node's properties live on EVERY chunk row."""
        good = ("u1", dict(STALE_STORED))
        blind = ("u2", {"content_hash": "x"})  # the fetch returned no metadata
        plan = repair.plan_metadata_repair([good, blind], self.DESIRED)
        self.assertIs(
            plan.verdict, RowAction.SKIP,
            "half a node repaired is a node in two states; judgeability is "
            "established for every row before any row is written",
        )

    def test_embed_is_never_the_verdict(self):
        """The classifier may not force work on text it proved unchanged."""
        names = repair.comparable_properties(self.DESIRED)
        for props in (dict(STALE_STORED), self._stored(), {}, {"tags": None}):
            action, _ = repair.classify_metadata_row(props, self.DESIRED, names)
            self.assertIn(action, (RowAction.SKIP, RowAction.STAMP))
            self.assertIsNot(action, RowAction.EMBED)

    def test_patch_failure_aborts_the_rest_and_is_returned(self):
        plan = repair.plan_metadata_repair(
            [("u1", dict(STALE_STORED)), ("u2", dict(STALE_STORED))],
            self.DESIRED,
        )
        seen = []

        def _patch(row_uuid, payload):
            seen.append(row_uuid)
            raise RuntimeError("boom")

        done, err = repair.apply_metadata_repair(plan, _patch)
        self.assertEqual(done, 0)
        self.assertEqual(seen, ["u1"], "the remaining patches are abandoned")
        self.assertIn("boom", err or "")


# ─────────────────────────────────────────────────────────────────────────
# BEHAVIOURAL layer — sync_node end to end against the fake.
# ─────────────────────────────────────────────────────────────────────────


class _RepairTestBase(unittest.TestCase):
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

    def _node_path(self, text: str = NESTED_NODE) -> Path:
        path = self.root / "knowledge" / "concepts" / "pay-payment-orchestration.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _seed_rows(self, mod, server, path: Path, stored_props: dict,
                   *, chunks: int = 1):
        """Insert the rows a PRE-v0.2.95 sync would have left behind.

        Same text (so the content hash matches), populated active vector
        slot, and the metadata the old parse produced.
        """
        text = path.read_text(encoding="utf-8")
        content_hash = mod._content_signature_excluding_updated(text)
        store = server.client.collections._store_for(PROJECT_KG)
        uuids = []
        for i in range(chunks):
            uid = f"row-{i + 1}"
            props = {
                "file_path": mod._canonical_file_path(path),
                "content_hash": content_hash,
                "chunk_num": i + 1,
                "total_chunks": chunks,
                "content": text,
                "links": [],
            }
            props.update(stored_props)
            store[uid] = _FakeObj(uid, props, {ACTIVE_SLOT: [0.1, 0.2, 0.3]})
            uuids.append(uid)
        return uuids

    def _sync(self, mod, server, path: Path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = mod.sync_node(server, path)
        return outcome, buf.getvalue()

    def _stored_props(self, server, uid: str) -> dict:
        return server.client.collections._store_for(PROJECT_KG)[uid].properties


class RepairOnSkipTests(_RepairTestBase):
    def test_stale_nested_dialect_row_is_repaired_with_zero_embeds(self):
        """THE red-proof: stored metadata healed, and not one embed paid."""
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        path = self._node_path()
        self._seed_rows(mod, server, path, dict(STALE_STORED))

        outcome, out = self._sync(mod, server, path)

        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED)
        self.assertEqual(
            server.embed_calls, 0,
            "the text is unchanged and the vector is valid — a repair that "
            "costs an embed is the wrong repair",
        )
        self.assertEqual(server.opts.inserts, 0)
        self.assertEqual(server.opts.deletes, 0)
        props = self._stored_props(server, "row-1")
        self.assertEqual(props["node_type"], "concept")
        self.assertEqual(
            sorted(props["tags"]), sorted(["Acme", "Acme-PAY", "payments"])
        )
        self.assertEqual(props["title"], "pay-payment-orchestration")
        self.assertEqual(
            props["content_hash"],
            mod._content_signature_excluding_updated(
                path.read_text(encoding="utf-8")
            ),
            "the content hash has five other readers — a repair must not "
            "perturb it",
        )
        self.assertIn("Metadata repair", out)

    def test_already_correct_row_is_not_written_at_all(self):
        """The negative: no difference, no write of any kind."""
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        path = self._node_path()
        self._seed_rows(mod, server, path, {
            "title": "pay-payment-orchestration",
            "node_type": "concept",
            "tags": ["Acme", "Acme-PAY", "payments"],
            "external_links": "",
        })

        outcome, out = self._sync(mod, server, path)

        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED)
        self.assertEqual(server.embed_calls, 0)
        self.assertEqual(
            server.opts.updates, [],
            "a converged node must cost zero writes; every patch tombstones "
            "the row's HNSW node, so a spurious one is not free",
        )
        self.assertEqual(server.opts.inserts, 0)
        self.assertEqual(server.opts.deletes, 0)
        self.assertNotIn("Metadata repair", out)

    def test_multi_chunk_node_repairs_every_chunk(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        path = self._node_path()
        uuids = self._seed_rows(mod, server, path, dict(STALE_STORED), chunks=3)

        outcome, _ = self._sync(mod, server, path)

        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED)
        self.assertEqual(server.embed_calls, 0)
        for uid in uuids:
            self.assertEqual(self._stored_props(server, uid)["node_type"], "concept")
        self.assertEqual(len(server.opts.updates), 3)

    def test_failed_patch_is_counted_and_named_never_swallowed(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer(fail_update_for=("row-2",))
        path = self._node_path()
        self._seed_rows(mod, server, path, dict(STALE_STORED), chunks=3)

        outcome, out = self._sync(mod, server, path)

        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED)
        self.assertEqual(
            server.embed_calls, 0,
            "a failed repair must not escalate into a re-embed of unchanged "
            "text (the skip block's except falls through to exactly that)",
        )
        self.assertEqual(server.opts.inserts, 0)
        self.assertEqual(server.opts.deletes, 0)
        self.assertIn("INCOMPLETE", out)
        self.assertIn("metadata repair incomplete", outcome.reason)
        self.assertEqual(mod._METADATA_REPAIR_FAILED_COUNT, 1)
        self.assertEqual(
            self._stored_props(server, "row-1")["node_type"], "concept",
            "a row already patched holds the CORRECT value; reverting it "
            "would write the wrong one back",
        )
        self.assertEqual(
            self._stored_props(server, "row-3")["node_type"], "concepts",
            "the patches after the failure are abandoned, not attempted",
        )

    def test_repair_counts_reach_the_run_report(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        path = self._node_path()
        self._seed_rows(mod, server, path, dict(STALE_STORED))
        self._sync(mod, server, path)
        self.assertEqual(mod._METADATA_REPAIRED_COUNT, 1)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod._print_metadata_repair_report()
        self.assertIn("Repaired stored metadata on 1", buf.getvalue())
        self.assertIn("zero re-embeds", buf.getvalue())

    def test_report_is_silent_when_nothing_was_repaired(self):
        mod = _load_sync_module(self.root)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod._print_metadata_repair_report()
        self.assertEqual(buf.getvalue(), "")


class ConservativeFetchArmTests(_RepairTestBase):
    """A fetch that could not carry the properties repairs NOTHING."""

    def test_legacy_schema_rejecting_the_props_keeps_prior_behaviour(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer(reject_metadata_props=True)
        path = self._node_path()
        self._seed_rows(mod, server, path, dict(STALE_STORED))

        outcome, _ = self._sync(mod, server, path)

        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED)
        self.assertEqual(server.embed_calls, 0)
        self.assertEqual(
            server.opts.updates, [],
            "properties the fetch never returned are ABSENT, not empty; "
            "reading absence as a difference would rewrite every row of a "
            "knowledge graph",
        )
        self.assertEqual(
            self._stored_props(server, "row-1")["node_type"], "concepts",
            "the row is left exactly as found — the pre-existing behaviour",
        )

    def test_old_client_hash_only_arm_still_skips_and_repairs_nothing(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer(reject_include_vector=True,
                                 reject_metadata_props=True)
        path = self._node_path()
        self._seed_rows(mod, server, path, dict(STALE_STORED))

        outcome, out = self._sync(mod, server, path)

        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED)
        self.assertEqual(server.embed_calls, 0)
        self.assertEqual(server.opts.updates, [])
        self.assertIn("falling back to hash-only check", out)

    def test_the_metadata_arm_is_the_first_ask(self):
        """The repairable properties ride the EXISTING fetch.

        Asserts what it says and no more: the FIRST ask already names them,
        so the healthy path pays no additional roundtrip for the repair.
        """
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        path = self._node_path()
        self._seed_rows(mod, server, path, dict(STALE_STORED))
        self._sync(mod, server, path)
        first = server.opts.fetch_props_seen[0]
        for name in repair.REPAIRABLE_PROPERTIES:
            self.assertIn(name, first)


class ChangedTextStillReEmbedsTests(_RepairTestBase):
    """The repair must not become a way to AVOID a needed re-embed."""

    def test_content_change_takes_the_ordinary_write_path(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        path = self._node_path()
        self._seed_rows(mod, server, path, dict(STALE_STORED))
        path.write_text(NESTED_NODE + "\nA genuinely new paragraph.\n",
                        encoding="utf-8")

        outcome, _ = self._sync(mod, server, path)

        self.assertEqual(outcome.status, mod.OUTCOME_SYNCED)
        self.assertGreater(server.embed_calls, 0)
        self.assertEqual(server.opts.deletes, 1)
        self.assertEqual(server.opts.inserts, 1)


class SyncDocHasNoSuchGapTests(unittest.TestCase):
    """Phase-0 finding, pinned: ``sync_doc`` cannot carry this defect.

    ``parse_doc_file`` never calls ``parse_frontmatter``, so
    ``_normalise_frontmatter`` cannot alter anything it produces, and the
    dev collection is written without ``tags`` / ``node_type`` at all. The
    only metadata it stores is a title derived from the body's first
    heading (falling back to the filename stem) — a pure function of the
    text whose change necessarily changes the content hash, which the
    existing skip gate already catches.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_snapshot = {k: os.environ.get(k) for k in _ENV_KEYS}
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_env)
        self.mod = _load_sync_module(self.root)

    def _restore_env(self):
        for k, v in self._env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_doc_parse_ignores_frontmatter_dialects_entirely(self):
        path = self.root / "docs" / "guide.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (
            "---\nname: not-the-title\nmetadata:\n  type: concept\n"
            "  tags: [a, b]\n---\n\n# The Real Title\n\nbody\n"
        )
        path.write_text(text, encoding="utf-8")
        doc = self.mod.parse_doc_file(text, path)
        self.assertEqual(doc["tags"], [])
        self.assertEqual(doc["node_type"], "doc")
        self.assertNotEqual(
            doc["title"], "not-the-title",
            "a docs title comes from the body heading, never from a "
            "frontmatter dialect — which is why no dialect promotion can "
            "leave a docs row stale",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
