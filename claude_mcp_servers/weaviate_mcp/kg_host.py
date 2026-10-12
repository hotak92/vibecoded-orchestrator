# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Which module answers ``server.<name>`` for the KG query layer (v0.2.102 W6-M).

The moved KG query/format layer (``kg_query_core``) and its satellites
(``embeddings``, ``rl_enrichment``, ``rl_client.*``) read shared state and
patchable helpers through "the server" — historically by importing
``weaviate_mcp.server``. That import costs ~0.7 s (weaviate-client + the MCP
SDK) and the hook KG producer pays it on every prompt.

:func:`host` keeps the pre-W6-M answer whenever a server module is loaded (the
MCP process, a CLI that imported it, the whole test suite — so every
``patch("…server.X")`` is still observed exactly as before) and answers with the
LIGHT host, ``kg_query_core``, only in a process where no server module exists
(the hook KG producer). The light host serves the KG query layer itself and
delegates anything else (see ``kg_query_core.__getattr__``) — the last resort
being the server import, so no caller can ever get a missing name instead of a
slow one.

Stdlib only, no state: importing this module under both package keys
(``weaviate_mcp`` / ``claude_mcp_servers.weaviate_mcp``) is harmless.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

_PACKAGES = ("weaviate_mcp", "claude_mcp_servers.weaviate_mcp")


def _ordered(package: "str | None") -> "tuple[str, ...]":
    pkg = package or _PACKAGES[0]
    return tuple(dict.fromkeys((pkg, *_PACKAGES)))


def _bare_script_server() -> "ModuleType | None":
    """The launcher's bare-script server (``python …/weaviate_mcp/server.py``)
    when it is ``__main__`` — identified STRICTLY by its file name plus a
    server-only attribute (the same rule as ``rl_enrichment``'s proxy, so a
    pytest ``__main__`` is never mistaken for it)."""
    main = sys.modules.get("__main__")
    if main is None:
        return None
    path = (getattr(main, "__file__", None) or "").replace("\\", "/")
    if path.endswith("weaviate_mcp/server.py") and hasattr(main, "_rl_client_instances"):
        return main
    return None


def server_loaded() -> bool:
    """True when ANY server module object exists in this process."""
    if _bare_script_server() is not None:
        return True
    return any(f"{pkg}.server" in sys.modules for pkg in _PACKAGES)


def host(package: "str | None" = None) -> Any:
    """The module whose attributes stand for ``server.<name>`` here.

    Typed ``Any``: callers read the host DUCK-typed (``host().X``), exactly as
    they read the ``server`` module before — its attributes are dynamic.

    * a server module is loaded → ``<package>.server`` (imported if only the
      other package key is loaded — byte-identical to the pre-W6-M
      ``from . import server`` / ``from <package> import server``);
    * no server anywhere → the light host ``kg_query_core`` (the copy already
      loaded under either package key, else imported under ``package``).
    """
    order = _ordered(package)
    if server_loaded():
        return importlib.import_module(f"{order[0]}.server")
    for pkg in order:
        mod = sys.modules.get(f"{pkg}.kg_query_core")
        if mod is not None:
            return mod
    return importlib.import_module(f"{order[0]}.kg_query_core")
