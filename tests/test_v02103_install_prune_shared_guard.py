# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — the install-time KG prune runs through the ONE guarded home.

Defect (owner-approved v0.2.103 must-fix): the prune that runs on every
``install.py --update`` (``vco_lib.install_weaviate._prune_stale_kg_rows``)
was a SECOND implementation beside ``vco_lib.collection_repair`` and lacked
its shared-identity guard. On the orchestrator root the KG collection IS the
shared collection (v0.2.81 adopt-and-route), where sibling projects'
shared-scope rows legitimately have no file under the root — the MCP writes
them with a path RELATIVE TO THE WRITING PROJECT or absolute under its tree
(``store_knowledge_node``, claude_mcp_servers/weaviate_mcp/server.py). An
unguarded root ``--update`` therefore DELETED other projects' shared
knowledge.

This file pins:

* root/shared install prune KEEPS a sibling project's shared rows — absolute
  path, relative path of another project, empty path — skipping before it
  even enumerates (both triggers of the home's ONE predicate
  ``collection_repair.is_shared_identity_collection``, v0.2.103 R2 review
  H-2: kg == shared by NAME, and the walked root carrying the
  orchestrator-clone markers — root ≡ shared by construction — which holds
  even when the shared NAME is empty/unresolved);
* a non-root project prune still DELETES its own gone rows (through
  ``collection_repair``'s guards: existing files kept, empty / absolute /
  ``..`` paths defensively kept) — a branch install.py never reaches in
  production (install.py only runs AS the orchestrator clone, so the root
  leg always fires there; review L-3), pinned here as defence in depth on a
  markerless root;
* a failed import of the prune home is soft-failed (logged), never raised
  into the installer (review L-2);
* enumeration failure ⇒ nothing deleted;
* dry-run reports without deleting;
* the ``install.py --update`` seed path calls the guarded home
  (behavioural drive through ``install._seed_weaviate`` — the real wrapper,
  the real adapter, only the Weaviate HTTP seam faked).

Red-proof: revert ``vco_lib/install_weaviate.py`` + ``install.py`` +
``vco_lib/collection_repair.py`` to the pre-fix state (cp-aside → restore) —
the shared-identity tests fail by DELETING the sibling rows through the old
unguarded path (faked here at the ``urllib.request.urlopen`` seam the old
code used), the marker-only root test fails because the pre-fix adapter
needed the (removed) ``is_orchestrator_root_install`` callable to arm the
skip, and the non-root deletion test fails because the old ``dry_run=None``
default kept every non-root run dry.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import make_launcher_db  # noqa: E402
import install  # noqa: E402
from vco_lib import collection_repair  # noqa: E402
from vco_lib import install_weaviate  # noqa: E402


# ─── Fakes ───────────────────────────────────────────────────────────────────

class _FakeRepairHTTP:
    """Fake for ``collection_repair._http_request`` (the ONE home's seam)."""

    def __init__(self, rows, *, graphql_errors: bool = False) -> None:
        self.rows = list(rows)
        self.graphql_errors = graphql_errors
        self.deleted_uuids: list[str] = []
        self.calls: list = []

    def __call__(self, method, url, *, body=None, timeout=30.0):  # noqa: ARG002
        self.calls.append((method, url, body))
        if self.graphql_errors:
            return (200, json.dumps({"errors": [{"message": "boom"}]}).encode())
        if method == "POST" and url.endswith("/v1/graphql"):
            assert body is not None  # GraphQL branch always carries a body
            objs = [
                {"_additional": {"id": u}, "file_path": fp, "title": t}
                for u, fp, t in self.rows
            ]
            coll = body["query"].split("{ Get { ")[1].split("(")[0]
            return (200, json.dumps(
                {"data": {"Get": {coll: objs}}}
            ).encode())
        if method == "DELETE" and url.endswith("/v1/batch/objects"):
            assert body is not None  # DELETE branch always carries a body
            chunk = body["match"]["where"]["valueTextArray"]
            self.deleted_uuids.extend(chunk)
            return (200, json.dumps(
                {"results": {"successful": len(chunk), "failed": 0}}
            ).encode())
        return (404, b"")


