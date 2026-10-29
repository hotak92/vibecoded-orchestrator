# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Config values ↔ code sites (v0.2.105): the ``config_users`` / ``config_of``
structure queries, the ``config`` search scope, the ``all`` scope without
config link rows, and the config-row ranking penalty.

Hermetic: in-memory fake collections with word-tokenized filter semantics (a
narrowing read is a token SUPERSET, as in Weaviate), ``WEAVIATE_URL`` pinned
by the suite's conftest. Fixtures are small synthetic config files shaped like
the real formats — no real project content.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import uuid as _uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests.test_codegraph_cli_readpath_v0270 import _load_cli_module  # noqa: E402
from vco_lib import codegraph_config_query as cq  # noqa: E402
from vco_lib.codegraph_lang import config as cfg  # noqa: E402

ROOT_A = "/work/proj"
ROOT_B = "/work/extra"

TWEAKERS = """AI Tweaks
Prefix groups with ====== items with > and descriptions with //
======Passing Logic
>AimPID_P:1.000000                     //proportional gain of the aim loop
>Min Time To Impact:default[3.0]       //minimum time to impact before a pass
"""

TWEAKERS_B = """AI Tweaks
======Passing Logic
>AimPID_P:9.5
"""

INI = """[GENERAL_OPTIONS]
ggv_file = "ggv.csv"
opt_type = mintime
"""

JSON = """{
  "frictionmap": {"coeffs": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]}
}
"""


# ---------------------------------------------------------------------------
# Fakes: word-tokenized filters over whole row objects
# ---------------------------------------------------------------------------

def _tokens(value: Any) -> set:
    return set(re.findall(r"[^\W_]+", str(value).lower()))


class _Pred:
    def __init__(self, fn: Callable[[Any], bool]) -> None:
        self._fn = fn

    def matches(self, obj: Any) -> bool:
        return bool(self._fn(obj))

    def __and__(self, other: "_Pred") -> "_Pred":
        return _Pred(lambda o: self.matches(o) and other.matches(o))

    def __or__(self, other: "_Pred") -> "_Pred":
        return _Pred(lambda o: self.matches(o) or other.matches(o))


def _props(obj: Any) -> Dict[str, Any]:
    return getattr(obj, "properties", None) or {}


class _Prop:
    def __init__(self, name: str) -> None:
        self.name = name

    def _has(self, obj: Any, value: Any) -> bool:
        raw = _props(obj).get(self.name)
        want = _tokens(value)
        if isinstance(raw, list):
            return any(want <= _tokens(r) for r in raw)
        return isinstance(raw, str) and want <= _tokens(raw)

    def equal(self, value: Any) -> _Pred:
        return _Pred(lambda o: self._has(o, value))

    def not_equal(self, value: Any) -> _Pred:
        return _Pred(lambda o: not self._has(o, value))

    def contains_any(self, values: List[Any]) -> _Pred:
        return _Pred(lambda o: any(self._has(o, v) for v in values))


class _Ref:
    def __init__(self, link: str) -> None:
        self.link = link

    def by_id(self) -> "_Ref":
        return self

    def equal(self, uid: str) -> _Pred:
        link = self.link
        return _Pred(lambda o: any(str(t.uuid) == uid
                                   for t in (getattr(o, "references", None) or {}).get(link, [])))


class FakeFilter:
    @staticmethod
    def by_property(name: str) -> _Prop:
        return _Prop(name)

    @staticmethod
    def by_ref(link: str) -> _Ref:
        return _Ref(link)


def fake_ref(link_on: str, return_properties=None) -> tuple:
    return ("ref", link_on)


class _Meta:
    distance = 0.2
    score = 0.8


class FakeColl:
    def __init__(self, rows: List[Any]) -> None:
        self.rows = rows
        self.query = self
        self.name = "Fake"
        self.near_vector_filters: List[Any] = []

    def fetch_objects(self, filters=None, limit=100, offset=0, **_kw):
        hits = [r for r in self.rows if filters is None or filters.matches(r)]
        return SimpleNamespace(objects=hits[offset:offset + limit])

    def near_vector(self, **kw):
        # Deliberately IGNORES the filter: Weaviate's word-tokenized clauses
        # are only a narrowing read, so the exact scope rule must hold even
        # when a filtered-out row comes back.
        self.near_vector_filters.append(kw.get("filters"))
        return SimpleNamespace(objects=list(self.rows))


