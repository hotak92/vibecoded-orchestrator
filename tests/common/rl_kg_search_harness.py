# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Service-free harness for ``claude_mcp_servers/scripts/rl_kg_search.py`` (v0.2.100).

ONE home for the fakes the dual-RL-log and hook-permission tests need, usable
two ways:

* in-process — :func:`install_fakes` monkeypatches the already-imported
  ``weaviate_mcp.server`` / ``search_pipeline`` modules;
* as a SUBPROCESS — ``python rl_kg_search_harness.py <config.json>`` applies
  the same fakes in a fresh interpreter (so module-level identity resolution
  runs for real, exactly as in a hook process), runs ``rl_kg_search.main()``,
  and writes a JSON result next to the config. Its wall time includes
  interpreter shutdown, which is what a hook's latency budget sees.

No Weaviate, no Ollama, no hub: every network seam is replaced. The real code
under test is rl_kg_search's flow, the shared ``resolve_and_enrich_dual``
helper, the dual-log gate, ``search_pipeline.rerank_and_emit`` (rerank off),
its log-node builders and the pending-file staging.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
SCRIPTS_DIR = MCP_DIR / "scripts"

ACTIVE_VEC = [0.11] * 1024
ARCTIC_VEC = [0.22] * 1024
ACTIVE_SLOT = "qwen3_embed"
OTHER_SLOT = "arctic2_embed"


def _node_vec(title: str, slot: str) -> list:
    base = 0.3 if slot == ACTIVE_SLOT else 0.6
    return [base + (len(title) % 7) * 0.01] * 1024


class FakeObj:
    def __init__(self, title: str, coll: str, distance: float) -> None:
        self.properties = {"title": title, "file_path": f"knowledge/{title}.md"}
        self.metadata = SimpleNamespace(distance=distance)
        self.coll = coll
        self.vector = {
            ACTIVE_SLOT: _node_vec(title, ACTIVE_SLOT),
            OTHER_SLOT: _node_vec(title, OTHER_SLOT),
        }


class FakeClient:
    """Records every collection a near_vector query was issued against."""

    def __init__(self, queried: list) -> None:
        self.queried = queried
        client = self

        class _Colls:
            def get(self, name):
                return _Coll(name, client)

        self.collections = _Colls()

    def close(self) -> None:
        pass


class _Coll:
    def __init__(self, name: str, client: FakeClient) -> None:
        self.name = name
        coll = self

        class _Q:
            def near_vector(self, **kw):
                client.queried.append(coll.name)
                return SimpleNamespace(
                    objects=[
                        FakeObj(f"{coll.name}-node-a", coll.name, 0.10),
                        FakeObj(f"{coll.name}-node-b", coll.name, 0.20),
                    ]
                )

            near_text = near_vector

        self.query = _Q()


class FakeEmbeddingService:
    """``embed_text_all_configured`` with an optional delay (slow secondary)."""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.calls = 0

    def embed_text_all_configured(self, text: str) -> dict:
        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        return {ACTIVE_SLOT: list(ACTIVE_VEC), OTHER_SLOT: list(ARCTIC_VEC)}


def _fake_enrich(state: dict):
    """Stand-in for ``_rl_enrich_nodes_with_linked_embs``: attach the other
    slot's per-node vector from the (fake) fetched object exactly when the
    shared helper asks for it, and record the kwargs it was called with."""

    def enrich(nodes, query_emb=None, active_slot="", **kw):
        state.setdefault("enrich_calls", []).append(
            {k: (v if not isinstance(v, list) else len(v)) for k, v in kw.items()}
        )
        other = kw.get("other_slot") or ""
        for n in nodes:
            n.setdefault("emb", _node_vec(n.get("title", ""), ACTIVE_SLOT))
            n.setdefault("cos_qn", 0.5)
            n.setdefault("chunks_matched", 2)
            n.setdefault("best_chunk_number", 1)
            if other:
                n["emb_other"] = _node_vec(n.get("title", ""), OTHER_SLOT)
                n["cos_qn_other"] = 0.4

    return enrich