class _FakeUrlopen:
    """Fake for ``urllib.request.urlopen`` — the seam the PRE-fix prune used.

    Exists so the shared-identity tests RED-prove against the old code: the
    old implementation fetched its rows and issued its deletes here.
    """

    def __init__(self, rows) -> None:
        self.rows = list(rows)
        self.graphql_calls = 0
        self.deleted_uuids: list[str] = []

    def __call__(self, req, timeout=None):  # noqa: ANN001
        url = req.full_url
        if url.endswith("/v1/graphql"):
            self.graphql_calls += 1
            payload = json.loads(req.data.decode("utf-8"))
            coll = payload["query"].split("{ Get { ")[1].split("(")[0]
            objs = [
                {"_additional": {"id": u}, "file_path": fp}
                for u, fp, _t in self.rows
            ]
            body = {"data": {"Get": {coll: objs}}}
            return _FakeUrlopen._Resp(json.dumps(body).encode())
        if url.endswith("/v1/batch/objects") and req.get_method() == "DELETE":
            payload = json.loads(req.data.decode("utf-8"))
            chunk = payload["match"]["where"]["valueTextArray"]
            self.deleted_uuids.extend(chunk)
            body = {"results": {"successful": len(chunk), "failed": 0}}
            return _FakeUrlopen._Resp(json.dumps(body).encode())
        return _FakeUrlopen._Resp(b"{}")

    class _Resp:
        status = 200

        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False


#: A sibling project's shared-scope rows as they can appear in the shared
#: collection: an ABSOLUTE path under the sibling's tree, a RELATIVE path
#: that only resolves under the SIBLING's root, and (legacy writers) EMPTY.
SIBLING_ROWS = [
    ("uuid-abs", "/home/sibling-project/knowledge/concepts/their-node.md",
     "Their Node"),
    ("uuid-rel", "knowledge/concepts/sibling-only-node.md", "Sibling Only"),
    ("uuid-empty", "", "No Path"),
    # …plus the ROOT's own genuinely-gone row, which must ALSO be kept on a
    # shared identity (not provably the root's — see the module docstring).
    ("uuid-own-gone", "knowledge/concepts/root-gone-node.md", "Root Gone"),
]


def _sibling_row_uuids() -> list[str]:
    return [u for u, _fp, _t in SIBLING_ROWS]


# ─── Adapter-level tests (vco_lib.install_weaviate._prune_stale_kg_rows) ─────

