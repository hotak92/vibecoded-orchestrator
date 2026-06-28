# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.47 RL-7: tests for the MCP-side citation-write path.

Two functions under test:

1. ``_rl_is_literal_cited(node, answer_lower)`` — pure-function regex/wikilink
   check. Pins the rule for "node identity appears in the answer".

2. ``_rl_compute_and_write_citations(task_id, answer, ctx)`` — the async
   helper called from ``_rl_answer_monitor`` after answer completion.
   Chunks + embeds the answer, computes per-node cosine_sims and
   literal_cited, runs the unified-target formula, and writes the
   citation event via the centralized ``RLTelemetryWriter``.

For (2) we mock ``Chunker.for_model``, ``_get_embedding_service``, and
``_get_rl_telemetry_writer`` so the test exercises the orchestration
logic without standing up real Ollama / Weaviate / launcher-hub stacks.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_mcp_servers.weaviate_mcp.server import (
    _RL_LITERAL_CITED_MIN_TITLE_LEN,
    _RL_MIN_ANSWER_CHARS_FOR_CITATION,
    _RL_MIN_ANSWER_TOKENS_FOR_CITATION,
    _rl_compute_and_write_citations,
    _rl_is_literal_cited,
)


# ----------------------------------------------------------------------
# 1. _rl_is_literal_cited — pure regex check
# ----------------------------------------------------------------------


class TestLiteralCitedRegex:
    def test_title_word_boundary_match(self) -> None:
        node = {"title": "RetrievalRL", "file_path": "knowledge/concepts/retrieval-rl.md"}
        assert _rl_is_literal_cited(node, "we use the retrievalrl module here")

    def test_no_match_returns_false(self) -> None:
        node = {"title": "SomeNode", "file_path": "knowledge/foo.md"}
        assert not _rl_is_literal_cited(node, "completely unrelated text")

    def test_substring_not_word_boundary_does_not_match(self) -> None:
        # Pre-fix bug: "RL" matched "curl" / "url". Word-boundary regex
        # plus the min-title-len guard rule it out.
        node = {"title": "RL", "file_path": "knowledge/rl.md"}
        # "RL" is below the min-title-len threshold (3), so the title
        # form is skipped — but `file_path` and slug `rl` are also < 3,
        # so they're skipped too. No match.
        assert not _rl_is_literal_cited(node, "we ran curl against url")

    def test_short_title_skipped_via_min_len_guard(self) -> None:
        node = {"title": "AI", "file_path": ""}
        assert not _rl_is_literal_cited(node, "we used AI for this")

    def test_long_title_three_chars_just_passes(self) -> None:
        # 3 chars is the minimum — at the boundary, it MUST match.
        assert _RL_LITERAL_CITED_MIN_TITLE_LEN == 3
        node = {"title": "API", "file_path": ""}
        assert _rl_is_literal_cited(node, "we hit the api endpoint")

    def test_wikilink_double_bracket_form_matches(self) -> None:
        node = {"title": "Compute Unified Targets", "file_path": ""}
        assert _rl_is_literal_cited(node, "see [[compute unified targets]] for details")

    def test_file_path_slug_matches(self) -> None:
        # The .md stem (slug) is used as a secondary identity form.
        node = {"title": "Some Long Title", "file_path": "knowledge/concepts/foo.md"}
        assert _rl_is_literal_cited(node, "described in the foo concept node")

    def test_file_path_with_md_extension_matches(self) -> None:
        node = {
            "title": "Long Title",
            "file_path": "knowledge/concepts/specific-thing.md",
        }
        assert _rl_is_literal_cited(node, "see knowledge/concepts/specific-thing.md")

    def test_empty_title_and_path_returns_false(self) -> None:
        node = {"title": "", "file_path": ""}
        assert not _rl_is_literal_cited(node, "any answer text here")

    def test_non_dict_input_returns_false(self) -> None:
        # Defensive: garbage input shouldn't crash the monitor.
        assert not _rl_is_literal_cited(None, "any text")  # type: ignore[arg-type]
        assert not _rl_is_literal_cited("not a dict", "any text")  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# 2. _rl_compute_and_write_citations — orchestration
# ----------------------------------------------------------------------


def _fake_chunk(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=text)


class _FakeChunker:
    """Stand-in for ``Chunker.for_model(...)``. Splits on newline."""

    def chunk_text(self, text: str, source_id: str = "") -> list:
        return [_fake_chunk(p) for p in text.split("\n") if p.strip()]


class _FakeEmbeddingService:
    """Stand-in for ``EmbeddingService.for_project()``. Returns a vector
    derived from the text length so each chunk's embedding is distinct
    enough to produce a non-degenerate cosine signal."""

    def __init__(self):
        self.calls: list[str] = []

    def embed_text(self, text: str) -> list[float]:
        self.calls.append(text)
        # Deterministic 4-dim "embedding" so the cosine math is testable.
        return [1.0, 0.0, 0.0, float(len(text) % 7) / 10.0]


