# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-R item 2 — chunk counts are ints, whichever client read them.

``chunk_num`` / ``total_chunks`` are Weaviate ``number`` properties. Both the
weaviate-client path and the kg_http adapter return a ``number`` as
``float``, and ``_format_obj`` passed them through, so a multi-chunk node
rendered as ``FULL NODE (2/2.0 chunks)``. ``_format_obj`` (and the sibling
chunk fetch) now normalise a whole-number float to ``int``.

Both shapes are driven here: a weaviate-client-shaped object carrying floats,
and the REAL kg_http ``HttpCollection`` over a fake transport (its own
``number`` conversion produces the floats).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import claude_mcp_servers.weaviate_mcp.kg_http as kg_http  # noqa: E402
import claude_mcp_servers.weaviate_mcp.kg_query_core as core  # noqa: E402

assert Path(core.__file__).resolve() == (MCP_DIR / "weaviate_mcp" / "kg_query_core.py").resolve()

TITLE = "Two Chunk Node"
FP = "knowledge/concepts/two-chunk-node.md"
ROWS = [
    {"title": TITLE, "file_path": FP, "content": "first half", "chunk_num": 1, "total_chunks": 2,
     "node_type": "concept"},
    {"title": TITLE, "file_path": FP, "content": "second half", "chunk_num": 2, "total_chunks": 2,
     "node_type": "concept"},
]


# ── weaviate-client shape: the v4 client hands back floats ──────────────────
class _ClientObj:
    def __init__(self, row: dict) -> None:
        self.uuid = f"u-{row['chunk_num']}"
        self.properties = {**row, "chunk_num": float(row["chunk_num"]),
                           "total_chunks": float(row["total_chunks"])}
        self.metadata = types.SimpleNamespace(distance=0.1)
        self.vector = {}


class _ClientColl:
    name = "KG"

    def __init__(self) -> None:
        self.query = self

    def fetch_objects(self, *, limit=None, offset=None, filters=None, **_kw):
        objs = [_ClientObj(r) for r in ROWS][offset or 0:]
        return types.SimpleNamespace(objects=objs[: limit or None])


# ── kg_http shape: the real adapter over a fake transport ──────────────────
class _Transport:
    SCHEMA = {"class": "KG", "properties": [
        {"name": "title", "dataType": ["text"]}, {"name": "file_path", "dataType": ["text"]},
        {"name": "content", "dataType": ["text"]}, {"name": "node_type", "dataType": ["text"]},
        {"name": "chunk_num", "dataType": ["number"]}, {"name": "total_chunks", "dataType": ["number"]},
    ]}

    def _get_json(self, path: str) -> dict:
        return self.SCHEMA

    def _post_json(self, path: str, body: dict) -> dict:
        # Weaviate's JSON carries whole numbers as ints; the adapter's
        # ``number`` conversion turns them into floats.
        rows = [{**r, "_additional": {"id": f"0000000{r['chunk_num']}-0000-0000-0000-000000000000",
                                      "distance": 0.1}} for r in ROWS]
        return {"data": {"Get": {"KG": rows}}}


def _http_coll():
    return kg_http.HttpCollection(_Transport(), "KG")


SHAPES = {"weaviate-client": _ClientColl, "kg_http": _http_coll}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_format_obj_chunk_counts_are_ints(shape: str) -> None:
    coll = SHAPES[shape]()
    objs = coll.query.fetch_objects(limit=10).objects
    # Precondition: this client really returns floats.
    assert isinstance(objs[0].properties["total_chunks"], float)
    out = core._format_obj(objs[1], "KG")
    assert (out["chunk_number"], out["total_chunks"]) == (2, 2)
    assert type(out["chunk_number"]) is int and type(out["total_chunks"]) is int


@pytest.mark.parametrize("shape", list(SHAPES))
def test_full_node_renders_whole_chunk_counts(shape: str) -> None:
    coll = SHAPES[shape]()
    hit = core._format_obj(coll.query.fetch_objects(limit=10).objects[0], "KG")
    entry = core._format_result_by_tier(hit, "full", sidecar_db={}, coll=coll)
    assert entry is not None
    shown, total = entry["chunks_shown"], entry["chunks_total"]
    # The exact header rl_kg_search prints for a multi-chunk full-tier hit.
    assert f"FULL NODE ({shown}/{total} chunks)" == "FULL NODE (2/2 chunks)"
    assert "first half" in entry["content"] and "second half" in entry["content"]


def test_server_bound_format_obj_normalises_too() -> None:
    # server.py REBINDS _format_obj into its own globals (kg_query_core.bind_into),
    # so the helper must resolve there as well — the MCP path, not just the hook's.
    import claude_mcp_servers.weaviate_mcp.server as srv

    out = srv._format_obj(_ClientObj(ROWS[1]), "KG")
    assert (out["chunk_number"], out["total_chunks"]) == (2, 2)
    assert type(out["total_chunks"]) is int


def test_whole_number_leaves_other_values_alone() -> None:
    assert core._whole_number(2.0) == 2 and type(core._whole_number(2.0)) is int
    assert core._whole_number(2.5) == 2.5
    assert core._whole_number(None) is None
    assert core._whole_number(0.0) == 0
    assert core._whole_number(3) == 3
