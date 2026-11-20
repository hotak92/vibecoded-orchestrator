# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 CG-1: code-graph reads must be property- and time-bounded."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT, REPO_ROOT / "claude_mcp_servers", REPO_ROOT / "templates" / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from claude_mcp_servers.weaviate_mcp import server as srv  # noqa: E402 — after sys.path
from tests.test_v02101_read_path_exact_match import (  # noqa: E402
    PROJ as REF_PROJECT, _fnrow, _RefColl, _structure,
)
from tests.test_v0292_mcp_query_structure import (  # noqa: E402
    _build_class_graph,
    _build_module_graph,
    _run,
)
from vco_lib import codegraph_resync as cr  # noqa: E402
from vco_lib import codegraph_source_purge as csp  # noqa: E402


def _assert_bounded(calls) -> None:
    assert calls, "the path issued no read"
    for call in calls:
        assert call.get("return_properties") is not None, call
        refs = call.get("return_references") or []
        if not isinstance(refs, (list, tuple)):
            refs = [refs]
        for ref in refs:
            assert getattr(ref, "return_properties", None) is not None, call


@pytest.mark.parametrize("query_type,target,builder", [
    ("dependencies", "src/a.py", _build_module_graph),
    ("imports", "src/b.py", _build_module_graph),
    ("extends", "pkg.Child", _build_class_graph),
])
def test_mcp_structure_reads_name_row_and_link_properties(query_type, target, builder):
    built = builder()
    coll = built[0] if isinstance(built, tuple) else built
    payload = _run({coll.query.coll_name: coll}, query_type, target)
    assert payload["success"], payload
    _assert_bounded(coll.query.calls)


class _AuditRefColl(_RefColl):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read_log = []

    def fetch_objects(self, filters=None, limit=100, offset=0,
                      return_references=None, return_properties=None, **kw):
        self.read_log.append({
            "filters": filters, "limit": limit, "offset": offset,
            "return_references": return_references,
            "return_properties": return_properties, **kw,
        })
        return super().fetch_objects(
            filters=filters, limit=limit, offset=offset,
            return_references=return_references,
            return_properties=return_properties, **kw,
        )

    def fetch_object_by_id(self, uuid, return_references=None,
                           return_properties=None, **kw):
        self.read_log.append({
            "return_references": return_references,
            "return_properties": return_properties, **kw,
        })
        return super().fetch_object_by_id(
            uuid, return_references=return_references,
            return_properties=return_properties, **kw,
        )


def test_mcp_path_names_only_the_fields_it_returns(monkeypatch):
    a = _fnrow("ua", "pkg.a", "src/a.py")
    b = _fnrow("ub", "pkg.b", "src/b.py")
    coll = _AuditRefColl([a, b], {"calls": {"ua": [b]}})
    payload = _structure(monkeypatch, {"CodeFunction": coll}, "path", "pkg.a->pkg.b")
    assert payload["count"] == 2, payload
    _assert_bounded(coll.read_log)
    for call in coll.read_log:
        assert "function_body" not in (call.get("return_properties") or [])


def _plain_row(uid, **properties):
    return types.SimpleNamespace(
        uuid=uid, properties={"project": REF_PROJECT, **properties}, references=None)


@pytest.mark.parametrize("query_type,target,base,row", [
    ("methods", "pkg.Child", "CodeClass", _plain_row(
        "c", full_name="pkg.Child", file_path="src/c.py", methods=["run"], chunk_num=0)),
    ("composes", "pkg.Child", "CodeClass", _plain_row(
        "c", full_name="pkg.Child", composes=["pkg.Part"], chunk_num=0)),
    ("composed_by", "pkg.Part", "CodeClass", _plain_row(
        "c", full_name="pkg.Child", file_path="src/c.py", composes=["pkg.Part"], chunk_num=0)),
    ("type_users", "pkg.Part", "CodeFunction", _plain_row(
        "f", full_name="pkg.use", signature="def use(x: Part)", file_path="src/f.py",
        type_uses=["pkg.Part"], chunk_num=0)),
])
def test_remaining_mcp_structure_reads_are_bounded(monkeypatch, query_type, target, base, row):
    original_filter = _structure.__globals__["_TokRefFilter"]
    predicate = _structure.__globals__["_TokPredicate"]

    class ContainsFilter(original_filter):
        @staticmethod
        def by_property(prop):
            built = original_filter.by_property(prop)
            built.contains_any = lambda terms: predicate(
                lambda props: bool(set(props.get(prop) or []) & set(terms)))
            return built

    monkeypatch.setitem(_structure.__globals__, "_TokRefFilter", ContainsFilter)
    coll = _AuditRefColl([row])
    payload = _structure(monkeypatch, {base: coll}, query_type, target)
    assert payload["success"], payload
    _assert_bounded(coll.read_log)


