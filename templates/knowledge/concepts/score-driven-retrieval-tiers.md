---
title: Score-Driven Retrieval Tiers
type: concept
tags: [retrieval, knowledge-graph, weaviate, mcp, token-efficiency, mid-level-architecture]
created: 2026-04-27T12:00:00Z
updated: 2026-06-25T00:00:00Z
valid_from: 2026-04-27T00:00:00Z
valid_until: null
status: active
---

# Score-Driven Retrieval Tiers

A per-result verbosity policy that adjusts how much content each search hit returns,
based on its relevance score. Replaces the prior uniform `detail` parameter (every
result rendered at the same verbosity) so highly-relevant nodes get rich content while
marginal nodes only contribute a one-paragraph orientation. Implemented in
[[implements::Weaviate]] MCP server, applied by [[uses::hybrid_search]],
[[uses::semantic_graph_search]], and the pre-edit context-injection hook
(`rl_kg_search.py`).

## Motivation

Before this refactor, `hybrid_search(detail="descriptions")` returned a uniform
6-line description for every result. The top hit (often the actual answer) and the
bottom hit (often borderline noise) carried the same context budget, which:

- Wasted tokens on low-score results the agent rarely needed
- Forced the agent to re-call with `detail="full"` for a deeper look at the top hit
- Made the calibrated 5-tier logic that already existed in `rl_kg_search.py` (used
  by the pre-edit hook) inconsistent with what `hybrid_search` callers received

Auto-tier mode resolves all three.

## The Five Tiers

Thresholds calibrated on 18 relevant + 20 irrelevant queries against a representative
KG collection. Score is normalised 0..1 (1 - distance, or
RL-reranked relevance), higher = better.

| Score range  | Tier           | Body                                                     |
|--------------|----------------|----------------------------------------------------------|
| < 0.42       | `discard`      | Filtered out (treated as topical noise)                  |
| 0.42..0.55   | `summary`      | LLM description from sidecar (~6 lines), or summary, or 200-char content fallback |
| 0.55..0.65   | `single_chunk` | Matched chunk only (~2000 chars). Multi-chunk node → prepend whole-node summary |
| 0.65..0.75   | `three_chunks` | 3 chunks centred on hit + chunk-map header                |
| >= 0.75      | `full`         | Whole node, capped at 7 chunks centred on hit             |

Threshold constants live in `_TIER_THRESHOLDS` in `claude_mcp_servers/weaviate_mcp/server.py`
and are env-tunable (`KG_TIER_MIN`, `KG_TIER_SINGLE_CHUNK`, `KG_TIER_THREE_CHUNKS`,
`KG_TIER_FULL`).

## Where Applied

- `hybrid_search(detail="auto")` — new default. Per-result tier from score.
- `semantic_graph_search(detail="auto")` — new default. Auto-tier on primary results;
  connected nodes always render at `summary` (graph topology, not score, drove their
  selection — re-fetching them via near_text would add 300-800ms latency for marginal
  benefit).
- `search_code_graph(detail="auto")` — code graph has no sidecar so tiering is
  position-based (top-4 full, rest metadata refs) rather than score-thresholded.
- `claude_mcp_servers/scripts/rl_kg_search.py` — invoked by pre-edit hook; produces
  the same tier-formatted output for context injection.

Explicit `detail` overrides (`titles`, `summary`, `single_chunk`, `three_chunks`,
`full`) apply uniformly to every result, bypassing the score-based dispatch. Legacy
alias `descriptions` maps to `summary` for backward compatibility.

## Sidecar Dependency

Tier formatting reads from `knowledge/.node_formats.json` (the LLM-generated
sidecar maintained by `claude_mcp_servers/scripts/generate_node_formats.py` and the
`kg-summary-generator.sh` post-edit hook). Fields used:

- `description` — preferred body for `summary` tier (~6 lines)
- `summary` — shorter fallback when description missing (~1-2 lines); also used as
  whole-node header on partial multi-chunk views
- `chunk_summaries` — per-chunk one-liners; rendered as a chunk-map header on
  `single_chunk` and `three_chunks` tiers so the agent can request additional
  chunks deliberately

When the sidecar entry is missing, the helper falls back through:
description → summary → 200-char content snippet (BUG-SIDECAR-DESC-FALLBACK fix —
the prior `hybrid_search` skipped the summary fallback and went straight to content).

## Implementation Anchors

- `claude_mcp_servers/weaviate_mcp/server.py:_TIER_THRESHOLDS` — tunable constants
- `claude_mcp_servers/weaviate_mcp/server.py:_get_result_verbosity_by_score` — score → tier
- `claude_mcp_servers/weaviate_mcp/server.py:_format_result_by_tier` — tier → result dict
- `claude_mcp_servers/weaviate_mcp/server.py:_chunk_summaries_header` — chunk-map header
- `claude_mcp_servers/scripts/rl_kg_search.py` — hook-invoked CLI, calls the same helpers
- `tests/test_shared_kg.py` — tier-formatting unit tests (`_format_result_by_tier`
  boundary scores + per-collection sidecar resolution + full-vs-summary equivalence)
- `tests/test_retrieval_tuning_roundtrip.py` — threshold round-trip + token-savings probe

## Observed Token Savings

Integration test against a live KG collection
(query "score-driven retrieval tiers", limit 8):

- `detail="auto"`: 8,496 bytes
- `detail="full"`: 19,709 bytes
- **Savings: 56.9%** for the same query, with the highest-relevance result still
  rendered at `full` tier (no information loss on the answer).

## RL Fallback (Silent by Design)

When the RL reranker (`_rl_cache_and_rerank`) cannot reach the RL server, results
fall back to Weaviate-distance order without surfacing a `_rl_fallback` flag.
Decision: RL is paid-tier infrastructure; surfacing the fallback would tempt callers
to retry, wasting tokens. The fallback is logged at `debug` level only.

## See Also

- [[uses::Weaviate]] — vector DB carrying the embeddings
- [[uses::Ollama]] — local embedding model (qwen3-embedding:0.6b)
- `.claude/context/kg-retrieval-pipeline-audit-2026-04-27.md` — pre-refactor audit
  documenting all entry points and the bug list this refactor closes
