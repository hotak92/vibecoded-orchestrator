# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-M — the hook KG producer runs the server's own KG code, light.

The KG query/format layer moved verbatim from ``weaviate_mcp/server.py`` into
``weaviate_mcp/kg_query_core.py``; the hook producer reaches it through
``kg_host.host()`` and talks to Weaviate over plain HTTP (``kg_http``) instead
of importing weaviate-client + the MCP SDK (~0.7 s per prompt).

What is pinned here:

* ONE definition, server semantics kept — every moved function on ``server``
  is the core code object rebound to ``server``'s globals, so a patch on
  ``server`` reaches the moved code (the suite's most-patched surface);
* the host resolver — server when one is loaded, the light host otherwise;
* the HTTP adapter's query construction (filters typed from the schema the
  way the server coerces a gRPC filter) and its result typing (what
  weaviate-client v4 returns: aware datetimes, UUIDs, float32-exact
  vectors/distances, nested objects);
* END-TO-END parity: the same canned collection served to
  ``rl_kg_search.main()`` once through the server path (weaviate-client-shaped
  fake, in-process) and once through the light path (a real HTTP fake
  Weaviate, in a fresh interpreter with NO server module) yields byte-identical
  hook output — and the light run never imports weaviate, the MCP SDK or the
  server.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import textwrap
