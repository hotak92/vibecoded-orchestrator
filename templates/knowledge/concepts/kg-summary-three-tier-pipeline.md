---
title: KG-Summary Three-Tier Generation Pipeline
type: concept
tags: [orchestrator, kg, hooks, ollama, claude-code, summarization, low-level-implementation, cross-platform]
created: 2026-04-27T05:30:00Z
updated: 2026-07-20T00:00:00Z
status: active
---

# KG-Summary Three-Tier Generation Pipeline

Auto-generated 2-3 sentence summaries for every KG node, written to `knowledge/.node_formats.json` and consumed by KG retrieval clients (summarized retrieval tier + `hybrid_search` descriptions detail level). Triggered by a `PostToolUse` hook on `Edit/Write(knowledge/**/*.md)` and `store_knowledge_node` MCP calls.

## Backend selection (auto, in order)

1. **`claude` CLI on PATH** — best quality, requires CLI install (Max sub OAuth or API key). Gated by a smoke-test, not just `--version`, so an installed-but-unauthenticated CLI doesn't get picked.
2. **Ollama (local, FREE)** at `http://localhost:11435` — works for any orchestrator user since Ollama is already required for embeddings.
3. **OpenAI API (opt-in)** — gated by the `kg_summary_openai_consent` app_state key (default false; set via launcher Preferences → KG Summaries; operator bypass `--force-api`). Costs apply.
4. **`ANTHROPIC_API_KEY` direct** — opt-in fallback, cost warning logged.
5. **Silent skip** — friendly log line, exits 0.

Forced via env: `KG_SUMMARY_BACKEND=cli|ollama|api|openai|skip` (`api` = Anthropic direct, `openai` = OpenAI).

## Ollama defaults (per [[uses::Ollama]] family)

| Family | temperature | top_p | top_k | num_ctx | num_predict | Notes |
|---|---|---|---|---|---|---|
| qwen3.5 / qwen3 | 0.5 | 0.8 | 20 | 32768 | 1024 | Pass `think: false`; `<think>` blocks stripped post-hoc (Ollama 0.5+ recognizes) |
| gemma4 / gemma3 | 0.8 | 0.95 | 64 | 32768 | 1024 | No thinking-mode quirks; accepts system prompts |

Override via `KG_SUMMARY_OLLAMA_OPTIONS='{...}'`. Default model `qwen3.5:9b` (16GB+ VRAM); for low-VRAM/CPU use `gemma4:e4b` (~4.5B effective params).

## Title resolution

The generator extracts the node title from the YAML frontmatter `title:` field. When the frontmatter omits a title (or the file has no frontmatter block at all), it falls back to the body H1 (`# Heading`) so a malformed node still gets a summary rather than being skipped. Nodes that resolve to no title at all are skipped with a log line. Well-formed nodes carry the standard frontmatter (`title`, `type`, `tags`, `created`, `updated`, `status`) which the rest of the graph-integrity tooling relies on.

## Files

- `templates/scripts/generate-kg-summary.py` — generator with three-tier dispatch (canonical; rendered into `<project>/.claude/scripts/` at install time)
- `templates/hooks/kg-summary-generator.sh` — PostToolUse hook source (rendered into `<project>/.claude/hooks/` at install time, background, debounced)
- `knowledge/.node_formats.json` — output destination (per-project)

## Failure modes (recurring)

- **30s timeout too short** for large nodes (~9k chars body) → bumped to 180s; hook timeout 5s→10s. Hook is `nohup` background so per-call timeout doesn't gate the user.
- **Race condition** when 4-way parallel generators wrote to the same JSON file → last-writer-wins, only a fraction of saves persisted. Fix: sequential `xargs -P 1` for backfill batches; runtime hook is single-call so unaffected.
- **Skipped nodes missing title** → the generator falls back to the body H1 before skipping, so only nodes with neither a frontmatter title nor an H1 are skipped.

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
