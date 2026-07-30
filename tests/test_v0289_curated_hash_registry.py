# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.89 §7.2 — curated-hash provenance registry (wave-2 P2).

Covers:
  * Builder against a fixture git repo with a renamed-path history
    (``knowledge/`` → ``templates/knowledge/`` — the V52-C move): BOTH
    historical versions covered under one rel path.
  * Legacy-only rel paths (never under ``templates/knowledge/``) excluded —
    the guard that keeps orchestrator-root project nodes out of the
    registry.
  * ``updated:``-only drift across committed versions collapses to ONE
    signature (the tolerance contract).
  * Signature parity: ``vco_lib.knowledge_residue.
    content_signature_excluding_updated`` == ``sync_knowledge_graph.py::
    _content_signature_excluding_updated`` (the one-home + parity lock —
    the sync script cannot be imported by vco_lib at runtime, so the
    mirrored implementation is pinned here instead).
  * Shipped-registry freshness invariant: every CURRENT
    ``templates/knowledge/**/*.md`` file's signature is present — goes red
    when someone edits a curated node without regenerating (mirrors the
    ``test_v0270_shipped_kg_summaries`` precedent).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.knowledge_residue import (  # noqa: E402
    content_signature_excluding_updated,
)

_REGISTRY_PATH = (
    REPO_ROOT / "templates" / "knowledge" / ".curated_hashes.json"
)
_BUILDER_PATH = REPO_ROOT / "scripts" / "build_curated_hash_registry.py"
_SYNC_SCRIPT = (
    REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
)


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        f"_curated_registry_builder_{uuid.uuid4().hex}", _BUILDER_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _node(title: str, body: str, updated: str = "2026-01-01T00:00:00Z") -> str:
    return (
        f"---\ntitle: {title}\ntype: concept\nupdated: {updated}\n---\n"
        f"{body}\n"
    )


