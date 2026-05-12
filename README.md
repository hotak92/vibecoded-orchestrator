# VibeCoded Orchestrator

[![CI](https://img.shields.io/github/actions/workflow/status/hotak92/vibecoded-orchestrator/ci.yml?branch=main&label=CI)](https://github.com/hotak92/vibecoded-orchestrator/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/hotak92/vibecoded-orchestrator)](https://github.com/hotak92/vibecoded-orchestrator/releases)
[![License: AGPL-3.0](https://img.shields.io/github/license/hotak92/vibecoded-orchestrator)](LICENSE)
[![Stability: alpha](https://img.shields.io/badge/stability-alpha-orange)](KNOWN_ISSUES.md)

A local knowledge graph and code graph for [Claude Code](https://claude.ai/code), so it stops forgetting your project between sessions.

> **Status**: alpha (v0.2.x). Validated end-to-end on Linux + Windows; macOS is Tier-2 (script-ready, no signed binary). Bugs and rough edges expected — please file Issues. License: AGPL-3.0. Runs entirely on your machine.

<!-- DEMO_PLACEHOLDER
Drop a 30–60s GIF or MP4 here showing:
  Session 1: edit a file, make a decision in chat, exit.
  Session 2: open Claude Code, ask a question that requires recalling
             session 1's decision — Claude answers correctly, references
             the file, no re-explanation needed.
Caption: "Session 2 picks up where session 1 left off — no manual context dump."
-->

## What it actually does

Three concrete pains everyone using Claude Code hits, and what VCO does about each:

| Pain | What VCO does |
|------|---------------|
| Claude has no memory between sessions — you re-explain the project every time | A persistent **Knowledge Graph** (markdown nodes + Weaviate vector index) that hooks read on session start and inject into the context window |
| Claude can't see your codebase structure — it greps blind, misses callers, re-reads the same files | A **Code Graph** built from Tree-sitter AST: modules, classes, functions, APIs, and cross-service calls, queryable by purpose ("find auth middleware") not just name |
| You set up the same `.claude/` config and hooks in every new project | An installer that drops 23 automation hooks + 28 skills + 29 agents into `.claude/`, wires the MCP servers, and stays out of your way |

You don't change how you use Claude Code. The orchestrator runs in the background through hooks. Open VS Code, talk to Claude, ship code.

## Install (≈5 min + first-run downloads)

> **Primary install guide**: [vibecodedtools.com/quickstart](https://www.vibecodedtools.com/quickstart) — same content, more readable, kept in sync with releases.

After cloning, run the entry point for your OS:

| OS            | Install (one-time)                                         | Start launcher                                       |
|---------------|------------------------------------------------------------|------------------------------------------------------|
| **Linux**     | `bash first-install.sh` or double-click `first-install.desktop` | `bash start-launcher.sh` or `start-launcher.desktop` |
| **macOS**     | Double-click `first-install.command` (or `bash first-install.sh`) | Double-click `start-launcher.command`                |
| **Windows**   | Double-click `first-install.bat` (or `.\first-install.bat` from a terminal — the leading `.\` is required in cmd/PowerShell) | Double-click `start-launcher.bat` |

One-liner (Linux / macOS):
```bash
git clone https://github.com/hotak92/vibecoded-orchestrator.git && cd vibecoded-orchestrator && bash first-install.sh
```

The installer auto-handles Python, the container runtime (Podman or Docker), GPU detection, and the launcher binary. Allow ~5–10 min plus first-run image downloads (~5 GB: Weaviate + Ollama qwen3 weights, +2.5 GB if GPU mode pulls CodeSage-Large-v2).

If anything fails partway through, paste [`docs/INSTALL_RECOVERY.md`](docs/INSTALL_RECOVERY.md) into Claude Code — it walks Claude through diagnosing and finishing the build.

## Who this is for

If you use **VS Code (or any IDE) with [Claude Code](https://claude.ai/code)**, this is for you. The orchestrator runs entirely in the background — it indexes your knowledge, your codebase, your tool calls, and feeds Claude richer context every time you talk to it. **Your workflow doesn't change.**

If you don't use Claude Code: the KG, code graph, MCP servers, and launcher GUI all work standalone, but the value is highest when there's an AI client driving them. See [Compatibility](#compatibility).

---

## Features

- **Knowledge Graph** — Obsidian-style markdown nodes with typed WikiLinks, indexed in Weaviate via qwen3 embeddings (1024-dim, local). Optional OpenAI embeddings.
- **Code Graph** — Tree-sitter AST analysis across 10+ languages, populating `CodeModule`, `CodeClass`, `CodeFunction`, `CodeAPI`, `CodeInteraction` collections. Optional Joern integration for CFG/PDG metrics.
- **23 automation hooks** — context injection on prompt submit, KG/code-graph auto-sync on file edit, credential scans, compaction-preserving context replay, security checks. Linux/macOS use `.sh`; Windows ships native `.ps1`.
- **5 MCP servers** — Weaviate (semantic + graph search), Ollama (local LLM, embeddings, vision via qwen3.5:9b, document summarization), search (web + GitHub code + arXiv), code-embedding (CodeSage-Large-v2 via FastAPI), and Playwright (browser automation, screenshots, GUI testing). All local; no per-tool API keys for the default tier.
- **29 agents + 28 skills** — shipped via `install.py` templates. Agents handle planning, coding, testing, doc maintenance, KG navigation, code-graph health. Skills cover security review, debugging, architecture, RAG advisory, accessibility, etc.
- **Workflow plumbing** — session state tracking (`CONTEXT_STATE.md`), plan files, memory management, pre-/post-compact context replay so a `/compact` doesn't lose your thread.

## How it works

```
You type in Claude Code
        |
        v
[UserPromptSubmit hook]
  -> Searches Knowledge Graph for relevant nodes
  -> Searches code graph for relevant entities
  -> Injects results into Claude's context window
        |
        v
Claude generates response, edits files
        |
        v
[PostToolUse hooks]
  -> Auto-syncs edited knowledge / code to KG / Code Graph
  -> Scans for credential leaks
  -> Updates session state
```

### What runs when you add a project

When you click **Add project** in the launcher GUI, `create_project_v2`
returns the moment bundle install finishes — but three background tasks
then fan out in parallel so a project with pre-existing
`knowledge/**/*.md`, `docs/**/*.md`, or source code lands fully indexed
without manual CLI invocations:

1. **Code graph build** — `code-graph-analyze` over the project root,
   populating `CodeModule` / `CodeClass` / `CodeFunction` / `CodeAPI` /
   `CodeInteraction` collections.
2. **KG sync** — `.claude/scripts/kg-sync --all` walks
   `knowledge/**/*.md` and `docs/**/*.md` and embeds them into the
   per-project Weaviate collections (`<Project>_KnowledgeGraph` and
   `<Project>_Development`). Added in 0.2.2.
3. **KG summaries** — `.claude/scripts/generate-kg-summary.py` over
   each `knowledge/**/*.md` file, producing the
   `knowledge/.node_formats.json` sidecar consumed by
   `hybrid_search`'s `summary` tier. Three-tier backend fallback:
   `claude` CLI on PATH → Ollama at `KG_SUMMARY_OLLAMA_URL` (default
   `http://localhost:11435`, model `qwen3.5:9b`) →
   `ANTHROPIC_API_KEY` direct → silent skip. Added in 0.2.3.

The project page shows three stacked status banners (KG summaries,
KG sync, code graph build) under the project header — `pending` /
`running` / `failed` always visible, `success` / `skipped` auto-hide
30 s after completion. The project header carries three retry
buttons (`Re-build code graph`, `Re-sync KG`, `Re-build KG summaries`).
The launcher's boot sweep marks any `running` rows left behind by a
prior crash as `failed` with `"launcher crashed mid-run; click Retry
to re-run"`, and re-spawns any `pending` rows. The summariser
content-hashes each node, so retries on an already-summarised
project are a cheap no-op.

If neither `claude` CLI nor Ollama is available when the KG-summary
task runs, the third banner goes yellow `skipped` after the first
node and shows the install hint under `Show details`; summaries then
backfill incrementally as you edit nodes in Claude Code sessions via
the `PostToolUse` hook `kg-summary-generator.{sh,ps1}`.

## Where this fits

VCO sits on top of Claude Code rather than replacing your AI assistant. The comparison below is for buyers choosing how to spend their AI-coding attention — VCO + Claude Code, vs. an all-in-one closed product, vs. another open-source extension.

| Dimension | Cursor | GitHub Copilot | Augment | Devin | OpenAI Codex CLI | Aider | Cline | **VCO + Claude Code** |
|---|---|---|---|---|---|---|---|---|
| Open source | No | No | Partial (some components) | No | Yes (CLI) | Yes (MIT) | Yes (Apache-2.0) | **Yes (AGPL-3.0)** |
| Runs locally (no code in vendor cloud) | Partial (cloud Composer) | No | No | No | Partial (CLI local, API cloud) | Yes | Yes | **Yes** |
| Persistent memory across sessions | No | Yes (Copilot Memory, repo-scoped, 28-day expiry) | Partial (team memory) | Partial (session-bound) | No | No | No | **Yes (KG, no expiry)** |
| Code graph (AST, callers, APIs) | Partial (file index, opaque) | Partial (vector index) | Yes (Context Engine) | Yes | No | Yes (repomap) | Partial (Tree-sitter, not persisted) | **Yes (persisted graph)** |
| Bring your own LLM subscription | Partial (chat only) | No | Partial (BYO agent, not LLM) | No | Yes (OpenAI) | Yes (75+ providers) | Yes (30+ providers) | **Yes (Claude)** |
| User-extensible (hooks / agents / skills) | Yes (hooks + skills, no marketplace) | No | Limited (MCP only) | No | Limited (skills as prompts) | Yes (open source) | Yes (open source) | **Yes (23 hooks, 28 skills, 29 agents)** |
| Pricing model | $20/mo SaaS | $10–20/user/mo | BYOA + cloud compute | $20/mo + usage | Per-token OpenAI | Free + your LLM | Free + your LLM | **Free + your Claude sub; €19/mo Pro** |
| Polished v1 product (vs. alpha) | Yes | Yes | Yes | Yes | Yes | Yes | Yes | **No — alpha** |

**Reading the table**: Cursor, Copilot, Augment, and Devin are closed all-in-ones — they ship the editor, the AI, the context layer, and the cloud, bundled. Aider, Cline, Continue, and Codex CLI are open-source / BYOL but lack persistent memory and (in most cases) a structural code graph. VCO is the combination that doesn't otherwise exist: open-source, local, BYO-Claude-subscription, *and* it has both persistent memory and a code graph. The trade you're making for that is product polish — VCO is alpha, the others are stable v1+.

Facts current as of May 2026. Competitor products move fast — verify the row you care about before quoting.

## Compatibility

Works with all three Claude Code surfaces. Primary target is the **VS Code extension**; the Desktop app and standalone CLI are also supported. Hooks fire, agents and skills load, and MCP servers connect regardless of which surface you launch from.

The launcher writes three config files when it creates a project — `.claude/settings.json` (canonical), `.vscode/settings.json` (extension compat), and `.claude/env` (shell wrapper) — all carrying the same values, so switching surfaces requires no reconfig.

See [`docs/CLAUDE_CODE_COMPATIBILITY.md`](docs/CLAUDE_CODE_COMPATIBILITY.md) for the surface matrix and known caveats.

## Downloads (Launcher GUI)

The launcher GUI ships as a per-OS standalone artifact on [GitHub Releases](https://github.com/hotak92/vibecoded-orchestrator/releases).

| OS                                  | Artifact                                              | Notes |
|-------------------------------------|-------------------------------------------------------|-------|
| **Windows 10/11 (x64)**             | `vct-launcher-windows-x64.exe`                        | Portable, no installer. Unsigned — SmartScreen → "More info" → "Run anyway" on first run. Code signing on backlog. |
| **Linux (x64)**                     | `*.AppImage` (portable) or `*.deb`                    | AppImage: `chmod +x VCT_Launcher_*.AppImage && ./VCT_Launcher_*.AppImage`. .deb: `sudo dpkg -i vct-launcher_*.deb`. |
| **macOS (Apple Silicon, experimental)** | `vct-launcher-macos-arm64.dmg`                    | Built unattended in CI; we have no Mac to test on. See macOS notes in [KNOWN_ISSUES.md](KNOWN_ISSUES.md). |

No GUI yet? The CLI install path (above) covers all three OSes.

## Requirements

**Required (install halts and prompts if missing):**
- **Python 3.11+** (3.12 is what we develop and CI-test on; 3.13 supported). 3.10 and older are rejected — we depend on stdlib `tomllib`.
- **Docker or Podman** (for Weaviate + Ollama containers).
- **Node.js 18+** with `npm` — only when building the launcher GUI from source AND/OR installing the Claude Code CLI. Not needed if you use the bundled prebuilt launcher and the VS Code extension.
- **A Claude subscription** (Pro / Max / Team / Enterprise) — Claude Code authenticates against your subscription; the orchestrator exists to feed it context. Free Anthropic accounts can browse `claude.ai` but cannot authenticate Claude Code.

**Recommended:**
- **VS Code with the [Claude Code extension](https://docs.anthropic.com/en/docs/claude-code/ide-integrations)** — primary target.
- **The standalone Claude Code CLI** (`npm install -g @anthropic-ai/claude-code`) — VCO uses it to summarize new KG nodes when present, falls back to a local Ollama model otherwise.

**Auto-installed when needed:**
- `pnpm` (via `npm install -g`, falls back to plain `npm`).
- Tauri Linux build deps (apt only) — needed only when building the launcher from source.
- GPU drivers — detected, not installed; the install prints download URLs if missing.

**Optional companions (install asks; default Y):**
- **[Joern](https://docs.joern.io/installation/)** — ~600 MB JVM-based code-property-graph tool. Adds CFG and PDG metrics. Skip with `--no-joern`.
- **[lean-ctx](https://github.com/yvgude/lean-ctx)** — Rust binary, MIT, zero telemetry. Compresses CLI output by 90–97%, which translates to shorter Claude context windows and lower token costs. Auto-installs via Homebrew / Cargo / AUR when available. Skip with `--no-lean-ctx`.

### Hardware

| Setup                  | Text embeddings              | Code embeddings                   | Notes                              |
|------------------------|------------------------------|-----------------------------------|------------------------------------|
| NVIDIA GPU (4 GB+ VRAM) | qwen3-embedding via Ollama   | CodeSage-Large-v2 (GPU)           | Best quality                       |
| CPU only               | qwen3-embedding via Ollama   | qwen3-embedding (fallback)        | Slower, still good quality         |
| OpenAI API key         | text-embedding-3-small       | text-embedding-3-small            | Fast, requires API key             |
| Apple Silicon          | qwen3-embedding (Metal)      | qwen3-embedding                   | Ollama uses Metal natively         |

## Install options & troubleshooting

The full flag list (`--gpu`, `--cpu-only`, `--low-resource`, `--openai-key`, `--update`, etc.) and the troubleshooting matrix live in:
- [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) — install flags, first-session walkthrough, cross-project setup
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — bypass-permissions, container / MCP issues, first-run problems

Non-interactive / CI: `python install.py --quiet --no-joern --no-containers`

## Project structure

```
vibecoded-orchestrator/
├── .claude/
│   ├── hooks/                 # 23 automation hooks (.sh + .ps1 per hook)
│   ├── scripts/               # CLI tools for KG and code graph
│   └── settings.json          # Claude Code configuration
├── claude_mcp_servers/
│   ├── weaviate_mcp/          # Semantic + graph search
│   ├── ollama_mcp/            # Local LLM, embeddings, vision
│   ├── search_mcp/            # Web + GitHub + arXiv
│   └── code_embedding_service/ # CodeSage-Large-v2 via FastAPI
├── templates/
│   ├── agents/free/           # 29 bundled agents
│   └── skills/                # 28 bundled skills
├── infrastructure/
│   ├── docker-compose.yml     # Weaviate + Ollama
│   └── docker-compose.gpu.yml # NVIDIA overlay
├── knowledge/                 # Knowledge graph nodes (your persistent memory)
├── docs/                      # Documentation
└── CLAUDE.md                  # Instructions for Claude when opening this repo
```

## Tiers

The whole repository is AGPL-3.0. The codebase you see here is the Free tier — fully functional. Optional paid modules ship as separate signed binaries delivered through the launcher; their source is not in this repo.

| Tier            | Price                | What you get                                                                                  |
|-----------------|----------------------|-----------------------------------------------------------------------------------------------|
| **Free**        | €0                   | Full orchestrator: KG, code graph, 23 hooks, 5 MCP servers, 29 agents, 28 skills. AGPL-3.0.   |
| **Pro**         | €19/month            | Free + RL-scored retrieval reranking module + coordination layer (Telegram groups + shared decision/task channels). Modules ship as separate signed binaries via the launcher. Self-host the coordination DB at no extra cost, or use our hosted instance for a small additional fee. |
| **Enterprise**  | Contact us           | Free + commercial AGPL exemption, priority support, custom SLAs. [team@vibecodedtools.com](mailto:team@vibecodedtools.com) |

Pricing finalized at module launch. The Free tier is the whole repository — no source-level dual licensing, no feature gating in the OSS code. RL retrieval falls back to cosine ordering when the Pro module isn't installed.

## Telemetry

**Telemetry is OPT-IN.** Nothing is sent unless you explicitly enable it during install (or later via Settings → Telemetry). When opted in, we collect ONE thing only:

- **Retrieval-chunk embeddings** — dense vectors (~2–8 sentences per chunk) produced by the local embedder when it indexes your KG / code graph, paired with which chunks were retrieved and which the user/agent actually used.

These are used **exclusively** to refine the RL reranker. The training pipeline is open source (`claude_mcp_servers/` + `state/rl_*`).

**Never collected, even with telemetry on:**
- Raw text of your KG nodes, code, files, or queries
- File paths, project names, repo identifiers
- API keys, secrets, environment variables
- Personal info, IP, hostname, machine identifiers

Embeddings are aggregated, irreversible representations — we can't reconstruct the source text from the vectors we receive.

**Toggle off anytime**: Settings → Telemetry → Disable, or `VCT_TELEMETRY=0` in `.env`. Disabling stops collection immediately; previously-collected data isn't deleted retroactively unless you email `privacy@vibecodedtools.it`.

## Licensing

Entire repository is **[AGPL-3.0-or-later](LICENSE)**. No source-level dual licensing.

- **Individuals and non-commercial users**: use freely under AGPL.
- **Companies running this in a service or product**: either open-source your modifications under AGPL, or buy a commercial license / Enterprise subscription. Email [team@vibecodedtools.com](mailto:team@vibecodedtools.com).

**Commercial-module model**: free source under AGPL, plus optional paid binaries delivered via signed-URL CDN. Paid modules ship as pre-compiled, Ed25519-signed artifacts gated by Lemon Squeezy — subscribers receive binaries, not source. The license validator in this repo (`VCThelpers/`) is AGPL like the rest; the trust root for paid-module access is server-side (Supabase + Lemon Squeezy + signature verification on download).

## Contributing

Small fixes and bug reports especially welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for workflow and [CLA.md](CLA.md) for the Contributor License Agreement (accepted via `git commit -s`).

## Documentation

- [Configuration philosophy](docs/CONFIGURATION.md) — minimal global, max per-project; where each config lives
- [Getting started](docs/GETTING_STARTED.md) — install, first-session walkthrough, cross-project setup
- [Troubleshooting](docs/TROUBLESHOOTING.md) — bypass-permissions, container/MCP issues, first-run problems
- [Claude Code compatibility](docs/CLAUDE_CODE_COMPATIBILITY.md) — surface matrix and caveats
- [Dependency licenses](docs/DEPENDENCY_LICENSES.md) — transitive licensing audit for the AGPL-3.0 release
- [Templates README](templates/README.md) — what `install.py` drops into `.claude/`

## Links

- [Report Issues](https://github.com/hotak92/vibecoded-orchestrator/issues)
- [vibecodedtools.it](https://vibecodedtools.it)

---

Made with VibeCoded Tools
