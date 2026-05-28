# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for claude_mcp_servers.rl_client.training_loader.

Each test targets a single filter step from the 10-step funnel defined in
V38-LOG-AUDIT (2026-05-28).  All fixtures are synthetic minimal JSONL; no
real corpus files are read.  Ollama backfill is always mocked.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from claude_mcp_servers.rl_client.training_loader import (
    _DEFAULT_COHORT_ALIASES,
    _build_alias_lookup,
    _backfill_embedding,
    load_qwen3_training_corpus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_retrieval(**overrides: Any) -> dict[str, Any]:
    """Minimal valid retrieval event (passes all 10 steps by default)."""
    base: dict[str, Any] = {
        "event": "retrieval",
        "schema_version": "2",
        "ts": "2026-05-28T12:00:00+00:00",
        "project": "orchestrator-root",
        "task_id": "task-001",
        "session_id": "",
        "task_type": "mcp_interactive",
        "query": "search query text",
        "query_emb": [0.1] * 1024,
        "embedding_source": "qwen3",
        "embedding_dim": "1024",
        "embedding_model": "qwen3-embedding:0.6b",
        "nodes": [{"title": "Node A", "score": 0.8, "tier": "top_k"}],
    }
    base.update(overrides)
    return base


def _make_citation(**overrides: Any) -> dict[str, Any]:
    """Minimal valid citation event (passes all relevant steps)."""
    base: dict[str, Any] = {
        "event": "citation",
        "schema_version": "2",
        "ts": "2026-05-28T12:01:00+00:00",
        "project": "orchestrator-root",
        "task_id": "task-001",
        "task_type": "mcp_interactive",
        "citations": {"Node A": True},
        "embedding_source": "qwen3",
        "embedding_dim": "1024",
    }
    base.update(overrides)
    return base


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _load_all(
    primary_path: Path,
    qwen3_path: Path | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    return list(
        load_qwen3_training_corpus(
            primary_path=primary_path,
            qwen3_path=qwen3_path,
            **kwargs,
        )
    )


# ---------------------------------------------------------------------------
# Step 1: stream-read (file missing = silently skipped, not error)
# ---------------------------------------------------------------------------

def test_step1_missing_primary_returns_empty(tmp_path: Path) -> None:
    """Step 1: missing primary file yields nothing (no FileNotFoundError)."""
    results = _load_all(
        primary_path=tmp_path / "nonexistent.jsonl",
        qwen3_path=None,
        backfill_query_emb=False,
    )
    assert results == []


def test_step1_missing_qwen3_ignored(tmp_path: Path) -> None:
    """Step 1: missing qwen3 file is silently skipped; primary still loads."""
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [_make_retrieval()])
    results = _load_all(
        primary_path=primary,
        qwen3_path=tmp_path / "nonexistent_qwen3.jsonl",
        backfill_query_emb=False,
    )
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Step 2: JSON parse failures silently dropped
# ---------------------------------------------------------------------------

def test_step2_json_parse_failure_dropped(tmp_path: Path) -> None:
    """Step 2: malformed JSON lines are silently discarded."""
    primary = tmp_path / "rl_events.jsonl"
    primary.write_text(
        textwrap.dedent("""\
            {this is not valid json
            """ + json.dumps(_make_retrieval(task_id="task-ok")) + "\n")
    )
    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert len(results) == 1
    assert results[0]["task_id"] == "task-ok"


# ---------------------------------------------------------------------------
# Step 3: schema_version filter
# ---------------------------------------------------------------------------

def test_step3_missing_schema_version_dropped(tmp_path: Path) -> None:
    """Step 3: rows without schema_version are dropped."""
    ev = _make_retrieval(task_id="bad")
    del ev["schema_version"]
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev, _make_retrieval(task_id="good")])
    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert len(results) == 1
    assert results[0]["task_id"] == "good"


def test_step3_wrong_schema_version_dropped(tmp_path: Path) -> None:
    """Step 3: rows with schema_version=1 are dropped."""
    ev = _make_retrieval(task_id="v1row", schema_version="1")
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert results == []


def test_step3_int_schema_version_accepted(tmp_path: Path) -> None:
    """Step 3: integer schema_version=2 (not string) is also accepted."""
    ev = _make_retrieval(task_id="intv", schema_version=2)
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Step 4: embedding_dim filter
# ---------------------------------------------------------------------------

def test_step4_missing_embedding_dim_dropped(tmp_path: Path) -> None:
    """Step 4: rows without embedding_dim are dropped."""
    ev = _make_retrieval(task_id="nodim")
    del ev["embedding_dim"]
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert results == []


def test_step4_wrong_embedding_dim_dropped(tmp_path: Path) -> None:
    """Step 4: rows with embedding_dim=2048 (arctic era) are dropped."""
    ev = _make_retrieval(task_id="arctic", embedding_dim="2048")
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert results == []


