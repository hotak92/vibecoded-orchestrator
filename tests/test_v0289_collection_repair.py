# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.89 §7.3 — foreign-row pruning (BUG-3 damage repair, wave-2 P2).

Act + leave-alone legs for ``vco_lib.collection_repair.prune_foreign_rows``
(+ the per-project wrapper and CLI). The load-bearing guard — the
shared-identity skip, without which BUG-6 shared-scoped nodes from other
projects would be destroyed — gets its own explicit test.

Offline throughout: Weaviate is faked at the module ``_http_request`` seam.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import collection_repair as crep  # noqa: E402
from vco_lib.collection_repair import (  # noqa: E402
    prune_foreign_rows,
    prune_foreign_rows_for_project,
)

_URL = "http://weaviate.test:8081"


class _FakeWeaviateHTTP:
    """GraphQL enumerate + batch delete-by-uuid recorder."""

    def __init__(self, rows_by_collection=None, *,
                 graphql_errors: bool = False,
                 transport_dead: bool = False):
        #: {collection: [(uuid, file_path), ...]}
        self.rows_by_collection = dict(rows_by_collection or {})
        self.graphql_errors = graphql_errors
        self.transport_dead = transport_dead
        self.calls: list = []
        self.deleted_uuids: list = []

    def __call__(self, method, url, *, body=None, timeout=30.0):
        self.calls.append((method, url, body))
        if self.transport_dead:
            raise OSError("connection refused")
        if url.endswith("/v1/.well-known/ready"):
            return (200, b"")
        if method == "POST" and url.endswith("/v1/graphql"):
            if self.graphql_errors:
                return (200, json.dumps(
                    {"errors": [{"message": "boom"}]}
                ).encode())
            # Which collection was asked for?
            assert body is not None
            query = body["query"]
            for coll, rows in self.rows_by_collection.items():
                if f" {coll}(" in query or f"{{ {coll}(" in query:
                    objs = [
                        {"_additional": {"id": u}, "file_path": fp}
                        for u, fp in rows
                    ]
                    return (200, json.dumps(
                        {"data": {"Get": {coll: objs}}}
                    ).encode())
            return (200, json.dumps({"data": {"Get": {}}}).encode())
        if method == "DELETE" and url.endswith("/v1/batch/objects"):
            assert body is not None
            chunk = body["match"]["where"]["valueTextArray"]
            self.deleted_uuids.extend(chunk)
            return (200, json.dumps(
                {"results": {"successful": len(chunk), "failed": 0}}
            ).encode())
        return (404, b"")


class PruneBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="vct-foreign-")
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / "docs").mkdir(parents=True)
        (self.project / "docs" / "real.md").write_text("real\n",
                                                       encoding="utf-8")

    def _run(self, rows, *, dry_run=False, is_shared_identity=False,
             collection="Proj_Development", graphql_errors=False):
        fake = _FakeWeaviateHTTP(
            {collection: rows}, graphql_errors=graphql_errors,
        )
        with mock.patch.object(crep, "_http_request", fake):
            result = prune_foreign_rows(
                self.project,
                collection,
                weaviate_url=_URL,
                is_shared_identity=is_shared_identity,
                dry_run=dry_run,
            )
        return result, fake


class ActTests(PruneBase):
    def test_absent_path_rows_deleted_present_kept(self):
        rows = [
            ("u-real", "docs/real.md"),      # exists → kept
            ("u-gone", "docs/gone.md"),      # absent → deleted
            ("u-gone2", "docs/gone.md"),     # same absent path, 2nd row
        ]
        result, fake = self._run(rows)
        self.assertIsNone(result["skipped"])
        self.assertEqual(sorted(fake.deleted_uuids), ["u-gone", "u-gone2"])
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["stale_paths"], ["docs/gone.md"])
        self.assertNotIn("u-real", fake.deleted_uuids,
                         "rows whose file EXISTS are NEVER touched")

    def test_all_present_zero_deletes(self):
        rows = [("u1", "docs/real.md"), ("u2", "docs/real.md")]
        result, fake = self._run(rows)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(
            [c for c in fake.calls if c[0] == "DELETE"], [],
            "no DELETE call when every row's file exists",
        )

    def test_backslash_path_pointing_at_existing_file_preserved(self):
        rows = [("u-win", "docs\\real.md")]
        result, fake = self._run(rows)
        self.assertEqual(fake.deleted_uuids, [],
                         "Windows-shaped path to an existing file is alive")

    def test_worktree_prefixed_path_preserved(self):
        rows = [("u-wt", ".claude/worktrees/agent-abc/docs/real.md")]
        result, fake = self._run(rows)
        self.assertEqual(fake.deleted_uuids, [])


