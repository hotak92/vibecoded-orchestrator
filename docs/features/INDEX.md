# vibecoded-orchestrator — Extended Features Index

## What is this?

`vibecoded-orchestrator` is an AGPL-3.0 release that bundles three subsystems into one repository:

**VCT Launcher** — cross-platform desktop app (Tauri 2 + SvelteKit) for project management, module installs, secrets, the knowledge graph viewer, and Claude Code configuration. Writes per-project env files that the rest of the system reads at startup.

**VibeCoded Orchestrator (VCO)** — a workflow engine for Claude Code: three default MCP servers (Weaviate-KG semantic search, academic-paper search, coordination notes), a Weaviate-backed knowledge graph, an AST-extracted code graph, 45 free agents, 53 skills, and 31 hooks. Gives Claude Code persistent memory, free local inference, and a place to put automation that would otherwise live in shell aliases.

**vct-secrets** — zero-dependency Bash CLI (`tools/vct-secrets/vct`) for keeping API keys and tokens out of `.env` files, git history, and shell history. Handles per-project scoping, `exec`-time injection, and an append-only audit log.

The three are designed to compose — Launcher writes, Orchestrator reads, vct-secrets wraps child processes — but each is independently usable.

**Public release**: 2026-05-12 — repo `hotak92/vibecoded-orchestrator`.

**Latest tag covered by these docs**: v0.2.53 (2026-06-11). Install architecture
landed via the Track G design doc, see
[INSTALL_ARCHITECTURE_v2.md](../INSTALL_ARCHITECTURE_v2.md) for the full
design + the actual-shipped vs deferred-to-v0.2.54 reconciliation
(Section 11).

---

## Navigation

| Page | Description | v0.2.53 highlights |
|---|---|---|
| [01-launcher.md](01-launcher.md) | Tauri 2 + SvelteKit desktop app: projects, modules, secrets, licensing, full Tauri command enumeration (~110), hub API routes, audit, CLI | PATH augmentation for Finder/.desktop launches (M-P0-7), InstallHealthGate refresh-on-focus + Re-check (M-P0-8), per-install-root localStorage scoping (M-P1-5), "Run installer now" button (M-P1-6), V52-AH-FE update-event toast |
| [02-mcps-and-agents.md](02-mcps-and-agents.md) | Three default MCP servers (Weaviate-KG, Search, coordination), code-embed service, KG/code-graph scripts | `_lib/update_gate.py` MCP-side mirror of `vco_lib.update_gate`; Track F two-layer case-rebind closes Fabio Symptom B |
| [03-agents-skills-hooks.md](03-agents-skills-hooks.md) | 45 free agents, 53 skills, 31 hooks, composition patterns | V52-M hook P1 bugs fixed (exec bit on POSIX, UTF-8 BOM on Windows PS 5.1); defensive `chmod 0o755` at install.py:11299–11305; FS-disable contract (B2) verified end-to-end |
| [04-knowledge-and-code-graph.md](04-knowledge-and-code-graph.md) | KG node format, Weaviate collections, code graph analysis, embeddings, maintenance scripts | Svelte parser (V52-O.11.B) + PowerShell parser (V52-O.11.N) added to code-graph indexer; NEW-2 case-insensitive class-name resolver in vct-hub; NEW-10 4-way → 2-SSOT class-prefix sanitiser consolidation |
| [05-install-and-secrets.md](05-install-and-secrets.md) | Installers, container lifecycle, vct-secrets CLI, secrets architecture, infrastructure compose | 3-step first-install shim sequence (bootstrap prepass → install → launcher post-install); `install.py --bootstrap --json` envelope at `state/logs/bootstrap-prepass.json`; 6 new vco_lib modules (2 implemented, 4 skeletons); tri-OS install smoke CI gate; Python wheel-coverage detection refuses 3.14+; Linux distro broadening (zypper + apk); macOS Podman machine auto-init; Windows PS 5.1 + Scheduled Task XML hardening |
| [06-license-and-commercial.md](06-license-and-commercial.md) | Tier model, license validator, Vault-token + LS-variant admin paths, Lemon Squeezy integration, telemetry, AGPL compliance, CLA | No v0.2.53 surface changes |
| [07-architecture.md](07-architecture.md) | Cross-cutting architecture, surface compatibility, security model, CI/CD, release process | Track F two-layer case-rebind (vct-hub `weaviate_schema_probe.rs` + install.py `_resolve_existing_casing`); DEDUP-14 paired sentinel+deferral writer at `installer.rs::write_resume_sentinel_and_deferral`; 6 new vco_lib modules document the v0.2.54 unification staging plan |

**Total documented features**: ~580 entries across the seven pages.

For the design rationale behind the v0.2.53 install changes (why
`--bootstrap` is additive and read-only, why the shims stay multi-language,
why DEDUPs land in tiers across two releases) see
[INSTALL_ARCHITECTURE_v2.md](../INSTALL_ARCHITECTURE_v2.md).

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
