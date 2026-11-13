# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Serve-time feature wire format for the RL reranker (``POST /cache_nodes``).

ONE home for what the client sends the reranker and how the server reads it
back. The client (``rl_client.client.RLClient.cache_nodes``) encodes; the
server (``rl_reranker.retrieval_rl.RetrievalRL.rerank``) decodes. The image
COPIES this file to ``/app/rl_client/rerank_wire.py``.

What the model sees at serve time is exactly what it was trained on: the
vectors the client already logs on the retrieval event (the training export
reads the same fields from ``rl_events``):

* the query vector = the event's ``query_emb`` (the active search vector, with
  the profile's query prefix already applied by the search path);
* the node vector = the node's ``emb``;
* the linked vectors = ``linked_embs`` (other chunks of the node, then linked
  nodes) with their type names from ``linked_type_names`` (``link_types`` is
  the older field name), selected by :func:`node_linked_fields` — the same
  rule the training export applies (``rl_reranker/training/export.py``
  ``export_project``; a parity test pins the two);
* the node type = ``node_type``.

Vectors travel as little-endian float16, base64-encoded, de-duplicated. The
training export stores vectors as float16 too, so the server scores the same
rounded values the trainer learned from.

Request field ``features`` (beside the slim ``nodes`` list, aligned with it)::

    {"format": "f16-b64-v1",
     "dim": D,
     "vectors": "<base64 of (M, D) float16, little-endian, row-major>",
     "query": <row of the query vector>,
     "nodes": [{"n": <row, -1 = no vector>, "t": <node_type>,
                "l": [<rows>], "lt": [<linked type names>]}, ...]}

Pure stdlib on the encode side (the MCP and the hook CLIs need no numpy for
it); the decode side (server) uses numpy, which the image always carries.
"""
from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass
from typing import Any, Optional, Sequence

#: Wire-contract version of ``/cache_nodes`` with serve-time features. The
#: client's ``PROTOCOL_VERSION`` and the server's ``/health``
#: ``protocol_version`` both read this constant. v1 (pre-v0.2.106) servers
#: embedded the query themselves through an injected function that was never
#: wired, so they could not score at all.
RERANK_PROTOCOL_VERSION = 2

FEATURE_FORMAT = "f16-b64-v1"

#: Linked-slot count of the model input. Must match
#: ``rl_reranker.rl_model.MAX_LINKED`` (parity test:
#: tests/rl_reranker/test_serve_time_scoring.py).
MAX_LINKED = 5


def node_linked_fields(node: dict) -> "tuple[list[list[float]], list[str]]":
    """The linked vectors of a logged node and their type names.

    Selection rule of the training export: the first :data:`MAX_LINKED`
    entries of ``linked_embs``; an empty entry is skipped (its type name too:
    names are taken by the entry's index in ``linked_embs``, from
    ``linked_type_names`` or else ``link_types``); a missing name is ``""``.
    Dimension checks are left to the caller (the export drops a mismatched
    vector, so does :func:`encode_rerank_features`).
    """
    names = node.get("linked_type_names") or node.get("link_types") or []
    vecs: list[list[float]] = []
    types: list[str] = []
    for li, lv in enumerate((node.get("linked_embs") or [])[:MAX_LINKED]):
        if not lv:
            continue
        vecs.append(lv)
        types.append(str(names[li]) if li < len(names) and names[li] else "")
    return vecs, types


class _VectorTable:
    """Collects vectors once each (identical vectors share a row)."""

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self._rows: dict[bytes, int] = {}
        self._chunks: list[bytes] = []

    def add(self, vec: Optional[Sequence[float]]) -> int:
        """Row of ``vec``; -1 when it is not a ``dim``-long numeric vector."""
        if vec is None or len(vec) != self.dim:
            return -1
        try:
            packed = struct.pack(f"<{self.dim}e", *vec)
        except (struct.error, TypeError, OverflowError):
            # Not numeric, or outside the float16 range (|x| > 65504) —
            # embedding vectors never are; refuse the vector, not the call.
            return -1
        row = self._rows.get(packed)
        if row is None:
            row = len(self._chunks)
            self._rows[packed] = row
            self._chunks.append(packed)
        return row

    def b64(self) -> str:
        return base64.b64encode(b"".join(self._chunks)).decode("ascii")

    def __len__(self) -> int:
        return len(self._chunks)


def encode_rerank_features(
    query_emb: Optional[Sequence[float]],
    nodes: Sequence[Any],
    *,
    dim: int,
    include_linked: bool = True,
) -> Optional[dict]:
    """Build the ``features`` request field, or ``None`` when nothing can be
    scored (no ``dim``-long query vector, or no node carries a vector).

    ``include_linked=False`` leaves the linked slots empty — used when the
    server reports that its profile masks them (``linked_slots_masked`` on
    ``/health``); the server masks them anyway, this only saves bytes.
    """
    table = _VectorTable(int(dim))
    q_row = table.add(query_emb) if query_emb is not None else -1
    if q_row < 0:
        return None
    entries: list[dict] = []
    scorable = 0
    for node in nodes:
        if not isinstance(node, dict):
            entries.append({"n": -1, "t": "", "l": [], "lt": []})
            continue
        emb = node.get("emb")
        n_row = table.add(emb) if emb else -1
        rows: list[int] = []
        ltypes: list[str] = []
        if n_row >= 0:
            scorable += 1
            if include_linked:
                vecs, names = node_linked_fields(node)
                for vec, name in zip(vecs, names):
                    r = table.add(vec)
                    if r >= 0:
                        rows.append(r)
                        ltypes.append(name)
        entries.append({
            "n": n_row,
            "t": str(node.get("node_type") or ""),
            "l": rows,
            "lt": ltypes,
        })
    if scorable == 0:
        return None
    return {
        "format": FEATURE_FORMAT,
        "dim": table.dim,
        "vectors": table.b64(),
        "query": q_row,
        "nodes": entries,
    }


@dataclass(frozen=True)
class NodeFeatures:
    """One candidate's decoded inputs (numpy float32 arrays)."""

    vec: Any                    # (D,) or None when the node carried no vector
    linked: list                # list of (D,) arrays, at most MAX_LINKED
    node_type: str
    linked_types: list          # type name per linked vector ("" = unknown)


