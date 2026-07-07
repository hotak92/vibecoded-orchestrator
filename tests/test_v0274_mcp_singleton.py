# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.74 T5-1: single-instance-per-workspace weaviate_mcp reaper + the
per-tool-call workspace-drift refuse-loud backstop.

Root fix: two ``weaviate_mcp/server.py`` subprocesses scoped to DIFFERENT
``CLAUDE_PROJECT_DIR`` values can be alive at once (a workspace switch / MCP
re-registration spawns a new one without stopping the old). ``server.py``
caches collection constants at module load, so a client binding to a stale
process fans out over the WRONG project's collections → 0 hits.

  * ``reap_stale_weaviate_mcp(W)`` kills every OTHER live weaviate_mcp that is
    PROVABLY cross-workspace (its CLAUDE_PROJECT_DIR != W, both known),
    best-effort. A SAME-workspace peer is LEFT ALONE (H-1): it is harmless (its
    collections match) and may be a legitimate concurrent session.
  * ``_assert_workspace_unchanged`` (backstop) refuses-loud when a tool call's
    LIVE CLAUDE_PROJECT_DIR diverges from the value the subprocess was spawned
    with, rather than silently serving wrong-project results.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ─── reaper ──────────────────────────────────────────────────────────────


def test_reaper_kills_cross_workspace_only_keeps_same_workspace():
    """H-1: spawning for /ws/A reaps ONLY the cross-workspace /ws/B process. A
    SAME-workspace peer (pid 100, /ws/A) is a legitimate concurrent session and
    is LEFT ALONE — its collections match ours, so it's harmless, and killing it
    would terminate another live session's MCP (the regression H-1 flagged)."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    killed: list[int] = []

    def fake_iter():
        yield (100, "/ws/A", "python .../weaviate_mcp/server.py")  # SAME ws → KEEP
        yield (200, "/ws/B", "python .../weaviate_mcp/server.py")  # DIFFERENT ws → reap
        yield (999, "/ws/A", "python .../weaviate_mcp/server.py")  # us (excluded)

    def fake_kill(pid):
        killed.append(pid)
        return True

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, _iter=fake_iter, _kill=fake_kill
    )
    # ONLY the cross-workspace (200) is reaped; the same-workspace peer (100)
    # survives; self (999) is never signalled.
    assert n == 1
    assert killed == [200]
    assert 100 not in killed  # concurrent same-project session preserved
    assert 999 not in killed


def test_reaper_never_kills_when_own_workspace_unknown():
    """Conservative: if OUR workspace is unknown (empty), we cannot prove any
    peer is cross-workspace → reap NOTHING (never kill on uncertainty)."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    killed: list[int] = []

    def fake_iter():
        yield (200, "/ws/B", "python .../weaviate_mcp/server.py")

    n = reap_stale_weaviate_mcp(
        "", self_pid=999, _iter=fake_iter,
        _kill=lambda p: killed.append(p) or True,
    )
    assert n == 0 and killed == []


def test_reaper_never_kills_peer_with_unknown_workspace():
    """A peer whose CLAUDE_PROJECT_DIR is unknown (empty) is NOT reaped — we
    can't prove it's cross-workspace."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    killed: list[int] = []

    def fake_iter():
        yield (200, "", "python .../weaviate_mcp/server.py")  # unknown ws

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, _iter=fake_iter,
        _kill=lambda p: killed.append(p) or True,
    )
    assert n == 0 and killed == []


def test_reaper_never_signals_self():
    """The fresh process's own PID is always excluded."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    killed: list[int] = []

    def fake_iter():
        yield (555, "/ws/A", "python .../weaviate_mcp/server.py")  # us only

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=555, _iter=fake_iter, _kill=lambda p: killed.append(p) or True
    )
    assert n == 0
    assert killed == []


