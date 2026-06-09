---
title: KG-Summary Three-Tier Generation Pipeline
type: concept
tags: [orchestrator, kg, hooks, ollama, claude-code, summarization, low-level-implementation, cross-platform]
created: 2026-04-27T05:30:00Z
updated: 2026-05-16T20:30:00Z
status: active
---

# KG-Summary Three-Tier Generation Pipeline

Auto-generated 2-3 sentence summaries for every KG node, written to `knowledge/.node_formats.json` and consumed by KG retrieval clients (summarized retrieval tier + `hybrid_search` descriptions detail level). Triggered by a `PostToolUse` hook on `Edit/Write(knowledge/**/*.md)` and `store_knowledge_node` MCP calls.

## Backend selection (auto, in order)

1. **`claude` CLI on PATH** — best quality, requires CLI install (Max sub OAuth or API key).
2. **Ollama (local, FREE)** at `http://localhost:11435` — works for any orchestrator user since Ollama is already required for embeddings.
3. **`ANTHROPIC_API_KEY` direct** — opt-in fallback, cost warning logged.
4. **Silent skip** — friendly log line, exits 0.

Forced via env: `KG_SUMMARY_BACKEND=cli|ollama|api|skip`.

## Ollama defaults (per [[uses::Ollama]] family)

| Family | temperature | top_p | top_k | num_ctx | num_predict | Notes |
|---|---|---|---|---|---|---|
| qwen3.5 / qwen3 | 0.5 | 0.8 | 20 | 32768 | 1024 | Pass `think: false`; `<think>` blocks stripped post-hoc (Ollama 0.5+ recognizes) |
| gemma4 / gemma3 | 0.8 | 0.95 | 64 | 32768 | 1024 | No thinking-mode quirks; accepts system prompts |

Override via `KG_SUMMARY_OLLAMA_OPTIONS='{...}'`. Default model `qwen3.5:9b` (16GB+ VRAM); for low-VRAM/CPU use `gemma4:e4b` (~4.5B effective params).

## Frontmatter contract

A separate `PreToolUse` hook (`pre-write-kg-frontmatter-validate.sh`) BLOCKS writes to `knowledge/**/*.md` if any of these required fields are missing: `title`, `type`, `tags`, `created`, `updated`, `status`. Rationale: the summary generator (and graph integrity in general) depend on these. Body-H1 fallback was rejected as a [[implements::silent-fallback-anti-pattern]] — force correctness at write-time.

## Files

- `templates/scripts/generate-kg-summary.py` — generator with three-tier dispatch (canonical; rendered into `<project>/.claude/scripts/` at install time)
- `templates/hooks/kg-summary-generator.sh` — PostToolUse hook source (rendered into `<project>/.claude/hooks/` at install time, background, debounced 60s)
- `templates/hooks/pre-write-kg-frontmatter-validate.sh` — PreToolUse blocker source
- `knowledge/.node_formats.json` — output destination (per-project)

## Failure modes (recurring)

- **30s timeout too short** for large nodes (~9k chars body) → bumped to 180s; hook timeout 5s→10s. Hook is `nohup` background so per-call timeout doesn't gate the user.
- **Race condition** when 4-way parallel generators wrote to the same JSON file → last-writer-wins, only a fraction of saves persisted. Fix: sequential `xargs -P 1` for backfill batches; runtime hook is single-call so unaffected.
- **Skipped nodes missing title** → resolved upstream by frontmatter pre-write hook.

## Cross-platform footguns

These all pass local Linux/macOS tests but bite Windows + CI:

- **Windows PATHEXT — bare-name subprocess** ([[implements::silent-fallback-anti-pattern]]):
  `subprocess.run(["claude", ...])` on Windows fails with `FileNotFoundError` when `claude` ships as `.cmd`/`.bat` via npm — Python's subprocess.run does NOT honor PATHEXT for bare names. Meanwhile `shutil.which("claude")` DOES find `.cmd`/`.bat`. Result: `cli_available()` says yes, then 3 consecutive call failures trip the fail-fast threshold → red banner with cryptic traceback. Lesson: always resolve via `shutil.which` and pass the absolute path to subprocess.run, never the bare name. Applies to ALL python subprocess invocations of npm-shipped CLIs on Windows.

- **Properly separated env vars** (project-root vs orchestrator-root):
  - `KG_PROJECT_ROOT` = the project being summarized (used for `FORMATS_PATH`, `KNOWLEDGE_DIR`, `LOG_PATH`)
  - `VCT_ORCHESTRATOR_ROOT` = where `claude_mcp_servers/` lives (used for `sys.path.insert` to import Weaviate)
  These are different concerns. Conflating them via a single env var silently breaks per-project installs whenever the project lacks its own `claude_mcp_servers/`. Resolution chain: env var → `KG_PROJECT_ROOT`-has-`claude_mcp_servers` → script's `parent.parent.parent` fallback.

- **WEAVIATE_URL / GRPC_PORT plumbing dead-weight**:
  If the caller sets `WEAVIATE_URL` env but the script hardcodes `host="localhost", port=8081`, non-default Weaviate ports silently degrade multi-chunk lookup to single-summary mode. Fix: parse via `urllib.parse.urlparse(os.getenv("WEAVIATE_URL", ...))` + honor `GRPC_PORT`.

[[uses::Ollama]]
[[uses::Claude Code Hooks]]
[[implements::Auto-Update Pattern]]
[[relatedTo::Hook Path Resolution Priority — Portable Multi-Host Support]]