def _row(props: Dict[str, Any], refs: Dict[str, List[Any]] | None = None) -> Any:
    return SimpleNamespace(uuid=str(_uuid.uuid4()), properties=props,
                           references=refs or {}, metadata=_Meta(), vector=None)


def _sections(fmt: str, rel: str, text: str, root: str, project: str = "Alpha") -> List[Any]:
    doc = cfg.parse_config(fmt, text)
    return [_row({
        "name": s.name, "full_name": f"{rel}::{s.name}", "file_path": rel,
        "language": fmt, "class_body": "\n".join(cfg.body_line(k) for k in s.keys),
        "methods": [k.path for k in s.keys], "start_line": s.start_line,
        "chunk_num": 0, "project": project, "project_source": root,
    }) for s in doc.sections]


def _fn(full_name: str, path: str, root: str, line: int = 10) -> Any:
    return _row({"full_name": full_name, "name": full_name.rsplit(".", 1)[-1],
                 "file_path": path, "project": "Alpha", "project_source": root,
                 "chunk_num": 0, "start_line": line, "language": "cpp"})


def _link(fn: Any, fmt: str, endpoint: str, raw: str, root: str, conf: str = "high") -> Any:
    return _row({
        "interaction_type": "config", "protocol": fmt, "endpoint": endpoint,
        "raw_target": raw, "confidence": conf, "direction": "outbound",
        "file_path": fn.properties["file_path"], "project": "Alpha", "project_source": root,
    }, {"source_function": [fn]})


class Graph:
    """Two roots: the project (A) and an extra code folder (B) that defines
    the SAME tweaker key with another value."""

    def __init__(self) -> None:
        self.steer_a = _fn("ai.Steer.update", "src/steer.cpp", ROOT_A)
        self.steer_b = _fn("ai.Steer.update", "src/steer.cpp", ROOT_B)
        self.lap = _fn("plan.load_params", "plan/params.py", ROOT_A, line=4)
        self.http = _row({"interaction_type": "http", "protocol": "http",
                          "endpoint": "https://example.invalid/api", "file_path": "src/net.py",
                          "project": "Alpha", "project_source": ROOT_A},
                         {"source_function": [self.lap]})
        self.classes = (
            _sections("tweakers", "data/ai.tweakers", TWEAKERS, ROOT_A)
            + _sections("tweakers", "data/ai.tweakers", TWEAKERS_B, ROOT_B)
            + _sections("ini", "params/racecar.ini", INI, ROOT_A)
            + _sections("json", "params/friction.json", JSON, ROOT_A)
        )
        self.links = [
            _link(self.steer_a, "tweakers", "tweakers:Passing Logic/AimPID_P", "AimPID_P", ROOT_A),
            _link(self.steer_b, "tweakers", "tweakers:Passing Logic/AimPID_P", "AimPID_P", ROOT_B),
            _link(self.lap, "ini", "ini:GENERAL_OPTIONS/ggv_file", "ggv_file", ROOT_A),
            _link(self.lap, "json", "json:frictionmap/coeffs", "coeffs", ROOT_A, conf="medium"),
            # A link whose root defines nothing it reads (cross-root bait).
            _link(_fn("tool.x", "t/x.py", "/work/other"), "tweakers",
                  "tweakers:Passing Logic/AimPID_P", "AimPID_P", "/work/other"),
            self.http,
        ]
        self.colls = {
            "CodeClass": FakeColl(self.classes),
            "CodeInteraction": FakeColl(self.links),
            "CodeFunction": FakeColl([self.steer_a, self.lap]),
            "CodeModule": FakeColl([]),
        }

    def coll_for(self, base: str) -> FakeColl:
        return self.colls[base.rsplit("_", 1)[-1] if "_" in base else base]


def _users(g: Graph, target: str) -> Dict[str, Any]:
    return cq.config_users(g.coll_for, target, "Alpha", filter_cls=FakeFilter, ref_cls=fake_ref)


