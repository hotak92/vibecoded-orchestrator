# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-B2 — an embedding backend that STAYS down is a loud error.

Owner rulings 2026-10-10: never fall back to a different embedding model; a
down backend is retried transiently while the hub watchdog restarts it, and if
it stays down the agent gets a loud error naming the backend / container, the
watchdog state and the remedy. Both arms are pinned here, per surface:

* stays down → ``search_code_graph`` / ``hybrid_search`` return the structured
  ``EmbeddingBackendUnavailable`` payload (never a silent empty result, never
  a vector from another endpoint); the hook path prints ONE visible line and
  the router never caches it;
* transient → the restart is ridden out and the query embeds normally.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import embed_outage  # noqa: E402
from vco_lib.embed_outage import (  # noqa: E402
    EmbeddingBackendUnavailable,
    NO_SWITCH_MARK,
    embed_query_or_raise,
    hook_unavailable_line,
    is_hook_unavailable_text,
)
from vco_lib.embedding_providers.codeembed import CodeEmbedAdapter  # noqa: E402
from vco_lib.embedding_providers.ollama import OllamaAdapter  # noqa: E402
from vco_lib.embedding_providers.openai import OpenAIAdapter, ValidationResult  # noqa: E402
from vco_lib.embedding_service import EmbeddingService  # noqa: E402


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    """Finite zero-delay transient mode (``VCO_EMBED_503_RETRY_DELAY=0``)."""
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")
    for k in ("CODE_EMBED_MODEL", "CODE_EMBED_BACKEND", "ACTIVE_EMBEDDING"):
        monkeypatch.delenv(k, raising=False)


def _codesage_service(reachable):
    """A real EmbeddingService on CodeSage whose container answers per the
    ``reachable`` sequence (the LAST value repeats)."""
    seq = list(reachable)

    def _is_reachable():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    ollama_m = MagicMock(spec=OllamaAdapter)
    ollama_m.is_reachable.return_value = True
    ollama_m.list_embedding_models.return_value = []
    ollama_m.list_models.return_value = [{"name": "qwen3-embedding:0.6b"}]
    code_m = MagicMock(spec=CodeEmbedAdapter)
    code_m.is_reachable.side_effect = _is_reachable
    code_m.base_url = "http://localhost:11440"
    code_m.embed.return_value = [0.5, 0.5, 0.5, 0.5]
    oa_m = MagicMock(spec=OpenAIAdapter)
    oa_m.validate.return_value = ValidationResult(valid=False, reason="no key")
    with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama_m), \
         patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code_m), \
         patch("vco_lib.embedding_service.OpenAIAdapter", return_value=oa_m):
        svc = EmbeddingService.for_project()
    return svc, ollama_m, code_m


# --- the shared query entry ------------------------------------------------


def test_container_stays_down_is_loud_and_never_switches_model():
    svc, ollama_m, code_m = _codesage_service([False])
    try:
        assert svc.code_model_id == "codesage-large-v2"
        with pytest.raises(EmbeddingBackendUnavailable) as ctx:
            embed_query_or_raise("code", svc, "def f(): pass")
        msg = str(ctx.value)
        assert "vco_code_embed" in msg and "watchdog" in msg and "Remedy" in msg
        assert NO_SWITCH_MARK in msg
        assert ctx.value.container == "vco_code_embed"
        code_m.embed.assert_not_called()
        ollama_m.embed.assert_not_called()
    finally:
        svc.close()


def test_container_restart_is_ridden_out_then_the_query_embeds():
    # Down at construction AND at the first query probe, back on a re-probe:
    # the transient wait recovers and the normal CodeSage query runs.
    svc, ollama_m, code_m = _codesage_service([False, False, True])
    try:
        vec = embed_query_or_raise("code", svc, "def f(): pass")
        assert vec == [0.5, 0.5, 0.5, 0.5]
        code_m.embed.assert_called_once()
        ollama_m.embed.assert_not_called()
    finally:
        svc.close()


