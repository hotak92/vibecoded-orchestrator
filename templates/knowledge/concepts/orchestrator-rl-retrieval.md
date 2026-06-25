---
title: Orchestrator RL Retrieval
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, reinforcement-learning, retrieval, weaviate, context-injection, pro-tier]
created: 2026-04-27T18:30:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

# Orchestrator RL Retrieval

> **Pro-tier feature.** The reinforcement-learning reranker is part of the orchestrator's paid tier. The free tier ships standard hybrid search (BM25 + vector) and works without it.

The RL retrieval system wraps Weaviate searches with a reinforcement-learning reranking layer. When Claude searches the knowledge graph, the system over-fetches candidates from Weaviate, passes them through an RL server for reranking based on learned relevance signals, and returns the reordered results. Online training fires after each search to improve the reranker continuously.

## Per-Project Deployment

Each project gets its own RL server instance with its own neural network state. Cross-project leakage is impossible — different projects' citation patterns are different latent distributions, and a single shared network would mode-collapse to whoever's data dominates.

**Port allocation**: each project's RL server port is project-specific, allocated at project-create time and persisted to the launcher DB. The port is mirrored to `<project>/.claude/settings.json env.RL_SERVER_URL` (e.g., `http://localhost:<project-rl-port>`). Do not hardcode `11439` — that was the legacy fixed port used before per-project allocation was introduced.

Systemd unit canonical pattern for launcher-managed RL lifecycle: `ExecStart=<install_root>/.venv/bin/python -m rl_server.rl_server --port <project_rl_port> --project-root <project_folder> --log-path <home>/.claude/retrieval_rl_data/rl_events_<project_slug>.jsonl --verbose`. `PYTHONPATH` must include `<install_root>/claude_mcp_servers`. `After=claude-mcp-containers.service` so Ollama is up first.

**License-gate behavior**: `feature_enabled("rl_retrieval")` returns `False` on free tier OR when license-validation has been offline >3 days. When `False`, `_rl_cache_and_rerank` at `weaviate_mcp/server.py` short-circuits to Weaviate cosine order without any HTTP call to the RL server. The 3-day grace is in `VCThelpers/license/validator.py`.

There is no client-side bypass that unlocks paid-server features. Patching `feature_enabled` locally would allow the launcher UI to render the toggle as "on" — but the paid container artifact still cannot be pulled without a valid pull-token from the signed-URL gateway (see [[relatedTo::Launcher Packaging & Paid-Module Distribution Design]]).

**Paid-module distribution**: the RL server's Python source ships in a GHCR container image (not in the AGPL public repo's `claude_mcp_servers/`). Image pulled via a short-lived (15-min TTL) per-user pull token issued by Supabase `/rl-artifact-url` after `/validate-tier` confirms Pro tier. Weekly model-weight rotation is the anti-piracy moat — a leaked snapshot degrades vs free-tier `hybrid_search` within ~2 weeks of stopping refreshes. See [[relatedTo::Launcher Packaging & Paid-Module Distribution Design]] for the full distribution plan.

**Online fine-tuning after global model download**: when the launcher polls `/rl-latest-version` and detects a newer weights bundle, it downloads + prompts the user: fine-tune now / fine-tune later / skip. Fine-tune runs against the last 30 days of events from `~/.claude/retrieval_rl_data/rl_events_<project_slug>.jsonl`. On fine-tune failure (OOM, corrupted log, etc.), the global model is kept unmodified. Last-30-days window keeps fine-tune fast even on CPU-only Pro users.

[[implements::Reinforcement Learning]] [[uses::Weaviate]] [[relatedTo::Orchestrator Knowledge Graph]] [[relatedTo::Orchestrator MCP Servers]] [[relatedTo::Orchestrator Context Management]]

## Why a Reranker

Standard Weaviate hybrid search returns results ranked by BM25 + vector similarity. This is a reasonable baseline but does not account for:

- Which detail level (titles / descriptions / full) produces the best downstream answer.
- Which result positions Claude actually uses.
- Domain-specific relevance patterns in this project.
- Whether the same node was already injected into context earlier in the session.

The RL layer learns these patterns online by observing search sessions and using call-sequence as a proxy for implicit feedback.

## Architecture

```
Claude calls hybrid_search(query, limit=5)
        |
[weaviate_mcp/server.py]
        |
        +-- Over-fetch from Weaviate: limit * 2 = 10 candidates
        |
        +-- POST http://localhost:<project-rl-port>/rerank
        |     Body: {query, candidates, session_id, call_seq}
        |
        |     RL Server (rl_server.py):
        |       1. Load model from state/rl_model_1024.pt
        |       2. Score each candidate
        |       3. Return reranked list (top-limit items)
        |       4. Trigger online training step
        |
        +-- Return top-limit items to Claude
```

## RL Server

**URL**: `http://localhost:<project-rl-port>` — project-specific port configured in `.claude/settings.json env.RL_SERVER_URL`.

A lightweight FastAPI process that:

1. Maintains the RL model in memory (`state/rl_model_1024.pt`, 1024-dim state space).
2. Exposes a `/rerank` endpoint consumed by the `weaviate-kg` MCP server.
3. Fires an online training step after each search using call-sequence as an implicit reward signal.

