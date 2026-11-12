---
title: Orchestrator RL Retrieval
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, reinforcement-learning, retrieval, weaviate, context-injection]
created: 2026-04-27T18:30:00Z
updated: 2026-11-12T12:00:00Z
status: active
---

# Orchestrator RL Retrieval

> **Ships with VCO — free, no licence.** Since v0.2.106 the RL reranker is part of the public repository (AGPL-3.0): source in `claude_mcp_servers/rl_reranker/`, image built locally from the checkout, weights bundled through `weights/manifest.json`. There is no paid tier for it any more and no licence gate; the earlier Pro-tier / GHCR-distribution design this node used to describe is retired.

The RL retrieval system wraps Weaviate searches with a reinforcement-learning reranking layer. When Claude searches the knowledge graph, the system over-fetches candidates from Weaviate, sends them (with their vectors) to an RL server for reranking based on learned relevance signals, and returns the reordered results. Retrieval and citation events are always logged (owner rule: training data is collected whatever the scoring state); training steps update the reranker.

## Deployment (v0.2.106 shape)

The reranker ships in the orchestrator repository itself:

- **Source**: `claude_mcp_servers/rl_reranker/` in the public AGPL repo — server (`rl_server.py`), model (`rl_model.py`), bundled-weights reader (`bundled_weights.py`), offline training pipeline (`training/`).
- **Image**: built LOCALLY at install/update from `claude_mcp_servers/rl_reranker/Dockerfile` (`install.method = "container_build"` in `vct-module.json`) as `localhost/vco-rl-reranker:<version>-<variant>`. No registry pull, no pull token, no licence check. A start of the module rebuilds the image if it is missing or built for the wrong GPU variant.
- **Scope**: one global container per machine (default host port 11450), serving every project; requests carry the `X-VCT-Project-ID` header so per-project model heads stay separate. Cross-project leakage is impossible by construction — each project's head only trains on its own citation pattern.

**Wiring**: the launcher deliberately does NOT propagate `RL_SERVER_URL` / `RL_SERVER_PORT` into `.claude/settings.json env` for MCP subprocesses. The MCP's client resolves the container's address as: env override first, then the hub-resolved `rl_server_port` from the project config (V52-AA — the port recorded in `module_ports`). `rl_doctor` resolves through the same shared function, so the doctor can never say "not wired" where the MCP finds the container.

**Scoring gates** (in order, one home each):
1. The shipped **scoring lock** `vco_lib/rl_scoring_lock.toml` — lifted in v0.2.106 (`locked = false`, owner ruling 2026-11-12); when set, reranking is off in every state (an explicit per-project row, a host-wide row, nothing overrides it). It gates scoring only, never event logging.
2. The per-project toggle `rl_reranker_enabled_for_project` (hub-served; launcher Modules panel) — OPT-IN: off by default, and off when it cannot be read.
3. The container's **per-project serving gate** (below): with the toggle on, a project's own model reranks its searches only once it qualifies; until then the project keeps its own search order, exactly as with the toggle off.

The pre-v0.2.106 licence gate (`feature_enabled("rl_retrieval")`) is gone — every install may use the reranker.

**Weights**: bundled in the repo through `claude_mcp_servers/rl_reranker/weights/manifest.json` (per-profile `current`, sha256-pinned; qwen3 and arctic ship together, owner rule). No trained weights ship (owner ruling 2026-11-12: no global weights); the container announces its per-project gate (`per_project_gate` on `/health`), so the client calls it without a trained base, and a project that has not qualified gets its search order back (`serving: "search_order"`) — a cold start never reranks with a shipped or base model. Weights ownership on disk follows `vco_lib/rl_weights_ownership.toml` (symlink / marked copy = managed by the launcher; other files = the user's).

**Training** (v0.2.106): an offline pipeline exports launcher.db events to text-free float16 shards (`training/export.py`), trains per-profile (`training/shard_trainer.py`) and evaluates against plain cosine (`training/eval_harness.py`, NDCG/MRR/Recall@10 with bootstrap CI); `ship` refuses unless both profiles pass the bar. The first run did not pass (arctic below cosine on the held-out project), so no weights ship. The product is instead **per-project online learning** (`online_learning.py`): the MCP posts each citation event to `/rl_update` (pre-packed payloads; the MCP is the sole citation and retrieval-event writer), and every 32 events a bounded round trains that project's own model with the one objective (`objective.batch_loss`, pairs only within a query — `/finetune` and Reset + specialize fit the same objective) and re-checks it on a held-out window of the project's newest queries (>= 300 queries and >= 20 that cite a node, never trained on while in the window or within 24 h after).

**Serving gate** (owner ruling 2026-11-12): a paired bootstrap over the window (1 000 resamples of its queries, the same resample for the model and for cosine, seeded per round) gives one-sided 90% lower bounds; the project's model is served only while the NDCG@10 gain over cosine is >= +2% AND the cited node's MRR@10 minus cosine's is >= 0 at that bound (a resample without a citing query counts as failing). The point estimates alone passed by noise on windows of 20-50 citing queries. `/project_status`, the module-DB row `rl_online_state` (the launcher's RL panel) and `rl_doctor` show both the point values and the bounds. A replay of real project histories (`training/transfer.py replay`) still found projects where the served stretches ranked the cited node below cosine — repeated checks on a shifting window are each a new chance to pass — so the gate is a guard, not a guarantee.

