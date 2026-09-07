# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5b — do these extractor fixes actually REACH an existing install?

WHY THIS EXISTS
---------------
An extractor fix changes what the analyzer EXTRACTS from a file that itself did
not change, and the analyzer's cheapest gate is keyed on the FILE. Landing code
in ``vco_lib/codegraph_lang/`` therefore delivers nothing to anyone who already
has a graph; the delivery mechanism is the extractor-generation force-rewalk
(``vco_lib/codegraph_extractor_generation.py``, wired at
``vco_lib/project_init.py``). WP-5 pinned that chain; this file pins WP-5b's own
claims ON that chain, VERIFIED rather than assumed:

  1. the fixes ride the EXISTING ``0.2.92`` ladder entry — the module's rule is
     "append, never edit", and a second entry for the same release would be
     wrong, so the assertion is that ``"0.2.92"`` is present, not that the tuple
     grew;
  2. after the rewalk bypasses the per-FILE gate, the per-ENTITY content-hash
     gate decides each write — and ``start_line`` / ``end_line`` are EXCLUDED
     from that hash, so a row whose only change is its display range is SKIPPED.

WHAT IS PINNED, and the honest boundary
---------------------------------------
WP-5 had to name a set of rows its rewalk does NOT deliver: eleven class rows
whose only change was ``end_line - 1``, body unchanged, hash unchanged, SKIP.
**WP-5b has no such set, and that is a checkable property rather than a hope.**
Every row this package changes changes something INSIDE the content hash:

  * a Ruby class/method whose ``end_line`` moved also has a re-sliced ``body``
    (the body IS the slice) — and its class row's ``methods`` list moved too;
  * ``Warehouse.Item``, ``Ledger.balanceOf``, ``BaseAccount.audit`` and the
    second row of a reopened class are NEW UUIDs with no stored row to compare
    against, so they are written unconditionally;
  * a module row's ``module_summary`` is hashed.

So the tests below assert the hash MOVES for a representative row of each shape
— computed with the shipped digest over before/after property dicts, not
asserted from a comment.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

import pytest

