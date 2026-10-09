# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W4-G items 2, 3, 6 — one model copy for GENERATION, W4-E/W4-H leftovers.

* Item 3: every Ollama ``/api/generate`` caller builds its body through
  ``vco_lib.ollama_generate.generate_request_body`` — the SAME load-time
  options (``num_ctx``) and keep_alive for the same model, so Ollama keeps
  ONE resident runner (W4-E measured ``qwen3.5:9b`` loaded twice: 32768 from
  the KG-summary ladder, unset from the node-formats generator).
* Item 2 (W4-E §7): the code-graph query CLI's raw CodeEmbed fallback
  retries a transient 503 instead of dropping the embed. (The weaviate-MCP
  NL-code-query generate leg it also covered was removed by owner decision
  2026-10-09.)
* Item 6 (W4-H review F2-F3): static catalogue lists Opus/Sonnet 5.5; the
  Haiku API call sends no temperature.

In-memory fakes only — no Ollama, no code-embed service, no Anthropic API.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers"),
           str(REPO_ROOT / "templates" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vco_lib.ollama_generate as og  # noqa: E402

MODEL = "qwen3.5:9b"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("VCO_OLLAMA_KEEP_ALIVE", og.GENERATE_NUM_CTX_ENV, "KG_SUMMARY_OLLAMA_OPTIONS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VCO_EMBED_503_RETRY_DELAY", "0")


# ─── the builder ─────────────────────────────────────────────────────────


def test_builder_owns_load_time_options_and_keep_alive():
    body = og.generate_request_body(
        MODEL, prompt="p", options={"temperature": 0.3, "num_ctx": 2048, "num_gpu": 1},
    )
    assert body["options"] == {"temperature": 0.3, "num_ctx": og.GENERATE_NUM_CTX_DEFAULT}
    assert body["keep_alive"] == "10m"
    assert body["stream"] is False and "think" not in body and "system" not in body


def test_num_ctx_env_override_changes_every_body(monkeypatch):
    monkeypatch.setenv(og.GENERATE_NUM_CTX_ENV, "16384")
    assert og.generate_request_body(MODEL, prompt="p")["options"]["num_ctx"] == 16384
    monkeypatch.setenv(og.GENERATE_NUM_CTX_ENV, "nonsense")
    assert og.generate_num_ctx() == og.GENERATE_NUM_CTX_DEFAULT


# ─── every caller, one shape ─────────────────────────────────────────────


def _capture_requests_post(captured):
    def _post(url, json=None, timeout=None, **kw):  # noqa: A002
        captured.append({"url": url, "json": json})
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"response": "ok"}
        return resp

    return _post


