---
title: RL Citation — Literal-Only Node Target Floor (F-C)
type: concept
tags: [RL, citation, training-targets, low-level-implementation, v0.2.70]
created: 2026-06-27T00:00:00Z
updated: 2026-06-27T00:00:00Z
valid_from: 2026-06-27T00:00:00Z
valid_until: null
status: active
---

# RL Citation — Literal-Only Node Target Floor (F-C)

## Context
`vco_lib/rl_training_targets.py::compute_unified_targets` is the vendored,
CI-byte-identity-enforced training-target formula shared by the MCP citation
monitor, the container online trainer, and the offline trainer. Per node:
`target = clamp(cos + 0.1·z_mapped, 0, 1)`, then literal-cited ×1.5+0.1, then
cross-encoder ×1.5+0.1, then `cited = target > 0.6`.

## The F-C bug (v0.2.70)
Pre-fix the function `return {}, {}` when `cosine_sims` was empty AND iterated
only `cosine_sims.items()`. A node that was `literal_cited=True` but had no
`n_emb` (no cosine entry — the ~96%-absent common case from the deep-bugsweep)
was passed in via `literal_cited` but NEVER appeared in the output. The
strongest, most trustworthy signal (Claude literally named the node) was lost
for exactly the nodes cosine could not score.

## The fix + the arithmetic gap it exposed
Fix: iterate the UNION of `cosine_sims ∪ literal_cited ∪ cross_encoder_cited`
keys; guard the mean/std stats for an empty/singleton cosine set; change the
early-return guard to also check literal/cross-encoder.

The plan's stated behaviour — "base target = 0.0, then the literal bonus lifts
it past 0.6 → cited" — is **arithmetically impossible** under the (unchanged)
×1.5+0.1 bonus shape: `0.0×1.5+0.1 = 0.1`, never > 0.6. To be cited after the
literal bonus the PRE-bonus base must exceed `(0.6-0.1)/1.5 ≈ 0.333`.

## Decision (shape-preserving)
A node ABSENT from `cosine_sims` is NOT penalised to 0 for the accident of a
missing `n_emb`. Its base cosine defaults to:
- the **mean of the present cosine population** when one exists (treated as a
  typical-similarity node, z-score neutral), OR
- a **0.4 floor** when there is NO cosine population at all (a literal/
  cross-encoder-only event) — the smallest round value comfortably above the
  0.333 cited-after-literal threshold, with margin for the z-term and float
  imprecision. base = 0.4 + 0.1·0.5 = 0.45 → literal 0.45×1.5+0.1 = 0.775.

The per-node Step 1–4 arithmetic is UNCHANGED — only the *input* cosine for a
missing-cosine node moves from "dropped" to this floor. The common path (every
node has a cosine entry) is byte-identical, so the reproducibility-contract
tests pass unchanged.

## Vendored sync
This edit changes the numeric contract → it WILL turn the RL chat's
`tests/test_vendored_file_sync.py` red until they run
`scripts/sync-vendored-files.sh` on `paid-modules/vct-rl-reranker/_training_targets.py`.
Ping the RL chat the moment the formula edit merges.

[[relatedTo::RL Citation Pipeline]]
