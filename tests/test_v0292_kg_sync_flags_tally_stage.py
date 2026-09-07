# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-B1 — kg-sync honesty: unknown flags, terminal tally,
finalize stage, canonical POSIX ``file_path``.

Field-report cluster (WFT, Windows):

* **Unknown flags degrade into sync targets.** Pre-fix,
  ``sync_knowledge_graph.py --typo value`` fell into the file-list branch
  (both tokens reported "not under knowledge/ or docs/ — skipping", exit
  0 with ``0 succeeded, 0 failed``) and a LONE ``--typo`` matched no
  dispatch branch at all — it connected to Weaviate, printed nothing
  after the banner, and exited 0. Both silent-success shapes are now
  hard usage errors (exit 2) BEFORE any backend connection. The flag
  vocabulary lives in ONE home (the Python ``main()``); the ``.sh`` /
  ``.ps1`` wrappers stay dumb forwarders, and the wrapper tests below
  prove the rejection reaches the user through both wrapper shapes.
* **Archived / frontmarker / excluded skips were counted as SUCCEEDED**
  (D12): "117/117 succeeded" could hide nodes intentionally absent from
  Weaviate, and no run ever listed WHICH paths were skipped or failed or
  why. The terminal line now appends ``, K skipped`` and a details block
  names every non-synced path with a reason (full list to
  ``<vct_root>/logs/kg-sync-<ts>.log``).
* **The post-summary ``.node_formats.json`` regen had no stage signal**:
  the script prints its final ``📊`` counts, then runs a synchronous
  regen (up to 600 s) while the launcher still shows "embedding (N/N)".
  The ``📝 Refreshing …`` stage marker is now pinned (text + flush) and
  mapped to the launcher's ``finalize`` phase (kg_sync.rs side).
* **``file_path`` was stored OS-native** (backslashes on Windows) while
  the MCP's ``store_knowledge_node`` stores POSIX (server.py C-7 since
  v0.2.75) — two shapes for one file, so each writer's delete-by-
  file_path missed the other's rows and duplicates accumulated (D13).
  The sync script now stores POSIX at every write and matches BOTH
  spellings at delete/lookup, mirroring the MCP.

