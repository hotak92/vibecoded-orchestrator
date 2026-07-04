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
