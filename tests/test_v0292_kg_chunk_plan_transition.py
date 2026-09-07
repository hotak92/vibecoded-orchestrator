# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — KG chunk-plan transition repair (revision-crossing re-chunk).

The defect these tests pin: ``sync_node``/``sync_doc``'s embed-skip gate
treats a SELF-CONSISTENT row set (stored ``total_chunks`` == number of
stored rows) as current. When ``_CHUNKER_REVISION`` crosses (this cycle:
the qwen3 chunk budget clamped 13 500 → 8 192 counter-units), a boundary
change is invisible to that check — every entry is skipped forever with
stale boundaries, and the ``chunker_preset_overhaul_pending`` deferral's
printed remedy (``kg-sync --all``) re-chunks nothing.

The fix: while the project's deferral ledger carries
``chunker_preset_overhaul_pending`` (the revision gate's crossing signal),
the skip additionally requires the stored plan to match what the CURRENT
chunker would produce for the content — count AND, when multi-chunk,
byte-identical boundaries. Everything else keeps the exact pre-fix
semantics (an install with no crossing pending pays no comparison).

In-memory fake Weaviate client — no live state. ``VCT_STATE_DIR`` and
``WEAVIATE_URL`` are pinned to disposable values (module loader below).
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
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
sys.path.insert(0, str(REPO_ROOT))

PROJECT_KG = "V0292T_KnowledgeGraph"
SHARED_KG = "V0292T_Shared_KnowledgeGraph"
DEV_COLL = "V0292T_Development"
RESYNC_CID = "chunker_preset_overhaul_pending"

# The pre-v0.2.92 qwen3 (xlarge) preset, unclamped — what existing rows on a
# crossing install were written under (see CHUNKING_PRESETS history in
# weaviate_mcp/chunking.py).
OLD_PRESET = (4600, 13500, 9500)


# ─── In-memory fake Weaviate client (per-collection stores) ──────────────
# Same fake family as tests/test_v0289_kg_sync_scope_routing.py.


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
    def __init__(self, store: dict):
        self._store = store

    def fetch_objects(self, filters=None, limit=100, return_properties=None,
                      include_vector=False):
        objs = list(self._store.values())
        if filters is not None:
            objs = [o for o in objs if filters.matches(o.properties)]
        return _FakeQueryResult(objs[:limit])


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


class _FakeCollection:
    def __init__(self, store: dict):
        self._store = store
        self.query = _FakeQuery(store)
        self.data = _FakeData(store)


class _FakeCollections:
    def __init__(self):
        self._stores: dict[str, dict] = {}

    def _store_for(self, name: str) -> dict:
        return self._stores.setdefault(name, {})

    def get(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._store_for(name))

    def exists(self, name: str) -> bool:  # noqa: ARG002
        return True

    def create(self, **kwargs):  # noqa: ARG002
        pass


class _FakeClient:
    def __init__(self):
        self.collections = _FakeCollections()


class _FakeEmbeddingService:
    text_model_id = "qwen3-embedding:0.6b"


class _CountingServer:
    """Fake server whose embed calls are COUNTED (fast-path assertions)."""

    def __init__(self, slot: str = "qwen3_embed"):
        self.client = _FakeClient()
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
        # W3: the tagged capture the sync write path now persists.
        self.embed_calls += 1
        return {self.text_vector_slot: [0.9, 0.9, 0.9]}, []


_ENV_KEYS = (
    "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
    "VCT_DISABLE_HUB_RESOLVER", "SHARED_KG_WRITE_DISABLED",
    "SHARED_KG_OPT_OUT", "VCT_PROJECT_ID", "KG_SYNC_PROJECT_ROOT",
    "VCT_STATE_DIR", "WEAVIATE_URL", "VCT_ORCHESTRATOR_ROOT",
)


def _load_sync_module(project_root: Path, *, dev: str = DEV_COLL, argv=None):
    """Load a FRESH sync module instance bound to ``project_root``.

    ``VCT_STATE_DIR`` / ``WEAVIATE_URL`` are pinned to disposable values so
    no run touches live state (the query logger + details-log sink under
    ``$VCT_STATE_DIR``).

    ``argv``: when given, the ``sys.argv`` list the module sees at import.
    ``--rechunk`` is extracted at import time (the production path
    ``RechunkFlagTests`` drives), so it can only be set this way; the list
    is passed as the SAME object the extraction mutates, letting callers
    assert the token was consumed. ``None`` leaves pytest's own argv.
    """
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = PROJECT_KG
    os.environ["SHARED_KG_COLLECTION"] = SHARED_KG
    os.environ["DEVELOPMENT_COLLECTION"] = dev
    os.environ["DUAL_EMBEDDING_ENABLED"] = "false"
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    os.environ["VCT_STATE_DIR"] = str(project_root / ".vct-state-disposable")
    os.environ["WEAVIATE_URL"] = "http://127.0.0.1:1"
    # Pin the orchestrator root to THIS repo. Unset, the script resolves it
    # from the machine (on a maintainer box: the dogfood checkout) and inserts
    # THAT tree on sys.path at import — after which `weaviate_mcp.*` is bound
    # to a different repo's chunking presets for the rest of the process, both
    # here and in every test module that imports it later. Measured 2026-09-05:
    # a 9 354-unit node planned 2 chunks against this tree's 8 192 budget and
    # 1 chunk against the other tree's 13 500.
    os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    os.environ.pop("KG_SYNC_PROJECT_ROOT", None)
    os.environ.pop("SHARED_KG_WRITE_DISABLED", None)
    os.environ.pop("SHARED_KG_OPT_OUT", None)
    os.environ.pop("VCT_PROJECT_ID", None)

    mod_name = f"_sync_kg_plan_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        if argv is None:
            spec.loader.exec_module(mod)
        else:
            with mock.patch.object(sys, "argv", argv):
                spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest(
            f"sync_knowledge_graph.py has runtime deps not installed ({exc})"
        )
    mod.Filter = _FakeFilter
    return mod


def _sentences(n: int) -> str:
    return " ".join(f"Sentence number {i} ends here." for i in range(n))


# ~7.8 counter-units per sentence (deterministic): 1200 sentences ≈ 9.4k
# units (old preset: single chunk ≤ 13 500; current: 2 chunks > 8 192) and
# 2100 sentences ≈ 16.5k units (old 2 chunks [~10 578, ~5 943]; current
# 2 chunks [~9 082, ~7 439] — SAME COUNT, DIFFERENT BOUNDARIES, the exact
# shape a count-only comparison would miss).
BODY_SINGLE_TO_MULTI = _sentences(1200)
BODY_SAME_COUNT_DIFF_BOUND = _sentences(2100)
BODY_SMALL = _sentences(200)


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


def _seed_pending_ledger(folder: Path) -> None:
    """Write a real ``chunker_preset_overhaul_pending`` ledger entry — the
    same condition_id the revision gate emits on a crossing."""
    from vco_lib.deferral_emit import emit
    from vco_lib.deferral_report import DeferralEntry

    emit(folder, DeferralEntry(
        condition_id=RESYNC_CID,
        title="KG + codegraph re-sync recommended (chunker revision changed)",
        detected="test fixture: a chunker revision crossing is pending repair",
        why_deferred="test fixture",
        command_to_apply=".claude/scripts/kg-sync --all",
        severity="info",
    ))


def _seed_rows(server: _CountingServer, collection: str, file_path: str,
               content_hash: str, chunk_contents, *, total_chunks: int,
               vectors=False) -> None:
    """Pre-seed stored rows for one file_path into a named collection."""
    store = server.client.collections._store_for(collection)
    for i, content in enumerate(chunk_contents, start=1):
        uid = str(uuid.uuid4())
        vec = {server.text_vector_slot: [0.1, 0.1, 0.1]} if vectors else None
        store[uid] = _FakeObj(uid, {
            "file_path": file_path,
            "content_hash": content_hash,
            "chunk_num": i,
            "total_chunks": total_chunks,
            "content": content,
        }, vec)


def _rows_for(server: _CountingServer, collection: str, file_path: str):
    store = server.client.collections._store_for(collection)
    rows = [o.properties for o in store.values()
            if o.properties.get("file_path") == file_path]
    rows.sort(key=lambda p: p.get("chunk_num") or 0)
    return rows


class _PlanTransitionTestBase(unittest.TestCase):
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

    def _plans(self, mod, server, text: str):
        """(old-preset plan contents, current-preset plan contents).

        W8 (v0.2.92): the CURRENT plan comes from the production planner
        (``_plan_for`` → the shared ``kg_chunk_plan.plan_node_chunks`` that
        both KG writers and the comparison call), not from a re-derived
        chunker — so these expectations track what the write path will
        actually store. A single-chunk plan stores the content VERBATIM,
        which is why it renders as ``[text]`` here rather than as the
        chunker's stripped single chunk.
        """
        # The OLD-preset fixture is built with the SAME Chunker class the
        # production plan binds (`kg_chunk_plan`'s sibling-first import), so
        # "old plan" and "current plan" are comparable by construction.
        from weaviate_mcp.kg_chunk_plan import Chunker as _PlanChunker
        old_chunks = _PlanChunker(*OLD_PRESET).chunk_text(
            text=text, source_id="t", metadata={})
        plan = mod._plan_for(server, text, source_id="t")
        new_contents = (
            [text] if plan.is_single else [c.content for c in plan.chunks]
        )
        return [c.content for c in old_chunks], new_contents


class NodePlanTransitionTests(_PlanTransitionTestBase):
    def test_pending_crossing_rechunks_entries_whose_plan_changed(self):
        """RED CORE: revision crossing pending + stored rows self-consistent
        under the OLD plan + content whose CURRENT plan differs → the entry
        must re-chunk, not skip (pre-fix it skipped with stale boundaries)."""
        node = self.root / "knowledge" / "concepts" / "big.md"
        text = _write_node(node, "Big Node", BODY_SINGLE_TO_MULTI)
        _seed_pending_ledger(self.root)
        mod = _load_sync_module(self.root)
        server = _CountingServer()

        old_plan, new_plan = self._plans(mod, server, text)
        # Preconditions: the transition is real for this content.
        self.assertEqual(len(old_plan), 1)
        self.assertEqual(len(new_plan), 2)

        fp = "knowledge/concepts/big.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, PROJECT_KG, fp, content_hash,
                   [text], total_chunks=1)

        outcome, out = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_SYNCED,
                         f"stale-plan entry must re-chunk, got skip:\n{out}")
        self.assertGreaterEqual(server.embed_calls, 1)
        self.assertEqual(mod._RECHUNKED_COUNT, 1)
        rows = _rows_for(server, PROJECT_KG, fp)
        self.assertEqual([r["content"] for r in rows], new_plan)
        self.assertEqual([r["chunk_num"] for r in rows], [1, 2])
        self.assertTrue(all(r["total_chunks"] == 2 for r in rows))
        self.assertIn("Re-chunking", out)

    def test_pending_crossing_same_count_different_boundaries_rechunks(self):
        """A budget change can move boundaries WITHOUT changing the count
        (measured on a real 20 763-unit node: 3→3 chunks). The comparison
        must inspect boundaries, not just the row count."""
        node = self.root / "knowledge" / "concepts" / "bounds.md"
        text = _write_node(node, "Bounds Node", BODY_SAME_COUNT_DIFF_BOUND)
        _seed_pending_ledger(self.root)
        mod = _load_sync_module(self.root)
        server = _CountingServer()

        old_plan, new_plan = self._plans(mod, server, text)
        self.assertEqual(len(old_plan), len(new_plan))          # same count
        self.assertNotEqual(old_plan, new_plan)                 # diff bounds

        fp = "knowledge/concepts/bounds.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, PROJECT_KG, fp, content_hash,
                   old_plan, total_chunks=len(old_plan))

        outcome, out = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_SYNCED,
                         f"same-count/diff-boundary entry must re-chunk:\n{out}")
        rows = _rows_for(server, PROJECT_KG, fp)
        self.assertEqual([r["content"] for r in rows], new_plan)

    def test_pending_crossing_unchanged_plan_still_skips(self):
        """The overwhelming majority: short nodes plan identically before
        and after the revision — they must STILL skip (no re-embed waste)."""
        node = self.root / "knowledge" / "concepts" / "small.md"
        text = _write_node(node, "Small Node", BODY_SMALL)
        _seed_pending_ledger(self.root)
        mod = _load_sync_module(self.root)
        server = _CountingServer()

        old_plan, new_plan = self._plans(mod, server, text)
        self.assertEqual((len(old_plan), len(new_plan)), (1, 1))

        fp = "knowledge/concepts/small.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, PROJECT_KG, fp, content_hash, [text],
                   total_chunks=1)

        outcome, out = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED,
                         f"unchanged plan must skip:\n{out}")
        self.assertEqual(server.embed_calls, 0)
        self.assertEqual(mod._RECHUNKED_COUNT, 0)

    def test_no_crossing_pending_preserves_legacy_skip_semantics(self):
        """Scoping guard: with NO revision crossing pending (no ledger
        entry — the state of an install already at the current revision),
        the gate behaves exactly as before: self-consistent rows skip, and
        no plan comparison is paid (a stale-shaped row set is untouched)."""
        node = self.root / "knowledge" / "concepts" / "big.md"
        text = _write_node(node, "Big Node", BODY_SINGLE_TO_MULTI)
        # NOTE: no ledger entry — nothing pending.
        mod = _load_sync_module(self.root)
        server = _CountingServer()

        fp = "knowledge/concepts/big.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, PROJECT_KG, fp, content_hash, [text],
                   total_chunks=1)

        outcome, out = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED,
                         f"no crossing pending → pre-v0.2.92 semantics:\n{out}")
        self.assertEqual(server.embed_calls, 0)
        self.assertEqual(mod._RECHUNKED_COUNT, 0)

    def test_second_run_after_repair_is_a_noop(self):
        """Idempotent + runs-once-per-entry: after the repair rewrote the
        rows under the current plan, a second run (crossing STILL pending)
        skips every entry — the durable record is the rows themselves."""
        node = self.root / "knowledge" / "concepts" / "big.md"
        text = _write_node(node, "Big Node", BODY_SINGLE_TO_MULTI)
        _seed_pending_ledger(self.root)
        mod = _load_sync_module(self.root)
        server = _CountingServer()

        fp = "knowledge/concepts/big.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, PROJECT_KG, fp, content_hash, [text],
                   total_chunks=1)

        first, _ = self._sync_node(mod, server, node)
        self.assertEqual(first.status, mod.OUTCOME_SYNCED)
        embeds_after_repair = server.embed_calls
        self.assertEqual(mod._RECHUNKED_COUNT, 1)

        second, out = self._sync_node(mod, server, node)
        self.assertEqual(second.status, mod.OUTCOME_EMBED_SKIPPED,
                         f"second run must be a no-op:\n{out}")
        self.assertEqual(server.embed_calls, embeds_after_repair)
        self.assertEqual(mod._RECHUNKED_COUNT, 1)

    def test_unevaluable_stored_rows_do_not_skip(self):
        """"Could not determine" is NOT "current": rows whose chunk_num is
        missing cannot be ordered or compared — the entry re-chunks rather
        than being skipped (never report success over what was skipped)."""
        node = self.root / "knowledge" / "concepts" / "bounds.md"
        text = _write_node(node, "Bounds Node", BODY_SAME_COUNT_DIFF_BOUND)
        _seed_pending_ledger(self.root)
        mod = _load_sync_module(self.root)
        server = _CountingServer()

        _, new_plan = self._plans(mod, server, text)
        self.assertEqual(len(new_plan), 2)

        fp = "knowledge/concepts/bounds.md"
        content_hash = mod._content_signature_excluding_updated(text)
        # Two rows, self-consistent counts, matching hash — but one row has
        # NO chunk_num: not judgeable.
        store = server.client.collections._store_for(PROJECT_KG)
        for i, content in enumerate(new_plan):
            uid = str(uuid.uuid4())
            props = {
                "file_path": fp,
                "content_hash": content_hash,
                "total_chunks": 2,
                "content": content,
            }
            if i == 0:
                props["chunk_num"] = 1  # second row: chunk_num absent
            store[uid] = _FakeObj(uid, props)

        outcome, out = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_SYNCED,
                         f"unevaluable rows must fall through to re-chunk:\n{out}")
        self.assertGreaterEqual(server.embed_calls, 1)

    def test_partial_write_from_interrupted_run_does_not_skip(self):
        """A backend death mid-chunk-write leaves fewer rows than
        total_chunks claims — self-consistency fails → re-embed (the
        interrupted run repairs itself; it never records itself done)."""
        node = self.root / "knowledge" / "concepts" / "bounds.md"
        text = _write_node(node, "Bounds Node", BODY_SAME_COUNT_DIFF_BOUND)
        _seed_pending_ledger(self.root)
        mod = _load_sync_module(self.root)
        server = _CountingServer()

        _, new_plan = self._plans(mod, server, text)
        fp = "knowledge/concepts/bounds.md"
        content_hash = mod._content_signature_excluding_updated(text)
        # Only chunk 1 of 2 landed before the "crash".
        _seed_rows(server, PROJECT_KG, fp, content_hash,
                   [new_plan[0]], total_chunks=2)

        outcome, out = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_SYNCED,
                         f"partial write must re-embed:\n{out}")
        rows = _rows_for(server, PROJECT_KG, fp)
        self.assertEqual(len(rows), 2)

    def test_repair_does_not_clear_the_deferral_entry(self):
        """The repair rewrites rows but never resolves the ledger entry:
        the entry also covers the code-graph half of the remedy and keeps
        its existing lifecycle (next update reconcile)."""
        from vco_lib.deferral_report import DeferralReport

        node = self.root / "knowledge" / "concepts" / "big.md"
        text = _write_node(node, "Big Node", BODY_SINGLE_TO_MULTI)
        _seed_pending_ledger(self.root)
        mod = _load_sync_module(self.root)
        server = _CountingServer()

        fp = "knowledge/concepts/big.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, PROJECT_KG, fp, content_hash, [text],
                   total_chunks=1)

        outcome, _ = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_SYNCED)
        self.assertTrue(
            DeferralReport.read(self.root).has_condition(RESYNC_CID),
            "the sync must not resolve chunker_preset_overhaul_pending "
            "(the code-graph half of the remedy is still owed)",
        )


