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

Claude Code is very good at writing code and very bad at remembering anything about your project. VCO is the layer that fixes the second half. Seven systems, one theme: stop re-explaining, stop re-searching, stop re-configuring.

**Knowledge Graph — Claude stops forgetting your project.**
Every session starts from zero: you paste the same architecture summary, the same "we decided X because Y", for the tenth time. VCO keeps a persistent knowledge graph — plain markdown notes on your disk, semantically indexed in a local vector database — that gets searched and injected into Claude's context before it answers. The moment you feel it: session two opens and Claude already knows the decision you made in session one, without you saying a word.

**Code Graph — Claude stops grepping blind.**
"What calls this function?" normally turns into five rounds of grep and re-reading files it already read yesterday. VCO extracts your modules, classes, functions, API endpoints, and cross-service calls into a graph Claude can query by purpose ("the auth middleware") rather than by exact name. You feel it when Claude names the three call-sites of a function before opening a single file.

**Automation hooks — the bookkeeping happens without you asking.**
Keeping context notes fresh, re-indexing what you just edited, catching a credential before it lands in a file — these are chores you'd have to remember to ask for, every time. Dozens of automation hooks fire at the right lifecycle moments instead: relevant knowledge is injected when you submit a prompt, edited notes and code re-index themselves, context survives a `/compact` instead of evaporating, and written files are scanned for leaked secrets. You feel this one as an absence — the maintenance requests you never had to type.

**MCP servers — Claude gets tools, not just files.**
Out of the box, Claude can only read what you point it at. VCO registers local MCP servers that give Claude callable tools: semantic search across your knowledge and docs, structural queries over your code graph, academic-paper search, browser automation, and opt-in diagram tools. All local, no per-tool API keys. You feel it when you ask "how did we handle retries?" and Claude runs an actual search over everything you've ever written down instead of guessing.

**Secrets — credentials resolve, they never get pasted.**
The usual failure mode: an agent needs a GitHub token, so it ends up in a chat message, an environment dump, or a committed `.env`. VCO stores secrets in your OS keychain (plus a permission-locked file store) and gives agents a resolver: a credential is injected into the child process that needs it, by key name, without ever being printed. Grepping the environment for tokens is blocked by a hook. You feel it when `git push` just works and the token never appears in the transcript.

**RL retrieval reranking (Pro) — search that learns what you actually use.**
Vector search returns plausible results; the one you needed is ranked fifth. The Pro module tracks which retrieved chunks actually get used and reranks future retrievals accordingly, so relevance improves the longer you work. Without a license nothing breaks — retrieval uses plain cosine ordering.

**Launcher GUI — one control panel instead of config sprawl.**
Everything above is per-project configuration, containers, collections, and toggles — hand-maintaining that stack would be a project in itself. The launcher GUI is where you add a project (it indexes existing knowledge, docs, and source code in the background), enable or disable agents/skills/hooks per project, manage secrets, watch service health, and update in place without losing your customizations. You feel it when adding an existing repo takes one click and comes out fully indexed.

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

`first-install.{sh,command,bat}` is a thin OS shim that (1) finds a usable Python 3.11+ (offering to install one via Homebrew / apt / dnf / pacman / zypper / apk / winget if missing), (2) runs a read-only system-detection prepass that writes a diagnostic report to `state/logs/bootstrap-prepass.json` — no side effects, and a failure there never blocks the install — then (3) runs `install.py` for the canonical install. On success the launcher GUI auto-starts (pass `--no-auto-launch` to skip). Root install and per-project bundle install share one engine — the orchestrator's own `.claude/` goes through the same code path the launcher uses for your projects. Full detail: [`docs/INSTALL_ARCHITECTURE_v2.md`](docs/INSTALL_ARCHITECTURE_v2.md) and [`docs/INSTALL_PARITY.md`](docs/INSTALL_PARITY.md).

The entry points run end-to-end in CI on Ubuntu 22.04/24.04, Fedora 40, macOS 14, and Windows on every push to main and on installer-touching PRs; a red run blocks release tags.

Allow ~5–10 min plus first-run image downloads (~5 GB: Weaviate + Ollama qwen3 weights, +2.5 GB if GPU mode pulls CodeSage-Large-v2).

If anything fails partway through, paste [`docs/INSTALL_RECOVERY.md`](docs/INSTALL_RECOVERY.md) into Claude Code — it walks Claude through diagnosing and finishing the build.

