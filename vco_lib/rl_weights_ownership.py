# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Python reader of ``vco_lib/rl_weights_ownership.toml``.

Who owns a local RL weights file: the orchestrator ("managed": a symlink to a
download, or a copy with the managed marker beside it) or the user ("user":
any other regular file). The table is the one home; the launcher's Rust side
(``module_default_weights.rs``) parses the same file. Standard library only,
so the RL reranker image can carry it (its Dockerfile COPYs both files).
"""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

Owner = Literal["managed", "user"]

TABLE_NAME = "rl_weights_ownership.toml"
SUPPORTED_FORMAT_VERSION = 1
_CLASSES = ("symlink", "file_with_marker", "file_without_marker")


def table_path() -> Path:
    return Path(__file__).with_name(TABLE_NAME)


def parse_table(text: str) -> dict:
    """Parse and validate the table text; raises ``ValueError`` when invalid."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{TABLE_NAME}: {exc}") from exc
    if doc.get("format_version") != SUPPORTED_FORMAT_VERSION:
        raise ValueError(f"{TABLE_NAME}: unsupported format_version {doc.get('format_version')!r}")
    suffix = doc.get("managed_marker_suffix")
    if not isinstance(suffix, str) or not suffix.startswith(".") or "/" in suffix or "\\" in suffix:
        raise ValueError(f"{TABLE_NAME}: managed_marker_suffix must be a '.'-prefixed file suffix")
    own = doc.get("ownership")
    if not isinstance(own, dict) or any(own.get(c) not in ("managed", "user") for c in _CLASSES):
        raise ValueError(f"{TABLE_NAME}: [ownership] must map {_CLASSES} to 'managed' or 'user'")
    return doc


@lru_cache(maxsize=1)
def _table() -> dict:
    return parse_table(table_path().read_text(encoding="utf-8"))


def managed_marker_path(weights_path: Path) -> Path:
    """``<weights_path><suffix>`` — the marker beside a launcher-placed copy."""
    p = Path(weights_path)
    return p.with_name(p.name + _table()["managed_marker_suffix"])


def classify(weights_path: Path) -> Optional[Owner]:
    """``"managed"`` / ``"user"`` for a file at ``weights_path``; ``None`` if absent."""
    p = Path(weights_path)
    own = _table()["ownership"]
    if p.is_symlink():
        return own["symlink"]
    if not p.is_file():
        return None
    if managed_marker_path(p).exists():
        return own["file_with_marker"]
    return own["file_without_marker"]