def test_mcp_interaction_reads_are_bounded(monkeypatch):
    fn = _fnrow("f", "pkg.run", "src/f.py")
    fn_coll = _AuditRefColl([fn])
    ix_coll = _AuditRefColl([_plain_row("ix", endpoint="/api")])
    payload = _structure(
        monkeypatch, {"CodeFunction": fn_coll, "CodeInteraction": ix_coll},
        "interactions", "pkg.run")
    assert payload["success"], payload
    _assert_bounded(fn_coll.read_log)
    _assert_bounded(ix_coll.read_log)


@pytest.mark.parametrize("query_type,target,base,row_factory", [
    ("dependencies", "pkg/a.py", "CodeModule", "_module_row"),
    ("extends", "pkg.Child", "CodeClass", "_class_row"),
])
def test_cli_reference_reads_are_bounded(query_type, target, base, row_factory):
    from tests import test_v0292_cli_structure_crossref as cli_test

    qcg = pytest.importorskip("query_code_graph")
    factory = getattr(cli_test, row_factory)
    row = factory(["pkg/b.py"] if base == "CodeModule" else ["pkg.Base"])
    coll = cli_test._Collection([row])
    query = qcg.CodeGraphQuery(project="Alpha")
    query.client = types.SimpleNamespace(
        collections=cli_test._Collections({f"Alpha_{base}": coll}))
    query.query_structure(query_type, target)
    _assert_bounded(coll.query.calls)


