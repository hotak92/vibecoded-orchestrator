# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — ``vco_lib.codegraph_references``, the one home for reading a
Weaviate cross-reference and for writing only the edges not stored yet.

Every branch that can meet a resolved reference is exercised against the REAL
``weaviate.collections.classes.internal._CrossReference``, not a stand-in.
That is the whole point of this file: the original defect survived a first
round of fixing precisely because the tests fed list-shaped fakes while
production returned a ``_CrossReference``, whose ``len()`` and ``iter()``
both raise ``TypeError`` and whose ``bool()`` is always ``True``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.codegraph_references import (  # noqa: E402
    NON_PROJECT_BASE_CLASSES,
    add_missing_reference_edges,
    build_module_name_index,
    build_short_name_index,
    dedup_ref_targets,
    normalize_reference_targets,
    read_cross_reference,
    reference_target_uuids,
    resolve_base_class_targets,
    resolve_import_target_path,
)

_CrossReference = pytest.importorskip(
    "weaviate.collections.classes.internal"
)._CrossReference


class _Row:
    """Minimal stand-in for a weaviate ``Object`` (uuid + properties)."""

    def __init__(self, uuid: str, **props: Any) -> None:
        self.uuid = uuid
        self.properties = dict(props)


class _Holder:
    """Object carrying a ``references`` mapping, like a fetched row."""

    def __init__(self, references: Any) -> None:
        self.references = references


# ---------------------------------------------------------------------------
# The premise: what the REAL wrapper does. If any of these ever stop holding,
# the shape-normalisation below is answering a question that no longer exists
# and this file should be revisited rather than deleted silently.
# ---------------------------------------------------------------------------
def test_real_cross_reference_has_no_len_and_is_not_iterable() -> None:
    cross = _CrossReference._from([_Row("u1", path="a.py")])
    with pytest.raises(TypeError):
        len(cross)
    with pytest.raises(TypeError):
        iter(cross)
    # The trap that makes a hand-rolled `if cross:` guard look correct: an
    # EMPTY wrapper is still truthy.
    assert bool(_CrossReference._from([])) is True


# ---------------------------------------------------------------------------
# normalize_reference_targets — one branch per shape, real type where it exists
# ---------------------------------------------------------------------------
def test_normalise_none_is_empty() -> None:
    assert normalize_reference_targets(None) == []


def test_normalise_real_cross_reference_returns_its_objects() -> None:
    targets = [_Row("u1", path="a.py"), _Row("u2", path="b.py")]
    assert normalize_reference_targets(_CrossReference._from(targets)) == targets


def test_normalise_empty_real_cross_reference_is_empty() -> None:
    assert normalize_reference_targets(_CrossReference._from([])) == []


def test_normalise_cross_reference_constructed_with_none_is_empty() -> None:
    """The client's own ``.objects`` property coerces a None payload to []."""
    assert normalize_reference_targets(_CrossReference(None)) == []


def test_normalise_plain_list_passes_through() -> None:
    targets = [_Row("u1", path="a.py")]
    out = normalize_reference_targets(targets)
    assert out == targets
    assert out is not targets, "must return a copy, not alias the caller's list"


def test_normalise_tuple_passes_through_as_list() -> None:
    target = _Row("u1", path="a.py")
    assert normalize_reference_targets((target,)) == [target]


def test_normalise_unknown_shape_is_empty_not_an_error() -> None:
    assert normalize_reference_targets(object()) == []
    assert normalize_reference_targets("not-a-reference") == []


# ---------------------------------------------------------------------------
# read_cross_reference — the None-guard fused with the shape-guard
# ---------------------------------------------------------------------------
def test_read_resolves_through_the_real_wrapper() -> None:
    target = _Row("u1", path="a.py")
    holder = _Holder({"imports": _CrossReference._from([target])})
    assert read_cross_reference(holder, "imports") == [target]


def test_read_none_references_is_empty() -> None:
    assert read_cross_reference(_Holder(None), "imports") == []


def test_read_object_without_a_references_attribute_is_empty() -> None:
    assert read_cross_reference(object(), "imports") == []


def test_read_link_that_was_not_requested_is_empty() -> None:
    holder = _Holder({"extends": _CrossReference._from([_Row("u1")])})
    assert read_cross_reference(holder, "imports") == []


def test_read_empty_real_wrapper_is_empty() -> None:
    holder = _Holder({"imports": _CrossReference._from([])})
    assert read_cross_reference(holder, "imports") == []


def test_read_plain_list_still_supported() -> None:
    target = _Row("u1", path="a.py")
    assert read_cross_reference(_Holder({"imports": [target]}), "imports") == [target]


def test_read_non_mapping_references_is_empty() -> None:
    """A shape surprise degrades to "no links", never an AttributeError."""
    assert read_cross_reference(_Holder(["not", "a", "mapping"]), "imports") == []