class PendingProbeTests(_PlanTransitionTestBase):
    def test_no_ledger_means_not_pending(self):
        mod = _load_sync_module(self.root)
        self.assertFalse(mod._chunker_resync_pending())

    def test_ledger_entry_means_pending(self):
        _seed_pending_ledger(self.root)
        mod = _load_sync_module(self.root)
        self.assertTrue(mod._chunker_resync_pending())

    def test_unreadable_ledger_is_conservatively_pending(self):
        """Cannot determine whether a repair is owed → run the comparison
        (pure CPU; it can only repair), never skip it."""
        from unittest.mock import patch

        mod = _load_sync_module(self.root)
        mod._resync_pending_cache = None  # re-probe
        with patch(
            "vco_lib.deferral_report.DeferralReport.read",
            side_effect=RuntimeError("ledger unreadable"),
        ):
            self.assertTrue(mod._chunker_resync_pending())


class DocPlanTransitionTests(_PlanTransitionTestBase):
    def test_pending_crossing_rechunks_docs_entry(self):
        """The docs/ embed-skip gate has the same self-consistency-only
        defect — the same repair applies while a crossing is pending."""
        doc = self.root / "docs" / "guide.md"
        text = _write_doc(doc, BODY_SINGLE_TO_MULTI)
        _seed_pending_ledger(self.root)
        mod = _load_sync_module(self.root)
        server = _CountingServer()

        old_plan, new_plan = self._plans(mod, server, text)
        self.assertEqual((len(old_plan), len(new_plan)), (1, 2))

        fp = "docs/guide.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, DEV_COLL, fp, content_hash, [text],
                   total_chunks=1, vectors=True)

        outcome, out = self._sync_doc(mod, server, doc)
        self.assertEqual(outcome.status, mod.OUTCOME_SYNCED,
                         f"stale-plan doc must re-chunk:\n{out}")
        self.assertGreaterEqual(server.embed_calls, 1)
        rows = _rows_for(server, DEV_COLL, fp)
        self.assertEqual([r["content"] for r in rows], new_plan)

    def test_no_crossing_pending_docs_entry_still_skips(self):
        doc = self.root / "docs" / "guide.md"
        text = _write_doc(doc, BODY_SINGLE_TO_MULTI)
        # No ledger entry — nothing pending.
        mod = _load_sync_module(self.root)
        server = _CountingServer()

        fp = "docs/guide.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, DEV_COLL, fp, content_hash, [text],
                   total_chunks=1, vectors=True)

        outcome, out = self._sync_doc(mod, server, doc)
        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED,
                         f"no crossing pending → legacy docs skip:\n{out}")
        self.assertEqual(server.embed_calls, 0)


