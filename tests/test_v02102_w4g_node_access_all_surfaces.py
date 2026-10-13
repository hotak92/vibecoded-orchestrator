# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W4-G item 1 — per-node ``cross_project_access`` on EVERY KG read.

W4-C enforced the launcher editor's per-node restriction on the weaviate-kg
MCP fan-out only; the ``kg-search`` CLI, the ``kg-info`` CLI and the hook
router's KG leg (``rl_kg_search.py``) still surfaced restricted shared/peer
rows, so the launcher notice "Enforced on cross-project reads" was false.

These tests pin, against in-memory fakes (no Weaviate, no hub):

* the ONE verdict home ``vco_lib.kg_node_access`` (table, lazy requester,
  requester rungs) and that the MCP server delegates to it;
* every surface: a peer row restricted to another project is ABSENT (act),
  while self rows, unrestricted, ``*`` and self-granted peer rows stay
  (leave-alone);
* a broken install (``vco_lib.kg_node_access`` unimportable) fails LOUDLY on
  the CLI instead of returning restricted rows.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"
HELPER_DIR = REPO_ROOT / "claude_mcp_servers" / "scripts"
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
for _p in (str(REPO_ROOT), str(MCP_DIR), str(SCRIPTS_DIR), str(HELPER_DIR), str(REPO_ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vco_lib.kg_node_access as kna  # noqa: E402

SELF = "Alpha_KnowledgeGraph"
PEER = "Beta_KnowledgeGraph"
_UNSET = object()


class _Obj:
    def __init__(self, title: str, access=_UNSET, links=None):
        self.uuid = f"uuid-{title}"
        self.properties = {
            "title": title, "node_type": "concept", "content": f"body {title}",
            "file_path": f"knowledge/concepts/{title}.md", "tags": [],
            "links": list(links or []), "source_node_id": title,
            "created_at": "2099-01-01T00:00:00Z", "updated_at": "2099-01-01T00:00:00Z",
        }
        if access is not _UNSET:
            self.properties["cross_project_access"] = access
        self.metadata = SimpleNamespace(distance=0.2)
        self.vector = {}


def _peer_rows(links=None):
    return [
        _Obj("PeerOtherOnly", ["proj-other"], links=links),
        _Obj("PeerGranted", ["proj-self"], links=links),
        _Obj("PeerStar", ["*"], links=links),
        _Obj("PeerLegacy", links=links),
        _Obj("PeerPrivate", [], links=links),
    ]


def _self_rows(links=None):
    return [_Obj("SelfPrivate", [], links=links), _Obj("SelfOtherOnly", ["proj-other"], links=links)]


PEER_VISIBLE = {"PeerGranted", "PeerStar", "PeerLegacy"}
PEER_HIDDEN = {"PeerOtherOnly", "PeerPrivate"}


class _Query:
    def __init__(self, objs):
        self._objs = objs

    def near_vector(self, **kw):
        return SimpleNamespace(objects=list(self._objs))

    near_text = near_vector

    def fetch_objects(self, **kw):
        if kw.get("after") is not None:
            return SimpleNamespace(objects=[])
        return SimpleNamespace(objects=list(self._objs))


class _Client:
    def __init__(self, contents):
        client = self
        self.contents = contents

        class _Colls:
            def get(self, name):
                return SimpleNamespace(query=_Query(client.contents.get(name, [])))

        self.collections = _Colls()

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    monkeypatch.setenv("VCT_DISABLE_HUB_RESOLVER", "1")
    monkeypatch.setenv("VCT_PROJECT_ID", "proj-self")
    monkeypatch.delenv("VCT_KG_ACCESS_LIST", raising=False)
    kna.resolve_requester_project_id.cache_clear()
    yield
    kna.resolve_requester_project_id.cache_clear()


# ─── the one home ────────────────────────────────────────────────────────


def test_verdict_table():
    a = kna.allows
    assert a(None, "me") and a({}, "me") and a({"cross_project_access": None}, "me")
    assert a({"cross_project_access": ["*"]}, "")
    assert a({"cross_project_access": ["me", "you"]}, "me")
    assert not a({"cross_project_access": ["you"]}, "me")
    assert not a({"cross_project_access": []}, "me")
    assert not a({"cross_project_access": ["me"]}, "")
    assert not a({"cross_project_access": "me"}, "me")


def test_requester_resolved_lazily_and_once():
    calls = []

    def who():
        calls.append(1)
        return "proj-self"

    unrestricted = [_Obj("A"), _Obj("B", ["*"])]
    assert kna.filter_objects(unrestricted, is_self=False, requester=who) == unrestricted
    assert calls == []  # no row needed identity → no hub round-trip
    out = kna.filter_objects(_peer_rows(), is_self=False, requester=who)
    assert {o.properties["title"] for o in out} == PEER_VISIBLE
    assert calls == [1]


def test_self_is_never_filtered():
    rows = _self_rows()
    assert kna.filter_objects(rows, is_self=True, requester="nobody") == rows


def test_requester_rungs(monkeypatch):
    assert kna.requester_from_config(SimpleNamespace(project_id="hub-id")) == "hub-id"
    assert kna.requester_from_config(None) == "proj-self"  # env rung
    monkeypatch.delenv("VCT_PROJECT_ID")
    assert kna.requester_from_config(SimpleNamespace(project_id=None)) == ""


def test_mcp_server_delegates_to_the_one_home(monkeypatch):
    srv = importlib.import_module("weaviate_mcp.server")
    assert srv._cross_project_access_allows is kna.allows
    monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: None)
    out = srv._filter_cross_project_access(_peer_rows(), is_self=False)
    assert {o.properties["title"] for o in out} == PEER_VISIBLE


# ─── kg-search CLI ───────────────────────────────────────────────────────


def _fresh(name, monkeypatch):
    for mod in list(sys.modules):
        if mod in (name, "kg_access"):
            del sys.modules[mod]
    monkeypatch.setenv("KG_COLLECTION", SELF)
    monkeypatch.setenv("SHARED_KG_COLLECTION", PEER)
    return importlib.import_module(name)


def _titles(out: str) -> set:
    names = {"PeerOtherOnly", "PeerGranted", "PeerStar", "PeerLegacy", "PeerPrivate",
             "SelfPrivate", "SelfOtherOnly"}
    return {n for n in names if n in out}


def test_kg_search_search_filters_peer_rows_keeps_self(monkeypatch, capsys):
    sk = _fresh("search_knowledge", monkeypatch)
    client = _Client({SELF: _self_rows(), PEER: _peer_rows()})
    monkeypatch.setattr(sk, "HAS_RL_PIPELINE", False)
    with mock.patch.object(sk, "get_weaviate_client", return_value=client), \
         mock.patch.object(sk, "get_embedding", return_value=[0.0] * 8), \
         mock.patch.object(sk, "_get_target_vector_slot", return_value="qwen3_embed"):
        sk.search_knowledge("q", limit=20, detail="titles")
    seen = _titles(capsys.readouterr().out)
    assert seen >= PEER_VISIBLE | {"SelfPrivate", "SelfOtherOnly"}
    assert not (seen & PEER_HIDDEN), seen


@pytest.mark.parametrize("command", ["list", "recent", "created"])
def test_kg_search_listing_commands_filter_peer_rows(monkeypatch, capsys, command):
    sk = _fresh("search_knowledge", monkeypatch)
    client = _Client({SELF: _self_rows(), PEER: _peer_rows()})
    with mock.patch.object(sk, "get_weaviate_client", return_value=client):
        if command == "list":
            sk.list_all_nodes(collections=[SELF, PEER])
        elif command == "recent":
            sk.search_recent(days=1, collections=[SELF, PEER])
        else:
            sk.search_created(days=1, collections=[SELF, PEER])
    seen = _titles(capsys.readouterr().out)
    assert seen == PEER_VISIBLE | {"SelfPrivate", "SelfOtherOnly"}, seen


def test_kg_search_broken_install_fails_loudly(monkeypatch):
    sk = _fresh("search_knowledge", monkeypatch)
    client = _Client({SELF: [], PEER: _peer_rows()})
    monkeypatch.setitem(sys.modules, "vco_lib.kg_node_access", None)
    with mock.patch.object(sk, "get_weaviate_client", return_value=client):
        with pytest.raises(ImportError):
            sk.list_all_nodes(collections=[PEER])


# ─── kg-info CLI ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("title,visible", [("PeerOtherOnly", False), ("PeerPrivate", False),
                                           ("PeerGranted", True), ("PeerLegacy", True)])