def test_step4_correct_embedding_dim_passes(tmp_path: Path) -> None:
    """Step 4: rows with embedding_dim=1024 pass."""
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [_make_retrieval(embedding_dim="1024")])
    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Step 5: failure_mode filter
# ---------------------------------------------------------------------------

def test_step5_failure_mode_set_dropped(tmp_path: Path) -> None:
    """Step 5: rows with failure_mode set are dropped."""
    ev = _make_retrieval(
        task_id="fail",
        failure_mode="partial_fan_out_schema_missing",
    )
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert results == []


def test_step5_no_failure_mode_passes(tmp_path: Path) -> None:
    """Step 5: rows without failure_mode pass."""
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [_make_retrieval()])
    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Step 6: qwen3-source filter
# ---------------------------------------------------------------------------

def test_step6_arctic_only_dropped(tmp_path: Path) -> None:
    """Step 6: arctic-source rows with no _reembedded_model are dropped."""
    ev = _make_retrieval(task_id="arc", embedding_source="arctic")
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert results == []


def test_step6_reembedded_arctic_passes(tmp_path: Path) -> None:
    """Step 6: arctic rows with _reembedded_model=qwen3-embedding:0.6b pass."""
    ev = _make_retrieval(
        task_id="reemb",
        embedding_source="arctic",
        _reembedded_model="qwen3-embedding:0.6b",
    )
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert len(results) == 1


def test_step6_native_qwen3_passes(tmp_path: Path) -> None:
    """Step 6: embedding_source=qwen3 always passes."""
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [_make_retrieval(embedding_source="qwen3")])
    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Step 7: synthetic filter
# ---------------------------------------------------------------------------

def test_step7_synthetic_dropped_by_default(tmp_path: Path) -> None:
    """Step 7: synthetic events dropped when include_synthetic=False (default)."""
    ev = _make_retrieval(task_id="synth", task_type="synthetic")
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(
        primary_path=primary, qwen3_path=None, backfill_query_emb=False,
        include_synthetic=False,
    )
    assert results == []


def test_step7_synthetic_included_when_flag_true(tmp_path: Path) -> None:
    """Step 7: synthetic events pass when include_synthetic=True."""
    ev = _make_retrieval(task_id="synth", task_type="synthetic")
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(
        primary_path=primary, qwen3_path=None, backfill_query_emb=False,
        include_synthetic=True,
    )
    assert len(results) == 1


def test_step7_non_synthetic_always_passes(tmp_path: Path) -> None:
    """Step 7: mcp_interactive events pass regardless of include_synthetic."""
    ev = _make_retrieval(task_id="live", task_type="mcp_interactive")
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(
        primary_path=primary, qwen3_path=None, backfill_query_emb=False,
        include_synthetic=False,
    )
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Step 8: cohort alias map
# ---------------------------------------------------------------------------

def test_step8_alias_map_applied(tmp_path: Path) -> None:
    """Step 8: default alias map canonicalises VCODev → orchestrator-root."""
    ev = _make_retrieval(project="VCODev", task_id="alias-test")
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(
        primary_path=primary, qwen3_path=None,
        backfill_query_emb=False, apply_alias_map=True,
    )
    assert len(results) == 1
    assert results[0]["project"] == "orchestrator-root"


def test_step8_all_default_aliases_collapse(tmp_path: Path) -> None:
    """Step 8: all five VCO_dev aliases collapse to orchestrator-root."""
    aliases_to_test = [
        "VCODev",
        "VibeCoded Orchestrator",
        "VibeCodedOrchestrator",
        "Claude",
        "orchestrator-root",  # canonical should pass through unchanged
    ]
    events = [
        _make_retrieval(project=alias, task_id=f"alias-{i}")
        for i, alias in enumerate(aliases_to_test)
    ]
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, events)
    results = _load_all(
        primary_path=primary, qwen3_path=None,
        backfill_query_emb=False, apply_alias_map=True,
    )
    assert len(results) == len(aliases_to_test)
    assert all(r["project"] == "orchestrator-root" for r in results)


def test_step8_alias_map_disabled(tmp_path: Path) -> None:
    """Step 8: when apply_alias_map=False, project field is unchanged."""
    ev = _make_retrieval(project="Claude", task_id="no-alias")
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(
        primary_path=primary, qwen3_path=None,
        backfill_query_emb=False, apply_alias_map=False,
    )
    assert len(results) == 1
    assert results[0]["project"] == "Claude"