def test_watchdog_gave_up_is_named(monkeypatch, tmp_path):
    from vco_lib.deferral_report import DeferralReport

    class _Entry:
        condition_id = "watchdog_gave_up"
        title = "Hub watchdog gave up restarting vco_code_embed"
        detected = "vco_code_embed exited 3 times in 10 minutes"

    monkeypatch.setattr(DeferralReport, "read", classmethod(
        lambda cls, root: type("R", (), {"entries": [_Entry()]})()))
    msg = embed_outage.code_backend_unavailable_message("codesage-large-v2", "http://x:11440")
    assert "GAVE UP restarting vco_code_embed" in msg


def test_ollama_served_code_model_rides_a_refused_connection(monkeypatch):
    calls = {"n": 0}
    svc = MagicMock()
    svc.code_model_id = "unclemusclez/jina-embeddings-v2-base-code:latest"
    svc.ollama.is_reachable.side_effect = [False, True]

    def _query(text, task=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("[Errno 111] Connection refused")
        return [0.1]

    svc.embed_code_query.side_effect = _query
    assert embed_query_or_raise("code", svc, "x") == [0.1]
    assert calls["n"] == 2


# --- MCP tools -------------------------------------------------------------


def _server():
    from weaviate_mcp import server

    return server


def test_search_code_graph_returns_the_loud_payload(monkeypatch):
    server = _server()
    svc, _o, code_m = _codesage_service([False])
    monkeypatch.setattr(server, "_get_embedding_service", lambda: svc)
    monkeypatch.setattr(server, "get_weaviate_client", lambda *a, **k: pytest.fail(
        "an un-embeddable query must not reach Weaviate"))
    try:
        out = json.loads(asyncio.run(server.search_code_graph("auth middleware")))
    finally:
        svc.close()
    assert out["success"] is False
    assert out["error_class"] == "EmbeddingBackendUnavailable"
    assert out["container"] == "vco_code_embed"
    assert NO_SWITCH_MARK in out["error"]
    code_m.embed.assert_not_called()


def test_hybrid_search_returns_the_loud_payload(monkeypatch):
    server = _server()

    async def _down(query, scheme="kg", task=None):
        raise EmbeddingBackendUnavailable(
            "KG embedding backend unavailable: Ollama (vco_ollama) … " + NO_SWITCH_MARK,
            scheme="kg", model="qwen3-embedding:0.6b", container="vco_ollama")

    monkeypatch.setattr(server, "EMBEDDING_SOURCE", "qwen3")
    monkeypatch.setattr(server, "_get_search_vector", _down)
    monkeypatch.setattr(server, "_assert_workspace_unchanged", lambda *_a: None)
    out = json.loads(asyncio.run(server.hybrid_search("how does x work")))
    assert out["success"] is False
    assert out["error_class"] == "EmbeddingBackendUnavailable"
    assert out["container"] == "vco_ollama" and out["count"] == 0


# --- hook path -------------------------------------------------------------


def test_hook_prints_one_unavailable_line(monkeypatch, tmp_path, capsys):
    from tests.common import rl_kg_search_harness as H

    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    import importlib

    mod = H.import_rl_kg_search()
    state: dict = {}
    H.install_fakes(
        importlib.import_module("weaviate_mcp.server"),
        importlib.import_module("claude_mcp_servers.rl_client.search_pipeline"),
        cfg={"collections": ["ProjA_KnowledgeGraph"]}, state=state,
        patch=monkeypatch.setattr,
    )

    async def _down(_q, task=None):
        raise EmbeddingBackendUnavailable("down " + NO_SWITCH_MARK, scheme="kg")

    monkeypatch.setattr(importlib.import_module("weaviate_mcp.server"),
                        "_get_search_vector", _down)
    H.run_main(mod)
    out = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert out == [hook_unavailable_line("kg")]
    assert state["queried"] == [], "no collection is searched without a query vector"
    assert is_hook_unavailable_text(out[0])


def test_router_never_caches_the_unavailable_line(tmp_path):
    scripts = str(REPO_ROOT / "claude_mcp_servers" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import hook_context_router as router

    router.cache_leg_output(str(tmp_path), "k-down", hook_unavailable_line("kg") + "\n")
    assert router.cache_get(str(tmp_path), "k-down") is None
    router.cache_leg_output(str(tmp_path), "k-ok", "KG: some-node | score=0.9\n")
    assert router.cache_get(str(tmp_path), "k-ok") == "KG: some-node | score=0.9\n"
    assert is_hook_unavailable_text(hook_unavailable_line("code"))