def _summary_backends_body(monkeypatch):
    sb = importlib.import_module("summary_backends")
    seen: list = []

    class _Resp:
        def read(self):
            return b'{"response": "ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(req, timeout=None):
        seen.append(json.loads(req.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr(sb.urllib.request, "urlopen", _urlopen)
    sb.call_ollama("prompt", model=MODEL)
    return seen[0]


def _node_formats_body(monkeypatch):
    gnf = importlib.import_module("claude_mcp_servers.scripts.generate_node_formats")
    seen: list = []
    monkeypatch.setattr(gnf.requests, "post", _capture_requests_post(seen))
    gnf.call_ollama(MODEL, "prompt", 64)
    return seen[0]["json"]


def _process_documents_body(monkeypatch, model=MODEL):
    pd = importlib.import_module("process_documents")
    sb = importlib.import_module("summary_backends")
    import requests

    monkeypatch.setattr(sb, "OLLAMA_DEFAULT_MODEL", model)

    seen: list = []
    monkeypatch.setattr(requests, "post", _capture_requests_post(seen))
    pd.generate_document_summary(None, "Title", "content")
    return seen[0]["json"]


CALLERS = {
    "summary_backends.call_ollama": _summary_backends_body,
    "generate_node_formats.call_ollama": _node_formats_body,
    "process_documents.generate_document_summary": _process_documents_body,
}


def _load_identity(body):
    opts = body.get("options") or {}
    return (
        body.get("model"),
        tuple(sorted((k, opts.get(k)) for k in og.LOAD_TIME_OPTIONS)),
        body.get("keep_alive"),
    )


def test_every_generate_caller_sends_one_load_identity(monkeypatch):
    """The identity test: the same model gets the SAME load-time options and
    keep_alive from every caller — one resident copy (red pre-fix: the
    summary ladder sent num_ctx 32768, the others none)."""
    identities = {name: _load_identity(fn(monkeypatch)) for name, fn in CALLERS.items()}
    assert len(set(identities.values())) == 1, identities
    model, load, keep_alive = next(iter(identities.values()))
    assert model == MODEL and keep_alive == "10m"
    assert dict(load)["num_ctx"] == og.GENERATE_NUM_CTX_DEFAULT


def test_kg_summary_options_num_ctx_is_superseded_sampling_kept(monkeypatch):
    monkeypatch.setenv("KG_SUMMARY_OLLAMA_OPTIONS", '{"temperature": 0.9, "num_ctx": 4096}')
    body = _summary_backends_body(monkeypatch)
    assert body["options"]["temperature"] == 0.9
    assert body["options"]["num_ctx"] == og.GENERATE_NUM_CTX_DEFAULT
    monkeypatch.setenv(og.GENERATE_NUM_CTX_ENV, "4096")  # the superseding knob
    assert _summary_backends_body(monkeypatch)["options"]["num_ctx"] == 4096


def test_process_documents_uses_the_machines_generation_model(monkeypatch):
    """Coordinator decision 2026-10-09: no hard-coded qwen3.5:9b — the model
    is the KG-summary ladder's (`summary_backends.OLLAMA_DEFAULT_MODEL`),
    so a machine whose ladder runs gemma4:e4b keeps ONE model resident."""
    body = _process_documents_body(monkeypatch, model="gemma4:e4b")
    assert body["model"] == "gemma4:e4b"


def test_node_formats_prefers_the_ladder_model(monkeypatch):
    gnf = importlib.import_module("claude_mcp_servers.scripts.generate_node_formats")
    monkeypatch.setattr(gnf._sb, "OLLAMA_DEFAULT_MODEL", "gemma4:e4b")
    assert gnf._generation_candidates()[0] == "gemma4:e4b"
    assert gnf.pick_model(gnf._generation_candidates(),
                          ["qwen3.5:9b", "gemma4:e4b"]) == "gemma4:e4b"


# ─── item 2: code-graph CLI raw CodeEmbed fallback retries ───────────────


def test_query_code_graph_codeembed_fallback_retries_503(monkeypatch):
    qcg = importlib.import_module("query_code_graph")
    import requests

    calls: list = []

    def _post(url, json=None, timeout=None, **kw):  # noqa: A002
        calls.append(url)
        resp = mock.MagicMock()
        if len(calls) < 3:
            resp.status_code, resp.text = 503, "busy"
        else:
            resp.status_code = 200
            resp.json.return_value = {"embedding": [0.5, 0.5]}
        return resp

    monkeypatch.setattr(qcg, "_get_or_create_embedding_service", lambda: None)
    monkeypatch.setattr(requests, "post", _post)
    assert qcg.generate_code_embedding("def f(): pass") == [0.5, 0.5]
    assert len(calls) == 3 and calls[0].endswith("/api/embeddings")


def test_query_code_graph_codeembed_non_transient_is_not_retried(monkeypatch):
    qcg = importlib.import_module("query_code_graph")
    import requests

    calls: list = []

    def _post(url, **kw):
        calls.append(url)
        return SimpleNamespace(status_code=400, text="bad")

    monkeypatch.setattr(qcg, "_get_or_create_embedding_service", lambda: None)
    monkeypatch.setattr(requests, "post", _post)
    assert qcg.generate_code_embedding("x") is None
    assert len(calls) == 1


# ─── item 6: W4-H review F1-F3 ───────────────────────────────────────────


def test_static_catalogue_lists_opus_and_sonnet_5_5():
    payload = json.loads(
        (REPO_ROOT / "claude_mcp_servers" / "model_router" / "static_catalog.json").read_text()
    )
    ids = [m["id"] for m in payload["families"]["anthropic"]["models"]]
    assert "claude-opus-5-5" in ids and "claude-sonnet-5-5" in ids
    assert "not a maintained list" not in payload["_comment"]


def test_haiku_api_call_sends_no_temperature(monkeypatch):
    gnf = importlib.import_module("claude_mcp_servers.scripts.generate_node_formats")
    captured: dict = {}

    def _post(url, **kw):
        captured.update(kw["json"])
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"content": [{"text": "ok"}]})

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    monkeypatch.setattr(gnf.requests, "post", _post)
    assert gnf.call_haiku("p", 64) == "ok"
    assert "temperature" not in captured
    assert os.environ.get("ANTHROPIC_API_KEY") == "test-not-a-key"


def test_process_documents_broken_install_fails_loudly(monkeypatch):
    """W4-G review F2: an unimportable builder is a broken install — it must
    raise, not degrade to the "Document about <title>" default summary."""
    pd = importlib.import_module("process_documents")
    monkeypatch.setitem(sys.modules, "vco_lib.ollama_generate", None)
    with pytest.raises(ImportError):
        pd.generate_document_summary(None, "Title", "content")
