# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 M2 (code-summary sidecar CONSUMER) + M4 (n_callers render).

M2: the code render path consumes ``.claude/.code_formats.json`` — the code
analogue of the KG ``.node_formats.json`` sidecar. FROZEN v1 shape (metadata
plan §3 D1):

    key   = f"{file_path}::{full_name}"           (one entry per ENTITY)
    entry = {full_name, file_path, collection, one_liner, summary,
             generated_at, content_hash, backend,
             total_chunks?, chunk_summaries?}
    chunk_summaries = {str(chunk_num): "one sentence", ...}
                       (keys = stringified stored chunk_num, 0-indexed)

Consumer contract under test:
  * summary tier lookup order: sidecar ``summary`` → stored ``doc`` → body
    snippet.
  * ``one_liner`` attached on EVERY tier when present.
  * three_chunks/full assembly prepends the ▶ (shown) / · (unshown) chunk map.
  * Missing sidecar → byte-identical pre-M2 output (regression).
  * Peer-project row (file_path not in the per-project sidecar) → no crash,
    no fields.

M4: ``n_callers`` renders in the identity block on both formatters; absent /
NULL property (older rows) → field omitted, no crash.

These tests use a hand-written fixture sidecar (the shape is FROZEN; the
generator lands separately and must match it).
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest


def _server():
    return importlib.import_module("weaviate_mcp.server")


FIXTURE_FILE_PATH = "src/alpha/processing.py"
FIXTURE_FULL_NAME = "alpha.processing.process_batch"


def _fixture_sidecar() -> dict:
    """A hand-written sidecar in the FROZEN v1 entry shape (plan §3 D1)."""
    return {
        f"{FIXTURE_FILE_PATH}::{FIXTURE_FULL_NAME}": {
            "full_name": FIXTURE_FULL_NAME,
            "file_path": FIXTURE_FILE_PATH,
            "collection": "CodeFunction",
            "one_liner": "Validates and batches incoming records for the pipeline.",
            "summary": (
                "Splits incoming records into size-bounded batches, validates "
                "each against the schema, and yields them to the pipeline "
                "runner. Used by the ingestion entry point."
            ),
            "generated_at": "2026-07-03T00:00:00+00:00",
            "content_hash": "a" * 64,
            "backend": "cli",
            "total_chunks": 3,
            "chunk_summaries": {
                "0": "Argument validation and batch-size resolution.",
                "1": "The batching loop with schema validation per record.",
                "2": "Error aggregation and the final yield of partial batches.",
            },
        },
    }


@pytest.fixture()
def srv_with_sidecar(monkeypatch, tmp_path):
    """Server module wired to a tmp project root carrying the fixture sidecar."""
    srv = _server()
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / ".code_formats.json").write_text(
        json.dumps(_fixture_sidecar()), encoding="utf-8"
    )
    monkeypatch.setattr(srv, "KG_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_code_formats_cache", None)
    yield srv
    srv._code_formats_cache = None


@pytest.fixture()
def srv_no_sidecar(monkeypatch, tmp_path):
    """Server module wired to a tmp project root with NO sidecar file."""
    srv = _server()
    monkeypatch.setattr(srv, "KG_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_code_formats_cache", None)
    yield srv
    srv._code_formats_cache = None


def _props(**overrides) -> dict:
    p = {
        "full_name": FIXTURE_FULL_NAME,
        "file_path": FIXTURE_FILE_PATH,
        "signature": "process_batch(records, batch_size=100)",
        "doc": "Batch incoming records.",
        "function_body": "def process_batch(records, batch_size=100):\n    ...",
        "chunk_num": 1,
        "total_chunks": 3,
    }
    p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# _load_code_formats / _get_code_format
# ---------------------------------------------------------------------------


def test_loader_resolves_kg_base_dir_sidecar(srv_with_sidecar):
    db = srv_with_sidecar._load_code_formats()
    assert f"{FIXTURE_FILE_PATH}::{FIXTURE_FULL_NAME}" in db


def test_loader_missing_file_returns_empty(srv_no_sidecar):
    assert srv_no_sidecar._load_code_formats() == {}


def test_get_code_format_levels(srv_with_sidecar):
    get = srv_with_sidecar._get_code_format
    assert get(FIXTURE_FILE_PATH, FIXTURE_FULL_NAME, "one_liner").startswith("Validates")
    assert isinstance(get(FIXTURE_FILE_PATH, FIXTURE_FULL_NAME, "chunk_summaries"), dict)
    assert get(FIXTURE_FILE_PATH, FIXTURE_FULL_NAME, "missing_level") is None
    # Peer / unknown entity → None, no crash.
    assert get("peer/other.py", "peer.other.fn", "summary") is None
    assert get("", FIXTURE_FULL_NAME, "summary") is None


# ---------------------------------------------------------------------------
# M2 — summary tier lookup order
# ---------------------------------------------------------------------------


def test_summary_tier_prefers_sidecar_over_doc(srv_with_sidecar):
    out = srv_with_sidecar._format_code_result_by_tier(
        _props(), "CodeFunction", "summary", score=0.30,
    )
    assert out["summary"].startswith("Splits incoming records")
    assert out["one_liner"].startswith("Validates and batches")


def test_summary_tier_falls_back_to_doc_without_sidecar(srv_no_sidecar):
    out = srv_no_sidecar._format_code_result_by_tier(
        _props(), "CodeFunction", "summary", score=0.30,
    )
    assert out["summary"] == "Batch incoming records."
    assert "one_liner" not in out


