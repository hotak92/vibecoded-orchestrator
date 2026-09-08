# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-5 — do the extractor fixes actually REACH an existing install?

WHY THIS EXISTS
---------------
An extractor fix changes what the analyzer EXTRACTS from a file that itself
did not change, and the analyzer's cheapest gate is keyed on the FILE. So the
code landing in ``vco_lib/codegraph_lang/`` delivers the fix to nobody who
already has a graph; the delivery mechanism is the extractor-generation
force-rewalk (``vco_lib/codegraph_extractor_generation.py``, wired at
``vco_lib/project_init.py``). These tests pin that WP-5's fixes ride it — and,
just as importantly, pin the ONE class of change that the rewalk does NOT
deliver, so that limitation is an asserted fact rather than a hopeful comment.

THE TWO HALVES, both required:
  1. the generation ladder must name a version at or beyond the release being
     tagged — otherwise ``decide`` never returns "owed" for the crossing;
  2. after the rewalk bypasses the per-FILE gate, the per-ENTITY content-hash
     gate decides each write. A row whose stored BODY changes is re-written; a
     row whose ONLY change is an excluded field (``start_line`` / ``end_line``)
     is SKIPPED, and its display range stays stale.

WHAT IS PINNED
--------------
* the ACT: a project whose graph predates the bump owes the walk — including
  when Weaviate cannot be reached to check (unknown is owed, never assumed
  healthy);
* the LEAVE-ALONE: a stamped-current project and a project with positively no
  graph do NOT get a surprise full build;
* the honest boundary: which of WP-5's own row changes the rewalk delivers,
  and which it does not.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from vco_lib import codegraph_guards as _guards
