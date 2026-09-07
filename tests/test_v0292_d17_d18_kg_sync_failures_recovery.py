# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 D17 — per-node KG-sync failures become owed, auto-retryable work.

Field report (Windows, 13 projects): ~210 knowledge nodes existed as ``.md``
files on disk with NO corresponding Weaviate object. The sync had counted
its per-node failures (WP-B1 fixed the honesty side), but NOTHING ever
retried a node whose write failed — ``rg kg_sync_pending`` returned 0 hits
anywhere. The nodes stayed invisible to retrieval forever, silently.

The fix (``templates/scripts/sync_knowledge_graph.py``):

* a run of ANY shape (``--all`` / ``--all-docs`` / file list) that finishes
  with per-node failures emits the ``kg_sync_failures_pending`` deferral
  condition, naming the failure count;
* the condition is registered ``auto_retryable`` with a REAL
  ``retry_action`` (``retry:py:kg_seed`` — the existing WP-H handler that
  re-runs ``sync_knowledge_graph.py --all`` for the project), so the
  detached retry driver re-attempts the owed work on its own;
* the paired clear is NARROW: only the end of a FULLY successful ``--all``
  tree sync resolves it (decision-#12 shape — a clean file-list or
  docs-only run proves nothing about the failed nodes).

Tests below cover the ACT and the LEAVE-ALONE cases both: a failing run
emits, a clean run does not emit, a later clean ``--all`` resolves an
entry a failing run left behind.

Scaffolding mirrors ``tests/test_v0292_kg_sync_flags_tally_stage.py``
(importlib-load the shipped script with pinned env, fake EmbeddingService
+ WeaviateMCPServer, in-memory stores). No live Weaviate, no real launcher
DB: ``VCT_STATE_DIR`` points at a disposable dir inside the test's tmp
root and every backend class is replaced before ``main()`` runs.
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
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"

PROJECT_KG = "V0292D17_KnowledgeGraph"
CID = "kg_sync_failures_pending"


# ─── In-memory fakes (shape mirrors test_v0292_kg_sync_flags_tally_stage) ──


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

    def matches(self, props: dict) -> bool:
        if self.subfilters is not None:
            return any(sf.matches(props) for sf in self.subfilters)
        return all(props.get(name) == value for name, value in self.matchers)


class _FakeObj:
    def __init__(self, uid, props, vector=None):
        self.uuid = uid
        self.properties = props
        self.vector = vector or {}


class _FakeQueryResult:
    def __init__(self, objects):
        self.objects = objects


class _FakeQuery:
    def __init__(self, store: dict, fail_insert: "list[bool]" | None = None):
        self._store = store

    def fetch_objects(self, filters=None, limit=100, return_properties=None,
                      include_vector=False):
        objs = list(self._store.values())
        if filters is not None:
            objs = [o for o in objs if filters.matches(o.properties)]
        return _FakeQueryResult(objs[:limit])


class _FakeData:
    def __init__(self, store: dict, insert_error: Exception | None = None):
        self._store = store
        self._insert_error = insert_error

    def insert(self, properties=None, vector=None):
        if self._insert_error is not None:
            raise self._insert_error
        uid = str(uuid.uuid4())
        self._store[uid] = _FakeObj(uid, dict(properties or {}), vector)
        return uid

    def delete_by_id(self, uid):
        self._store.pop(str(uid), None)

    def reference_add(self, **kwargs):  # noqa: ARG002
        pass


class _FakeCollection:
    def __init__(self, store: dict, insert_error: Exception | None = None):
        self._store = store
        self.query = _FakeQuery(store)
        self.data = _FakeData(store, insert_error)

    def set_insert_error(self, error: Exception | None) -> None:
        """Flip the store between failing and working (the recovery arc)."""
        self.data._insert_error = error


class _FakeCollections:
    def __init__(self, insert_error: Exception | None = None):
        self._stores: dict[str, dict] = {}
        self._insert_error = insert_error
        self._col_cache: dict[str, _FakeCollection] = {}

    def _store_for(self, name: str) -> dict:
        return self._stores.setdefault(name, {})

    def get(self, name: str) -> _FakeCollection:
        col = self._col_cache.get(name)
        if col is None:
            col = _FakeCollection(self._store_for(name), self._insert_error)
            self._col_cache[name] = col
        return col

    def exists(self, name: str) -> bool:  # noqa: ARG002
        return True

    def create(self, **kwargs):  # noqa: ARG002
        pass


class _FakeClient:
    def __init__(self, insert_error: Exception | None = None):
        self.collections = _FakeCollections(insert_error)


class _FakeEmbeddingService:
    text_model_id = "qwen3-embedding:0.6b"


class _FakeServer:
    def __init__(self, insert_error: Exception | None = None, **kwargs):  # noqa: ARG004
        self.client = _FakeClient(insert_error)
        self.embedding_service = _FakeEmbeddingService()
        self.text_vector_slot = "qwen3_embed"

    def _get_embedding(self, text):  # noqa: ARG002
        return [0.5, 0.5, 0.5]

    def _get_all_kg_embeddings(self, text):  # noqa: ARG002
        return {self.text_vector_slot: [0.5, 0.5, 0.5]}

    def _get_all_kg_embeddings_tagged(self, text):  # noqa: ARG002
        # W3: the tagged capture the sync write path now persists.
        return {self.text_vector_slot: [0.5, 0.5, 0.5]}, []

    def close(self):
        pass