def test_summary_tier_falls_back_to_body_without_doc(srv_no_sidecar):
    out = srv_no_sidecar._format_code_result_by_tier(
        _props(doc=""), "CodeFunction", "summary", score=0.30,
    )
    assert "def process_batch" in out["summary"]


def test_missing_sidecar_output_is_regression_identical(srv_no_sidecar):
    """No sidecar file → the rendered dict carries exactly the pre-M2 fields."""
    out = srv_no_sidecar._format_code_result_by_tier(
        _props(), "CodeFunction", "single_chunk", score=0.40,
    )
    assert "one_liner" not in out
    assert "n_callers" not in out
    assert out["function_body"].startswith("def process_batch")


# ---------------------------------------------------------------------------
# M2 — one_liner on every tier + chunk-map header
# ---------------------------------------------------------------------------


def test_one_liner_attached_on_single_chunk_tier(srv_with_sidecar):
    out = srv_with_sidecar._format_code_result_by_tier(
        _props(), "CodeFunction", "single_chunk", score=0.40,
    )
    assert out["one_liner"].startswith("Validates and batches")


def test_chunk_map_header_marks_shown_and_unshown(srv_with_sidecar):
    def _fetcher(full_name, hit_chunk, total, max_chunks, file_path=""):  # C-8: +file_path
        return [
            {"chunk_num": 0, "function_body": "part zero"},
            {"chunk_num": 1, "function_body": "part one"},
        ]

    out = srv_with_sidecar._format_code_result_by_tier(
        _props(), "CodeFunction", "three_chunks", score=0.55,
        chunk_fetcher=_fetcher,
    )
    body = out["function_body"]
    assert body.startswith("[Chunk map:")
    assert "▶ 0: Argument validation" in body
    assert "▶ 1: The batching loop" in body
    assert "· 2: Error aggregation" in body
    assert "part zero" in body and "part one" in body
    assert out["chunks_shown"] == 2


def test_chunk_map_header_on_degraded_fallback(srv_with_sidecar):
    """three_chunks with a failing fetcher still shows the map (hit marked)."""
    def _fetcher(full_name, hit_chunk, total, max_chunks, file_path=""):  # C-8: +file_path
        return []

    out = srv_with_sidecar._format_code_result_by_tier(
        _props(), "CodeFunction", "three_chunks", score=0.55,
        chunk_fetcher=_fetcher,
    )
    body = out["function_body"]
    assert body.startswith("[Chunk map:")
    assert "▶ 1: The batching loop" in body
    assert out["chunks_shown"] == 1


def test_no_chunk_map_without_sidecar(srv_no_sidecar):
    def _fetcher(full_name, hit_chunk, total, max_chunks, file_path=""):  # C-8: +file_path
        return [
            {"chunk_num": 0, "function_body": "part zero"},
            {"chunk_num": 1, "function_body": "part one"},
        ]

    out = srv_no_sidecar._format_code_result_by_tier(
        _props(), "CodeFunction", "three_chunks", score=0.55,
        chunk_fetcher=_fetcher,
    )
    assert not out["function_body"].startswith("[Chunk map:")


# ---------------------------------------------------------------------------
# M4 — n_callers identity render
# ---------------------------------------------------------------------------


def test_n_callers_rendered_when_present(srv_no_sidecar):
    out = srv_no_sidecar._format_code_result_by_tier(
        _props(n_callers=7), "CodeFunction", "summary", score=0.30,
    )
    assert out["n_callers"] == 7


def test_n_callers_absent_or_null_is_graceful(srv_no_sidecar):
    out = srv_no_sidecar._format_code_result_by_tier(
        _props(n_callers=None), "CodeFunction", "summary", score=0.30,
    )
    assert "n_callers" not in out


def test_by_rank_formatter_carries_one_liner_and_n_callers(srv_with_sidecar):
    out = srv_with_sidecar._format_code_result_by_rank(
        _props(n_callers=3), "CodeFunction", 0, detail="auto",
        score=0.80, distance=0.20,
    )
    assert out["n_callers"] == 3
    assert out["one_liner"].startswith("Validates and batches")


# ---------------------------------------------------------------------------
# CLI print side (field parity: the CLI prints what the formatter emits)
# ---------------------------------------------------------------------------


def _cli_mod():
    import importlib.util

    cli_src = PROJECT_ROOT / "templates" / "scripts" / "query_code_graph.py"
    spec = importlib.util.spec_from_file_location("_qcg_v0273", cli_src)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pytest.skip("query_code_graph.py hard-exits without weaviate deps")
    return mod


def test_cli_prints_one_liner_and_callers(capsys):
    mod = _cli_mod()
    rendered = {
        "collection": "CodeFunction",
        "signature": "process_batch(records)",
        "one_liner": "Validates and batches incoming records.",
        "n_callers": 7,
        "doc": "Batch incoming records.",
        "tier": "summary",
    }
    mod.CodeGraphQuery._print_body(rendered, indent="  ", hook_format=True)
    out = capsys.readouterr().out
    assert "One-liner: Validates and batches incoming records." in out
    assert "Callers: 7" in out


def test_cli_omits_absent_fields(capsys):
    mod = _cli_mod()
    rendered = {
        "collection": "CodeFunction",
        "signature": "process_batch(records)",
        "doc": "Batch incoming records.",
        "tier": "summary",
    }
    mod.CodeGraphQuery._print_body(rendered, indent="  ", hook_format=True)
    out = capsys.readouterr().out
    assert "One-liner:" not in out
    assert "Callers:" not in out