from vco_lib.codegraph_content_hash import _CONTENT_HASH_EXCLUDE
from vco_lib.codegraph_extractor_generation import (
    EXTRACTOR_GENERATION_BUMPS,
    REASON_CROSSES_BUMP,
    REASON_NO_GRAPH,
    REASON_STAMP_CURRENT,
    REASON_UNKNOWN_GENERATION,
    decide,
    parse_semver,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _declared_package_version() -> str:
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml has no top-level version"
    return m.group(1)


# ═══════════════════════════════════════════════════════════════════════════
# HALF 1 — the ladder must cover the release being tagged
# ═══════════════════════════════════════════════════════════════════════════
def test_the_generation_ladder_covers_the_release_being_tagged() -> None:
    """If the release version ever moves PAST the newest bump without a new
    entry, every extractor fix in that release is delivered to nobody who
    already has a graph — silently, with no error anywhere.

    ``>=`` rather than ``==`` because the newest bump is declared while the
    version is still the previous one (0.2.92 was appended during the 0.2.91
    -> 0.2.92 cycle, before the version bump).
    """
    newest = parse_semver(EXTRACTOR_GENERATION_BUMPS[-1])
    declared = parse_semver(_declared_package_version())
    assert newest is not None and declared is not None
    assert newest >= declared, (
        f"EXTRACTOR_GENERATION_BUMPS ends at {EXTRACTOR_GENERATION_BUMPS[-1]} but "
        f"the package version is {_declared_package_version()} — append the "
        "release version to the ladder, or extractor fixes in it never reach "
        "an existing graph"
    )


def test_wp5_rides_the_existing_0292_bump_rather_than_adding_one() -> None:
    """WP-5's extractor changes belong to the SAME generation the C# route and
    Python CodeAPI fixes already declared, so the ladder is unchanged. The
    module's maintainer rule is "append, never edit" — this asserts the entry
    it needs is present."""
    assert "0.2.92" in EXTRACTOR_GENERATION_BUMPS


# ═══════════════════════════════════════════════════════════════════════════
# HALF 1 — the decision, per axis
# ═══════════════════════════════════════════════════════════════════════════
def test_an_existing_pre_bump_graph_owes_the_rewalk() -> None:
    """UPDATE axis: the project's prior manifest says 0.2.91, the orchestrator
    installing now is 0.2.92 → the crossing is the designed trigger."""
    v = decide(
        prev_version="0.2.91", running_version="0.2.92",
        stamp_generation=None, graph_exists=True,
    )
    assert v.needs_reindex is True
    assert v.reason == REASON_CROSSES_BUMP
    assert v.stamp_now is False


def test_an_unprovable_graph_still_owes_the_rewalk() -> None:
    """Weaviate unreachable → ``graph_exists=None``. Unknown is OWED: a
    needless re-extraction costs one cheap pass and zero embeds, while a
    skipped stale project stays broken forever with no error."""
    v = decide(
        prev_version="", running_version="0.2.92",
        stamp_generation=None, graph_exists=None,
    )
    assert v.needs_reindex is True
    assert v.reason == REASON_UNKNOWN_GENERATION


def test_a_stamped_project_is_left_alone() -> None:
    """LEAVE-ALONE: the stamp is written only after a clean force walk."""
    # v0.2.93 release-time pin move: the scenario versions are DERIVED from
    # the ladder so this stays the same test at every future append (the
    # shape — version crossing the newest bump, stamp AT the newest
    # generation — is what "leave alone" means).
    newest = EXTRACTOR_GENERATION_BUMPS[-1]
    below_newest = EXTRACTOR_GENERATION_BUMPS[-2] if len(EXTRACTOR_GENERATION_BUMPS) > 1 else "0.2.91"
    v = decide(
        prev_version=below_newest, running_version=newest,
        stamp_generation=newest, graph_exists=True,
    )
    assert v.needs_reindex is False
    assert v.reason == REASON_STAMP_CURRENT
    assert v.stamp_now is False


def test_a_fresh_install_does_not_get_a_surprise_full_build() -> None:
    """FRESH axis: no graph to repair; whatever builds it next uses the fixed
    analyzer. Stamp so future updates stop asking, and walk nothing."""
    v = decide(
        prev_version="", running_version="0.2.92",
        stamp_generation=None, graph_exists=False,
    )
    assert v.needs_reindex is False
    assert v.reason == REASON_NO_GRAPH
    assert v.stamp_now is True


# ═══════════════════════════════════════════════════════════════════════════
# HALF 2 — what the rewalk then writes, and what it does not
# ═══════════════════════════════════════════════════════════════════════════
def _classify(stored_hash: str, computed_hash: str) -> _guards.RowAction:
    return _guards.classify_row(
        stored_hash, 3, computed_hash,
        current_revision=3, floor_revision=1,
    )


def test_a_row_whose_body_changed_is_rewritten() -> None:
    """THE ACT for almost every WP-5 row: correcting ``start_line`` re-slices
    the body from the source, so the content hash moves and the row is
    re-written with its corrected range."""
    assert _classify("aaa", "bbb") is _guards.RowAction.EMBED


def test_a_row_whose_only_change_is_its_line_range_is_NOT_rewritten() -> None:
    """THE HONEST BOUNDARY — stated as an assertion so it cannot rot into a
    comment nobody re-checks.

    ``start_line`` / ``end_line`` are deliberately excluded from the content
    hash (including them would re-write every row below any edit, on every
    keystroke — the exact write amplification the skip exists to avoid). So
    WP-5's class-row ``end_line`` correction, where the body is unchanged,
    does NOT land via the force-rewalk: those rows keep a display range one
    line long until a graph is rebuilt from scratch. Every row whose stored
    BODY was wrong — which is every row whose range was materially wrong —
    IS corrected, because its hash moves with its body.
    """
    assert "start_line" in _CONTENT_HASH_EXCLUDE
    assert "end_line" in _CONTENT_HASH_EXCLUDE
    assert _classify("same", "same") is _guards.RowAction.SKIP


@pytest.mark.parametrize("stored_rev", [None, 0, -1])
def test_a_vectorless_or_unknown_row_is_always_rewritten(stored_rev) -> None:
    """Fail-safe: every uncertainty resolves to EMBED, so a partially-written
    graph converges rather than freezing."""
    assert _guards.classify_row(
        "same", stored_rev, "same", current_revision=3, floor_revision=1,
    ) is _guards.RowAction.EMBED
