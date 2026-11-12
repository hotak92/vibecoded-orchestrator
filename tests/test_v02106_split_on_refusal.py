# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 split on refusal (owner ruling 2026-11-11).

When the embedding model refuses a chunk (context window exceeded), the chunk
is split in two at the best boundary nearest its middle and both halves are
embedded, recursively, so no content is ever lost. Row writers (kg-sync node
and doc paths, the MCP ``store_knowledge_node``, the shipped-sidecar builder)
store the pieces as extra rows of the same node — and since the follow-up
ruling ("extra rows in both places") a refusal by ANY model of the row
(secondary, carry) splits the rows too, so no stored vector is averaged; only
a one-vector consumer outside a row writer combines the piece vectors. Prose
that fits keeps today's chunks and vectors. The code-graph half lives in
``tests/test_v02106_split_rows.py``.

Every fake embedder here refuses above a TOKEN limit computed at a dense
ratio (1.5 chars/token), the shape of the real refusals measured on JSON,
tables and CJK.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import embed_split as ES  # noqa: E402
from vco_lib import embedding_fidelity as F  # noqa: E402

DENSE_CHARS_PER_TOKEN = 1.5


def _tokens(text: str) -> int:
    return math.ceil(len(text) / DENSE_CHARS_PER_TOKEN)


def _overflow_exc(n: int) -> RuntimeError:
    # The exact Ollama /api/embed refusal body (HTTP 400).
    return RuntimeError(
        f"Ollama HTTP 400: {{\"error\":\"the input length exceeds the context "
        f"length\"}} ({n} chars)"
    )


def _vec_for(text: str) -> list[float]:
    """A deterministic 4-dim 'embedding' that depends on the whole text."""
    h = sum((i + 1) * ord(c) for i, c in enumerate(text[:2000])) + len(text)
    return [float((h >> s) % 97 + 1) for s in (0, 3, 7, 11)]


def _dense_text(paragraphs: int = 12) -> str:
    """JSON-ish dense content with paragraph, sentence and line boundaries."""
    parts = []
    for p in range(paragraphs):
        lines = [
            f'{{"id": {p * 100 + i}, "k": "v{i:03d}", "arr": [1,2,3,4,5]}}.'
            for i in range(8)
        ]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


@pytest.fixture(autouse=True)
def _fresh_fidelity_state():
    F.reset_run_state()
    yield
    F.reset_run_state()


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


class TestSplitPoint:
    def test_paragraph_beats_a_nearer_sentence(self):
        text = "a" * 40 + "\n\n" + "b" * 20 + ". " + "c" * 38
        pos = ES.split_point(text)
        assert text[:pos].endswith("\n\n")

    def test_sentence_beats_line_and_whitespace(self):
        text = "word " * 10 + "end. " + "x" * 10 + "\n" + "word " * 8
        pos = ES.split_point(text)
        assert text[:pos].rstrip().endswith(".")

    def test_line_then_whitespace_then_hard_cut(self):
        assert "\n" in "aaaa bbbb\ncccc dddd"
        text = "aaaa bbbb\ncccc dddd"
        assert text[: ES.split_point(text)].endswith("\n")
        text2 = "aaaaaaa bbbbbbb"
        assert text2[: ES.split_point(text2)].endswith(" ")
        text3 = "x" * 101
        assert ES.split_point(text3) == 50

    def test_only_the_middle_half_qualifies(self):
        # The only paragraph break sits at 5% — outside the middle half, so
        # the cut falls back to whitespace near the middle.
        text = "a" * 5 + "\n\n" + "b " * 50
        pos = ES.split_point(text)
        n = len(text)
        assert n // 4 <= pos <= n - n // 4

    def test_never_splits_a_surrogate_pair(self):
        pair = "😀"  # an emoji as a UTF-16 surrogate pair
        text = pair * 51  # 102 code units, no boundaries, middle = 51 (a low surrogate)
        pos = ES.split_point(text)
        assert not (ES._is_high_surrogate(text[pos - 1]) and ES._is_low_surrogate(text[pos]))

    def test_code_points_are_never_cut(self):
        text = "日本語のテキスト" * 40
        left, right = ES.split_in_two(text)
        assert left + right == text
        left.encode("utf-8")
        right.encode("utf-8")

    def test_deterministic_and_lossless(self):
        text = _dense_text(5)
        assert ES.split_point(text) == ES.split_point(text)
        left, right = ES.split_in_two(text)
        assert left + right == text and left and right

    def test_pieces_within_bounds_every_piece(self):
        text = _dense_text(10)
        pieces = ES.pieces_within(text, 300)
        assert "".join(pieces) == text
        assert all(len(p) <= 300 for p in pieces)
        assert ES.pieces_within(text, 0) == [text]
        assert ES.pieces_within("short", 300) == ["short"]


# ---------------------------------------------------------------------------
# One vector (split + combine)
# ---------------------------------------------------------------------------


class _RefusingEmbedder:
    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens
        self.accepted: list[str] = []
        self.calls = 0

    def __call__(self, text: str) -> list[float]:
        self.calls += 1
        if _tokens(text) > self.max_tokens:
            raise _overflow_exc(len(text))
        self.accepted.append(text)
        return _vec_for(text)