class GuardTests(PruneBase):
    def test_shared_identity_skips_entirely(self):
        """THE load-bearing guard (§7.3): shared-scoped nodes from other
        projects make 'absent locally' NORMAL in a shared-identity
        collection — no enumeration, no deletion, nothing."""
        rows = [("u-foreign", "docs/other-projects-file.md")]
        result, fake = self._run(rows, is_shared_identity=True,
                                 collection="Shared_KnowledgeGraph")
        self.assertEqual(result["skipped"], "shared-identity collection")
        self.assertEqual(fake.calls, [],
                         "shared-identity: not even an enumerate call")
        self.assertEqual(result["deleted"], 0)

    def test_absolute_and_dotdot_paths_defensively_skipped(self):
        rows = [
            ("u-abs", "/etc/passwd"),
            ("u-drive", "C:\\other\\tree.md"),
            ("u-unc", "\\\\server\\share\\x.md"),
            ("u-dots", "a/../../outside.md"),
            ("u-empty", ""),
        ]
        result, fake = self._run(rows)
        self.assertEqual(fake.deleted_uuids, [],
                         "absolute/../empty paths are NEVER deleted")
        self.assertEqual(result["defensive_skipped"], 5)

    def test_enumeration_failure_no_destructive_action(self):
        rows = [("u-gone", "docs/gone.md")]
        result, fake = self._run(rows, graphql_errors=True)
        self.assertEqual(result["skipped"], "enumeration failed")
        self.assertEqual(fake.deleted_uuids, [])

    def test_dry_run_reports_without_deleting(self):
        rows = [("u-gone", "docs/gone.md")]
        result, fake = self._run(rows, dry_run=True)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(fake.deleted_uuids, [])
        self.assertEqual(result["stale_paths"], ["docs/gone.md"])

    def test_empty_collection_name_skips(self):
        result = prune_foreign_rows(self.project, "", weaviate_url=_URL)
        self.assertEqual(result["skipped"], "no collection name")


class WrapperTests(PruneBase):
    def _run_wrapper(self, rows_by_collection, *, kg="Proj_KnowledgeGraph",
                     dev="Proj_Development", shared="Shared_KG",
                     is_root_target=False, dry_run=False, reachable=True):
        fake = _FakeWeaviateHTTP(rows_by_collection)
        with mock.patch.object(crep, "_http_request", fake), \
                mock.patch.object(crep, "weaviate_reachable",
                                  lambda *_a, **_k: reachable):
            result = prune_foreign_rows_for_project(
                self.project,
                weaviate_url=_URL,
                kg_collection=kg,
                development_collection=dev,
                shared_kg_collection=shared,
                is_root_target=is_root_target,
                dry_run=dry_run,
            )
        return result, fake

    def test_kg_leg_skipped_on_shared_identity_case_insensitive(self):
        result, fake = self._run_wrapper(
            {"Shared_KG": [("u1", "docs/gone.md")],
             "Proj_Development": []},
            kg="Shared_KG", shared="shared_kg",
        )
        kg_leg = result["legs"][0]
        self.assertEqual(kg_leg["skipped"], "shared-identity collection")
        self.assertNotIn("u1", fake.deleted_uuids)

    def test_dev_leg_skipped_on_root_target(self):
        result, _ = self._run_wrapper(
            {"Proj_KnowledgeGraph": [], "Proj_Development": []},
            kg="Shared_KG", shared="Shared_KG",  # root: kg IS shared
            is_root_target=True,
        )
        legs = {leg["collection"]: leg for leg in result["legs"]}
        self.assertEqual(legs["Shared_KG"]["skipped"],
                         "shared-identity collection")
        self.assertEqual(legs["Proj_Development"]["skipped"], "root target")

    def test_wrapper_unreachable_skips_both_legs(self):
        result, fake = self._run_wrapper(
            {"Proj_KnowledgeGraph": [("u1", "docs/gone.md")]},
            reachable=False,
        )
        self.assertEqual(result["skipped"], "weaviate unreachable")
        self.assertEqual(result["legs"], [])
        self.assertEqual(fake.deleted_uuids, [])

    def test_wrapper_emits_single_deferral_with_counts(self):
        result, _ = self._run_wrapper({
            "Proj_KnowledgeGraph": [("u-k", "knowledge/gone.md")],
            "Proj_Development": [("u-d1", "docs/gone.md"),
                                 ("u-d2", "docs/gone2.md")],
        })
        self.assertEqual(result["total_deleted"], 3)
        deferred = (self.project / ".claude" / "context" /
                    "UPDATE_DEFERRED.md")
        text = deferred.read_text(encoding="utf-8")
        self.assertEqual(text.count("## foreign_rows_pruned"), 1,
                         "exactly ONE entry for both collections")
        self.assertIn("Proj_KnowledgeGraph", text)
        self.assertIn("Proj_Development", text)

    def test_wrapper_dry_run_no_deferral(self):
        result, fake = self._run_wrapper(
            {"Proj_KnowledgeGraph": [("u-k", "knowledge/gone.md")]},
            dry_run=True,
        )
        self.assertEqual(result["total_deleted"], 0)
        self.assertEqual(fake.deleted_uuids, [])
        self.assertFalse(
            (self.project / ".claude" / "context" /
             "UPDATE_DEFERRED.md").exists(),
        )


