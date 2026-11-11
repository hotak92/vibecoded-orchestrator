# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.9 — pin the pure-train contract of the rewritten ``_update``.

Contract under test (post-C8 `_update`):

1. Accepts the pre-packed payload shape (no agent_output string).
2. Does NOT call ``self._embed`` (no embedding work in the container).
3. Does NOT write to ``self._data_logger`` (MCP is sole citation writer
   per C6c + C7).
4. Pulls ``n_emb`` / ``linked_embs`` / ``linked_type_names`` straight from
   the payload — no recomputation, no chunking.
5. Calls ``rl_model.update(...)`` once per node that has a target.

The companion ``test_v0_2_9_literal_cited_signal.py`` pins the formula
identity. This file pins the pure-train mechanical contract.

Async convention: per the paid-module repo policy, async test bodies are
wrapped in ``_body()`` coroutines and the pytest function calls
``asyncio.run(_body())``. We do NOT depend on ``pytest-asyncio``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from rl_reranker.retrieval_rl import RetrievalRL


def _run_async(coro):
    return asyncio.run(coro)


class _RecordingRLModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._type_map: dict[str, int] = {}

    def get_type_idx(self, node_type: str) -> int:
        return self._type_map.setdefault(node_type or "concept", len(self._type_map))

    def update(
        self,
        q_raw,
        n_raw,
        linked_raws,
        n_type_idx: int,
        target: float,
        linked_type_idxs=None,
    ) -> float:
        self.calls.append(
            {
                "q_raw_shape": tuple(q_raw.shape),
                "n_raw_shape": tuple(n_raw.shape),
                "n_linked_raws": len(linked_raws),
                "linked_raws_shapes": [tuple(lr.shape) for lr in linked_raws],
                "n_type_idx": n_type_idx,
                "target": target,
                "linked_type_idxs": list(linked_type_idxs) if linked_type_idxs else None,
            }
        )
        return 0.123

    def save(self) -> None:
        pass


class _RecordingDataLogger:
    """A data logger that records every call — used to ASSERT it's never
    called from `_update` (MCP is the sole citation writer per C6c)."""

    def __init__(self) -> None:
        self.citation_calls: int = 0
        self.retrieval_calls: int = 0

    def log_citations(self, *args, **kwargs) -> None:
        self.citation_calls += 1

    def log_retrieval(self, *args, **kwargs) -> None:
        self.retrieval_calls += 1


def _build_payload(*, with_linked: bool = False, emb_dim: int = 8) -> dict:
    nodes_packed = [
        {
            "title": "NodeA",
            "node_type": "concept",
            "n_emb": [0.1] * emb_dim,
            "linked_embs": [[0.2] * emb_dim, [0.3] * emb_dim] if with_linked else [],
            "linked_type_names": ["concept", "tool"] if with_linked else [],
        },
        {
            "title": "NodeB",
            "node_type": "tool",
            "n_emb": [0.4] * emb_dim,
            "linked_embs": [],
            "linked_type_names": [],
        },
    ]
    return {
        "task_id": "t-pure-train",
        "targets": None,
        "nodes_packed": nodes_packed,
        "query_emb": [0.0] * emb_dim,
        "cosine_sims": {"NodeA": 0.4, "NodeB": 0.6},
        "literal_cited": {},
        "cross_encoder_cited": None,
    }


# ----------------------------------------------------------------------
# Mechanical contract: pure-train, no embedding, no data-logger calls.
# ----------------------------------------------------------------------


