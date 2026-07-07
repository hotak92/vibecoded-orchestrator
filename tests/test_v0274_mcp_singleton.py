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


_SRV = "python .../weaviate_mcp/server.py"
_MY_PPID = 4242  # the fake "our harness" parent for these tests


def test_reaper_full_matrix_cross_ws_and_parenthood():
    """H-1 + F1 (Fable review): a peer is reaped ONLY when it is BOTH
    cross-workspace AND (spawned by OUR OWN parent — a superseded sibling — OR
    orphaned). Matrix:
      * same-ws, any parent            → KEEP (concurrent same-project session)
      * cross-ws, SAME parent          → REAP (superseded within our harness)
      * cross-ws, different LIVE parent→ KEEP (another session's serving MCP —
                                          killing it caused the kill/respawn
                                          ping-pong F1 flagged)
      * cross-ws, ORPHANED parent      → REAP (nobody holds its pipe)
      * self                           → excluded
    """
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    killed: list[int] = []
    ORPHAN_PPIDS = {1}

    def fake_iter():
        yield (100, "/ws/A", _SRV, _MY_PPID)   # same-ws, same parent → KEEP
        yield (150, "/ws/A", _SRV, 7777)       # same-ws, other parent → KEEP
        yield (200, "/ws/B", _SRV, _MY_PPID)   # cross-ws, SAME parent → REAP
        yield (300, "/ws/C", _SRV, 8888)       # cross-ws, live foreign parent → KEEP
        yield (400, "/ws/D", _SRV, 1)          # cross-ws, orphaned → REAP
        yield (999, "/ws/A", _SRV, _MY_PPID)   # us (excluded)

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, self_ppid=_MY_PPID, _iter=fake_iter,
        _kill=lambda p: killed.append(p) or True,
        _orphaned=lambda ppid: ppid in ORPHAN_PPIDS,
    )
    assert n == 2
    assert killed == [200, 400]
    assert 100 not in killed and 150 not in killed, "same-ws peers preserved"
    assert 300 not in killed, (
        "F1: a cross-workspace peer with a live foreign parent is another "
        "session's MCP — must never be reaped"
    )
    assert 999 not in killed


def test_reaper_never_kills_when_own_workspace_unknown():
    """Conservative: if OUR workspace is unknown (empty), we cannot prove any
    peer is cross-workspace → reap NOTHING (never kill on uncertainty)."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    killed: list[int] = []

    def fake_iter():
        yield (200, "/ws/B", _SRV, _MY_PPID)

    n = reap_stale_weaviate_mcp(
        "", self_pid=999, self_ppid=_MY_PPID, _iter=fake_iter,
        _kill=lambda p: killed.append(p) or True,
        _orphaned=lambda p: False,
    )
    assert n == 0 and killed == []


def test_reaper_never_kills_peer_with_unknown_workspace():
    """A peer whose CLAUDE_PROJECT_DIR is unknown (empty) is NOT reaped — we
    can't prove it's cross-workspace."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    killed: list[int] = []

    def fake_iter():
        yield (200, "", _SRV, _MY_PPID)  # unknown ws

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, self_ppid=_MY_PPID, _iter=fake_iter,
        _kill=lambda p: killed.append(p) or True,
        _orphaned=lambda p: False,
    )
    assert n == 0 and killed == []


def test_reaper_never_kills_peer_with_unknown_ppid():
    """Unknown parenthood (ppid None, or a legacy 3-tuple iterator) → NOT
    reaped, even cross-workspace — no kill on uncertainty."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    killed: list[int] = []

    def fake_iter():
        yield (200, "/ws/B", _SRV, None)   # 4-tuple, ppid unknown
        yield (300, "/ws/C", _SRV)         # legacy 3-tuple (no ppid at all)

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, self_ppid=_MY_PPID, _iter=fake_iter,
        _kill=lambda p: killed.append(p) or True,
        _orphaned=lambda p: True,  # even a always-orphan predicate can't fire
    )
    assert n == 0 and killed == []


def test_reaper_never_signals_self():
    """The fresh process's own PID is always excluded."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    killed: list[int] = []

    def fake_iter():
        yield (555, "/ws/A", _SRV, _MY_PPID)  # us only

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=555, self_ppid=_MY_PPID, _iter=fake_iter,
        _kill=lambda p: killed.append(p) or True,
        _orphaned=lambda p: False,
    )
    assert n == 0
    assert killed == []


def test_reaper_soft_fails_on_enumeration_error():
    """An iterator that raises must not propagate — returns count so far."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    def boom_iter():
        yield (100, "/ws/B", _SRV, _MY_PPID)  # reapable (cross-ws, same parent)
        raise RuntimeError("proc scan blew up mid-iteration")

    killed: list[int] = []
    # Must NOT raise; the one candidate before the boom is still reaped.
    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, self_ppid=_MY_PPID, _iter=boom_iter,
        _kill=lambda p: killed.append(p) or True,
        _orphaned=lambda p: False,
    )
    assert n == 1
    assert killed == [100]


def test_reaper_soft_fails_on_kill_error():
    """A kill that returns False (race / no perm) is not counted, no crash."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    def fake_iter():
        yield (100, "/ws/B", _SRV, _MY_PPID)
        yield (200, "/ws/C", _SRV, _MY_PPID)

    # pid 100 already gone (kill returns False); 200 killed OK.
    def fake_kill(pid):
        return pid == 200

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, self_ppid=_MY_PPID, _iter=fake_iter,
        _kill=fake_kill, _orphaned=lambda p: False,
    )
    assert n == 1  # only 200 counted


def test_reaper_normalizes_trailing_slash():
    """/ws/A and /ws/A/ are the SAME workspace — not reaped as cross-ws."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    killed: list[int] = []

    def fake_iter():
        # Same workspace, expressed with a trailing slash — must normalize equal
        # to ours (/ws/A). Same-ws peers are KEPT, so the slash normalization is
        # what proves it's classified same-ws (not spuriously cross-ws → which
        # would wrongly reap it, since it's same-parent and would pass rule 2).
        yield (100, "/ws/A/", _SRV, _MY_PPID)

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, self_ppid=_MY_PPID, _iter=fake_iter,
        _kill=lambda p: killed.append(p) or True,
        _orphaned=lambda p: False,
    )
    assert n == 0
    assert killed == []


def test_peer_is_orphaned_init_and_dead_parent():
    """_peer_is_orphaned: ppid<=1 → orphan; a live real parent (our own pid,
    which certainly exists and is python, not systemd/init) → NOT orphan."""
    import os as _os
    from vco_lib.mcp_singleton import _peer_is_orphaned

    assert _peer_is_orphaned(1) is True
    assert _peer_is_orphaned(0) is True
    # Our own live python process is a definitely-alive, non-init parent.
    assert _peer_is_orphaned(_os.getpid()) is False


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
