# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 (defect 6): the resync metadata backfill must ENSURE its target
properties before it probes/patches them.

Root cause under test: the resync spawns the backfill as a detached child that
races the analyzer walk, and the walk's analyzer is what adds missing schema
properties — so the FIRST backfill after a property shipped (``is_test`` via
the spec table, ``file_path`` on CodeAPI/CodeInteraction) probed/patched a
class that lacked the property: WARNING, nothing updated (the same lines in
real logs for months).

Fix under test: ``backfill_codegraph_metadata`` routes through the ONE
property-spec home (``vco_lib.codegraph_schema``) —
``ensure_codegraph_properties`` + ``ensure_codegraph_file_path`` — BEFORE the
halves run. The fakes here ENFORCE the schema (an iterator/update naming an
absent property raises, like real Weaviate's 422), so the act-arms FAIL on
pre-fix code (no ensure → per-collection soft-fail → zero updates).
"""

from __future__ import annotations

import types

import pytest

from vco_lib import codegraph_resync as cr


class _SchemaColl:
    """Weaviate collection fake that ENFORCES its schema: an iterator or
    data.update naming a property absent from the schema raises (the 422 an
    old-schema class produces in real Weaviate — the defect's failure mode)."""

    def __init__(self, name, rows, props, probe_has_null=True):
        self.name = name
        self._rows = rows
        self._props = set(props)
        self.added = []  # property names added via config.add_property
        self.updates = []
        self.iter_calls = 0
        self.query = types.SimpleNamespace(
            fetch_objects=lambda **kw: types.SimpleNamespace(
                objects=[object()] if probe_has_null else []
            )
        )
        self.config = types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(
                properties=[
                    types.SimpleNamespace(name=n) for n in sorted(self._props)
                ]
            ),
            add_property=self._add_property,
        )
        self.data = types.SimpleNamespace(update=self._update)

    def _add_property(self, prop):
        self._props.add(prop.name)
        self.added.append(prop.name)

    def _update(self, uuid, properties):
        missing = set(properties) - self._props
        if missing:
            raise RuntimeError(f"422 no such property: {sorted(missing)}")
        self.updates.append({"uuid": uuid, "properties": properties})

    def iterator(self, return_properties=None, return_references=None):
        self.iter_calls += 1
        missing = set(return_properties or []) - self._props
        if missing:
            raise RuntimeError(f"422 no such property: {sorted(missing)}")
        for i, row in enumerate(self._rows):
            props = {k: v for k, v in row.items() if k != "_references"}
            obj = types.SimpleNamespace(uuid=f"u{i}", properties=props)
            obj.references = row.get("_references", {}) or {}
            yield obj


class _RefObj:
    def __init__(self, properties):
        self.properties = dict(properties)


def _ref(ref_prop, target_prop, path):
    return {
        ref_prop: types.SimpleNamespace(
            objects=[_RefObj({target_prop: path})]
        )
    }


def _client(colls):
    return types.SimpleNamespace(
        collections=types.SimpleNamespace(
            exists=lambda n: n in colls, get=lambda n: colls[n],
        ),
        close=lambda: None,
    )


def _patch_helpers(monkeypatch):
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    monkeypatch.setattr(
        cr, "_resolve_metadata_helpers",
        lambda: (lambda p: p.startswith("tests/"), None),
    )


def test_old_schema_collection_gets_is_test_ensured_then_backfilled(monkeypatch):
    """ACT arm: a CodeFunction class born before `is_test` existed — the
    backfill must ADD the property (through the shared home) and then stamp
    the rows. Pre-fix: the enforcing iterator 422s, the per-collection
    soft-fail logs, counts stay 0 and nothing is added."""
    _patch_helpers(monkeypatch)
    func = _SchemaColl(
        "Proj_CodeFunction",
        [{"file_path": "tests/a.py", "is_test": None, "doc": "",
          "function_body": "x", "language": "python", "chunk_num": 0}],
        props=["file_path", "function_body", "doc", "language", "chunk_num"],
    )
    counts = cr.backfill_codegraph_metadata("Proj", client=_client(
        {"Proj_CodeFunction": func}))
    assert "is_test" in func.added, (
        f"the backfill must ensure is_test before probing; added={func.added}"
    )
    assert counts.get("Proj_CodeFunction") == 1, (
        f"with the property ensured the row must be backfilled; counts={counts}"
    )
    assert func.updates == [
        {"uuid": "u0", "properties": {"is_test": True}}
    ]


def test_old_schema_interaction_gets_file_path_ensured_then_anchored(monkeypatch):
    """ACT arm (anchor half): a CodeInteraction class without the `file_path`
    property — the anchor backfill's PATCH would 422. The backfill must ensure
    file_path (shared home) and then anchor the row from its reference."""
    _patch_helpers(monkeypatch)
    ix = _SchemaColl(
        "Proj_CodeInteraction",
        [{"file_path": None,
          "_references": _ref("source_function", "file_path", "src/caller.py")}],
        props=["project"],
    )
    counts = cr.backfill_codegraph_metadata("Proj", client=_client(
        {"Proj_CodeInteraction": ix}))
    assert "file_path" in ix.added, (
        f"the backfill must ensure file_path before patching; added={ix.added}"
    )
    assert ix.updates == [
        {"uuid": "u0", "properties": {"file_path": "src/caller.py"}}
    ]
    assert counts.get("_file_path_backfilled") == 1


def test_current_schema_collection_is_left_alone(monkeypatch):
    """LEAVE-ALONE arm: a fully-current schema with populated rows — the
    ensure adds nothing and the probe short-circuit skips the scan."""
    _patch_helpers(monkeypatch)
    func = _SchemaColl(
        "Proj_CodeFunction",
        [{"file_path": "src/b.py", "is_test": False}],
        props=["file_path", "is_test", "embed_revision", "chunk_num",
               "total_chunks", "n_callers", "function_body", "doc",
               "language"],
        probe_has_null=False,
    )
    counts = cr.backfill_codegraph_metadata("Proj", client=_client(
        {"Proj_CodeFunction": func}))
    assert func.added == [], "a current schema must not be touched"
    assert func.iter_calls == 0, "probe short-circuit must skip the scan"
    assert counts.get("Proj_CodeFunction") == 0
    assert counts.get("_file_path_backfilled") == 0


def test_ensure_failure_does_not_wedge_the_backfill(monkeypatch):
    """A class whose config read FAILS: the ensure soft-fails with a warning
    and the backfill continues (per-collection soft-degrade, as pre-fix)."""
    _patch_helpers(monkeypatch)

    class _BrokenConfig:
        @property
        def config(self):
            raise RuntimeError("config read failed")

    broken = _BrokenConfig()
    ok = _SchemaColl(
        "Proj_CodeModule",
        [{"path": "tests/m.py", "is_test": None}],
        props=["path", "is_test"],
    )
    counts = cr.backfill_codegraph_metadata("Proj", client=_client(
        {"Proj_CodeFunction": broken, "Proj_CodeModule": ok}))
    assert counts.get("Proj_CodeModule") == 1, (
        "one un-ensurable class must not wedge the others"
    )


def test_file_path_ensure_uses_the_shared_spec_home():
    """The ensure function exists in the ONE property-spec home and covers
    exactly the four file-anchored classes (analyzer twin parity by spec)."""
    from vco_lib.codegraph_schema import (
        CODEGRAPH_FILE_PATH_CLASSES, ensure_codegraph_file_path,
    )

    assert set(CODEGRAPH_FILE_PATH_CLASSES) == {
        "CodeFunction", "CodeClass", "CodeAPI", "CodeInteraction",
    }
    colls = {
        f"Proj_{s}": _SchemaColl(f"Proj_{s}", [], props=[])
        for s in CODEGRAPH_FILE_PATH_CLASSES
    }
    results = ensure_codegraph_file_path(_client(colls), "Proj")
    assert results == {name: "ensured" for name in colls}
    assert all(c.added == ["file_path"] for c in colls.values())
    # idempotent: a second run adds nothing
    results2 = ensure_codegraph_file_path(_client(colls), "Proj")
    assert results2 == {name: "ensured" for name in colls}
    assert all(c.added == ["file_path"] for c in colls.values())


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
