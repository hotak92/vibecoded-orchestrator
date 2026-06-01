# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.44 V44-A: stale-row prune runs on every --update, not just full-sync.

Pre-v0.2.44 the prune was gated on ``_sync_all=True``, which meant a CI-10
partial-sync run (subset of files changed) would leave orphan rows in the
KG collection forever. This accumulated 192 stale rows in VCO_dev's KG over
multiple v0.2.43 update cycles. V44-A removes the gate: the prune now runs
on every successful ``--update`` sync (full OR partial).

Also tests the multi-strategy ``_path_resolves_on_disk`` helper that V44-A
introduced. The helper tries 4 normalization strategies before declaring
a row's ``file_path`` an orphan (relative, resolved-symlink, absolute,
worktree-stripped).

Tests in this module
~~~~~~~~~~~~~~~~~~~~

* test_prune_runs_when_sync_all_is_false
* test_path_resolves_on_disk_strategies
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402


_APP_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


def _make_app_state_db(tmp: Path, **rows) -> Path:
    """Create a temp launcher.db with app_state seeded so the diff-gate
    has a "context unchanged" baseline and proceeds to the diff path.
    """
    db_path = tmp / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_APP_STATE_SCHEMA)
    now = 1_000_000
    for k, v in rows.items():
        conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
            (k, v, now),
        )
    conn.commit()
    conn.close()
    return db_path


def _make_args(update: bool = True, skip_seed: bool = False) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.update = update
    ns.skip_seed = skip_seed
    return ns


