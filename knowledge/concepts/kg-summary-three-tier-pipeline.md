---
title: KG-Summary Three-Tier Generation Pipeline
type: concept
tags: [orchestrator, kg, hooks, ollama, claude-code, summarization, low-level-implementation]
created: 2026-04-27T18:30:00Z
updated: 2026-04-27T18:30:00Z
status: active
---

# KG-Summary Three-Tier Generation Pipeline

Auto-generated 2-3 sentence summaries for every KG node, written to `knowledge/.node_formats.json` and consumed by the RL retrieval layer (summarized retrieval tier) and `hybrid_search` (descriptions detail level). Triggered by the `PostToolUse` hook on `Edit/Write(knowledge/**/*.md)` and on `mcp__weaviate-kg__store_knowledge_node`.

## Backend selection (auto, in order)

1. **`claude` CLI on PATH** — best quality, requires Claude Code CLI install.
2. **Ollama (local, FREE)** at `http://localhost:11435` — works for any orchestrator user since Ollama is already required for embeddings.
3. **`ANTHROPIC_API_KEY` direct** — opt-in fallback, cost warning logged.
4. **Silent skip** — friendly log line, exits 0.

Forced via env: `KG_SUMMARY_BACKEND=cli|ollama|api|skip`.

## Ollama defaults (per [[uses::Ollama]] family)

| Family | temperature | top_p | top_k | num_ctx | num_predict | Notes |
|---|---|---|---|---|---|---|
| qwen3.5 / qwen3 | 0.5 | 0.8 | 20 | 32768 | 1024 | Pass `think: false`; `<think>` blocks stripped post-hoc |
| gemma4 / gemma3 | 0.8 | 0.95 | 64 | 32768 | 1024 | No thinking-mode quirks; accepts system prompts |

Override via `KG_SUMMARY_OLLAMA_OPTIONS='{...}'`. Default model `qwen3.5:9b` (16GB+ VRAM); for low-VRAM/CPU use `gemma4:e4b` (~4.5B effective params). See [[Qwen3.5]] and [[Gemma 4 E4B]].

## Frontmatter contract

A separate `PreToolUse` hook (`pre-write-kg-frontmatter-validate.sh`) BLOCKS writes to `knowledge/**/*.md` if any of these required fields are missing: `title`, `type`, `tags`, `created`, `updated`, `status`. Rationale: the summary generator (and graph integrity in general) depends on these. Body-H1 fallback was rejected as a silent-fallback anti-pattern — force correctness at write time.

## Files

- `.claude/scripts/generate-kg-summary.py` — generator with three-tier dispatch
- `.claude/hooks/kg-summary-generator.sh` — PostToolUse hook (background, debounced 60s)
- `.claude/hooks/pre-write-kg-frontmatter-validate.sh` — PreToolUse blocker
- `knowledge/.node_formats.json` — output destination

## Failure modes diagnosed

- **30s timeout too short** for large nodes (~9k chars body) → bumped to 180s; hook timeout 5s → 10s. Hook is `nohup` background so per-call timeout doesn't gate the user.
- **Race condition** when 4-way parallel generators wrote to the same JSON file → last-writer-wins, ~14 of 50 saves persisted. Fix: sequential `xargs -P 1` for backfill batches; runtime hook is single-call so unaffected.
- **Skipped nodes missing title** → resolved upstream by the frontmatter pre-write hook.

[[uses::Ollama]]
[[uses::Claude Code Hooks]]
[[implements::Auto-Update Pattern]]
