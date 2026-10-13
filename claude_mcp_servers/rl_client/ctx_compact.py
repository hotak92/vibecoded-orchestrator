# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Compact in-memory form of a staged citation ctx — one home (W6-MEM).

A citation ctx (the dict ``search_pipeline._populate_citation_cache`` stages
for the answer monitor) carries every candidate node's embedding vectors —
``emb``, ``n_emb``, up to five ``linked_embs`` — plus the query vector, and the
dual-log ``other_nodes`` / ``other_query_emb`` twins. As Python lists a single
1024-dim vector costs about 33 KB (a 24-byte float object plus an 8-byte list
slot per value), so one ctx is 5-9 MB, and the 256-entry cache could hold
about 2 GB (2026-10-13 leak investigation, finding 2).

``compact_ctx`` returns a copy whose float vectors are packed into
``array.array`` buffers: ``'f'`` (4 bytes per value) when every value survives
the float32 round trip unchanged — true for vectors read back from Weaviate,
which stores float32 — otherwise ``'d'`` (8 bytes per value, exact for any
Python float). ``expand_ctx`` turns every buffer back into the identical
``list[float]``. The round trip is LOSSLESS by construction: the caller that
reads the ctx gets exactly the values it would have got before, so citation
cosines, the citation event and the online-training payload are unchanged.

Only lists made entirely of ``float`` values (no ints, no bools) and at least
``_MIN_VECTOR_LEN`` long are packed; everything else — titles, links, scores,
metadata — is copied through untouched. ``expand_ctx`` is a no-op on a ctx that
was never compacted (plain lists pass through), so a caller can always expand.
"""

from __future__ import annotations

from array import array
from typing import Any

__all__ = ["compact_ctx", "expand_ctx"]

# Shorter float lists are not worth a buffer (array header ≈ 64-80 bytes).
_MIN_VECTOR_LEN = 8


def _pack(vec: list) -> "array | list":
    if len(vec) < _MIN_VECTOR_LEN or not all(type(v) is float for v in vec):
        return [_compact_value(v) for v in vec]
    packed = array("f", vec)
    if packed.tolist() == vec:
        return packed
    return array("d", vec)


def _compact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _compact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return _pack(value)
    return value


def compact_ctx(ctx: dict) -> dict:
    """Return a compacted COPY of ``ctx`` (the input is not modified)."""
    return _compact_value(ctx)


def _expand_value(value: Any) -> Any:
    if isinstance(value, array):
        return value.tolist()
    if isinstance(value, dict):
        return {k: _expand_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_value(v) for v in value]
    return value


def expand_ctx(ctx: "dict | None") -> "dict | None":
    """Return a plain-list COPY of a (possibly compacted) ctx; None stays None."""
    if ctx is None:
        return None
    return _expand_value(ctx)
