# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.75 P3 batch — small closes.

  * P3a (C-13): survivors' total_chunks patched on shrink (metadata, no re-embed).
  * P3b (C-9): embed-service in-flight counter + consistent shed on BOTH
    endpoints + honest 503 / /health naming.
  * P3c (R9): peer/single-chunk truncation note on multi-chunk entities.
  * P3d (CG-5): minified-content skip at walk time (skip + log, never delete).
  * P3f: _RL_OVERFETCH promoted to KG_OVERFETCH_MULTIPLIER env (default 2).
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


@pytest.fixture(scope="module")
def analyzer_mod():
    spec = importlib.util.spec_from_file_location("_acg_p3", str(_ANALYZER_PATH))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ─────────────────── P3d: minified-content heuristic ───────────────────


def test_p3d_minified_long_single_line_is_flagged(analyzer_mod):
    content = "var x=1;" + "a=1;" * 3000  # one huge line
    assert analyzer_mod._is_minified_content(content) is True


def test_p3d_high_median_line_length_is_flagged(analyzer_mod):
    # 60 lines each ~500 chars → median well above hand-written code.
    content = "\n".join("x" * 500 for _ in range(60))
    assert analyzer_mod._is_minified_content(content) is True


def test_p3d_normal_code_not_flagged(analyzer_mod):
    content = "\n".join([
        "def foo(a, b):",
        "    # a normal, readable line",
        "    return a + b",
    ] * 400)  # large but normal median line length
    assert analyzer_mod._is_minified_content(content) is False


def test_p3d_short_file_never_flagged(analyzer_mod):
    # Below the min-content threshold, even a dense line is kept.
    assert analyzer_mod._is_minified_content("x" * 100) is False


def test_p3d_empty_never_flagged(analyzer_mod):
    assert analyzer_mod._is_minified_content("") is False


# ─────────────────── P3a: survivor total_chunks patch ───────────────────


class _Obj:
    def __init__(self, uuid, props):
        self.uuid = uuid
        self.properties = props


class _Data:
    def __init__(self):
        self.updates = []

    def update(self, uuid, properties):
        self.updates.append({"uuid": uuid, "properties": dict(properties)})


class _Coll:
    def __init__(self, rows, prop_names):
        self.name = "P_CodeFunction"
        self._rows = rows
        self.data = _Data()
        self.config = type("C", (), {
            "get": lambda self=None: type("Cfg", (), {
                "properties": [type("P", (), {"name": n})() for n in prop_names],
            })()
        })()

    def iterator(self, return_properties=None):
        return iter(list(self._rows))


class _PatchStub:
    def __init__(self, analyzer_mod, coll, project="P"):
        self.modules_collection = None
        self.project_name = project
        self._c = coll
        cls = analyzer_mod.CodeGraphAnalyzer
        self._patch_survivor_total_chunks = (
            cls._patch_survivor_total_chunks.__get__(self, _PatchStub)
        )


def _rows_shrunk_3_to_2(src):
    # chunks 0,1 survive with a STALE total_chunks=3; chunk 2 already deleted.
    return [
        _Obj("c0", {"full_name": "m.f", "chunk_num": 0, "total_chunks": 3,
                    "project": "P", "project_source": src, "file_path": "a.py"}),
        _Obj("c1", {"full_name": "m.f", "chunk_num": 1, "total_chunks": 3,
                    "project": "P", "project_source": src, "file_path": "a.py"}),
    ]


def test_p3a_survivors_get_new_total_chunks(analyzer_mod):
    src = "/repo"
    coll = _Coll(
        _rows_shrunk_3_to_2(src),
        ("full_name", "chunk_num", "total_chunks", "project", "project_source", "file_path"),
    )
    stub = _PatchStub(analyzer_mod, coll)
    stub._patch_survivor_total_chunks(coll, "m.f", "a.py", src, 2)
    # Both survivors patched to total_chunks=2 (metadata only — no vector).
    assert len(coll.data.updates) == 2
    for u in coll.data.updates:
        assert u["properties"] == {"total_chunks": 2}, u


def test_p3a_idempotent_when_already_correct(analyzer_mod):
    """LEAVE-ALONE: survivors already carrying the correct total → no writes."""
    src = "/repo"
    rows = [
        _Obj("c0", {"full_name": "m.f", "chunk_num": 0, "total_chunks": 2,
                    "project": "P", "project_source": src, "file_path": "a.py"}),
        _Obj("c1", {"full_name": "m.f", "chunk_num": 1, "total_chunks": 2,
                    "project": "P", "project_source": src, "file_path": "a.py"}),
    ]
    coll = _Coll(rows, ("full_name", "chunk_num", "total_chunks", "project", "project_source", "file_path"))
    stub = _PatchStub(analyzer_mod, coll)
    stub._patch_survivor_total_chunks(coll, "m.f", "a.py", src, 2)
    assert coll.data.updates == []