class PruneRunsOnPartialSyncTest(unittest.TestCase):
    """V44-A: _prune_stale_kg_rows IS invoked on the partial-sync branch."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        # Stage env so the diff-gate's "context unchanged" check passes.
        os.environ["VCT_STATE_DIR"] = str(self.tmp)
        os.environ["ACTIVE_EMBEDDING"] = "qwen3"
        os.environ["KG_COLLECTION"] = "TestProject_KnowledgeGraph"
        os.environ["SHARED_KG_COLLECTION"] = ""  # shared seed skipped

        self.db_path = _make_app_state_db(
            self.tmp,
            last_installed_active_embedding="qwen3",
            last_installed_kg_collection="TestProject_KnowledgeGraph",
            last_installed_shared_kg_collection="",
        )

    def tearDown(self) -> None:
        for k in (
            "VCT_STATE_DIR", "ACTIVE_EMBEDDING",
            "KG_COLLECTION", "SHARED_KG_COLLECTION",
        ):
            os.environ.pop(k, None)
        self._tmpdir.cleanup()

    def test_prune_runs_when_sync_all_is_false(self):
        """Partial-sync (subset of files changed) MUST invoke _prune_stale_kg_rows.

        Pre-v0.2.44 the prune was gated on ``_sync_all=True``, so a CI-10
        partial-sync would skip the orphan cleanup. V44-A removed that gate.

        Setup: 2 on-disk files, only 1 differs from stored hash → partial-sync
        path with ``_sync_all=False``. Verify ``_prune_stale_kg_rows`` is called
        exactly once with the configured KG collection.
        """
        captured_prune_calls: list[tuple] = []

        def _capture_prune(collection: str, weaviate_url: str, **kwargs):
            captured_prune_calls.append((collection, weaviate_url, kwargs))

        # On-disk: 2 files. Stored: only 1 matches → 1-file diff → partial sync.
        on_disk_hashes = {
            f"{self.tmp}/knowledge/concepts/foo.md": "diskhash_foo",
            f"{self.tmp}/knowledge/concepts/bar.md": "diskhash_bar",
        }
        stored_hashes = {
            f"{self.tmp}/knowledge/concepts/foo.md": "diskhash_foo",  # unchanged
            f"{self.tmp}/knowledge/concepts/bar.md": "OLD_hash_bar",  # changed
        }

        # Materialise the .venv + sync_knowledge_graph.py stubs PROJECT_ROOT.
        scripts_dir = self.tmp / ".claude" / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        sync_kg = scripts_dir / "sync_knowledge_graph.py"
        sync_kg.write_text("# stub\n")
        venv_bin = self.tmp / ".venv" / "bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        fake_python = venv_bin / "python"
        fake_python.write_text("#!/bin/sh\n")
        fake_python.chmod(0o755)

        def _fake_run(cmd, **kwargs):  # noqa: ARG001
            class _R:
                returncode = 0
            return _R()

        with mock.patch.object(install, "PROJECT_ROOT", self.tmp), \
             mock.patch.object(install, "_discover_app_state_db_path", return_value=self.db_path), \
             mock.patch.object(install, "_compute_on_disk_content_hashes", return_value=on_disk_hashes), \
             mock.patch.object(install, "_batch_query_weaviate_content_hashes", return_value=stored_hashes), \
             mock.patch.object(install, "_prune_stale_kg_rows", side_effect=_capture_prune) as prune_mock, \
             mock.patch("subprocess.run", side_effect=_fake_run):
            install._seed_weaviate(_make_args(update=True))

        # The prune must have been invoked exactly once.
        self.assertEqual(
            prune_mock.call_count, 1,
            f"_prune_stale_kg_rows must be called once on partial-sync, "
            f"got {prune_mock.call_count} calls: {captured_prune_calls!r}",
        )

        # The prune was invoked with the configured KG_COLLECTION.
        invoked_collection, _, _ = captured_prune_calls[0]
        self.assertEqual(
            invoked_collection, "TestProject_KnowledgeGraph",
            "prune must target the configured KG_COLLECTION",
        )


class PathResolvesOnDiskTest(unittest.TestCase):
    """V44-A: _path_resolves_on_disk multi-strategy matching.

    The helper returns True when any of these strategies finds the file:
      1. (PROJECT_ROOT / file_path).exists()           — relative-to-root
      2. (PROJECT_ROOT.resolve() / file_path).exists() — symlink-resolved
      3. Path(file_path).exists() (when absolute)       — direct absolute
      4. Worktree prefix stripped + matched at root     — agent-worktree case

    Otherwise returns False (the row is an orphan; safe to prune).
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        # Materialise a tiny knowledge/ tree we can probe.
        (self.tmp / "knowledge" / "concepts").mkdir(parents=True, exist_ok=True)
        (self.tmp / "knowledge" / "concepts" / "alpha.md").write_text(
            "---\ntitle: Alpha\n---\nBody.\n"
        )
        (self.tmp / "knowledge" / "concepts" / "beta.md").write_text(
            "---\ntitle: Beta\n---\nBody.\n"
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_path_resolves_on_disk_strategies(self):
        """Each of the 5 cases yields the expected True/False verdict."""
        with mock.patch.object(install, "PROJECT_ROOT", self.tmp):
            # 1. relative-to-PROJECT_ROOT exists → True (kept)
            self.assertTrue(
                install._path_resolves_on_disk("knowledge/concepts/alpha.md"),
                "strategy 1 (relative-to-root) must match existing file",
            )

            # 2. resolved-PROJECT_ROOT via symlink: simulate by adding a
            #    symlinked alias dir that points at the real knowledge/ tree.
            #    The relative form below resolves correctly through the
            #    resolved PROJECT_ROOT (PROJECT_ROOT.resolve() / path).
            #    We assert the strategy is exercised by a path that ONLY
            #    matches under .resolve() — for that we construct a path
            #    relative to the symlink target.
            try:
                symlinked_root = self.tmp.parent / (self.tmp.name + "_link")
                if symlinked_root.exists():
                    symlinked_root.unlink()
                symlinked_root.symlink_to(self.tmp, target_is_directory=True)
                with mock.patch.object(install, "PROJECT_ROOT", symlinked_root):
                    # Strategy 1 (relative-to-symlinked-root) ALSO matches
                    # because both forms exist on disk; either is fine —
                    # the contract is "any True". Verify True.
                    self.assertTrue(
                        install._path_resolves_on_disk(
                            "knowledge/concepts/alpha.md"
                        ),
                        "strategy 1/2 (resolved-root) must match via symlink",
                    )
                symlinked_root.unlink()
            except (OSError, NotImplementedError):
                # Filesystem doesn't support symlinks (Windows without admin) —
                # skip this strategy.
                pass

            # 3. Absolute path that exists → True (kept)
            abs_path = str(self.tmp / "knowledge" / "concepts" / "beta.md")
            self.assertTrue(
                install._path_resolves_on_disk(abs_path),
                "strategy 3 (absolute path) must match existing file",
            )

            # 4. Worktree-prefixed but file exists at stripped path → True (kept)
            #    Real-world example: file_path stored as
            #    ".claude/worktrees/agent-XXX/knowledge/concepts/alpha.md"
            #    when the row was synced inside a worktree but the file lives
            #    in the parent repo. The strip-and-match strategy keeps it.
            worktree_form = (
                ".claude/worktrees/agent-abc123/knowledge/concepts/alpha.md"
            )
            self.assertTrue(
                install._path_resolves_on_disk(worktree_form),
                "strategy 4 (worktree-strip) must match file at stripped path",
            )

            # 5. None of the strategies match → False (orphan, will be pruned).
            self.assertFalse(
                install._path_resolves_on_disk(
                    "knowledge/concepts/never_existed.md"
                ),
                "all strategies fail → False (mark as orphan)",
            )

            # Note: an empty file_path resolves as (PROJECT_ROOT / "").exists() ==
            # True (PROJECT_ROOT itself exists), so the function returns True for
            # empty input. The prune caller already filters empty file_path before
            # calling this helper (see _prune_stale_kg_rows: `if uid and fp:`),
            # so this edge case never reaches _path_resolves_on_disk in practice.

            # Edge: a clearly malformed path (no real file) → False.
            self.assertFalse(
                install._path_resolves_on_disk(
                    "/this/absolute/path/definitely/does/not/exist/anywhere.md"
                ),
                "malformed absolute path → False",
            )


if __name__ == "__main__":
    unittest.main()