class ChunkingTests(PruneBase):
    def test_delete_batches_are_chunked(self):
        rows = [(f"u-{i}", f"docs/gone-{i}.md") for i in range(1201)]
        result, fake = self._run(rows)
        delete_calls = [c for c in fake.calls if c[0] == "DELETE"]
        self.assertEqual(len(delete_calls), 3, "1201 uuids → 3 chunks of 500")
        self.assertEqual(result["deleted"], 1201)


class CliTests(PruneBase):
    def test_cli_dry_run_json(self):
        fake = _FakeWeaviateHTTP(
            {"Proj_KnowledgeGraph": [("u-k", "knowledge/gone.md")],
             "Proj_Development": []},
        )
        ctx = {
            "weaviate_url": _URL,
            "kg_collection": "Proj_KnowledgeGraph",
            "development_collection": "Proj_Development",
            "shared_kg_collection": "Shared_KG",
            "source": "test",
        }
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(crep, "_http_request", fake), \
                mock.patch.object(crep, "weaviate_reachable",
                                  lambda *_a, **_k: True), \
                mock.patch.object(crep, "_resolve_project_context",
                                  lambda _f: dict(ctx)), \
                contextlib.redirect_stdout(buf):
            rc = crep.main([
                "--project", str(self.project), "--dry-run", "--json",
            ])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["total_deleted"], 0)
        self.assertEqual(fake.deleted_uuids, [], "dry-run deletes nothing")

    def test_cli_missing_folder_exit_2(self):
        rc = crep.main(["--project", "/nonexistent/nowhere-xyz"])
        self.assertEqual(rc, 2)