@dataclass(frozen=True)
class DecodedFeatures:
    dim: int
    query: Any                  # (D,) float32
    nodes: list                 # list[NodeFeatures], aligned with the request nodes


def decode_rerank_features(features: Any) -> DecodedFeatures:
    """Parse a ``features`` field. Raises ``ValueError`` on any malformed part."""
    import numpy as np  # the server image always carries numpy

    if not isinstance(features, dict):
        raise ValueError("features must be an object")
    if features.get("format") != FEATURE_FORMAT:
        raise ValueError(f"unsupported features format {features.get('format')!r}")
    dim = features.get("dim")
    if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
        raise ValueError("features.dim must be a positive integer")
    try:
        raw = base64.b64decode(features.get("vectors") or "", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"features.vectors is not base64: {exc}") from exc
    if len(raw) % (2 * dim):
        raise ValueError("features.vectors length is not a multiple of dim")
    table = np.frombuffer(raw, dtype="<f2").reshape(-1, dim).astype(np.float32)
    m = table.shape[0]

    def row(i: Any) -> Any:
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < m:
            raise ValueError(f"vector row {i!r} out of range (0..{m - 1})")
        return table[i]

    query = row(features.get("query"))
    entries = features.get("nodes")
    if not isinstance(entries, list):
        raise ValueError("features.nodes must be a list")
    out: list[NodeFeatures] = []
    for e in entries:
        if not isinstance(e, dict):
            raise ValueError("features.nodes entries must be objects")
        n = e.get("n", -1)
        vec = None if n == -1 else row(n)
        rows = e.get("l") or []
        names = e.get("lt") or []
        if not isinstance(rows, list) or not isinstance(names, list) or len(names) != len(rows):
            raise ValueError("features.nodes[].l and .lt must be lists of equal length")
        if len(rows) > MAX_LINKED:
            raise ValueError(f"more than {MAX_LINKED} linked vectors")
        out.append(NodeFeatures(
            vec=vec,
            linked=[row(r) for r in rows],
            node_type=str(e.get("t") or ""),
            linked_types=[str(x or "") for x in names],
        ))
    return DecodedFeatures(dim=dim, query=query, nodes=out)


# ─── X-VCT-Project-ID: the project-id rule both sides apply ──────────────
# The container uses the header value as a filesystem path component
# (``/data/state/projects/<project_id>/``) and a JSONL filename suffix
# (``rl_events_<project_id>.jsonl``), so a value like ``../etc`` would put
# state outside ``/data/state/``. The client sends only an id that passes
# this rule (else no header: the container serves the base model), and the
# container refuses a request whose header fails it (400) — a request does
# not have to come from this client. Accepted: ASCII letters, digits, dash,
# underscore; 1..64 characters (a launcher UUID is 36, slugs <= 32).
# ``fullmatch``, never ``match`` + ``$``: ``$`` also matches before a
# trailing newline, so ``"abc\n"`` used to pass.
PROJECT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def sanitize_project_id(value: Optional[str]) -> Optional[str]:
    """``value`` when it is a safe-for-filesystem project identifier, else
    ``None`` (never raises).

    Examples:
        sanitize_project_id("02fbc934-ada5-433c-b606-d1f56194035a")
            -> "02fbc934-ada5-433c-b606-d1f56194035a"  (UUID v4 OK)
        sanitize_project_id("orchestrator-root") -> "orchestrator-root"
        sanitize_project_id("../etc/passwd") -> None  (path traversal blocked)
        sanitize_project_id("project id") -> None  (space blocked)
        sanitize_project_id("abc\n") -> None  (trailing newline blocked)
        sanitize_project_id(None) -> None; sanitize_project_id("") -> None
        sanitize_project_id("a" * 65) -> None  (length cap)
    """
    if not isinstance(value, str) or not value:
        return None
    if PROJECT_ID_RE.fullmatch(value) is None:
        return None
    return value