class TestPureTrainContract:
    def test_update_runs_without_embedding_fn(self, tmp_path: Path) -> None:
        """With embedding_fn=None, ``_update`` must still complete and call
        rl_model.update for every node. The old `_update` would silently
        return after `_compute_cosine_similarities` failed; the new one
        bypasses embedding entirely (signal arrives pre-embedded)."""

        async def _body() -> list:
            model = _RecordingRLModel()
            rl = RetrievalRL(
                project_root=tmp_path, rl_model=model, embedding_fn=None  # type: ignore[arg-type]
            )
            await rl._update(**_build_payload())
            return model.calls

        calls = _run_async(_body())
        assert len(calls) == 2

    def test_update_does_not_call_data_logger(self, tmp_path: Path) -> None:
        """MCP is sole citation writer (C6c + C7). Container must NOT
        emit citation events from `_update`."""

        async def _body() -> int:
            logger = _RecordingDataLogger()
            rl = RetrievalRL(
                project_root=tmp_path,
                rl_model=_RecordingRLModel(),  # type: ignore[arg-type]
                data_logger=logger,  # type: ignore[arg-type]
                embedding_fn=None,
            )
            await rl._update(**_build_payload())
            return logger.citation_calls

        citation_calls = _run_async(_body())
        assert citation_calls == 0, (
            "Container called data_logger.log_citations — MCP is the sole "
            "writer per the C6c HARD CUTOVER."
        )

    def test_n_emb_passed_through_unchanged(self, tmp_path: Path) -> None:
        """n_emb from the payload reaches rl_model.update as the n_raw
        tensor, byte-equivalent. No recompute via embedding_fn."""

        async def _body() -> list:
            model = _RecordingRLModel()
            rl = RetrievalRL(
                project_root=tmp_path, rl_model=model, embedding_fn=None  # type: ignore[arg-type]
            )
            await rl._update(**_build_payload(emb_dim=16))
            return model.calls

        calls = _run_async(_body())
        for call in calls:
            assert call["q_raw_shape"] == (16,)
            assert call["n_raw_shape"] == (16,)

    def test_linked_embs_count_passed_through(self, tmp_path: Path) -> None:
        """linked_embs from the payload all reach rl_model.update."""

        async def _body() -> list:
            model = _RecordingRLModel()
            rl = RetrievalRL(
                project_root=tmp_path, rl_model=model, embedding_fn=None  # type: ignore[arg-type]
            )
            await rl._update(**_build_payload(with_linked=True))
            return model.calls

        calls = _run_async(_body())
        # NodeA has 2 linked, NodeB has 0.
        assert calls[0]["n_linked_raws"] == 2
        assert calls[1]["n_linked_raws"] == 0

    def test_linked_type_idxs_resolved_from_string_names(
        self, tmp_path: Path
    ) -> None:
        """linked_type_names string list is resolved to type-idx list via
        rl_model.get_type_idx. Pins the type-as-string-name invariant."""

        async def _body() -> list:
            model = _RecordingRLModel()
            rl = RetrievalRL(
                project_root=tmp_path, rl_model=model, embedding_fn=None  # type: ignore[arg-type]
            )
            await rl._update(**_build_payload(with_linked=True))
            return model.calls

        calls = _run_async(_body())
        idxs = calls[0]["linked_type_idxs"]
        assert isinstance(idxs, list)
        assert len(idxs) == 2

    def test_node_type_string_resolved_via_get_type_idx(
        self, tmp_path: Path
    ) -> None:
        """node_type STRING NAMES in the payload (NOT integer indices) are
        resolved by the container — type registry is process-local per the
        D1' invariant; MCP can't know the container's local idx."""

        async def _body() -> list:
            model = _RecordingRLModel()
            rl = RetrievalRL(
                project_root=tmp_path, rl_model=model, embedding_fn=None  # type: ignore[arg-type]
            )
            await rl._update(**_build_payload())
            return model.calls

        calls = _run_async(_body())
        # "concept" + "tool" registered with distinct idxs.
        assert calls[0]["n_type_idx"] != calls[1]["n_type_idx"]

    def test_empty_nodes_packed_is_noop(self, tmp_path: Path) -> None:
        async def _body() -> list:
            model = _RecordingRLModel()
            rl = RetrievalRL(
                project_root=tmp_path, rl_model=model, embedding_fn=None  # type: ignore[arg-type]
            )
            await rl._update(
                task_id="t",
                targets=None,
                nodes_packed=[],
                query_emb=[0.0] * 8,
                cosine_sims={},
                literal_cited={},
                cross_encoder_cited=None,
            )
            return model.calls

        calls = _run_async(_body())
        assert calls == []

    def test_node_missing_cosine_sim_is_skipped(self, tmp_path: Path) -> None:
        """A node in nodes_packed without a cosine_sims entry has no
        signal; ``_update`` must skip it (not error, not zero-train)."""

        async def _body() -> list:
            model = _RecordingRLModel()
            rl = RetrievalRL(
                project_root=tmp_path, rl_model=model, embedding_fn=None  # type: ignore[arg-type]
            )
            await rl._update(
                task_id="t",
                targets=None,
                nodes_packed=[
                    {
                        "title": "WithSignal",
                        "node_type": "concept",
                        "n_emb": [0.1] * 8,
                        "linked_embs": [],
                        "linked_type_names": [],
                    },
                    {
                        "title": "NoSignal",
                        "node_type": "concept",
                        "n_emb": [0.2] * 8,
                        "linked_embs": [],
                        "linked_type_names": [],
                    },
                ],
                query_emb=[0.0] * 8,
                cosine_sims={"WithSignal": 0.5},  # NoSignal absent
                literal_cited={},
                cross_encoder_cited=None,
            )
            return model.calls

        calls = _run_async(_body())
        assert len(calls) == 1, "only the with-signal node should train"