import threading
import types
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
for _p in (str(REPO_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests.common.child_env import child_env  # noqa: E402

pytest.importorskip("weaviate_mcp.server")
from weaviate_mcp import kg_host, kg_http  # noqa: E402


def _live(name: str):
    """The module CURRENTLY registered under ``weaviate_mcp.<name>``. Other
    suites purge + re-import the server (which reloads kg_query_core), so a
    module object captured at collection time can be a stale one."""
    import importlib

    return importlib.import_module(f"weaviate_mcp.{name}")


class _LiveModule:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr):
        return getattr(_live(self._name), attr)


srv = _LiveModule("server")
kg_query_core = _LiveModule("kg_query_core")

# ─── shared canned collection ─────────────────────────────────────────────

CLASS = "Acme_KnowledgeGraph"  # a FIXTURE_PROJECT_NAMES stem
SLOT = "qwen3_embed"
QVEC = [0.25, -0.5, 0.125, 0.0625]


def _vec(seed: float) -> list:
    # Values that are NOT exact in float32, so a transport that loses the
    # float32 rounding shows up as a different emb / score.
    return [seed + 0.1, seed - 0.3, seed * 0.7, 0.03096459]


ROWS = [
    {"id": "aaaaaaaa-0000-4000-8000-000000000001", "title": "Alpha node",
     "file_path": "knowledge/concepts/alpha.md", "node_type": "concept",
     "content": "[chunk 1/2]\n\n" + "Alpha first chunk body. " * 20,
     "chunk_num": 1, "total_chunks": 2,
     "source_node_id": "11111111-1111-4111-8111-111111111111",
     "created_at": "2026-01-15T10:30:00.278513Z", "tags": ["alpha"], "links": ["Beta node"],
     "typed_links": [{"target_title": "Beta node", "relation_type": "relatedTo"}],
     "distance": 0.19999999, "vec": _vec(0.11), "hit": True},
    {"id": "aaaaaaaa-0000-4000-8000-000000000002", "title": "Alpha node",
     "file_path": "knowledge/concepts/alpha.md", "node_type": "concept",
     "content": "[chunk 2/2]\n\nAlpha second chunk body.",
     "chunk_num": 2, "total_chunks": 2,
     "source_node_id": "11111111-1111-4111-8111-111111111111",
     "created_at": "2026-01-15T10:30:00.278513Z", "tags": ["alpha"], "links": [],
     "typed_links": [], "distance": None, "vec": _vec(0.12), "hit": False},
    {"id": "bbbbbbbb-0000-4000-8000-000000000003", "title": "Beta node",
     "file_path": "knowledge/concepts/beta.md", "node_type": "pattern",
     "content": "Beta is a single-chunk node. " * 3,
     "chunk_num": 1, "total_chunks": 1,
     "source_node_id": "22222222-2222-4222-8222-222222222222",
     "created_at": "2026-02-01T00:00:00Z", "tags": [], "links": [],
     "typed_links": None, "distance": 0.4, "vec": _vec(0.4), "hit": True},
]

SCHEMA = {
    "class": CLASS,
    "properties": [
        {"name": "title", "dataType": ["text"]},
        {"name": "file_path", "dataType": ["text"]},
        {"name": "node_type", "dataType": ["text"]},
        {"name": "content", "dataType": ["text"]},
        {"name": "chunk_num", "dataType": ["number"]},
        {"name": "total_chunks", "dataType": ["number"]},
        {"name": "source_node_id", "dataType": ["uuid"]},
        {"name": "created_at", "dataType": ["date"]},
        {"name": "tags", "dataType": ["text[]"]},
        {"name": "links", "dataType": ["text[]"]},
        {"name": "typed_links", "dataType": ["object[]"], "nestedProperties": [
            {"name": "target_title", "dataType": ["text"]},
            {"name": "relation_type", "dataType": ["text"]},
        ]},
        {"name": "linksTo", "dataType": [CLASS]},
    ],
    "vectorConfig": {SLOT: {}, "arctic2_embed": {}},
}
PROP_NAMES = [p["name"] for p in SCHEMA["properties"] if p["name"] != "linksTo"]


def _f32(x: float) -> float:
    from array import array

    return array("f", [x])[0]


def _v4_object(row: dict, *, with_vector: bool, with_distance: bool):
    """What weaviate-client v4 returns for ``row`` (gRPC transport)."""
    props = {
        "title": row["title"], "file_path": row["file_path"], "node_type": row["node_type"],
        "content": row["content"], "chunk_num": float(row["chunk_num"]),
        "total_chunks": float(row["total_chunks"]),
        "source_node_id": uuid.UUID(row["source_node_id"]),
        "created_at": datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
        "tags": row["tags"], "links": row["links"], "typed_links": row["typed_links"],
    }
    vector = {SLOT: [_f32(v) for v in row["vec"]], "arctic2_embed": [0.5] * 4} if with_vector else {}
    meta = kg_http.MetadataReturn(distance=_f32(row["distance"]) if with_distance else None)
    return SimpleNamespace(uuid=uuid.UUID(row["id"]), properties=props, metadata=meta, vector=vector)


# ─── weaviate-client-shaped fake (server path) ────────────────────────────


def _v4_matches(flt, row: dict) -> bool:
    kind = type(flt).__name__
    if kind == "_FilterAnd":
        return all(_v4_matches(f, row) for f in flt.filters)
    if kind == "_FilterOr":
        return any(_v4_matches(f, row) for f in flt.filters)
    op = flt.operator.value
    value = row.get(flt.target)
    if op == "Equal":
        return value == flt.value
    if op == "ContainsAny":
        return value in flt.value
    raise AssertionError(f"fake: operator {op} not modelled")


class _V4Coll:
    def __init__(self, calls: list) -> None:
        self.name = CLASS
        self.calls = calls
        self.query = self

    def near_vector(self, near_vector, limit=None, return_metadata=None,
                    include_vector=False, target_vector=None):
        self.calls.append(("near_vector", limit, tuple(return_metadata or ()), include_vector, target_vector))
        hits = [r for r in ROWS if r["hit"]][: limit or None]
        return SimpleNamespace(objects=[_v4_object(r, with_vector=include_vector, with_distance=True)
                                        for r in hits])

    def fetch_objects(self, filters=None, limit=None, offset=None, include_vector=False):
        rows = [r for r in ROWS if filters is None or _v4_matches(filters, r)]
        rows = rows[(offset or 0):][: limit or None]
        return SimpleNamespace(objects=[_v4_object(r, with_vector=include_vector, with_distance=False)
                                        for r in rows])


class _V4Client:
    def __init__(self, calls: list) -> None:
        self.collections = SimpleNamespace(get=lambda name: _V4Coll(calls))

    def close(self) -> None:
        pass


# ─── real-HTTP fake Weaviate (light path) ─────────────────────────────────

_LEAF = re.compile(r'path: \["(\w+)"\], (value\w+): ("(?:[^"\\]|\\.)*"|[-0-9.e]+|\[[^\]]*\])')


def _gql_row(row: dict, query: str) -> dict:
    out = {name: row.get(name) for name in PROP_NAMES}
    extra: dict = {"id": row["id"]}
    if "distance" in query.split("_additional", 1)[1]:
        extra["distance"] = row["distance"]
    if "vectors {" in query:
        extra["vectors"] = {SLOT: row["vec"], "arctic2_embed": [0.5] * 4}
    out["_additional"] = extra
    return out


class _FakeWeaviate(BaseHTTPRequestHandler):
    queries: list = []

    def log_message(self, *a):  # noqa: D401 — silence
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/.well-known/ready":
            return self._send({})
        if self.path == f"/v1/schema/{CLASS}":
            return self._send(SCHEMA)
        return self._send({"error": "no"}, 404)

    def do_POST(self):
        query = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["query"]
        type(self).queries.append(query)
        if "nearVector" in query:
            rows = [r for r in ROWS if r["hit"]]
        else:
            rows = list(ROWS)
            for prop, key, raw in _LEAF.findall(query):
                want = json.loads(raw)
                rows = [r for r in rows if r.get(prop) == want]
        offset = int(m.group(1)) if (m := re.search(r"offset: (\d+)", query)) else 0
        limit = int(m.group(1)) if (m := re.search(r"limit: (\d+)", query)) else None
        rows = rows[offset:][: limit]
        return self._send({"data": {"Get": {CLASS: [_gql_row(r, query) for r in rows]}}})


@pytest.fixture
def fake_weaviate():
    _FakeWeaviate.queries = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWeaviate)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