# ---------------------------------------------------------------------------
# config_users / config_of: the value next to each site
# ---------------------------------------------------------------------------

def test_config_users_shows_the_value_next_to_each_site() -> None:
    g = Graph()
    res = _users(g, "AimPID_P")
    assert res["success"], res
    by_root = {e["project_source"]: e for e in res["results"]}
    a = by_root[ROOT_A]
    assert a["full_name"] == "ai.Steer.update" and a["line"] == 10
    assert a["endpoint"] == "tweakers:Passing Logic/AimPID_P"
    assert [(v["key"], v["value"], v["description"]) for v in a["values"]] == [
        ("AimPID_P", "1.000000", "proportional gain of the aim loop")]
    # The definition list names where the key lives.
    assert {(d["file_path"], d["value"]) for d in res["definitions"]} == {
        ("data/ai.tweakers", "1.000000"), ("data/ai.tweakers", "9.5")}


def test_default_and_description_are_shown() -> None:
    g = Graph()
    link = _link(g.steer_a, "tweakers", "tweakers:Passing Logic/Min Time To Impact",
                 "Min Time To Impact", ROOT_A)
    g.colls["CodeInteraction"].rows.append(link)
    res = _users(g, "Min Time To Impact")
    (entry,) = res["results"]
    (value,) = entry["values"]
    assert value["value"] == "3.0" and value["default"] is True
    assert value["description"] == "minimum time to impact before a pass"


def test_array_value_shows_its_preview_and_length() -> None:
    g = Graph()
    res = _users(g, "coeffs")
    (entry,) = res["results"]
    (value,) = entry["values"]
    assert value["value"].endswith("(len=10)")
    assert value["value"].startswith("[0.1, 0.2")


def test_config_of_lists_every_value_the_function_reads() -> None:
    g = Graph()
    res = cq.config_of(g.coll_for, "plan.load_params", "Alpha",
                       filter_cls=FakeFilter, ref_cls=fake_ref)
    assert res["success"], res
    got = {e["endpoint"]: [v["value"] for v in e["values"]] for e in res["results"]}
    assert got == {
        "ini:GENERAL_OPTIONS/ggv_file": ['"ggv.csv"'],
        "json:frictionmap/coeffs": [got["json:frictionmap/coeffs"][0]],
    }
    # The http interaction of the same function is not a config read.
    assert all(e["endpoint"].startswith(("ini:", "json:")) for e in res["results"])


def test_section_and_file_targets_resolve() -> None:
    g = Graph()
    link = _link(g.lap, "ini", "ini:GENERAL_OPTIONS", "GENERAL_OPTIONS", ROOT_A)
    g.colls["CodeInteraction"].rows.append(link)
    sec = _users(g, "GENERAL_OPTIONS")
    eps = sorted(e["endpoint"] for e in sec["results"])
    # A section target covers the section AND its keys.
    assert eps == ["ini:GENERAL_OPTIONS", "ini:GENERAL_OPTIONS/ggv_file"]
    section_entry = next(e for e in sec["results"] if e["endpoint"] == "ini:GENERAL_OPTIONS")
    assert [k["key"] for k in section_entry["values"][0]["keys"]] == ["ggv_file", "opt_type"]
    by_file = _users(g, "params/racecar.ini")
    assert sorted(e["endpoint"] for e in by_file["results"]) == eps
    by_endpoint = _users(g, "ini:GENERAL_OPTIONS/ggv_file")
    assert [e["endpoint"] for e in by_endpoint["results"]] == ["ini:GENERAL_OPTIONS/ggv_file"]


def test_unknown_target_is_a_clear_error_not_an_empty_success() -> None:
    res = _users(Graph(), "NoSuchKey_42")
    assert res["success"] is False and "NoSuchKey_42" in res["error"]


# ---------------------------------------------------------------------------
# One root: no cross-root results
# ---------------------------------------------------------------------------

def test_each_site_gets_only_its_own_roots_value() -> None:
    g = Graph()
    res = _users(g, "AimPID_P")
    by_root = {e["project_source"]: [v["value"] for v in e["values"]] for e in res["results"]}
    assert by_root == {ROOT_A: ["1.000000"], ROOT_B: ["9.5"]}


