# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Mermaid wrapper MCP — proxies ``claude-mermaid`` with per-project
tool filtering + scoped-path enforcement (Phase 1.2 of the diagrams-
integration plan, 2026-05-24).

Entry point::

    python -m claude_mcp_servers.wrappers.mermaid_proxy

Registered in ``~/.claude.json`` by the launcher's
``--register-default-mcps`` flow (NOT by direct npm install — the
wrapper proxies the npm MCP).

Spawn shape
-----------
Reads the pinned ``claude-mermaid`` version from
``bundled_mcp_versions.toml`` at startup so a bump to the pin
propagates here without any code change. Spawn command:

    npx -y claude-mermaid@<pinned-version>

Node-CLI resolution goes through :mod:`vco_lib.npx_resolver` (v0.2.91): the
fnm/nvm-aware npx ladder first, then ``npm exec --yes -- <pkg>@<ver>`` when
npx is absent but npm is present, cross-OS (``npx.cmd`` on Windows included).
Only when NEITHER resolves do we exit 1 with a clear message.

Per-call enforcement
--------------------
For ``save_diagram`` / ``render`` we extract the destination path from
the call arguments and run it through
:func:`vco_lib.diagram_paths.validate_scoped_path`. Failure → MCP
error code -32602 (Invalid params) with a corrective message. This is
defense-in-depth: the PreToolUse hook (Phase 1.5.A — sibling) ALSO
enforces the same rule on direct ``Write`` tool calls that bypass the
MCP entirely.

