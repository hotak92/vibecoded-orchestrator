# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — the code-graph content hash is a WIRE FORMAT. Lock it.

The per-object ``content_hash`` was extracted out of the 7k-line
``templates/scripts/analyze_code_graph.py`` into
``vco_lib/codegraph_content_hash.py`` so it sits next to its consumer
(``codegraph_guards.classify_row``) and is unit-testable without loading the
template script. The move was verbatim, and it MUST stay verbatim in effect:

Every row in every project's code graph on every install carries a stored
digest produced by the pre-move code. If this rule's output changes by one
byte, EVERY row's stored hash stops matching, the tombstone-skip stops
skipping, and the next walk re-``replace()``s and re-EMBEDS the entire graph —
on this machine alone that is 163,974 live entities across 18 projects. This
repo has already been bitten once by a sidecar hash-scheme mismatch.

So the digests below are GOLDEN. They were captured from the pre-extraction
implementation (verified byte-identical at extraction time across the five
recognised collections, the unknown-collection fallback, and the empty-props
edge). A failure here is never "update the expected value" — it means a change
landed that would re-embed every user's graph, and the change is what has to
go, or ship with a deliberate ``CODEGRAPH_EMBED_REVISION`` migration.
"""
from __future__ import annotations

import pytest

from vco_lib.codegraph_content_hash import (
    _content_hash_for_object,
    _CONTENT_HASH_EXCLUDE,
    _CONTENT_HASH_FIELDS,
    _stable_scalar,
)

# (id, collection, properties, expected sha-256). The digests were produced by
# the PRE-extraction implementation (read out of git HEAD and compared
# case-by-case at extraction time); they are reproduced here so the lock
# survives without depending on git state.
GOLDEN = [
    (
        "function",
        "X_CodeFunction",
        {"full_name": "a.b", "signature": "def b()", "function_body": "return 1",
         "type_uses": ["int", "str"], "chunk_num": 0, "file_path": "a.py",
         "is_test": True, "n_callers": 7, "embed_revision": 1},
        "205958143b7a62bda97778b4d3e165f146e030e6f3a0c663a26a45968c33907d",
    ),
    (
        "class",
        "Y_CodeClass",
        {"full_name": "a.C", "signature": "class C", "class_body": "pass",
         "methods": ["m1", "m2"], "composes": ["D"], "chunk_num": 0},
        "0a25eb9f84675b1222385589c7b4c60ebd46b1165e8a069b256845e357be62d5",
    ),
    (
        "module",
        "Z_CodeModule",
        {"path": "pkg/x.py", "module_summary": "Module: pkg/x.py",
         "import_names": ["os", "sys"], "last_modified": "now"},
        "bb61c69bca308ba4326d296fa76ef21d98d62eb9f8e2fc1f00bce37b0e9ea95a",
    ),
    (
        "api",
        "Q_CodeAPI",
        {"endpoint": "/v1/items", "method": "GET", "api_description": "list",
         "parameters": ["a"], "returns": "list"},
        "b5ea12fe8750798ee6c78b16a7eea15ed5507ee5f0337a97053c752ab5b734e8",
    ),
    (
        "interaction",
        "R_CodeInteraction",
        {"interaction_type": "http", "protocol": "https", "endpoint": "/x",
         "raw_target": "svc", "direction": "out", "description": "d"},
        "c708ace989d4185b1f9398d9509c51128667ca0ec6fac19446e33351de9c4b45",
    ),
    (
        "unknown-collection-fallback",
        "Totally_Unknown",
        {"alpha": 1, "beta": [1, 2], "content_hash": "zz", "is_test": False,
         "gamma": None},
        "0a78f0d42813ae6d7292e49c28404ba6fb03bca71cd963e7845bd196bc285cdd",
    ),
    (
        "empty-props",
        "X_CodeFunction",
        {},
        "059f8dd6cc7b26c4842905d078b163c6c49b740c40a974d74899660dddf3681a",
    ),
]


def _digest(collection: str, props: dict) -> str:
    return _content_hash_for_object(collection, props)


@pytest.mark.parametrize("collection,props,expected",
                         [(g[1], g[2], g[3]) for g in GOLDEN],
                         ids=[g[0] for g in GOLDEN])
def test_golden_digest_is_unchanged(collection, props, expected):
    """If this fails, the change under test would re-embed EVERY row of EVERY
    project's code graph on the next walk. Do not update the constant."""
    assert _digest(collection, props) == expected


@pytest.mark.parametrize("collection,props",
                         [(g[1], g[2]) for g in GOLDEN],
                         ids=[g[0] for g in GOLDEN])
def test_digest_is_deterministic(collection, props):
    """Same input, same digest — across calls and dict orderings."""
    reordered = dict(reversed(list(props.items())))
    assert _digest(collection, props) == _digest(collection, reordered)
    assert _digest(collection, props) == _digest(collection, dict(props))


