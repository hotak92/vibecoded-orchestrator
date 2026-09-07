# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — the ``code-graph-query structure`` CLI resolves cross-references.

``templates/scripts/query_code_graph.py`` ships as ``.claude/scripts/
query_code_graph.py`` and backs ``.claude/scripts/code-graph-query structure
dependencies|extends``. Both branches did::

    _refs = response.objects[0].references or {}
    imports = _refs.get("imports", [])
    print(f"   Imports {len(imports)} modules:")
    for imp in imports: ...

The ``or {}`` guard (v0.2.70 C1c) fixed the case where NO references resolve.
It left the SUCCESS path broken: when a link does resolve, ``.get()`` returns a
``weaviate.collections.classes.internal._CrossReference``, and on the installed
client (4.21.0) ``len()`` raises ``TypeError: object of type
'_CrossReference' has no len()`` — before the loop is ever reached. So both
modes failed whenever there was actually something to report, and the CLI
printed ``❌ Structure query error: object of type '_CrossReference' has no
len()`` after its header line.

The fakes below therefore hand back the REAL ``_CrossReference``. A
list-shaped fake is exactly what kept the defect invisible.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_SRC = REPO_ROOT / "templates" / "scripts" / "query_code_graph.py"

# Pin `weaviate_mcp` to THIS checkout before anything imports it: a pip-editable
# install can point at a DIFFERENT orchestrator clone, and the identity check
# below would then compare against that clone's server module.
for _extra in (REPO_ROOT / "claude_mcp_servers", REPO_ROOT):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

_CrossReference = pytest.importorskip(
    "weaviate.collections.classes.internal"
)._CrossReference


@pytest.fixture(scope="module")
def cli_mod() -> types.ModuleType:
    """Import the CLI script as a module (same loader dance as
    tests/test_codegraph_cli_readpath_v0270.py)."""
    for extra in (REPO_ROOT / "claude_mcp_servers", REPO_ROOT):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    spec = importlib.util.spec_from_file_location("_qcg_v0292", CLI_SRC)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class _Row:
    def __init__(self, uuid: str, **props: Any) -> None:
        self.uuid = uuid
        self.properties = dict(props)
        self.references: Any = None


