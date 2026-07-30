# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.89 §7.2 — bundled-knowledge residue cleanup (Fabio wave-2 P2).

Act + leave-alone legs for ``vco_lib.knowledge_residue.
cleanup_bundled_knowledge_residue`` (destructive-action rule: every gate
gets its leave-alone test), plus the ``install_project_bundle`` wiring
(non-root only, soft-fail, dry-run passthrough).

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

from vco_lib import knowledge_residue as kres  # noqa: E402
from vco_lib import project_init  # noqa: E402
from vco_lib.knowledge_residue import (  # noqa: E402
    cleanup_bundled_knowledge_residue,
    content_signature_excluding_updated,
    load_curated_registry,
)

_URL = "http://weaviate.test:8081"
_KG = "ResidueProj_KnowledgeGraph"


def _node(title: str, body: str, updated: str = "2026-01-01T00:00:00Z") -> str:
    return (
        f"---\ntitle: {title}\ntype: concept\nupdated: {updated}\n"
        f"status: active\n---\n{body}\n"
    )


def _write_registry(path: Path, files: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "generated_at": "2026-07-30T00:00:00Z",
        "files": files,
    }), encoding="utf-8")


class _FakeWeaviateHTTP:
    """Records every call; simulates ready-probe + batch delete-by-filter."""

    def __init__(self, project_root: Path, *, ready: bool = True,
                 rows_by_path=None, fail_delete_paths=(),
                 transport_fail_paths=()):
        self.project_root = project_root
        self.ready = ready
        self.rows_by_path = dict(rows_by_path or {})
        self.fail_delete_paths = set(fail_delete_paths)
        self.transport_fail_paths = set(transport_fail_paths)
        self.calls: list = []
        #: fp → whether the on-disk file still existed when the row delete
        #: for that fp arrived (pins the rows-FIRST ordering).
        self.file_exists_at_delete: dict = {}

    def __call__(self, method, url, *, body=None, timeout=30.0):
        self.calls.append((method, url, body))
        if url.endswith("/v1/.well-known/ready"):
            if not self.ready:
                raise OSError("connection refused")
            return (200, b"")
        if method == "DELETE" and url.endswith("/v1/batch/objects"):
            fp = body["match"]["where"]["valueText"]
            norm = fp.replace("\\", "/")
            self.file_exists_at_delete[fp] = (
                self.project_root / norm
            ).exists()
            if fp in self.transport_fail_paths:
                raise OSError("connection reset mid-run")
            if fp in self.fail_delete_paths:
                return (500, b'{"error": "boom"}')
            n = self.rows_by_path.pop(fp, 0)
            return (200, json.dumps(
                {"results": {"successful": n, "failed": 0}}
            ).encode())
        return (404, b"")

    @property
    def delete_paths(self) -> list:
        return [
            c[2]["match"]["where"]["valueText"]
            for c in self.calls
            if c[0] == "DELETE"
        ]


class ResidueCleanupBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="vct-residue-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = self.root / "proj"
        self.knowledge = self.project / "knowledge"
        (self.knowledge / "concepts").mkdir(parents=True)
        self.registry_path = self.root / "orch" / "templates" / "knowledge" / \
            ".curated_hashes.json"

        self.foo_content = _node("Foo", "The foo pattern.")
        self.foo_sig = content_signature_excluding_updated(self.foo_content)
        (self.knowledge / "concepts" / "foo.md").write_text(
            self.foo_content, encoding="utf-8",
        )
        _write_registry(self.registry_path, {
            "concepts/foo.md": [self.foo_sig],
        })

    def _run(self, *, dry_run=False, fake=None, kg=_KG,
             is_root_target=False, shared_read_disabled=False,
             registry_path=None):
        fake = fake or _FakeWeaviateHTTP(
            self.project,
            rows_by_path={"knowledge/concepts/foo.md": 2},
        )
        with mock.patch.object(kres, "_http_request", fake):
            result = cleanup_bundled_knowledge_residue(
                self.project,
                _URL,
                kg,
                dry_run=dry_run,
                is_root_target=is_root_target,
                shared_read_disabled=shared_read_disabled,
                registry_path=registry_path or self.registry_path,
            )
        return result, fake

    def _deferred_text(self) -> str:
        p = self.project / ".claude" / "context" / "UPDATE_DEFERRED.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""


