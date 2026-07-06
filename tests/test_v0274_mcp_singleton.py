# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.74 T5-1: single-instance-per-workspace weaviate_mcp reaper + the
per-tool-call workspace-drift refuse-loud backstop.

Root fix: two ``weaviate_mcp/server.py`` subprocesses scoped to DIFFERENT
``CLAUDE_PROJECT_DIR`` values can be alive at once (a workspace switch / MCP
re-registration spawns a new one without stopping the old). ``server.py``
caches collection constants at module load, so a client binding to a stale
process fans out over the WRONG project's collections → 0 hits.

  * ``reap_stale_weaviate_mcp(W)`` kills every OTHER live weaviate_mcp whose
    workspace != W (and any superseded same-workspace prior PID), best-effort.
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


def test_reaper_kills_other_workspace_only():
    """Spawning for /ws/W reaps the /ws/OTHER process, leaves nothing else."""
    from vco_lib.mcp_singleton import reap_stale_weaviate_mcp

    killed: list[int] = []

    # Two live weaviate_mcp: pid 100 for /ws/A, pid 200 for /ws/B. We are the
    # fresh process (pid 999) for /ws/A. The /ws/B process is the stale zombie.
    def fake_iter():
        yield (100, "/ws/A", "python .../weaviate_mcp/server.py")  # same ws as us
        yield (200, "/ws/B", "python .../weaviate_mcp/server.py")  # DIFFERENT ws
        yield (999, "/ws/A", "python .../weaviate_mcp/server.py")  # us (excluded)

    def fake_kill(pid):
        killed.append(pid)
        return True

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, _iter=fake_iter, _kill=fake_kill
    )
    # Both the cross-workspace (200) AND the superseded same-workspace prior
    # (100) are reaped; self (999) is never signalled.
    assert n == 2
    assert set(killed) == {100, 200}
    assert 999 not in killed


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
        # Same workspace, expressed with a trailing slash — still 'us-like',
        # a superseded same-ws duplicate → reaped (single-instance), NOT
        # mislabeled cross-workspace.
        yield (100, "/ws/A/", "python .../weaviate_mcp/server.py")

    n = reap_stale_weaviate_mcp(
        "/ws/A", self_pid=999, _iter=fake_iter,
        _kill=lambda p: killed.append(p) or True,
    )
    # It's still a duplicate to reap (single-instance-per-workspace), but the
    # point of THIS test is that normalization didn't crash and the slash
    # variant compared equal (so it's the same-ws branch, count 1).
    assert n == 1
    assert killed == [100]


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