def _is_overflow(exc: BaseException) -> bool:
    from vco_lib.embedding_service import _is_context_overflow_error
    return _is_context_overflow_error(exc)


class TestEmbedSplitPieces:
    def test_text_that_fits_is_one_call_and_unchanged(self):
        emb = _RefusingEmbedder(10_000)
        text = _dense_text(2)
        pieces = ES.embed_split_pieces(emb, text, is_overflow=_is_overflow)
        assert pieces == [(text, _vec_for(text))]
        assert emb.calls == 1

    def test_refused_text_recurses_until_every_piece_fits(self):
        emb = _RefusingEmbedder(600)
        text = _dense_text(12)
        assert _tokens(text) > 600 * 4
        pieces = ES.embed_split_pieces(emb, text, is_overflow=_is_overflow)
        assert "".join(p for p, _ in pieces) == text  # nothing lost
        assert all(_tokens(p) <= 600 for p, _ in pieces)
        assert len(pieces) >= 4
        again = ES.embed_split_pieces(_RefusingEmbedder(600), text, is_overflow=_is_overflow)
        assert [p for p, _ in again] == [p for p, _ in pieces]  # deterministic

    def test_non_overflow_error_propagates_untouched(self):
        def boom(_t):
            raise RuntimeError("401 unauthorized")

        with pytest.raises(RuntimeError, match="401"):
            ES.embed_split_pieces(boom, "x" * 5000, is_overflow=_is_overflow)

    def test_floor_raises_unsplittable_never_drops(self):
        def always_refuse(t):
            raise _overflow_exc(len(t))

        with pytest.raises(ES.UnsplittableTextError) as info:
            ES.embed_split_pieces(always_refuse, "y " * 2000, is_overflow=_is_overflow,
                                  model_id="m")
        assert info.value.chars <= ES.SPLIT_FLOOR_CHARS

    def test_combine_single_piece_is_identity(self):
        assert ES.combine_vectors([("abc", [3.0, 4.0])]) == [3.0, 4.0]

    def test_combine_is_length_weighted_and_normalised(self):
        out = ES.combine_vectors([("a" * 3, [1.0, 0.0]), ("b", [0.0, 1.0])])
        assert math.isclose(math.hypot(*out), 1.0)
        assert out[0] > out[1]  # the longer piece dominates (3:1)
        assert math.isclose(out[0] / out[1], 3.0)

    def test_combine_rejects_mismatched_dimensions(self):
        with pytest.raises(ValueError):
            ES.combine_vectors([("a", [1.0]), ("b", [1.0, 2.0])])


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


class _RowEmbedder:
    """Raises ChunkOverflowError (what the service raises in a row scope)."""

    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens
        self.calls: list[str] = []
        self.saw_scope: list[bool] = []

    def __call__(self, text: str):
        self.calls.append(text)
        self.saw_scope.append(ES.row_split_active())
        if _tokens(text) > self.max_tokens:
            raise ES.ChunkOverflowError("dense-model", len(text), "exceeds the context length")
        return _vec_for(text)