def test_a_link_whose_root_defines_nothing_is_not_listed() -> None:
    res = _users(Graph(), "AimPID_P")
    assert "/work/other" not in {e["project_source"] for e in res["results"]}


def test_config_of_stays_in_the_functions_root() -> None:
    g = Graph()
    res = cq.config_of(g.coll_for, "ai.Steer.update", "Alpha",
                       filter_cls=FakeFilter, ref_cls=fake_ref)
    (entry,) = res["results"]
    assert entry["project_source"] == ROOT_A
    assert [v["value"] for v in entry["values"]] == ["1.000000"]


def test_a_chunked_section_reads_every_chunk_and_skips_noise() -> None:
    rows = _sections("ini", "p.ini", INI, ROOT_A)
    head = rows[0].properties
    chunk0 = dict(head, class_body="[chunk 1/2]\n\n[GENERAL_OPTIONS]\nggv_file = \"ggv.csv\"\nopt_type = mint")
    chunk1 = dict(head, class_body="[chunk 2/2]\n\nopt_type = mintime", chunk_num=1)
    (sec,) = cq._sections_from_rows([_row(chunk1), _row(chunk0)])
    assert sec.keys["opt_type"][0] == "mintime"
    assert set(sec.keys) == {"ggv_file", "opt_type"}


# ---------------------------------------------------------------------------
# The MCP and the CLI share the one implementation
# ---------------------------------------------------------------------------

class _Client:
    def __init__(self, g: Graph) -> None:
        self.collections = SimpleNamespace(get=g.coll_for)


def _mcp_tool(srv: Any) -> Callable[..., str]:
    tool = srv.query_code_structure
    return getattr(tool, "fn", None) or getattr(tool, "__wrapped__", None) or tool


def test_mcp_and_cli_agree(monkeypatch, capsys) -> None:
    srv = importlib.import_module("weaviate_mcp.server")
    g = Graph()
    monkeypatch.setattr(srv, "get_weaviate_client", lambda: _Client(g))
    monkeypatch.setattr(srv, "Filter", FakeFilter)
    monkeypatch.setattr(srv, "QueryReference", fake_ref)
    monkeypatch.setattr(srv, "CODE_GRAPH_PROJECT", "Alpha")
    monkeypatch.setattr(srv, "_emit_code_structure_telemetry", lambda **_k: None)
    mcp = json.loads(_mcp_tool(srv)("config_users", "AimPID_P"))
    assert mcp["success"] and mcp["count"] == 2
    assert {v["value"] for e in mcp["results"] for v in e["values"]} == {"1.000000", "9.5"}

    cli = _load_cli_module()
    monkeypatch.setattr(cli, "Filter", FakeFilter)
    monkeypatch.setattr(cli, "QueryReference", fake_ref)
    q = cli.CodeGraphQuery(project="Alpha")
    q.client = _Client(g)
    q.query_structure("config_users", "AimPID_P")
    out = capsys.readouterr().out
    # The CLI prints the shared renderer's text of the SAME result.
    expected = cq.format_config_result(cq.config_users(
        g.coll_for, "AimPID_P", "", filter_cls=FakeFilter, ref_cls=fake_ref))
    assert out.strip() == expected.strip()
    assert "AimPID_P = 1.000000" in out and "AimPID_P = 9.5" in out
    # Same sites, same values on both surfaces.
    mcp_rows = sorted((e["full_name"], e["project_source"], tuple(v["value"] for v in e["values"]))
                      for e in mcp["results"])
    assert mcp_rows == sorted([("ai.Steer.update", ROOT_A, ("1.000000",)),
                               ("ai.Steer.update", ROOT_B, ("9.5",))])


def test_cli_parser_accepts_the_new_query_types() -> None:
    cli = _load_cli_module()
    for qt in cq.CONFIG_QUERY_TYPES:
        args = cli.build_parser().parse_args(["structure", qt, "AimPID_P"])
        assert args.query_type == qt


# ---------------------------------------------------------------------------
# search_code_graph scopes
# ---------------------------------------------------------------------------

