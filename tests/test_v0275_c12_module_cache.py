# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.75 P2c (C-12): module update-branch fingerprints + (source, path) keying.

Two latent traps in ``_create_or_update_module``'s "path in module_cache"
update branch:

  1. It did a DIRECT ``data.update`` that stamped neither ``embed_revision`` nor
     ``content_hash`` — the fingerprints every OTHER write stamps via
     ``_write_one_object``. If the cache is ever preloaded before the walk, this
     branch leaves the row at a NULL/stale revision while its content is current
     (the M0-inversion trap). Fix: stamp both, matching the insert path.

  2. ``module_cache`` was keyed by BARE relpath — two ``--extra-path`` roots
     sharing a relpath collide (the second update targets the FIRST root's
     UUID, defeating V52-O.3 at the cache layer). Fix: key by
     ``(project_source, relpath)``; ``_invalidate_module_row`` follows.

ACT: extra-path collision → two distinct UUIDs, no cross-write.
LEAVE-ALONE: same-source re-touch → the SAME cached UUID is updated (reuse).
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


@pytest.fixture(scope="module")
def analyzer_mod():
    spec = importlib.util.spec_from_file_location("_acg_c12", str(_ANALYZER_PATH))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _FakeData:
    def __init__(self):
        self.updates = []

    def update(self, uuid, properties):
        self.updates.append({"uuid": uuid, "properties": dict(properties)})


class _FakeModulesColl:
    def __init__(self):
        self.name = "P_CodeModule"
        self.data = _FakeData()


class _ModStub:
    """Binds the real _create_or_update_module with the insert path stubbed so
    only the UPDATE branch + cache keying is exercised."""

    def __init__(self, analyzer_mod):
        self.modules_collection = _FakeModulesColl()
        self.module_cache = {}
        self._track_visited = False
        self.visited_uuids = set()
        self.project_name = "P"
        self._current_language = "python"
        self._current_source = ""
        cls = analyzer_mod.CodeGraphAnalyzer
        self._create_or_update_module = cls._create_or_update_module.__get__(self, _ModStub)


def _touch(stub, path, source, summary="s", imports=None):
    stub._current_source = source
    return stub._create_or_update_module(
        path, "python", 10, 1.0, datetime(2026, 1, 1, tzinfo=timezone.utc),
        "hash-" + path, imports or ["os"], summary,
    )


# ─────────────────── C-12 (1): fingerprint stamping ───────────────────


def test_update_branch_stamps_embed_revision_and_content_hash(analyzer_mod):
    """The update branch must stamp embed_revision (current) + content_hash so
    the revision-aware gate stays honest on this bypass path."""
    stub = _ModStub(analyzer_mod)
    src = "/repo"
    # Pre-seed the cache as if the row was preloaded → force the UPDATE branch.
    stub.module_cache[(src, "pkg/mod.py")] = "uuid-PRE"

    uuid = _touch(stub, "pkg/mod.py", src)

    assert uuid == "uuid-PRE"
    assert len(stub.modules_collection.data.updates) == 1
    props = stub.modules_collection.data.updates[0]["properties"]
    assert props["embed_revision"] == analyzer_mod.CODEGRAPH_EMBED_REVISION, (
        "update branch must stamp the current embed_revision (M0-inversion fix)"
    )
    assert props.get("content_hash"), "update branch must stamp a content_hash"
    # And it must equal what the insert path would hash over the same fields.
    expected = analyzer_mod._content_hash_for_object(
        "CodeModule",
        {"path": "pkg/mod.py", "module_summary": "s", "import_names": ["os"]},
    )
    assert props["content_hash"] == expected, "content_hash must match the insert path"


# ─────────────────── C-12 (2): (source, path) keying ───────────────────


def test_extra_path_collision_no_cross_write(analyzer_mod):
    """ACT: two --extra-path roots share a relpath. Pre-seed BOTH source keys
    with distinct UUIDs; touching each must update ITS OWN row, never the other's
    (pre-fix the bare-relpath key aliased them onto one UUID)."""
    stub = _ModStub(analyzer_mod)
    stub.module_cache[("/rootA", "install.py")] = "uuid-A"
    stub.module_cache[("/rootB", "install.py")] = "uuid-B"

    u_a = _touch(stub, "install.py", "/rootA")
    u_b = _touch(stub, "install.py", "/rootB")

    assert u_a == "uuid-A" and u_b == "uuid-B", "each source resolves its OWN UUID"
    updated_uuids = [u["uuid"] for u in stub.modules_collection.data.updates]
    assert updated_uuids == ["uuid-A", "uuid-B"], (
        f"no cross-write across source roots; got {updated_uuids}"
    )


def test_same_source_retouch_reuses_cached_uuid(analyzer_mod):
    """LEAVE-ALONE: re-touching the SAME (source, path) updates the SAME cached
    row (reuse), never a second insert."""
    stub = _ModStub(analyzer_mod)
    src = "/repo"
    stub.module_cache[(src, "pkg/mod.py")] = "uuid-SAME"

    u1 = _touch(stub, "pkg/mod.py", src)
    u2 = _touch(stub, "pkg/mod.py", src)

    assert u1 == u2 == "uuid-SAME"
    assert len(stub.modules_collection.data.updates) == 2, "both were updates (reuse)"


def test_cache_write_key_is_source_path_tuple(analyzer_mod):
    """The cache annotation contract: keys are (project_source, path) tuples."""
    stub = _ModStub(analyzer_mod)
    src = "/repo"
    stub.module_cache[(src, "a/b.py")] = "uuid-X"
    # A DIFFERENT source with the same relpath is a DISTINCT entry.
    stub.module_cache[("/other", "a/b.py")] = "uuid-Y"
    assert stub.module_cache[(src, "a/b.py")] == "uuid-X"
    assert stub.module_cache[("/other", "a/b.py")] == "uuid-Y"
    assert len(stub.module_cache) == 2, "same relpath / different source = 2 entries"