class _RecordingCLIColl:
    """Records every read; answers one row so the caller keeps going."""

    def __init__(self, name, rows=()):
        self.name = name
        self.calls = []
        self._rows = list(rows)
        self.query = types.SimpleNamespace(
            fetch_objects=self._fetch_objects,
            fetch_object_by_id=self._fetch_object_by_id,
        )

    def _fetch_objects(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(objects=list(self._rows))

    def _fetch_object_by_id(self, uuid, **kwargs):
        self.calls.append(kwargs)
        return None


class _RecordingCLIClient:
    def __init__(self, colls):
        self.collections = types.SimpleNamespace(get=lambda name: colls[name])


def test_cli_anchor_resolution_names_its_ranking_fields():
    qcg = pytest.importorskip("query_code_graph")
    row = types.SimpleNamespace(uuid="a", properties={
        "full_name": "pkg.mod.run_it", "project": "Alpha", "chunk_num": 0})
    colls = {f"Alpha_{b}": _RecordingCLIColl(f"Alpha_{b}", [row])
             for b in ("CodeFunction", "CodeClass")}
    query = qcg.CodeGraphQuery(project="Alpha")
    query.client = _RecordingCLIClient(colls)
    assert query._resolve_anchor_props("pkg.mod.run_it") is not None
    calls = [c for coll in colls.values() for c in coll.calls]
    _assert_bounded(calls)
    for call in calls:
        assert "function_body" not in call["return_properties"]
        assert "class_body" not in call["return_properties"]


def test_cli_similarity_reference_and_slot_reads_are_bounded():
    qcg = pytest.importorskip("query_code_graph")
    coll = _RecordingCLIColl("Alpha_CodeFunction")
    assert qcg._single_populated_slot(coll, "u") == ""
    _assert_bounded(coll.calls)
    from vco_lib import codegraph_bounded_read as br

    assert br.similarity_reference_properties("CodeFunction") == [
        "full_name", "project", "function_body", "doc"]
    src = (REPO_ROOT / "templates" / "scripts" / "query_code_graph.py").read_text(encoding="utf-8")
    assert "_bounded_read.similarity_reference_properties(collection)" in src


def test_cli_and_mcp_enrichment_reads_share_one_property_list():
    """Siblings and chunk windows: the CLI and the MCP name the SAME fields
    (one home), and never a whole row."""
    from vco_lib import codegraph_bounded_read as br

    for base, body in (("CodeFunction", "function_body"), ("CodeClass", "class_body")):
        props = br.chunk_properties(base)
        assert body in props and "chunk_num" in props and "project" in props
    for path in (REPO_ROOT / "templates" / "scripts" / "query_code_graph.py",
                 REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"):
        src = path.read_text(encoding="utf-8")
        assert "_bounded_read.SIBLING_PROPERTIES" in src, path
        assert "_bounded_read.chunk_properties(base)" in src, path


def test_cli_caller_reads_name_row_and_link_properties(monkeypatch, capsys):
    qcg = pytest.importorskip("query_code_graph")
    from tests.test_v02105_caller_scope import _Client, _Coll, _Filter, _two_root_rows

    coll = _Coll(_two_root_rows())
    calls = []
    original = coll.query.fetch_objects

    def record(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(coll.query, "fetch_objects", record)
    monkeypatch.setattr(qcg, "Filter", _Filter)
    query = qcg.CodeGraphQuery(project=None)
    query.client = _Client(coll)
    query.query_structure("callers", "pkg.model.Model.combine")
    assert "pkg.model.Model.run" in capsys.readouterr().out
    _assert_bounded(calls)


def _load_analyzer():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_cg1_bounded_analyzer", REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _audit_analyzer_world(monkeypatch):
    from tests.test_v0292_analyzer_reference_idempotence import _analyzer, _build_world

    module = _load_analyzer()
    mods, classes, funcs = _build_world()
    classes.rows["c-child"].properties["file_path"] = "pkg/a.py"
    classes.rows["c-base"].properties["file_path"] = "pkg/b.py"
    analyzer = _analyzer(module, mods, classes, funcs)
    logs = {id(c): [] for c in (mods, classes, funcs)}
    for coll in (mods, classes, funcs):
        original_iter = coll.iterator
        original_point = coll.query.fetch_object_by_id

        def record_iter(_coll=coll, _original=original_iter, **kwargs):
            logs[id(_coll)].append(("iterator", kwargs))
            yield from _original(**kwargs)

        def record_point(*args, _coll=coll, _original=original_point, **kwargs):
            logs[id(_coll)].append(("point", args, kwargs))
            return _original(*args, **kwargs)

        monkeypatch.setattr(coll, "iterator", record_iter)
        monkeypatch.setattr(coll.query, "fetch_object_by_id", record_point)
    return analyzer, mods, classes, funcs, logs


def test_analyzer_cache_and_cross_reference_reads_are_bounded(monkeypatch):
    analyzer, mods, classes, funcs, logs = _audit_analyzer_world(monkeypatch)
    stats = analyzer.create_cross_references(changed_files={"pkg/a.py"})
    assert stats["calls"] == 1
    for coll in (mods, classes, funcs):
        _assert_bounded([entry[-1] for entry in logs[id(coll)]])
    class_point_ids = [entry[1][0] for entry in logs[id(classes)] if entry[0] == "point"]
    assert class_point_ids == ["c-child"], "a scoped run read an unchanged file's class"


def test_analyzer_module_gate_names_properties():
    module = _load_analyzer()
    analyzer = module.CodeGraphAnalyzer.__new__(module.CodeGraphAnalyzer)
    analyzer.force_rewalk = False
    analyzer._get_stale_file_set = lambda: frozenset()
    calls = []

    def fetch_objects(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(objects=[])

    analyzer.modules_collection = types.SimpleNamespace(
        query=types.SimpleNamespace(fetch_objects=fetch_objects))
    assert analyzer._get_existing_module("pkg/a.py", "hash") is None
    _assert_bounded(calls)


class _CountColl:
    def __init__(self, name, rows=()):
        self.name = name
        self.rows = list(rows)
        self.aggregate_calls = []
        self.fetch_calls = []
        self.iterator_calls = []
        self.aggregate = types.SimpleNamespace(over_all=self._over_all)
        self.query = types.SimpleNamespace(fetch_objects=self._fetch_objects)

    def _over_all(self, **kwargs):
        self.aggregate_calls.append(kwargs)
        return types.SimpleNamespace(total_count=len(self.rows))

    def _fetch_objects(self, **kwargs):
        self.fetch_calls.append(kwargs)
        return types.SimpleNamespace(objects=list(self.rows)[:kwargs.get("limit", 100)])

    def iterator(self, return_properties=None, **kwargs):
        self.iterator_calls.append({"return_properties": return_properties, **kwargs})
        for row in self.rows:
            yield row


def test_stale_count_replaces_filtered_aggregate_with_bounded_existence_read():
    row = types.SimpleNamespace(uuid="u", properties={"embed_revision": 1})
    coll = _CountColl("P_CodeFunction", [row])
    assert cr._count_stale_in_collection(coll, 2) == 1
    assert coll.aggregate_calls == []
    _assert_bounded(coll.fetch_calls)


def test_current_revision_probe_is_a_one_row_existence_read(monkeypatch):
    row = types.SimpleNamespace(uuid="u", properties={})
    coll = _CountColl("P_CodeFunction", [row])
    monkeypatch.setattr(cr, "_collection_prefix", lambda _name: "P")
    monkeypatch.setattr(cr, "_RESYNC_PROBE_BASES", ("CodeFunction",))
    client = types.SimpleNamespace(collections=types.SimpleNamespace(
        exists=lambda name: name == "P_CodeFunction", get=lambda _name: coll))
    assert cr.has_rows_at_current_revision("Project", current_revision=2, client=client) is True
    assert coll.aggregate_calls == []
    assert coll.fetch_calls[0]["limit"] == 1
    _assert_bounded(coll.fetch_calls)


def test_identity_probe_is_a_one_row_existence_read(monkeypatch):
    row = types.SimpleNamespace(uuid="u", properties={"project": "Old"})
    coll = _CountColl("P_CodeFunction", [row])
    monkeypatch.setattr(cr, "_CODEGRAPH_BASES", ("CodeFunction",))
    monkeypatch.setattr(cr, "_IDENTITY_UNMIGRATABLE_BASES", ())
    client = types.SimpleNamespace(collections=types.SimpleNamespace(
        exists=lambda name: name == "P_CodeFunction", get=lambda _name: coll))
    assert cr._probe_stale_identity_count(client, "P", "P") == 1
    assert coll.aggregate_calls == []
    assert coll.iterator_calls == [], "the probe scanned the whole class"
    assert [c["limit"] for c in coll.fetch_calls] == [1]
    _assert_bounded(coll.fetch_calls)
    empty = _CountColl("P_CodeFunction")
    client_empty = types.SimpleNamespace(collections=types.SimpleNamespace(
        exists=lambda name: name == "P_CodeFunction", get=lambda _name: empty))
    assert cr._probe_stale_identity_count(client_empty, "P", "P") == 0


def _source_client(coll):
    return types.SimpleNamespace(collections=types.SimpleNamespace(
        exists=lambda name: name == coll.name, get=lambda _name: coll))


def test_source_count_replaces_filtered_aggregate(monkeypatch):
    coll = _CountColl("P_CodeModule")
    monkeypatch.setattr(csp._cr, "_collection_prefix", lambda _name: "P")
    assert csp.count_rows_for_project_source(
        "Project", "/gone", bases=("CodeModule",), client=_source_client(coll)) == 0
    assert coll.aggregate_calls == []
    _assert_bounded(coll.fetch_calls)


def test_source_count_positive_needs_no_class_scan(monkeypatch):
    """A present source is told by one bounded page, not a scan of the class
    (the steady state of every enabled extra folder)."""
    rows = [types.SimpleNamespace(uuid=f"u{i}", properties={"project_source": "/x/extra"})
            for i in range(200)]
    coll = _CountColl("P_CodeModule", rows)
    monkeypatch.setattr(csp._cr, "_collection_prefix", lambda _name: "P")
    n = csp.count_rows_for_project_source(
        "Project", "/x/extra", bases=("CodeModule",), client=_source_client(coll))
    assert n and n > 0
    assert coll.iterator_calls == [] and coll.aggregate_calls == []
    assert coll.fetch_calls[0]["return_properties"] == ["project_source"]


def test_source_count_token_sibling_page_is_rechecked_exactly(monkeypatch):
    """A page of token-matching siblings only (never the exact source) is not
    a positive: a short page reads as zero, a full one falls to the exact scan."""
    sibling = [types.SimpleNamespace(uuid="s", properties={"project_source": "/x/extra/sub"})]
    coll = _CountColl("P_CodeModule", sibling)
    monkeypatch.setattr(csp._cr, "_collection_prefix", lambda _name: "P")
    assert csp.count_rows_for_project_source(
        "Project", "/x/extra", bases=("CodeModule",), client=_source_client(coll)) == 0
    assert coll.iterator_calls == []
    full = [types.SimpleNamespace(uuid=f"s{i}", properties={"project_source": "/x/extra/sub"})
            for i in range(csp._SOURCE_COUNT_PAGE)]
    full.append(types.SimpleNamespace(uuid="e", properties={"project_source": "/x/extra"}))
    coll_full = _CountColl("P_CodeModule", full)
    assert csp.count_rows_for_project_source(
        "Project", "/x/extra", bases=("CodeModule",), client=_source_client(coll_full)) == 1
    assert coll_full.iterator_calls == [{"return_properties": ["project_source"]}]


def test_analyzer_stale_probe_replaces_filtered_aggregate():
    module = _load_analyzer()
    analyzer = module.CodeGraphAnalyzer.__new__(module.CodeGraphAnalyzer)
    coll = _CountColl("P_CodeFunction")
    assert analyzer._count_stale_rows_in_collection(coll) == 0
    assert coll.aggregate_calls == []
    _assert_bounded(coll.fetch_calls)


def test_resource_exhausted_logs_collection_and_call_site_and_returns_no_rows(monkeypatch, caplog):
    target = types.SimpleNamespace(uuid="target", properties={
        "full_name": "pkg.target", "project_source": ""})
    calls = []

    class Query:
        def fetch_objects(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return types.SimpleNamespace(objects=[target])
            if getattr(kwargs.get("filters"), "target", "") == "call_names":
                raise RuntimeError("RESOURCE_EXHAUSTED: received message larger than max")
            return types.SimpleNamespace(objects=[])

    coll = types.SimpleNamespace(name="P_CodeFunction", query=Query())
    client = types.SimpleNamespace(collections=types.SimpleNamespace(get=lambda _name: coll))
    monkeypatch.setattr(srv, "get_weaviate_client", lambda: client)
    with caplog.at_level("ERROR"):
        payload = json.loads(srv.query_code_structure("callers", "pkg.target", project=""))
    assert payload["success"] is True and payload["results"] == [], payload
    caller_reads = [c for c in calls if getattr(c.get("filters"), "target", "") == "call_names"]
    assert len(caller_reads) == 1, "the oversized read was retried"
    messages = [r.getMessage() for r in caplog.records if "RESOURCE_EXHAUSTED" in r.getMessage()]
    assert len(messages) == 1, messages
    assert "P_CodeFunction" in messages[0]
    assert "query_code_structure.callers" in messages[0]


def test_cli_resource_exhausted_logs_once_and_answers_empty(monkeypatch, caplog):
    qcg = pytest.importorskip("query_code_graph")
    calls = []

    class Query:
        def fetch_objects(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("RESOURCE_EXHAUSTED: received message larger than max")

    coll = types.SimpleNamespace(name="Alpha_CodeFunction", query=Query())
    with caplog.at_level("ERROR"):
        resp = qcg._structure_fetch_objects(
            coll, "query_structure.callers", limit=50, return_properties=["full_name"])
    assert resp.objects == [] and len(calls) == 1
    messages = [r.getMessage() for r in caplog.records if "RESOURCE_EXHAUSTED" in r.getMessage()]
    assert len(messages) == 1
    assert "Alpha_CodeFunction" in messages[0] and "query_structure.callers" in messages[0]


def test_other_errors_still_raise():
    from vco_lib import codegraph_bounded_read as br

    class Query:
        def fetch_objects(self, **kwargs):
            raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError):
        br.fetch_objects(types.SimpleNamespace(name="c", query=Query()), "site", limit=1)


def test_mcp_and_cli_resolve_links_with_the_same_fields():
    qcg = pytest.importorskip("query_code_graph")
    for link in ("calls", "imports", "extends"):
        cli = qcg._structure_reference(link)
        mcp = srv._code_return_references(link)[0]
        assert list(cli.return_properties) == list(mcp.return_properties)
        assert cli.return_properties, link


def test_path_node_budget_is_reported(monkeypatch):
    rows = [_fnrow(f"u{i}", f"pkg.f{i}", f"src/f{i}.py") for i in range(4)]
    links = {"calls": {f"u{i}": [rows[i + 1]] for i in range(3)}}
    coll = _AuditRefColl(rows, links)
    monkeypatch.setattr(_structure.__globals__["srv"], "_CODE_PATH_NODE_BUDGET", 2, raising=False)
    payload = _structure(monkeypatch, {"CodeFunction": coll}, "path", "pkg.f0->pkg.f3")
    assert payload["path_found"] is False
    assert payload["truncated"] is True
    assert payload["truncation_reason"] == "node_budget"
    assert payload["nodes_visited"] == 2


def test_path_time_budget_is_reported(monkeypatch):
    rows = [_fnrow("ua", "pkg.a", "src/a.py"), _fnrow("ub", "pkg.b", "src/b.py")]
    coll = _AuditRefColl(rows, {"calls": {"ua": [rows[1]]}})
    monkeypatch.setattr(_structure.__globals__["srv"], "_CODE_PATH_TIME_BUDGET_SECONDS", 0.0, raising=False)
    payload = _structure(monkeypatch, {"CodeFunction": coll}, "path", "pkg.a->pkg.b")
    assert payload["path_found"] is False
    assert payload["truncated"] is True
    assert payload["truncation_reason"] == "time_budget"
