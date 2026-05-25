# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Excalidraw wrapper MCP — proxies the in-tree-vendored
``excalidraw-mcp-server`` with per-project tool filtering + scoped-path
enforcement (Phase 2 of the diagrams-integration plan, 2026-05-24).

Entry point::

    python -m claude_mcp_servers.wrappers.excalidraw_proxy

Registered in ``~/.claude.json`` by the launcher's
``--register-default-mcps`` flow (NOT by direct npm install — the
wrapper proxies the vendored fork).

Spawn shape
-----------
Unlike :mod:`mermaid_proxy` (which spawns ``npx -y claude-mermaid@<pin>``
because Mermaid MCP is a published registry package), Excalidraw's
``bundled_mcp_versions.toml::[npm.excalidraw_mcp]`` uses a
``file:vco_lib/excalidraw_mcp_fork`` pin. The vendored copy lives
in-tree and is invoked directly via Node:

    node <repo-root>/vco_lib/excalidraw_mcp_fork/dist/mcp/index.js

(The vendored tree moved from ``claude_mcp_servers/`` to ``vco_lib/``
in v0.2.34 so it ships in the Python wheel — see
``vco_lib/bundled_versions.py`` for the rationale. The resolver itself
is unchanged: it still computes ``repo_root / <pin-relative-path>``,
which now resolves to the new location automatically because the pin
in the .toml moved too.)

This avoids:

  * needing the user's global npm prefix to be writable at install time,
  * a ~10 MB transitive-dependency tree per project,
  * any registry-fetch path that could drift from the vendored version.

``node`` resolution is cross-OS via :func:`shutil.which`, which handles
``node.exe`` on Windows. If ``node`` is missing we exit 1 with a clear
message — same posture as the Mermaid wrapper's npx check.

Per-call enforcement
--------------------
The upstream ``excalidraw-mcp-server`` v2.0.0 exposes 16 tools (verified
2026-05-25 via ``grep -hoE "server\\.tool\\('[a-z_]+'" dist/mcp/index.js``).
Of those, only ``export_scene`` actually writes files to disk (PNG / SVG
export). For ``export_scene`` we extract the destination path from the
call arguments and run it through :func:`vco_lib.diagram_paths.validate_scoped_path`
with ``kind="excalidraw"`` only when the path's extension is
``.excalidraw`` — for image exports (``.svg`` / ``.png``) the scoped-path
rule doesn't apply (those land wherever the user asked, like any other
file-write tool).

The other 15 tools (``create_element``, ``update_element``,
``query_elements``, ``get_resource``, etc.) operate on the in-memory
scene store; no path validation is needed for them. Allowlist
filtering still applies — that's the wrapper's primary job — but
this module's :meth:`validate_tool_call` only fires the scoped-path
check for ``export_scene``.

