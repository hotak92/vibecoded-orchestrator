# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Canonical keys for the KG summary sidecar ``knowledge/.node_formats.json``.

The sidecar is keyed by the node's project-relative path, and the ONE shape
of that key is canonical POSIX (``knowledge/concepts/foo.md``) — the same
shape the Weaviate ``file_path`` property carries on every OS
(``sync_knowledge_graph.py``, v0.2.92 WP-B1), which is what the retrieval
readers (``claude_mcp_servers/weaviate_mcp/kg_query_core.py``) look entries
up by. Before v0.2.103 the writer keyed entries with ``str(Path.relative_to
(...))``, so on Windows every key carried ``\\`` and every summary /
description lookup missed silently.

This module is the one home for the two operations every writer and reader
of those keys needs:

* :func:`sidecar_key` — the canonical key for a node path (the separator
  swap is :func:`vco_lib.paths.to_posix_rel`, the repo's ONE normaliser);
* :func:`canonicalize_keys` — re-key a loaded sidecar dict in place so
  legacy ``\\`` keys become canonical. Idempotent and lossless: when both
  forms of one key exist, :func:`_pick` keeps the entry that still holds a
  summary, then the one matching the node's current content hash, then the
  newer ``generated_at`` — a usable summary is never dropped for an
  unusable one.

Pure and dependency-free apart from ``vco_lib.paths``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Callable, Optional

from vco_lib.paths import to_posix_rel


def sidecar_key(node_path: "str | Path", base: "str | Path | None" = None) -> str:
    """The canonical sidecar key for *node_path*.

    With *base*, the key is *node_path* relative to *base* (a path outside
    *base* keeps its full form, as the writer always did); either way the
    separators are POSIX.
    """
    # A PurePath keeps its flavour (a PureWindowsPath stays Windows-shaped
    # on any host), so the Windows key shape is testable everywhere.
    p = node_path if isinstance(node_path, PurePath) else Path(node_path)
    if base is not None:
        base_p = base if isinstance(base, PurePath) else type(p)(base)
        try:
            p = p.relative_to(base_p)
        except ValueError:
            pass
    return to_posix_rel(p)


def _usable(entry: object) -> bool:
    return isinstance(entry, dict) and bool(
        entry.get("summary") or entry.get("description")
    )


def _stamp(entry: object) -> datetime:
    """``generated_at`` as an aware datetime; unparsable/missing → oldest."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    if not isinstance(entry, dict):
        return floor
    raw = entry.get("generated_at")
    if not isinstance(raw, str) or not raw:
        return floor
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return floor
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pick(
    candidates: "list[tuple[str, object]]",
    canonical: str,
    current: Optional[str],
) -> object:
    """The entry to keep among several keyed to the same canonical path.

    Order of preference: holds a summary/description > its ``content_hash``
    matches the node's current content (when known) > newer
    ``generated_at`` > the key already in canonical form.
    """

    def rank(item: "tuple[str, object]") -> tuple:
        key, entry = item
        hash_match = bool(
            current
            and isinstance(entry, dict)
            and entry.get("content_hash") == current
        )
        return (_usable(entry), hash_match, _stamp(entry), key == canonical)

    return max(candidates, key=rank)[1]


def canonicalize_keys(
    db: dict,
    *,
    current_hash: "Callable[[str], Optional[str]] | None" = None,
) -> int:
    """Re-key *db* in place to canonical POSIX keys. Returns how many keys
    were rewritten or merged away (``0`` = already canonical; idempotent).

    *current_hash*, when given, maps a canonical key to the node's current
    content hash (or ``None`` when unknown) — consulted only to break a
    collision between two entries that both hold a summary.
    """
    groups: dict[str, list[tuple[str, object]]] = {}
    for key, entry in db.items():
        groups.setdefault(to_posix_rel(key), []).append((key, entry))
    changed = sum(
        1 for canon, items in groups.items() for key, _ in items if key != canon
    )
    if not changed:
        return 0
    rebuilt: dict = {}
    for canon, items in groups.items():
        if len(items) == 1:
            rebuilt[canon] = items[0][1]
            continue
        current = current_hash(canon) if current_hash is not None else None
        rebuilt[canon] = _pick(items, canon, current)
    db.clear()
    db.update(rebuilt)
    return changed