**Model architecture**: SharedEncoder (Linear → SiLU, 1024 → 48) → HiddenLayer → RefinementLayer (with type embeddings + position features) → CrossAttention (2-layer, query × node interaction) → 5-layer MLP → Sigmoid score. Input dimension 1024 matches the active text embedder ([[Qwen3 Embedding]]). Online + offline training via analog advantage rewards (Z-score normalized).

**Training signal**: `_rl_call_seq` tracks how many search calls have occurred in a session. Higher-sequence calls (later in a conversation) that retrieve the same nodes as earlier calls imply those nodes were used/valued — they receive positive reward. Nodes retrieved once and never revisited get neutral or negative reward over time.

**Graceful degradation**: if the RL server is unavailable, the MCP server falls back to standard Weaviate ranking without error:

```python
try:
    reranked = await rl_client.rerank(query, candidates)
except (ConnectionError, TimeoutError):
    reranked = candidates  # Fallback to original order
```

The free tier runs without the RL server entirely — the fallback path is the default behavior.

## Over-Fetch Pattern

The pipeline fetches `2 × limit` candidates from Weaviate (`_RL_OVERFETCH = 2`) so the RL reranker has a pool to reorder. With the default `limit=5` it fetches 10 candidates and returns the best 5. This is the standard "retrieve-then-rerank" pattern. The 2x over-fetch ratio is conservative; higher ratios improve recall at the cost of RL inference latency.

## Call-Sequence Tracking

Each MCP session maintains an `_rl_call_seq` integer that increments on every search. Passed to the RL server with each request so the server can:

- Detect when the same node appears in multiple searches within a session (implicit positive signal).
- Track the answer horizon — how long after retrieval does Claude use the information.

## Session-Level Deduplication

The pre-edit-context-inject hook tracks which KG and code-graph nodes have already been injected into context during the current session. When a node appears in a new search result:

1. Checks `${TMPDIR:-/tmp}/claude_seen_nodes_${SESSION_ID}` for the node ID.
2. Skips re-injection if the node was recently injected (within same session).
3. Resets dedup tracking on compaction (fresh context state).

Prevents repetitive context bloat when the same pattern nodes match multiple similar queries.

## Node Summaries

KG node metadata (descriptions, summaries, chunk summaries) is generated on-write and stored in `knowledge/.node_formats.json` (see [[KG-Summary Three-Tier Generation Pipeline]]):

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
- RL scoring uses summaries as ranking features.
- The `summary` tier of `hybrid_search` returns these descriptions instead of full content.
- The `full` tier returns the full markdown body.

## Retrieval Logging

The MCP server logs each retrieval event to `.claude/logs/YYYY-MM-DD_retrieval_expansion.jsonl`:

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

These bonuses bias the RL model to learn that full-detail retrievals are more valuable than title-only browsing.

## Answer-Window Extraction

The RL monitor extracts agent output from the transcript to compute cosine similarity between retrieved nodes and what the agent actually produced. The `_rl_extract_answer_window()` function in `weaviate_mcp/server.py` scans forward from the KG search tool_use block and collects:

- `text` blocks — the visible chat response
- `thinking` blocks — internal reasoning
- `Write` tool_use blocks — the `content` parameter (truncated to 20K chars per call)
- `Edit` tool_use blocks — the `new_string` parameter (truncated to 20K chars; excludes `old_string`)

Write/Edit inclusion is critical because agents frequently write findings to files rather than explaining them in chat. Without this, nodes whose content was written to a file would receive zero reward signal, biasing training toward "explain in chat" behavior. The 20K per-tool-call truncation prevents a single large file write from consuming the entire 64K answer budget.

## Embedding Chunking

All embedding-for-similarity paths use unified chunking constants:

| Constant | Value | Rationale |
|---|---|---|
| `EMBED_CHUNK_SIZE` | 6000 chars (~1500 tokens) | embedder supports ~2000 tokens; 75% budget leaves headroom |
| `EMBED_CHUNK_OVERLAP` | 300 chars (~5%) | Avoids hard semantic cuts without wasting compute |

The token-based `Chunker` in `weaviate_mcp/chunking.py` uses a different unit (1500 target / 2000 max tokens, sentence-boundary splitting) for KG node storage in Weaviate.

## Model File

`state/rl_model_1024.pt` — PyTorch state dict for the neural reranker.

- 1024-dim input space (matches the active text embedder).
- Updated after every session via online training.
- Tracked in git so the reranker accumulates learning across sessions.

## Comparison to Standard RAG

| Feature | Standard RAG | RL Retrieval |
|---|---|---|
| Ranking | BM25 + vector | Learned reranking |
| Adaptation | None | Online per-session |
| Detail level | Fixed | Reward-shaped |
| Personalization | None | Session-aware |
| Dedup | None | Session-level |
| Fallback | N/A | Graceful to BM25 |

## Integration Points

- **weaviate-kg MCP server**: calls the RL server on every `hybrid_search` invocation.
- **pre-edit-context-inject.sh**: uses RL scoring + dedup when building edit-time context.
- **kg-summary-generator.sh**: background job generates summaries on KG edits.
- **Logs**: `.claude/logs/YYYY-MM-DD_retrieval_expansion.jsonl` (retrieval events) + `.claude/logs/kg-summary-generator.log` (summary generation).