class InstallPruneSharedIdentityGuardTest(unittest.TestCase):
    """The shared-identity skip — the load-bearing v0.2.103 guard."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        (self.root / "knowledge" / "concepts").mkdir(parents=True)
        (self.root / "knowledge" / "concepts" / "mine.md").write_text("x")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _mark_orchestrator_clone(self) -> None:
        """Give the tmp root the orchestrator-clone identity markers
        (``vco_lib.orchestrator_identity.is_orchestrator_clone``: the
        manifest parses to ``id == "orchestrator"``)."""
        (self.root / "vct-module.json").write_text(
            '{"id": "orchestrator"}', encoding="utf-8",
        )

    def _drive(self, *, shared_kg_collection, is_root):
        if is_root:
            self._mark_orchestrator_clone()
        seam = _FakeRepairHTTP(SIBLING_ROWS)
        urlopen = _FakeUrlopen(SIBLING_ROWS)
        with mock.patch.object(collection_repair, "_http_request", seam), \
             mock.patch.object(urllib.request, "urlopen", urlopen), \
             mock.patch.object(install, "_log_install_event"):
            install_weaviate._prune_stale_kg_rows(
                "RootKG", "http://localhost:8081",
                dry_run=None,  # the install.py default: would DELETE pre-fix
                project_root=self.root,
                log_event=install._log_install_event,
                shared_kg_collection=shared_kg_collection,
            )
        return seam, urlopen

    def test_shared_identity_by_name_keeps_sibling_rows(self) -> None:
        """kg == shared by NAME (case-insensitive) ⇒ skip before enumeration.

        The name comparison must arm the guard on its own — this is the same
        gate ``kg-sync --all`` and the bundle update use, so a non-root
        project whose KG identity IS the shared collection is protected too.
        """
        seam, urlopen = self._drive(
            shared_kg_collection="rootkg", is_root=False,
        )
        self.assertEqual(seam.calls, [], "shared identity must skip the leg")
        self.assertEqual(urlopen.graphql_calls, 0,
                         "shared identity must not even enumerate")
        self.assertEqual(urlopen.deleted_uuids, [])
        self.assertEqual(seam.deleted_uuids, [])

    def test_root_install_marker_alone_keeps_sibling_rows(self) -> None:
        """Root clone ⇒ shared identity by CONSTRUCTION (v0.2.81 root ≡
        shared), even when the shared name is unknown or has drifted.

        Called WITHOUT the ``shared_kg_collection`` kwarg on purpose: the
        pre-fix adapter needed its (removed) ``is_orchestrator_root_install``
        callable to arm the skip, and the pre-0.2.103 root leg DELETED
        (``dry_run=None`` → root ⇒ live delete) — this is the red-proof
        shape for the data-destroying path, now armed by the clone markers
        through the home's ONE predicate.
        """
        self._mark_orchestrator_clone()
        seam = _FakeRepairHTTP(SIBLING_ROWS)
        urlopen = _FakeUrlopen(SIBLING_ROWS)
        with mock.patch.object(collection_repair, "_http_request", seam), \
             mock.patch.object(urllib.request, "urlopen", urlopen), \
             mock.patch.object(install, "_log_install_event"):
            install_weaviate._prune_stale_kg_rows(
                "RootKG", "http://localhost:8081",
                dry_run=None,  # pre-fix root default: LIVE DELETE
                project_root=self.root,
                log_event=install._log_install_event,
            )
        self.assertEqual(seam.calls, [])
        self.assertEqual(urlopen.graphql_calls, 0,
                         "a root install must not even enumerate")
        self.assertEqual(urlopen.deleted_uuids, [],
                         "a root install must never delete shared rows")
        self.assertEqual(seam.deleted_uuids, [])

    def test_root_clone_with_empty_shared_name_never_prunes(self) -> None:
        """Review H-2's exact gap: the shared name is EMPTY (hub down, no
        launcher.db row, free tier) and the root must STILL skip — the
        name-only pre-fix predicate let this through and deleted."""
        seam, urlopen = self._drive(shared_kg_collection="", is_root=True)
        self.assertEqual(seam.calls, [],
                         "empty shared name on the root must still skip "
                         "before enumeration")
        self.assertEqual(urlopen.deleted_uuids, [])
        self.assertEqual(seam.deleted_uuids, [])

    def test_explicit_dry_run_still_skips_on_shared_identity(self) -> None:
        """dry_run=True on a shared identity: skipped (not even a report)."""
        seam, urlopen = self._drive(
            shared_kg_collection="RootKG", is_root=False,
        )
        self.assertEqual(seam.calls, [])
        self.assertEqual(urlopen.graphql_calls, 0)


class InstallPruneNonRootTest(unittest.TestCase):
    """A non-root project prune DELETES its own gone rows, guarded."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        (self.root / "knowledge" / "concepts").mkdir(parents=True)
        (self.root / "knowledge" / "concepts" / "mine.md").write_text("x")
        self._orig_pending = install._PENDING_EVENTS[:]
        install._PENDING_EVENTS.clear()

    def tearDown(self) -> None:
        install._PENDING_EVENTS.clear()
        install._PENDING_EVENTS.extend(self._orig_pending)
        self._tmpdir.cleanup()

    def _drive(self, rows, *, dry_run=None, graphql_errors=False):
        seam = _FakeRepairHTTP(rows, graphql_errors=graphql_errors)
        with mock.patch.object(collection_repair, "_http_request", seam), \
             mock.patch.object(install, "_log_install_event"):
            install_weaviate._prune_stale_kg_rows(
                "ProjKG", "http://localhost:8081",
                dry_run=dry_run,
                project_root=self.root,
                log_event=install._log_install_event,
                shared_kg_collection="SomeSharedStore",
            )
        return seam

    def test_collection_repair_import_error_is_soft_failed_not_raised(self):
        """Review L-2: the docstring promises "soft-fail throughout … never
        raises", so a failed import of the prune home (broken install) is
        LOGGED as a warn, never raised into the installer."""
        with mock.patch.dict(sys.modules, {"vco_lib.collection_repair": None}), \
             mock.patch.object(install, "_log_install_event") as log_mock:
            install_weaviate._prune_stale_kg_rows(
                "ProjKG", "http://localhost:8081",
                dry_run=None,
                project_root=self.root,
                log_event=install._log_install_event,
                shared_kg_collection="SomeSharedStore",
            )
        warns = [
            c for c in log_mock.call_args_list
            if len(c.args) >= 2 and c.args[1] == "warn"
        ]
        self.assertTrue(
            warns,
            "a failed prune-home import must surface as a WARN log, "
            f"not a raise; calls: {log_mock.call_args_list!r}",
        )
        self.assertIn("ProjKG", warns[0].args[2])

    def test_non_root_deletes_own_gone_rows_and_keeps_the_rest(self) -> None:
        """The install-time prune deletes exactly this project's gone rows.

        Kept: the row whose file exists on disk; a sibling's ABSOLUTE path
        (defensive — unresolvable to THIS root is not staleness evidence);
        an EMPTY path; a ``..``-bearing path.
        """
        rows = [
            ("uuid-kept", "knowledge/concepts/mine.md", "Mine"),
            ("uuid-gone", "knowledge/concepts/gone.md", "Gone"),
            ("uuid-abs", "/home/sibling/knowledge/their.md", "Theirs"),
            ("uuid-empty", "", "No Path"),
            ("uuid-dotdot", "knowledge/../../escape.md", "Escape"),
        ]
        seam = self._drive(rows, dry_run=None)  # install.py default → DELETE
        self.assertEqual(seam.deleted_uuids, ["uuid-gone"])

    def test_enumeration_failure_deletes_nothing(self) -> None:
        seam = self._drive(
            [("uuid-a", "knowledge/concepts/gone.md", "G")],
            dry_run=None, graphql_errors=True,
        )
        self.assertEqual(seam.deleted_uuids, [])

    def test_dry_run_reports_without_deleting(self) -> None:
        seam = self._drive(
            [("uuid-gone", "knowledge/concepts/gone.md", "Gone")],
            dry_run=True,
        )
        self.assertEqual(
            seam.deleted_uuids, [],
            "dry-run must not issue any batch delete",
        )
        self.assertEqual(len(seam.calls), 1,
                         "dry-run still enumerates (reports) once")