## Who this is for

If you use **VS Code (or any IDE) with [Claude Code](https://claude.ai/code)**, this is for you: the orchestrator indexes your knowledge and your codebase in the background and feeds Claude richer context every time you talk to it. If you don't use Claude Code, the KG, code graph, MCP servers, and launcher GUI all work standalone, but the value is highest when there's an AI client driving them. See [Compatibility](#compatibility).

## Where this fits

VCO sits on top of Claude Code rather than replacing your AI assistant. The comparison below is for buyers choosing how to spend their AI-coding attention — VCO + Claude Code, vs. an all-in-one closed product, vs. another open-source extension.

| Dimension | Cursor | GitHub Copilot | Augment | Devin | OpenAI Codex CLI | Aider | Cline | **VCO + Claude Code** |
|---|---|---|---|---|---|---|---|---|
| Open source | No | No | Partial (some components) | No | Yes (CLI) | Yes (MIT) | Yes (Apache-2.0) | **Yes (AGPL-3.0)** |
| Runs locally (no code in vendor cloud) | Partial (cloud Composer) | No | No | No | Partial (CLI local, API cloud) | Yes | Yes | **Yes** |
| Persistent memory across sessions | No | Yes (Copilot Memory, repo-scoped, 28-day expiry) | Partial (team memory) | Partial (session-bound) | No | No | No | **Yes (KG, no expiry)** |
| Code graph (AST, callers, APIs) | Partial (file index, opaque) | Partial (vector index) | Yes (Context Engine) | Yes | No | Yes (repomap) | Partial (Tree-sitter, not persisted) | **Yes (persisted graph)** |
| Bring your own LLM subscription | Partial (chat only) | No | Partial (BYO agent, not LLM) | No | Yes (OpenAI) | Yes (75+ providers) | Yes (30+ providers) | **Yes (Claude)** |
| User-extensible (hooks / agents / skills) | Yes (hooks + skills, no marketplace) | No | Limited (MCP only) | No | Limited (skills as prompts) | Yes (open source) | Yes (open source) | **Yes (45 hooks, 53 skills, 44 agents)** |
| Pricing model | $20/mo SaaS | $10–20/user/mo | BYOA + cloud compute | $20/mo + usage | Per-token OpenAI | Free + your LLM | Free + your LLM | **Free + your Claude sub; €19/mo Pro** |
| Polished v1 product (vs. alpha) | Yes | Yes | Yes | Yes | Yes | Yes | Yes | **No — alpha** |

**Reading the table**: Cursor, Copilot, Augment, and Devin are closed all-in-ones — they ship the editor, the AI, the context layer, and the cloud, bundled. Aider, Cline, and Codex CLI are open-source / BYOL but lack persistent memory and (in most cases) a structural code graph. VCO is the combination that doesn't otherwise exist: open-source, local, BYO-Claude-subscription, *and* it has both persistent memory and a code graph. The trade you're making for that is product polish — VCO is alpha, the others are stable v1+.

Competitor products move fast — verify the row you care about before quoting.

## Compatibility

Works with all three Claude Code surfaces. Primary target is the **VS Code extension**; the Desktop app and standalone CLI are also supported. Hooks fire, agents and skills load, and MCP servers connect regardless of which surface you launch from.

The launcher writes two config files when it creates a project — `.claude/settings.json` (canonical, read by every Claude Code surface AND propagated to MCP subprocesses) and `.claude/env` (POSIX shell-sourceable copy) — both carrying the same values, so switching surfaces requires no reconfig.

See [`docs/CLAUDE_CODE_COMPATIBILITY.md`](docs/CLAUDE_CODE_COMPATIBILITY.md) for the surface matrix and known caveats.

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

When you click **Add project** in the launcher GUI, the project is usable the moment bundle install finishes — and three background tasks fan out in parallel, so a repo with pre-existing `knowledge/**/*.md`, `docs/**/*.md`, or source code lands fully indexed without manual CLI invocations:

1. **Code graph build** — structural analysis over the project root, populating the `CodeModule` / `CodeClass` / `CodeFunction` / `CodeAPI` / `CodeInteraction` collections.
2. **KG sync** — walks `knowledge/**/*.md` and `docs/**/*.md` and embeds them into the per-project Weaviate collections.
3. **KG summaries** — LLM-written node summaries consumed by `hybrid_search`'s `summary` tier (backend fallback: `claude` CLI → local Ollama → `ANTHROPIC_API_KEY` → skip; summaries backfill later as you edit nodes if no backend is available).

Each task gets a status banner on the project page (`pending` / `running` / `failed`, with `Show details` and `Retry`); runs interrupted by a crash are marked failed and retryable, and retries are cheap no-ops for already-indexed content. Full walkthrough: [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md#background-tasks-the-launcher-fans-out-on-add-project).

## Under the hood

- **Knowledge Graph** — Obsidian-style markdown nodes with typed WikiLinks, indexed in Weaviate via qwen3 embeddings (1024-dim, local). Optional OpenAI embeddings.
- **Code Graph** — per-language structural analysis across 10+ languages, populating `CodeModule`, `CodeClass`, `CodeFunction`, `CodeAPI`, `CodeInteraction` collections. Call edges (`callers` / `path` queries) come from Python's `ast`; installing the optional `codegraph-ts` extra (`pip install '.[codegraph-ts]'`, opt out at install with `VCT_SKIP_CODEGRAPH_TS=1`) adds tree-sitter grammars so call edges extend to rust, go, javascript, typescript, java, c#, c/c++, ruby, lua, and bash. Without the extra those languages simply get no call edges (the rest of the graph is unaffected).
- **45 automation hooks** — context injection on prompt submit, KG/code-graph auto-sync on file edit, credential scans, compaction-preserving context replay, security checks. 43 are event-registered in `settings.json`; 2 more (`kg-sync-on-edit`, `code-graph-incremental`) run as helpers invoked by sibling hooks. Every hook ships as `.sh` (Linux/macOS) with a native `.ps1` sibling (Windows). The `vct-hub` background service resolves per-project config for hooks, MCPs, and scripts.
- **MCP servers (default install)** — 4 registered in `~/.claude.json` at install: `weaviate-kg` (semantic + graph search + code graph) and `search` (academic papers via OpenAlex + arXiv) are **enabled by default per project**; `mermaid` and `excalidraw` are **registered but default-disabled** — connected in `claude mcp list`, tools not callable until you opt in via the launcher's Diagrams tab. A fifth MCP — `playwright` — is **enabled by default**, invoked via `npx -y @playwright/mcp@latest` (pre-cached at install; opt out with `VCT_SKIP_PLAYWRIGHT=1`). The `vct-coordination` MCP is **Pro-tier**. All local, no per-tool API keys. Ollama (Weaviate vectorizer + embedding fallback) and the code-embedding FastAPI service on port 11440 are backend infrastructure, not MCPs.
- **Secrets primitive** — OS-keychain storage (launcher-managed) plus a chmod-600 file store under `~/.vct-secrets/`, resolved through the `vct` CLI and the `vct-hub` service. Agents inject credentials into child processes by key name (`vct exec --secret KEY=ENV_VAR -- cmd`) instead of printing them; a hook blocks env-grepping for tokens. See [`docs/VCT_SECRETS_PRIMITIVE.md`](docs/VCT_SECRETS_PRIMITIVE.md).
- **44 agents + 53 skills** — shipped via `install.py` templates. Agents handle planning, coding, testing, doc maintenance, KG navigation, code-graph health. Skills cover security review, debugging, architecture, RAG advisory, accessibility, etc.
- **Workflow plumbing** — session state tracking (`CONTEXT_STATE.md`), plan files, memory management, pre-/post-compact context replay so a `/compact` doesn't lose your thread.

## Downloads (Launcher GUI)

The launcher GUI ships as a per-OS standalone artifact on [GitHub Releases](https://github.com/hotak92/vibecoded-orchestrator/releases).

| OS                                  | Artifact                                              | Notes |
|-------------------------------------|-------------------------------------------------------|-------|
| **Windows 10/11 (x64)**             | `vct-launcher-windows-x64.exe`                        | Portable, no installer. Unsigned — SmartScreen → "More info" → "Run anyway" on first run. Code signing on backlog. |
| **Linux (x64)**                     | `*.AppImage` (portable) or `*.deb`                    | AppImage: `chmod +x VCT_Launcher_*.AppImage && ./VCT_Launcher_*.AppImage`. .deb: `sudo dpkg -i vct-launcher_*.deb`. |
| **macOS (Apple Silicon)**           | `vct-launcher-macos-arm64.zip` (dist binary at `launcher/dist/macos-arm64/vct-launcher`) | Tier-2. Ad-hoc codesigned, not notarized — Gatekeeper warns once. See [docs/macos-install.md](docs/macos-install.md). |

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
- **[lean-ctx](https://github.com/yvgude/lean-ctx)** — Rust binary, MIT, zero telemetry. Compresses CLI output by 90–97%, which translates to shorter Claude context windows and lower token costs. Auto-installs via Homebrew / Cargo / AUR when available. Skip with `--no-lean-ctx`.

### Hardware

The installer auto-selects three backends — code embeddings, KG / text embeddings, and KG-summary generation — based on detected VRAM / RAM / CPU cores. All three can be overridden later from the launcher's Preferences panel.

**Code embeddings** (vectorise functions, classes, modules; populates `CodeModule`, `CodeFunction`, `CodeClass`, etc.):

| Tier                       | Backend     | Model                                                       | Notes                                                              |
|----------------------------|-------------|-------------------------------------------------------------|--------------------------------------------------------------------|
| GPU, VRAM ≥ 12 GB          | GPU service | `codesage-large-v2` (2048-dim)                              | Best quality. Code-specialised, served from `code_embedding_service`. |
| GPU, VRAM ≥ 6 GB           | Ollama      | `qwen3-embedding:0.6b` (1024-dim)                           | Generalist; runs comfortably alongside other GPU workloads.        |
| GPU, VRAM > 2 GB           | Ollama      | `unclemusclez/jina-embeddings-v2-base-code:latest` (768-dim)| Code-specialised, low VRAM footprint.                              |
| CPU, RAM > 24 GB & 8+ cores¹| Ollama      | `qwen3-embedding:0.6b`                                      | Pure-CPU path on capable workstations.                             |
| CPU, otherwise             | Ollama      | `unclemusclez/jina-embeddings-v2-base-code:latest`          | Floor — runs on anything that can run Ollama.                      |
| OpenAI API (opt-in)        | OpenAI      | `text-embedding-3-small` (1536-dim)                         | Override only; costs per embedding. Configure via `--openai-key`.  |

**KG / text embeddings** (vectorise knowledge nodes + docs; populates `<KG_COLLECTION>`, shared KG, `<DEVELOPMENT_COLLECTION>`):

| Tier                       | Backend | Model                                       | Notes                                                            |
|----------------------------|---------|---------------------------------------------|------------------------------------------------------------------|
| GPU, VRAM > 8 GB           | Ollama  | `qwen3-embedding:0.6b` (1024-dim)           | Default; matches the existing KG schema slot `qwen3_embed`.      |
| GPU, VRAM 4–8 GB¹          | Ollama  | `snowflake-arctic-embed2:latest` (1024-dim) | Same dims as qwen3 → same schema slot, smaller working set.      |
| GPU, VRAM < 4 GB (or unsupported) | Ollama | `snowflake-arctic-embed2:latest`     | Falls through to CPU treatment.                                  |
| CPU, RAM > 24 GB & 8+ cores¹| Ollama  | `qwen3-embedding:0.6b`                      | Pure-CPU path on capable workstations.                           |
| CPU, otherwise             | Ollama  | `snowflake-arctic-embed2:latest`            | Floor for low-RAM / low-core hosts.                              |
| OpenAI API (opt-in)        | OpenAI  | `text-embedding-3-small` (1536-dim)         | Override only; configure via `--openai-key`.                     |

**KG-summary generation** (LLM-written descriptions + per-chunk summaries used by `hybrid_search`'s `detail="summary"` tier — search still works without these, just with raw KG content):

| Tier                                    | Backend          | Model              | Notes                                                              |
|-----------------------------------------|------------------|--------------------|--------------------------------------------------------------------|
| `claude` CLI on PATH (authenticated)    | Claude CLI       | `haiku`            | Always preferred; costs come out of your Claude subscription quota.|
| GPU, VRAM ≥ 16 GB                       | Ollama           | `qwen3.5:9b`       | Highest local quality.                                             |
| GPU, VRAM ≥ 6 GB                        | Ollama           | `gemma4:e4b`       | Fast, low VRAM footprint.                                          |
| CPU, RAM ≥ 12 GB & 6+ cores             | Ollama           | `gemma4:e4b`       | Pure-CPU fallback.                                                 |
| Anything below those tiers              | _none_           | _none_             | Summaries skipped; raw KG content still embedded + searchable.     |
| OpenAI API (opt-in, requires consent)   | OpenAI           | `gpt-4o-mini`      | Off by default; toggle via Preferences → KG Summaries. Cost warning surfaced on enable. |

¹ CPU core thresholds count **physical cores** (not logical/SMT threads), and the 24 GB RAM / 8 GB VRAM cutoffs are strict `>` boundaries — this avoids performance cliffs on hardware sitting exactly at a boundary.

## Install options & troubleshooting

The full flag list (`--gpu`, `--cpu-only`, `--low-resource`, `--openai-key`, `--update`, etc.) and the troubleshooting matrix live in:
- [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) — install flags, first-session walkthrough, cross-project setup
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — bypass-permissions, container / MCP issues, first-run problems

Non-interactive / CI: `python install.py --quiet --no-containers`

## Project structure

```
vibecoded-orchestrator/
├── .claude/
│   ├── hooks/                 # 45 automation hooks (.sh + .ps1 per hook)
│   ├── scripts/               # CLI tools for KG and code graph
│   └── settings.json          # Claude Code configuration
├── claude_mcp_servers/
│   ├── weaviate_mcp/          # Semantic + graph search (default)
│   ├── search_mcp/            # Academic-paper search via OpenAlex + arXiv (default)
│   └── code_embedding_service/ # CodeSage-Large-v2 via FastAPI (backend service, not an MCP)
│   # mermaid + excalidraw MCPs are registered at install but default-disabled
│   # per project — opt in via the launcher's Diagrams tab
├── templates/
│   ├── agents/free/           # 44 bundled agents
│   ├── skills/                # 53 bundled skills
│   └── hooks/                 # Hook sources rendered into .claude/hooks/ at install
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
| **Free**        | €0                   | Full orchestrator: KG, code graph, 45 hooks, 44 agents, 53 skills, all default MCP servers (see [Under the hood](#under-the-hood)). AGPL-3.0. |
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

**Toggle off anytime**: Settings → Telemetry → Disable, or `VCT_TELEMETRY=0` in `.env`. Disabling stops collection immediately; previously-collected data isn't deleted retroactively unless you email `team@vibecodedtools.com`.

## Licensing

Entire repository is **[AGPL-3.0-or-later](LICENSE)**. No source-level dual licensing.

- **Individuals and non-commercial users**: use freely under AGPL.
- **Companies running this in a service or product**: either open-source your modifications under AGPL, or buy a commercial license / Enterprise subscription. Open the launcher → Settings → Activate License (the OnboardingWizard walks you through the in-product purchase flow).

**Commercial-module model**: free source under AGPL, plus optional paid binaries delivered via signed-URL CDN. Paid modules ship as pre-compiled, Ed25519-signed artifacts gated by Lemon Squeezy — subscribers receive binaries, not source. The license validator in this repo (`VCThelpers/`) is AGPL like the rest; the trust root for paid-module access is server-side (Supabase + Lemon Squeezy + signature verification on download).

## Contributing

Small fixes and bug reports especially welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for workflow and [CLA.md](CLA.md) for the Contributor License Agreement (accepted via `git commit -s`).

## Documentation

- [Configuration philosophy](docs/CONFIGURATION.md) — minimal global, max per-project; where each config lives
- [Getting started](docs/GETTING_STARTED.md) — install, first-session walkthrough, cross-project setup
- [Troubleshooting](docs/TROUBLESHOOTING.md) — bypass-permissions, container/MCP issues, first-run problems
- [Claude Code compatibility](docs/CLAUDE_CODE_COMPATIBILITY.md) — surface matrix and caveats
- [Install parity](docs/INSTALL_PARITY.md) — the shared bundle-install engine behind root install, add-project, and update
- [Secrets primitive](docs/VCT_SECRETS_PRIMITIVE.md) — keychain + file store, `vct` CLI, credential injection
- [Dependency licenses](docs/DEPENDENCY_LICENSES.md) — transitive licensing audit for the AGPL-3.0 release
- [Templates README](templates/README.md) — what `install.py` drops into `.claude/`

## Links

- [Report Issues](https://github.com/hotak92/vibecoded-orchestrator/issues)
- [vibecodedtools.it](https://vibecodedtools.it)

---

Made with VibeCoded Tools
