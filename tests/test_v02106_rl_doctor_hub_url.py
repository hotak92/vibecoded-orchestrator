# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — rl-doctor resolves the reranker URL through the hub too.

The MCP's ``_get_rl_client`` falls back to the hub-resolved
``rl_server_port`` (V52-AA) when ``RL_SERVER_URL`` / ``RL_SERVER_PORT`` are
unset — which they always are for MCP subprocesses on the default install.
``rl_doctor`` resolved from env only, so on a healthy install it reported
"container not wired" while the MCP was talking to the container. Both now
share ``rl_client.client.resolve_wired_base_url`` (one home).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

pytest.importorskip("claude_mcp_servers.rl_client.client")


@pytest.fixture(autouse=True)
def _no_rl_env(monkeypatch):
    monkeypatch.delenv("RL_SERVER_URL", raising=False)
    monkeypatch.delenv("RL_SERVER_PORT", raising=False)


def _fake_hub_config(monkeypatch, port):
    """Pin the hub's ProjectConfig answer: only ``rl_server_port``."""
    import claude_mcp_servers.weaviate_mcp.server as srv

    monkeypatch.setattr(
        srv, "_try_resolve_project_config",
        lambda: SimpleNamespace(rl_server_port=port, project_id="p1"),
    )


def _probe_container():
    from claude_mcp_servers.scripts import rl_doctor

    return rl_doctor._probe_container()


def test_hub_resolved_port_wires_the_doctor(monkeypatch):
    """Env unset + a hub-resolved rl_server_port → the doctor's probe is NOT
    'disabled' and targets the hub's port (the MCP's own wiring)."""
    _fake_hub_config(monkeypatch, 11450)
    res = _probe_container()
    assert res["status"] != "disabled", res
    assert res["client_base_url"] == "http://127.0.0.1:11450", res


def test_env_url_wins_over_the_hub_port(monkeypatch):
    """An explicit RL_SERVER_URL overrides the hub-resolved port (the same
    precedence the MCP applies)."""
    monkeypatch.setenv("RL_SERVER_URL", "http://127.0.0.1:11439/")
    _fake_hub_config(monkeypatch, 11450)
    res = _probe_container()
    assert res["status"] != "disabled", res
    assert res["client_base_url"] == "http://127.0.0.1:11439", res


def test_no_env_and_no_hub_port_is_still_disabled(monkeypatch):
    """Nothing wired anywhere → the honest 'disabled' verdict, now naming
    the hub channel too."""
    _fake_hub_config(monkeypatch, None)
    res = _probe_container()
    assert res["status"] == "disabled", res
    assert "rl_server_port" in res["detail"], res


def test_the_shared_resolver_is_the_one_home():
    """``rl_client.client.resolve_wired_base_url`` implements the V52-AA
    precedence exactly, and the MCP path uses it (not a second copy)."""
    from claude_mcp_servers.rl_client.client import resolve_wired_base_url

    assert resolve_wired_base_url(None) is None
    assert resolve_wired_base_url(11450) == "http://127.0.0.1:11450"


def test_mcp_get_rl_client_uses_the_shared_resolver(monkeypatch):
    """The MCP's own wiring resolves through the same function (behavioural:
    the MCP is driven against a fake hub config and its client's base_url is
    asserted — no source scan)."""
    _fake_hub_config(monkeypatch, 11450)
    import claude_mcp_servers.weaviate_mcp.server as srv
    import claude_mcp_servers.rl_client.client as rlc

    # resolve_wired_base_url is imported INSIDE _get_rl_client, so patch the
    # defining module.
    monkeypatch.setattr(
        rlc, "resolve_wired_base_url", lambda hub_port=None: f"stubbed:{hub_port}"
    )
    srv._rl_client_instances.clear()
    try:
        client = srv._get_rl_client()
        assert client is not None
        assert client.base_url == "stubbed:11450", client.base_url
    finally:
        srv._rl_client_instances.clear()