# ─── 1. one definition, server semantics ──────────────────────────────────

#: Moved out of embeddings.py — server re-exports them FROM embeddings (as it
#: always did), so they are bound to embeddings' globals, not server's.
EMBEDDINGS_MOVED = ("_code_record_changed", "_get_embedding_service",
                    "_get_search_vector", "_kg_search_vector")
#: Light-host-only names: server keeps its own (weaviate-client) versions.
LIGHT_ONLY = ("get_weaviate_client", "bind_into", "_canonical_sanitize_for_weaviate_class")
def _same_code(a, b) -> bool:
    # A purge + re-import recompiles, so compare the code, not the object.
    return (a.__code__.co_code == b.__code__.co_code
            and a.__code__.co_filename == b.__code__.co_filename
            and a.__code__.co_firstlineno == b.__code__.co_firstlineno)


def test_every_moved_function_is_one_code_object_bound_to_server():
    core, server, embeddings = _live("kg_query_core"), _live("server"), _live("embeddings")
    moved = [
        name for name, obj in vars(core).items()
        if isinstance(obj, types.FunctionType) and obj.__module__ == core.__name__
        and name not in EMBEDDINGS_MOVED + LIGHT_ONLY and not name.startswith("__")
    ]
    assert len(moved) == 37, moved
    for name in moved:
        fn = getattr(server, name)
        assert _same_code(fn, getattr(core, name)), name
        assert fn.__code__.co_filename.endswith("kg_query_core.py"), name
        assert fn.__globals__ is vars(server), f"server.{name} is not bound to server's globals"
        assert fn.__module__ == server.__name__
    for name in EMBEDDINGS_MOVED:
        fn = getattr(embeddings, name)
        assert _same_code(fn, getattr(core, name)), name
        assert fn.__globals__ is vars(embeddings), name
        if hasattr(server, name):  # the three server always re-exported
            assert getattr(server, name) is fn, name


def test_server_keeps_its_own_client_and_filter():
    assert srv.get_weaviate_client is not kg_query_core.get_weaviate_client
    assert srv.Filter.__module__.startswith("weaviate.")
    http = _live("kg_http")  # not the collection-time object: suites purge weaviate_mcp.*
    assert kg_query_core.Filter is http.Filter
    assert srv.WeaviateUnreachable is http.WeaviateUnreachable