class _ServerHarness:
    """Stands in for the WeaviateMCPServer CLASS; keeps the built server."""

    def __init__(self, insert_error: Exception | None = None):
        self.insert_error = insert_error
        self.instances: list[_FakeServer] = []

    def __call__(self, **kwargs) -> _FakeServer:
        srv = _FakeServer(self.insert_error, **kwargs)
        self.instances.append(srv)
        return srv

    @property
    def last(self) -> _FakeServer:
        return self.instances[-1]


# ─── Module loading (env-controlled, mirrors the WP-B1 lane) ────────────

_ENV_KEYS = (
    "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
    "VCT_DISABLE_HUB_RESOLVER", "KG_SYNC_PROJECT_ROOT", "VCT_STATE_DIR",
)


def _load_sync_module(project_root: Path, *, dev: str = ""):
    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = PROJECT_KG
    os.environ["SHARED_KG_COLLECTION"] = ""
    os.environ["DEVELOPMENT_COLLECTION"] = dev
    os.environ["DUAL_EMBEDDING_ENABLED"] = "false"
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
    os.environ.pop("KG_SYNC_PROJECT_ROOT", None)

    mod_name = f"_sync_kg_v0292d17_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest(
            f"sync_knowledge_graph.py has runtime deps not installed ({exc})"
        )
    mod.Filter = _FakeFilter
    # Keep the test run off the real ~/.claude/metrics — telemetry is
    # optional-by-design and irrelevant to the recovery contract.
    mod.HAS_LOGGER = False
    return mod


def _write_node(path: Path, title: str, body: str = "Body text.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\ntype: concept\nstatus: active\n---\n{body}\n",
        encoding="utf-8",
    )


