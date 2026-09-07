---
title: KG-Summary Three-Tier Generation Pipeline
type: concept
tags: [orchestrator, kg, hooks, ollama, claude-code, summarization, low-level-implementation, cross-platform]
created: 2026-04-27T05:30:00Z
updated: 2026-09-03T00:00:00Z
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

## What moves you DOWN the ladder (v0.2.92)

The order above says which tier is preferred. Until v0.2.92 nothing said how to **leave** a tier that had stopped working: `cli_available()` cached only the result of the initial smoke test, so an account that hit its cap partway through a backfill kept spawning a real `claude -p` subprocess for every remaining node. Two gates now exist, and they are deliberately different kinds of thing.

**1. Circuit breaker — a TIER condition.** A request-time failure is classified (`classify_backend_failure`) and a tier that cannot serve is latched open for a cooldown; the ladder then descends and the node still gets its summary from the next tier.

| Reason | Trips after | Cooldown | Why |
|---|---|---|---|
| `rate_limit` | 1 occurrence | 900 s (`VCO_SUMMARY_BREAKER_COOLDOWN`) | Retrying cannot succeed, and the clock is not yours to move. |
| `auth` | 1 occurrence | 120 s (`VCO_SUMMARY_BREAKER_AUTH_COOLDOWN`) | Also unretryable — but *you* are the fix, and after re-authenticating you expect the next node to use the tier. |
| `capacity` (529/503/timeout) | 3 consecutive (`VCO_SUMMARY_BREAKER_CAPACITY_STRIKES`) | 60 s (`VCO_SUMMARY_BREAKER_CAPACITY_COOLDOWN`) | Transient and self-recovering; a permanent latch on one 529 would be its own bug. |
| `other` | never | — | A breaker that demotes on every unknown error falls back when it should retry. |

The latch is a JSON file in the VCT state dir, not an in-process dict, because the generator runs **one process per node** — an in-process latch would be re-armed once per node. It expires rather than persisting: a recovered endpoint must work again without the user deleting a file. Kill switch `VCO_SUMMARY_BREAKER=off`. A backend named explicitly via `KG_SUMMARY_BACKEND` is never auto-demoted or substituted.

**2. Non-answer gate — a CONTENT condition.** A reply that is a refusal or a canned prompt-back (`"Ready. What do you need summarized?"`, `"I cannot..."`, an empty or sub-8-character string) is **not** a summary. `is_non_answer()` is prefix-anchored on purpose: a real technical summary may *mention* a refusal phrase, it does not *begin* with one. A non-answer **raises** and is never cached; it deliberately does **not** demote the tier and does **not** fall through to the next one — falling through is what would have hidden the Windows defect below, by quietly re-answering on Ollama.

The read side matters as much as the write side: both writers of `knowledge/.node_formats.json` treat a *stored* non-answer as **not satisfied**, so poisoned rows regenerate on the next ordinary run with no user action. A stored non-answer chunk summary invalidates its entry; a **missing** chunk summary does not — absent is not poisoned, and treating it as such would re-run two whole-node LLM calls on every sync for any node whose chunk fetch keeps failing.

Observability: every demotion, skipped tier and fallback prints a line naming the tier and the reason, and the sidecar's `backend` field records the tier that ACTUALLY answered — a summary produced by Ollama is never reported as one produced by the CLI.

## Ollama defaults (per [[uses::Ollama]] family)

| Family | temperature | top_p | top_k | num_ctx | num_predict | Notes |
|---|---|---|---|---|---|---|
| qwen3.5 / qwen3 | 0.5 | 0.8 | 20 | 32768 | 1024 | Pass `think: false`; `<think>` blocks stripped post-hoc (Ollama 0.5+ recognizes) |
| gemma4 / gemma3 | 0.8 | 0.95 | 64 | 32768 | 1024 | No thinking-mode quirks; accepts system prompts |

Override via `KG_SUMMARY_OLLAMA_OPTIONS='{...}'`. Default model `qwen3.5:9b` (16GB+ VRAM); for low-VRAM/CPU use `gemma4:e4b` (~4.5B effective params).

## Title resolution

The generator extracts the node title from the YAML frontmatter `title:` field. When the frontmatter omits a title (or the file has no frontmatter block at all), it falls back to the body H1 (`# Heading`) so a malformed node still gets a summary rather than being skipped. Nodes that resolve to no title at all are skipped with a log line. Well-formed nodes carry the standard frontmatter (`title`, `type`, `tags`, `created`, `updated`, `status`) which the rest of the graph-integrity tooling relies on.

## Files

- `templates/scripts/summary_backends.py` — the shared ladder: backend selection, circuit breaker, `is_non_answer`. ONE implementation; every other file here calls it rather than copying it.
- `templates/scripts/generate-kg-summary.py` — per-node runtime generator (canonical; rendered into `<project>/.claude/scripts/` at install time)
- `claude_mcp_servers/scripts/generate_node_formats.py` — the **second writer of the same sidecar**: the manual `--all` backfill, also spawned by `sync_knowledge_graph._regen_node_formats_after_full_sync()` on the orchestrator-root layout. It imports the ladder's `is_non_answer` for exactly this reason — a validity gate on only one of two writers means one path heals a row and the other re-poisons it.
- `templates/hooks/kg-summary-generator.sh` — PostToolUse hook source (rendered into `<project>/.claude/hooks/` at install time, background, debounced)
- `launcher/src-tauri/src/commands/kg_summary.rs` — the launcher's walk over the same generator; parses the backend line and the no-backend/cooling-down lines out of its stdout
- `knowledge/.node_formats.json` — output destination (per-project)

## Failure modes (recurring)

- **30s timeout too short** for large nodes (~9k chars body) → bumped to 180s; hook timeout 5s→10s. Hook is `nohup` background so per-call timeout doesn't gate the user.
- **Race condition** when 4-way parallel generators wrote to the same JSON file → last-writer-wins, only a fraction of saves persisted. Fix: sequential `xargs -P 1` for backfill batches; runtime hook is single-call so unaffected.
- **Skipped nodes missing title** → the generator falls back to the body H1 before skipping, so only nodes with neither a frontmatter title nor an H1 are skipped.
- **Cached non-answers, frozen by the hash gate** (v0.2.92) → a field scan found 43% of one project's stored summaries holding `"Ready. What do you need summarized?"`. The content hash of the unchanged source file kept matching, so the row never regenerated. Root cause was the Windows argv defect below, not model quality; the fix has two halves — stop producing them (stdin), and stop treating a stored one as satisfied (the non-answer gate above).

## Cross-platform footguns

These all pass local Linux/macOS tests but bite Windows + CI:

- **Windows `.cmd` shim re-parses argv — a newline in the prompt TERMINATED the command** (v0.2.92, and the root cause of the 43% above):
  `claude` ships from npm as a `.cmd` shim, so `CreateProcess` routes it through `cmd.exe`, which **re-parses the arguments**. The prompt was passed as an argv element and contained `SYSTEM_PROMPT + "\n\n" + body` — the embedded newline ended the command line, the model received the system prompt alone, and replied `"Ready. What do you need summarized?"`. Windows-only incidence, which is why a Linux box showed 0/821 poisoned KG entries while the field report showed 43%. Node bodies also contain `&`, `|` and `%`, i.e. the BatBadBut injection shape. **Fix: the prompt goes over stdin, never argv.** Lesson: on Windows an argv element is not a value, it is text a second parser will read.

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
