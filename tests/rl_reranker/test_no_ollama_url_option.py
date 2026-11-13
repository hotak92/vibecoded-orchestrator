# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — the reranker server has no ``--ollama-url`` any more.

Since the server scores the vectors the client sends
(``rl_client/rerank_wire.py``, protocol v2) it embeds nothing and has no
Ollama to reach: the option was unread and is removed (superseded by the
client-sent vectors). These tests pin the removal — against the pre-fix
server, ``--ollama-url`` was accepted and ``OLLAMA_URL`` fed its default.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

rl_server = pytest.importorskip("rl_reranker.rl_server")


def test_parse_args_rejects_ollama_url():
    """The option is gone: argparse must refuse it, not accept-and-ignore."""
    with pytest.raises(SystemExit) as exc:
        rl_server._parse_args(["--ollama-url", "http://localhost:11435"])
    assert exc.value.code != 0


def test_ollama_url_env_is_not_read(monkeypatch):
    """``OLLAMA_URL`` no longer feeds anything (other consumers of that
    variable — the embedding services, the weaviate-kg MCP — are
    untouched; this pins only the reranker server)."""
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")
    args = rl_server._parse_args([])
    assert not hasattr(args, "ollama_url")


def test_server_constructs_without_ollama_url(tmp_path, monkeypatch):
    """``RLServer.__init__`` takes no ``ollama_url`` (and the constructor
    it forwards to no longer receives one)."""
    monkeypatch.setenv("RL_DEVICE", "cpu")
    srv = rl_server.RLServer(
        project_root=tmp_path / "project",
        log_path=str(tmp_path / "logs" / "rl_events.jsonl"),
        port=0,
    )
    assert srv is not None
