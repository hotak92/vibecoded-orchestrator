# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 pull-in ⑧b — ``store_knowledge_node`` leaves no RENAME orphans.

The store's narrowing read used to AND a tokenized ``title`` clause onto the
path clause. When a node's TITLE changed at the same ``file_path``, the
old-title rows were never read back, so never deleted: they sat beside the new
rows as duplicates until a kg-sync (whose upsert keys on the exact path alone).

Now the read narrows on the path only and ``is_exact_path`` confirms each row:
every row at exactly this node's path is this upsert's stale data, whatever its
title; a token-superset sibling still never qualifies.

Same in-memory fakes as the B3 suite (``test_v02101_kg_exact_path_delete``):
a ``Filter`` with Weaviate ``word``-tokenized ``Equal`` semantics. No Weaviate,
no Ollama, no fixture-class writes.
"""

from __future__ import annotations

import pytest

import vco_lib.weaviate_exact_match as wem
from tests.test_v02101_kg_exact_path_delete import _mcp_coll, _TokFilter
from tests.test_v0273_kg_write_path import _patch_server_for_store, _store

PATH = "knowledge/concepts/sample_a.md"
SIBLING = "knowledge/concepts/sample_a_extended.md"  # token SUPERSET of PATH
OTHER = "knowledge/archive/sample_b.md"


def _rows(coll, fp):
    return [o for o in coll.objects if o.properties.get("file_path") == fp]


@pytest.fixture
def revert_to_title_and_path_narrowing(monkeypatch):
    """Red-proof switch: restore the pre-⑧b narrowing — ``title AND path`` —
    at the shared home the store calls (resolved at call time)."""
    orig = wem.path_narrowing_filter

    def _apply(title: str):
        def _title_and_path(filter_cls, canonical, prop="file_path"):
            return (filter_cls.by_property("title").equal(title)
                    & orig(filter_cls, canonical, prop))
        monkeypatch.setattr(wem, "path_narrowing_filter", _title_and_path)
    return _apply


@pytest.mark.parametrize("reverted", [False, True], ids=["shipped", "red-proof"])
def test_rename_at_same_path_removes_old_title_rows(
    monkeypatch, tmp_path, revert_to_title_and_path_narrowing, reverted,
):
    coll = _mcp_coll(
        # the node as previously stored: 3 chunks, OLD title, both spellings
        {"title": "Old Name", "file_path": PATH, "chunk_num": 1},
        {"title": "Old Name", "file_path": PATH, "chunk_num": 2},
        {"title": "Old Name", "file_path": PATH.replace("/", "\\"), "chunk_num": 3},
        # LEAVE-ALONE: a token-superset sibling carrying the OLD title too…
        {"title": "Old Name", "file_path": SIBLING},
        # …and another node at another path with the NEW title.
        {"title": "New Name", "file_path": OTHER},
    )
    srv = _patch_server_for_store(monkeypatch, tmp_path, coll)
    monkeypatch.setattr(srv, "Filter", _TokFilter)
    if reverted:
        revert_to_title_and_path_narrowing("New Name")

    result = _store(srv, title="New Name", file_path=PATH)
    assert result.get("success") is True

    at_path = _rows(coll, PATH) + _rows(coll, PATH.replace("/", "\\"))
    titles = sorted(str(o.properties.get("title")) for o in at_path)
    if reverted:
        # Pre-⑧b: the old-title rows survive beside the new row — the orphans.
        assert titles == ["New Name", "Old Name", "Old Name", "Old Name"], titles
    else:
        assert titles == ["New Name"], f"old-title rows must be gone: {titles}"
    # Neither the token-superset sibling nor the other node is ever touched.
    assert [o.properties.get("title") for o in _rows(coll, SIBLING)] == ["Old Name"]
    assert [o.properties.get("title") for o in _rows(coll, OTHER)] == ["New Name"]


def test_plain_restore_still_replaces_same_title_rows(monkeypatch, tmp_path):
    """LEAVE-ALONE leg for the ordinary upsert: an unrenamed re-store replaces
    its own rows exactly as before and leaves the sibling alone."""
    coll = _mcp_coll(
        {"title": "Sample Title", "file_path": PATH},
        {"title": "Sample Title", "file_path": PATH},
        {"title": "Sample Title Extended", "file_path": SIBLING},
    )
    srv = _patch_server_for_store(monkeypatch, tmp_path, coll)
    monkeypatch.setattr(srv, "Filter", _TokFilter)

    result = _store(srv, title="Sample Title", file_path=PATH)
    assert result.get("success") is True
    assert len(_rows(coll, PATH)) == 1
    assert len(_rows(coll, SIBLING)) == 1


def test_embed_failure_on_a_rename_keeps_the_old_rows(monkeypatch, tmp_path):
    """D-2 ordering holds for the widened delete set: a failed embed deletes
    nothing — the old-title rows are only removed once the new rows are in."""
    coll = _mcp_coll({"title": "Old Name", "file_path": PATH})
    srv = _patch_server_for_store(monkeypatch, tmp_path, coll, embed_raises=True)
    monkeypatch.setattr(srv, "Filter", _TokFilter)

    _store(srv, title="New Name", file_path=PATH)
    assert [o.properties.get("title") for o in _rows(coll, PATH)] == ["Old Name"]