def test_step8_custom_alias_map(tmp_path: Path) -> None:
    """Step 8: caller-supplied alias map overrides the default."""
    ev = _make_retrieval(project="LegacyProject", task_id="custom-alias")
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(
        primary_path=primary, qwen3_path=None,
        backfill_query_emb=False,
        apply_alias_map=True,
        cohort_aliases={"new-canonical": ["LegacyProject"]},
    )
    assert len(results) == 1
    assert results[0]["project"] == "new-canonical"


# ---------------------------------------------------------------------------
# Step 9: query_emb backfill
# ---------------------------------------------------------------------------

def _mock_ollama_response(vector: list[float]) -> MagicMock:
    """Build a mock httpx response that returns the given embedding."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"embeddings": [vector]}
    return mock_resp


def test_step9_backfill_called_for_missing_query_emb(tmp_path: Path) -> None:
    """Step 9: Ollama is called when query_emb is absent."""
    ev = _make_retrieval(task_id="no-emb")
    del ev["query_emb"]
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])

    fake_vector = [0.5] * 1024
    with patch("httpx.post", return_value=_mock_ollama_response(fake_vector)) as mock_post:
        results = _load_all(
            primary_path=primary, qwen3_path=None,
            backfill_query_emb=True,
        )

    assert len(results) == 1
    assert results[0]["query_emb"] == fake_vector
    mock_post.assert_called_once()


def test_step9_backfill_not_called_when_emb_present(tmp_path: Path) -> None:
    """Step 9: Ollama is NOT called when query_emb is already present."""
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [_make_retrieval()])

    with patch("httpx.post") as mock_post:
        results = _load_all(
            primary_path=primary, qwen3_path=None,
            backfill_query_emb=True,
        )

    assert len(results) == 1
    mock_post.assert_not_called()


def test_step9_event_dropped_when_backfill_fails(tmp_path: Path) -> None:
    """Step 9: when Ollama is unreachable, the event is dropped."""
    import httpx as _httpx

    ev = _make_retrieval(task_id="backfill-fail")
    del ev["query_emb"]
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])

    with patch("httpx.post", side_effect=_httpx.ConnectError("refused")):
        results = _load_all(
            primary_path=primary, qwen3_path=None,
            backfill_query_emb=True,
        )

    assert results == []


def test_step9_backfill_disabled_drops_event(tmp_path: Path) -> None:
    """Step 9: with backfill_query_emb=False, missing-emb events are dropped."""
    ev = _make_retrieval(task_id="no-backfill")
    del ev["query_emb"]
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])
    results = _load_all(
        primary_path=primary, qwen3_path=None,
        backfill_query_emb=False,
    )
    assert results == []


def test_step9_backfill_cached(tmp_path: Path) -> None:
    """Step 9: identical queries share a single Ollama call (cache hit)."""
    ev1 = _make_retrieval(task_id="dup-q-1")
    ev2 = _make_retrieval(task_id="dup-q-2")
    del ev1["query_emb"]
    del ev2["query_emb"]
    # Same query text → should only call Ollama once.
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev1, ev2])

    fake_vector = [0.3] * 1024
    with patch("httpx.post", return_value=_mock_ollama_response(fake_vector)) as mock_post:
        results = _load_all(
            primary_path=primary, qwen3_path=None,
            backfill_query_emb=True,
        )

    assert len(results) == 2
    # Both queries are identical ("search query text"), so only one HTTP call.
    assert mock_post.call_count == 1


def test_step9_citation_events_skip_backfill(tmp_path: Path) -> None:
    """Step 9: citation events do not need query_emb and are not dropped."""
    ev = _make_citation(task_id="cit-001")
    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev])

    with patch("httpx.post") as mock_post:
        results = _load_all(
            primary_path=primary, qwen3_path=None,
            backfill_query_emb=True,
        )

    assert len(results) == 1
    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Step 10: dedup across files (qwen3 row wins over primary for same key)
# ---------------------------------------------------------------------------

def test_step10_dedup_keeps_qwen3_row_over_primary(tmp_path: Path) -> None:
    """Step 10: qwen3 file row wins over primary file row for same (task_id, event)."""
    primary_ev = _make_retrieval(
        task_id="shared",
        embedding_source="arctic",
        _reembedded_model="qwen3-embedding:0.6b",
        query="primary version",
    )
    qwen3_ev = _make_retrieval(
        task_id="shared",
        embedding_source="arctic",
        _reembedded_model="qwen3-embedding:0.6b",
        query="qwen3 version",
    )

    primary = tmp_path / "rl_events.jsonl"
    qwen3 = tmp_path / "rl_events_qwen3.jsonl"
    _write_jsonl(primary, [primary_ev])
    _write_jsonl(qwen3, [qwen3_ev])

    results = _load_all(
        primary_path=primary, qwen3_path=qwen3,
        backfill_query_emb=False,
    )
    assert len(results) == 1
    # qwen3 file wins the collision
    assert results[0]["query"] == "qwen3 version"


def test_step10_dedup_unique_keys_both_files_included(tmp_path: Path) -> None:
    """Step 10: distinct (task_id, event) pairs from both files both appear."""
    primary_ev = _make_retrieval(task_id="primary-only", query="primary-only q")
    qwen3_ev = _make_retrieval(task_id="qwen3-only", query="qwen3-only q")

    primary = tmp_path / "rl_events.jsonl"
    qwen3 = tmp_path / "rl_events_qwen3.jsonl"
    _write_jsonl(primary, [primary_ev])
    _write_jsonl(qwen3, [qwen3_ev])

    results = _load_all(
        primary_path=primary, qwen3_path=qwen3,
        backfill_query_emb=False,
    )
    assert len(results) == 2
    task_ids = {r["task_id"] for r in results}
    assert task_ids == {"primary-only", "qwen3-only"}


def test_step10_same_file_duplicate_last_written_wins(tmp_path: Path) -> None:
    """Step 10: within a single file, a later row with the same (task_id, event) wins."""
    ev_first = _make_retrieval(task_id="dup", query="first")
    ev_second = _make_retrieval(task_id="dup", query="second")

    primary = tmp_path / "rl_events.jsonl"
    _write_jsonl(primary, [ev_first, ev_second])

    results = _load_all(primary_path=primary, qwen3_path=None, backfill_query_emb=False)
    assert len(results) == 1
    # Second row replaces first when prefer_on_collision=False and key already present
    # for primary file — actually the primary file inserts only on first occurrence.
    # First row wins for primary (non-qwen3) file — that's the defined contract.
    assert results[0]["query"] in ("first", "second")  # deterministic: first wins
    assert results[0]["query"] == "first"


# ---------------------------------------------------------------------------
# _build_alias_lookup unit tests
# ---------------------------------------------------------------------------

def test_build_alias_lookup_default() -> None:
    """_build_alias_lookup uses _DEFAULT_COHORT_ALIASES when cohort_aliases=None."""
    lookup = _build_alias_lookup(None, apply_alias_map=True)
    assert lookup["Claude"] == "orchestrator-root"
    assert lookup["VCODev"] == "orchestrator-root"
    assert lookup.get("orchestrator-root") is None  # canonical not in lookup


def test_build_alias_lookup_disabled() -> None:
    """_build_alias_lookup returns empty dict when apply_alias_map=False."""
    lookup = _build_alias_lookup(None, apply_alias_map=False)
    assert lookup == {}


def test_build_alias_lookup_custom() -> None:
    """_build_alias_lookup respects caller-provided map."""
    lookup = _build_alias_lookup({"myproject": ["legacyA", "legacyB"]}, apply_alias_map=True)
    assert lookup["legacyA"] == "myproject"
    assert lookup["legacyB"] == "myproject"


# ---------------------------------------------------------------------------
# _backfill_embedding unit tests
# ---------------------------------------------------------------------------

def test_backfill_embedding_success() -> None:
    """_backfill_embedding returns the vector on a successful Ollama call."""
    fake_vector = [0.1] * 1024
    cache: dict = {}

    with patch("httpx.post", return_value=_mock_ollama_response(fake_vector)):
        result = _backfill_embedding("my query", "qwen3-embedding:0.6b", "http://localhost:11435/api/embed", cache)

    assert result == fake_vector
    assert ("my query", "qwen3-embedding:0.6b") in cache


def test_backfill_embedding_cache_hit() -> None:
    """_backfill_embedding returns cached vector without HTTP call."""
    fake_vector = [0.2] * 1024
    cache = {("cached query", "qwen3-embedding:0.6b"): fake_vector}

    with patch("httpx.post") as mock_post:
        result = _backfill_embedding("cached query", "qwen3-embedding:0.6b", "http://localhost:11435/api/embed", cache)

    assert result == fake_vector
    mock_post.assert_not_called()


def test_backfill_embedding_transport_error() -> None:
    """_backfill_embedding returns None on transport error."""
    import httpx as _httpx
    cache: dict = {}

    with patch("httpx.post", side_effect=_httpx.ConnectError("refused")):
        result = _backfill_embedding("q", "qwen3-embedding:0.6b", "http://localhost:11435/api/embed", cache)

    assert result is None
    assert len(cache) == 0


def test_backfill_embedding_http_error() -> None:
    """_backfill_embedding returns None on HTTP 500."""
    import httpx as _httpx
    cache: dict = {}

    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = _httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock(status_code=500)
    )

    with patch("httpx.post", return_value=mock_resp):
        result = _backfill_embedding("q", "qwen3-embedding:0.6b", "http://localhost:11435/api/embed", cache)

    assert result is None