def test_reaper_soft_fails_on_enumeration_error():
    """An iterator that raises must not propagate — returns count so far."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    def boom_iter():
        yield (100, "/ws/B", "python .../weaviate_mcp/server.py")
        raise RuntimeError("proc scan blew up mid-iteration")

    killed: list[int] = []
    # Must NOT raise; the one candidate before the boom is still reaped.
    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, _iter=boom_iter,
        _kill=lambda p: killed.append(p) or True,
    )
    assert n == 1
    assert killed == [100]


def test_reaper_soft_fails_on_kill_error():
    """A kill that returns False (race / no perm) is not counted, no crash."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    def fake_iter():
        yield (100, "/ws/B", "python .../weaviate_mcp/server.py")
        yield (200, "/ws/C", "python .../weaviate_mcp/server.py")

    # pid 100 already gone (kill returns False); 200 killed OK.
    def fake_kill(pid):
        return pid == 200

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, _iter=fake_iter, _kill=fake_kill
    )
    assert n == 1  # only 200 counted


def test_reaper_normalizes_trailing_slash():
    """/ws/A and /ws/A/ are the SAME workspace — not reaped as cross-ws."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    killed: list[int] = []

    def fake_iter():
        # Same workspace, expressed with a trailing slash — must normalize equal
        # to ours (/ws/A). Under H-1 a same-workspace peer is KEPT, so the slash
        # normalization is what proves it's classified same-ws (not spuriously
        # cross-ws → which would wrongly reap it).
        yield (100, "/ws/A/", "python .../weaviate_mcp/server.py")

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, _iter=fake_iter,
        _kill=lambda p: killed.append(p) or True,
    )
    # Normalization made the slash variant compare EQUAL to ours → same-ws →
    # NOT reaped (H-1: concurrent same-project session preserved). If
    # normalization had failed, it would look cross-ws and be wrongly killed.
    assert n == 0
    assert killed == []


# ─── M-1: cmdline matcher must not match editors/pagers/grep ────────────────


def test_argv_matcher_matches_real_server_process():
    from vco_lib.mcp_singleton import _argv_is_weaviate_mcp
    assert _argv_is_weaviate_mcp(
        ["python3", "/opt/vco/claude_mcp_servers/weaviate_mcp/server.py"]
    )
    assert _argv_is_weaviate_mcp(
        ["/venv/bin/python", "-u", "/x/weaviate_mcp/server.py"]
    )
    # Windows-style backslashes normalize.
    assert _argv_is_weaviate_mcp(
        ["python.exe", "C:\\x\\weaviate_mcp\\server.py"]
    )


def test_argv_matcher_rejects_editor_pager_grep_on_the_file():
    """M-1: an editor/pager/grep OPERATING ON the file must NOT be matched (it
    would get SIGTERM'd). Requires a python argv[0] + the script as an argv
    element ending in weaviate_mcp/server.py."""
    from vco_lib.mcp_singleton import _argv_is_weaviate_mcp
    assert not _argv_is_weaviate_mcp(["vim", "/x/weaviate_mcp/server.py"])
    assert not _argv_is_weaviate_mcp(["grep", "-rn", "foo", "/x/weaviate_mcp/server.py"])
    assert not _argv_is_weaviate_mcp(["tail", "-f", "/x/weaviate_mcp/server.py.log"])
    assert not _argv_is_weaviate_mcp(["less", "/x/weaviate_mcp/server.py"])
    # A python process but the file is a .log/.bak (endswith fails).
    assert not _argv_is_weaviate_mcp(["python3", "/x/weaviate_mcp/server.py.bak"])
    # Empty / non-python.
    assert not _argv_is_weaviate_mcp([])
    assert not _argv_is_weaviate_mcp(["node", "/x/weaviate_mcp/server.py"])


# ─── backstop (refuse-loud on workspace drift) ─────────────────────────────
#
# HERMETIC: these tests patch the already-imported server module's
# ``_MODULE_LOAD_WORKSPACE`` attribute (via monkeypatch.setattr, auto-restored)
# and the live ``CLAUDE_PROJECT_DIR`` env (monkeypatch.setenv, auto-restored)
# rather than reloading the module. Reloading server.py (del sys.modules +
# importlib.reload) leaves a re-executed module object in sys.modules with a
# pinned env baked in, which pollutes every OTHER test in the same process
# that imports the cached server module. setattr keeps the real module intact.


@pytest.fixture
def srv():
    """The already-imported weaviate_mcp.server module (VCT_DISABLE_HUB_RESOLVER
    is set by the tests/conftest autouse fixture so config resolves via env)."""
    import claude_mcp_servers.weaviate_mcp.server as server_mod  # type: ignore

    return server_mod


def test_backstop_raises_on_drift(srv, monkeypatch, tmp_path):
    """Live CLAUDE_PROJECT_DIR != module-load value → refuse-loud."""
    ws_load = tmp_path / "wsLoad"
    ws_live = tmp_path / "wsLive"
    ws_load.mkdir()
    ws_live.mkdir()

    # Pin the spawn-time workspace + a DIFFERENT live workspace.
    monkeypatch.setattr(srv, "_MODULE_LOAD_WORKSPACE", str(ws_load))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ws_live))

    with pytest.raises(srv.WeaviateWorkspaceDriftError) as ei:
        srv._assert_workspace_unchanged("hybrid_search")
    msg = str(ei.value)
    # Names BOTH paths + the restart remediation.
    assert str(ws_load.resolve()) in msg
    assert str(ws_live.resolve()) in msg
    assert "RESTART" in msg or "restart" in msg


def test_backstop_noop_when_unchanged(srv, monkeypatch, tmp_path):
    """Same live + load workspace → no exception."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(srv, "_MODULE_LOAD_WORKSPACE", str(ws))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ws))
    srv._assert_workspace_unchanged("hybrid_search")  # must not raise


def test_backstop_noop_when_load_workspace_empty(srv, monkeypatch, tmp_path):
    """CLI / non-workspace spawn (empty load ws) → backstop never fires."""
    monkeypatch.setattr(srv, "_MODULE_LOAD_WORKSPACE", "")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "whatever"))
    # No baseline to diverge from → no raise.
    srv._assert_workspace_unchanged("hybrid_search")