class _Query:
    def __init__(self, objects: list[_Row]) -> None:
        self._objects = objects
        self.calls: list[dict] = []

    def fetch_objects(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(objects=self._objects)


class _Collection:
    def __init__(self, objects: list[_Row]) -> None:
        self.query = _Query(objects)


class _Collections:
    def __init__(self, by_name: dict[str, _Collection]) -> None:
        self._by_name = by_name

    def get(self, name: str) -> _Collection:
        return self._by_name[name]


def _querier(cli_mod, base: str, objects: list[_Row]):
    q = cli_mod.CodeGraphQuery(project="Alpha")
    q.client = types.SimpleNamespace(
        collections=_Collections({f"Alpha_{base}": _Collection(objects)})
    )
    return q


def _module_row(target_paths: list[str] | None) -> _Row:
    row = _Row("m-a", path="pkg/a.py")
    if target_paths is not None:
        row.references = {
            "imports": _CrossReference._from(
                [_Row(f"m-{i}", path=p) for i, p in enumerate(target_paths)]
            )
        }
    return row


def _class_row(base_names: list[str] | None) -> _Row:
    row = _Row("c-child", full_name="pkg.Child")
    if base_names is not None:
        row.references = {
            "extends": _CrossReference._from(
                [_Row(f"c-{i}", full_name=n, name=n.rsplit(".", 1)[-1])
                 for i, n in enumerate(base_names)]
            )
        }
    return row


# --------------------------------------------------------------------------
# dependencies
# --------------------------------------------------------------------------
def test_dependencies_resolves_a_real_cross_reference(cli_mod, capsys) -> None:
    q = _querier(cli_mod, "CodeModule", [_module_row(["pkg/b.py"])])
    q.query_structure("dependencies", "pkg/a.py")
    out = capsys.readouterr()

    assert "Structure query error" not in (out.out + out.err), (
        "the _CrossReference read path still raises"
    )
    assert "Imports 1 modules" in out.out
    assert "pkg/b.py" in out.out


def test_dependencies_collapses_duplicate_beacons(cli_mod, capsys) -> None:
    """Live data carries up to 1518 ``imports`` beacons on one module; the
    answer to "what does pkg/a.py import?" must be one line per module."""
    q = _querier(cli_mod, "CodeModule", [_module_row(["pkg/b.py"] * 66)])
    q.query_structure("dependencies", "pkg/a.py")
    out = capsys.readouterr().out

    assert "Imports 1 modules" in out
    assert out.count("pkg/b.py") == 1


def test_dependencies_keeps_distinct_targets(cli_mod, capsys) -> None:
    q = _querier(
        cli_mod,
        "CodeModule",
        [_module_row(["pkg/b.py", "pkg/c.py", "pkg/b.py"])],
    )
    q.query_structure("dependencies", "pkg/a.py")
    out = capsys.readouterr().out

    assert "Imports 2 modules" in out
    assert "pkg/b.py" in out and "pkg/c.py" in out


def test_dependencies_with_no_references_is_zero_not_an_error(cli_mod, capsys) -> None:
    """The v0.2.70 C1c case must keep working — this is the LEAVE-ALONE half."""
    q = _querier(cli_mod, "CodeModule", [_module_row(None)])
    q.query_structure("dependencies", "pkg/a.py")
    out = capsys.readouterr()

    assert "Structure query error" not in (out.out + out.err)
    assert "Imports 0 modules" in out.out


def test_dependencies_with_an_empty_wrapper_is_zero(cli_mod, capsys) -> None:
    """An empty ``_CrossReference`` is TRUTHY — a naive ``if refs:`` guard
    would sail past it into the broken path."""
    q = _querier(cli_mod, "CodeModule", [_module_row([])])
    q.query_structure("dependencies", "pkg/a.py")
    out = capsys.readouterr()

    assert "Structure query error" not in (out.out + out.err)
    assert "Imports 0 modules" in out.out


def test_dependencies_requests_the_imports_link(cli_mod) -> None:
    """The fix must not quietly stop asking Weaviate to resolve the link."""
    coll = _Collection([_module_row(["pkg/b.py"])])
    q = cli_mod.CodeGraphQuery(project="Alpha")
    q.client = types.SimpleNamespace(
        collections=_Collections({"Alpha_CodeModule": coll})
    )
    q.query_structure("dependencies", "pkg/a.py")

    sent = coll.query.calls[0]["return_references"]
    assert getattr(sent, "link_on", None) == "imports"


# --------------------------------------------------------------------------
# extends
# --------------------------------------------------------------------------
def test_extends_resolves_a_real_cross_reference(cli_mod, capsys) -> None:
    q = _querier(cli_mod, "CodeClass", [_class_row(["pkg.Base"])])
    q.query_structure("extends", "pkg.Child")
    out = capsys.readouterr()

    assert "Structure query error" not in (out.out + out.err)
    assert "Extends 1 classes" in out.out
    assert "pkg.Base" in out.out


def test_extends_collapses_duplicate_beacons(cli_mod, capsys) -> None:
    """506 identical ``extends`` beacons were observed on a single live row."""
    q = _querier(cli_mod, "CodeClass", [_class_row(["pkg.Base"] * 506)])
    q.query_structure("extends", "pkg.Child")
    out = capsys.readouterr().out

    assert "Extends 1 classes" in out
    assert out.count("pkg.Base") == 1


def test_extends_with_no_references_is_zero_not_an_error(cli_mod, capsys) -> None:
    q = _querier(cli_mod, "CodeClass", [_class_row(None)])
    q.query_structure("extends", "pkg.Child")
    out = capsys.readouterr()

    assert "Structure query error" not in (out.out + out.err)
    assert "Extends 0 classes" in out.out


def test_extends_requests_the_extends_link(cli_mod) -> None:
    coll = _Collection([_class_row(["pkg.Base"])])
    q = cli_mod.CodeGraphQuery(project="Alpha")
    q.client = types.SimpleNamespace(
        collections=_Collections({"Alpha_CodeClass": coll})
    )
    q.query_structure("extends", "pkg.Child")

    sent = coll.query.calls[0]["return_references"]
    assert getattr(sent, "link_on", None) == "extends"


# --------------------------------------------------------------------------
# One home, not a third copy
# --------------------------------------------------------------------------
def test_cli_uses_the_shared_reference_helpers(cli_mod) -> None:
    """Identity, not equality: a per-surface fork of the normalisation is what
    let the MCP and the CLI drift apart in the first place."""
    from vco_lib import codegraph_references as shared

    assert cli_mod.normalize_reference_targets is shared.normalize_reference_targets
    assert cli_mod.dedup_ref_targets is shared.dedup_ref_targets


def test_mcp_uses_the_same_shared_helpers() -> None:
    from vco_lib import codegraph_references as shared
    from weaviate_mcp import server as mcp_server

    assert mcp_server._read_cross_reference is shared.read_cross_reference
    assert mcp_server._dedup_ref_targets is shared.dedup_ref_targets


def test_no_open_coded_len_over_a_reference_value() -> None:
    """Static guard: neither structure branch may go back to calling ``len()``
    / iterating straight off a ``.get()`` result."""
    src = CLI_SRC.read_text(encoding="utf-8")
    for bad in (
        '_refs.get("imports", [])',
        '(response.objects[0].references or {}).get("extends", [])',
    ):
        assert bad not in src, (
            f"{bad} is the exact pre-v0.2.92 defect — the value is a "
            f"_CrossReference, normalise it via vco_lib.codegraph_references"
        )