class DeferralRemedyTextTests(unittest.TestCase):
    """Item 3: the live deferral emitter's printed remedy must state what
    ``kg-sync --all`` really does now (re-chunk only changed-plan entries),
    not claim it re-embeds every chunk."""

    def test_live_emitter_text_states_the_real_remediation(self):
        from vco_lib.project_init import _emit_chunker_revision_resync_deferral

        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            _emit_chunker_revision_resync_deferral(
                folder, "v0.2.88", "v0.2.92"
            )
            content = (
                folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
            ).read_text(encoding="utf-8")
            self.assertIn(".claude/scripts/kg-sync --all", content)
            # v0.2.92 (BLOCKER-2): --from-resolver, and the folder as the
            # analyzer's positional repo_path (absolute wrapper path, no `cd`).
            self.assertIn(
                ".claude/scripts/code-graph-analyze", content,
            )
            self.assertIn("--from-resolver --force-recreate", content)
            self.assertNotIn(
                "code-graph-analyze . --force-recreate", content,
            )
            # The now-false blanket claim about the KG half is gone…
            self.assertNotIn("re-embeds every chunk", content)
            # …replaced by what the command actually does.
            self.assertIn("hash-", content)


# ── --rechunk: the by-hand remedy is TRUE for every population ──────────