class ResolutionReasonTests(PruneBase):
    """v0.2.96 — the hub-failure reason must survive the soft-fail.

    `_resolve_project_context` swallowed the exception with `except
    Exception: pass`, so `resolution_source` could say WHICH resolver
    answered and never WHY the preferred one did not. A prune that addresses
    an unexpected collection is then unreportable: its own JSON says
    "settings/binding-first" with no way to tell a hub that was DOWN from a
    project that is simply unregistered.

    Red-proof for all three: restore `except Exception: pass` (and drop the
    two `fallback_reason` keys) — every test in this class fails.
    """

    def test_hub_failure_reason_is_carried_not_swallowed(self):
        import vco_lib.project_config as pc

        def _boom(_folder):
            raise RuntimeError("hub refused: HTTP 503 from 127.0.0.1:7700")

        with mock.patch.object(pc, "resolve", _boom):
            ctx = crep._resolve_project_context(self.project)

        self.assertEqual(ctx["source"], "settings/binding-first")
        self.assertIsNotNone(
            ctx["fallback_reason"],
            "the reason the hub path was abandoned must be reportable",
        )
        self.assertIn("HTTP 503", ctx["fallback_reason"])
        self.assertIn("RuntimeError", ctx["fallback_reason"])

    def test_hub_success_carries_no_reason(self):
        """No failure ⇒ no reason. The key is the evidence, not decoration."""
        import vco_lib.project_config as pc

        class _Cfg:
            weaviate_url = _URL
            kg_collection = "Proj_KnowledgeGraph"
            development_collection = "Proj_Development"
            shared_kg_collection = "Shared_KG"

        with mock.patch.object(pc, "resolve", lambda _f: _Cfg()):
            ctx = crep._resolve_project_context(self.project)

        self.assertEqual(ctx["source"], "hub")
        self.assertIsNone(ctx["fallback_reason"])

    def test_cli_surfaces_the_reason_on_both_output_surfaces(self):
        """JSON consumers never read stderr; humans never read the JSON."""
        import contextlib
        import io

        fake = _FakeWeaviateHTTP(
            {"Proj_KnowledgeGraph": [], "Proj_Development": []},
        )
        ctx = {
            "weaviate_url": _URL,
            "kg_collection": "Proj_KnowledgeGraph",
            "development_collection": "Proj_Development",
            "shared_kg_collection": "Shared_KG",
            "source": "settings/binding-first",
            "fallback_reason": "ConnectionRefusedError: hub not running",
        }
        patches = (
            mock.patch.object(crep, "_http_request", fake),
            mock.patch.object(crep, "weaviate_reachable", lambda *_a, **_k: True),
            mock.patch.object(crep, "_resolve_project_context", lambda _f: dict(ctx)),
        )

        out, err = io.StringIO(), io.StringIO()
        with patches[0], patches[1], patches[2], \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = crep.main(["--project", str(self.project), "--dry-run", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(
            payload["resolution_fallback_reason"],
            "ConnectionRefusedError: hub not running",
        )

        out, err = io.StringIO(), io.StringIO()
        with patches[0], patches[1], patches[2], \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = crep.main(["--project", str(self.project), "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("hub not running", err.getvalue())
        self.assertNotIn(
            "hub not running", out.getvalue(),
            "the caveat belongs on stderr, not inside the counts",
        )


class SymlinkExistenceTests(PruneBase):
    """v0.2.103 R2 (review H-1): existence is judged on the path AS THE
    SYNC STORES IT — lexically, following symlinks.

    The pre-fix check re-resolved every candidate and required the result
    INSIDE the project root, so a symlinked ``knowledge/`` (the Obsidian
    vault layout) made EVERY row read as "file gone" and a full
    ``kg-sync --all`` deleted the rows it had just written. These tests
    build real symlinks in tmp; where the platform refuses symlinks they
    skip (the lexical-existence rule itself is platform-independent).
    """

    def _symlink(self, link: Path, target: Path, *, directory: bool) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable on this platform: {exc}")

    def _run_kg(self, rows):
        fake = _FakeWeaviateHTTP({"Proj_KnowledgeGraph": rows})
        with mock.patch.object(crep, "_http_request", fake):
            result = prune_foreign_rows(
                self.project, "Proj_KnowledgeGraph", weaviate_url=_URL,
            )
        return result, fake

    def test_symlinked_knowledge_dir_rows_are_kept(self):
        """knowledge/ → an OUTSIDE vault: the row's stored path resolves
        outside the root but the file EXISTS through the link — kept."""
        vault = Path(self._tmp.name) / "vault"
        (vault / "concepts").mkdir(parents=True)
        (vault / "concepts" / "vault-node.md").write_text(
            "v\n", encoding="utf-8")
        self._symlink(self.project / "knowledge", vault, directory=True)
        _result, fake = self._run_kg([
            ("u-vault", "knowledge/concepts/vault-node.md"),
        ])
        self.assertEqual(fake.deleted_uuids, [],
                         "a row reachable through a symlinked knowledge/ is "
                         "NOT stale")

    def test_symlinked_single_node_kept(self):
        """A single symlinked node file (real knowledge/, linked leaf) whose
        target lives outside the root is kept."""
        outside = Path(self._tmp.name) / "outside-node.md"
        outside.write_text("o\n", encoding="utf-8")
        (self.project / "knowledge").mkdir()
        self._symlink(self.project / "knowledge" / "linked.md", outside,
                      directory=False)
        _result, fake = self._run_kg([
            ("u-linked", "knowledge/linked.md"),
        ])
        self.assertEqual(fake.deleted_uuids, [])

    def test_resolves_outside_with_missing_target_is_kept_defensively(self):
        """A BROKEN symlink whose chain escapes the root is unjudgeable by
        this tree's walk — kept, never read as 'gone'."""
        (self.project / "knowledge").mkdir()
        self._symlink(
            self.project / "knowledge" / "broken.md",
            Path(self._tmp.name) / "no-such-target.md",
            directory=False,
        )
        _result, fake = self._run_kg([
            ("u-broken", "knowledge/broken.md"),
        ])
        self.assertEqual(fake.deleted_uuids, [],
                         "resolves-outside ⇒ KEEP (defensive), even broken")

    def test_genuinely_gone_plain_row_still_deleted(self):
        """The act leg is unchanged: a plain missing relative path whose
        resolution stays INSIDE the root is stale and deleted."""
        (self.project / "knowledge").mkdir()
        (self.project / "knowledge" / "present.md").write_text(
            "p\n", encoding="utf-8")
        result, fake = self._run_kg([
            ("u-present", "knowledge/present.md"),
            ("u-gone", "knowledge/gone.md"),
        ])
        self.assertEqual(fake.deleted_uuids, ["u-gone"])
        self.assertEqual(result["deleted"], 1)


class SharedIdentityPredicateTests(PruneBase):
    """v0.2.103 R2 (review H-2): ONE predicate, computed INSIDE the prune
    home — name match (case-insensitive) OR the walked root is the
    orchestrator clone — so an EMPTY/unresolved shared name on the root
    can no longer let the prune run."""

    def _mark_orchestrator_clone(self, root: Path) -> None:
        (root / "vct-module.json").write_text(
            '{"id": "orchestrator"}', encoding="utf-8")

    def test_predicate_name_match_case_insensitive(self):
        self.assertTrue(crep.is_shared_identity_collection(
            "RootKG", "rootkg", self.project))
        self.assertFalse(crep.is_shared_identity_collection(
            "ProjKG", "Shared_KG", self.project))

    def test_predicate_root_clone_with_empty_shared_name(self):
        self._mark_orchestrator_clone(self.project)
        self.assertTrue(
            crep.is_shared_identity_collection("RootKG", "", self.project),
            "root ≡ shared by construction — the empty shared name must "
            "not disarm the skip",
        )
        self.assertTrue(crep.is_shared_identity_collection(
            "RootKG", None, self.project))

    def test_predicate_non_root_empty_shared_is_not_identity(self):
        self.assertFalse(crep.is_shared_identity_collection(
            "ProjKG", "", self.project))

    def test_predicate_empty_kg_is_never_identity(self):
        self._mark_orchestrator_clone(self.project)
        self.assertFalse(crep.is_shared_identity_collection(
            "", "", self.project),
            "the 'no collection name' skip owns the empty-name case")

    def test_prune_skips_before_enumerating_on_root_clone_empty_shared(self):
        """Behavioural, through the home: a root clone with an unresolved
        shared name does not even enumerate."""
        self._mark_orchestrator_clone(self.project)
        fake = _FakeWeaviateHTTP({"RootKG": [("u-gone", "docs/gone.md")]})
        with mock.patch.object(crep, "_http_request", fake):
            result = prune_foreign_rows(
                self.project, "RootKG",
                weaviate_url=_URL, shared_kg_collection="",
            )
        self.assertEqual(result["skipped"], "shared-identity collection")
        self.assertEqual(fake.calls, [])

    def test_wrapper_skips_kg_leg_on_root_clone_empty_shared(self):
        """The bundle-update caller: root clone + EMPTY shared name ⇒ the
        KG leg is skipped (pre-fix it pruned)."""
        self._mark_orchestrator_clone(self.project)
        fake = _FakeWeaviateHTTP(
            {"RootKG": [("u-gone", "docs/gone.md")],
             "Proj_Development": []},
        )
        with mock.patch.object(crep, "_http_request", fake), \
                mock.patch.object(crep, "weaviate_reachable",
                                  lambda *_a, **_k: True):
            result = prune_foreign_rows_for_project(
                self.project,
                weaviate_url=_URL,
                kg_collection="RootKG",
                development_collection="Proj_Development",
                shared_kg_collection="",
                is_root_target=True,
            )
        kg_leg = result["legs"][0]
        self.assertEqual(kg_leg["skipped"], "shared-identity collection")
        self.assertEqual(fake.deleted_uuids, [])

    def test_wrapper_non_root_still_prunes_its_own_gone_rows(self):
        """Leave-alone pin: a NON-root project with a distinct shared name
        keeps the act leg — its own gone rows are deleted."""
        fake = _FakeWeaviateHTTP(
            {"Proj_KnowledgeGraph": [("u-gone", "docs/gone.md")],
             "Proj_Development": []},
        )
        with mock.patch.object(crep, "_http_request", fake), \
                mock.patch.object(crep, "weaviate_reachable",
                                  lambda *_a, **_k: True):
            result = prune_foreign_rows_for_project(
                self.project,
                weaviate_url=_URL,
                kg_collection="Proj_KnowledgeGraph",
                development_collection="Proj_Development",
                shared_kg_collection="Shared_KG",
            )
        self.assertEqual(result["total_deleted"], 1)
        self.assertEqual(fake.deleted_uuids, ["u-gone"])

    def test_cli_skips_kg_leg_on_root_clone_empty_shared(self):
        """The CLI caller goes through the same wrapper — root markers +
        empty shared name skip the KG leg there too."""
        self._mark_orchestrator_clone(self.project)
        fake = _FakeWeaviateHTTP(
            {"RootKG": [("u-gone", "docs/gone.md")],
             "Proj_Development": []},
        )
        ctx = {
            "weaviate_url": _URL,
            "kg_collection": "RootKG",
            "development_collection": "Proj_Development",
            "shared_kg_collection": "",
            "source": "test",
        }
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(crep, "_http_request", fake), \
                mock.patch.object(crep, "weaviate_reachable",
                                  lambda *_a, **_k: True), \
                mock.patch.object(crep, "_resolve_project_context",
                                  lambda _f: dict(ctx)), \
                contextlib.redirect_stdout(buf):
            rc = crep.main(["--project", str(self.project), "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        legs = {leg["collection"]: leg for leg in payload["legs"]}
        self.assertEqual(legs["RootKG"]["skipped"],
                         "shared-identity collection")
        self.assertEqual(fake.deleted_uuids, [])


if __name__ == "__main__":
    unittest.main()


class _NoTitleWeaviateHTTP(_FakeWeaviateHTTP):
    """A class WITHOUT a ``title`` property: real Weaviate rejects any query
    that names it (measured live 2026-10-21 on the V46-C gate collection)."""

    def __call__(self, method, url, *, body=None, timeout=30.0):
        if method == "POST" and url.endswith("/v1/graphql") \
                and body is not None and " title " in body["query"]:
            self.calls.append((method, url, body))
            return (200, json.dumps({"errors": [{
                "message": 'Cannot query field "title" on type "Proj_Development".',
            }]}).encode())
        return super().__call__(method, url, body=body, timeout=timeout)


class NoTitleSchemaTests(PruneBase):
    """v0.2.104 (live V46-C gate): the v0.2.103 title join made every prune of
    a class without ``title`` skip as "enumeration failed"."""

    def test_a_class_without_title_still_prunes(self):
        fake = _NoTitleWeaviateHTTP({"Proj_Development": [
            ("u-real", "docs/real.md"), ("u-gone", "docs/gone.md")]})
        with mock.patch.object(crep, "_http_request", fake):
            result = prune_foreign_rows(
                self.project, "Proj_Development", weaviate_url=_URL,
                is_shared_identity=False, dry_run=False,
            )
        self.assertNotIn("skipped", {k: v for k, v in result.items() if v})
        self.assertEqual(fake.deleted_uuids, ["u-gone"])

    def test_any_other_graphql_error_still_skips(self):
        result, fake = self._run([("u-gone", "docs/gone.md")],
                                 graphql_errors=True)
        self.assertEqual(result.get("skipped"), "enumeration failed")
        self.assertEqual(fake.deleted_uuids, [])

    def test_a_title_error_beside_any_other_error_still_skips(self):
        """R29 NIT: the retry is for the missing-``title`` answer ALONE. The
        same error next to any other one is an enumeration failure: no retry,
        no prune — the conservative ``all()`` gate, pinned."""
        class _Mixed(_FakeWeaviateHTTP):
            def __call__(self, method, url, *, body=None, timeout=30.0):
                if method == "POST" and url.endswith("/v1/graphql"):
                    self.calls.append((method, url, body))
                    return (200, json.dumps({"errors": [
                        {"message": 'Cannot query field "title" on type "Proj_Development".'},
                        {"message": "shard Proj_Development not ready"},
                    ]}).encode())
                return super().__call__(method, url, body=body, timeout=timeout)

        fake = _Mixed({"Proj_Development": [("u-gone", "docs/gone.md")]})
        with mock.patch.object(crep, "_http_request", fake):
            result = prune_foreign_rows(
                self.project, "Proj_Development", weaviate_url=_URL,
                is_shared_identity=False, dry_run=False,
            )
        self.assertEqual(result.get("skipped"), "enumeration failed")
        self.assertEqual(fake.deleted_uuids, [])
        graphql = [c for c in fake.calls if c[1].endswith("/v1/graphql")]
        self.assertEqual(len(graphql), 1, "a mixed error list must not be retried")
