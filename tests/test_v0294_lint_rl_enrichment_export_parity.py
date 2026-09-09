# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.94: server.py's RL re-export surface must be a REAL import statement.

Until v0.2.94 the 44 names extracted into ``weaviate_mcp.rl_enrichment`` (M-1)
were re-exported with a dynamic loop::

    for _name in _RL_ENRICHMENT_EXPORTS:
        globals()[_name] = getattr(_rl_enrichment, _name)

Behaviourally that is fine; statically it is invisible. Ruff read all 17
call-sites of those helpers inside server.py as ``F821 undefined name`` (which
is what kept the repo off a ruff CI gate), pyright could not follow them, and a
TYPO in the inventory tuple would have raised only at runtime — on the first
request that reached the mistyped name, in production, not at startup.

The fix writes the re-export out as one explicit ``from .rl_enrichment import
(...)``. ``_RL_ENRICHMENT_EXPORTS`` survives as the documented inventory, so
these tests pin the two together: the tuple and the import list must name the
SAME 44 things, and every one of them must be bound on ``server`` as the very
object ``rl_enrichment`` defines (the property rl_client's
``from …weaviate_mcp.server import <fn>`` and every ``monkeypatch.setattr(srv,
…)`` depend on).
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_PARENT = REPO_ROOT / "claude_mcp_servers"
SERVER_PY = PKG_PARENT / "weaviate_mcp" / "server.py"
if str(PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(PKG_PARENT))


def _server_ast() -> ast.Module:
    return ast.parse(SERVER_PY.read_text(encoding="utf-8"))


def _exports_tuple(tree: ast.Module) -> list[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_RL_ENRICHMENT_EXPORTS"
            for t in node.targets
        ):
            assert isinstance(node.value, ast.Tuple), (
                "_RL_ENRICHMENT_EXPORTS must stay a literal tuple of names"
            )
            return [ast.literal_eval(e) for e in node.value.elts]
    raise AssertionError("_RL_ENRICHMENT_EXPORTS not found in server.py")


def _rl_import_node(tree: ast.Module) -> ast.ImportFrom:
    hits = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and n.level == 1
        and n.module == "rl_enrichment"
    ]
    assert len(hits) == 1, (
        "server.py must contain exactly ONE `from .rl_enrichment import (...)` "
        f"statement (found {len(hits)})"
    )
    return hits[0]


def test_import_list_matches_the_documented_inventory():
    """The explicit import and the inventory tuple name the same 44 things."""
    tree = _server_ast()
    declared = _exports_tuple(tree)
    imported = [a.name for a in _rl_import_node(tree).names]
    assert imported == declared, (
        "the `from .rl_enrichment import (...)` list drifted from "
        "_RL_ENRICHMENT_EXPORTS. Keep them equal (same names, same order): the "
        "tuple documents the surface, the import is what actually binds it.\n"
        f"only in tuple:  {sorted(set(declared) - set(imported))}\n"
        f"only in import: {sorted(set(imported) - set(declared))}"
    )


def test_every_name_is_an_explicit_reexport_alias():
    """`X as X` — the PEP 484 form ruff and pyright both read as a re-export.

    37 of the 44 are never called from server.py itself; without the redundant
    alias they read as unused imports and the next editor's instinct is to
    delete them, silently breaking rl_client's `from …server import <fn>`.
    """
    bad = [a.name for a in _rl_import_node(_server_ast()).names if a.asname != a.name]
    assert not bad, (
        "every re-exported name must use the explicit `X as X` form so it is "
        f"declared (not merely tolerated) as a re-export: {bad}"
    )


def test_no_dynamic_globals_reexport_loop_remains():
    """A `globals()[name] = …` re-export is what this change removed.

    Structural (AST) check, not a text scan: prose mentioning the old loop in
    the block comment must not satisfy it, and a reintroduced loop must not
    hide behind a passing parity test.
    """
    offenders = []
    for node in ast.walk(_server_ast()):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Call)
                and isinstance(target.value.func, ast.Name)
                and target.value.func.id == "globals"
            ):
                offenders.append(node.lineno)
    assert not offenders, (
        "server.py assigns into globals() at line(s) "
        f"{offenders} — dynamic name binding is invisible to ruff/pyright "
        "(it is why the RL call-sites read as F821). Use a real import."
    )


def test_runtime_bindings_are_the_rl_enrichment_objects():
    """Behaviour half: same names, same objects, still on `server`."""
    srv = importlib.import_module("weaviate_mcp.server")
    rl = importlib.import_module("weaviate_mcp.rl_enrichment")
    missing = [n for n in srv._RL_ENRICHMENT_EXPORTS if not hasattr(srv, n)]
    assert not missing, f"re-export missing from server: {missing}"
    not_identical = [
        n
        for n in srv._RL_ENRICHMENT_EXPORTS
        if getattr(srv, n) is not getattr(rl, n)
    ]
    assert not not_identical, (
        "server.<name> must be the SAME object rl_enrichment defines — a copy "
        "would desync `monkeypatch.setattr(srv, …)` from the running code: "
        f"{not_identical}"
    )