class RechunkFlagTests(_PlanTransitionTestBase):
    """MAJOR-R5-4 part 1 — ``--rechunk`` arms the plan comparison without
    a ledger entry.

    The comparison that re-chunks stale-boundary entries is armed by
    ``_chunker_resync_pending``, which reads the deferral ledger — and a
    re-cloned / manifest-deleted project is classified FRESH by the
    revision gate, never receives ``chunker_preset_overhaul_pending``, so
    ``kg-sync --all`` alone hash-skips everything: a printed remedy that
    cannot work. ``--rechunk`` arms the comparison for THIS run instead.

    Every test loads the sync module with a controlled ``sys.argv`` (the
    production import-time extraction) and drives the production
    ``sync_node``; NO ledger entry is ever seeded — that is the point.
    """

    def _load(self, extra_argv):
        argv = ["sync_knowledge_graph.py"] + extra_argv
        mod = _load_sync_module(self.root, argv=argv)
        return mod, argv

    def test_rechunk_arms_plan_comparison_without_ledger_entry(self):
        """ACT: stored rows self-consistent under the OLD plan + content
        whose CURRENT plan differs + NO ledger entry → with ``--rechunk``
        the entry re-chunks (without the flag this exact fixture skips,
        which was the whole defect)."""
        node = self.root / "knowledge" / "concepts" / "big.md"
        text = _write_node(node, "Big Node", BODY_SINGLE_TO_MULTI)
        mod, argv = self._load(["--all", "--rechunk"])
        self.assertEqual(
            argv, ["sync_knowledge_graph.py", "--all"],
            "the token must be consumed at import so main()'s dispatch "
            "and flag validation never see it",
        )
        self.assertTrue(mod._RECHUNK_FORCED)
        server = _CountingServer()

        old_plan, new_plan = self._plans(mod, server, text)
        self.assertEqual(len(old_plan), 1)
        self.assertEqual(len(new_plan), 2)

        fp = "knowledge/concepts/big.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, PROJECT_KG, fp, content_hash, [text],
                   total_chunks=1)

        outcome, out = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_SYNCED,
                         f"--rechunk must arm the comparison and re-chunk:\n{out}")
        self.assertGreaterEqual(server.embed_calls, 1)
        self.assertEqual(mod._RECHUNKED_COUNT, 1)
        rows = _rows_for(server, PROJECT_KG, fp)
        self.assertEqual([r["content"] for r in rows], new_plan)
        self.assertEqual([r["chunk_num"] for r in rows], [1, 2])

    def test_no_flag_no_ledger_keeps_legacy_skip(self):
        """The without-it twin: same fixture, argv WITHOUT ``--rechunk``
        → nothing arms the comparison → the pre-v0.2.92 skip semantics
        hold (this is the state a re-cloned project was stuck in)."""
        node = self.root / "knowledge" / "concepts" / "big.md"
        text = _write_node(node, "Big Node", BODY_SINGLE_TO_MULTI)
        mod, argv = self._load(["--all"])
        self.assertFalse(mod._RECHUNK_FORCED)
        server = _CountingServer()

        fp = "knowledge/concepts/big.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, PROJECT_KG, fp, content_hash, [text],
                   total_chunks=1)

        outcome, out = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED,
                         f"no flag + no ledger entry → nothing arms the "
                         f"comparison:\n{out}")
        self.assertEqual(server.embed_calls, 0)
        self.assertEqual(mod._RECHUNKED_COUNT, 0)

    def test_rechunk_with_unchanged_plan_still_skips(self):
        """``--rechunk`` forces the COMPARISON, not a blind re-embed: an
        entry whose stored plan already matches the current chunker keeps
        skipping (the overwhelming majority on a forced run)."""
        node = self.root / "knowledge" / "concepts" / "small.md"
        text = _write_node(node, "Small Node", BODY_SMALL)
        mod, _ = self._load(["--all", "--rechunk"])
        server = _CountingServer()

        old_plan, new_plan = self._plans(mod, server, text)
        self.assertEqual((len(old_plan), len(new_plan)), (1, 1))

        fp = "knowledge/concepts/small.md"
        content_hash = mod._content_signature_excluding_updated(text)
        _seed_rows(server, PROJECT_KG, fp, content_hash, [text],
                   total_chunks=1)

        outcome, out = self._sync_node(mod, server, node)
        self.assertEqual(outcome.status, mod.OUTCOME_EMBED_SKIPPED,
                         f"unchanged plan must still skip under --rechunk:\n{out}")
        self.assertEqual(server.embed_calls, 0)
        self.assertEqual(mod._RECHUNKED_COUNT, 0)