def test_kg_info_info_hides_restricted_peer_node(monkeypatch, capsys, title, visible):
    gi = _fresh("get_node_info", monkeypatch)
    row = next(o for o in _peer_rows() if o.properties["title"] == title)
    client = _Client({PEER: [row]})
    with mock.patch.object(gi, "get_weaviate_client", return_value=client):
        got = gi.get_node_info(title)
    assert (got is not None) is visible
    if not visible:
        assert "Node not found" in capsys.readouterr().out


def test_kg_info_info_keeps_self_private_node(monkeypatch):
    gi = _fresh("get_node_info", monkeypatch)
    client = _Client({SELF: [_Obj("SelfPrivate", [])]})
    with mock.patch.object(gi, "get_weaviate_client", return_value=client):
        assert gi.get_node_info("SelfPrivate") is not None


def test_kg_info_connections_inbound_scan_filters_peer_rows(monkeypatch, capsys):
    gi = _fresh("get_node_info", monkeypatch)
    target = _Obj("Target")
    client = _Client({
        SELF: [target] + _self_rows(links=["Target"]),
        PEER: _peer_rows(links=["Target"]),
    })
    with mock.patch.object(gi, "get_weaviate_client", return_value=client):
        gi.find_connections("Target")
    out = capsys.readouterr().out
    seen = _titles(out)
    assert seen == PEER_VISIBLE | {"SelfPrivate", "SelfOtherOnly"}, out


# ─── hook router KG leg (rl_kg_search) ───────────────────────────────────


def test_hook_kg_leg_filters_peer_rows_keeps_self(monkeypatch, tmp_path):
    from common import rl_kg_search_harness as H

    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    mod = H.import_rl_kg_search()
    srv = importlib.import_module("weaviate_mcp.server")
    sp = importlib.import_module("claude_mcp_servers.rl_client.search_pipeline")
    state: dict = {}
    H.install_fakes(srv, sp, cfg={"collections": [SELF, PEER]}, state=state,
                    patch=monkeypatch.setattr)
    monkeypatch.setattr(srv, "KG_COLLECTION", SELF)
    monkeypatch.setattr(srv, "get_weaviate_client",
                        lambda *a, **k: _Client({SELF: _self_rows(), PEER: _peer_rows()}))
    formatted: list = []

    def _fmt(obj, coll, dist):
        formatted.append(obj.properties["title"])
        return {"title": obj.properties["title"], "file_path": obj.properties["file_path"],
                "collection": coll, "distance": dist, "node_type": "concept", "chunk_number": 0}

    monkeypatch.setattr(srv, "_format_obj", _fmt)
    monkeypatch.setattr(srv, "_try_resolve_project_config",
                        lambda: SimpleNamespace(project_id="proj-self"))
    H.run_main(mod, limit=10)
    seen = set(formatted)
    assert seen == PEER_VISIBLE | {"SelfPrivate", "SelfOtherOnly"}, seen