class ActTests(ResidueCleanupBase):
    def test_act_removes_rows_then_file(self):
        result, fake = self._run()
        self.assertIsNone(result["skipped"])
        self.assertEqual(result["removed"], ["concepts/foo.md"])
        self.assertFalse((self.knowledge / "concepts" / "foo.md").exists())
        # Rows deleted for BOTH separator shapes (v0.2.81 B1 lesson).
        self.assertIn("knowledge/concepts/foo.md", fake.delete_paths)
        self.assertIn("knowledge\\concepts\\foo.md", fake.delete_paths)
        # Ordering rule: the file still existed when the row delete ran.
        self.assertTrue(
            fake.file_exists_at_delete["knowledge/concepts/foo.md"],
            "rows must be deleted BEFORE the file (orphan-embedding "
            "asymmetry)",
        )
        self.assertEqual(result["rows_deleted"], 2)
        # One residue_cleanup_removed info deferral entry.
        text = self._deferred_text()
        self.assertIn("residue_cleanup_removed", text)
        self.assertIn("concepts/foo.md", text)
        self.assertIn("shared", text.lower())

    def test_updated_only_drift_still_removed(self):
        """Signature tolerance leg: an `updated:`-only drift STILL matches."""
        drifted = _node("Foo", "The foo pattern.",
                        updated="2026-07-29T23:59:59Z")
        self.assertNotEqual(drifted, self.foo_content)
        (self.knowledge / "concepts" / "foo.md").write_text(
            drifted, encoding="utf-8",
        )
        result, _ = self._run()
        self.assertEqual(result["removed"], ["concepts/foo.md"])
        self.assertFalse((self.knowledge / "concepts" / "foo.md").exists())

    def test_backslash_registry_keys_match(self):
        """Backslash-shaped registry keys normalize and match on POSIX."""
        _write_registry(self.registry_path, {
            "concepts\\foo.md": [self.foo_sig],
        })
        result, _ = self._run()
        self.assertEqual(result["removed"], ["concepts/foo.md"])

    def test_empty_curated_subdir_pruned_user_dirs_kept(self):
        userdir = self.knowledge / "mystuff"
        userdir.mkdir()
        (userdir / "note.md").write_text("mine\n", encoding="utf-8")
        result, _ = self._run()
        self.assertEqual(result["removed"], ["concepts/foo.md"])
        self.assertFalse((self.knowledge / "concepts").exists(),
                         "emptied curated subdir must be pruned")
        self.assertTrue(userdir.exists(), "user dirs are never pruned")
        self.assertTrue(self.knowledge.exists(),
                        "knowledge/ itself is never pruned")

    def test_no_candidates_resolves_pending(self):
        from vco_lib import deferral_emit
        from vco_lib.deferral_report import DeferralEntry
        deferral_emit.emit(self.project, DeferralEntry(
            condition_id=kres.CID_PENDING,
            title="t", detected="d", why_deferred="w",
            command_to_apply="c", severity="info",
        ))
        self.assertIn("residue_cleanup_pending", self._deferred_text())
        # Make the on-disk file NOT match the registry → zero candidates.
        (self.knowledge / "concepts" / "foo.md").write_text(
            _node("Foo", "user-edited body"), encoding="utf-8",
        )
        result, fake = self._run()
        self.assertEqual(result["candidates"], [])
        self.assertNotIn("residue_cleanup_pending", self._deferred_text())