class _FakeTelemetryWriter:
    """Records the args of every log_citations call so tests can assert
    on what was written."""

    def __init__(self):
        self.calls: list[dict] = []

    def log_citations(self, **kwargs):
        self.calls.append(dict(kwargs))


def _patch_helpers(svc, writer, chunker=None):
    """Patch the four module-level seams in one place."""
    chunker = chunker or _FakeChunker()
    return (
        patch(
            "claude_mcp_servers.weaviate_mcp.server.Chunker.for_model",
            return_value=chunker,
        ),
        patch(
            "claude_mcp_servers.weaviate_mcp.server._get_embedding_service",
            return_value=svc,
        ),
        patch(
            "claude_mcp_servers.weaviate_mcp.server._get_rl_telemetry_writer",
            return_value=writer,
        ),
    )


class TestComputeAndWriteCitations:
    def test_happy_path_writes_event_with_all_fields(self) -> None:
        svc = _FakeEmbeddingService()
        writer = _FakeTelemetryWriter()
        ctx = {
            "active_model": "qwen3-embedding:0.6b",
            "nodes": [
                {
                    "title": "NodeA",
                    "n_emb": [1.0, 0.0, 0.0, 0.0],
                    "file_path": "knowledge/concepts/node-a.md",
                },
                {
                    "title": "NodeB",
                    "n_emb": [0.0, 1.0, 0.0, 0.0],
                    "file_path": "knowledge/concepts/node-b.md",
                },
            ],
            "task_type": "mcp_interactive",
        }
        answer = "we used NodeA from knowledge/concepts/node-a.md to solve this"

        p1, p2, p3 = _patch_helpers(svc, writer)
        with p1, p2, p3:
            result = asyncio.run(
                _rl_compute_and_write_citations("task-123", answer, ctx)
            )
        # v0.2.9 C8: return type is dict|None (was bool). Truthy dict on
        # success carries cosine_sims + literal_cited + cited for the
        # caller's downstream container POST.
        assert result is not None
        assert "cosine_sims" in result
        assert "literal_cited" in result
        # Stashed back on ctx for monitor reuse.
        assert ctx.get("cosine_sims_computed") == result["cosine_sims"]
        assert ctx.get("literal_cited_computed") == result["literal_cited"]
        assert len(writer.calls) == 1
        call = writer.calls[0]
        assert call["task_id"] == "task-123"
        assert call["task_type"] == "mcp_interactive"
        # Both nodes get cosine_sims (each had n_emb).
        assert set(call["cosine_sims"].keys()) == {"NodeA", "NodeB"}
        # NodeA appears literally; NodeB does not.
        assert call["literal_cited"] == {"NodeA": True, "NodeB": False}
        assert call["cross_encoder_cited"] is None
        # Binary citations derived via compute_unified_targets — at least
        # NodeA is cited (literal-cited bonus lifts it past 0.6).
        assert call["citations"]["NodeA"] is True

    def test_empty_nodes_returns_false_without_writing(self) -> None:
        svc = _FakeEmbeddingService()
        writer = _FakeTelemetryWriter()
        ctx = {"active_model": "qwen3-embedding:0.6b", "nodes": []}
        p1, p2, p3 = _patch_helpers(svc, writer)
        with p1, p2, p3:
            result = asyncio.run(_rl_compute_and_write_citations("t", "answer", ctx))
        assert result is None
        assert writer.calls == []

    def test_no_embedding_service_returns_false(self) -> None:
        writer = _FakeTelemetryWriter()
        ctx = {
            "active_model": "qwen3-embedding:0.6b",
            "nodes": [{"title": "A", "n_emb": [1.0, 0.0]}],
        }
        # _get_embedding_service returns None — service unavailable.
        p1 = patch(
            "claude_mcp_servers.weaviate_mcp.server.Chunker.for_model",
            return_value=_FakeChunker(),
        )
        p2 = patch(
            "claude_mcp_servers.weaviate_mcp.server._get_embedding_service",
            return_value=None,
        )
        p3 = patch(
            "claude_mcp_servers.weaviate_mcp.server._get_rl_telemetry_writer",
            return_value=writer,
        )
        with p1, p2, p3:
            result = asyncio.run(_rl_compute_and_write_citations("t", "a", ctx))
        assert result is None
        assert writer.calls == []

    def test_chunker_failure_returns_false(self) -> None:
        svc = _FakeEmbeddingService()
        writer = _FakeTelemetryWriter()
        ctx = {
            "active_model": "qwen3-embedding:0.6b",
            "nodes": [{"title": "A", "n_emb": [1.0, 0.0]}],
        }

        class _ExplodingChunker:
            def chunk_text(self, *args, **kwargs):
                raise RuntimeError("simulated chunker crash")

        p1, p2, p3 = _patch_helpers(svc, writer, chunker=_ExplodingChunker())
        with p1, p2, p3:
            result = asyncio.run(_rl_compute_and_write_citations("t", "a", ctx))
        assert result is None
        assert writer.calls == []

    def test_node_without_n_emb_is_dropped_not_fabricated(self) -> None:
        # F-C CORRECTED (v0.2.70): a node with NO n_emb is DROPPED, never
        # fabricated into a citation. The cure for the missing embedding is the
        # F-G attach+regenerate path upstream (so by compute time every node
        # whose text we have HAS a vector); a node that STILL has no vector here
        # means regeneration genuinely failed → drop it. The withdrawn floor /
        # literal-only-fabrication path poisoned training data.
        svc = _FakeEmbeddingService()
        writer = _FakeTelemetryWriter()
        ctx = {
            "active_model": "qwen3-embedding:0.6b",
            "nodes": [
                {"title": "VisibleNode", "file_path": "knowledge/visible.md"},
                # No n_emb and no content to regenerate from → must be dropped.
            ],
        }
        p1, p2, p3 = _patch_helpers(svc, writer)
        with p1, p2, p3:
            result = asyncio.run(
                _rl_compute_and_write_citations(
                    "t",
                    "we use VisibleNode prominently here",
                    ctx,
                )
            )
        # No cosine signal for any node → nothing written (dropped, not
        # fabricated-to-cited).
        assert result is None
        assert writer.calls == []

    def test_node_with_n_emb_still_writes_alongside_dropped_no_emb_node(self) -> None:
        # A node WITH n_emb is scored normally; a sibling without one is dropped
        # from the map (not poisoned in). The write still happens for the scored
        # node.
        svc = _FakeEmbeddingService()
        writer = _FakeTelemetryWriter()
        ctx = {
            "active_model": "qwen3-embedding:0.6b",
            "nodes": [
                {"title": "Scored", "n_emb": [1.0, 0.0]},
                {"title": "NoEmb", "file_path": "knowledge/noemb.md"},
            ],
        }
        p1, p2, p3 = _patch_helpers(svc, writer)
        with p1, p2, p3:
            result = asyncio.run(
                _rl_compute_and_write_citations("t", "Scored and NoEmb mentioned", ctx)
            )
        assert result is not None
        assert writer.calls
        call = writer.calls[0]
        assert "Scored" in call["cosine_sims"]
        assert "NoEmb" not in call["cosine_sims"]
        assert "NoEmb" not in call["literal_cited"]

    def test_no_writer_returns_false(self) -> None:
        svc = _FakeEmbeddingService()
        ctx = {
            "active_model": "qwen3-embedding:0.6b",
            "nodes": [{"title": "A", "n_emb": [1.0, 0.0]}],
        }
        p1 = patch(
            "claude_mcp_servers.weaviate_mcp.server.Chunker.for_model",
            return_value=_FakeChunker(),
        )
        p2 = patch(
            "claude_mcp_servers.weaviate_mcp.server._get_embedding_service",
            return_value=svc,
        )
        p3 = patch(
            "claude_mcp_servers.weaviate_mcp.server._get_rl_telemetry_writer",
            return_value=None,
        )
        with p1, p2, p3:
            result = asyncio.run(_rl_compute_and_write_citations("t", "a", ctx))
        assert result is None

    def test_writer_log_citations_raising_returns_false(self) -> None:
        svc = _FakeEmbeddingService()

        class _ExplodingWriter:
            def log_citations(self, **kwargs):
                raise RuntimeError("simulated hub down")

        writer = _ExplodingWriter()
        ctx = {
            "active_model": "qwen3-embedding:0.6b",
            "nodes": [{"title": "NodeA", "n_emb": [1.0, 0.0, 0.0, 0.0]}],
        }
        p1, p2, p3 = _patch_helpers(svc, writer)
        with p1, p2, p3:
            # No exception propagates.
            result = asyncio.run(_rl_compute_and_write_citations("t", "a", ctx))
        assert result is None


# ----------------------------------------------------------------------
# 3. Thresholds module exports
# ----------------------------------------------------------------------


class TestThresholdConstants:
    def test_min_answer_tokens_default(self) -> None:
        # v0.2.47 RL-7.5: bumped to a token-based threshold.
        # Default 25k tokens; tunable via RL_MIN_ANSWER_TOKENS_FOR_CITATION env.
        # 25k tokens ≈ 2-3 chunks at qwen3's xlarge preset (target=9500).
        assert _RL_MIN_ANSWER_TOKENS_FOR_CITATION >= 10_000

    def test_chars_alias_is_4x_the_token_value(self) -> None:
        # Back-compat alias for downstream callers still importing the
        # chars-based name. 1 token ≈ 4 chars conventional approximation.
        assert (
            _RL_MIN_ANSWER_CHARS_FOR_CITATION
            == _RL_MIN_ANSWER_TOKENS_FOR_CITATION * 4
        )

    def test_min_title_len_is_3(self) -> None:
        # Pinning at 3 — same rule as the paid module.
        assert _RL_LITERAL_CITED_MIN_TITLE_LEN == 3
