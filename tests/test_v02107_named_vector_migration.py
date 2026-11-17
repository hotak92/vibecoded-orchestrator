# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 S-2 — the two manual named-vector migrations
(``migrate_to_new_embeddings.add_named_vector_to_collection`` and the MCP
server's ``migrate_embeddings``) through their one home,
``vco_lib.named_vector_migration``.

* A class that already has named vectors gets the slot IN PLACE: no drop,
  no create, no insert.
* A legacy single-vector class is rebuilt only when the rows read equal the
  server's count; an unreadable count or a mismatch drops nothing.
* The rebuild keeps every row's uuid.

Every Weaviate call is faked; nothing here opens a socket.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO), str(REPO / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import fixture_class_guard as fcg  # noqa: E402
from vco_lib import named_vector_migration as nvm  # noqa: E402
from vco_lib import weaviate_helpers as wh  # noqa: E402
from vco_lib import weaviate_schema as ws  # noqa: E402

CLS = "Proj_KnowledgeGraph"
UUIDS = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(1, 4)]


class _World:
    """One fake Weaviate: a schema, its rows, and a count the server reports."""

    def __init__(self, *, named: bool, rows: int = 3, count: Optional[int] = None):
        self.vector_config: Dict[str, Any] = (
            {"ollama_embed": {"vectorizer": {"none": {}}}} if named else {})
        self.rows = [
            SimpleNamespace(uuid=UUIDS[i], properties={"title": f"n{i}", "content": f"c{i}"},
                            vector=({"ollama_embed": [0.1] * 4} if named else [0.2] * 4))
            for i in range(rows)
        ]
        self.count = rows if count is None else count
        self.count_unreadable = False
        self.dropped: List[str] = []
        self.created: List[dict] = []
        self.inserted: List[dict] = []
        self.puts: List[dict] = []

    # -- REST ---------------------------------------------------------------
    def http(self, method, url, *, body=None, timeout=30.0):
        if url.endswith(f"/v1/schema/{CLS}") and method == "GET":
            return 200, json.dumps({"class": CLS, "vectorConfig": self.vector_config}).encode()
        if url.endswith(f"/v1/schema/{CLS}") and method == "PUT":
            self.puts.append(body)
            self.vector_config = dict(body["vectorConfig"])
            return 200, b"{}"
        if url.endswith("/v1/graphql"):
            if self.count_unreadable:
                return 500, b"boom"
            return 200, json.dumps(
                {"data": {"Aggregate": {CLS: [{"meta": {"count": self.count}}]}}}).encode()
        if url.endswith("/v1/meta"):
            return 200, b'{"version": "1.37.0"}'
        return 404, b""

    # -- client -------------------------------------------------------------
    def client(self):
        world = self

        def fetch_objects(limit, after, **_kw):
            ids = [r.uuid for r in world.rows]
            start = 0 if after == "00000000-0000-0000-0000-000000000000" else ids.index(after) + 1
            return SimpleNamespace(objects=world.rows[start:start + limit])

        coll = SimpleNamespace(
            name=CLS,
            query=SimpleNamespace(fetch_objects=fetch_objects),
            config=SimpleNamespace(get=lambda: SimpleNamespace(properties=[])),
            data=SimpleNamespace(insert=lambda **kw: world.inserted.append(kw)),
        )
        collections = SimpleNamespace(
            exists=lambda name: name == CLS,
            get=lambda name: coll,
            create=lambda **kw: world.created.append(kw),
        )
        return SimpleNamespace(collections=collections)


@pytest.fixture
def world_factory(monkeypatch):
    def make(**kw):
        world = _World(**kw)
        monkeypatch.setattr(wh, "http_request", world.http)
        monkeypatch.setattr(ws, "_http_request", world.http)
        monkeypatch.setattr(fcg, "record_collection_delete",
                            lambda client, name: world.dropped.append(name))
        return world
    return make


def _script():
    return importlib.import_module("claude_mcp_servers.scripts.migrate_to_new_embeddings")


def _run_tool(world, monkeypatch):
    server = importlib.import_module("weaviate_mcp.server")
    monkeypatch.setattr(server, "get_weaviate_client", world.client)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "")
    return json.loads(asyncio.run(server.migrate_embeddings(CLS, "kg")))


def _run_script(world):
    return _script().add_named_vector_to_collection(world.client(), CLS, "qwen3_embed", 1024)


# ── a class with named vectors: in place, never a drop ─────────────────────


def test_script_adds_the_slot_in_place_on_a_named_vector_class(world_factory):
    world = world_factory(named=True)
    assert _run_script(world) is True
    assert (world.dropped, world.created, world.inserted) == ([], [], [])
    assert "qwen3_embed" in world.vector_config and "ollama_embed" in world.vector_config


def test_tool_adds_the_slots_in_place_on_a_named_vector_class(world_factory, monkeypatch):
    world = world_factory(named=True)
    out = _run_tool(world, monkeypatch)
    assert out["mode"] == "in_place"
    assert sorted(out["added_vectors"]) == ["openai_embed", "qwen3_embed"]
    assert (world.dropped, world.created, world.inserted) == ([], [], [])


# ── a legacy class: the count gates the drop ───────────────────────────────


@pytest.mark.parametrize("count", [None, 2, 5])
def test_script_drops_nothing_when_the_count_disagrees(world_factory, count):
    world = world_factory(named=False, count=count or 0)
    world.count_unreadable = count is None
    with pytest.raises(nvm.MigrationRefused):
        _run_script(world)
    assert (world.dropped, world.created, world.inserted) == ([], [], [])


@pytest.mark.parametrize("count", [None, 2, 5])
def test_tool_drops_nothing_when_the_count_disagrees(world_factory, monkeypatch, count):
    world = world_factory(named=False, count=count or 0)
    world.count_unreadable = count is None
    out = _run_tool(world, monkeypatch)
    assert "Nothing was dropped" in out["error"]
    assert (world.dropped, world.created, world.inserted) == ([], [], [])


def test_script_rebuilds_a_legacy_class_keeping_every_uuid(world_factory):
    world = world_factory(named=False)
    assert _run_script(world) is True
    assert world.dropped == [CLS] and len(world.created) == 1
    assert [i["uuid"] for i in world.inserted] == UUIDS
    assert all(set(i["vector"]) == {"ollama_embed"} for i in world.inserted)


def test_tool_rebuilds_a_legacy_class_keeping_every_uuid(world_factory, monkeypatch):
    world = world_factory(named=False)
    out = _run_tool(world, monkeypatch)
    assert out["status"] == "success" and out["inserted"] == 3
    assert world.dropped == [CLS]
    assert [i["uuid"] for i in world.inserted] == UUIDS
    assert all(set(i["vector"]) == {"qwen3_embed"} for i in world.inserted)
