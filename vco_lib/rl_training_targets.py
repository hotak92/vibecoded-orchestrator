# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unified RL training-target computation — single source of truth.

Used by THREE paths that must agree numerically:

1. **MCP-side citation detection** (`claude_mcp_servers/weaviate_mcp/server.py::_rl_answer_monitor`)
   — writes the citation event for all tiers (free + Pro) directly to JSONL.

2. **Container-side online training** (`paid-modules/vct-rl-reranker/retrieval_rl.py::_update`)
   — consumes the same inputs to derive training targets for the online gradient step.

3. **Container-side offline training** (`paid-modules/vct-rl-reranker/offline_trainer.py`)
   — replays historical JSONL events through the same formula at batch-training time.

History — pre-v0.2.9, each path had its own subtly different formula
(query-name boost on the query text, missed-citation penalty, signed-cosine
collapse for cross-encoder verdict). The drift made offline retraining
diverge from online learning and made Pro-vs-free training data
incompatible. v0.2.9 collapses all three onto this one function. See
`knowledge/concepts/rl-arch-comparison-pitfalls-2026-05-22.md` for the
audit + rationale.

Pure function: no I/O, no async, no globals. Reproducible by construction.
"""

from __future__ import annotations

__all__ = ["compute_unified_targets"]


def compute_unified_targets(
    cosine_sims: dict[str, float],
    literal_cited: dict[str, bool] | None = None,
    cross_encoder_cited: dict[str, bool] | None = None,
    *,
    beta_z: float = 0.1,
    bonus_mult: float = 1.5,
    bonus_add: float = 0.1,
    binary_threshold: float = 0.6,
) -> tuple[dict[str, float], dict[str, bool]]:
    """Compute per-node BCE training targets + binary citation labels.

    Formula (4 steps, applied per node):

        # Step 1: base cosine + small z-score component
        z_i        = clip((cos_i - mean) / max(std, 0.05), -1, +1)
        z_mapped_i = 0.5 + z_i / 2                            # [-1, +1] -> [0, 1]
        target_i   = clamp(cos_i + beta_z * z_mapped_i, 0, 1)

        # Step 2: literal-citation bonus (multiplicative + additive)
        if literal_cited_i:
            target_i = min(1, target_i * bonus_mult)
            target_i = min(1, target_i + bonus_add)

        # Step 3: cross-encoder-cited bonus (same shape as step 2)
        if cross_encoder_cited_i:
            target_i = min(1, target_i * bonus_mult)
            target_i = min(1, target_i + bonus_add)

        # Step 4: binary label
        cited_i = target_i > binary_threshold

    Args:
        cosine_sims: ``{node_title: raw_cosine}`` — max-over-chunks cosine
            similarity between the agent's ANSWER and each candidate node's
            existing Weaviate embedding. RAW values, no bonuses pre-applied.
        literal_cited: ``{node_title: bool}`` — True iff the node's identity
            (title / slug / wikilink / file_path) appears in the ANSWER text
            via word-boundary regex. Missing entries default to False.
        cross_encoder_cited: ``{node_title: bool}`` — Pro-tier cross-encoder
            verdict. None or empty when no reranker ran (free tier OR Pro
            without reranker enabled). Missing entries default to False.
        beta_z: weight for the z-score relative-differentiation term.
        bonus_mult: multiplicative boost factor per active bonus flag.
        bonus_add: additive boost per active bonus flag.
        binary_threshold: target > threshold -> cited=True.

    Returns:
        ``(targets, cited)`` — both keyed by node_title.

        - ``targets[t]``: float in [0, 1], ready as a BCE training target.
        - ``cited[t]``: bool, derived ``targets[t] > binary_threshold``.

    Examples:
        >>> # Pure cosine, no boosts
        >>> t, c = compute_unified_targets({"A": 0.3, "B": 0.7})
        >>> round(t["A"], 3), round(t["B"], 3)
        (0.3, 0.8)
        >>> c["A"], c["B"]
        (False, True)

        >>> # Literal-cited lifts mid-range cosine past the binary threshold
        >>> t, c = compute_unified_targets({"X": 0.3}, literal_cited={"X": True})
        >>> round(t["X"], 3) > 0.6
        True
        >>> c["X"]
        True

        >>> # Both bonuses saturate to 1.0 from a mid-range start
        >>> t, _ = compute_unified_targets(
        ...     {"X": 0.7},
        ...     literal_cited={"X": True},
        ...     cross_encoder_cited={"X": True},
        ... )
        >>> t["X"]
        1.0

        >>> # Empty input
        >>> compute_unified_targets({})
        ({}, {})
    """
    if not cosine_sims:
        return {}, {}

    literal_cited = literal_cited or {}
    cross_encoder_cited = cross_encoder_cited or {}

    vals = list(cosine_sims.values())
    mean_val = sum(vals) / len(vals)
    variance = sum((v - mean_val) ** 2 for v in vals) / len(vals)
    std_val = variance ** 0.5
    denom = max(std_val, 0.05)

    targets: dict[str, float] = {}
    cited: dict[str, bool] = {}

    for title, cosine in cosine_sims.items():
        # Step 1: base + z-score
        z = max(-1.0, min(1.0, (cosine - mean_val) / denom))
        z_mapped = 0.5 + z / 2.0
        target = max(0.0, min(1.0, cosine + beta_z * z_mapped))

        # Step 2: literal-citation bonus
        if literal_cited.get(title, False):
            target = min(1.0, target * bonus_mult)
            target = min(1.0, target + bonus_add)

        # Step 3: cross-encoder-cited bonus
        if cross_encoder_cited.get(title, False):
            target = min(1.0, target * bonus_mult)
            target = min(1.0, target + bonus_add)

        targets[title] = target
        cited[title] = target > binary_threshold

    return targets, cited
