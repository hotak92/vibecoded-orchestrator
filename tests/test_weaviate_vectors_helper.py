# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.82 (FIX-B / B4 + L4): the ONE named-vector round-trip cleaner.

``vco_lib.weaviate_vectors.clean_named_vector`` drops configured-but-empty
``{slot: []}`` slots that weaviate rejects on re-insert
(``WeaviateInvalidInputError('Invalid vectors: [].')``). BOTH vector copiers —
``vco_lib.codegraph_vector_copy`` (code-graph identity migration) and
``vco_lib.project_init._copy_collection_with_vectors`` (KG/Dev schema migration)
— must route through this ONE helper, never a private inline dict-comp.

This file:
  * unit-tests the helper's shape handling (dict / list / None / all-empty);
  * pins (source-shape) that BOTH call sites import + call ``clean_named_vector``
    and that neither carries a surviving inline ``{k: v for ... if v}`` copy.
"""
from __future__ import annotations

from pathlib import Path

from vco_lib.weaviate_vectors import clean_named_vector

REPO_ROOT = Path(__file__).resolve().parent.parent
_VECTOR_COPY = REPO_ROOT / "vco_lib" / "codegraph_vector_copy.py"
_PROJECT_INIT = REPO_ROOT / "vco_lib" / "project_init.py"


# ─────────────────────────── unit: shape handling ────────────────────────────


def test_drops_empty_named_slot():
    """A configured-but-empty ``{slot: []}`` slot is dropped; populated slots
    survive verbatim."""
    vec = {"codesage_embed": [0.1, 0.2, 0.3], "openai_embed": []}
    out = clean_named_vector(vec)
    assert out == {"codesage_embed": [0.1, 0.2, 0.3]}
    assert "openai_embed" not in out


def test_keeps_all_populated_slots_verbatim():
    vec = {"codesage_embed": [1.0], "openai_embed": [2.0, 3.0]}
    out = clean_named_vector(vec)
    assert out == vec
    # New dict (does not mutate the input in place).
    assert out is not vec


def test_all_empty_dict_becomes_empty_dict():
    """An all-empty named-vector dict collapses to ``{}`` (weaviate accepts an
    empty dict as 'no vector'; it rejects only an empty LIST inside a slot)."""
    assert clean_named_vector({"a": [], "b": []}) == {}


def test_none_passthrough():
    assert clean_named_vector(None) is None


def test_legacy_single_vector_list_unchanged():
    """A legacy single-vector list has no per-slot ``[]`` footgun → returned
    unchanged (the caller decides whether a single vector is copyable)."""
    lst = [0.1, 0.2, 0.3]
    assert clean_named_vector(lst) is lst


def test_falsy_but_nonempty_zero_vector_slot_kept():
    """A slot whose vector is all zeros is still a POPULATED slot (a non-empty
    list is truthy) — it must be kept, not dropped as 'empty'."""
    vec = {"codesage_embed": [0.0, 0.0, 0.0]}
    assert clean_named_vector(vec) == vec


# ─────────────────── source-shape parity: both call sites ────────────────────


def test_both_copiers_import_the_shared_helper():
    """Both vector copiers import ``clean_named_vector`` from the shared home —
    no private inline reimplementation."""
    vc_src = _VECTOR_COPY.read_text(encoding="utf-8")
    pi_src = _PROJECT_INIT.read_text(encoding="utf-8")
    assert "from vco_lib.weaviate_vectors import clean_named_vector" in vc_src, (
        "codegraph_vector_copy must import the shared clean_named_vector helper"
    )
    assert "from vco_lib.weaviate_vectors import clean_named_vector" in pi_src, (
        "project_init must import the shared clean_named_vector helper"
    )


def test_both_copiers_call_the_shared_helper():
    """Both call sites CALL ``clean_named_vector(...)`` before writing."""
    vc_src = _VECTOR_COPY.read_text(encoding="utf-8")
    pi_src = _PROJECT_INIT.read_text(encoding="utf-8")
    assert "clean_named_vector(src_vec)" in vc_src, (
        "codegraph_vector_copy must clean the vector before insert/replace"
    )
    assert "clean_named_vector(vec)" in pi_src, (
        "project_init's _copy_collection_with_vectors must clean the vector "
        "before add_object"
    )


def test_no_surviving_inline_empty_slot_dictcomp():
    """Neither call site keeps the OLD inline ``{k: v for k, v in vec.items()
    if v}`` copy — the rule lives once in the helper (extract-before-duplicate).
    """
    for path in (_VECTOR_COPY, _PROJECT_INIT):
        src = path.read_text(encoding="utf-8")
        assert "for k, v in vec.items() if v" not in src, (
            f"{path.name} still carries the inline empty-slot dict-comp — it "
            "must route through vco_lib.weaviate_vectors.clean_named_vector."
        )


def test_helper_import_is_loud_fail_not_wrapped():
    """The helper import is a bare top-level import (LOUD-FAIL): a broken
    vco_lib install surfaces, never silently inline-degrades. Assert neither
    call site wraps the import in a try/except that would swallow ImportError."""
    for path in (_VECTOR_COPY, _PROJECT_INIT):
        src = path.read_text(encoding="utf-8")
        # The import line exists and is not preceded on its own logical block by
        # a `try:` guarding it (cheap heuristic: no `try:` on the import line's
        # own indentation immediately above). We assert the import is at module
        # top level (zero indentation).
        for line in src.splitlines():
            if "from vco_lib.weaviate_vectors import clean_named_vector" in line:
                assert not line.startswith((" ", "\t")), (
                    f"{path.name}: the clean_named_vector import must be a "
                    "top-level LOUD-FAIL import, not indented under a try/except."
                )
                break
        else:
            raise AssertionError(f"{path.name}: import line not found")


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