def install_fakes(srv, sp, *, cfg: dict, state: dict, patch) -> None:
    """Apply the fakes. ``patch(obj, name, value)`` is the setter (monkeypatch
    in-process, plain setattr in the subprocess)."""
    queried = state.setdefault("queried", [])
    emitted = state.setdefault("emitted", [])
    svc = FakeEmbeddingService(delay_s=float(cfg.get("embed_delay_s") or 0.0))
    state["svc"] = svc

    async def _search_vector(_q):
        return list(ACTIVE_VEC), ACTIVE_SLOT

    patch(srv, "get_weaviate_client", lambda *a, **k: FakeClient(queried))
    patch(srv, "_get_search_vector", _search_vector)
    patch(
        srv,
        "_format_obj",
        lambda obj, coll, dist: {
            "title": obj.properties["title"],
            "file_path": obj.properties["file_path"],
            "collection": coll,
            "distance": dist,
            "node_type": "concept",
            "chunk_number": 0,
        },
    )
    patch(srv, "_enrich_with_adjacent_chunks", lambda coll, formatted, name: formatted)
    patch(srv, "_extract_obj_vector", lambda obj, slot: obj.vector.get(slot))
    patch(srv, "_get_result_verbosity_by_score", lambda score: "discard")
    patch(srv, "_rl_enrich_nodes_with_linked_embs", _fake_enrich(state))
    patch(srv, "_get_embedding_service", lambda: svc)
    patch(srv, "EMBEDDING_SOURCE", "qwen3")
    patch(srv, "EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    if cfg.get("collections") is not None:
        fixed = list(cfg["collections"])
        patch(srv, "_kg_collections_to_search", lambda include_dev=False, **k: list(fixed))

    import vco_lib.embedding_service as es

    patch(es, "configured_text_models", lambda: ["qwen3-embedding:0.6b", "snowflake-arctic-embed2:latest"])

    def _capture(ev, *, writer_factory=None):
        emitted.append(
            {
                "task_id": ev.task_id,
                "task_type": ev.task_type,
                "embedding_source": ev.embedding_source,
                "embedding_dim": ev.embedding_dim,
                "query_emb_head": (ev.query_emb or [None])[0],
                "query_emb_len": len(ev.query_emb or []),
                "nodes": [
                    {k: (v[0] if isinstance(v, list) and v and isinstance(v[0], float) else v)
                     for k, v in n.items()}
                    for n in ev.nodes
                ],
                "t": time.monotonic(),
            }
        )
        return True

    # Hermetic project identity for the staged ctx: the pipeline reads the
    # project config from the `claude_mcp_servers.weaviate_mcp.server` alias,
    # whose memoised `_resolved_project_config` another suite may have left as
    # a MagicMock (seen: "Object of type MagicMock is not JSON serializable"
    # when the pending file was staged). Pin it on both aliases unless the
    # caller is testing identity resolution itself.
    if cfg.get("project_configs") is None:
        import importlib

        for alias in ("weaviate_mcp.server", "claude_mcp_servers.weaviate_mcp.server"):
            patch(importlib.import_module(alias), "_try_resolve_project_config", lambda: None)
    patch(sp, "emit_rl_event", _capture)
    patch(sp, "_resolve_rl_enabled", lambda: False)
    patch(sp, "_retrieval_emit_has_consumer", lambda: True)
    patch(sp, "_should_capture_citations", lambda rl_enabled: True)


def import_rl_kg_search():
    for p in (str(SCRIPTS_DIR), str(MCP_DIR), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib

    return importlib.import_module("rl_kg_search")


def run_main(mod, query: str = "dual log query", limit: int = 1) -> None:
    saved = sys.argv
    sys.argv = ["rl_kg_search.py", query, "--limit", str(limit), "--hook-format"]
    try:
        asyncio.run(mod.main())
    finally:
        sys.argv = saved


def _install_project_config_fake(configs: dict) -> list:
    """Replace ``vco_lib.project_config.resolve`` BEFORE server import with a
    per-path table (the hub's per-project ProjectConfig). Records every path
    it was asked about."""
    import vco_lib.project_config as pc

    asked: list = []

    def resolve(project_root):
        key = str(Path(project_root).resolve())
        asked.append(key)
        row = configs.get(key)
        if row is None:
            raise RuntimeError(f"project not registered: {key}")
        ns = SimpleNamespace(
            kg_access_list=[],
            diagrams_access_list=[],
            code_graph_access_list=[],
        )
        for k, v in row.items():
            setattr(ns, k, v)
        return _Cfg(ns)

    pc.resolve = resolve
    return asked


class _Cfg:
    """ProjectConfig stand-in: unknown fields read as None (\"no hub value\")."""

    def __init__(self, ns) -> None:
        self.__dict__.update(vars(ns))

    def __getattr__(self, name):
        return None


def _subprocess_main(config_path: str) -> int:
    cfg = json.loads(Path(config_path).read_text())
    asked = None
    if cfg.get("project_configs") is not None:
        asked = _install_project_config_fake(cfg["project_configs"])
    if cfg.get("rl_kg_search_path"):
        # Run the script from an explicit location (the orchestrator root).
        sys.path.insert(0, str(Path(cfg["rl_kg_search_path"]).parent))
    mod = import_rl_kg_search()
    import weaviate_mcp.server as srv
    from claude_mcp_servers.rl_client import search_pipeline as sp

    state: dict = {}
    install_fakes(srv, sp, cfg=cfg, state=state, patch=setattr)
    t0 = time.monotonic()
    run_main(mod, query=cfg.get("query", "dual log query"))
    state["main_elapsed_s"] = time.monotonic() - t0
    out = {
        "queried": state.get("queried", []),
        "emitted": state.get("emitted", []),
        "enrich_calls": state.get("enrich_calls", []),
        "embed_calls": state["svc"].calls,
        "main_elapsed_s": state["main_elapsed_s"],
        "kg_collection": srv.KG_COLLECTION,
        "asked_paths": asked,
    }
    Path(cfg["result_path"]).write_text(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(_subprocess_main(sys.argv[1]))