class TestEmbedRows:
    def test_only_refused_chunks_split_and_numbering_is_contiguous(self):
        small = "short prose chunk."
        big = _dense_text(12)
        emb = _RowEmbedder(450)
        rows = ES.embed_rows([small, big, small], emb)
        assert all(emb.saw_scope)
        assert not ES.row_split_active()  # the scope does not leak
        by_source: dict[int, list[ES.RowPiece]] = {}
        for r in rows:
            by_source.setdefault(r.source_index, []).append(r)
        assert len(by_source[0]) == 1 and len(by_source[2]) == 1
        assert len(by_source[1]) > 1
        # Reassembly equals the original — no content lost, in order.
        assert "".join(r.text for r in by_source[1]) == big
        assert [r.piece_index for r in by_source[1]] == list(range(len(by_source[1])))
        assert {r.piece_count for r in by_source[1]} == {len(by_source[1])}
        assert all(_tokens(r.text) <= 450 for r in rows)
        assert [r.source_index for r in rows] == sorted(r.source_index for r in rows)

    def test_rows_are_deterministic(self):
        texts = [_dense_text(9), _dense_text(3)]
        a = ES.embed_rows(texts, _RowEmbedder(420))
        b = ES.embed_rows(texts, _RowEmbedder(420))
        assert [(r.text, r.source_index, r.payload) for r in a] == [
            (r.text, r.source_index, r.payload) for r in b
        ]

    def test_split_is_recorded_as_a_split_not_a_truncation(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
        ES.embed_rows([_dense_text(12)], _RowEmbedder(450))
        state = F._STATE
        assert state.splits["dense-model"]["count"] == 1
        assert state.splits["dense-model"]["pieces"] > 1
        assert state.shrinks == {}

    def test_unsplittable_piece_is_reported(self):
        class _Always:
            def __call__(self, text):
                raise ES.ChunkOverflowError("m", len(text), "nope")

        with pytest.raises(ES.UnsplittableTextError):
            ES.embed_rows(["z " * 3000], _Always())
        assert F._STATE.floor_refusals["m"]["count"] == 1

    def test_other_errors_propagate(self):
        def boom(_t):
            raise ConnectionError("backend down")

        with pytest.raises(ConnectionError):
            ES.embed_rows(["text"], boom)

    def test_async_twin_produces_the_same_rows(self):
        texts = [_dense_text(10), "fits"]
        sync_rows = ES.embed_rows(texts, _RowEmbedder(430))
        emb = _RowEmbedder(430)

        async def _aemb(text):
            await asyncio.sleep(0)
            return emb(text)

        async_rows = asyncio.run(ES.embed_rows_async(texts, _aemb))
        assert [(r.text, r.source_index, r.piece_index) for r in async_rows] == [
            (r.text, r.source_index, r.piece_index) for r in sync_rows
        ]
        assert all(emb.saw_scope)

    def test_scope_nests_and_resets(self):
        # v0.2.106 "extra rows in both places": there is no suspended scope any
        # more — the carry splits rows like every other model of the row.
        assert not hasattr(ES, "row_split_suspended")
        with ES.row_split_scope():
            with ES.row_split_scope():
                assert ES.row_split_active()
            assert ES.row_split_active()
        assert not ES.row_split_active()


class TestLayout:
    def test_layout_round_trip(self):
        texts = ["fits", _dense_text(12), "also fits"]
        rows = ES.embed_rows(texts, _RowEmbedder(450))
        layout = ES.row_layout(rows)
        assert list(layout) == ["2"]
        rebuilt = ES.apply_row_layout(texts, json.loads(json.dumps(layout)))
        assert rebuilt == [(r.source_index, r.text) for r in rows]
        assert ES.apply_row_layout(texts, {}) == list(enumerate(texts))

    @pytest.mark.parametrize("bad", [
        {"9": [1, 2]},            # out of range
        {"1": [1, 1]},            # lengths do not sum to the chunk
        {"1": [4]},               # a single piece is not a split
        {"1": [0, 4]},            # non-positive length
        {"x": [2, 2]},            # non-integer key
        {"1": "22"},              # not a list
    ])
    def test_bad_layouts_are_refused(self, bad):
        assert ES.apply_row_layout(["fits"], bad) is None

    def test_stored_rows_cover_plan(self):
        plan = ["abcdef", "ghij"]
        assert ES.stored_rows_cover_plan(["abcdef", "ghij"], plan)
        assert ES.stored_rows_cover_plan(["abc", "def", "gh", "ij"], plan)
        assert not ES.stored_rows_cover_plan(["abcde", "fghij"], plan)  # moved boundary
        assert not ES.stored_rows_cover_plan(["abcdef"], plan)
        assert not ES.stored_rows_cover_plan(["abcdef", "ghij", "x"], plan)
        assert not ES.stored_rows_cover_plan(["abcdef", None], plan)


# ---------------------------------------------------------------------------
# EmbeddingService wiring
# ---------------------------------------------------------------------------


class _FakeOllama:
    """Refuses above ``max_tokens`` at the dense ratio, records what it saw."""

    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens
        self.accepted: list[tuple[str, str]] = []

    def is_reachable(self) -> bool:
        return True

    def embed(self, model_id: str, text: str) -> list[float]:
        if _tokens(text) > self.max_tokens:
            raise _overflow_exc(len(text))
        self.accepted.append((model_id, text))
        return _vec_for(text)

    def embed_batch(self, model_id: str, texts: list[str]) -> list[list[float]]:
        for t in texts:
            if _tokens(t) > self.max_tokens:
                raise _overflow_exc(len(t))
        self.accepted.extend((model_id, t) for t in texts)
        return [_vec_for(t) for t in texts]


def _service(max_tokens: int, model: str = "qwen3-embedding:0.6b"):
    from vco_lib.embedding_service import EmbeddingService

    svc = EmbeddingService(
        project_root=REPO_ROOT,
        ollama_url="http://127.0.0.1:9",
        code_embed_url="http://127.0.0.1:9",
        text_model_id=model,
        code_model_id=model,
        openai_api_key="",
    )
    svc.ollama = _FakeOllama(max_tokens)
    return svc


class TestServiceWiring:
    def test_document_embed_combines_and_records_no_truncation(self):
        svc = _service(400)
        text = _dense_text(12)
        vec = svc._embed_text_via_active(text)
        sent = [t for _m, t in svc.ollama.accepted]
        assert "".join(sent) == text  # every character embedded
        assert len(sent) > 1
        assert vec == ES.combine_vectors([(t, _vec_for(t)) for t in sent])
        assert svc.last_active_truncated is False
        assert F._STATE.splits and not F._STATE.shrinks

    def test_fitting_document_vector_is_byte_identical(self):
        svc = _service(100_000)
        text = _dense_text(3)
        assert svc._embed_text_via_active(text) == _vec_for(text)

    def test_row_scope_makes_the_active_leg_raise(self):
        svc = _service(400)
        with ES.row_split_scope():
            with pytest.raises(ES.ChunkOverflowError) as info:
                svc._embed_text_via_active(_dense_text(12))
        assert info.value.model_id == "qwen3-embedding:0.6b"
        # ... and through the tagged fan-out, which must not swallow it.
        with ES.row_split_scope():
            with pytest.raises(ES.ChunkOverflowError):
                svc.embed_text_all_configured_tagged(_dense_text(12))

    def test_tagged_rows_through_the_service_lose_nothing(self):
        svc = _service(400)
        text = _dense_text(12)
        rows = ES.embed_rows([text], svc.embed_text_all_configured_tagged)
        assert "".join(r.text for r in rows) == text
        for r in rows:
            slots, truncated = r.payload
            assert slots[svc.text_vector_slot] == _vec_for(r.text)
            assert truncated == []

    def test_query_keeps_the_leading_window_ladder(self):
        svc = _service(400)
        svc.embed_text_query(_dense_text(12), "kg_search")
        assert F._STATE.shrinks  # recorded as a shrink — the query arm
        assert svc.last_active_truncated is True

    def test_secondary_document_is_whole_and_not_tagged(self):
        from vco_lib.embedding_service import _embed_secondary_with_refusal_retry

        fake = _FakeOllama(100_000)
        text = _dense_text(12)
        vec, truncated = _embed_secondary_with_refusal_retry(
            fake, "snowflake-arctic-embed2:latest", text
        )
        assert truncated is False
        assert [t for _m, t in fake.accepted] == [text]  # ONE whole call
        assert vec == _vec_for(text)

    def test_secondary_document_refusal_raises_never_averages(self):
        # Red-proof target: afa192f4 pre-split by the char budget and combined
        # the piece vectors into the row's one secondary vector.
        from vco_lib.embedding_service import _embed_secondary_with_refusal_retry

        fake = _FakeOllama(450)
        with pytest.raises(ES.ChunkOverflowError) as info:
            _embed_secondary_with_refusal_retry(
                fake, "snowflake-arctic-embed2:latest", _dense_text(12)
            )
        assert info.value.model_id == "snowflake-arctic-embed2:latest"
        assert fake.accepted == []  # nothing embedded, nothing averaged

    def test_secondary_query_still_bounds(self):
        from vco_lib.embedding_service import _embed_secondary_with_refusal_retry

        fake = _FakeOllama(600)
        _vec, truncated = _embed_secondary_with_refusal_retry(
            fake, "snowflake-arctic-embed2:latest", _dense_text(12), query=True
        )
        assert truncated is True

    def test_batch_embeds_over_budget_items_whole_and_alone(self):
        # The batch only fills EXISTING rows (enrichment): an over-budget item
        # is kept out of the batch call and embedded whole — never pieces.
        from vco_lib.embedding_service import _char_budget_for_model

        model = "snowflake-arctic-embed2:latest"
        svc = _service(100_000, model=model)
        budget = _char_budget_for_model(model)
        big = (_dense_text(12) * 8)[: budget * 2 + 7]
        small = "a short row"
        out = svc._embed_ollama_batch_bounded(model, [small, big])
        assert out == [_vec_for(small), _vec_for(big)]
        assert [t for _m, t in svc.ollama.accepted] == [small, big]

    def test_batch_refusal_yields_the_sentinel_never_an_average(self):
        model = "qwen3-embedding:0.6b"
        svc = _service(400, model=model)
        text = _dense_text(12)  # under the char budget, over the dense limit
        out = svc._embed_ollama_batch_bounded(model, ["fits", text])
        assert out[0] == _vec_for("fits")
        assert out[1] == []  # the row's writer re-lays it as split rows
        assert all(t != text for _m, t in svc.ollama.accepted)

    def test_carry_refusal_splits_rows_instead_of_averaging(self, monkeypatch):
        from vco_lib import kg_embed_model as K

        carry_svc = _service(450)

        class _Res:
            carry = "snowflake-arctic-embed2:latest"

        monkeypatch.setattr(K, "resolve_kg_model", lambda *a, **k: _Res())
        monkeypatch.setattr(K, "sibling_text_service", lambda svc, model: carry_svc)
        target = _service(100_000)
        text = _dense_text(12)
        with ES.row_split_scope():
            with pytest.raises(ES.ChunkOverflowError):
                K.complete_write_vectors(
                    target, {target.text_vector_slot: [1.0, 0, 0, 0]}, text, "Coll", [])

        # Through the row driver: every row carries BOTH models' whole vectors.
        def _row(piece: str):
            slots = {target.text_vector_slot: target._embed_text_via_active(piece)}
            return K.complete_write_vectors(target, slots, piece, "Coll", [])

        rows = ES.embed_rows([text], _row)
        assert len(rows) > 1 and "".join(r.text for r in rows) == text
        carry_slot = K.text_model_slot(_Res.carry)[0]
        for r in rows:
            assert r.payload[carry_slot] == _vec_for(r.text)
            assert r.payload[target.text_vector_slot] == _vec_for(r.text)

    def test_secondary_refusal_splits_rows_for_every_model(self, monkeypatch):
        # The finest split any model of the row's vector set needs: the active
        # model accepts the chunk, the arctic secondary refuses it → BOTH
        # models embed every piece; no row lacks a vector, none is averaged.
        monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
        monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "true")

        class _TwoWindows(_FakeOllama):
            def embed(self, model_id, text):
                limit = 450 if "arctic" in model_id else 100_000
                if _tokens(text) > limit:
                    raise _overflow_exc(len(text))
                self.accepted.append((model_id, text))
                return _vec_for(model_id + text)

        svc = _service(100_000)
        svc.ollama = _TwoWindows(0)
        text = _dense_text(12)
        rows = ES.embed_rows([text], svc.embed_text_all_configured_tagged)
        assert len(rows) > 1 and "".join(r.text for r in rows) == text
        for r in rows:
            slots, truncated = r.payload
            assert set(slots) == {"qwen3_embed", "arctic2_embed"}
            assert slots["arctic2_embed"] == _vec_for("snowflake-arctic-embed2:latest" + r.text)
            assert slots["qwen3_embed"] == _vec_for("qwen3-embedding:0.6b" + r.text)
            assert truncated == []
        # Deterministic for the model set: the same chunk → the same rows.
        again = ES.embed_rows([text], svc.embed_text_all_configured_tagged)
        assert [r.text for r in again] == [r.text for r in rows]

    def test_secondary_refusal_outside_a_row_writer_leaves_the_slot_empty(self, monkeypatch):
        monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
        monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "true")

        class _ArcticRefuses(_FakeOllama):
            def embed(self, model_id, text):
                if "arctic" in model_id:
                    raise _overflow_exc(len(text))
                return _vec_for(text)

        svc = _service(100_000)
        svc.ollama = _ArcticRefuses(0)
        slots = svc.embed_text_all_configured(_dense_text(12))
        assert "arctic2_embed" not in slots and "qwen3_embed" in slots

    def test_a_combined_vector_is_never_memoized_for_a_row_writer(self):
        svc = _service(400)
        text = _dense_text(12)
        outside = svc.embed_text(text)  # one-vector consumer: combined
        assert len(svc.ollama.accepted) > 1
        with ES.row_split_scope():
            with pytest.raises(ES.ChunkOverflowError):
                svc.embed_text(text)  # a row writer must split, not reuse it
        assert outside