def test_p3a_leaves_other_entities_and_sources_alone(analyzer_mod):
    """A different full_name / project_source is never patched (scoping)."""
    src = "/repo"
    rows = _rows_shrunk_3_to_2(src) + [
        _Obj("other", {"full_name": "m.OTHER", "chunk_num": 0, "total_chunks": 3,
                       "project": "P", "project_source": src, "file_path": "a.py"}),
        _Obj("xsrc", {"full_name": "m.f", "chunk_num": 0, "total_chunks": 3,
                      "project": "P", "project_source": "/other", "file_path": "a.py"}),
    ]
    coll = _Coll(rows, ("full_name", "chunk_num", "total_chunks", "project", "project_source", "file_path"))
    stub = _PatchStub(analyzer_mod, coll)
    stub._patch_survivor_total_chunks(coll, "m.f", "a.py", src, 2)
    patched = {u["uuid"] for u in coll.data.updates}
    assert patched == {"c0", "c1"}, patched


# ─────────────────── P3c: peer / single-chunk truncation note ───────────────────


def test_p3c_peer_row_multichunk_gets_truncation_note():
    import weaviate_mcp.server as srv
    props = {
        "full_name": "m.f", "file_path": "a.py",
        "function_body": "[chunk 1/3]\n\nbody", "chunk_num": 1, "total_chunks": 3,
    }
    # chunk_fetcher=None → peer/degrade path.
    out = srv._format_code_result_by_tier(
        props, "CodeFunction", "three_chunks", score=0.7, chunk_fetcher=None,
    )
    assert out["chunks_shown"] == 1
    assert "note" in out and "1 of 3" in out["note"]


def test_p3c_single_chunk_entity_no_note():
    import weaviate_mcp.server as srv
    props = {
        "full_name": "m.f", "file_path": "a.py",
        "function_body": "body", "chunk_num": 0, "total_chunks": 1,
    }
    out = srv._format_code_result_by_tier(
        props, "CodeFunction", "single_chunk", score=0.5, chunk_fetcher=None,
    )
    assert out["chunks_shown"] == 1
    assert "note" not in out


# ─────────────────── P3f: KG_OVERFETCH_MULTIPLIER env ───────────────────


def test_p3f_overfetch_env(monkeypatch):
    import weaviate_mcp.rl_state as st
    assert st._resolve_overfetch() == 2  # default
    monkeypatch.setenv("KG_OVERFETCH_MULTIPLIER", "5")
    assert st._resolve_overfetch() == 5
    monkeypatch.setenv("KG_OVERFETCH_MULTIPLIER", "notanint")
    assert st._resolve_overfetch() == 2  # bad → default
    monkeypatch.setenv("KG_OVERFETCH_MULTIPLIER", "0")
    assert st._resolve_overfetch() == 2  # <1 → default


# ─────────────────── P3b: embed-service shed parity ───────────────────


def test_p3b_should_shed_at_capacity():
    import claude_mcp_servers.code_embedding_service.server as es
    es._in_flight = 0
    assert es._should_shed() is False
    es._in_flight = es.MAX_CONCURRENT
    assert es._should_shed() is True
    es._in_flight = 0  # reset for other tests


def test_p3b_both_endpoints_shed_on_the_same_condition(monkeypatch):
    """Under a saturated in-flight count, BOTH /embed and /api/embeddings 503
    (pre-fix /api/embeddings never shed → divergence)."""
    import claude_mcp_servers.code_embedding_service.server as es
    from fastapi import HTTPException

    es._in_flight = es.MAX_CONCURRENT  # saturated

    async def _run():
        with pytest.raises(HTTPException) as e1:
            await es.embed_endpoint(es.EmbedRequest(texts=["x"]))
        assert e1.value.status_code == 503
        assert "capacity" in e1.value.detail.lower()

        with pytest.raises(HTTPException) as e2:
            await es.ollama_compat_endpoint(es.OllamaEmbedRequest(prompt="x"))
        assert e2.value.status_code == 503
        assert "capacity" in e2.value.detail.lower()

    try:
        asyncio.run(_run())
    finally:
        es._in_flight = 0


def test_p3b_health_reports_in_flight_not_private_value():
    import claude_mcp_servers.code_embedding_service.server as es

    async def _run():
        return await es.health()

    es._in_flight = 0
    payload = asyncio.run(_run())
    # Honest key name + real counter (no `queued`, no _value read).
    assert "in_flight" in payload
    assert "queued" not in payload
    assert payload["in_flight"] == 0