def test_patch_on_server_reaches_the_moved_tier_renderer(monkeypatch):
    """The suite patches ``server._fetch_node_chunks`` and calls
    ``server._format_result_by_tier`` — the moved code must see the patch."""
    srv = _live("server")
    seen = []

    def _fake_chunks(coll, title, hit, total, window, file_path=""):
        seen.append((title, window))
        return [(1, "one"), (2, "two")]

    monkeypatch.setattr(srv, "_fetch_node_chunks", _fake_chunks)
    out = srv._format_result_by_tier(
        {"title": "T", "file_path": "knowledge/t.md", "node_type": "concept",
         "total_chunks": 2, "chunk_number": 1, "content": "x"},
        "full", sidecar_db={}, coll=object())
    assert seen == [("T", 7)]
    assert out["content"] == "one\ntwo" and out["coverage"] == "complete"


def test_patch_on_server_config_reaches_the_moved_fan_out(monkeypatch):
    srv = _live("server")
    monkeypatch.setattr(srv, "KG_COLLECTION", "Foo_KnowledgeGraph")
    monkeypatch.setattr(srv, "SHARED_KG_COLLECTION", "Bar_KnowledgeGraph")
    monkeypatch.setattr(srv, "SHARED_KG_READ_DISABLED", False)
    monkeypatch.setattr(srv, "_kg_peer_collections", lambda: ["Peer_KnowledgeGraph"])
    assert srv._kg_collections_to_search() == [
        "Foo_KnowledgeGraph", "Bar_KnowledgeGraph", "Peer_KnowledgeGraph"]
    monkeypatch.setattr(srv, "SHARED_KG_READ_DISABLED", True)
    assert srv._kg_collections_to_search() == ["Foo_KnowledgeGraph", "Peer_KnowledgeGraph"]


# ─── 2. host resolution ───────────────────────────────────────────────────


def test_host_is_the_server_whenever_one_is_loaded():
    assert kg_host.server_loaded()
    assert kg_host.host("weaviate_mcp") is sys.modules["weaviate_mcp.server"]


def test_host_is_the_light_core_without_a_server(monkeypatch):
    _live("kg_query_core")
    for key in ("weaviate_mcp.server", "claude_mcp_servers.weaviate_mcp.server"):
        monkeypatch.delitem(sys.modules, key, raising=False)
    assert not kg_host.server_loaded()
    assert kg_host.host("weaviate_mcp") is sys.modules["weaviate_mcp.kg_query_core"]
    # the light host serves the RL satellites' names without the server
    core = sys.modules["weaviate_mcp.kg_query_core"]
    assert core._RL_OVERFETCH == sys.modules["weaviate_mcp.rl_state"]._RL_OVERFETCH
    assert core._cosine is sys.modules["weaviate_mcp.rl_enrichment"]._cosine
    assert "weaviate_mcp.server" not in sys.modules


# ─── 3. HTTP adapter: query construction + v4 result typing ──────────────


def _client(url: str) -> kg_http.HttpWeaviateClient:
    return kg_http.HttpWeaviateClient(url)


def test_filters_are_typed_from_the_schema_like_the_grpc_coercion(fake_weaviate):
    coll = _client(fake_weaviate).collections.get(CLASS)
    flt = (kg_http.Filter.by_property("file_path").equal("knowledge/concepts/alpha.md")
           & kg_http.Filter.by_property("chunk_num").equal(2))
    rows = coll.query.fetch_objects(filters=flt, limit=1)
    query = _FakeWeaviate.queries[-1]
    # chunk_num is a `number` property: an int value goes out as valueNumber
    # (GraphQL refuses valueInt there; gRPC coerces) — never valueInt.
    assert 'path: ["chunk_num"], valueNumber: 2' in query
    assert 'path: ["file_path"], valueText: "knowledge/concepts/alpha.md"' in query
    assert "operator: And" in query and "limit: 1" in query
    assert [o.properties["chunk_num"] for o in rows.objects] == [2.0]
    anyf = (kg_http.Filter.by_property("source_node_id").contains_any(["x"])
            | kg_http.Filter.by_property("title").contains_any(["Alpha node", "Beta node"]))
    assert anyf.to_graphql({"title": "text", "source_node_id": "uuid"}) == (
        '{operator: Or, operands: [{operator: ContainsAny, path: ["source_node_id"], '
        'valueText: ["x"]}, {operator: ContainsAny, path: ["title"], '
        'valueText: ["Alpha node", "Beta node"]}]}')