# ─── Behavioural drive through install.py's --update seed path ───────────────

class _SeedDrive:
    """Shared harness for driving ``install._seed_weaviate`` (from
    test_v0244_prune_runs_on_partial_sync, minus the prune mock — the real
    wrapper + adapter run; only the Weaviate HTTP seam is faked)."""

    def __init__(self, tmp: Path, *, kg: str, shared: str):
        os.environ["VCT_STATE_DIR"] = str(tmp)
        os.environ["ACTIVE_EMBEDDING"] = "qwen3"
        os.environ["KG_COLLECTION"] = kg
        os.environ["SHARED_KG_COLLECTION"] = shared
        self.db_path = make_launcher_db(
            tmp / "launcher.db",
            app_state={
                "last_installed_active_embedding": "qwen3",
                "last_installed_kg_collection": kg,
                "last_installed_shared_kg_collection": shared,
            },
        )
        # Materialise the .venv + sync_knowledge_graph.py stubs PROJECT_ROOT.
        scripts_dir = tmp / ".claude" / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "sync_knowledge_graph.py").write_text("# stub\n")
        venv_bin = tmp / ".venv" / "bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        fake_python = venv_bin / "python"
        fake_python.write_text("#!/bin/sh\n")
        fake_python.chmod(0o755)

    def close(self) -> None:
        for k in ("VCT_STATE_DIR", "ACTIVE_EMBEDDING",
                  "KG_COLLECTION", "SHARED_KG_COLLECTION"):
            os.environ.pop(k, None)


