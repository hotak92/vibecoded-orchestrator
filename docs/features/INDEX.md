# vibecoded-orchestrator — Extended Features Index

## What is this?

`vibecoded-orchestrator` is an AGPL-3.0 release that bundles three subsystems into one repository:

**VCT Launcher** — cross-platform desktop app (Tauri 2 + SvelteKit) for project management, module installs, secrets, the knowledge graph viewer, and Claude Code configuration. Writes per-project env files that the rest of the system reads at startup.

**VibeCoded Orchestrator (VCO)** — a workflow engine for Claude Code: five MCP servers (semantic search, local inference, web search, code embeddings, Playwright), a Weaviate-backed knowledge graph, an AST-extracted code graph, 29 free agents, 28 skills, and 23 hooks. Gives Claude Code persistent memory, free local inference, and a place to put automation that would otherwise live in shell aliases.

**vct-secrets** — zero-dependency Bash CLI (`tools/vct-secrets/vct`) for keeping API keys and tokens out of `.env` files, git history, and shell history. Handles per-project scoping, `exec`-time injection, and an append-only audit log.

The three are designed to compose — Launcher writes, Orchestrator reads, vct-secrets wraps child processes — but each is independently usable.

**Public release**: 2026-05-12 — repo `hotak92/vibecoded-orchestrator`.

---

## Navigation

| Page | Description | ~Entries |
|---|---|---|
| [01-launcher.md](01-launcher.md) | Tauri 2 + SvelteKit desktop app: projects, modules, secrets, licensing, full Tauri command enumeration (~110), hub API routes, audit, CLI | ~190 |
| [02-mcps-and-agents.md](02-mcps-and-agents.md) | Five MCP servers (Weaviate-KG, Ollama, Search, code-embed service, Playwright), KG/code-graph scripts | ~65 |
| [03-agents-skills-hooks.md](03-agents-skills-hooks.md) | 29 free agents, 28 skills, 23 hooks, composition patterns | ~75 |
| [04-knowledge-and-code-graph.md](04-knowledge-and-code-graph.md) | KG node format, Weaviate collections, code graph analysis, embeddings, maintenance scripts | ~65 |
| [05-install-and-secrets.md](05-install-and-secrets.md) | Installers, container lifecycle, vct-secrets CLI, secrets architecture, infrastructure compose | ~80 |
| [06-license-and-commercial.md](06-license-and-commercial.md) | Tier model, license validator, Vault-token + LS-variant admin paths, Lemon Squeezy integration, telemetry, AGPL compliance, CLA | ~90 |
| [07-architecture.md](07-architecture.md) | Cross-cutting architecture, surface compatibility, security model, CI/CD, release process | ~55 |

**Total documented features**: ~580 entries across the seven pages.

---

## Quickstart

- **Install from GitHub**: See [BOOTSTRAP.md](../../BOOTSTRAP.md) — Path B (manual clone).
- **Install via VCT Launcher**: See [BOOTSTRAP.md](../../BOOTSTRAP.md) — Path A (Launcher handles everything).
- **Python installer flags**: `python install.py --help` — or see [05-install-and-secrets.md](05-install-and-secrets.md#install-flags).

---

## Out-of-scope (paid-only, separate repos)

These exist but are **not** in the OSS bundle:

| Component | Description | Where |
|---|---|---|
| `coordination` MCP | Team coordination platform with Supabase backend | `hotak92/vct-coordination` (separate repo) |
| `orchestrator_tools_mcp` | Pro-tier agent tooling MCP | Pro module, not bundled |
| RL reranker | Reinforcement-learning result reranking server | Pro module; MCP falls through cleanly when absent |

---

## Terminology Glossary

| Term | Meaning |
|---|---|
| **MCP** | Model Context Protocol — Anthropic's standard for giving Claude Code tools via a server process |
| **KG** | Knowledge Graph — the Obsidian-style `.md` node store in `knowledge/`, backed by Weaviate |
| **MAO** | Tier-name and DB host value (`projects.host = "mao"`) for the multi-agent stack tier; the actual multi-agent runtime is not in the OSS bundle. |
| **Weaviate** | Open-source vector database used for semantic search over KG nodes, docs, and code |
| **Ollama** | Local LLM server; powers free inference (`qwen3`) and text embeddings |
| **CodeSage** | CodeSage-Large-v2 — 1.3B-parameter code embedding model (GPU service, Apache 2.0) |
| **RAG** | Retrieval-Augmented Generation — feeding search results into an LLM context |
| **GraphRAG** | Graph-augmented RAG — follows typed WikiLinks between KG nodes to expand context |
| **RL reranking** | Reinforcement-learning-trained search result reranker (Pro tier) |
| **AGPL-3.0** | GNU Affero General Public License v3 — copyleft applies to network use |
| **LS** | Lemon Squeezy — payment processor used for license key issuance |
| **Hub API** | Local HTTP server (`port 7700`) bundled in the Launcher for CLI and inter-app IPC |
| **vct-secrets** | Zero-dependency Bash secret manager at `tools/vct-secrets/vct` |
| **Worktree isolation** | Agent flag that runs the agent in a throwaway git branch; changes only merge on review |
| **Blackboard** | Coordination pattern where agents volunteer for tasks from a shared state file |
| **Slug** | URL-safe project identifier auto-generated from project name, used in `/p/<slug>` routes |
| **Tier cache** | SQLite table in the Launcher that caches the validated license tier for 72-hour offline use |
| **Typed WikiLinks** | KG link syntax: `[[relationshipType::Target]]` (e.g. `[[uses::Weaviate]]`) |
| **CFG / PDG** | Control-Flow Graph / Program Dependence Graph — optional Joern-powered code metrics |
| **RLS** | Row-Level Security — Supabase policy that prevents clients from self-granting paid tiers |