# ---------------------------------------------------------------------------
# Query-enrichment budget
# ---------------------------------------------------------------------------


class TestQueryEnrichmentBudget:
    @pytest.mark.parametrize("model", [
        "qwen3-embedding:0.6b", "snowflake-arctic-embed2:latest",
        "text-embedding-3-small",
    ])
    def test_budget_never_exceeds_the_dense_budget(self, model):
        from weaviate_mcp.chunking import CHARS_PER_TOKEN_TEXT, dense_char_budget
        from vco_lib.query_enrichment import _resolve_budget

        budget, _t, conservative = _resolve_budget([model])
        assert conservative is False
        assert budget * CHARS_PER_TOKEN_TEXT <= dense_char_budget(model)
        assert budget == dense_char_budget(model) // CHARS_PER_TOKEN_TEXT

    def test_qwen3_budget_is_no_longer_9216_units(self):
        from vco_lib.query_enrichment import _resolve_budget

        assert _resolve_budget(["qwen3-embedding:0.6b"])[0] == 4_890

    def test_multi_model_takes_the_tightest(self):
        from weaviate_mcp.chunking import CHARS_PER_TOKEN_TEXT, dense_char_budget
        from vco_lib.query_enrichment import _resolve_budget

        budget, _t, _c = _resolve_budget(
            ["qwen3-embedding:0.6b", "snowflake-arctic-embed2:latest"]
        )
        assert budget == dense_char_budget("snowflake-arctic-embed2:latest") // CHARS_PER_TOKEN_TEXT


