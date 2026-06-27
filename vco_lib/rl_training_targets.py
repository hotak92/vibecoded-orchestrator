# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# This file is VENDORED into paid-modules/vct-rl-reranker/_training_targets.py
# Both copies MUST stay byte-identical. The byte-identity test in
# tests/test_vendored_file_sync.py enforces this on every CI run.
# To re-sync after an edit: ./scripts/sync-vendored-files.sh
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

        >>> # Literal-cited node with NO cosine entry (no n_emb available):
        >>> # its base cosine defaults to the missing-cosine floor (0.4 when no
        >>> # cosine population exists), then the literal bonus lifts it past the
        >>> # binary threshold -> cited. base = 0.4 + 0.1*0.5 = 0.45;
        >>> # literal: 0.45*1.5 = 0.675, +0.1 = 0.775.
        >>> t, c = compute_unified_targets({}, literal_cited={"Q": True})
        >>> round(t["Q"], 3)
        0.775
        >>> c["Q"]
        True

        >>> # Cross-encoder-only node with no cosine: same recovery path.
        >>> t, c = compute_unified_targets({}, cross_encoder_cited={"Q": True})
        >>> c["Q"]
        True

        >>> # Mixed: a literal-only node (no cosine) inherits the present
        >>> # population's mean cosine as its base, then the literal bonus
        >>> # lifts it. With one cosine node {"A": 0.8} the mean is 0.8, so
        >>> # B's base = clamp(0.8 + 0.1*0.5) = 0.85, then literal saturates.
        >>> t, c = compute_unified_targets(
        ...     {"A": 0.8}, literal_cited={"B": True}
        ... )
        >>> sorted(t)
        ['A', 'B']
        >>> c["A"], c["B"]
        (True, True)

        >>> # Empty input (no cosine, no literal, no cross-encoder)
        >>> compute_unified_targets({})
        ({}, {})
    """
    literal_cited = literal_cited or {}
    cross_encoder_cited = cross_encoder_cited or {}

    # F-C (v0.2.70): a node that is literal_cited (or cross_encoder_cited) but
    # has NO cosine entry (its n_emb was missing at citation time, the common
    # case per the deep-bugsweep ~96%-absent finding) used to be discarded
    # entirely — the early-return guard checked only ``cosine_sims`` and the
    # loop iterated only ``cosine_sims.items()``. The strongest, most
    # trustworthy signal (Claude literally named the node) was lost for exactly
    # the nodes cosine could not score. Iterate the UNION of all three input
    # dicts so a literal/cross-encoder-only node still produces a target.
    if not cosine_sims and not literal_cited and not cross_encoder_cited:
        return {}, {}

    # Z-score population stats over the PRESENT cosine entries. A non-empty
    # cosine set keeps the original mean/std computation byte-for-byte (the
    # reproducibility contract — see test_unified_targets.py); an empty set
    # degenerates to a neutral mid-point.
    vals = list(cosine_sims.values())
    # Missing-cosine base. The plan's stated "base 0.0 then literal lifts past
    # 0.6" is arithmetically impossible under the (unchanged) x1.5+0.1 bonus
    # shape — a base of 0 yields 0.1, never cited. To honour BOTH "literal-
    # cited is the strongest trustworthy signal and MUST be cited" AND "do not
    # change the formula's shape", a node ABSENT from cosine_sims is not
    # penalised to 0 for the accident of a missing n_emb: it inherits the
    # present population's MEAN cosine as its base (treated as a typical-
    # similarity node, so its z-score term is neutral). When there is NO cosine
    # population at all (a literal/cross-encoder-only event), the mean is
    # undefined → use a 0.4 floor, the smallest round value comfortably above
    # the cited-after-literal threshold ((0.6-0.1)/1.5 ≈ 0.333) with margin for
    # the z-term + float imprecision. The per-node Step1-4 arithmetic below is
    # unchanged; only the *input* cosine for a missing-cosine node moves from
    # "dropped" to this floor. See knowledge node
    # rl-citation-literal-only-target-floor.
    if vals:
        mean_val = sum(vals) / len(vals)
        variance = sum((v - mean_val) ** 2 for v in vals) / len(vals)
        std_val = variance ** 0.5
        _MISSING_COSINE_BASE: float = mean_val
    else:
        # No real cosine population: anchor the z-score mean to the floor so a
        # missing-cosine node lands at z=0 (z_mapped=0.5) rather than a spurious
        # high z. base = 0.4 + 0.1*0.5 = 0.45 -> literal 0.45*1.5+0.1 = 0.775.
        _MISSING_COSINE_BASE = 0.4
        mean_val = _MISSING_COSINE_BASE
        std_val = 0.0
    denom = max(std_val, 0.05)

    targets: dict[str, float] = {}
    cited: dict[str, bool] = {}

    # Deterministic iteration order: present cosine_sims keys first (in their
    # order — preserves the pre-F-C output ordering for the common path), then
    # any literal/cross-encoder-only keys not already seen. Output values are
    # title-keyed so they're order-independent, but a stable order keeps logs
    # and any order-sensitive downstream replay reproducible.
    seen: set[str] = set()
    ordered_titles: list[str] = []
    for title in cosine_sims:
        if title not in seen:
            seen.add(title)
            ordered_titles.append(title)
    for extra in (*literal_cited.keys(), *cross_encoder_cited.keys()):
        if extra not in seen:
            seen.add(extra)
            ordered_titles.append(extra)

    for title in ordered_titles:
        cosine = cosine_sims.get(title, _MISSING_COSINE_BASE)
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