from vco_lib import codegraph_guards as _guards
from vco_lib.codegraph_content_hash import (
    _CONTENT_HASH_EXCLUDE,
    _content_hash_for_object,
)
from vco_lib.codegraph_extractor_generation import (
    EXTRACTOR_GENERATION_BUMPS,
    REASON_CROSSES_BUMP,
    REASON_NO_GRAPH,
    parse_semver,
    decide,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════════════════════════════════
# HALF 1 — the ladder. VERIFIED, not assumed.
# ═══════════════════════════════════════════════════════════════════════════
def test_wp5b_rides_the_existing_0292_bump_rather_than_adding_one() -> None:
    """The brief said to VERIFY this rather than assume it. WP-5b's changes
    belong to the same generation as WP-5's, the C# route fixes and the Python
    CodeAPI fix, so the ladder needs no new entry — only the one that is
    already there."""
    assert "0.2.92" in EXTRACTOR_GENERATION_BUMPS
    assert EXTRACTOR_GENERATION_BUMPS[-1] == "0.2.92", (
        "a NEWER entry appeared: re-check that WP-5b's fixes are covered by it"
    )


def test_the_ladder_still_covers_the_release_being_tagged() -> None:
    """Duplicated deliberately from WP-5's delivery file: the coordinator's
    version bump is part of this lane's delivery too, and a lane that does not
    assert it cannot claim its fixes ship."""
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml has no top-level version"
    newest = parse_semver(EXTRACTOR_GENERATION_BUMPS[-1])
    declared = parse_semver(m.group(1))
    assert newest is not None and declared is not None
    assert newest >= declared, (
        f"ladder ends at {EXTRACTOR_GENERATION_BUMPS[-1]}, package version is "
        f"{m.group(1)} — append the release version or WP-5b reaches nobody"
    )


def test_an_existing_pre_bump_graph_owes_the_rewalk() -> None:
    """UPDATE axis. Without this crossing, a Ruby project's every class and
    method keeps a body running to end-of-file forever."""
    v = decide(
        prev_version="0.2.91", running_version="0.2.92",
        stamp_generation=None, graph_exists=True,
    )
    assert v.needs_reindex is True
    assert v.reason == REASON_CROSSES_BUMP


def test_a_fresh_install_needs_no_rewalk_because_it_has_no_graph() -> None:
    """FRESH axis, and the LEAVE-ALONE: whatever builds the graph first uses
    the fixed extractors, so there is nothing to repair."""
    v = decide(
        prev_version="", running_version="0.2.92",
        stamp_generation=None, graph_exists=False,
    )
    assert v.needs_reindex is False
    assert v.reason == REASON_NO_GRAPH
    assert v.stamp_now is True


# ═══════════════════════════════════════════════════════════════════════════
# HALF 2 — the per-entity gate, computed with the shipped digest
# ═══════════════════════════════════════════════════════════════════════════
def _hash(coll: str, props: Dict[str, Any]) -> str:
    return _content_hash_for_object(coll, props)


def _classify(stored: str, computed: str) -> Any:
    return _guards.classify_row(
        stored, 3, computed, current_revision=3, floor_revision=1
    )


def test_a_ruby_class_row_moves_its_hash_so_the_rewalk_rewrites_it() -> None:
    """The Ruby class change touches ``class_body`` AND ``methods``, both of
    which ARE hashed — unlike WP-5's eleven ``end_line``-only class rows."""
    before = {
        "full_name": "ledger.Accounting", "signature": "class Accounting",
        "class_body": "module Accounting\n...33 lines to EOF...",
        "methods": ["version", "initialize", "deposit", "default", "withdraw?", "apply_interest"],
        "composes": [], "chunk_num": 0,
        "start_line": 8, "end_line": 40,
    }
    after = dict(before, class_body="module Accounting\n...5 lines...",
                 methods=["version"], end_line=12)
    assert _hash("CodeClass", before) != _hash("CodeClass", after)
    assert _classify(_hash("CodeClass", before), _hash("CodeClass", after)) is _guards.RowAction.EMBED


def test_a_ruby_function_row_moves_its_hash_because_its_body_is_the_slice() -> None:
    before = {
        "full_name": "Accounting.version", "signature": "def version()",
        "function_body": "  def self.version\n...32 lines to EOF...",
        "type_uses": [], "chunk_num": 0, "start_line": 9, "end_line": 40,
    }
    after = dict(before, function_body="  def self.version\n    '1.0'\n  end", end_line=11)
    assert _hash("CodeFunction", before) != _hash("CodeFunction", after)


def test_a_module_row_moves_its_hash_when_the_type_list_grows() -> None:
    """``Inventory.cs`` is UNTOUCHED on disk; only its summary changed, because
    the positional record finally appears in it."""
    before = {
        "path": "src/Inventory.cs",
        "module_summary": "C# module: src/Inventory.cs\nClasses: IRepository, InventoryController",
        "import_names": ["System"],
    }
    after = dict(
        before,
        module_summary="C# module: src/Inventory.cs\nClasses: IRepository, Item, InventoryController",
    )
    assert _hash("CodeModule", before) != _hash("CodeModule", after)


def test_the_line_range_alone_still_does_not_move_the_hash() -> None:
    """The EXCLUSION is unchanged and must stay unchanged — putting
    ``end_line`` into the hash would rewrite every row below any edit in every
    project, the write amplification the exclusion exists to prevent. WP-5b
    does not need it, because none of its row changes is range-ONLY."""
    assert "start_line" in _CONTENT_HASH_EXCLUDE
    assert "end_line" in _CONTENT_HASH_EXCLUDE
    base = {
        "full_name": "x.Y", "signature": "class Y", "class_body": "class Y {}",
        "methods": [], "composes": [], "chunk_num": 0, "start_line": 1, "end_line": 2,
    }
    assert _hash("CodeClass", base) == _hash("CodeClass", dict(base, start_line=9, end_line=99))
    assert _classify("same", "same") is _guards.RowAction.SKIP


@pytest.mark.parametrize(
    "coll,props",
    [
        ("CodeClass", {
            "full_name": "Warehouse.Item", "signature": "class Item",
            "class_body": "public record Item(int Id, string Name);",
            "methods": [], "composes": [], "chunk_num": 0,
        }),
        ("CodeFunction", {
            "full_name": "Ledger.balanceOf", "signature": "balanceOf(String owner)",
            "function_body": "    long balanceOf(String owner);",
            "type_uses": [], "chunk_num": 0,
        }),
    ],
)
def test_a_brand_new_entity_is_written_unconditionally(coll: str, props: Dict[str, Any]) -> None:
    """A type or method that produced NO row before has no stored hash to
    compare against, so the fail-safe path writes it. This is why the record
    and the bodiless declarations reach an already-damaged install without
    depending on the content-hash comparison at all."""
    computed = _hash(coll, props)
    assert _guards.classify_row(
        None, None, computed, current_revision=3, floor_revision=1
    ) is _guards.RowAction.EMBED
    assert _guards.classify_row(
        "", 0, computed, current_revision=3, floor_revision=1
    ) is _guards.RowAction.EMBED


def test_a_renamed_row_leaves_an_orphan_that_a_shipped_pass_reclaims() -> None:
    """THE ONE BOUNDARY WORTH NAMING. Three Ruby function rows change their
    ``full_name`` (``Accounting.initialize`` -> ``Account.initialize``, because
    the reopened class no longer hides the first declaration). ``full_name``
    feeds the deterministic UUID, so the rewalk writes a NEW row and never
    touches the old one.

    That is not a leak: v0.2.91 WP-C's per-file entity reconciliation deletes
    rows anchored to a file this walk ACTUALLY walked whose UUIDs this walk did
    NOT upsert — the "entity-orphan immortality" class, whose whole point is
    exactly this (a rename makes an entity vanish while its file survives).
    Asserted here as the existence of the mechanism, so the claim in the report
    has a consumer in the tree.
    """
    from vco_lib import codegraph_resync

    assert hasattr(codegraph_resync, "reconcile_walked_file_rows")
    doc = codegraph_resync.reconcile_walked_file_rows.__doc__ or ""
    assert "renaming a class" in doc or "rename" in doc.lower(), (
        "the reconcile pass no longer documents the rename case — re-check that "
        "a renamed entity's old row is still reclaimed"
    )
