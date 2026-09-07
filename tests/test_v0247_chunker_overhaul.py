# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.47 RL-7.5: tests for chunker preset routing + Ollama num_ctx
auto-resolution + subagent transcript discovery.

Three pieces under test:

1. ``MODEL_TOKEN_LIMITS`` now tracks ``num_ctx`` we actually send to
   Ollama (NOT the model's architectural max). Verifies the new
   conservative defaults match what was locked in chat 2026-06-04.

2. ``chunking_preset_for_model`` routes to one of FIVE presets
   (xsmall/small/medium/large/xlarge) instead of three. Each new
   embedding model from the local Ollama inventory gets the right tier.

3. ``OllamaAdapter._num_ctx_for_model`` resolves num_ctx via
   ``MODEL_TOKEN_LIMITS`` lookup with partial-name matching, falling
   back to 8192 for unknown models.

4. ``_rl_find_all_transcripts_in_dir`` finds both parent transcripts
   AND subagent transcripts under ``<sessionId>/subagents/agent-*.jsonl``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from claude_mcp_servers.weaviate_mcp.chunking import (
    CHUNK_TOKEN_POLICY_CEILING,
    CHUNKING_PRESETS,
    MODEL_TOKEN_LIMITS,
    chunking_preset_for_model,
)
from claude_mcp_servers.weaviate_mcp.server import (
    _rl_find_all_transcripts_in_dir,
)
from vco_lib.embedding_providers.ollama import _num_ctx_for_model


# ----------------------------------------------------------------------
# 1. MODEL_TOKEN_LIMITS — verified values from 2026-06-04 chat
# ----------------------------------------------------------------------


class TestModelTokenLimits:
    def test_qwen3_embedding_set_to_10k(self) -> None:
        # User-locked conservative value for the 0.6B model (architecture
        # supports 32k, we use 10k to keep quality high).
        assert MODEL_TOKEN_LIMITS["qwen3-embedding:0.6b"] == 10_240
        assert MODEL_TOKEN_LIMITS["qwen3-embedding"] == 10_240

    def test_arctic2_set_to_4k(self) -> None:
        # User-locked: keep arctic2 as "the light one".
        assert MODEL_TOKEN_LIMITS["snowflake-arctic-embed2:latest"] == 4_096

    def test_jina_v2_base_code_set_to_2k(self) -> None:
        # User-locked: jina v2 was trained at 512; 2k is safe middle ground.
        assert (
            MODEL_TOKEN_LIMITS["unclemusclez/jina-embeddings-v2-base-code:latest"]
            == 2_048
        )

    def test_codesage_at_served_window(self) -> None:
        # W1 (wiring audit, 2026-09-05): the SERVED window, not the 2 048
        # architectural cap — sentence_bert_config.json max_seq_length=1024,
        # verified live (the vector is identical past position 1 024).
        assert MODEL_TOKEN_LIMITS["codesage/codesage-large-v2"] == 1_024

    def test_openai_text_3_small_at_8191(self) -> None:
        assert MODEL_TOKEN_LIMITS["text-embedding-3-small"] == 8_191

    def test_new_models_have_entries(self) -> None:
        # NEW in v0.2.47 RL-7.5: bge-m3, embeddinggemma, granite-embedding.
        assert MODEL_TOKEN_LIMITS["bge-m3:latest"] == 8_192
        assert MODEL_TOKEN_LIMITS["embeddinggemma:300m-bf16"] == 2_048
        assert MODEL_TOKEN_LIMITS["granite-embedding:278m-fp16"] == 512


# ----------------------------------------------------------------------
# 2. Five-tier preset routing
# ----------------------------------------------------------------------


class TestChunkingPresets:
    def test_five_tiers_exist(self) -> None:
        for tier in (
            "xsmall_context",
            "small_context",
            "medium_context",
            "large_context",
            "xlarge_context",
        ):
            assert tier in CHUNKING_PRESETS

    def test_preset_values_match_locked_design(self) -> None:
        # User-locked numbers from 2026-06-04 chat.
        assert CHUNKING_PRESETS["xsmall_context"] == (170, 400, 330)
        assert CHUNKING_PRESETS["small_context"] == (550, 1600, 1100)
        assert CHUNKING_PRESETS["medium_context"] == (1100, 3200, 2500)
        assert CHUNKING_PRESETS["large_context"] == (2200, 6400, 4600)
        assert CHUNKING_PRESETS["xlarge_context"] == (4600, 13500, 9500)

    @pytest.mark.parametrize(
        "model_name,expected_preset_name",
        [
            ("granite-embedding:278m-fp16", "xsmall_context"),
            ("embeddinggemma:300m-bf16", "small_context"),
            ("jina-embeddings-v2-base-code", "small_context"),
            ("unclemusclez/jina-embeddings-v2-base-code:latest", "small_context"),
            ("snowflake-arctic-embed2:latest", "medium_context"),
            ("snowflake-arctic-embed2", "medium_context"),
            ("text-embedding-3-small", "large_context"),
            ("bge-m3:latest", "large_context"),
            # Unknown model UNDER-fills to the tightest general tier (D16,
            # v0.2.92) — the old large_context guess could over-fill an unknown
            # model whose real window is smaller than the 8k-class tier assumes.
            ("unknown-model-foo", "small_context"),
        ],
    )
    def test_routing(self, model_name: str, expected_preset_name: str) -> None:
        actual = chunking_preset_for_model(model_name)
        expected = CHUNKING_PRESETS[expected_preset_name]
        assert actual == expected, (
            f"{model_name} routed to {actual}, expected {expected_preset_name}={expected}"
        )

    def test_codesage_routes_to_small_clamped_to_its_measured_window(self) -> None:
        """Wiring-audit W1 (v0.2.92): CodeSage's served window is 1 024, not
        the 2 048 its architectural cap advertises — measured, and confirmed
        by the snapshot's own ``sentence_bert_config.json``.

        It therefore no longer routes to an UNCLAMPED ``small_context``: that
        tier's 1 600-token max exceeds the real window, which is exactly the
        over-budgeting that silently halved every maximal code entity at
        HTTP 200. R39's rule applies — the retrieval-quality policy bounds
        the chunk, the model's own window can only tighten it further — so
        the tier is kept and clamped componentwise to
        ``int(1024 * (1 - _BUDGET_SAFETY_MARGIN_RATIO))``.

        Kept OUT of the generic routing table above and given its own test
        for the same reason qwen3 is: the table asserts "routes to tier X"
        and a clamped model does not equal its tier's published tuple.
        """
        from claude_mcp_servers.weaviate_mcp.chunking import (
            _BUDGET_SAFETY_MARGIN_RATIO,
            MODEL_TOKEN_LIMITS,
        )

        window = MODEL_TOKEN_LIMITS["codesage-large-v2"]
        assert window == 1_024, (
            "the measured served window; if this changes, re-measure the "
            "plateau rather than trusting the architectural cap"
        )
        budget = int(window * (1 - _BUDGET_SAFETY_MARGIN_RATIO))
        min_t, max_t, target_t = CHUNKING_PRESETS["small_context"]
        assert chunking_preset_for_model("codesage-large-v2") == (
            min(min_t, budget), min(max_t, budget), min(target_t, budget)
        )
        # And the load-bearing property, stated independently of the formula:
        # nothing the chunker emits may exceed the window the model serves.
        assert max(chunking_preset_for_model("codesage-large-v2")) <= window

    def test_qwen3_routes_to_xlarge_clamped_to_its_own_window(self) -> None:
        # D16 + R39 (v0.2.92): qwen3-embedding's num_ctx is 10 240 — the
        # xlarge tier SHAPE (max 13 500) was cut for a ~16k window. The
        # resolver keeps the tier but clamps it componentwise to
        # min(CHUNK_TOKEN_POLICY_CEILING 8 192, the model's own window
        # budget int(10 240 * 0.9) = 9 216) — the R39 retrieval-quality
        # policy binds for qwen3 (8 192 < 9 216), so a max-packed chunk
        # can never exceed the embedding window (silent Ollama truncation)
        # NOR the retrieval-quality ceiling.
        capacity = int(MODEL_TOKEN_LIMITS["qwen3-embedding:0.6b"] * (1 - 0.1))
        budget = min(capacity, CHUNK_TOKEN_POLICY_CEILING)
        assert chunking_preset_for_model("qwen3-embedding:0.6b") == (
            4_600, 8_192, 8_192
        ) == (
            min(4_600, budget), min(13_500, budget), min(9_500, budget)
        )


# ----------------------------------------------------------------------
# 3. Ollama num_ctx resolution
# ----------------------------------------------------------------------


class TestNumCtxResolution:
    def test_known_model_returns_token_limit(self) -> None:
        assert _num_ctx_for_model("qwen3-embedding:0.6b") == 10_240
        assert _num_ctx_for_model("snowflake-arctic-embed2:latest") == 4_096

    def test_unknown_model_falls_back_to_8192(self) -> None:
        assert _num_ctx_for_model("never-heard-of-this:7b") == 8_192

    def test_partial_name_match_works(self) -> None:
        # Caller passes the short name; dict has the tagged one. Should
        # still resolve via the substring-match fallback.
        assert _num_ctx_for_model("qwen3-embedding") == 10_240


# ----------------------------------------------------------------------
# 4. Subagent transcript discovery
# ----------------------------------------------------------------------


class TestSubagentTranscriptDiscovery:
    def test_finds_parent_transcripts_only_when_no_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            slug_dir = Path(td)
            (slug_dir / "session-001.jsonl").write_text('{"type":"user"}\n')
            (slug_dir / "session-002.jsonl").write_text('{"type":"user"}\n')
            results = _rl_find_all_transcripts_in_dir(slug_dir)
            assert len(results) == 2
            assert all(p.name.startswith("session-") for p in results)

    def test_finds_subagent_transcripts_alongside_parents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            slug_dir = Path(td)
            (slug_dir / "parent-aaa.jsonl").write_text("{}\n")
            # Subagent transcripts live in a nested subagents/ dir
            (slug_dir / "parent-aaa" / "subagents").mkdir(parents=True)
            (slug_dir / "parent-aaa" / "subagents" / "agent-bbb.jsonl").write_text("{}\n")
            (slug_dir / "parent-aaa" / "subagents" / "agent-ccc.jsonl").write_text("{}\n")
            results = _rl_find_all_transcripts_in_dir(slug_dir)
            names = sorted(p.name for p in results)
            assert names == ["agent-bbb.jsonl", "agent-ccc.jsonl", "parent-aaa.jsonl"]

    def test_subagents_under_multiple_parent_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            slug_dir = Path(td)
            # Two parent sessions, each with one subagent.
            for parent in ("session-AAA", "session-BBB"):
                (slug_dir / f"{parent}.jsonl").write_text("{}\n")
                (slug_dir / parent / "subagents").mkdir(parents=True)
                (slug_dir / parent / "subagents" / f"agent-from-{parent}.jsonl").write_text("{}\n")
            results = _rl_find_all_transcripts_in_dir(slug_dir)
            assert len(results) == 4

    def test_non_subagent_subdirs_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            slug_dir = Path(td)
            (slug_dir / "session-001.jsonl").write_text("{}\n")
            # Some other random subdir (not under subagents/) — must be ignored.
            (slug_dir / "session-001" / "other").mkdir(parents=True)
            (slug_dir / "session-001" / "other" / "spurious.jsonl").write_text("{}\n")
            results = _rl_find_all_transcripts_in_dir(slug_dir)
            assert len(results) == 1
            assert results[0].name == "session-001.jsonl"

    def test_missing_slug_dir_returns_empty(self) -> None:
        results = _rl_find_all_transcripts_in_dir(Path("/nonexistent/path/xyz"))
        assert results == []