Red-proof
---------
The module under test defaults to the tree's shipped script. Setting
``KG_SYNC_SCRIPT_UNDER_TEST=/tmp/<lane>/pre/sync_knowledge_graph.py``
runs the SAME assertions against a pre-fix copy — the flag tests then
fail with the historical silent-exit-0/usage-less behaviour, and the
tally/duplicate tests fail with archived-counted-as-succeeded /
duplicate-rows-stacked. That is the red side of the red-proof; it is a
lane activity, never part of CI.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path, PureWindowsPath
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = Path(os.environ.get(
    "KG_SYNC_SCRIPT_UNDER_TEST",
    str(REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"),
))
KG_SYNC_SH = REPO_ROOT / "templates" / "scripts" / "kg-sync"
KG_SYNC_PS1 = REPO_ROOT / "templates" / "scripts" / "kg-sync.ps1"

PROJECT_KG = "V0292WPB1_KnowledgeGraph"

_IS_PRE_FIX = "KG_SYNC_SCRIPT_UNDER_TEST" in os.environ


# ─── In-memory fake Weaviate client (shape mirrors test_v0289) ──────────


class _FakeProp:
    def __init__(self, name: str):
        self.name = name

    def equal(self, value):
        return _FakeFilter([(self.name, value)])


class _FakeFilter:
    """Fake of ``weaviate.classes.query.Filter`` incl. ``any_of`` (the
    dual-shape delete filter added in v0.2.92 WP-B1)."""

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

    def __init__(self, **kwargs):  # noqa: ARG002 — WeaviateMCPServer ctor shape
        self.client = _FakeClient()
        self.embedding_service = _FakeEmbeddingService()
        self.text_vector_slot = "qwen3_embed"
        self.embed_calls = 0

    def _get_embedding(self, text):  # noqa: ARG002
        self.embed_calls += 1
        return [0.5, 0.5, 0.5]

    def _get_all_kg_embeddings(self, text):  # noqa: ARG002
        self.embed_calls += 1
        return {self.text_vector_slot: [0.5, 0.5, 0.5]}

    def _get_all_kg_embeddings_tagged(self, text):  # noqa: ARG002
        # W3: the tagged capture the sync write path now persists.
        self.embed_calls += 1
        return {self.text_vector_slot: [0.5, 0.5, 0.5]}, []

    def close(self):
        pass


class _ServerHarness:
    """Stands in for the WeaviateMCPServer CLASS: constructs counting
    servers and records them so tests can inspect the fake stores."""

    def __init__(self):
        self.instances: list[_CountingServer] = []

    def __call__(self, **kwargs) -> _CountingServer:
        srv = _CountingServer(**kwargs)
        self.instances.append(srv)
        return srv

    @property
    def last(self) -> _CountingServer:
        return self.instances[-1]


# ─── Module loading (env-controlled, mirrors test_v0289) ────────────────

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

    mod_name = f"_sync_kg_v0292wpb1_{uuid.uuid4().hex}"
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
    return mod


def _write_node(path: Path, title: str, body: str = "Body text.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\ntype: concept\nstatus: active\n---\n{body}\n",
        encoding="utf-8",
    )


def _seed_row(server: _CountingServer, collection: str, file_path: str,
              content_hash: str = "seedhash", *, chunk_num: int = 1,
              total_chunks: int = 1) -> str:
    """Pre-seed one row into a named fake collection; returns its uuid."""
    store = server.client.collections._store_for(collection)
    uid = str(uuid.uuid4())
    store[uid] = _FakeObj(uid, {
        "file_path": file_path,
        "content_hash": content_hash,
        "chunk_num": chunk_num,
        "total_chunks": total_chunks,
    })
    return uid


def _rows(server: _CountingServer, collection: str):
    return list(server.client.collections._store_for(collection).values())


class _SyncTestBase(unittest.TestCase):
    """tmp project root + env isolation + loaded module + fake backends."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
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
        """Patch main()'s collaborators so a run proceeds on fake stores.
        Records _regen / paired-clear invocations. The REAL regen fn is
        kept on ``self.real_regen`` — the finalize tests restore it so the
        genuine marker-emission path runs against a stub generator."""
        self.calls = {"regen": 0, "clear": 0}
        self.real_regen = mod._regen_node_formats_after_full_sync
        harness = _ServerHarness()

        class _FakeEmbeddingServiceCls:
            @staticmethod
            def for_project(root):  # noqa: ARG003
                return _FakeEmbeddingService()

        mod.EmbeddingService = _FakeEmbeddingServiceCls
        mod.WeaviateMCPServer = harness
        mod.ensure_collection_exists = lambda srv: True  # noqa: ARG003
        mod.ensure_dev_collection_exists = lambda srv: None  # noqa: ARG003
        mod._regen_node_formats_after_full_sync = (
            lambda: self.calls.__setitem__("regen", self.calls["regen"] + 1)
        )
        mod._clear_sync_deferral_no_backend = (
            lambda root: self.calls.__setitem__("clear", self.calls["clear"] + 1)  # noqa: ARG003
        )
        return harness

    def run_main(self, mod, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                mod.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


# ─── 1. Flag validation ─────────────────────────────────────────────────


class FlagValidationTests(_SyncTestBase):
    """Unknown flags → usage error (exit 2) BEFORE any backend work."""

    def _run_main(self, argv):
        """Run main() with backend collaborators replaced by SENTINELS
        that raise — proof that a rejected run never constructs backends
        and that an accepted run does (main's broad ``except`` turns the
        sentinel into "❌ Fatal error: <sentinel>" + exit 1, which the
        tests read back out of the captured output)."""
        mod = self.load()

        class _SentinelEmbeddingService:
            @staticmethod
            def for_project(root):  # noqa: ARG003
                raise RuntimeError("reached-backend")

        class _SentinelServer:
            def __init__(self, **kwargs):  # noqa: ARG004
                raise RuntimeError("reached-weaviate-constructor")

        mod.EmbeddingService = _SentinelEmbeddingService
        mod.WeaviateMCPServer = _SentinelServer
        mod.ensure_collection_exists = lambda srv: True  # noqa: ARG003
        mod.ensure_dev_collection_exists = lambda srv: None  # noqa: ARG003
        return self.run_main(mod, argv)

    def test_lone_unknown_flag_exits_2_naming_token(self):
        code, out, err = self._run_main(["kg-sync", "--foo"])
        self.assertEqual(code, 2)
        self.assertIn("--foo", err)
        self.assertIn("Unrecognized option", err)
        self.assertIn("Usage:", err)
        self.assertNotIn("🧭", out, "usage errors must precede the banner")
        self.assertNotIn("reached-backend", out + err,
                         "must be rejected before any backend construction")

    def test_flag_with_value_exits_2_not_silent_sync_targets(self):
        # Pre-fix: fell into the file-list branch → "0 succeeded, 0 failed",
        # exit 0. Red-proof target.
        code, out, err = self._run_main(["kg-sync", "--foo", "bar"])
        self.assertEqual(code, 2)
        self.assertIn("--foo", err)
        self.assertNotIn("succeeded", out)

    def test_unknown_flag_after_mode_flag_exits_2(self):
        # `--all --force` was silently ignored pre-fix (dispatch keyed on
        # argv[1] only).
        code, _out, err = self._run_main(["kg-sync", "--all", "--force"])
        self.assertEqual(code, 2)
        self.assertIn("--force", err)

    def test_mode_flag_with_file_args_exits_2(self):
        code, _out, err = self._run_main(["kg-sync", "--all", "extra.md"])
        self.assertEqual(code, 2)
        self.assertIn("takes no file arguments", err)
        self.assertIn("extra.md", err)

    def test_leftover_duplicate_project_root_exits_2(self):
        # The first --project-root is consumed at import; a leftover one in
        # main()'s argv means it appeared twice.
        code, _out, err = self._run_main(["kg-sync", "--project-root", "/tmp"])
        self.assertEqual(code, 2)
        self.assertIn("at most once", err)

    def test_help_prints_usage_and_exits_0(self):
        code, out, err = self._run_main(["kg-sync", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("Usage:", out)
        self.assertEqual(err, "")

    def test_no_args_is_usage_error_exit_2(self):
        # v0.2.92 exit contract: usage = 2 (was 1 pre-fix).
        code, _out, err = self._run_main(["kg-sync"])
        self.assertEqual(code, 2)
        self.assertIn("Usage:", err)

    def test_accepted_flags_reach_backend_construction(self):
        # Validation must not OVER-reject: each accepted form proceeds to
        # EmbeddingService.for_project (the sentinel names it in the fatal
        # handler's output).
        for argv in (["kg-sync", "--all"], ["kg-sync", "--all-docs"],
                     ["kg-sync", "knowledge/concepts/x.md"]):
            with self.subTest(argv=argv):
                code, out, err = self._run_main(argv)
                self.assertIn("reached-backend", out + err,
                              f"{argv} must pass validation and reach the "
                              f"EmbeddingService construction")


# ─── 2. Terminal tally + details block ──────────────────────────────────


class TallyAndDetailsTests(_SyncTestBase):
    """`--all` counts skips honestly and names every non-synced path."""

    def _fixture(self):
        _write_node(self.root / "knowledge" / "concepts" / "active.md", "Active")
        _write_node(self.root / "knowledge" / "archive" / "old.md", "Old")
        _write_node(self.root / "knowledge" / "TAG_HIERARCHY.md", "Tags")

    def test_all_reports_skipped_and_not_succeeded(self):
        self._fixture()
        mod = self.load()
        harness = self.install_working_backends(mod)
        code, out, err = self.run_main(mod, ["kg-sync", "--all"])

        self.assertEqual(code, 0, f"clean run (skips are fine). stderr:\n{err}")
        # 1 real write; the archived node and TAG_HIERARCHY.md are SKIPPED,
        # never succeeded (D12).
        self.assertIn("📊 KG:   1 succeeded, 0 failed, 2 skipped", out)
        self.assertIn("📊 Docs: 0 succeeded, 0 failed, 0 skipped", out)
        # Discovered count (post-exclusion of TAG_HIERARCHY.md): active +
        # archived = 2.
        self.assertIn("📚 Found 2 markdown files in knowledge/", out)
        # Details block names both non-synced paths with reasons.
        self.assertIn("📋 2 not-synced item(s) this run (--all): "
                      "1 archived-skipped, 1 excluded-skipped", out)
        self.assertIn("knowledge/archive/old.md — archived node:", out)
        self.assertIn("knowledge/TAG_HIERARCHY.md — excluded meta file", out)
        # Only the ACTIVE node reached Weaviate (1 embed, 1 row).
        server = harness.last
        self.assertEqual(len(_rows(server, PROJECT_KG)), 1)
        self.assertEqual(server.embed_calls, 1)
        # Paired clear still fires on a zero-FAILURE run (skips don't block).
        self.assertEqual(self.calls["clear"], 1)
        # The finalize stage step ran after the summary lines.
        self.assertEqual(self.calls["regen"], 1)
        self.assertLess(
            out.index("📊 KG:"), out.index("📋"),
            "summary lines precede the details block",
        )

    def test_details_log_written_under_vct_state_dir(self):
        self._fixture()
        mod = self.load()
        self.install_working_backends(mod)
        _code, out, _err = self.run_main(mod, ["kg-sync", "--all"])
        logs = list((self.state_dir / "logs").glob("kg-sync-*.log"))
        self.assertEqual(len(logs), 1, f"one run log per run; out tail:\n{out[-500:]}")
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn("[archived-skipped] knowledge/archive/old.md", text)
        self.assertIn("[excluded-skipped] knowledge/TAG_HIERARCHY.md", text)

    def test_failure_in_file_list_prints_path_and_exits_1(self):
        mod = self.load()
        self.install_working_backends(mod)
        ghost = str(self.root / "knowledge" / "concepts" / "ghost.md")
        out_of_root = "knowledge/concepts/other.md"
        code, out, err = self.run_main(
            mod, ["kg-sync", ghost, out_of_root])
        self.assertEqual(code, 1, f"stderr:\n{err}")
        self.assertIn("📊 List: 0 succeeded, 1 failed, 1 skipped", out)
        # The failing path is named in the details block (reason: not found).
        self.assertIn("knowledge/concepts/ghost.md — file not found", out)
        # The out-of-root target is an excluded-skip WITH its reason.
        self.assertIn(
            "knowledge/concepts/other.md — not under knowledge/ or docs/", out)


# ─── 3. Finalize stage marker ───────────────────────────────────────────


class FinalizeStageMarkerTests(_SyncTestBase):
    """The `📝 Refreshing …` marker is pinned: exact text, flushed,
    printed BEFORE the regen subprocess, never fatal."""

    def test_marker_line_is_exact_and_flushed_in_source(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn(
            'print("📝 Refreshing .node_formats.json summaries (KG-4, soft-fail) ...",\n'
            "              flush=True)",
            text,
            "the stage marker's text is load-bearing (kg_sync.rs::is_finalize_line "
            "matches its stable prefix) and must be flushed so the launcher's line "
            "reader sees it before the up-to-600 s regen starts",
        )
        # Ordering pin: the marker print precedes subprocess.run in the
        # regen function body.
        fn_text = text[text.index("def _regen_node_formats_after_full_sync"):]
        fn_text = fn_text[:fn_text.index("\ndef ")]
        self.assertLess(
            fn_text.index("📝 Refreshing .node_formats.json summaries"),
            fn_text.index("subprocess.run("),
            "the marker must be printed before the regen subprocess starts",
        )

    def _stub_generator(self, *, exit_code: int = 0) -> Path:
        gen = self.root / ".claude" / "scripts" / "generate-kg-summary.py"
        gen.parent.mkdir(parents=True, exist_ok=True)
        sentinel = self.root / "regen-ran.sentinel"
        if exit_code == 0:
            gen.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('ran')\n",
                encoding="utf-8",
            )
        else:
            gen.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                f"sys.exit({exit_code})\n",
                encoding="utf-8",
            )
        return sentinel

    def test_marker_emitted_and_stub_generator_invoked(self):
        # A stub summary generator exists → the REAL regen path prints the
        # marker and runs the stub; the run still exits 0.
        sentinel = self._stub_generator(exit_code=0)
        _write_node(self.root / "knowledge" / "concepts" / "active.md", "Active")
        mod = self.load()
        self.install_working_backends(mod)
        mod._regen_node_formats_after_full_sync = self.real_regen
        code, out, err = self.run_main(mod, ["kg-sync", "--all"])
        self.assertEqual(code, 0, f"stderr:\n{err}")
        self.assertIn(
            "📝 Refreshing .node_formats.json summaries (KG-4, soft-fail) ...",
            out,
        )
        self.assertTrue(sentinel.exists(), "the regen generator was invoked")

    def test_regen_failure_is_non_fatal(self):
        self._stub_generator(exit_code=3)
        _write_node(self.root / "knowledge" / "concepts" / "active.md", "Active")
        mod = self.load()
        self.install_working_backends(mod)
        mod._regen_node_formats_after_full_sync = self.real_regen
        code, _out, err = self.run_main(mod, ["kg-sync", "--all"])
        self.assertEqual(code, 0, "a failed summary regen never fails the sync")
        self.assertIn("node-format refresh exited 3", err)


# ─── 4. Canonical POSIX file_path (D13) ─────────────────────────────────


class CanonicalFilePathTests(_SyncTestBase):
    def test_canonical_helper_is_posix_on_windows_shaped_paths(self):
        mod = self.load()
        with mock.patch.object(mod, "PROJECT_ROOT", PureWindowsPath(r"C:\proj")):
            got = mod._canonical_file_path(
                PureWindowsPath(r"C:\proj\knowledge\concepts\foo.md")
            )
        self.assertEqual(got, "knowledge/concepts/foo.md",
                         "the stored file_path must be POSIX on Windows")

    def test_file_path_filter_matches_both_shapes_when_they_differ(self):
        mod = self.load()
        f = mod._file_path_filter("knowledge/concepts/foo.md")
        self.assertTrue(f.matches({"file_path": "knowledge/concepts/foo.md"}))
        self.assertTrue(f.matches({"file_path": "knowledge\\concepts\\foo.md"}),
                        "legacy Windows-written rows must be matched for delete")
        self.assertFalse(f.matches({"file_path": "knowledge/other.md"}))

    def test_file_path_filter_single_shape_when_no_separator(self):
        mod = self.load()
        f = mod._file_path_filter("README.md")
        self.assertTrue(f.matches({"file_path": "README.md"}))
        self.assertFalse(f.matches({"file_path": "README\\md"}))

    def test_sync_node_stores_canonical_posix(self):
        node = self.root / "knowledge" / "concepts" / "foo.md"
        _write_node(node, "Foo")
        mod = self.load()
        server = _CountingServer()
        outcome = mod.sync_node(server, node)
        self.assertTrue(outcome)
        rows = _rows(server, PROJECT_KG)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].properties["file_path"],
                         "knowledge/concepts/foo.md")

    def test_legacy_backslash_row_is_deleted_not_duplicated(self):
        # D13 transition: a row written by a pre-canonical (Windows) sync
        # carries the backslash spelling. The re-write must FIND it via the
        # dual-shape filter and DELETE it — not stack a duplicate set.
        node = self.root / "knowledge" / "concepts" / "foo.md"
        _write_node(node, "Foo", body="Version two of the body.")
        mod = self.load()
        server = _CountingServer()
        _seed_row(server, PROJECT_KG, "knowledge\\concepts\\foo.md")
        outcome = mod.sync_node(server, node)
        self.assertTrue(outcome)
        rows = _rows(server, PROJECT_KG)
        self.assertEqual(
            len(rows), 1,
            f"duplicate rows stacked: {[r.properties['file_path'] for r in rows]}",
        )
        self.assertEqual(rows[0].properties["file_path"],
                         "knowledge/concepts/foo.md")

    def test_embed_skip_fires_for_canonical_unchanged_rows(self):
        node = self.root / "knowledge" / "concepts" / "foo.md"
        _write_node(node, "Foo")
        mod = self.load()
        server = _CountingServer()
        # Seed the row exactly as a previous successful sync would have
        # written it (canonical shape, matching content hash, 1 chunk).
        # The signature function deliberately excludes `updated:`, so
        # hashing the post-timestamp-rewrite text is stable.
        content2 = mod._update_frontmatter_timestamp(
            node, node.read_text(encoding="utf-8"))
        h = mod._content_signature_excluding_updated(content2)
        _seed_row(server, PROJECT_KG, "knowledge/concepts/foo.md",
                  content_hash=h)
        outcome = mod.sync_node(server, node)
        self.assertTrue(outcome)
        self.assertEqual(server.embed_calls, 0, "unchanged row → no embed")
        self.assertEqual(len(_rows(server, PROJECT_KG)), 1)
        self.assertEqual(getattr(outcome, "status", "embed-skipped"),
                         "embed-skipped")

    def test_embed_skip_does_NOT_fire_for_legacy_shaped_rows(self):
        # Same hash, but the stored row carries the backslash spelling —
        # the fast path must NOT preserve a legacy-shaped row; it falls
        # through to delete-and-rewrite so the shape heals.
        node = self.root / "knowledge" / "concepts" / "foo.md"
        _write_node(node, "Foo")
        mod = self.load()
        server = _CountingServer()
        content2 = mod._update_frontmatter_timestamp(
            node, node.read_text(encoding="utf-8"))
        h = mod._content_signature_excluding_updated(content2)
        _seed_row(server, PROJECT_KG, "knowledge\\concepts\\foo.md",
                  content_hash=h)
        outcome = mod.sync_node(server, node)
        self.assertTrue(outcome)
        self.assertGreater(server.embed_calls, 0, "legacy shape → re-embed")
        rows = _rows(server, PROJECT_KG)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].properties["file_path"],
                         "knowledge/concepts/foo.md")

    def test_outcome_truthiness_contract_for_external_callers(self):
        # maintain_knowledge_graph.py does `if sync_node(...):` — every
        # non-failure outcome must stay truthy and failures falsy.
        mod = self.load()
        ok = mod.SyncOutcome(mod.OUTCOME_SYNCED, "a.md")
        skip = mod.SyncOutcome(mod.OUTCOME_ARCHIVED_SKIPPED, "a.md", "r")
        fail = mod.SyncOutcome(mod.OUTCOME_FAILED, "a.md", "r")
        self.assertTrue(bool(ok) and bool(skip))
        self.assertFalse(bool(fail))


# ─── 5. Wrappers forward the rejection (both OS shapes) ─────────────────


def _write_forwarding_shim(path: Path) -> None:
    """An executable that forwards every invocation to the test run's
    real interpreter — so the wrapper tests exercise the REAL wrapper +
    REAL script while the ``import weaviate, weaviate_mcp`` probe
    succeeds exactly when the test environment can run the script."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"exec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _wrapper_env(venv_root: Path) -> dict:
    env = os.environ.copy()
    env.pop("VCT_INSTALL_ROOT", None)
    env.pop("KG_SYNC_PROJECT_ROOT", None)
    env.pop("KG_BASE_DIR", None)
    env.pop("VIRTUAL_ENV", None)
    env["VCT_VENV"] = str(venv_root)
    env["PYTHONPATH"] = str(REPO_ROOT)
    # PIN THE ORCHESTRATOR ROOT TO THIS CHECKOUT.
    #
    # `sync_knowledge_graph.py` resolves its `vco_lib` parent from
    # $VCT_ORCHESTRATOR_ROOT and inserts it at `sys.path[0]` — ahead of
    # PYTHONPATH. Inheriting the developer's value therefore made these
    # subprocesses import a DIFFERENT CHECKOUT's `vco_lib`: on this repo's dev
    # box that variable points at the dogfood fork, which lags the public tree.
    #
    # It surfaced as a `ModuleNotFoundError` for a module that plainly exists
    # here — but the quiet failure is worse than the loud one. When the two
    # trees merely DISAGREE (a constant retuned in one and not the other),
    # nothing raises and the test simply measures the wrong tree: an audit
    # earlier in this cycle read a stale 13 500-token chunk budget from exactly
    # this leak and nearly filed it as a defect in code that was already fixed.
    env["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    return env


@unittest.skipIf(_IS_PRE_FIX, "wrapper subprocess tests target the tree's script")
class BashWrapperForwardsRejectionTests(unittest.TestCase):
    """The bash wrapper is a dumb forwarder — the Python-side exit 2 and
    stderr token must reach the caller unchanged."""

    def test_unknown_flag_through_bash_wrapper(self):
        with tempfile.TemporaryDirectory() as td:
            venv_root = Path(td) / "venv"
            _write_forwarding_shim(venv_root / "bin" / "python")
            proc = subprocess.run(
                ["bash", str(KG_SYNC_SH), "--foo"],
                env=_wrapper_env(venv_root),
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(proc.returncode, 2,
                         f"out={proc.stdout!r} err={proc.stderr!r}")
        self.assertIn("--foo", proc.stderr + proc.stdout,
                      "the bad token must be named in the wrapper's output")

    def test_flag_with_value_through_bash_wrapper(self):
        with tempfile.TemporaryDirectory() as td:
            venv_root = Path(td) / "venv"
            _write_forwarding_shim(venv_root / "bin" / "python")
            proc = subprocess.run(
                ["bash", str(KG_SYNC_SH), "--foo", "bar"],
                env=_wrapper_env(venv_root),
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--foo", proc.stderr)


def _pwsh() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


class Ps1WrapperForwardTests(unittest.TestCase):
    """The PowerShell sibling forwards @args and propagates
    $LASTEXITCODE — the source-level mechanism pin ALWAYS runs (a skipped
    gate is a gate that passes when switched off); the live pwsh drive
    runs when an interpreter is available.

    On Linux pwsh, Join-Path normalizes the .ps1's Windows-shaped
    candidates (``bin\\python``) to the POSIX layout (``bin/python``), so
    the same forwarding-venv fixture as the bash wrapper serves both —
    exercising the .ps1's own candidate ladder, unmodified."""

    def test_ps1_forwards_args_and_exit_code_source_pin(self):
        text = KG_SYNC_PS1.read_text(encoding="utf-8")
        self.assertIn('@args', text,
                      "kg-sync.ps1 must forward @args verbatim to the script")
        self.assertIn('exit $LASTEXITCODE', text,
                      "kg-sync.ps1 must propagate the script's exit code — "
                      "an exit-2 usage rejection must reach the caller")

    @unittest.skipUnless(_pwsh(), "pwsh not available")
    def test_unknown_flag_through_pwsh_wrapper(self):
        with tempfile.TemporaryDirectory() as td:
            venv_root = Path(td) / "venv"
            _write_forwarding_shim(venv_root / "bin" / "python")
            proc = subprocess.run(
                [_pwsh(), "-NoProfile", "-File", str(KG_SYNC_PS1), "--foo"],
                env=_wrapper_env(venv_root),
                capture_output=True, text=True, timeout=90,
            )
        self.assertEqual(proc.returncode, 2,
                         f"out={proc.stdout!r} err={proc.stderr!r}")
        self.assertIn("--foo", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()
