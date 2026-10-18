# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A project's Claude Code settings ``env`` — as Claude Code itself merges it.

v0.2.104 L5. The projected env lives in TWO files of one settings layer:

* ``.claude/settings.json`` ``env`` — committable; the portable keys
  (collection names, project name, gates, the embedding profile);
* ``.claude/settings.local.json`` ``env`` — per-machine, kept out of git; the
  keys whose value describes this machine (absolute paths, launcher-DB ids,
  service URLs and ports — :data:`vco_lib.config_projection.MACHINE_LOCAL_ENV_KEYS`).

Claude Code merges the two KEY BY KEY, ``settings.local.json`` winning, for the
session, its hooks and its MCP subprocesses (verified on Claude Code 2.1.285).
A VCO process that reads a project's env from disk instead of from its own
environment must apply the same merge, or it sees half the env: this module is
the ONE reader that does. Both files are read as JSONC (the ONE reader,
:mod:`vco_lib.jsonc_edit`). Soft-fail: a missing or unreadable file, or a
non-object ``env``, contributes nothing; never raises.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["SETTINGS_ENV_FILES", "read_settings_env", "settings_env_sources"]

#: Lowest precedence first — the order Claude Code applies them in.
SETTINGS_ENV_FILES: tuple[str, ...] = (
    ".claude/settings.json",
    ".claude/settings.local.json",
)


def _env_block(path: Path) -> dict[str, Any]:
    from vco_lib.jsonc_edit import load_object

    try:
        loaded = load_object(path)
    except Exception:  # noqa: BLE001 — a settings read is best-effort
        return {}
    env = loaded[0].get("env") if loaded is not None else None
    return dict(env) if isinstance(env, dict) else {}


def read_settings_env(folder: Path) -> dict[str, Any]:
    """The merged ``env`` of ``<folder>/.claude/settings.json`` and
    ``<folder>/.claude/settings.local.json`` — local wins per key, exactly
    as Claude Code merges them. ``{}`` when neither carries one."""
    merged: dict[str, Any] = {}
    for rel in SETTINGS_ENV_FILES:
        merged.update(_env_block(Path(folder) / rel))
    return merged


def settings_env_sources(folder: Path) -> dict[str, str]:
    """``{key: rel}`` — the file each merged key's value comes from (the
    later file wins, as in :func:`read_settings_env`). For diagnostics that
    name where a value was read."""
    sources: dict[str, str] = {}
    for rel in SETTINGS_ENV_FILES:
        for key in _env_block(Path(folder) / rel):
            sources[key] = rel
    return sources