def _search_rows() -> Dict[str, List[Any]]:
    fn = _fn("plan.load_params", "plan/params.py", ROOT_A)
    fn.properties.update(signature="def load_params()", total_chunks=1)
    section = _sections("ini", "params/racecar.ini", INI, ROOT_A)[0]
    module = _row({"path": "params/racecar.ini", "language": "ini", "project": "Alpha",
                   "module_summary": "INI config", "project_source": ROOT_A})
    link = _link(fn, "ini", "ini:GENERAL_OPTIONS/ggv_file", "ggv_file", ROOT_A)
    http = _row({"interaction_type": "http", "protocol": "http", "endpoint": "https://x.invalid",
                 "file_path": "src/net.py", "project": "Alpha", "project_source": ROOT_A})
    return {"CodeFunction": [fn], "CodeClass": [section], "CodeModule": [module],
            "CodeAPI": [], "CodeInteraction": [link, http]}


def _search(monkeypatch, scope: str) -> Dict[str, Any]:
    srv = importlib.import_module("weaviate_mcp.server")
    rows = _search_rows()
    colls = {base: FakeColl(r) for base, r in rows.items()}
    monkeypatch.setattr(srv, "_assert_workspace_unchanged", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(srv, "CODE_GRAPH_PROJECT", "Alpha", raising=False)
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", False, raising=False)
    monkeypatch.setattr(srv, "_parse_csv_env", lambda _n: [])
    monkeypatch.setattr(srv, "Filter", FakeFilter)

    async def _embed(_q):
        return [0.1, 0.2]

    monkeypatch.setattr(srv, "get_code_query_embedding", _embed)
    monkeypatch.setattr(srv, "get_weaviate_client", lambda *_a, **_k: SimpleNamespace(
        collections=SimpleNamespace(get=lambda name: colls[name.rsplit("_", 1)[-1]])))
    captured: Dict[str, Any] = {}

    def _pipeline(cands, *_a, **kw):
        captured["config_penalty"] = kw.get("config_penalty")
        return cands

    monkeypatch.setattr(srv, "run_code_retrieval_pipeline", _pipeline)
    data = json.loads(asyncio.run(srv.search_code_graph(query="ggv file", scope=scope, limit=20)))
    assert data.get("success") is not False, data
    data["_captured"] = captured
    return data


def _kinds(data: Dict[str, Any]) -> List[str]:
    out = []
    for r in data["results"]:
        blob = json.dumps(r)
        out.append("link" if "ini:GENERAL_OPTIONS/ggv_file" in blob
                   else "http" if "x.invalid" in blob
                   else "section" if "GENERAL_OPTIONS" in blob
                   else "module" if "INI config" in blob or r.get("file_path") == "params/racecar.ini"
                   else "function")
    return out


def test_scope_all_excludes_config_link_rows(monkeypatch) -> None:
    kinds = _kinds(_search(monkeypatch, "all"))
    assert "link" not in kinds
    assert "http" in kinds and "section" in kinds and "function" in kinds


def test_scope_interaction_keeps_config_link_rows(monkeypatch) -> None:
    kinds = _kinds(_search(monkeypatch, "interaction"))
    assert "link" in kinds and "http" in kinds


def test_scope_config_finds_only_config_rows(monkeypatch) -> None:
    data = _search(monkeypatch, "config")
    kinds = _kinds(data)
    assert "section" in kinds and "link" in kinds
    assert "function" not in kinds and "http" not in kinds
    assert data["_captured"]["config_penalty"] == 0.0


def test_scope_clauses_narrow_the_read() -> None:
    from weaviate_mcp import code_ranking as cr

    link = _link(_fn("a.b", "a.py", ROOT_A), "ini", "ini:S/k", "k", ROOT_A)
    http = _row({"interaction_type": "http"})
    section = _sections("ini", "p.ini", INI, ROOT_A)[0]
    code = _row({"language": "python"})
    all_ix = cr.scope_filter("all", "CodeInteraction", FakeFilter)
    assert not all_ix.matches(link) and all_ix.matches(http)
    cfg_ix = cr.scope_filter("config", "CodeInteraction", FakeFilter)
    assert cfg_ix.matches(link) and not cfg_ix.matches(http)
    cfg_cls = cr.scope_filter("config", "CodeClass", FakeFilter)
    assert cfg_cls.matches(section) and not cfg_cls.matches(code)
    assert cr.scope_filter("code", "CodeClass", FakeFilter) is None
    assert cr.scope_collections("config") == ["CodeModule", "CodeClass", "CodeInteraction"]


# ---------------------------------------------------------------------------
# Ranking: the config penalty and the duplicate-section collapse
# ---------------------------------------------------------------------------

def _cand(props: Dict[str, Any], s: float) -> Dict[str, Any]:
    return {"_s": s, "_p": props}


def test_config_penalty_demotes_config_rows_and_env_overrides(monkeypatch) -> None:
    from weaviate_mcp import code_ranking as cr

    section = {"language": "ini", "name": "S", "class_body": "a = 1", "file_path": "p.ini",
               "full_name": "p.ini::S"}
    code = {"language": "python", "full_name": "m.f", "file_path": "m.py"}
    pool = [_cand(dict(section), 0.60), _cand(dict(code), 0.58)]
    out = cr.run_code_retrieval_pipeline(pool, retrieval_floor=0.1, post_rerank_floor=0.1,
                                         limit=5, env={}, config_penalty=0.05)
    assert [c["_p"]["full_name"] for c in out] == ["m.f", "p.ini::S"]
    assert out[1]["_boost"]["signals"]["is_config_penalty"] is True
    # Leave-alone: penalty 0 keeps pure semantic order, signal absent.
    pool = [_cand(dict(section), 0.60), _cand(dict(code), 0.58)]
    out = cr.run_code_retrieval_pipeline(pool, retrieval_floor=0.1, post_rerank_floor=0.1,
                                         limit=5, env={}, config_penalty=0.0)
    assert [c["_p"]["full_name"] for c in out] == ["p.ini::S", "m.f"]
    assert "is_config_penalty" not in out[0]["_boost"]["signals"]
    assert cr.resolve_config_penalty({"VCO_CODE_GRAPH_CONFIG_PENALTY": "0.07"}) == pytest.approx(0.07)
    assert cr.resolve_config_penalty({"VCO_CODE_GRAPH_CONFIG_PENALTY": ""}) == cr.PENALTY_CONFIG_ENTITY
    assert cr.resolve_config_penalty({}) == cr.PENALTY_CONFIG_ENTITY


def test_config_penalty_spares_a_config_anchor() -> None:
    from weaviate_mcp import code_ranking as cr

    section = {"language": "ini", "name": "S", "class_body": "a = 1"}
    delta, _sig = cr.rerank_score(section, {"language": "json", "name": "T"},
                                  test_penalty=0.0, config_penalty=0.05)
    assert delta == 0.0
    delta, _sig = cr.rerank_score(section, None, test_penalty=0.0, config_penalty=0.05)
    assert delta == pytest.approx(-0.05)


def test_identical_config_sections_in_copied_folders_show_once() -> None:
    from weaviate_mcp import code_ranking as cr

    body = "ggv_file = \"ggv.csv\""
    a = {"language": "ini", "name": "G", "class_body": body, "file_path": "v1/p.ini", "full_name": "v1/p.ini::G"}
    b = dict(a, file_path="v2/p.ini", full_name="v2/p.ini::G")
    c = dict(a, file_path="v3/p.ini", full_name="v3/p.ini::G", class_body="ggv_file = other")
    out = cr.run_code_retrieval_pipeline([_cand(a, 0.6), _cand(b, 0.59), _cand(c, 0.58)],
                                         retrieval_floor=0.1, post_rerank_floor=0.1,
                                         limit=5, env={}, config_penalty=0.0)
    assert [x["_p"]["file_path"] for x in out] == ["v1/p.ini", "v3/p.ini"]


def test_the_shipped_config_penalty_is_the_measured_value():
    """The 0.02 default was MEASURED (2026-10-29, read-only, on a real
    C++/Python tree: 20 code-intent + 10 config-intent queries, scope "all",
    top-5 — 0.02 was the smallest penalty that cut code-result contamination
    without losing config recall; 0.08 dropped recall 7/10 -> 3/10). Changing
    it needs a new measurement, not a guess — this pin makes that visible."""
    from weaviate_mcp.code_ranking import PENALTY_CONFIG_ENTITY

    assert PENALTY_CONFIG_ENTITY == 0.02
