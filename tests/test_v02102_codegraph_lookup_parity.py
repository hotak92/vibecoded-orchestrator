# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W4-C L5 — ONE code-graph identity lookup, CLI ≡ MCP.

``full_name`` / ``path`` / ``project`` are word-TOKENIZED, so an ``Equal`` is
a NARROWING read that also returns token-superset entities (``foo.bar`` →
``foo.bar.baz``). The MCP moved to exact-then-tolerant identity reads in
v0.2.101; the CLI kept ``Equal + limit`` at its chunk fetcher, sibling fetch,
anchor resolution and structure lookups — so the two surfaces could answer
the SAME question with DIFFERENT rows (P1 report :157-163).

The fix is ONE shared home — :mod:`vco_lib.codegraph_lookup` — that both
surfaces call. These tests pin:

* the unit behaviour (exactness when an exact row exists, tolerant fallback
  for user-typed spellings, confirmed-only rows for the chunk/sibling
  fetchers);
* L4's tolerant path identity (:func:`names_same_path`) the reverse-imports
  lookup confirms with;
* the PARITY itself: the MCP's ``_code_identity_rows`` and the CLI's
  ``query_structure`` path return the same rows for the same fixture.

In-memory fakes only — no Weaviate, no network.
"""

from __future__ import annotations

import importlib
import json
import sys
import uuid as _uuid
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)



class _CliTokFilter:
    """The tokenized fake plus ``contains_any`` (the callers candidate
    fetch's ``call_names`` clause — list membership, not tokenization).
    Same word-tokenization semantics as the shared `_TokFilter`, kept
    standalone so its wider `by_property` surface type-checks."""

    @staticmethod
    def by_property(name: str):
        return _CliTokProp(name)

    @staticmethod
    def any_of(preds):
        preds = list(preds)
        return _TokPredicate(lambda p: any(pr.matches(p) for pr in preds))


class _CliTokProp:
    def __init__(self, name: str):
        self._name = name

    def equal(self, value):
        name, want = self._name, _tokens(value)

        def _match(p):
            raw = p.get(name)
            return isinstance(raw, str) and want <= _tokens(raw)

        return _TokPredicate(_match)

    def contains_any(self, values):
        name, want = self._name, set(values)

        def _match(p):
            raw = p.get(name)
            return isinstance(raw, list) and bool(set(raw) & want)

        return _TokPredicate(_match)


class _TokPredicate:
    def __init__(self, fn):
        self._fn = fn

    def matches(self, props: dict) -> bool:
        return bool(self._fn(props))

    def __and__(self, other):
        return _TokPredicate(lambda p: self.matches(p) and other.matches(p))


def _tokens(value) -> set:
    import re

    return set(re.findall(r"[^\W_]+", str(value).lower()))
from tests.test_codegraph_cli_readpath_v0270 import _load_cli_module  # noqa: E402
from vco_lib import codegraph_lookup as cgl  # noqa: E402
from vco_lib.weaviate_exact_match import names_same_path  # noqa: E402


class _PagingColl:
    """Minimal collection whose fetch honours limit/offset and the filter."""

    def __init__(self, rows):
        self.rows = rows
        self.query = self
        self.name = "Fake"

    def fetch_objects(self, filters=None, limit=100, offset=0, **kw):
        hits = [
            r for r in self.rows
            if filters is None or filters.matches(r.properties)
        ]
        return SimpleNamespace(objects=hits[offset:offset + limit])


def _fn_row(full_name: str, project: str = "Alpha", **extra):
    props = {
        "full_name": full_name, "name": full_name.rsplit(".", 1)[-1],
        "project": project, "chunk_num": 0, "file_path": "src/a.py",
        "signature": "def x(): ...", "call_names": [],
        **extra,
    }
    return SimpleNamespace(uuid=str(_uuid.uuid4()), properties=props, references=None)


# ─── Unit: the shared home ────────────────────────────────────────────────


def test_exact_identity_wins_over_token_superset_sibling():
    """THE L5 red-proof: a lookup for ``foo.bar`` no longer returns
    ``foo.bar.baz`` (the tokenized ``Equal`` returned it pre-fix)."""
    want = _fn_row("foo.bar")
    coll = _PagingColl([want, _fn_row("foo.bar.baz"), _fn_row("foo_bar")])
    rows = cgl.code_identity_rows(
        coll, "full_name", "foo.bar", "Alpha",
        filter_cls=_CliTokFilter, tolerant_limit=8,
    )
    assert [r.properties["full_name"] for r in rows] == ["foo.bar"]


def test_tolerant_fallback_keeps_user_typed_spellings_resolving():
    """No exact row → the pre-fix tokenized read still answers (availability
    never regresses: ``Foo.Bar`` finds ``foo.bar``)."""
    coll = _PagingColl([_fn_row("foo.bar")])
    rows = cgl.code_identity_rows(
        coll, "full_name", "Foo.Bar", "Alpha",
        filter_cls=_CliTokFilter, tolerant_limit=8,
    )
    assert [r.properties["full_name"] for r in rows] == ["foo.bar"]


def test_project_clause_confirms_by_token_sequence():
    coll = _PagingColl([_fn_row("foo.bar", "Alpha"), _fn_row("foo.bar", "Beta")])
    rows = cgl.code_identity_rows(
        coll, "full_name", "foo.bar", "Alpha",
        filter_cls=_CliTokFilter, tolerant_limit=8,
    )
    assert [r.properties["project"] for r in rows] == ["Alpha"]


def test_confirmed_entity_rows_reject_superset_paths_and_names():
    """The chunk/sibling fetcher shape: confirmed-only, no fallback."""
    want = _fn_row("foo.bar", file_path="src/a.py")
    coll = _PagingColl([
        want,
        _fn_row("foo.bar.baz", file_path="src/a/b.py"),
        _fn_row("foo.bar", file_path="src/a/b.py"),
    ])
    rows = cgl.confirmed_entity_rows(
        coll, filter_cls=_CliTokFilter, project="Alpha",
        full_name="foo.bar", file_path="src/a.py",
    )
    assert [r.properties["file_path"] for r in rows] == ["src/a.py"]


def test_pick_canonical_chunk_prefers_chunk_zero():
    coll_rows = [
        _fn_row("foo.bar", chunk_num=2),
        _fn_row("foo.bar", chunk_num=0),
        _fn_row("foo.bar", chunk_num=1),
    ]
    assert cgl.pick_canonical_chunk(coll_rows).properties["chunk_num"] == 0
    # Legacy no-chunk-num rows still serve; empty list → None.
    legacy = _fn_row("foo.bar")
    legacy.properties.pop("chunk_num")
    assert cgl.pick_canonical_chunk([legacy]) is legacy
    assert cgl.pick_canonical_chunk([]) is None


# ─── L4: the tolerant path identity ──────────────────────────────────────


def test_names_same_path_tolerates_spelling_not_superset_paths():
    assert names_same_path("./a/b.py", "a/b.py")
    assert names_same_path("a\\b\\c.py", "a/b/c.py")
    assert names_same_path("/home/x/repo/src/a.py", "src/a.py")
    assert names_same_path("src/a.py/", "src/a.py")
    assert not names_same_path("a/b.py", "a/b/c.py")
    assert not names_same_path("", "a")
    assert not names_same_path(None, "a")


# ─── Parity: the CLI answers with the same rows the MCP does ─────────────


def test_cli_and_mcp_return_the_same_rows_for_the_same_fixture(monkeypatch, capsys):
    """Both surfaces route through the ONE shared home, so the SAME fixture
    answers identically: the exact row (and never the token-superset
    sibling) on both."""
    import asyncio

    srv = importlib.import_module("weaviate_mcp.server")
    cli = _load_cli_module()
    monkeypatch.setattr(cli, "Filter", _CliTokFilter)
    # The MCP side builds its narrowing filter from the module-global Filter
    # too — swap it for the tokenized fake so the fake collection can judge.
    monkeypatch.setattr(srv, "Filter", _CliTokFilter)

    fixture = [
        _fn_row("foo.bar", project="Alpha", start_line=3),
        _fn_row("foo.bar.baz", project="Alpha", start_line=9),
    ]

    # MCP side: the module's identity-rows entry point.
    mcp_rows = srv._code_identity_rows(
        _PagingColl(list(fixture)), "full_name", "foo.bar", "",
        tolerant_limit=8,
    )

    # CLI side: the real `query_structure("callers", …)` path (target-row
    # fetch) against its own fake client over the SAME rows.
    class _CliColl(_PagingColl):
        pass

    cli_coll = _CliColl(list(fixture))

    class _Client:
        class _Collections:
            def get(self, _name):
                return cli_coll

        collections = _Collections()

    q = cli.CodeGraphQuery(project="Alpha")
    q.client = _Client()
    q.query_structure("callers", "foo.bar")
    out = capsys.readouterr().out
    assert "Found 0 callers" in out  # nobody calls it — but the TARGET resolved

    # And the identity decision agrees row-for-row with the MCP's.
    cli_rows = cli.code_identity_rows(
        cli_coll, "full_name", "foo.bar", "",
        filter_cls=_CliTokFilter, tolerant_limit=8,
    )
    assert [r.properties["full_name"] for r in cli_rows] == [
        r.properties["full_name"] for r in mcp_rows
    ] == ["foo.bar"]
    # The sibling never answers on either surface.
    assert all(r.properties["full_name"] != "foo.bar.baz" for r in cli_rows + mcp_rows)
    # asyncio import stays referenced for future legs of this file.
    assert asyncio is not None


def test_cli_methods_does_not_answer_for_a_token_superset_sibling(
        monkeypatch, capsys):
    """The behavioral red-proof this lane shipped: pre-fix, the CLI's
    ``methods`` lookup answered with the FIRST row of a tokenized ``Equal``
    read — here the sibling ``foo.bar.baz``, sorted first, answered for
    ``foo.bar``."""
    cli = _load_cli_module()
    monkeypatch.setattr(cli, "Filter", _CliTokFilter)

    class _MethodsColl(_PagingColl):
        pass

    coll = _MethodsColl([
        _fn_row("foo.bar.baz", methods=["sibling_method"]),
        _fn_row("foo.bar", methods=["wanted_method"]),
    ])

    class _Client:
        class _Collections:
            def get(self, _name):
                return coll

        collections = _Collections()

    q = cli.CodeGraphQuery(project="Alpha")
    q.client = _Client()
    q.query_structure("methods", "foo.bar")
    out = capsys.readouterr().out
    assert "wanted_method" in out
    assert "sibling_method" not in out


# ─── L4: reverse-imports confirms the tolerant target path ───────────────


def test_reverse_imports_confirms_the_tolerant_target_path(monkeypatch):
    """``query_code_structure("imports", "a/b.py")`` — the by_ref narrowing
    filter reads the TARGET's tokenized ``path``, so it over-matches token
    supersets. Pre-fix the raw response rows were returned unconfirmed:
    an importer of ``a/b/c.py`` answered for ``a/b.py``, and no tolerant
    spelling (``./a/b.py``, absolute, Windows separators) was ever checked.
    Post-fix each importer is confirmed: at least one of its ``imports``
    references NAMES the target under :func:`names_same_path`."""
    import re

    srv = importlib.import_module("weaviate_mcp.server")

    def _tokens(v):
        return set(re.findall(r"[^\W_]+", str(v).lower()))

    class _Pred:
        def __init__(self, fn):
            self._fn = fn

        def matches(self, props):
            return bool(self._fn(props))

        def __and__(self, other):
            return _Pred(lambda p: self.matches(p) and other.matches(p))

    class _Prop:
        def __init__(self, name):
            self._name = name

        def equal(self, value):
            name, want = self._name, _tokens(value)
            return _Pred(
                lambda p: isinstance(p.get(name), str)
                and want <= _tokens(p.get(name))
            )

    class _RefProp:
        def __init__(self, link):
            self._link = link

        def by_property(self, name):
            self._prop = name
            return self

        def equal(self, value):
            # The narrowing semantics of by_ref: the row's cross-referenced
            # TARGET property tokenizes to a superset of the value's tokens.
            want = _tokens(value)

            def _match(props):
                refs = props.get(self._link) or []
                return any(
                    isinstance(r, str) and want <= _tokens(r) for r in refs
                )

            return _Pred(_match)

    class _RefFilter:
        @staticmethod
        def by_property(name):
            return _Prop(name)

        @staticmethod
        def by_ref(link):
            return _RefProp(link)

    def _mod(path, imports):
        props = {"path": path, "project": "Alpha", "imports": list(imports)}
        refs = SimpleNamespace(objects=[
            SimpleNamespace(uuid=f"ref-{i}", properties={"path": p})
            for i, p in enumerate(props["imports"])
        ])
        return SimpleNamespace(
            uuid=f"mod-{path}", properties=props,
            references={"imports": refs},
        )

    rows = [
        _mod("src/tolerant.py", ["./a/b.py"]),    # tolerant spelling — FOUND
        _mod("src/superset.py", ["a/b/c.py"]),    # token superset — REJECTED
        _mod("src/abs.py", ["/repo/a/b.py"]),     # absolute — FOUND (suffix)
        _mod("src/unrelated.py", ["x/y.py"]),     # not an importer
    ]

    class _Coll:
        def __init__(self):
            self.query = self

        def fetch_objects(self, filters=None, limit=100, **kw):
            hits = [r for r in rows if filters is None or filters.matches(r.properties)]
            return SimpleNamespace(objects=hits[:limit])

    monkeypatch.setattr(srv, "get_weaviate_client", lambda: SimpleNamespace(
        collections=SimpleNamespace(get=lambda _n: _Coll()),
    ))
    monkeypatch.setattr(srv, "Filter", _RefFilter)
    monkeypatch.setattr(srv, "CODE_GRAPH_PROJECT", "Alpha")

    tool = srv.query_code_structure
    raw = getattr(tool, "fn", None) or getattr(tool, "__wrapped__", None) or tool
    payload = json.loads(raw("imports", "a/b.py"))
    paths = [r["path"] for r in payload["results"]]
    assert sorted(paths) == ["src/abs.py", "src/tolerant.py"], paths


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(importlib.import_module("pytest").main([__file__]))
