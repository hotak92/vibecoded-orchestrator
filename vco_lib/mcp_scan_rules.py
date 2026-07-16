# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Loader for ``mcp_scan_rules.toml`` — the cross-language MCP scan/
registration rule table (v0.2.83 WP-B4).

This module is the Python side of a tier-(B) shared-config loader (see
CLAUDE.md "Share, don't mirror, cross-language logic"). The Rust side lives
at ``launcher/src-tauri/vct-launcher-core/src/mcp_scan_rules.rs``; both parse
the SAME ``vco_lib/mcp_scan_rules.toml`` with the SAME semantics so the
install.py path and the launcher path agree on the MCP rule DATA (env-key
allowlist, secret-shaped needles, the bundled/default-composed MCP name sets,
and the deprecated-default registry). A cross-language parity test
(``tests/test_mcp_scan_rules_parity.py``) keeps them in lockstep — the same
triangulation shape used for ``bundled_mcp_versions.toml`` /
``orchestrator-managed-paths.txt``.

The table is plain stdlib ``tomllib`` (Python 3.11+ — install.py already
requires 3.11 via :data:`install.MIN_PYTHON`). No third-party dependency.

Failure mode
------------
A missing or unreadable table is FATAL — raises ``RuntimeError`` with a
clear, recoverable message pointing at the file path and the upstream repo.
Silently falling back to a hard-coded default would re-introduce the exact
two-language drift this file was written to eliminate (WP-B4). vco_lib is
part of every healthy VCO install, so an unreadable table means a BROKEN
install, surfaced loudly — never a quiet inline-copy degrade.

Co-location
-----------
The .toml lives as a SIBLING of this loader under ``vco_lib/`` so it ships in
the Python wheel automatically (hatchling ships every file under the listed
``packages``), mirroring ``bundled_mcp_versions.toml``'s v0.2.34 move. The
Rust loader embeds it at COMPILE time via ``include_str!`` (4 levels up into
``vco_lib/``); the include path must follow any future move in lockstep.
"""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

#: The .toml sits next to this loader inside the ``vco_lib`` package.
#: ``.resolve()`` so symlinks / relative-CWD invocations still land on the
#: right file.
_DEFAULT_TABLE_PATH: Path = Path(__file__).resolve().parent / "mcp_scan_rules.toml"

#: The format version this loader knows how to read. A future schema
#: extension bumps the .toml version and this constant in the same commit;
#: the parity tests notice any mismatch.
_SUPPORTED_FORMAT_VERSION = 1


def table_path() -> Path:
    """Absolute path to the on-disk rule table. Exposed for diagnostics."""
    return _DEFAULT_TABLE_PATH


def load_mcp_scan_rules(path: Path | None = None) -> dict[str, Any]:
    """Parse ``mcp_scan_rules.toml`` and return the raw nested dict.

    Args:
        path: Optional override for the table path. Defaults to the
            ``vco_lib/`` sibling of this module. Tests pass a tempfile path;
            production code lets it default.

    Returns:
        Parsed TOML as a nested dict (structure mirrors the .toml). Unknown
        sections / extra keys are preserved (forward-compat).

    Raises:
        RuntimeError: table missing or unreadable, OR ``format_version``
            is not the version this loader supports. The message names the
            absolute path it tried and points at the upstream repo.
        tomllib.TOMLDecodeError: malformed TOML — propagated unwrapped so
            callers (and tests) get the exact stdlib parser error.
    """
    target = path if path is not None else _DEFAULT_TABLE_PATH
    try:
        raw_bytes = target.read_bytes()
    except OSError as e:
        raise RuntimeError(
            f"Could not read MCP scan-rules table at {target}: {e}. "
            f"This file is the cross-language source of truth for the "
            f"MCP env-key allowlist, secret-shaped needle set, bundled MCP "
            f"name sets, and deprecated-default registry used by install.py "
            f"and the launcher. It ships with every VCO install; a missing "
            f"copy means a BROKEN install. Re-fetch from "
            f"https://github.com/hotak92/vibecoded-orchestrator."
        ) from e

    parsed = tomllib.loads(raw_bytes.decode("utf-8"))

    version = parsed.get("format_version")
    if version != _SUPPORTED_FORMAT_VERSION:
        raise RuntimeError(
            f"MCP scan-rules table at {target} has format_version "
            f"{version!r}, but this loader supports "
            f"{_SUPPORTED_FORMAT_VERSION}. Coordinate the schema bump across "
            f"the Rust loader (mcp_scan_rules.rs) and the parity tests."
        )
    return parsed


@lru_cache(maxsize=1)
def _cached_rules() -> dict[str, Any]:
    """Load-once cache for the default-path table (the hot path). Tests that
    pass an explicit ``path`` bypass this via the public typed accessors'
    ``path=`` argument, which re-parse each call."""
    return load_mcp_scan_rules()


def _rules(path: Path | None) -> dict[str, Any]:
    return load_mcp_scan_rules(path) if path is not None else _cached_rules()


# ── Typed accessors — the callers use THESE, never the raw dict ────────────


def allowed_global_env_keys(path: Path | None = None) -> tuple[str, ...]:
    """Env keys that MAY be written into ~/.claude.json mcpServers.*.env.
    ORDER preserved from the table (equality-sensitive)."""
    return tuple(_rules(path)["env"]["allowed_global_keys"])


def secret_shaped_needles(path: Path | None = None) -> tuple[str, ...]:
    """Credential-shaped needle SEGMENTS. A key is secret-shaped when one
    of these appears as a whole ``[_-]``-delimited segment (plus the
    language-local ``KEY`` / ``*_KEY`` suffix rule)."""
    return tuple(_rules(path)["env"]["secret_shaped_needles"])


def default_mcp_entry_names(path: Path | None = None) -> tuple[str, ...]:
    """The MCP ids whose ~/.claude.json entries are composed by the entry
    builder (the registrar-owned / rewritable set). ORDER matches the
    builder's emit order (shape tests assert equality)."""
    return tuple(_rules(path)["entries"]["default_names"])


def bundled_mcp_names(path: Path | None = None) -> tuple[str, ...]:
    """Every orchestrator-shipped MCP name (superset of
    default_mcp_entry_names). Used for is_user_added classification + the
    uninstall scrub. NOTE (WP-B4): the Python uninstall-scrub CONSUMER
    (install.py) is not yet migrated onto this — see the .toml header."""
    return tuple(_rules(path)["bundled"]["all_names"])


def default_disabled_mcp_names(path: Path | None = None) -> tuple[str, ...]:
    """Bundled MCPs that ship default-disabled per project."""
    return tuple(_rules(path)["bundled"]["default_disabled"])


def deprecated_default_mcps(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Registry of MCPs dropped from the default install set, keyed by MCP
    name. Each value carries ``removed_in`` / ``reason`` / ``opt_in_manifest``
    (``opt_in_manifest`` defaults to "" when absent)."""
    raw = _rules(path).get("deprecated", {})
    out: dict[str, dict[str, str]] = {}
    for name, info in raw.items():
        out[name] = {
            "removed_in": info.get("removed_in", ""),
            "reason": info.get("reason", ""),
            "opt_in_manifest": info.get("opt_in_manifest", ""),
        }
    return out