Post-tool hook
--------------
The upstream MCP keeps scene state in memory; persistence to disk is
the user's job (via ``export_scene`` for renders, or via the Save
button in the embedded Svelte editor for ``.excalidraw`` JSON dumps).
We DO want the indexer to fire when the embedded editor saves a
``.excalidraw`` file — but that path goes through Tauri's
``write_text_file`` command + the ``PostToolUse`` hook on
``Write(.claude/diagrams/**)``, NOT through this wrapper. So
:meth:`post_tool_success` is intentionally a no-op for now: there's
no MCP-routed save that puts a ``.excalidraw`` file on disk in the
v2.0.0 upstream surface.

If a future upstream version adds a ``save_scene``-equivalent tool
that does persist to disk, extend the ``_SAVE_TOOLS`` set + the
indexer dispatch chain — same shape as :mod:`mermaid_proxy`.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from ._base import WrapperMCP
except ImportError:  # pragma: no cover — script-mode invocation
    _here = Path(__file__).resolve().parent.parent.parent
    if str(_here) not in sys.path:
        sys.path.insert(0, str(_here))
    from claude_mcp_servers.wrappers._base import WrapperMCP

try:
    from vco_lib.bundled_versions import load_bundled_versions
    from vco_lib.diagram_paths import validate_scoped_path
except ImportError:  # pragma: no cover — script-mode invocation
    _here = Path(__file__).resolve().parent.parent.parent
    if str(_here) not in sys.path:
        sys.path.insert(0, str(_here))
    from vco_lib.bundled_versions import load_bundled_versions
    from vco_lib.diagram_paths import validate_scoped_path


logger = logging.getLogger(__name__)


# ─── Tool-classification constants ──────────────────────────────────────
#
# Tools whose arguments may include a destination path subject to the
# scoped-path rule. For Excalidraw v2.0.0 this is just ``export_scene``;
# everything else mutates the in-memory scene store.
#
# Argument-key fallback chain mirrors :mod:`mermaid_proxy` so both
# wrappers reject identical key shapes.
_PATH_ENFORCED_TOOLS: frozenset[str] = frozenset({
    "export_scene",
})
_PATH_ARG_KEYS: tuple[str, ...] = (
    "path",
    "file_path",
    "output",
    "output_path",
    "dest",
    "destination",
)


class ExcalidrawProxy(WrapperMCP):
    """Wrapper for the vendored ``excalidraw-mcp-server`` Node MCP."""

    def __init__(self) -> None:
        upstream_argv = _resolve_upstream_argv()
        super().__init__(
            mcp_name="excalidraw",
            upstream_argv=upstream_argv,
        )

    # ─── Per-call validation: scoped-path enforcement ──────────────

    def validate_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        if tool_name not in _PATH_ENFORCED_TOOLS:
            return None

        path_value: str | None = None
        for key in _PATH_ARG_KEYS:
            v = arguments.get(key)
            if isinstance(v, str) and v:
                path_value = v
                break

        if path_value is None:
            return None

        # Only enforce the scoped-path rule for actual ``.excalidraw``
        # saves — ``export_scene`` can also emit ``.svg`` / ``.png``
        # images that land wherever the user wants (same posture as
        # writing any other artefact to disk). Suffix check is
        # case-insensitive because the upstream tool doesn't normalise.
        lower = path_value.lower()
        if not lower.endswith(".excalidraw"):
            return None

        return validate_scoped_path(path_value, kind="excalidraw")

    # ─── Post-success hook (intentionally inert for v2.0.0) ───────

    async def post_tool_success(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        # v2.0.0 upstream has no MCP-routed tool that writes a
        # ``.excalidraw`` file to disk; the embedded launcher editor is
        # what produces those files (via Tauri ``write_text_file`` +
        # the ``PostToolUse(Write(.claude/diagrams/**))`` hook chain).
        # Indexer dispatch happens there, not here.
        #
        # If a future upstream version adds an MCP tool that DOES
        # persist a scene to disk, mirror :class:`MermaidProxy` here:
        # detect the tool name, extract the path-arg via _PATH_ARG_KEYS,
        # call ``vco_lib.diagram_indexer.index_diagram_async``.
        return None


# ─── Upstream argv resolution ────────────────────────────────────────────


def _resolve_upstream_argv() -> list[str]:
    """Build the spawn argv for the vendored Excalidraw MCP.

    Reads ``bundled_mcp_versions.toml`` so a vendor swap auto-propagates
    via the ``package = "file:..."`` pin. Raises SystemExit(1) with a
    clear message if ``node`` is missing or the vendored entry point
    isn't where the pin says it is.
    """
    node = shutil.which("node")
    if node is None:
        sys.stderr.write(
            "[excalidraw_proxy] ERROR: node not found on PATH. The "
            "Excalidraw wrapper MCP needs Node.js to spawn the "
            "vendored `excalidraw-mcp-server` package. Install Node.js "
            "18+ (https://nodejs.org) and reopen Claude Code.\n"
        )
        raise SystemExit(1)

    try:
        pins = load_bundled_versions()
        spec = pins["npm"]["excalidraw_mcp"]
        package_pin = spec["package"]
    except (KeyError, RuntimeError) as e:
        sys.stderr.write(
            f"[excalidraw_proxy] ERROR: cannot resolve excalidraw pin from "
            f"bundled_mcp_versions.toml: {e}\n"
        )
        raise SystemExit(1)

    # Two supported pin shapes:
    #   1. ``file:<relative-or-absolute-path>`` — vendored in-tree (the
    #      Phase 2 default). Resolve relative to the repo root (=
    #      parent of ``vco_lib``).
    #   2. ``<npm-pkg-name>`` (no ``file:`` prefix) — registry-published
    #      fallback for a future where we switch back to a real npm
    #      package. We spawn via ``npx -y <pkg>@<version>`` matching the
    #      Mermaid wrapper shape.
    if package_pin.startswith("file:"):
        rel = package_pin[len("file:"):]
        repo_root = Path(__file__).resolve().parent.parent.parent
        candidate = (repo_root / rel).resolve()
        entry = candidate / "dist" / "mcp" / "index.js"
        if not entry.is_file():
            sys.stderr.write(
                f"[excalidraw_proxy] ERROR: vendored entry point not "
                f"found at {entry}. Check that {candidate} contains the "
                f"vendored excalidraw-mcp-server tree (see "
                f"vco_lib/excalidraw_mcp_fork/VENDORED.md).\n"
            )
            raise SystemExit(1)
        return [node, str(entry)]

    npx = shutil.which("npx")
    if npx is None:
        sys.stderr.write(
            "[excalidraw_proxy] ERROR: npm-registry pin requires `npx` "
            "on PATH but it was not found. Install Node.js 18+ or "
            "switch back to a file: pin.\n"
        )
        raise SystemExit(1)
    version = spec.get("version", "")
    if not version:
        sys.stderr.write(
            "[excalidraw_proxy] ERROR: registry pin missing version "
            "in bundled_mcp_versions.toml::[npm.excalidraw_mcp].\n"
        )
        raise SystemExit(1)
    return [npx, "-y", f"{package_pin}@{version}"]


# ─── Entry point ─────────────────────────────────────────────────────────


def main() -> None:
    """``python -m claude_mcp_servers.wrappers.excalidraw_proxy`` entry."""
    # Log to stderr so we don't interfere with the stdout JSON-RPC channel.
    logging.basicConfig(
        level=logging.INFO,
        format="[excalidraw_proxy] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    proxy = ExcalidrawProxy()
    asyncio.run(proxy.run())


if __name__ == "__main__":
    main()