def test_results_are_typed_like_weaviate_client_v4(fake_weaviate):
    coll = _client(fake_weaviate).collections.get(CLASS)
    res = coll.query.near_vector(near_vector=QVEC, limit=5, return_metadata=["distance"],
                                 include_vector=True, target_vector=SLOT)
    query = _FakeWeaviate.queries[-1]
    assert f'targetVectors: ["{SLOT}"]' in query and "distance" in query
    assert "linksTo" not in query  # cross-references are not returned by v4 either
    got = res.objects[0]
    want = _v4_object(ROWS[0], with_vector=True, with_distance=True)
    assert got.uuid == want.uuid and isinstance(got.uuid, uuid.UUID)
    assert got.properties == want.properties
    assert got.properties["created_at"].tzinfo == timezone.utc
    assert got.vector == want.vector  # float32-exact, every named vector
    assert got.metadata.distance == want.metadata.distance
    assert res.objects[1].properties["typed_links"] is None  # unset → None, as v4
    plain = coll.query.fetch_objects(limit=1)
    assert plain.objects[0].vector == {} and plain.objects[0].metadata.distance is None


def test_unsupported_arguments_fail_loudly(fake_weaviate):
    coll = _client(fake_weaviate).collections.get(CLASS)
    with pytest.raises(TypeError):
        coll.query.fetch_objects(limit=1, return_properties=["title"])
    with pytest.raises(TypeError):
        coll.query.near_vector(near_vector=QVEC, return_metadata=["explain_score"])


def test_unreachable_weaviate_raises_at_construction():
    with pytest.raises(kg_http.WeaviateUnreachable):
        kg_http.HttpWeaviateClient("http://127.0.0.1:9", init_timeout=0.5)


def test_graphql_errors_raise(fake_weaviate, monkeypatch):
    client = _client(fake_weaviate)
    monkeypatch.setattr(client, "_post_json",
                        lambda path, body: {"errors": [{"message": "no such class"}]})
    with pytest.raises(kg_http.WeaviateHttpQueryError, match="no such class"):
        client.collections.get(CLASS).query.fetch_objects(limit=1)


# ─── 4. end-to-end parity: server path vs light path ─────────────────────

ARGV = ["how does alpha relate to beta", "--limit", "3", "--hook-format"]


def _hook_env(tmp_path: Path, weaviate_url: str, base: dict) -> dict:
    env = dict(base)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update({
        "WEAVIATE_URL": weaviate_url, "KG_COLLECTION": CLASS, "SHARED_KG_COLLECTION": "",
        "DEVELOPMENT_COLLECTION": "", "VCT_DISABLE_HUB_RESOLVER": "1",
        "RL_LOCAL_LOGGING_DISABLED": "1", "RL_ONLINE_TRAINING_DISABLED": "1",
        "DUAL_RL_LOG_ENABLED": "false", "HOME": str(home), "VCT_USER_HOME_OVERRIDE": str(home),
        "VCT_STATE_DIR": str(home / ".vct"), "CLAUDE_PROJECT_DIR": str(tmp_path),
        "KG_BASE_DIR": str(tmp_path),
        # no live RL reranker may reorder either run
        "RL_SERVER_URL": "http://127.0.0.1:9", "RL_SERVER_PORT": "9",
    })
    for key in ("VCT_KG_ACCESS_LIST", "VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT"):
        env.pop(key, None)
    return env