# ---------------------------------------------------------------------------
# reference_target_uuids — the write side's question
# ---------------------------------------------------------------------------
def test_target_uuids_from_the_real_wrapper() -> None:
    holder = _Holder(
        {"imports": _CrossReference._from([_Row("u1"), _Row("u2")])}
    )
    assert reference_target_uuids(holder, "imports") == ["u1", "u2"]


def test_target_uuids_preserves_duplicate_beacons() -> None:
    """A faithful read of what is STORED — the caller tests membership, and
    collapsing here would hide the duplication from any future audit."""
    holder = _Holder(
        {"imports": _CrossReference._from([_Row("u1"), _Row("u1"), _Row("u1")])}
    )
    assert reference_target_uuids(holder, "imports") == ["u1", "u1", "u1"]


def test_target_uuids_are_strings_even_for_uuid_objects() -> None:
    import uuid as _uuid

    real = _uuid.uuid4()
    holder = _Holder({"calls": _CrossReference._from([_Row(real)])})
    out = reference_target_uuids(holder, "calls")
    assert out == [str(real)]
    assert all(isinstance(x, str) for x in out)


def test_target_uuids_skips_rows_without_a_uuid() -> None:
    class _NoUuid:
        properties: dict = {}

    holder = _Holder({"calls": _CrossReference._from([_NoUuid(), _Row("u2")])})
    assert reference_target_uuids(holder, "calls") == ["u2"]


def test_target_uuids_none_references_is_empty() -> None:
    assert reference_target_uuids(_Holder(None), "imports") == []


# ---------------------------------------------------------------------------
# dedup_ref_targets — the read-side collapse of stored duplicate beacons
# ---------------------------------------------------------------------------
def test_dedup_collapses_duplicate_beacons_from_a_real_wrapper() -> None:
    dup = [_Row(f"u{i}", path="pkg/b.py") for i in range(66)]
    targets = normalize_reference_targets(_CrossReference._from(dup))
    kept = dedup_ref_targets(targets, ("path",))
    assert [o.properties["path"] for o in kept] == ["pkg/b.py"]


def test_dedup_keeps_distinct_targets_in_first_seen_order() -> None:
    objs = [
        _Row("u1", path="b.py"),
        _Row("u2", path="a.py"),
        _Row("u3", path="b.py"),
    ]
    assert [o.properties["path"] for o in dedup_ref_targets(objs, ("path",))] == [
        "b.py",
        "a.py",
    ]


def test_dedup_falls_back_to_the_next_identity_property() -> None:
    objs = [_Row("u1", name="Base"), _Row("u2", name="Base")]
    kept = dedup_ref_targets(objs, ("full_name", "name"))
    assert [o.uuid for o in kept] == ["u1"]


def test_dedup_never_merges_rows_lacking_every_identity_property() -> None:
    objs = [_Row("u1"), _Row("u2")]
    assert len(dedup_ref_targets(objs, ("path",))) == 2


def test_dedup_survives_a_row_whose_properties_raise() -> None:
    class _Hostile:
        @property
        def properties(self):
            raise RuntimeError("boom")

    kept = dedup_ref_targets([_Hostile(), _Row("u1", path="a.py")], ("path",))
    assert len(kept) == 2


# ---------------------------------------------------------------------------
# add_missing_reference_edges — the write side (act AND leave-alone)
# ---------------------------------------------------------------------------
class _RecordingCollection:
    def __init__(self) -> None:
        self.added: list[tuple] = []
        outer = self

        class _Data:
            @staticmethod
            def reference_add(from_uuid, from_property, to):
                outer.added.append((str(from_uuid), from_property, str(to)))

        self.data = _Data()


def test_add_creates_every_edge_when_nothing_is_stored() -> None:
    coll = _RecordingCollection()
    n = add_missing_reference_edges(coll, "src", "imports", ["t1", "t2"])
    assert n == 2
    assert coll.added == [("src", "imports", "t1"), ("src", "imports", "t2")]


def test_add_writes_nothing_when_every_edge_is_already_stored() -> None:
    """The LEAVE-ALONE half: a re-analyze that changes nothing must not write.

    This is the defect itself — `data.reference_add` is not idempotent, so the
    pre-fix loop grew the stored set on every pass, forever.
    """
    coll = _RecordingCollection()
    n = add_missing_reference_edges(
        coll, "src", "imports", ["t1", "t2"], existing=["t1", "t2"]
    )
    assert n == 0
    assert coll.added == []


def test_add_writes_only_the_genuinely_new_edge() -> None:
    coll = _RecordingCollection()
    n = add_missing_reference_edges(
        coll, "src", "imports", ["t1", "t2"], existing=["t1"]
    )
    assert n == 1
    assert coll.added == [("src", "imports", "t2")]


