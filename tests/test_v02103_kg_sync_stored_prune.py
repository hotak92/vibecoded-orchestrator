# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — a full ``kg-sync --all`` prunes stored-only KG rows.

Owner request (2026-10-14): rows whose source ``.md`` left ``knowledge/``
stayed in Weaviate forever and kept surfacing in search. The single-file
sync removed rows for ARCHIVED nodes, and install.py's seed step plus the
bundle-update repair pruned gone-file rows — but a plain ``--all`` (the
launcher's Sync button, the doctor census's printed remedy) never did.

This file pins the new behaviour end-to-end through ``main()``:

* deleted node's rows pruned on ``--all`` (through the repo's ONE prune
  home, ``vco_lib.collection_repair.prune_foreign_rows`` — faked at its
  ``_http_request`` seam, never at the wiring);
* archived node's rows removed by the walk (the pre-existing per-node leg);
* current node's rows kept;
* shared-identity collections (kg == shared, the orchestrator root) never
  pruned — sibling projects' shared-scope rows live there;
* rows with empty / absolute / ``..`` file_path defensively kept;
* a PARTIAL walk prunes nothing (failures ⇒ no destructive action);
* an empty knowledge walk prunes nothing (mis-resolved root guard);
* ``scan_drift`` reports no stored-only for a shared-identity collection;
* the doctor census NAMES the stored-only paths and prints a true remedy
  command (a re-sync prunes them).

Red-proof: set ``KG_SYNC_SCRIPT_UNDER_TEST`` to a pre-fix copy of the sync
script (the prune tests fail — no prune call existed), or revert the
doctor / drift-scanner edits (the census and identity tests fail).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
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