_LIGHT_DRIVER = textwrap.dedent('''
    import asyncio, io, json, sys
    from contextlib import redirect_stdout
    sys.path[:0] = [{mcp!r}, {root!r}, {scripts!r}]
    import rl_kg_search
    from weaviate_mcp import kg_query_core as core

    async def _vec(text, collection=None, task=None, *, steady=None):
        return {qvec!r}, {slot!r}

    core._kg_search_vector = _vec
    sys.argv = ["rl_kg_search.py", *{argv!r}]
    buf = io.StringIO()
    with redirect_stdout(buf):
        asyncio.run(rl_kg_search.main())
    heavy = sorted(m for m in sys.modules
                   if m.split(".")[0] in ("weaviate", "mcp", "grpc")
                   or m.endswith("weaviate_mcp.server"))
    print(json.dumps({{"stdout": buf.getvalue(), "heavy": heavy}}))
''')


def _light_run(tmp_path: Path, url: str) -> dict:
    driver = _LIGHT_DRIVER.format(mcp=str(MCP_DIR), root=str(REPO_ROOT),
                                  scripts=str(MCP_DIR / "scripts"), qvec=QVEC, slot=SLOT,
                                  argv=ARGV)
    p = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True,
                       env=_hook_env(tmp_path, url, child_env()), cwd=str(tmp_path), timeout=120)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def _server_run(tmp_path: Path, monkeypatch, capsys) -> str:
    from tests.common import rl_kg_search_harness as H
    import claude_mcp_servers.rl_client.search_pipeline as sp

    for key, value in _hook_env(tmp_path, "http://127.0.0.1:9", {}).items():
        if key.startswith(("RL_", "DUAL_", "KG_BASE")):
            monkeypatch.setenv(key, value)
    mod = H.import_rl_kg_search()

    async def _vec(text, collection=None, task=None, *, steady=None):
        return list(QVEC), SLOT

    calls: list = []
    srv = _live("server")  # the module rl_kg_search's host() will resolve
    monkeypatch.setattr(srv, "get_weaviate_client", lambda: _V4Client(calls))
    monkeypatch.setattr(srv, "_kg_search_vector", _vec)
    monkeypatch.setattr(srv, "KG_COLLECTION", CLASS)
    monkeypatch.setattr(srv, "SHARED_KG_COLLECTION", "")
    monkeypatch.setattr(srv, "DEVELOPMENT_COLLECTION", "")
    monkeypatch.setattr(srv, "KG_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_node_formats_cache", None)
    monkeypatch.setattr(srv, "_node_formats_by_collection", {})
    monkeypatch.setattr(srv, "_kg_peer_collections", lambda: [])
    monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: None)
    monkeypatch.setattr(sp, "_resolve_rl_enabled", lambda: False)
    monkeypatch.setattr(sp, "_retrieval_emit_has_consumer", lambda: False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["rl_kg_search.py", *ARGV])
    capsys.readouterr()
    asyncio.run(mod.main())
    assert calls and calls[0][0] == "near_vector"
    return capsys.readouterr().out


def test_hook_output_is_byte_identical_across_the_two_transports(
        tmp_path, fake_weaviate, monkeypatch, capsys):
    light = _light_run(tmp_path, fake_weaviate)
    via_server = _server_run(tmp_path, monkeypatch, capsys)
    # (``2/2.0``: v4 returns ``number`` properties as float — the live format.)
    assert "KG: Alpha node | concept | score=0.80 | FULL NODE (2/2.0 chunks):" in via_server
    assert "Alpha second chunk body." in via_server  # the chunk fetch ran
    assert "KG: Beta node | pattern | score=0.60 | 1 CHUNK:" in via_server
    assert light["stdout"] == via_server


def test_light_hook_run_imports_no_weaviate_mcp_or_server(tmp_path, fake_weaviate):
    light = _light_run(tmp_path, fake_weaviate)
    assert light["stdout"].startswith("KG: Alpha node")
    assert light["heavy"] == [], light["heavy"]
    # the light run read Weaviate only through the HTTP adapter
    assert any("nearVector" in q for q in _FakeWeaviate.queries)