def test_add_collapses_the_intra_pass_repeat() -> None:
    """Two import statements in one file can resolve to the same module row."""
    coll = _RecordingCollection()
    n = add_missing_reference_edges(coll, "src", "imports", ["t1", "t1", "t1"])
    assert n == 1
    assert coll.added == [("src", "imports", "t1")]


def test_add_tolerates_duplicate_entries_in_existing() -> None:
    """`existing` comes straight from the stored beacons, which may already be
    duplicated 66 times — membership is what matters, not multiplicity."""
    coll = _RecordingCollection()
    n = add_missing_reference_edges(
        coll, "src", "imports", ["t1"], existing=["t1"] * 66
    )
    assert n == 0
    assert coll.added == []


def test_add_compares_uuid_objects_as_strings() -> None:
    import uuid as _uuid

    real = _uuid.uuid4()
    coll = _RecordingCollection()
    n = add_missing_reference_edges(
        coll, "src", "calls", [real], existing=[str(real)]
    )
    assert n == 0, "a uuid object and its str form are the same edge"


def test_add_propagates_client_errors_to_the_callers_soft_fail() -> None:
    """The analyzer wraps each call site in its own try/except and logs; the
    helper must not swallow the error itself."""

    class _Boom:
        class data:  # noqa: N801
            @staticmethod
            def reference_add(**_kw):
                raise RuntimeError("weaviate down")

    with pytest.raises(RuntimeError):
        add_missing_reference_edges(_Boom(), "src", "imports", ["t1"])


# ---------------------------------------------------------------------------
# Edge RESOLUTION — moved out of analyze_code_graph.py so it can be tested at
# all. Behaviour must stay byte-for-byte what the analyzer did inline.
# ---------------------------------------------------------------------------
def test_short_name_index_groups_by_last_segment() -> None:
    index = build_short_name_index(["pkg.mod.helper", "other.helper", "solo"])
    assert index == {
        "helper": ["pkg.mod.helper", "other.helper"],
        "solo": ["solo"],
    }


def test_module_name_index_indexes_stem_and_dotted_tail() -> None:
    index = build_module_name_index(["src/foo/bar.py", "top.py"])
    assert index["bar"] == ["src/foo/bar.py"]
    assert index["foo.bar"] == ["src/foo/bar.py"]
    assert index["top"] == ["top.py"]
    # A single-segment path has no dotted form to index.
    assert "top.top" not in index


def test_resolve_import_prefers_the_name_as_written() -> None:
    index = {"foo.bar": ["src/foo/bar.py"], "bar": ["other/bar.py"]}
    assert resolve_import_target_path("foo.bar", index) == "src/foo/bar.py"


def test_resolve_import_falls_back_to_the_last_component() -> None:
    index = {"bar": ["src/foo/bar.py"]}
    assert resolve_import_target_path("pkg.deep.bar", index) == "src/foo/bar.py"


def test_resolve_import_unknown_name_is_none() -> None:
    assert resolve_import_target_path("os.path", {}) is None


def test_resolve_bases_exact_match_wins() -> None:
    cache = {"pkg.Base": "u-base", "Base": "u-shadow"}
    assert resolve_base_class_targets(
        "class Child(Base)", cache, {"Base": ["pkg.Base"]}
    ) == ["u-shadow"], "a base named exactly as cached resolves without the index"


def test_resolve_bases_short_name_match() -> None:
    cache = {"pkg.Base": "u-base"}
    assert resolve_base_class_targets(
        "class Child(Base)", cache, build_short_name_index(cache)
    ) == ["u-base"]


def test_resolve_bases_skips_non_project_bases() -> None:
    cache = {"pkg.Base": "u-base"}
    index = build_short_name_index(cache)
    got = resolve_base_class_targets(
        "class Child(object, Enum, unittest.TestCase, Base)", cache, index
    )
    assert got == ["u-base"]
    for skipped in ("object", "Enum", "unittest.TestCase"):
        assert skipped in NON_PROJECT_BASE_CLASSES


def test_resolve_bases_unknown_base_is_dropped_not_an_error() -> None:
    assert resolve_base_class_targets("class Child(Nowhere)", {}, {}) == []


def test_resolve_bases_signature_without_parens_is_empty() -> None:
    assert resolve_base_class_targets("class Child", {"x": "u"}, {}) == []
    assert resolve_base_class_targets("", {}, {}) == []
    assert resolve_base_class_targets(None, {}, {}) == []


def test_resolve_bases_keeps_repeats_for_the_writer_to_collapse() -> None:
    """Resolution stays a faithful "what does the signature name?"; collapsing
    is add_missing_reference_edges' job (one concern, one home)."""
    cache = {"pkg.Base": "u-base"}
    index = build_short_name_index(cache)
    assert resolve_base_class_targets(
        "class Child(Base, pkg.Base)", cache, index
    ) == ["u-base", "u-base"]