Post-tool hook
--------------
Successful ``save_diagram`` triggers
:func:`vco_lib.diagram_indexer.index_diagram_async` — the indexer
parses the saved file, updates SQLite + sidecar + Weaviate. The
indexer module is owned by Phase 1.5.A (sibling). We import
defensively: when the sibling module hasn't landed yet, the
ImportError is swallowed silently so the wrapper still works.
"""
from __future__ import annotations

import asyncio
import logging
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
    from vco_lib.log_setup import configure_logging
    from vco_lib.npx_resolver import package_run_argv
except ImportError:  # pragma: no cover — script-mode invocation
    _here = Path(__file__).resolve().parent.parent.parent
    if str(_here) not in sys.path:
        sys.path.insert(0, str(_here))
    from vco_lib.bundled_versions import load_bundled_versions
    from vco_lib.diagram_paths import validate_scoped_path
    from vco_lib.log_setup import configure_logging
    from vco_lib.npx_resolver import package_run_argv


logger = logging.getLogger(__name__)


# Tools whose first/key argument is a destination path the
# scoped-path rule applies to. Keep this list narrow — upstream
# may grow new tools but only the SAVE-shaped tools deserve the
# rejection, and only saves under .claude/diagrams/ matter for tag
# derivation.
#
# Argument-key fallback chain mirrors how the upstream MCP names them
# in its current and historical surface (we don't have one canonical
# arg name across versions, so check all the common shapes).
_PATH_ENFORCED_TOOLS: frozenset[str] = frozenset({
    "save_diagram",
    "render",
})
_PATH_ARG_KEYS: tuple[str, ...] = (
    "path",
    "file_path",
    "output",
    "output_path",
    "dest",
    "destination",
)


class MermaidProxy(WrapperMCP):
    """Wrapper for the ``claude-mermaid`` npm MCP."""

    def __init__(self) -> None:
        upstream_argv = _resolve_upstream_argv()
        super().__init__(
            mcp_name="mermaid",
            upstream_argv=upstream_argv,
        )

    # ─── Per-call validation: scoped-path enforcement ───────────────

    def validate_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        if tool_name not in _PATH_ENFORCED_TOOLS:
            return None

        # Find the first path-shaped arg present in the call. If NONE
        # is present, we don't have a path to validate — let the
        # upstream decide whether that's a usage error (some upstream
        # tools render to stdout / return SVG without a path).
        path_value: str | None = None
        for key in _PATH_ARG_KEYS:
            v = arguments.get(key)
            if isinstance(v, str) and v:
                path_value = v
                break

        if path_value is None:
            return None

        return validate_scoped_path(path_value, kind="mermaid")

    # ─── Post-success hook: trigger indexer ─────────────────────────

    async def post_tool_success(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        if tool_name != "save_diagram":
            return

        # Same arg-key fallback as the validator — keep the two in
        # lock-step so we never validate one key and index another.
        path_value: str | None = None
        for key in _PATH_ARG_KEYS:
            v = arguments.get(key)
            if isinstance(v, str) and v:
                path_value = v
                break
        if path_value is None:
            return

        # Indexer lives in vco_lib/diagram_indexer.py — owned by Phase
        # 1.5.A sibling. Import defensively: when the sibling hasn't
        # landed yet, the wrapper still functions; the file is on
        # disk, the indexer just hasn't picked it up yet.
        try:
            from vco_lib.diagram_indexer import index_diagram_async  # type: ignore[import-not-found]
        except ImportError:
            logger.debug(
                "mermaid_proxy: vco_lib.diagram_indexer not importable yet "
                "(Phase 1.5.A pending); skipping post-save indexer"
            )
            return

        try:
            await index_diagram_async(path_value)
        except Exception as e:  # noqa: BLE001 — indexer is best-effort
            logger.warning(
                "mermaid_proxy: index_diagram_async(%s) raised: %s",
                path_value, e,
            )


# ─── Upstream argv resolution ────────────────────────────────────────────


def _resolve_upstream_argv() -> list[str]:
    """Build the spawn argv for claude-mermaid at the pinned version.

    Reads ``bundled_mcp_versions.toml`` so a pin bump auto-propagates.

    v0.2.91 WP-D — npm-exec fallback. Previously this was a bare
    ``shutil.which("npx")`` and an immediate ``SystemExit(1)``, so a machine
    with npm but no npx (fnm/nvm installs where only ``node``/``npm`` were
    symlinked onto PATH — the exact shape observed in the field) could not run
    the Mermaid MCP at all, even though ``npm exec`` is an equivalent
    invocation. Resolution now goes through ``vco_lib.npx_resolver``, which
    carries the fnm/nvm ladder AND the ``npm exec --yes --`` fallback (same
    precedent as ``scripts/build-bundled-launcher.sh``). Both absent still
    exits 1 with a clear message.

    This is VCO-owned WRAPPER code: nothing here touches the REGISTERED entry
    shape in ``~/.claude.json``, so the third-party-preservation and
    stale-detection fingerprints (which match on a bare ``"command": "npx"``)
    are unaffected. Changing the registration is a separate, out-of-scope
    decision precisely because it would fork those fingerprints in two
    languages.
    """
    try:
        pins = load_bundled_versions()
        spec = pins["npm"]["mermaid_mcp"]
        package = spec["package"]
        version = spec["version"]
    except (KeyError, RuntimeError) as e:
        sys.stderr.write(
            f"[mermaid_proxy] ERROR: cannot resolve mermaid pin from "
            f"bundled_mcp_versions.toml: {e}\n"
        )
        raise SystemExit(1)

    argv = package_run_argv(package, version)
    if argv is None:
        sys.stderr.write(
            "[mermaid_proxy] ERROR: neither npx nor npm found on PATH. The "
            "Mermaid wrapper MCP needs Node.js to spawn the upstream "
            "`claude-mermaid` package. Install Node.js 18+ "
            "(https://nodejs.org) and reopen Claude Code.\n"
        )
        raise SystemExit(1)
    return argv


# ─── Entry point ─────────────────────────────────────────────────────────


def main() -> None:
    """``python -m claude_mcp_servers.wrappers.mermaid_proxy`` entry."""
    # Log to stderr so we don't interfere with the stdout JSON-RPC channel.
    configure_logging(
        format="[mermaid_proxy] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    proxy = MermaidProxy()
    asyncio.run(proxy.run())


if __name__ == "__main__":
    main()