class LeaveAloneTests(ResidueCleanupBase):
    def test_modified_body_left_alone(self):
        (self.knowledge / "concepts" / "foo.md").write_text(
            _node("Foo", "The foo pattern. USER EDIT."), encoding="utf-8",
        )
        result, fake = self._run()
        self.assertEqual(result["removed"], [])
        self.assertTrue((self.knowledge / "concepts" / "foo.md").exists())
        self.assertEqual(fake.delete_paths, [])

    def test_relocated_path_left_alone(self):
        """Byte-identical copy at a DIFFERENT rel path = user-curated
        placement (rel-path + signature must BOTH match)."""
        (self.knowledge / "concepts" / "sub").mkdir()
        relocated = self.knowledge / "concepts" / "sub" / "foo.md"
        (self.knowledge / "concepts" / "foo.md").rename(relocated)
        result, fake = self._run()
        self.assertEqual(result["removed"], [])
        self.assertTrue(relocated.exists())
        self.assertEqual(fake.delete_paths, [])

    def test_root_target_skips(self):
        result, fake = self._run(is_root_target=True)
        self.assertEqual(result["skipped"], "root target")
        self.assertTrue((self.knowledge / "concepts" / "foo.md").exists())
        self.assertEqual(fake.calls, [], "no network on a gate miss")

    def test_shared_read_disabled_skips(self):
        result, fake = self._run(shared_read_disabled=True)
        self.assertEqual(result["skipped"], "shared-read disabled")
        self.assertTrue((self.knowledge / "concepts" / "foo.md").exists())
        self.assertEqual(fake.calls, [])

    def test_shared_read_disabled_resolved_from_settings(self):
        settings = self.project / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(
            {"env": {"SHARED_KG_READ_DISABLED": "true"}}
        ), encoding="utf-8")
        fake = _FakeWeaviateHTTP(self.project)
        with mock.patch.object(kres, "_http_request", fake):
            result = cleanup_bundled_knowledge_residue(
                self.project, _URL, _KG,
                is_root_target=False,
                shared_read_disabled=None,  # ← must resolve from settings
                registry_path=self.registry_path,
            )
        self.assertEqual(result["skipped"], "shared-read disabled")

    def test_registry_missing_skips(self):
        result, fake = self._run(
            registry_path=self.root / "nope" / ".curated_hashes.json",
        )
        self.assertEqual(result["skipped"], "registry missing or unparseable")
        self.assertTrue((self.knowledge / "concepts" / "foo.md").exists())
        self.assertEqual(fake.calls, [])

    def test_registry_unparseable_skips(self):
        self.registry_path.write_text("{not json", encoding="utf-8")
        result, _ = self._run()
        self.assertEqual(result["skipped"], "registry missing or unparseable")

    def test_no_kg_collection_skips(self):
        result, fake = self._run(kg="")
        self.assertEqual(result["skipped"], "no kg_collection resolved")
        self.assertTrue((self.knowledge / "concepts" / "foo.md").exists())
        self.assertEqual(fake.calls, [])

    def test_weaviate_down_files_intact_pending_deferral(self):
        fake = _FakeWeaviateHTTP(self.project, ready=False)
        result, fake = self._run(fake=fake)
        self.assertTrue(result["pending"])
        self.assertEqual(result["removed"], [])
        self.assertTrue((self.knowledge / "concepts" / "foo.md").exists())
        self.assertEqual(fake.delete_paths, [], "no deletion when down")
        text = self._deferred_text()
        self.assertIn("residue_cleanup_pending", text)

    def test_allowlisted_top_level_untouched(self):
        tag_content = _node("Tags", "tag tree")
        (self.knowledge / "TAG_HIERARCHY.md").write_text(
            tag_content, encoding="utf-8",
        )
        _write_registry(self.registry_path, {
            "concepts/foo.md": [self.foo_sig],
            "TAG_HIERARCHY.md":
                [content_signature_excluding_updated(tag_content)],
        })
        result, _ = self._run()
        self.assertEqual(result["removed"], ["concepts/foo.md"])
        self.assertTrue(
            (self.knowledge / "TAG_HIERARCHY.md").exists(),
            "allowlisted per-project files must NEVER be residue-cleaned",
        )

    def test_dry_run_touches_nothing(self):
        result, fake = self._run(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["candidates"], ["concepts/foo.md"])
        self.assertEqual(result["removed"], [])
        self.assertTrue((self.knowledge / "concepts" / "foo.md").exists())
        self.assertEqual(fake.delete_paths, [])
        self.assertEqual(self._deferred_text(), "", "dry-run writes nothing")

    def test_row_delete_http_error_keeps_file(self):
        fake = _FakeWeaviateHTTP(
            self.project,
            fail_delete_paths={"knowledge/concepts/foo.md"},
        )
        result, fake = self._run(fake=fake)
        self.assertEqual(result["removed"], [])
        self.assertTrue(
            (self.knowledge / "concepts" / "foo.md").exists(),
            "file must survive when its rows could not be deleted first",
        )
        self.assertTrue(result["errors"])

    def test_transport_lost_mid_run_partial_plus_pending(self):
        bar_content = _node("Bar", "The bar pattern.")
        bar_sig = content_signature_excluding_updated(bar_content)
        (self.knowledge / "concepts" / "bar.md").write_text(
            bar_content, encoding="utf-8",
        )
        _write_registry(self.registry_path, {
            "concepts/bar.md": [bar_sig],
            "concepts/foo.md": [self.foo_sig],
        })
        # bar processes first (sorted) and succeeds; foo's delete loses
        # the transport.
        fake = _FakeWeaviateHTTP(
            self.project,
            rows_by_path={"knowledge/concepts/bar.md": 1},
            transport_fail_paths={"knowledge/concepts/foo.md"},
        )
        result, fake = self._run(fake=fake)
        self.assertEqual(result["removed"], ["concepts/bar.md"])
        self.assertTrue(result["pending"])
        self.assertTrue(
            (self.knowledge / "concepts" / "foo.md").exists(),
            "candidate after transport loss must stay on disk",
        )
        text = self._deferred_text()
        self.assertIn("residue_cleanup_removed", text)
        self.assertIn("residue_cleanup_pending", text)