[[implements::Reinforcement Learning]] [[uses::Weaviate]] [[relatedTo::Orchestrator Knowledge Graph]] [[relatedTo::Orchestrator MCP Servers]] [[relatedTo::Orchestrator Context Management]]

## Why a Reranker

Standard Weaviate hybrid search returns results ranked by BM25 + vector similarity. This is a reasonable baseline but does not account for:

- Which detail level (titles / descriptions / full) produces the best downstream answer.
- Which result positions Claude actually uses.
- Domain-specific relevance patterns in this project.
- Whether the same node was already injected into context earlier in the session.

The RL layer learns these patterns by observing search sessions and using call-sequence as a proxy for implicit feedback.

## Architecture (protocol v2 — client-sent vectors)

```
Claude calls hybrid_search(query, limit=5)
        |
[weaviate_mcp/server.py]
        |
        +-- Over-fetch from Weaviate: limit * 2 = 10 candidates
        |     (each candidate carries the vectors the retrieval event logs)
        |
        +-- POST http://127.0.0.1:<rl_port>/cache_nodes
        |     Body (rl_client/rerank_wire.py, float16/base64, de-duplicated):
        |       query vector + per-node vector, linked vectors and
        |       type names — the SAME feature selection the training export uses
        |
        |     RL Server (rl_server.py):
        |       1. Load the model for the active profile (bundled weights,
        |          or a local checkpoint that earned its place)
        |       2. Score each candidate (RLModel._build_inputs)
        |       3. Return the reranked order + scores (top-limit items)
        |       4. Training happens through /rl_update (pre-packed payloads)
        |
        +-- Return top-limit items to Claude (the caller's own dicts,
            in the served order)
```

The server EMBEDS NOTHING — the client sends every vector it already computed for logging (since v0.2.106 the server has no Ollama dependency at all; the `--ollama-url` option is gone). A negotiation probe (`/health`) is taken once per client instance; the container is called only when the verdict is `compatible` (trained weights, matching embedding space, current protocol).

## RL Server

A lightweight aiohttp process (`python -m rl_reranker.rl_server`; the container runs the same module) that:

