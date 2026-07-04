# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 M-1 regression: rl_enrichment's `server` reference must survive a
re-import of weaviate_mcp.server.

The monolith split (M-1) moved 29 functions into ``rl_enrichment.py`` that read
server-module state as ``server.<name>``. An EAGER ``from . import server``
would bind ONE server module object at import time — so if a later test purges
+ re-imports ``weaviate_mcp`` (many tests do, to exercise env-at-import-time
behaviour) while leaving a stale ``rl_enrichment`` in ``sys.modules``, the
re-exported functions would keep resolving the STALE server, and a
``monkeypatch.setattr(fresh_server, …)`` would NOT be observed → code-path
emit tests silently used the real hub writer and captured nothing.

The fix is a lazy ``server`` proxy that forwards to the LIVE
``sys.modules["weaviate_mcp.server"]`` on every attribute access. These tests
pin that behaviour so a future editor doesn't reintroduce the eager binding.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_PARENT = REPO_ROOT / "claude_mcp_servers"
if str(PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(PKG_PARENT))


def _purge_weaviate_mcp():
    for m in list(sys.modules):
        if m == "weaviate_mcp" or m.startswith("weaviate_mcp."):
            del sys.modules[m]


def test_server_reference_is_a_lazy_proxy_not_an_eager_bind():
    """rl_enrichment.server must be the lazy proxy, not a bound module object.

    (If someone reverts to ``from . import server`` this becomes a real
    ModuleType and the re-import-safety below breaks.)"""
    _purge_weaviate_mcp()
    rl = importlib.import_module("weaviate_mcp.rl_enrichment")
    assert type(rl.server).__name__ == "_LazyServerProxy", (
        "rl_enrichment.server must be the lazy proxy so bare server.<name> "
        "resolves the LIVE server module (re-import safety). Someone likely "
        "reverted to an eager `from . import server`."
    )


def test_reexported_fn_sees_a_patch_on_a_reimported_server():
    """The exact desync the full-suite failure exposed: purge + re-import
    server, patch the FRESH server, and a re-exported rl_enrichment function
    must observe the patch (not a stale server)."""
    _purge_weaviate_mcp()
    srv1 = importlib.import_module("weaviate_mcp.server")
    rl1 = importlib.import_module("weaviate_mcp.rl_enrichment")
    # Sanity: the proxy resolves srv1 now.
    assert rl1.server._embedding_dim_for is srv1._embedding_dim_for

    # Simulate a polluter: purge the package + server (leaving rl1 referenced
    # by THIS test), then re-import a FRESH server.
    for m in ("weaviate_mcp", "weaviate_mcp.server"):
        sys.modules.pop(m, None)
    srv2 = importlib.import_module("weaviate_mcp.server")

    # Patch a resolver on the FRESH server object.
    sentinel = object()
    srv2._get_rl_telemetry_writer_for = lambda *a, **k: sentinel  # type: ignore[attr-defined]

    # The OLD rl module's proxy must forward to the CURRENT (srv2) server,
    # so a re-exported function's `server._get_rl_telemetry_writer_for` sees
    # the patch. We assert via the proxy directly (what the fn body does).
    assert rl1.server._get_rl_telemetry_writer_for() is sentinel, (
        "rl_enrichment.server must forward to the LIVE re-imported server — "
        "a stale eager bind would still point at srv1 and miss the patch."
    )


def test_bare_server_attr_reads_resolve_after_reimport():
    """All the ordinary server.<const> reads the 29 fns do must resolve after a
    re-import (no AttributeError from a dead proxy target)."""
    _purge_weaviate_mcp()
    importlib.import_module("weaviate_mcp.server")
    rl = importlib.import_module("weaviate_mcp.rl_enrichment")
    # A representative constant + a helper the moved fns read.
    assert isinstance(rl.server.EMBEDDING_MODEL, str)
    assert callable(rl.server._cosine)
    assert callable(rl.server._extract_obj_vector)