SCRIPT_PATH = Path(os.environ.get(
    "KG_SYNC_SCRIPT_UNDER_TEST",
    str(REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"),
))
PROJECT_KG = "V02103Prune_KnowledgeGraph"

_ENV_KEYS = (
    "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
    "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
    "VCT_DISABLE_HUB_RESOLVER", "KG_SYNC_PROJECT_ROOT", "VCT_STATE_DIR",
)


# ─── In-memory fake Weaviate client (shape mirrors test_v0292 flags file) ─


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
    def __init__(self, **kwargs):  # noqa: ARG002
        self.client = _FakeClient()
        self.embedding_service = _FakeEmbeddingService()
        self.text_vector_slot = "qwen3_embed"

    def _get_embedding(self, text):  # noqa: ARG002
        return [0.5, 0.5, 0.5]

    def _get_all_kg_embeddings(self, text):  # noqa: ARG002
        return {self.text_vector_slot: [0.5, 0.5, 0.5]}

    def _get_all_kg_embeddings_tagged(self, text):  # noqa: ARG002
        return {self.text_vector_slot: [0.5, 0.5, 0.5]}, []

    def close(self):
        pass


class _ServerHarness:
    def __init__(self):
        self.instances: list[_CountingServer] = []

    def __call__(self, **kwargs) -> _CountingServer:
        srv = _CountingServer(**kwargs)
        self.instances.append(srv)
        return srv

    @property
    def last(self) -> _CountingServer:
        return self.instances[-1]


# ─── collection_repair HTTP seam fake (mirrors test_v0289) ────────────────


class _FakeRepairHTTP:
    """GraphQL enumerate + batch delete-by-uuid recorder."""

    def __init__(self, rows, *, graphql_errors: bool = False):
        #: [(uuid, file_path, title), ...]
        self.rows = list(rows)
        self.graphql_errors = graphql_errors
        self.deleted_uuids: list[str] = []
        self.calls: list = []

    def __call__(self, method, url, *, body=None, timeout=30.0):  # noqa: ARG002
        self.calls.append((method, url, body))
        if self.graphql_errors:
            return (200, json.dumps({"errors": [{"message": "boom"}]}).encode())
        if method == "POST" and url.endswith("/v1/graphql"):
            objs = [
                {"_additional": {"id": u}, "file_path": fp, "title": t}
                for u, fp, t in self.rows
            ]
            coll = body["query"].split("{ Get { ")[1].split("(")[0]
            return (200, json.dumps(
                {"data": {"Get": {coll: objs}}}
            ).encode())
        if method == "DELETE" and url.endswith("/v1/batch/objects"):
            chunk = body["match"]["where"]["valueTextArray"]
            self.deleted_uuids.extend(chunk)
            return (200, json.dumps(
                {"results": {"successful": len(chunk), "failed": 0}}
            ).encode())
        return (404, b"")


# ─── Test base ────────────────────────────────────────────────────────────


def _write_node(path: Path, title: str, body: str = "Body text.",
                extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\ntype: concept\nstatus: active\n{extra}"
        f"---\n{body}\n",
        encoding="utf-8",
    )


def _seed_row(server: _CountingServer, collection: str, file_path: str,
              content_hash: str = "seedhash") -> str:
    store = server.client.collections._store_for(collection)
    uid = str(uuid.uuid4())
    store[uid] = _FakeObj(uid, {
        "file_path": file_path,
        "content_hash": content_hash,
        "chunk_num": 1,
        "total_chunks": 1,
    })
    return uid


def _rows(server: _CountingServer, collection: str):
    return list(server.client.collections._store_for(collection).values())


class PruneTestBase(unittest.TestCase):
    """tmp project root + env isolation + loaded module + fake backends."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="vct-prune-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
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

    def load(self, *, shared: str = ""):
        os.environ["KG_BASE_DIR"] = str(self.root)
        os.environ["KG_COLLECTION"] = PROJECT_KG
        os.environ["SHARED_KG_COLLECTION"] = shared
        os.environ["DEVELOPMENT_COLLECTION"] = ""
        os.environ["DUAL_EMBEDDING_ENABLED"] = "false"
        os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
        os.environ.pop("KG_SYNC_PROJECT_ROOT", None)

        mod_name = f"_sync_kg_v02103_{uuid.uuid4().hex}"
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

    def install_working_backends(self, mod) -> _ServerHarness:
        harness = _ServerHarness()

        class _FakeEmbeddingServiceCls:
            @staticmethod
            def for_project(root):  # noqa: ARG003
                return _FakeEmbeddingService()

        mod.EmbeddingService = _FakeEmbeddingServiceCls
        mod.WeaviateMCPServer = harness
        mod.ensure_collection_exists = lambda srv: True  # noqa: ARG003
        mod.ensure_dev_collection_exists = lambda srv: None  # noqa: ARG003
        mod._regen_node_formats_after_full_sync = lambda: None
        return harness

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

    def _run_all_with_rows(self, repair_rows, *, shared: str = "",
                           graphql_errors: bool = False):
        """Load the module, install fake backends + the repair HTTP fake,
        run ``--all``. Returns (code, stdout, harness, fake)."""
        mod = self.load(shared=shared)
        harness = self.install_working_backends(mod)
        fake = _FakeRepairHTTP(repair_rows, graphql_errors=graphql_errors)
        import vco_lib.collection_repair as crep

        with mock.patch.object(crep, "_http_request", fake):
            code, out, err = self.run_main(mod, ["kg-sync", "--all"])
        return code, out, err, harness, fake


class ActTests(PruneTestBase):
    def test_deleted_node_pruned_current_node_kept(self):
        _write_node(self.root / "knowledge" / "concepts" / "active.md",
                    "Active")
        code, out, err, harness, fake = self._run_all_with_rows([
            ("u-active", "knowledge/concepts/active.md", "Active"),
            ("u-gone", "knowledge/concepts/gone.md", "Gone"),
        ])
        self.assertEqual(code, 0, f"clean run. stderr:\n{err}")
        self.assertIn("u-gone", fake.deleted_uuids,
                      "a row whose file is gone must be pruned on --all")
        self.assertNotIn("u-active", fake.deleted_uuids,
                         "a current node's row is NEVER touched")
        self.assertIn("removed stale row: knowledge/concepts/gone.md", out)
        self.assertIn("title: Gone", out,
                      "every removed row is logged by path AND title")
        self.assertIn("🧹 Pruned 1 stale Weaviate row(s)", out)

    def test_archived_node_rows_removed_by_the_walk(self):
        """An on-disk archived node: the per-node leg (v0.2.70 FIX #1 +
        the shared archived predicate) removes its rows during --all."""
        _write_node(self.root / "knowledge" / "concepts" / "active.md",
                    "Active")
        _write_node(self.root / "knowledge" / "archive" / "old.md", "Old")
        mod = self.load()
        self.install_working_backends(mod)
        # main() constructs its own server; hand it one PRE-SEEDED with the
        # archived node's row so the per-node delete leg has work to do.
        server = _CountingServer()
        u_arch = _seed_row(server, PROJECT_KG, "knowledge/archive/old.md")
        mod.WeaviateMCPServer = lambda **kwargs: server  # noqa: ARG005
        fake = _FakeRepairHTTP([
            ("u-active", "knowledge/concepts/active.md", "Active"),
        ])
        import vco_lib.collection_repair as crep

        with mock.patch.object(crep, "_http_request", fake):
            code, out, err = self.run_main(mod, ["kg-sync", "--all"])
        self.assertEqual(code, 0, f"skips are fine. stderr:\n{err}")
        self.assertIn("Skipping archived node", out)
        stored_paths = [
            o.properties.get("file_path") for o in _rows(server, PROJECT_KG)
        ]
        self.assertNotIn("knowledge/archive/old.md", stored_paths,
                         "the archived node's stored rows are removed")
        store = server.client.collections._store_for(PROJECT_KG)
        self.assertNotIn(u_arch, store,
                         "the seeded archived row's uuid is gone")
        self.assertNotIn("u-active", fake.deleted_uuids,
                         "the prune never touches an on-disk node's rows")

    def test_current_only_run_prunes_nothing_and_stays_quiet(self):
        _write_node(self.root / "knowledge" / "concepts" / "active.md",
                    "Active")
        code, out, err, _h, fake = self._run_all_with_rows([
            ("u-active", "knowledge/concepts/active.md", "Active"),
        ])
        self.assertEqual(code, 0, f"stderr:\n{err}")
        self.assertEqual(fake.deleted_uuids, [])
        self.assertNotIn("🧹 Pruned", out,
                         "zero deletions ⇒ no prune count line")


class GuardTests(PruneTestBase):
    def test_shared_identity_collection_never_pruned(self):
        """THE root guard: kg == shared (the orchestrator root) holds every
        sibling project's shared-scope rows — no prune may run there."""
        _write_node(self.root / "knowledge" / "concepts" / "active.md",
                    "Active")
        code, out, err, _h, fake = self._run_all_with_rows(
            [("u-sibling", "knowledge/concepts/siblings-row.md", "Sibling")],
            shared=PROJECT_KG,  # identity: same name, same case
        )
        self.assertEqual(code, 0, f"stderr:\n{err}")
        self.assertEqual(fake.deleted_uuids, [])
        self.assertIn("shared collection", out)
        self.assertIn("never pruned", out)

    def test_shared_identity_case_insensitive(self):
        _write_node(self.root / "knowledge" / "concepts" / "active.md",
                    "Active")
        code, out, _err, _h, fake = self._run_all_with_rows(
            [("u-sibling", "knowledge/concepts/siblings-row.md", "Sibling")],
            shared=PROJECT_KG.lower(),
        )
        self.assertEqual(fake.deleted_uuids, [],
                         "case-insensitive identity must skip the prune")

    def test_defensive_rows_are_kept_and_named(self):
        """Empty / absolute / ``..`` file_path rows are unresolvable to this
        knowledge root — never deleted on a guess (the sync writer never
        stores these shapes; they are legacy/foreign anomalies)."""
        _write_node(self.root / "knowledge" / "concepts" / "active.md",
                    "Active")
        code, out, err, _h, fake = self._run_all_with_rows([
            ("u-abs", "/other/project/knowledge/x.md", "Foreign"),
            ("u-empty", "", "NoPath"),
            ("u-dots", "a/../../outside.md", "Escapee"),
            ("u-active", "knowledge/concepts/active.md", "Active"),
        ])
        self.assertEqual(code, 0, f"stderr:\n{err}")
        self.assertEqual(fake.deleted_uuids, [])
        self.assertIn("3 row(s) with an empty or absolute file_path left "
                      "untouched", out)

    def test_partial_walk_prunes_nothing(self):
        """A failing node ⇒ partial walk ⇒ no destructive action (the tree's
        completeness is the prune's premise)."""
        _write_node(self.root / "knowledge" / "concepts" / "active.md",
                    "Active")
        # Invalid frontmatter `type:` fails the node loudly (v0.2.101
        # P299-A3) — one failure makes the walk partial.
        (self.root / "knowledge" / "concepts" / "broken.md").write_text(
            "---\ntitle: Broken\ntype: '!!'\nstatus: active\n---\nBody\n",
            encoding="utf-8",
        )
        code, out, err, _h, fake = self._run_all_with_rows([
            ("u-gone", "knowledge/concepts/gone.md", "Gone"),
        ])
        self.assertEqual(code, 1, "the broken node fails the run")
        self.assertIn("1 failed", out)
        self.assertEqual(fake.deleted_uuids, [],
                         "a partial walk must prune NOTHING")
        self.assertIn("walk was partial", out)

    def test_empty_knowledge_walk_prunes_nothing(self):
        """knowledge/ present but empty (0 md files): more likely a
        mis-resolved root than a deliberate bulk delete — refuse."""
        code, out, err, _h, fake = self._run_all_with_rows([
            ("u-gone", "knowledge/concepts/gone.md", "Gone"),
        ])
        self.assertEqual(code, 0, f"nothing to fail. stderr:\n{err}")
        self.assertEqual(fake.deleted_uuids, [])
        self.assertIn("considered 0 knowledge nodes", out)

    def test_enumeration_failure_prunes_nothing(self):
        """collection_repair's own guard, reached through the sync wiring:
        no authoritative row list ⇒ no delete."""
        _write_node(self.root / "knowledge" / "concepts" / "active.md",
                    "Active")
        code, out, err, _h, fake = self._run_all_with_rows(
            [("u-gone", "knowledge/concepts/gone.md", "Gone")],
            graphql_errors=True,
        )
        self.assertEqual(code, 0, f"prune is soft-fail. stderr:\n{err}")
        self.assertEqual(fake.deleted_uuids, [])
        self.assertIn("Stored-only prune skipped: enumeration failed", out)


class DriftScannerTests(unittest.TestCase):
    """scan_drift's stored-only dimension honours the shared-identity case."""

    def _scan(self, *, kg="Root_KG", shared="root_kg", hashes=None):
        from vco_lib import kg_sync_drift

        import tempfile
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            rel = folder / "knowledge" / "concepts" / "x.md"
            rel.parent.mkdir(parents=True)
            rel.write_text(
                "---\ntitle: X\ntype: concept\nstatus: active\n---\nbody\n",
                encoding="utf-8",
            )
            return kg_sync_drift.scan_drift(
                folder / "knowledge",
                weaviate_url="http://127.0.0.1:9",
                kg_collection=kg,
                shared_kg_collection=shared,
                reachable_fn=lambda url: True,
                query_hashes_fn=lambda url, coll, on_warn=None: dict(
                    hashes or {}),
            )

    def test_shared_identity_stored_only_is_empty_not_sibling_rows(self):
        report = self._scan(hashes={
            "knowledge/concepts/x.md": "anything",
            "knowledge/concepts/siblings-row.md": "deadbeef",
        })
        self.assertEqual(report.stored_only, (),
                         "sibling projects' shared rows are NOT this root's "
                         "stored-only")
        self.assertIn("not computable", report.detail)

    def test_normal_project_stored_only_still_reported(self):
        report = self._scan(kg="Proj_KG", shared="Shared_KG", hashes={
            "knowledge/concepts/gone.md": "deadbeef",
        })
        self.assertEqual(report.stored_only, ("knowledge/concepts/gone.md",))
        self.assertIn("prunes them", report.detail)


class CensusTests(unittest.TestCase):
    """The doctor census names stored-only paths + a true remedy command."""

    def test_census_lists_stored_only_paths_and_true_command(self):
        import tempfile

        from vco_lib import doctor as _doctor
        from vco_lib import kg_sync_drift
        from vco_lib.project_identity import (
            SOURCE_BINDING,
            IdentitySnapshot,
            ProjectIdentity,
        )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            folder = tmp / "Alpha-folder"
            (folder / "knowledge" / "concepts").mkdir(parents=True)
            (folder / "knowledge" / "concepts" / "x.md").write_text(
                "---\ntitle: X\ntype: concept\nstatus: active\n---\nb\n",
                encoding="utf-8",
            )
            ident = ProjectIdentity(
                name="Alpha", folder_path=str(folder),
                kg_primary="Alpha_KnowledgeGraph",
                kg_primary_source=SOURCE_BINDING,
            )
            snapshot = IdentitySnapshot(resolvable=True, projects=(ident,))
            from functools import partial

            import vco_lib.project_identity as pi
            with mock.patch.object(pi, "resolve_snapshot",
                                   return_value=snapshot):
                with mock.patch.object(
                    kg_sync_drift, "scan_drift",
                    partial(
                        kg_sync_drift.scan_drift,
                        reachable_fn=lambda url: True,
                        query_hashes_fn=lambda url, coll, on_warn=None: {
                            "knowledge/concepts/gone.md": "deadbeef",
                        },
                    ),
                ):
                    findings = _doctor.probe_kg_drift_census(
                        tmp, _doctor.DoctorResolvers(),
                        {"scope": _doctor.SCOPE_FULL},
                    )
        finding = findings[0]
        self.assertEqual(finding.status, _doctor.STATUS_PROBLEM)
        project = finding.detail["projects"][0]
        self.assertEqual(project["stored_only"], 1)
        self.assertEqual(project["stored_only_paths"],
                         ["knowledge/concepts/gone.md"],
                         "the census NAMES the rows, not just the count")
        self.assertIn("#   stored-only: knowledge/concepts/gone.md",
                      finding.command)
        self.assertIn("PRUNED by the same re-sync", finding.command,
                      "the printed command must match the new behaviour")
        self.assertNotIn("NOT removed by a re-sync", finding.command)


if __name__ == "__main__":
    unittest.main()