class _SyncTestBase(unittest.TestCase):
    """tmp project root + env isolation + loaded module + fake backends."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        # Disposable state dir — the deferral machinery writes ONLY under
        # this root; the real ~/.vct/launcher.db is never consulted.
        self.state_dir = self.root / "vct-state"
        os.environ["VCT_STATE_DIR"] = str(self.state_dir)
        (self.root / "knowledge").mkdir()

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def load(self, *, dev: str = ""):
        return _load_sync_module(self.root, dev=dev)

    def install_working_backends(self, mod) -> _ServerHarness:
        harness = _ServerHarness()
        self._install(mod, harness)
        return harness

    def install_failing_backends(self, mod) -> _ServerHarness:
        harness = _ServerHarness(RuntimeError("weaviate insert unavailable"))
        self._install(mod, harness)
        return harness

    def _install(self, mod, harness) -> None:
        class _FakeEmbeddingServiceCls:
            @staticmethod
            def for_project(root):  # noqa: ARG003
                return _FakeEmbeddingService()

        mod.EmbeddingService = _FakeEmbeddingServiceCls
        mod.WeaviateMCPServer = harness
        mod.ensure_collection_exists = lambda srv: True  # noqa: ARG003
        mod.ensure_dev_collection_exists = lambda srv: None  # noqa: ARG003
        # The post-summary regen subprocess is out of scope here (it is
        # soft-fail and never changes the exit code) — skip its 600 s cap.
        mod._regen_node_formats_after_full_sync = lambda: None

    def run_main(self, mod, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                mod.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    # ── ledger helpers ──

    def ledger_entries(self, cid: str = CID):
        from vco_lib.deferral_report import DeferralReport

        report = DeferralReport.read(self.root)
        return [e for e in report.entries if e.condition_id == cid]


# ─── 1. Registry wiring — the retry_action is REAL ──────────────────────


class RegistryWiringTests(unittest.TestCase):
    """The condition is auto_retryable AND the retry driver can select it.

    The codegraph_embed_resync_pending lesson (v0.2.91 dogfood): a row may
    ship classed auto_retryable with NO real retry_action, and the ledger
    then says "VCO retries this itself" while nothing does. These pins make
    that state a test failure for THIS condition.
    """

    def test_registry_row_is_auto_retryable_with_kg_seed_retry(self):
        from vco_lib import deferral_registry as dr

        specs = {s.pattern: s for s in dr.all_specs()}
        spec = specs.get(CID)
        self.assertIsNotNone(spec, f"{CID} must be registered")
        self.assertEqual(spec.condition_class, "auto_retryable")
        self.assertEqual(spec.retry_action, "retry:py:kg_seed")

    def test_dispatcher_selects_a_real_handler(self):
        from vco_lib.deferral_retry import HANDLERS, handler_name_for

        self.assertEqual(handler_name_for(CID), "kg_seed")
        self.assertIn("kg_seed", HANDLERS, "retry_action must name a real handler")

    def test_handler_reruns_the_sync_all_script(self):
        """The retry_action's payload is the recovery the field lacked:
        the handler re-runs sync_knowledge_graph.py --all for the project."""
        import inspect

        from vco_lib.deferral_retry import retry_kg_seed

        body = inspect.getsource(retry_kg_seed)
        self.assertIn('"--all"', body)
        self.assertIn("sync_knowledge_graph", body)


# ─── 2. Behaviour — emit / leave-alone / paired clear ───────────────────


class SyncFailuresRecoveryTests(_SyncTestBase):
    def test_failing_all_run_emits_condition_naming_count(self):
        """THE ACT: per-node write failures → owed-work entry with the
        failure count, in THIS project's ledger."""
        _write_node(self.root / "knowledge" / "concepts" / "a.md", "Node A")
        _write_node(self.root / "knowledge" / "concepts" / "b.md", "Node B")
        mod = self.load()
        self.install_failing_backends(mod)

        code, out, err = self.run_main(mod, ["kg-sync", "--all"])

        self.assertEqual(code, 1, "per-node failures must exit 1 (WP-B1)")
        entries = self.ledger_entries()
        self.assertEqual(
            len(entries), 1,
            f"expected exactly one {CID} entry, got {len(entries)}; "
            f"stdout tail: {out[-400:]!r}; stderr tail: {err[-400:]!r}",
        )
        entry = entries[0]
        self.assertIn("2", entry.detected, "the entry must NAME the count")
        self.assertIn("knowledge node(s)", entry.detected)
        self.assertIn("--all", entry.detected)

    def test_clean_all_run_does_not_emit(self):
        """LEAVE-ALONE: a clean run owes nothing — no entry, no ledger."""
        _write_node(self.root / "knowledge" / "concepts" / "a.md", "Node A")
        mod = self.load()
        self.install_working_backends(mod)

        code, out, err = self.run_main(mod, ["kg-sync", "--all"])

        self.assertEqual(code, 0)
        self.assertEqual(
            self.ledger_entries(), [],
            "a clean run must not emit the owed-work condition",
        )
        self.assertFalse(
            (self.root / ".claude" / "context" / "UPDATE_DEFERRED.json").exists(),
            "a clean run on a clean ledger writes no ledger at all",
        )

    def test_later_clean_run_resolves_condition_left_by_failing_run(self):
        """The recovery arc: fail → entry exists; heal + clean --all → the
        paired clear resolves it (this is also what the WP-H dispatcher's
        ledger re-read observes)."""
        _write_node(self.root / "knowledge" / "concepts" / "a.md", "Node A")
        _write_node(self.root / "knowledge" / "concepts" / "b.md", "Node B")
        mod = self.load()
        harness = self.install_failing_backends(mod)

        code, _out, _err = self.run_main(mod, ["kg-sync", "--all"])
        self.assertEqual(code, 1)
        self.assertEqual(len(self.ledger_entries()), 1)

        # Backend heals (the precondition the retry driver gates on).
        # main() builds a NEW server per run, so the heal must flip the
        # HARNESS (the factory), not just the first run's instance.
        harness.insert_error = None

        code, _out, _err = self.run_main(mod, ["kg-sync", "--all"])
        self.assertEqual(code, 0)
        self.assertEqual(
            self.ledger_entries(), [],
            "a later fully-successful --all must resolve the condition",
        )
        self.assertFalse(
            (self.root / ".claude" / "context" / "UPDATE_DEFERRED.json").exists(),
            "resolving the last entry deletes the ledger files",
        )

    def test_failing_file_list_run_emits_too(self):
        """The kg-sync-on-edit hook path (explicit file list) leaves the
        same owed work — pre-fix, a failed hook sync was never retried."""
        node = self.root / "knowledge" / "concepts" / "a.md"
        _write_node(node, "Node A")
        mod = self.load()
        self.install_failing_backends(mod)

        code, _out, _err = self.run_main(
            mod, ["kg-sync", str(node)]
        )

        self.assertEqual(code, 1)
        entries = self.ledger_entries()
        self.assertEqual(len(entries), 1)
        self.assertIn("file list", entries[0].detected)
        self.assertIn("1", entries[0].detected)

    def test_clean_file_list_run_does_not_clear_a_prior_failure(self):
        """NARROW clear: a clean file-list run proves nothing about nodes
        an earlier --all failed on, so it must NOT resolve the entry."""
        _write_node(self.root / "knowledge" / "concepts" / "a.md", "Node A")
        _write_node(self.root / "knowledge" / "concepts" / "b.md", "Node B")
        mod = self.load()
        harness = self.install_failing_backends(mod)

        code, _out, _err = self.run_main(mod, ["kg-sync", "--all"])
        self.assertEqual(code, 1)
        self.assertEqual(len(self.ledger_entries()), 1)

        harness.insert_error = None

        # Clean run over ONE file only (the hook shape) — still owes the
        # other node's recovery.
        code, _out, _err = self.run_main(
            mod, ["kg-sync", str(self.root / "knowledge" / "concepts" / "a.md")]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            len(self.ledger_entries()), 1,
            "only a fully-successful --all may resolve the condition",
        )


if __name__ == "__main__":
    unittest.main()