def test_proxy_tracks_the_callers_import_path_not_a_fixed_key():
    """DUAL-IMPORT-PATH safety: the package is importable under BOTH
    ``weaviate_mcp`` and ``claude_mcp_servers.weaviate_mcp`` — which are
    DISTINCT module objects in sys.modules. rl_enrichment's proxy must resolve
    the sibling ``server`` under ITS OWN __package__, so a patch on whichever
    server object the caller imported is the one the moved functions observe.

    Regression for the v0.2.73 round-3 failure: the proxy hard-coded
    ``weaviate_mcp.server`` while tests import
    ``claude_mcp_servers.weaviate_mcp.server`` and patch THAT object — the proxy
    resolved the wrong module and never saw the patch."""
    # Run in a SUBPROCESS: this test loads the package under the
    # ``claude_mcp_servers.*`` prefix (a DISTINCT module object from the
    # ``weaviate_mcp.*`` prefix most tests use). Doing that in the shared pytest
    # process leaves both variants in sys.modules and poisons downstream tests
    # (their proxy would resolve the wrong server object). A clean subprocess
    # proves the property with zero cross-test contamination.
    import subprocess
    driver = (
        "import sys, importlib\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'claude_mcp_servers')!r})\n"
        "srv = importlib.import_module('claude_mcp_servers.weaviate_mcp.server')\n"
        "rl = importlib.import_module('claude_mcp_servers.weaviate_mcp.rl_enrichment')\n"
        # Proxy must resolve the SAME server object the caller imported.
        "assert rl.server._live() is srv, 'proxy resolved the wrong package variant'\n"
        # A patch on THAT object must be observed via the proxy.
        "from unittest.mock import patch\n"
        "sentinel = object()\n"
        "with patch.object(srv, '_extract_obj_vector', lambda *a, **k: sentinel):\n"
        "    assert rl.server._extract_obj_vector() is sentinel, 'proxy missed the patch'\n"
        "print('DUAL_IMPORT_OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", driver], capture_output=True, text=True, timeout=90,
    )
    assert r.returncode == 0 and "DUAL_IMPORT_OK" in r.stdout, (
        "rl_enrichment.server must resolve the server under its own package "
        "(claude_mcp_servers.weaviate_mcp), not a hard-coded weaviate_mcp.server.\n"
        f"rc={r.returncode}\nstderr:\n{r.stderr[-2000:]}"
    )


def test_server_py_imports_when_run_as_a_bare_script():
    """SHIP-BLOCKER regression (v0.2.73): the launcher starts the weaviate-kg
    MCP as ``python .../weaviate_mcp/server.py`` — a BARE SCRIPT, so server.py
    runs as ``__main__`` with an empty ``__package__``. M-1's re-export blocks
    (``from .embeddings`` / ``from .rl_enrichment``) are relative imports that
    raise "attempted relative import with no known parent package" in that mode
    unless guarded with an absolute-import fallback. Without the guard the MCP
    fails to start for EVERY user on update. The whole pytest suite MISSES this
    because tests import server as a PACKAGE module — only a real bare-script
    run exercises the launcher's actual invocation.

    This test runs server.py exactly as the launcher does and asserts it gets
    past the import phase (we stub the serve loop so it exits cleanly)."""
    import subprocess

    server_py = REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
    assert server_py.exists()

    # Run server.py EXACTLY as the launcher does: ``python <path>/server.py``
    # (a bare script → __main__, empty __package__). server.py logs
    # "Starting Claude Orchestrator Weaviate MCP Server" only AFTER every import
    # — including M-1's re-export blocks — has succeeded, then blocks on the
    # stdio serve loop. So the "Starting" line is proof the import phase is
    # clean; we kill the process (timeout) once we see it. If the relative
    # imports were unguarded, server.py would traceback BEFORE that line.
    proc = subprocess.Popen(
        [sys.executable, str(server_py)],
        cwd=str(server_py.parent),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        out, _ = proc.communicate(timeout=45)
        combined = out or ""
    except subprocess.TimeoutExpired:
        # Blocked on the serve loop = imports succeeded. Capture what printed.
        proc.kill()
        out, _ = proc.communicate()
        combined = out or ""

    assert "attempted relative import" not in combined, (
        "server.py's M-1 re-export blocks must have an absolute-import fallback "
        f"for the bare-script launch. output:\n{combined[-2000:]}"
    )
    assert "Starting Claude Orchestrator Weaviate MCP Server" in combined, (
        "server.py did NOT reach its post-import 'Starting …' log line when run "
        "as a bare script — the import phase failed (likely M-1's relative "
        f"re-export imports without the absolute fallback).\noutput:\n{combined[-2000:]}"
    )