def test_backstop_noop_when_live_env_empty(srv, monkeypatch, tmp_path):
    """A hook/script call that dropped the env → trust the spawn-time value."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(srv, "_MODULE_LOAD_WORKSPACE", str(ws))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    srv._assert_workspace_unchanged("hybrid_search")  # must not raise


def test_backstop_response_shape(srv, monkeypatch, tmp_path):
    """The refuse-loud wrapper response is a well-formed JSON envelope."""
    import json as _json

    ws_load = tmp_path / "L"
    ws_live = tmp_path / "V"
    ws_load.mkdir()
    ws_live.mkdir()
    monkeypatch.setattr(srv, "_MODULE_LOAD_WORKSPACE", str(ws_load))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ws_live))

    try:
        srv._assert_workspace_unchanged("hybrid_search")
        raise AssertionError("expected drift error")
    except srv.WeaviateWorkspaceDriftError as exc:
        out = srv._workspace_drift_response(exc, query="q")
    parsed = _json.loads(out)
    assert parsed["error"] is True
    assert parsed["error_class"] == "WeaviateWorkspaceDriftError"
    assert parsed["results"] == []
    assert "restart" in parsed["message"].lower()


def test_drift_backstop_wired_into_all_three_tools():
    """v0.2.74 T5-1 follow-up (zero-deferral): the workspace-drift guard must be
    called by ALL THREE workspace-scoped tools — a read to the wrong project
    returns 0 hits, but a store_knowledge_node WRITE to the wrong project
    CORRUPTS its KG, so the guard must not be scoped to hybrid_search alone.
    Source-level pin so a refactor can't silently drop it from two of them."""
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parent.parent
           / "claude_mcp_servers" / "weaviate_mcp" / "server.py").read_text(encoding="utf-8")
    for tool in ("hybrid_search", "semantic_graph_search", "store_knowledge_node"):
        assert f'_assert_workspace_unchanged("{tool}")' in src, (
            f"{tool} must call the workspace-drift backstop "
            f"_assert_workspace_unchanged(\"{tool}\")"
        )
        # And each must catch WeaviateWorkspaceDriftError (not let it fall into a
        # generic handler that would mask the actionable restart message).
    assert src.count("except WeaviateWorkspaceDriftError") >= 3, (
        "each of the 3 guarded tools must catch WeaviateWorkspaceDriftError"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