class RegistryLoaderTests(unittest.TestCase):
    def test_loader_normalizes_backslash_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".curated_hashes.json"
            _write_registry(p, {"concepts\\a.md": ["s1"],
                                "concepts/a.md": ["s2"]})
            reg = load_curated_registry(p)
        self.assertEqual(reg, {"concepts/a.md": {"s1", "s2"}})

    def test_loader_rejects_wrong_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".curated_hashes.json"
            p.write_text(json.dumps({"schema_version": 2, "files": {}}),
                         encoding="utf-8")
            self.assertIsNone(load_curated_registry(p))


# ---------------------------------------------------------------------------
# install_project_bundle wiring
# ---------------------------------------------------------------------------

def _make_fake_orchestrator(root: Path) -> None:
    """Minimal orchestrator tree — enough for install_project_bundle."""
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    hooks = root / "templates" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "foo.sh").write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
    (hooks / "foo.ps1").write_text("Write-Host 'v1'\n", encoding="utf-8")
    scripts = root / "templates" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "notify.py").write_text("def n(): pass\n", encoding="utf-8")


class BundleWiringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="vct-residue-wire-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.orch = self.root / "orch"
        self.orch.mkdir()
        _make_fake_orchestrator(self.orch)
        self.project = self.root / "userproj"
        self.project.mkdir()

    def _pin_identity(self):
        """Write the settings-env KG_COLLECTION pin that arms the step
        (a positively-pinned collection identity — see the wiring's
        conservative-defaults gate)."""
        settings = self.project / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({
            "env": {"KG_COLLECTION": "WireProj_KnowledgeGraph"},
        }), encoding="utf-8")

    def _patched(self):
        from vco_lib import collection_repair as crep
        cleanup_rec = mock.MagicMock(return_value={"skipped": None,
                                                   "removed": []})
        prune_rec = mock.MagicMock(return_value={"skipped": None,
                                                 "total_deleted": 0,
                                                 "legs": []})
        p1 = mock.patch.object(
            kres, "cleanup_bundled_knowledge_residue", cleanup_rec,
        )
        p2 = mock.patch.object(
            crep, "prune_foreign_rows_for_project", prune_rec,
        )
        return p1, p2, cleanup_rec, prune_rec

    def test_wiring_runs_for_non_root_target(self):
        self._pin_identity()
        p1, p2, cleanup_rec, prune_rec = self._patched()
        with p1, p2:
            result = project_init.install_project_bundle(
                self.project, self.orch,
            )
        self.assertEqual(cleanup_rec.call_count, 1)
        self.assertEqual(prune_rec.call_count, 1)
        args, kwargs = cleanup_rec.call_args
        self.assertEqual(Path(args[0]), self.project.resolve())
        self.assertEqual(args[2], "WireProj_KnowledgeGraph",
                         "kg_collection must come from the pinned identity")
        self.assertIs(kwargs["is_root_target"], False)
        self.assertIn("residue_cleanup", result)
        self.assertIn("foreign_rows", result)

    def test_wiring_skipped_when_identity_not_pinned(self):
        """No launcher binding + no settings-env KG_COLLECTION pin ⇒ the
        step must not touch anything (a name-derived identity is a guess —
        the conservative-defaults leave-alone leg)."""
        p1, p2, cleanup_rec, prune_rec = self._patched()
        with p1, p2:
            result = project_init.install_project_bundle(
                self.project, self.orch,
            )
        self.assertEqual(cleanup_rec.call_count, 0)
        self.assertEqual(prune_rec.call_count, 0)
        self.assertEqual(
            result["residue_cleanup"],
            {"skipped": "collection identity not pinned"},
        )
        self.assertEqual(
            result["foreign_rows"],
            {"skipped": "collection identity not pinned"},
        )

    def test_wiring_skipped_for_root_target(self):
        p1, p2, cleanup_rec, prune_rec = self._patched()
        with p1, p2:
            result = project_init.install_project_bundle(
                self.orch, self.orch,
            )
        self.assertEqual(cleanup_rec.call_count, 0,
                         "root target must never run the residue step")
        self.assertEqual(prune_rec.call_count, 0)
        self.assertNotIn("residue_cleanup", result)

    def test_wiring_soft_fail_never_breaks_bundle(self):
        self._pin_identity()
        from vco_lib import collection_repair as crep
        boom = mock.MagicMock(side_effect=RuntimeError("weaviate exploded"))
        with mock.patch.object(
            kres, "cleanup_bundled_knowledge_residue", boom,
        ), mock.patch.object(
            crep, "prune_foreign_rows_for_project",
            mock.MagicMock(return_value={}),
        ):
            result = project_init.install_project_bundle(
                self.project, self.orch,
            )
        self.assertTrue(result["manifest_written"],
                        "bundle install must complete despite the step crash")
        self.assertTrue(
            any("non-fatal" in w for w in result["warnings"]),
            f"soft-fail warning missing: {result['warnings']}",
        )

    def test_wiring_dry_run_passthrough(self):
        self._pin_identity()
        p1, p2, cleanup_rec, prune_rec = self._patched()
        with p1, p2:
            project_init.install_project_bundle(
                self.project, self.orch, dry_run=True,
            )
        self.assertIs(cleanup_rec.call_args.kwargs["dry_run"], True)
        self.assertIs(prune_rec.call_args.kwargs["dry_run"], True)


if __name__ == "__main__":
    unittest.main()
