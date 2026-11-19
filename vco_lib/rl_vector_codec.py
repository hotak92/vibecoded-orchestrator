# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Compact at-rest form of the vectors an ``rl_events`` row carries.

Why
---
A retrieval row used to store every vector as JSON decimal text: the query
vector, each candidate's ``emb`` and up to five ``linked_embs`` per candidate,
each value written like ``-0.0123, `` — about 8.5 bytes per value, so ≈151 KB
per retrieval row and a ``launcher.db`` that grew by more than a gigabyte a day
(audit A-014). The trainer keeps every vector as float16 anyway
(``rl_reranker/online_learning.py::VectorStore`` and the export's store), so
the decimal text carried precision nothing downstream keeps.

What
----
One vector is stored as the string ``"f16:" + base64(little-endian IEEE
754 half floats)``: 2 bytes per value before base64, ≈2.7 bytes after — about
a third of the decimal text, with no precision loss the trainer would see.
The writer (``rl_client/telemetry_writer.py``) encodes; every reader that
parses a stored ``payload_json`` decodes through :func:`decode_event_vectors`
right after ``json.loads`` (``rl_reranker/hub_event_loader.py`` — the trainer's
one loader, which also serves archived rows — and
``rl_reranker/training/export.py``), so the code past the parse sees float
lists exactly as before. Rows written before this form keep their lists; the
decoder passes a list through unchanged.

The fields are fixed (:data:`EVENT_VECTOR_FIELDS`, :data:`NODE_VECTOR_FIELDS`
and the list-of-vectors variants): a new vector field must be added here or it
stays decimal text.

Stdlib only — the reranker image copies this file beside the loader.
"""
from __future__ import annotations

import base64
import math
import struct
from typing import Any, Dict, List, Sequence

#: Prefix of an encoded vector. A string field without it is not a vector.
PREFIX = "f16:"

#: Event-level fields holding ONE vector.
EVENT_VECTOR_FIELDS = ("query_emb",)
#: Event-level fields holding a LIST of vectors.
EVENT_VECTOR_LIST_FIELDS = ("answer_chunk_embs",)
#: Per-node fields holding ONE vector.
NODE_VECTOR_FIELDS = ("emb",)
#: Per-node fields holding a LIST of vectors.
NODE_VECTOR_LIST_FIELDS = ("linked_embs",)

# The largest finite half float. A value beyond it would become inf; such a
# vector is not an embedding and is kept as decimal text instead.
_F16_MAX = 65504.0


def encode_vector(vec: Sequence[float]) -> "str | List[float]":
    """``vec`` as an ``f16:`` string; a vector that half floats cannot hold
    (non-finite, or a value past ±65504) is returned as a float list."""
    values = [float(v) for v in vec]
    if not all(math.isfinite(v) and abs(v) <= _F16_MAX for v in values):
        return values
    raw = struct.pack(f"<{len(values)}e", *values)
    return PREFIX + base64.b64encode(raw).decode("ascii")


def decode_vector(value: Any) -> "List[float] | Any":
    """The float list of a stored vector.

    A list (a row written before the compact form) passes through unchanged;
    an ``f16:`` string is decoded; anything else is returned as it is (the
    caller's own checks treat it as a missing vector). A string that carries
    the prefix but is not valid base64 of whole half floats raises
    ``ValueError`` — a damaged vector is never decoded into a wrong one.
    """
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    try:
        raw = base64.b64decode(value[len(PREFIX):], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"rl vector: not base64 ({exc})") from exc
    if len(raw) % 2:
        raise ValueError("rl vector: odd byte count for half floats")
    return list(struct.unpack(f"<{len(raw) // 2}e", raw))


def _map_vectors(event: Dict[str, Any], fn) -> Dict[str, Any]:
    for field in EVENT_VECTOR_FIELDS:
        if event.get(field) is not None:
            event[field] = fn(event[field])
    for field in EVENT_VECTOR_LIST_FIELDS:
        vecs = event.get(field)
        if isinstance(vecs, list):
            event[field] = [fn(v) for v in vecs]
    nodes = event.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for field in NODE_VECTOR_FIELDS:
                if node.get(field) is not None:
                    node[field] = fn(node[field])
            for field in NODE_VECTOR_LIST_FIELDS:
                vecs = node.get(field)
                if isinstance(vecs, list):
                    node[field] = [fn(v) for v in vecs]
    return event


def _encode_one(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and value and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
    ):
        return encode_vector(value)
    return value


def encode_event_vectors(event: Dict[str, Any]) -> Dict[str, Any]:
    """Encode every vector field of ``event`` IN PLACE; returns ``event``.
    Non-numeric or empty values are left as they are."""
    return _map_vectors(event, _encode_one)


def decode_event_vectors(event: Dict[str, Any]) -> Dict[str, Any]:
    """Decode every vector field of a parsed ``payload_json`` IN PLACE;
    returns ``event``. Raises ``ValueError`` on a damaged encoded vector."""
    return _map_vectors(event, decode_vector)


def sql_vector_present(doc: str, path: str) -> str:
    """An SQLite predicate: the JSON document ``doc`` (a column or an
    expression) holds a non-empty vector at ``path``, in either stored form.
    ``doc`` and ``path`` are trusted, code-supplied SQL fragments."""
    n = len(PREFIX)
    return (
        f"((json_type({doc}, '{path}') = 'array'"
        f" AND json_array_length({doc}, '{path}') > 0)"
        f" OR (json_type({doc}, '{path}') = 'text'"
        f" AND substr(json_extract({doc}, '{path}'), 1, {n}) = '{PREFIX}'"
        f" AND length(json_extract({doc}, '{path}')) > {n}))"
    )


__all__ = [
    "PREFIX",
    "decode_event_vectors",
    "decode_vector",
    "encode_event_vectors",
    "encode_vector",
    "sql_vector_present",
]
