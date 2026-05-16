---
title: KG-Summary Three-Tier Generation Pipeline
type: concept
tags: [orchestrator, kg, hooks, ollama, claude-code, summarization, low-level-implementation, vco-launcher, cross-platform, vct-orchestrator-root]
created: 2026-04-27T05:30:00Z
updated: 2026-05-16T20:30:00Z
status: active
---

# KG-Summary Three-Tier Generation Pipeline

Auto-generated 2-3 sentence summaries for every KG node, written to `knowledge/.node_formats.json` and consumed by `rl_kg_search.py` (summarized retrieval tier) and `hybrid_search` (descriptions detail level). Triggered by `PostToolUse` hook on `Edit/Write(knowledge/**/*.md)` and `mcp__weaviate-kg__store_knowledge_node`.

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

## Cross-repo summary reuse

`merge-vco-summaries-into-claude-kg.py`: when the bundled `vibecoded-orchestrator/knowledge/.node_formats.json` is updated, summaries copy into this Claude KG for matching paths (gated by content-hash match — drift triggers regeneration via the hook).

## Files

- `templates/scripts/generate-kg-summary.py` — generator with three-tier dispatch (canonical; rendered into `<project>/.claude/scripts/` at install time)
- `templates/scripts/merge-vco-summaries-into-claude-kg.py` — cross-repo merge
- `templates/hooks/kg-summary-generator.sh` — PostToolUse hook source (rendered into `<project>/.claude/hooks/` at install time, background, debounced 60s)
- `templates/hooks/pre-write-kg-frontmatter-validate.sh` — PreToolUse blocker source
- `knowledge/.node_formats.json` — output destination (per-project)

## Failure modes diagnosed 2026-04-27

- **30s timeout too short** for large nodes (~9k chars body) → bumped to 180s; hook timeout 5s→10s. Hook is `nohup` background so per-call timeout doesn't gate the user.
- **Race condition** when 4-way parallel generators wrote to the same JSON file → last-writer-wins, ~14 of 50 saves persisted. Fix: sequential `xargs -P 1` for backfill batches; runtime hook is single-call so unaffected.
- **Skipped nodes missing title** → resolved upstream by frontmatter pre-write hook.

## VCO Launcher add-project auto-backfill (2026-05-12, v0.2.3)

Before v0.2.3, when a user added a project via the VCO launcher GUI:
- KG was synced into Weaviate (auto, v0.2.2)
- BUT `knowledge/.node_formats.json` was NOT generated — the PostToolUse hook only fires on Claude Code Edit/Write events, NOT on subprocess invocations of `kg-sync`
- Result: freshly-added project had Weaviate populated but the summary sidecar empty; user needed N Claude sessions (one per node) to backfill

v0.2.3 adds a third background task in `create_project_v2`: `kg_summaries` table (migration 012), `commands::kg_summary` module, dedicated banner + pill + "Re-build KG summaries" header button + boot-time resume sweep. Per-file invocation of `generate-kg-summary.py <file>` over `knowledge/**/*.md` after the bundle install. Same launcher pattern as code-graph + kg-sync (third instance of the canonical 3-task topology).

The launcher detects "no backend available" via a literal stdout marker from the script and lands the row in `skipped` (not failed) state with an actionable banner hint listing the three install paths.

## Gotchas surfaced shipping v0.2.3 (2026-05-12)

These ALL passed local Linux tests but bit CI / would have bitten Windows:

- **Windows PATHEXT — bare-name subprocess** ([[implements::silent-fallback-anti-pattern]]):
  `subprocess.run(["claude", ...])` on Windows fails with `FileNotFoundError` when `claude` ships as `.cmd`/`.bat` via npm — Python's subprocess.run does NOT honor PATHEXT for bare names. Meanwhile `shutil.which("claude")` DOES find `.cmd`/`.bat`. Result: `cli_available()` says yes, then 3 consecutive call failures trip the fail-fast threshold → red banner with cryptic traceback. Lesson: always resolve via `shutil.which` and pass the absolute path to subprocess.run, never the bare name. Applies to ALL python subprocess invocations of npm-shipped CLIs on Windows.

- **PR-2 portability contract silently dropped on script propagation**:
  The canonical 440-line summariser lived in the orchestrator's `.claude/scripts/`. When propagated to `templates/scripts/` (and from there into per-project installs), it inherited the orchestrator's habit of resolving `claude_mcp_servers/` via script-relative path — but per-project copies live in `<project>/.claude/scripts/`, where `parent.parent.parent` is the project root NOT the orchestrator. The orchestrator's central copy never needed `VCT_ORCHESTRATOR_ROOT` because the script lives next to `claude_mcp_servers/` directly. CI caught this via `test_all_reference_vct_orchestrator_root` (enforces that all PR-2-rewired scripts grep-contain the env var). **Lesson**: before "promoting" any script from a central location to a per-project template, verify it honors the portability contract for the per-project case — central code is the most-trusted-but-least-portable source.

- **Properly separated env vars**:
  - `KG_PROJECT_ROOT` = the project being summarized (used for `FORMATS_PATH`, `KNOWLEDGE_DIR`, `LOG_PATH`)
  - `VCT_ORCHESTRATOR_ROOT` = where `claude_mcp_servers/` lives (used for `sys.path.insert` to import Weaviate)
  These are different concerns. Conflating them via a single `CLAUDE_PROJECT` (the previous shape) silently broke per-project installs whenever the project lacked its own `claude_mcp_servers/`. Resolution chain: env var → `KG_PROJECT_ROOT`-has-`claude_mcp_servers` → script's `parent.parent.parent` fallback.

- **WEAVIATE_URL / GRPC_PORT plumbing dead-weight**:
  Launcher set `WEAVIATE_URL` env when invoking, but script hardcoded `host="localhost", port=8081`. Non-default Weaviate ports silently degraded multi-chunk lookup to single-summary mode. Fix: parse via `urllib.parse.urlparse(os.getenv("WEAVIATE_URL", ...))` + honor `GRPC_PORT`.

## Files (updated 2026-05-12)

- `templates/scripts/generate-kg-summary.py` — canonical generator (propagated into `<project>/.claude/scripts/` at install time)
- `templates/scripts/merge-vco-summaries-into-claude-kg.py` — cross-repo merge
- `templates/hooks/kg-summary-generator.sh` — PostToolUse hook source (rendered into `<project>/.claude/hooks/` at install time, background, debounced 60s)
- `templates/hooks/pre-write-kg-frontmatter-validate.sh` — PreToolUse blocker source
- `knowledge/.node_formats.json` — output destination (per-project)
- `launcher/src-tauri/src/commands/kg_summary.rs` — Rust background task that drives `generate-kg-summary.py` per-file on add-project

[[uses::Ollama]]
[[uses::Claude Code Hooks]]
[[implements::Auto-Update Pattern]]
[[relatedTo::Hook Path Resolution Priority — Portable Multi-Host Support]]
[[relatedTo::VCO Release Packaging Direction (2026-05-10)]]
