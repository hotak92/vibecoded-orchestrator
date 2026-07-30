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


if __name__ == "__main__":
    unittest.main()