class InstallUpdateSeedPathPruneTest(unittest.TestCase):
    """``install.py --update`` reaches the ONE guarded prune home."""

    def setUp(self) -> None:
        self._gate = mock.patch.object(
            install, "_wait_for_weaviate_ready", lambda *a, **k: True
        )
        self._gate.start()
        self.addCleanup(self._gate.stop)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        (self.tmp / "knowledge" / "concepts").mkdir(parents=True)
        (self.tmp / "knowledge" / "concepts" / "foo.md").write_text("f")
        (self.tmp / "knowledge" / "concepts" / "bar.md").write_text("b")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _run_update(self, drive: _SeedDrive, seam, urlopen):
        on_disk = {
            f"{self.tmp}/knowledge/concepts/foo.md": "diskhash_foo",
            f"{self.tmp}/knowledge/concepts/bar.md": "diskhash_bar",
        }
        stored = {
            f"{self.tmp}/knowledge/concepts/foo.md": "diskhash_foo",
            f"{self.tmp}/knowledge/concepts/bar.md": "OLD_hash_bar",
        }

        def _fake_run(cmd, **kwargs):  # noqa: ARG001
            class _R:
                returncode = 0
            return _R()

        args = argparse.Namespace(update=True, skip_seed=False)
        with mock.patch.object(install, "PROJECT_ROOT", self.tmp), \
             mock.patch.object(install, "_discover_app_state_db_path",
                               return_value=drive.db_path), \
             mock.patch.object(install, "_compute_on_disk_content_hashes",
                               return_value=on_disk), \
             mock.patch.object(install, "_batch_query_weaviate_content_hashes",
                               return_value=stored), \
             mock.patch.object(collection_repair, "_http_request", seam), \
             mock.patch.object(urllib.request, "urlopen", urlopen), \
             mock.patch("subprocess.run", side_effect=_fake_run), \
             mock.patch.object(install, "run_child_logged",
                               side_effect=_fake_run), \
             mock.patch(
                 # The whole-tree seed leaves via this detached spawn: never
                 # start a real retry driver from a test (it outlived the
                 # run — v0.2.103 final review I-3's ResourceWarnings).
                 "vco_lib.deferral_retry.spawn_detached", return_value=True):
            install._seed_weaviate(args)

    def test_update_on_root_shared_identity_prunes_nothing(self) -> None:
        """KG == SHARED (the orchestrator root) ⇒ the --update seed path
        issues NO prune enumeration and NO delete — sibling projects' shared
        rows are kept.

        The tmp root also carries the orchestrator-clone markers
        (vct-module.json with id "orchestrator" / install.py / vco_lib/) so
        the PRE-fix code — whose root leg ran with ``dry_run=False`` —
        live-deletes through the urlopen seam: this is the red-proof shape
        for the --update path.
        """
        (self.tmp / "vct-module.json").write_text('{"id": "orchestrator"}')
        (self.tmp / "install.py").write_text("# stub\n")
        (self.tmp / "vco_lib").mkdir(exist_ok=True)
        drive = _SeedDrive(self.tmp, kg="RootKG", shared="RootKG")
        self.addCleanup(drive.close)
        seam = _FakeRepairHTTP(SIBLING_ROWS)
        urlopen = _FakeUrlopen(SIBLING_ROWS)
        self._run_update(drive, seam, urlopen)
        self.assertEqual(
            seam.calls, [],
            "the shared-identity KG leg must be skipped by the --update path",
        )
        self.assertEqual(
            urlopen.deleted_uuids, [],
            "no batch delete may be issued for a shared-identity collection",
        )

    def test_update_on_root_with_empty_shared_name_prunes_nothing(self) -> None:
        """Review H-2 through the real --update path: the shared name is
        EMPTY (unresolved) and the clone markers alone must skip the prune."""
        (self.tmp / "vct-module.json").write_text('{"id": "orchestrator"}')
        (self.tmp / "install.py").write_text("# stub\n")
        (self.tmp / "vco_lib").mkdir(exist_ok=True)
        drive = _SeedDrive(self.tmp, kg="RootKG", shared="")
        self.addCleanup(drive.close)
        seam = _FakeRepairHTTP(SIBLING_ROWS)
        urlopen = _FakeUrlopen(SIBLING_ROWS)
        self._run_update(drive, seam, urlopen)
        self.assertEqual(
            seam.calls, [],
            "an EMPTY shared name on the orchestrator root must still skip "
            "the prune (root ≡ shared by construction)",
        )
        self.assertEqual(urlopen.deleted_uuids, [])

    def test_update_on_non_root_deletes_gone_row_via_guarded_home(self) -> None:
        """A non-root --update prunes its own gone rows THROUGH the guarded
        home (the collection_repair HTTP seam), keeping existing files."""
        drive = _SeedDrive(self.tmp, kg="ProjKG", shared="")
        self.addCleanup(drive.close)
        rows = [
            ("uuid-kept", "knowledge/concepts/foo.md", "Foo"),
            ("uuid-gone", "knowledge/concepts/gone.md", "Gone"),
            ("uuid-sibling-abs", "/home/sibling/knowledge/their.md", "Theirs"),
        ]
        seam = _FakeRepairHTTP(rows)
        urlopen = _FakeUrlopen(rows)
        self._run_update(drive, seam, urlopen)
        self.assertEqual(
            seam.deleted_uuids, ["uuid-gone"],
            "the --update path must delete exactly the project's gone row "
            "through the guarded home",
        )


if __name__ == "__main__":
    unittest.main()