# ---------------------------------------------------------------------------
# Fidelity accounting
# ---------------------------------------------------------------------------


class TestFidelityAccounting:
    def test_split_only_run_writes_a_row_but_raises_no_notice(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
        F.note_split("qwen3-embedding:0.6b", 30_000, 3)
        assert F.flush_run() is True
        rows = [json.loads(line) for line in F._jsonl_path().read_text().splitlines()]
        assert rows[-1]["splits"]["qwen3-embedding:0.6b"] == {
            "count": 1, "chars": 30_000, "pieces": 3,
        }
        assert rows[-1]["shrinks"] == {}
        assert F.render_notice(rows, F._jsonl_path()) == ""

    def test_a_loss_row_lists_splits_separately(self):
        row = {
            "kind": F.KIND_SHRINK_SUMMARY, "timestamp": "t",
            "shrinks": {"m": {"count": 1, "orig_chars": 100, "sent_chars": 50}},
            "floor_refusals": {}, "splits": {"m": {"count": 2, "chars": 10, "pieces": 5}},
        }
        notice = F.render_notice([row], Path("/x"))
        assert "leading" in notice and "split into 5 piece(s)" in notice


# ---------------------------------------------------------------------------
# Writers: kg-sync + MCP store (shared fakes from the W8 parity test)
# ---------------------------------------------------------------------------

from tests import test_wiring_w8_shared_chunk_plan as W8  # noqa: E402

REFUSE_ABOVE_CHARS = 9_000  # the fake active model's window for these tests


class _RefusingSyncServer(W8._SyncServer):
    def __init__(self):
        super().__init__()
        self.embedded: list[str] = []

    def _get_all_kg_embeddings_tagged(self, text):
        if len(text) > REFUSE_ABOVE_CHARS:
            raise ES.ChunkOverflowError("qwen3-embedding:0.6b", len(text), "exceeds the context length")
        self.embedded.append(text)
        return {self.text_vector_slot: _vec_for(text)[:2]}, []


def _dense_body() -> str:
    """A single-chunk node (under the qwen3 preset max) the model refuses."""
    return _dense_text(30)


class TestWriters:
    @pytest.fixture
    def harness(self, tmp_path, monkeypatch):
        case = W8.W8SharedChunkPlanTests("test_node_between_the_two_gates_is_single_chunk_on_both")
        case.root = tmp_path
        case._env = {k: __import__("os").environ.get(k) for k in W8._ENV_KEYS}
        yield case
        case._restore()

    def _sync(self, case, rel, body, server=None):
        path = case._write_node(rel, "Split Node", body)
        mod = W8._load_sync_module(case.root)
        server = server or _RefusingSyncServer()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = mod.sync_node(server, path)
        assert bool(outcome), buf.getvalue()[-1500:]
        return mod, server, path, buf.getvalue()

    def test_kg_sync_splits_a_refused_chunk_into_rows_and_loses_nothing(self, harness):
        rel = "knowledge/concepts/split_node.md"
        mod, server, path, out = self._sync(harness, rel, _dense_body())
        rows = W8._plan_rows(server.client.collections._store_for(W8.PROJECT_KG), rel)
        final_text = path.read_text(encoding="utf-8")
        assert len(final_text) > REFUSE_ABOVE_CHARS
        assert len(rows) > 1
        assert "".join(r["content"] for r in rows) == final_text
        assert [r["chunk_num"] for r in rows] == list(range(1, len(rows) + 1))
        assert {r["total_chunks"] for r in rows} == {len(rows)}
        assert all(len(r["content"]) <= REFUSE_ABOVE_CHARS for r in rows)
        assert "refused 1 chunk(s) whole" in out

    def test_second_sync_embeds_nothing(self, harness):
        rel = "knowledge/concepts/split_again.md"
        mod, server, path, _out = self._sync(harness, rel, _dense_body())
        first = list(server.embedded)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = mod.sync_node(server, path)
        assert bool(outcome)
        assert server.embedded == first, "a second sync must re-embed nothing"
        assert "Embed-skip" in buf.getvalue()

    def test_a_row_recorded_as_cut_is_re_embedded_once(self, harness):
        """A row an earlier version stored with a leading-window ACTIVE vector
        (its truncation record names the active slot) is not current: the
        next sync re-embeds it, and the sync after that skips."""

        class _OldShrinkServer(W8._SyncServer):
            def _get_all_kg_embeddings_tagged(self, text):
                return {self.text_vector_slot: [0.1, 0.2]}, [self.text_vector_slot]

        rel = "knowledge/concepts/was_cut.md"
        mod, _old, path, _out = self._sync(harness, rel, "short prose. " * 40,
                                           server=_OldShrinkServer())
        server = _RefusingSyncServer()
        server.client = _old.client  # same store, new embedder
        for expect_embeds, phrase in ((1, "covers only part of its text"), (1, "Embed-skip")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                assert bool(mod.sync_node(server, path))
            assert len(server.embedded) == expect_embeds, buf.getvalue()[-800:]
            assert phrase in buf.getvalue()

    @staticmethod
    def _writing(models, client=None):
        """A refusing sync server whose service WRITES exactly ``models``."""
        import types as _t

        server = _RefusingSyncServer()
        server.embedding_service = _t.SimpleNamespace(
            text_model_id="qwen3-embedding:0.6b",
            configured_text_write_models=lambda: dict(models),
        )
        if client is not None:
            server.client = client
        return server

    def test_a_secondary_recorded_as_cut_is_re_embedded_only_when_written(
        self, harness, monkeypatch,
    ):
        """v0.2.105 and before recorded a secondary's leading window in the
        row's truncation record. A sync that WRITES that slot re-lays the
        node once; a sync that does not write it must not re-embed for it
        (it would never clear: an RL slot regen re-adds such a record)."""
        import types as _t

        class _OldArcticCut(W8._SyncServer):
            def _get_all_kg_embeddings_tagged(self, text):
                return ({self.text_vector_slot: [0.1, 0.2], "arctic2_embed": [0.3, 0.4]},
                        ["arctic2_embed"])

        rel = "knowledge/concepts/arctic_was_cut.md"
        mod, old, path, _out = self._sync(harness, rel, "short prose. " * 40,
                                          server=_OldArcticCut())
        monkeypatch.setattr(mod._kg_embed_model, "resolve_kg_model",
                            lambda *a, **k: _t.SimpleNamespace(carry=None))
        qwen_only = self._writing({"qwen3_embed": "qwen3-embedding:0.6b"}, old.client)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert bool(mod.sync_node(qwen_only, path))
        assert qwen_only.embedded == [] and "Embed-skip" in buf.getvalue()

        dual = self._writing({"qwen3_embed": "qwen3-embedding:0.6b",
                              "arctic2_embed": "snowflake-arctic-embed2:latest"}, old.client)
        for expect, phrase in ((1, "arctic2_embed vector covers only part"), (1, "Embed-skip")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                assert bool(mod.sync_node(dual, path))
            assert len(dual.embedded) == expect, buf.getvalue()[-800:]
            assert phrase in buf.getvalue()

    def test_crossing_re_lays_a_possibly_averaged_secondary_once(self, harness, monkeypatch):
        """The first split-on-refusal build AVERAGED a refusing secondary's
        pieces and recorded nothing. The chunker-revision crossing (bumped to
        v0.2.106.1 for exactly this) re-lays a node holding such a slot when
        its model may have refused one of the node's chunks — and only then."""
        import types as _t

        from weaviate_mcp.chunking import _CHUNKER_REVISION

        assert _CHUNKER_REVISION == "v0.2.106.1"

        class _OldAveraged(W8._SyncServer):
            def _get_all_kg_embeddings_tagged(self, text):
                return {self.text_vector_slot: [0.1, 0.2], "arctic2_embed": [0.3, 0.4]}, []

        big_rel, small_rel = "knowledge/concepts/maybe_avg.md", "knowledge/concepts/fits.md"
        mod, old, big_path, _o = self._sync(
            harness, big_rel, "lorem ipsum dolor sit amet. " * 300, server=_OldAveraged())
        same_store = _OldAveraged()
        same_store.client = old.client
        _m2, _o2, small_path, _o3 = self._sync(
            harness, small_rel, "short prose. " * 20, server=same_store)
        monkeypatch.setattr(mod._kg_embed_model, "resolve_kg_model",
                            lambda *a, **k: _t.SimpleNamespace(carry=None))
        dual = self._writing({"qwen3_embed": "qwen3-embedding:0.6b",
                              "arctic2_embed": "snowflake-arctic-embed2:latest"}, old.client)
        # Not crossing: unrecorded vectors are trusted (no blanket re-embed).
        with contextlib.redirect_stdout(io.StringIO()):
            assert bool(mod.sync_node(dual, big_path))
        assert dual.embedded == []
        monkeypatch.setattr(mod, "_chunker_resync_pending", lambda: True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert bool(mod.sync_node(dual, big_path))
            assert bool(mod.sync_node(dual, small_path))
        assert len(dual.embedded) == 1, buf.getvalue()[-1200:]
        assert "may be an average of split pieces" in buf.getvalue()

    def test_stored_split_rows_count_as_the_current_plan(self, harness):
        rel = "knowledge/concepts/split_plan.md"
        mod, server, path, _out = self._sync(harness, rel, _dense_body())
        coll = server.client.collections.get(W8.PROJECT_KG)
        rows = W8._plan_rows(server.client.collections._store_for(W8.PROJECT_KG), rel)
        content = path.read_text(encoding="utf-8")
        assert mod._stored_plan_matches_current(server, coll, rel, content, len(rows))

    def test_mcp_store_writes_the_same_rows_as_kg_sync(self, harness, monkeypatch):
        rel = "knowledge/concepts/split_parity.md"
        mod, server, path, _out = self._sync(harness, rel, _dense_body())
        sync_rows = W8._plan_rows(server.client.collections._store_for(W8.PROJECT_KG), rel)
        srv = W8._mcp_server()

        mcp_store: dict = {}
        final_text = path.read_text(encoding="utf-8")

        async def _tagged(text):
            if len(text) > REFUSE_ABOVE_CHARS:
                raise ES.ChunkOverflowError("qwen3-embedding:0.6b", len(text), "exceeds the context length")
            return {"qwen3_embed": _vec_for(text)[:2]}, []

        result = W8._mcp_store(
            monkeypatch, harness.root / "_mcp", mcp_store,
            title="Split Node", file_path=rel, content=final_text,
        )
        assert result["success"]
        # Re-run with the refusing embedder (the W8 helper installs a
        # non-refusing one; patch after it and store again).
        monkeypatch.setattr(srv, "_get_all_kg_embeddings_tagged", _tagged)
        fn = W8._unwrap(srv.store_knowledge_node)
        result = json.loads(asyncio.run(fn(
            title="Split Node", content=final_text, node_type="concept",
            tags=["w8"], links=[], file_path=rel, scope="project",
        )))
        assert result["success"], result
        mcp_rows = W8._plan_rows(mcp_store, rel)
        assert W8._shape(mcp_rows) == W8._shape(sync_rows)
        assert result["chunks_stored"] == len(sync_rows)

    def test_mcp_store_failing_embed_leaves_old_rows_intact(self, harness, monkeypatch):
        rel = "knowledge/concepts/keep_old.md"
        store: dict = {}
        result = W8._mcp_store(
            monkeypatch, harness.root / "_mcp", store,
            title="Keep Old", file_path=rel, content="old body " * 50,
        )
        assert result["success"]
        before = {uid: dict(o.properties) for uid, o in store.items()}
        assert before
        srv = W8._mcp_server()

        async def _down(text):  # noqa: ARG001
            raise ConnectionError("embedding backend down")

        async def _plain_down(text):  # noqa: ARG001
            raise ConnectionError("embedding backend down")

        monkeypatch.setattr(srv, "_get_all_kg_embeddings_tagged", _down)
        monkeypatch.setattr(srv, "get_embedding", _plain_down)
        fn = W8._unwrap(srv.store_knowledge_node)
        raw = asyncio.run(fn(
            title="Keep Old", content="new body " * 50, node_type="concept",
            tags=["w8"], links=[], file_path=rel, scope="project",
        ))
        assert json.loads(raw).get("success") is not True
        after = {uid: dict(o.properties) for uid, o in store.items()}
        assert after == before, "a failed embed must leave the old rows untouched"

    def test_mcp_store_unsplittable_piece_fails_and_keeps_old_rows(self, harness, monkeypatch):
        rel = "knowledge/concepts/keep_old2.md"
        store: dict = {}
        W8._mcp_store(
            monkeypatch, harness.root / "_mcp", store,
            title="Keep Old2", file_path=rel, content="old body " * 50,
        )
        before = {uid: dict(o.properties) for uid, o in store.items()}
        srv = W8._mcp_server()

        async def _always(text):
            raise ES.ChunkOverflowError("m", len(text), "exceeds the context length")

        monkeypatch.setattr(srv, "_get_all_kg_embeddings_tagged", _always)
        fn = W8._unwrap(srv.store_knowledge_node)
        raw = asyncio.run(fn(
            title="Keep Old2", content="new body " * 400, node_type="concept",
            tags=["w8"], links=[], file_path=rel, scope="project",
        ))
        assert json.loads(raw).get("success") is not True
        assert {uid: dict(o.properties) for uid, o in store.items()} == before


# ---------------------------------------------------------------------------
# Shipped sidecar: builder + ingest round trip
# ---------------------------------------------------------------------------


class TestSidecarRoundTrip:
    def test_builder_ships_split_rows_and_ingest_rebuilds_them(self, tmp_path, monkeypatch):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_dsplit_builder", REPO_ROOT / "scripts" / "build_shipped_kg_embeddings.py"
        )
        gen = importlib.util.module_from_spec(spec)
        sys.modules["_dsplit_builder"] = gen
        spec.loader.exec_module(gen)

        node = tmp_path / "concepts" / "dense.md"
        node.parent.mkdir(parents=True)
        node.write_text(
            "---\ntitle: Dense\ntype: concept\ntags: [x]\nstatus: active\n---\n"
            + _dense_body() + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(gen, "TEMPLATES_KNOWLEDGE", tmp_path)

        def embed(text):
            if len(text) > REFUSE_ABOVE_CHARS:
                raise _overflow_exc(len(text))
            return _vec_for(text)

        data = gen.build_slot_data("qwen3_embed", "qwen3-embedding:0.6b", embed, 4,
                                   progress=False)
        (entry,) = data["nodes"].values()
        assert entry["splits"]
        text = node.read_text(encoding="utf-8")
        rows = gen.expected_rows(text, "qwen3-embedding:0.6b", entry)
        assert rows is not None and "".join(rows) == text
        assert entry["total_chunks"] == len(rows)
        for c, row in zip(entry["chunks"], rows):
            assert c["vector"] == [gen._round7(x) for x in _vec_for(row)]

    def test_secondary_with_a_different_layout_is_not_merged(self, tmp_path, monkeypatch):
        import os

        saved = {k: os.environ.get(k) for k in W8._ENV_KEYS}

        def _restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        try:
            self._layout_case(tmp_path)
        finally:
            _restore()

    def _layout_case(self, tmp_path):
        mod = W8._load_sync_module(tmp_path)
        sig = "a" * 64
        plan = ["x" * 40]
        for slot, splits in (("qwen3_embed", {"1": [20, 20]}), ("arctic2_embed", {"1": [10, 30]})):
            (tmp_path / f".node_embeddings.{slot}.json").write_text(json.dumps({
                "schema_version": 1, "slot": slot, "model_id": "m", "dim": 2,
                "chunker_revision": mod._current_chunker_revision(),
                "nodes": {sig: {"total_chunks": 2, "splits": splits, "chunks": [
                    {"chunk_num": 1, "vector": [1.0, 0.0]},
                    {"chunk_num": 2, "vector": [0.0, 1.0]},
                ]}},
            }), encoding="utf-8")
        mod._SHIPPED_EMBED_CACHE.clear()

        class _S:
            text_vector_slot = "qwen3_embed"

        rows = mod._shipped_rows_for(_S(), tmp_path, sig, plan)
        assert [r[0] for r in rows] == ["x" * 20, "x" * 20]
        assert all(set(r[2]) == {"qwen3_embed"} for r in rows)  # arctic not merged
        # A layout that does not fit the plan → compute locally.
        assert mod._shipped_rows_for(_S(), tmp_path, sig, ["y" * 41]) is None