if __name__ == "__main__":
    unittest.main()


# ── The gate must ARM on pre-existing projects (round-3 BLOCKER-A) ──────────

class GateArmsOnPreExistingProjectsTests(unittest.TestCase):
    """A missing sentinel on an EXISTING project is not evidence it is current.

    `vco_lib/chunker_revision.py` is NEW in v0.2.92, so no project installed
    before it has the sentinel file. Mapping "no sentinel" to
    "first-observation, nothing owed" therefore declared the ENTIRE installed
    base up to date, and the headline repair — qwen3's chunk budget 13 500 ->
    8 192, where the old value sat 32% above the model's context window and
    was silently truncating — reached none of them.

    The distinction that fixes it: a FRESH install has no sentinel because it
    is being built now, under the current revision (nothing owed). An UPDATE
    has no sentinel because the project predates the sentinel (resync owed).
    """

    def test_prior_install_with_no_sentinel_emits_the_resync(self):
        from vco_lib import chunker_revision
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / ".claude").mkdir(parents=True, exist_ok=True)
            self.assertIsNone(chunker_revision.read_last_revision(folder),
                              "fixture must start with NO sentinel")
            emitted = []
            with mock.patch(
                "vco_lib.project_init._emit_chunker_revision_resync_deferral",
                side_effect=lambda f, prev, cur: emitted.append((prev, cur)),
            ):
                outcome = chunker_revision.gate(folder, had_prior_install=True)
            self.assertEqual(outcome, "resync-emitted",
                             "a pre-existing project must be told a resync is owed")
            self.assertEqual(len(emitted), 1)

    def test_fresh_install_with_no_sentinel_owes_nothing(self):
        """The leave-alone case: built now, under the current revision."""
        from vco_lib import chunker_revision
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / ".claude").mkdir(parents=True, exist_ok=True)
            emitted = []
            with mock.patch(
                "vco_lib.project_init._emit_chunker_revision_resync_deferral",
                side_effect=lambda f, prev, cur: emitted.append((prev, cur)),
            ):
                outcome = chunker_revision.gate(folder, had_prior_install=False)
            self.assertEqual(outcome, "first-observation")
            self.assertEqual(emitted, [], "a fresh install owes no resync")

    def test_the_caller_threads_update_mode(self):
        """The gate cannot arm if install_project_bundle never tells it."""
        import inspect
        from vco_lib import project_init
        src = inspect.getsource(project_init.install_project_bundle)
        self.assertIn("had_prior_install=", src.split("_chunker_revision.gate(")[1][:200],
                      "install_project_bundle must tell the gate whether a prior install existed")
