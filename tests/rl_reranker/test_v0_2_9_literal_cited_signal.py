# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.9 — pin literal_cited as a TRAINING SIGNAL for the cos3heads NN.

User directive 2026-06-04: "literal citation as a training signal for our
own reranking neural network is for this patch". This test is the
load-bearing guard against regressions where a future refactor accidentally
drops ``literal_cited`` from the path that reaches ``rl_model.update()``.

Contract under test (post-C8 `_update`):

    Given identical cosine_sims, two calls to `_update` with different
    `literal_cited` dicts MUST produce different `target` values passed to
    `rl_model.update(...)`. Specifically, a node with `literal_cited=True`
    must see a HIGHER target than the same node with `literal_cited=False`,
    by exactly the unified-target bonus (multiplicative *1.5 then additive
    +0.1, both clamped to 1.0).

If the container ever logs `literal_cited` but doesn't feed it through
``compute_unified_targets`` before the gradient step, this test fails.

Async convention: per the paid-module repo policy, async test bodies are
wrapped in ``_body()`` coroutines and the pytest function calls
``asyncio.run(_body())``. We do NOT depend on ``pytest-asyncio``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from rl_reranker.retrieval_rl import RetrievalRL
from vco_lib.rl_training_targets import compute_unified_targets


def _run_async(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# Test fixture — a RetrievalRL with a mocked rl_model that captures every
# call to ``update`` so we can inspect the ``target`` arg.
# ----------------------------------------------------------------------


class _TargetCapturingRLModel:
    """Mock RLModel that records each ``update`` call's target value."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        # Type-registry stub mimicking RLModel.get_type_idx.
        self._type_map = {"concept": 0, "tool": 1, "project": 2}

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
                "n_type_idx": n_type_idx,
                "target": target,
                "linked_type_idxs": list(linked_type_idxs) if linked_type_idxs else None,
                "n_linked": len(linked_raws),
            }
        )
        return 0.0  # fake loss

    def save(self) -> None:  # noqa: D401 — RetrievalRL._update calls this
        pass


def _build_rl(tmp_path: Path) -> tuple[RetrievalRL, _TargetCapturingRLModel]:
    """Construct a RetrievalRL with a mock rl_model + no data logger."""
    fake_model = _TargetCapturingRLModel()
    rl = RetrievalRL(
        project_root=tmp_path,
        rl_model=fake_model,  # type: ignore[arg-type]
        data_logger=None,
    )
    return rl, fake_model


def _build_payload(
    titles: list[str],
    *,
    cosine_sims: dict[str, float],
    literal_cited: dict[str, bool],
    cross_encoder_cited: "dict[str, bool] | None" = None,
    emb_dim: int = 8,
) -> dict:
    """Build a v0.2.9 pre-packed task payload (same shape MCP will send)."""
    nodes_packed: list[dict] = []
    for title in titles:
        nodes_packed.append(
            {
                "title": title,
                "node_type": "concept",
                "n_emb": [0.1] * emb_dim,
                "linked_embs": [],
                "linked_type_names": [],
            }
        )
    return {
        "task_id": "t-literal-cited",
        "targets": None,  # MCP must NOT send pre-computed targets (we re-derive)
        "nodes_packed": nodes_packed,
        "query_emb": [0.0] * emb_dim,
        "cosine_sims": cosine_sims,
        "literal_cited": literal_cited,
        "cross_encoder_cited": cross_encoder_cited,
    }


# ----------------------------------------------------------------------
# 1. The core load-bearing property: literal_cited changes the NN target.
# ----------------------------------------------------------------------


class TestLiteralCitedAsTrainingSignal:
    def test_literal_cited_true_yields_higher_target_than_false(
        self, tmp_path: Path
    ) -> None:
        """Given identical cosine, literal_cited=True MUST send a HIGHER
        target to ``rl_model.update`` than literal_cited=False.

        Quantitative anchor: with a single-node payload at cos=0.40, the
        unified-target formula yields target=0.45 for cited=False (cos +
        beta_z * 0.5 = 0.40 + 0.05). For cited=True: 0.45*1.5=0.675, then
        +0.1=0.775. So the bonus delta is 0.775 - 0.45 = 0.325.
        """

        async def _body() -> tuple[float, float]:
            rl_a, model_a = _build_rl(tmp_path)
            rl_b, model_b = _build_rl(tmp_path)

            payload_false = _build_payload(
                ["NodeA"],
                cosine_sims={"NodeA": 0.40},
                literal_cited={"NodeA": False},
            )
            payload_true = _build_payload(
                ["NodeA"],
                cosine_sims={"NodeA": 0.40},
                literal_cited={"NodeA": True},
            )

            await rl_a._update(**payload_false)
            await rl_b._update(**payload_true)

            assert len(model_a.calls) == 1
            assert len(model_b.calls) == 1
            return model_a.calls[0]["target"], model_b.calls[0]["target"]

        target_false, target_true = _run_async(_body())
        assert target_true > target_false, (
            f"literal_cited=True target ({target_true}) must exceed "
            f"literal_cited=False target ({target_false}). If they're "
            f"equal, the bonus never reaches the NN."
        )

    def test_target_matches_compute_unified_targets_exactly(
        self, tmp_path: Path
    ) -> None:
        """The target passed to ``rl_model.update`` must equal
        ``compute_unified_targets(cosine_sims, literal_cited, ...)``'s
        output for that title."""

        async def _body() -> list[dict[str, Any]]:
            rl, model = _build_rl(tmp_path)
            cosine_sims = {"NodeA": 0.30, "NodeB": 0.70}
            literal_cited = {"NodeA": True, "NodeB": False}
            payload = _build_payload(
                ["NodeA", "NodeB"],
                cosine_sims=cosine_sims,
                literal_cited=literal_cited,
            )
            await rl._update(**payload)
            return model.calls

        calls = _run_async(_body())
        cosine_sims = {"NodeA": 0.30, "NodeB": 0.70}
        literal_cited = {"NodeA": True, "NodeB": False}
        expected, _ = compute_unified_targets(cosine_sims, literal_cited, None)
        for title, call in zip(["NodeA", "NodeB"], calls):
            assert call["target"] == pytest.approx(expected[title], abs=1e-6)

    def test_cross_encoder_cited_also_boosts(self, tmp_path: Path) -> None:
        """Cross_encoder_cited composes correctly when supplied (still None
        in v0.2.9 production, but the field is in the schema and the formula
        must handle it). Regression guard for v0.2.10+."""

        async def _body() -> float:
            rl, model = _build_rl(tmp_path)
            cosine_sims = {"X": 0.30}
            literal_cited = {"X": True}
            cross_encoder_cited = {"X": True}
            payload = _build_payload(
                ["X"],
                cosine_sims=cosine_sims,
                literal_cited=literal_cited,
                cross_encoder_cited=cross_encoder_cited,
            )
            await rl._update(**payload)
            return model.calls[0]["target"]

        target = _run_async(_body())
        cosine_sims = {"X": 0.30}
        expected, _ = compute_unified_targets(
            cosine_sims,
            literal_cited={"X": True},
            cross_encoder_cited={"X": True},
        )
        assert target == pytest.approx(expected["X"], abs=1e-6)
        # 0.30 single-node -> step1: 0.30 + 0.1*0.5 = 0.35
        # literal: 0.35*1.5=0.525, +0.1=0.625
        # cross-enc: 0.625*1.5=0.9375, +0.1=1.0 (clamped)
        assert target == pytest.approx(1.0, abs=1e-6)


# ----------------------------------------------------------------------
# 2. Default-to-False semantics: missing literal_cited entries = no boost.
# ----------------------------------------------------------------------


class TestLiteralCitedDefaultsToFalse:
    def test_node_missing_from_literal_cited_dict_gets_no_boost(
        self, tmp_path: Path
    ) -> None:
        """A node title absent from literal_cited dict is treated as False
        (per compute_unified_targets's missing-entry semantics)."""

        async def _body() -> list[dict[str, Any]]:
            rl, model = _build_rl(tmp_path)
            cosine_sims = {"NodeA": 0.50, "NodeB": 0.50}
            literal_cited = {"NodeA": True}  # NodeB missing
            payload = _build_payload(
                ["NodeA", "NodeB"],
                cosine_sims=cosine_sims,
                literal_cited=literal_cited,
            )
            await rl._update(**payload)
            return model.calls

        calls = _run_async(_body())
        targets_by_title = {
            "NodeA": calls[0]["target"],
            "NodeB": calls[1]["target"],
        }
        cosine_sims = {"NodeA": 0.50, "NodeB": 0.50}
        expected, _ = compute_unified_targets(
            cosine_sims, {"NodeA": True}, None
        )
        assert targets_by_title["NodeA"] > targets_by_title["NodeB"]
        assert targets_by_title["NodeA"] == pytest.approx(expected["NodeA"], abs=1e-6)
        assert targets_by_title["NodeB"] == pytest.approx(expected["NodeB"], abs=1e-6)

    def test_empty_literal_cited_equals_no_boost(self, tmp_path: Path) -> None:
        """Empty literal_cited dict produces cosine-only targets (= what
        v2 events without the field will replay as)."""

        async def _body() -> tuple[list, list]:
            rl_a, model_a = _build_rl(tmp_path)
            rl_b, model_b = _build_rl(tmp_path)
            cosine_sims = {"NodeA": 0.40, "NodeB": 0.70}
            await rl_a._update(
                **_build_payload(
                    ["NodeA", "NodeB"],
                    cosine_sims=cosine_sims,
                    literal_cited={},
                )
            )
            await rl_b._update(
                **_build_payload(
                    ["NodeA", "NodeB"],
                    cosine_sims=cosine_sims,
                    literal_cited={"NodeA": False, "NodeB": False},
                )
            )
            return model_a.calls, model_b.calls

        a_calls, b_calls = _run_async(_body())
        for ca, cb in zip(a_calls, b_calls):
            assert ca["target"] == pytest.approx(cb["target"], abs=1e-9)


# ----------------------------------------------------------------------
# 3. Skip-train flag still respected.
# ----------------------------------------------------------------------


class TestSkipTrainStillRespected:
    def test_skip_train_true_does_not_call_update(self, tmp_path: Path) -> None:
        """rl_online_training_disabled flag is still honored — payload
        accepted but ``rl_model.update`` NOT called."""

        async def _body() -> list:
            rl, model = _build_rl(tmp_path)
            payload = _build_payload(
                ["NodeA"],
                cosine_sims={"NodeA": 0.5},
                literal_cited={"NodeA": True},
            )
            await rl._update(skip_train=True, **payload)
            return model.calls

        calls = _run_async(_body())
        assert calls == []