@unittest.skipIf(shutil.which("git") is None, "git binary not available")
class BuilderFixtureRepoTests(unittest.TestCase):
    """Builder behavior on a synthetic history with the V52-C move shape."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="vct-curated-reg-")
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@test.invalid")
        self._git("config", "user.name", "Test")
        self._git("config", "commit.gpgsign", "false")

    def _git(self, *args):
        subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True, capture_output=True,
        )

    def _commit_all(self, msg):
        self._git("add", "-A")
        self._git("commit", "-q", "-m", msg)

    def _write(self, rel: str, content: str):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def _build(self):
        builder = _load_builder()
        return builder.build_registry(self.repo)

    def test_renamed_path_history_both_versions_covered(self):
        v1 = _node("A", "version one body")
        v2 = _node("A", "version two body")
        v3 = _node("A", "version three body (post-move)")

        # Pre-V52-C era: curated set lives at knowledge/.
        self._write("knowledge/concepts/a.md", v1)
        self._commit_all("seed a v1")
        self._write("knowledge/concepts/a.md", v2)
        self._commit_all("edit a v2")

        # The V52-C move: knowledge/ → templates/knowledge/ (+ edit).
        (self.repo / "knowledge" / "concepts" / "a.md").unlink()
        self._write("templates/knowledge/concepts/a.md", v3)
        self._commit_all("V52-C move + edit")

        payload, stats = self._build()
        sigs = set(payload["files"]["concepts/a.md"])
        self.assertIn(content_signature_excluding_updated(v1), sigs,
                      "legacy pre-move version must be covered")
        self.assertIn(content_signature_excluding_updated(v2), sigs)
        self.assertIn(content_signature_excluding_updated(v3), sigs)
        self.assertEqual(len(sigs), 3)
        self.assertGreaterEqual(stats["legacy_blob_versions"], 2)

    def test_legacy_only_rel_paths_excluded(self):
        """A knowledge/ node whose rel path never appears under
        templates/knowledge/ (an orchestrator-root project node) must NOT
        enter the registry."""
        self._write("templates/knowledge/concepts/curated.md",
                    _node("C", "curated"))
        self._write("knowledge/concepts/root-only.md",
                    _node("R", "root project node — never shipped"))
        self._commit_all("mixed commit")

        payload, stats = self._build()
        self.assertIn("concepts/curated.md", payload["files"])
        self.assertNotIn("concepts/root-only.md", payload["files"])
        self.assertEqual(stats["legacy_rel_paths_excluded"], 1)

    def test_updated_only_drift_collapses_to_one_signature(self):
        v1 = _node("B", "same body", updated="2026-01-01T00:00:00Z")
        v2 = _node("B", "same body", updated="2026-06-30T12:00:00Z")
        self.assertNotEqual(v1, v2)
        self._write("templates/knowledge/concepts/b.md", v1)
        self._commit_all("b v1")
        self._write("templates/knowledge/concepts/b.md", v2)
        self._commit_all("b v2 (updated: only)")

        payload, _ = self._build()
        self.assertEqual(len(payload["files"]["concepts/b.md"]), 1,
                         "updated:-only churn must collapse to ONE sig")

    def test_worktree_state_included(self):
        """Uncommitted template edits are covered (edit → regen → commit
        keeps the freshness invariant green pre-commit)."""
        v1 = _node("W", "committed")
        self._write("templates/knowledge/concepts/w.md", v1)
        self._commit_all("w v1")
        v2 = _node("W", "uncommitted edit")
        self._write("templates/knowledge/concepts/w.md", v2)

        payload, _ = self._build()
        sigs = set(payload["files"]["concepts/w.md"])
        self.assertIn(content_signature_excluding_updated(v1), sigs)
        self.assertIn(content_signature_excluding_updated(v2), sigs)


class SignatureParityTests(unittest.TestCase):
    """Locks the vco_lib mirror to the sync script's implementation."""

    @classmethod
    def setUpClass(cls):
        env_backup = {
            k: os.environ.get(k)
            for k in (
                "KG_BASE_DIR", "KG_COLLECTION", "SHARED_KG_COLLECTION",
                "DEVELOPMENT_COLLECTION", "DUAL_EMBEDDING_ENABLED",
                "VCT_DISABLE_HUB_RESOLVER", "KG_SYNC_PROJECT_ROOT",
            )
        }
        cls._env_backup = env_backup
        os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
        os.environ["KG_COLLECTION"] = "ParityTest_KnowledgeGraph"
        os.environ["SHARED_KG_COLLECTION"] = ""
        os.environ["DEVELOPMENT_COLLECTION"] = ""
        os.environ["DUAL_EMBEDDING_ENABLED"] = "false"
        os.environ.pop("KG_SYNC_PROJECT_ROOT", None)
        cls._tmp = tempfile.TemporaryDirectory(prefix="vct-parity-")
        os.environ["KG_BASE_DIR"] = cls._tmp.name

        mod_name = f"_sync_kg_parity_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(mod_name, _SYNC_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(
                f"sync_knowledge_graph.py has runtime deps not installed "
                f"({exc})"
            )
        cls.sync_mod = mod

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls._tmp.cleanup()

    def _both(self, content: str):
        return (
            content_signature_excluding_updated(content),
            self.sync_mod._content_signature_excluding_updated(content),
        )

    def test_parity_on_fixtures(self):
        fixtures = [
            _node("X", "body"),
            _node("X", "body", updated="2099-12-31T00:00:00Z"),
            _node("X", "different body"),
            "no frontmatter at all\njust text\n",
            "---\nonly one delimiter",
            "---\ntitle: T\n---\n",
            "",
        ]
        for content in fixtures:
            ours, theirs = self._both(content)
            self.assertEqual(
                ours, theirs,
                f"signature parity broken for fixture {content[:40]!r}",
            )

    def test_updated_drift_equal_body_drift_unequal(self):
        a = _node("X", "body", updated="2026-01-01T00:00:00Z")
        b = _node("X", "body", updated="2026-07-30T00:00:00Z")
        c = _node("X", "body CHANGED")
        self.assertEqual(self._both(a)[0], self._both(b)[0])
        self.assertEqual(self._both(a)[1], self._both(b)[1])
        self.assertNotEqual(self._both(a)[0], self._both(c)[0])


class ShippedRegistryInvariantTests(unittest.TestCase):
    """The committed registry must be present, parseable, and FRESH."""

    def test_registry_present_and_schema_v1(self):
        self.assertTrue(_REGISTRY_PATH.is_file(),
                        f"missing shipped registry {_REGISTRY_PATH}")
        payload = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("schema_version"), 1)
        self.assertIsInstance(payload.get("files"), dict)
        self.assertGreater(len(payload["files"]), 100,
                           "curated set is ~117 files; registry looks empty")

    def test_every_current_template_node_signature_present(self):
        """Goes RED when a curated node is edited without running
        `python scripts/build_curated_hash_registry.py`."""
        payload = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
        files = payload["files"]
        knowledge_root = REPO_ROOT / "templates" / "knowledge"
        missing = []
        for f in sorted(knowledge_root.rglob("*.md")):
            rel = str(f.relative_to(knowledge_root)).replace("\\", "/")
            sig = content_signature_excluding_updated(
                f.read_text(encoding="utf-8", errors="replace")
            )
            if sig not in set(files.get(rel, [])):
                missing.append(rel)
        self.assertEqual(
            missing, [],
            "registry is STALE for these templates/knowledge files — "
            "regenerate with `python scripts/build_curated_hash_registry.py`: "
            f"{missing}",
        )


if __name__ == "__main__":
    unittest.main()