1. Maintains one model per project and profile in memory (`state/projects/<project_id>/`, a seeded fresh network on a project's first event), each with its online learner and serving gate; a project that has not qualified is answered with its own search order.
2. Exposes `/cache_nodes` (score + reorder) and `/health` (protocol version, embedding space, trained flag) for the `weaviate-kg` MCP client, plus lifecycle endpoints (`/rotate_weights`, `/reset`, `/finetune`, `/global/retrain`) the launcher drives without restarting the container.
3. Trains through `/rl_update`; the MCP sends pre-packed payloads and is the sole event writer.

**Model architecture** (v0.2.0, `rl_model.py`): 7 entity slots (query, target, 5 linked) through a shared encoder (Linear+SiLU, emb_dim → 512 → 448 → 128) with type embeddings and per-slot cosine scalars, twin tracks with a cross-path query×target mixer, a 2-layer transformer tail (pre-norm MHA + FFN) and a 5-layer MLP head to a sigmoid score (~1.78M params at emb_dim 1024). Per-text-embedding-source profiles (qwen3 / arctic / openai) each have their own weights. The v0.1.x network is preserved in `rl_model_v1.py` for A/B comparison and rollback.

**Training signal**: `_rl_call_seq` tracks how many search calls have occurred in a session. Higher-sequence calls (later in a conversation) that retrieve the same nodes as earlier calls imply those nodes were used/valued — they receive positive reward. Nodes retrieved once and never revisited get neutral or negative reward over time.

**Graceful degradation**: if the RL server is unavailable, the MCP server falls back to standard Weaviate ranking without error. A genuine fault (container down, too old, wrong embedding space, timeout) keeps the search order and is counted as a fallback; "no container wired" and "not trained" are legitimate off states and are not counted.

## Over-Fetch Pattern

The pipeline fetches `2 × limit` candidates from Weaviate (`KG_OVERFETCH_MULTIPLIER`, default 2, resolved in `rl_state.py`) so the RL reranker has a pool to reorder. With the default `limit=5` it fetches 10 candidates and returns the best 5. This is the standard "retrieve-then-rerank" pattern. The 2x over-fetch ratio is conservative; higher ratios improve recall at the cost of RL inference latency.

## Call-Sequence Tracking

Each MCP session maintains an `_rl_call_seq` integer that increments on every search. Passed with each request so the training side can:

- Detect when the same node appears in multiple searches within a session (implicit positive signal).
- Track the answer horizon — how long after retrieval does Claude use the information.

## Session-Level Deduplication

The pre-edit-context-inject hook tracks which KG and code-graph nodes have already been injected into context during the current session. When a node appears in a new search result:

1. Checks `${TMPDIR:-/tmp}/claude_seen_nodes_${SESSION_ID}` for the node ID.
2. Skips re-injection if the node was recently injected (within same session).
3. Resets dedup tracking on compaction (fresh context state).

Prevents repetitive context bloat when the same pattern nodes match multiple similar queries.

## Node Summaries

KG node metadata (descriptions, summaries, chunk summaries) is generated on-write and stored in `knowledge/.node_formats.json` (see [[KG-Summary Generation Pipeline — Backend Ladder]]):

```json
{
  "knowledge/concepts/foo.md": {
    "title": "Foo Pattern",
    "description": "3-4 sentence overview",
    "summary": "1-2 sentence whole-node summary",
    "chunk_summaries": {"1": "...", "2": "..."},
    "total_chunks": 2,
    "generated_at": "2026-04-11T...",
    "content_hash": "abc123..."
  }
}
```

**Consumption**:
- The `summary` tier of `hybrid_search` returns these descriptions instead of full content; the `full` tier returns the full markdown body.
- Training-side features come from the logged events (vectors + citations); summaries are a retrieval-time tier, not a server input.

## Retrieval Logging

Retrieval events go through the hub-backed `rl_events` writer (the `rl_client` telemetry); `.claude/logs/YYYY-MM-DD_retrieval_expansion.jsonl` records the detail-level choices:

```json
{
  "timestamp": "2026-04-09T10:00:00Z",
  "query": "authentication patterns",
  "detail_level": "descriptions",
  "fetched": 20,
  "returned": 10,
  "detail_bonus": 0.3,
  "rl_reranked": true
}
```

**Detail-level bonuses** (reward shaping):
- `titles`: +0.0 — used for browsing, low value
- `descriptions`: +0.3 — default, medium value
- `full`: +0.8 — used when implementing, high value

These bonuses bias the RL model to learn that full-detail retrievals are more valuable than title-only browsing. Logging happens whatever the scoring lock says — training data is always collected.

## Answer-Window Extraction

The RL monitor extracts agent output from the transcript to compute cosine similarity between retrieved nodes and what the agent actually produced. The `_rl_extract_answer_window()` function in `weaviate_mcp/server.py` scans forward from the KG search tool_use block and collects:

- `text` blocks — the visible chat response
- `thinking` blocks — internal reasoning
- `Write` tool_use blocks — the `content` parameter (truncated to 20K chars per call)
- `Edit` tool_use blocks — the `new_string` parameter (truncated to 20K chars; excludes `old_string`)

Write/Edit inclusion is critical because agents frequently write findings to files rather than explaining them in chat. Without this, nodes whose content was written to a file would receive zero reward signal, biasing training toward "explain in chat" behavior. The 20K per-tool-call truncation prevents a single large file write from consuming the entire 64K answer budget.

## Comparison to Standard RAG

| Feature | Standard RAG | RL Retrieval |
|---|---|---|
| Ranking | BM25 + vector | Learned reranking |
| Adaptation | None | Per-project online + offline training |
| Detail level | Fixed | Reward-shaped |
| Personalization | None | Session-aware |
| Dedup | None | Session-level |
| Fallback | N/A | Graceful to BM25 |

## Integration Points

- **weaviate-kg MCP server**: negotiation-gated `/cache_nodes` call on every `hybrid_search` invocation.
- **pre-edit-context-inject.sh**: uses RL scoring + dedup when building edit-time context.
- **kg-summary-generator.sh**: background job generates summaries on KG edits.
- **Logs**: hub `rl_events` (retrieval + citation events) + `.claude/logs/YYYY-MM-DD_retrieval_expansion.jsonl` (detail choices).