def test_the_per_project_prefix_is_irrelevant():
    """Digests must not vary by project, or moving/renaming a project would
    re-embed its whole graph."""
    props = {"full_name": "a.b", "signature": "s", "function_body": "b",
             "type_uses": [], "chunk_num": 0}
    a = _digest("Alpha_CodeFunction", props)
    b = _digest("Beta_CodeFunction", props)
    c = _digest("CodeFunction", props)
    assert a == b == c


@pytest.mark.parametrize("field", sorted(_CONTENT_HASH_EXCLUDE))
def test_excluded_fields_never_move_the_digest(field):
    """The exclusion set is what makes a backfill migration (stamping
    ``file_path`` / ``is_test`` / ``embed_revision`` onto old rows) free. If any
    of these started counting, that migration would re-embed every row."""
    base = {"full_name": "a.b", "signature": "s", "function_body": "body",
            "type_uses": ["int"], "chunk_num": 0}
    with_field = dict(base, **{field: "some-value"})
    assert _digest("P_CodeFunction", with_field) == _digest("P_CodeFunction", base)


def test_absent_and_empty_hash_identically():
    """``_stable_scalar(None) == _stable_scalar("")`` is load-bearing: it is
    what lets the v0.2.73 CG-3 tombstone padding (``cfg_summary`` /
    ``data_flow_vars``, still listed but no longer emitted) hash as a constant
    empty string instead of re-hashing every function ever indexed."""
    assert _stable_scalar(None) == _stable_scalar("") == ""
    base = {"full_name": "a.b", "signature": "s", "function_body": "b",
            "type_uses": [], "chunk_num": 0}
    assert _digest("P_CodeFunction", base) == _digest(
        "P_CodeFunction", dict(base, cfg_summary="", data_flow_vars=[]))


def test_cg3_tombstone_padding_is_still_declared():
    """Dropping these two names from the CodeFunction field list would change
    every stored CodeFunction digest at once."""
    assert "cfg_summary" in _CONTENT_HASH_FIELDS["CodeFunction"]
    assert "data_flow_vars" in _CONTENT_HASH_FIELDS["CodeFunction"]


def test_field_lists_are_ordered_and_complete():
    """The hash mixes fields in LIST order, so a reorder is a wire break."""
    assert _CONTENT_HASH_FIELDS["CodeModule"] == [
        "path", "module_summary", "import_names"]
    assert _CONTENT_HASH_FIELDS["CodeClass"] == [
        "full_name", "signature", "class_body", "methods", "composes",
        "chunk_num"]
    assert _CONTENT_HASH_FIELDS["CodeFunction"] == [
        "full_name", "signature", "function_body", "type_uses",
        "cfg_summary", "data_flow_vars", "chunk_num"]
    assert _CONTENT_HASH_FIELDS["CodeAPI"] == [
        "endpoint", "method", "api_description", "parameters", "returns"]
    assert _CONTENT_HASH_FIELDS["CodeInteraction"] == [
        "interaction_type", "protocol", "endpoint", "raw_target", "direction",
        "description"]


def test_a_genuine_content_change_moves_the_digest():
    """The mirror of every test above: the skip must not become universal."""
    base = {"full_name": "a.b", "signature": "s", "function_body": "return 1",
            "type_uses": [], "chunk_num": 0}
    for changed in (
        dict(base, function_body="return 2"),
        dict(base, signature="s2"),
        dict(base, full_name="a.c"),
        dict(base, type_uses=["int"]),
        dict(base, chunk_num=1),
    ):
        assert _digest("P_CodeFunction", changed) != _digest("P_CodeFunction", base)


def test_unknown_collection_falls_back_to_sorted_non_excluded_keys():
    """The fallback errs toward hashing MORE, which can only cause an extra
    (correct) write — never a wrong skip."""
    a = _digest("Totally_Unknown", {"beta": 2, "alpha": 1})
    b = _digest("Totally_Unknown", {"alpha": 1, "beta": 2})
    assert a == b
    assert _digest("Totally_Unknown", {"alpha": 1}) != a


def test_the_analyzer_still_exposes_the_historical_names():
    """The extraction must be invisible to every existing call site and test
    (``tests/test_codegraph_metadata_producers_v0273.py`` reads them off the
    analyzer module)."""
    import importlib.util
    from pathlib import Path

    pytest.importorskip("weaviate")
    src = Path(__file__).resolve().parent.parent / "templates" / "scripts" / \
        "analyze_code_graph.py"
    spec = importlib.util.spec_from_file_location("_acg_hash_parity", str(src))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    assert mod._CONTENT_HASH_FIELDS is _CONTENT_HASH_FIELDS
    assert mod._CONTENT_HASH_EXCLUDE is _CONTENT_HASH_EXCLUDE
    assert mod._content_hash_for_object is _content_hash_for_object
    assert mod._stable_scalar is _stable_scalar
