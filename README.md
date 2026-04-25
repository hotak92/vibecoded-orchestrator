# VibeCoded Tools — Orchestrator

**The only AI coding tool with persistent memory, code understanding, and workflow automation.**

Open source. Runs locally. Learns from you.

---

## What It Does

The orchestrator is an invisible infrastructure layer for [Claude Code](https://claude.ai/code) that solves three problems no other tool addresses together:

| Problem | How we solve it |
|---------|----------------|
| **Context amnesia** | Knowledge Graph with semantic search — Claude remembers across sessions |
| **Code blindness** | Code Graph indexes modules, classes, functions, APIs, and cross-service calls for your whole repo |
| **Workflow repetition** | 26 specialist agents + 29 skills auto-install; 16 hooks automate repetitive ops (sync, security scan, context injection) |

You use Claude Code normally. The orchestrator works in the background via hooks and MCP servers.

## Features

- **Knowledge Graph** — Markdown nodes with typed WikiLinks, stored in Weaviate with semantic embeddings (qwen3 1024-dim by default; optional OpenAI)
- **Code Graph** — AST analysis via Tree-sitter across 10+ languages: `CodeModule`, `CodeClass`, `CodeFunction`, `CodeAPI`, `CodeInteraction`
- **16 Automation Hooks** — SessionStart through Stop: context injection, auto-sync on file edits, credential scanning, KG/code graph maintenance
- **4 MCP Servers** — Weaviate (semantic search), Ollama (local LLM + embeddings), search (web + code + arXiv), code-embedding (CodeSage-Large-v2 via FastAPI)
- **26 Agents + 29 Skills** — shipped via `install.py` templates; opt-in MAO-tier specialists add 10 more agents
- **Workflow Automation** — session state tracking, plans, memory management, compaction-preserving context replay

## Quick Install

### Linux / macOS
```bash
git clone https://github.com/hotak92/vibecoded-orchestrator.git
cd vibecoded-orchestrator
./install.sh
```

### Windows (PowerShell)
```powershell
git clone https://github.com/hotak92/vibecoded-orchestrator.git
cd vibecoded-orchestrator
.\install.ps1
```

**Windows notes**:
- The installer and MCP servers run natively on Windows.
- Automation hooks (`.claude/hooks/*.sh`) are bash scripts and require **WSL2** (Windows Subsystem for Linux) to fire automatically. Without WSL, the core orchestrator still works but session-start container checks, auto-sync on file edits, and context-injection hooks do not run. Install WSL: `wsl --install`.
- PowerShell wrappers (`.ps1`) are provided for the main CLI tools: `kg-search.ps1`, `kg-sync.ps1`, `kg-info.ps1`, `code-graph-query.ps1`, `code-graph-analyze.ps1`.
- Docker Desktop is the recommended container runtime on Windows. Podman Desktop also works.

### Options
```
python install.py --gpu               # Enable NVIDIA GPU acceleration
python install.py --cpu-only          # Force CPU mode
python install.py --low-resource      # Lightest models (low-RAM/low-VRAM machines)
python install.py --openai-key KEY    # Use OpenAI embeddings instead of local
python install.py --no-containers     # Skip Docker/Podman (manual setup)
python install.py --container docker  # Force Docker (default: auto-detect)
python install.py --no-agents         # Skip installing agent templates
python install.py --no-skills         # Skip installing skill templates
python install.py --with-mao-agents   # Install the 10 MAO-tier specialist agents (requires MAO license)
python install.py --with-joern        # Install Joern for richer code-graph metrics (~600MB JVM)
python install.py --no-joern          # Skip Joern detection (don't prompt)
python install.py --skip-models       # Don't pre-pull Ollama models (do it manually later)
python install.py --update            # Re-run on an existing install (preserves .env / settings)
```

For non-interactive / CI installs:
```
python install.py --quiet --no-joern --no-containers
```

### Requirements

- **Python 3.11+**
- **Docker** or **Podman** (for Weaviate + Ollama containers)
- **Claude Code** CLI (`npm install -g @anthropic-ai/claude-code`)
- **Claude Max** subscription (for Claude Code access)
- **Node.js 18+** (for Claude CLI)

### Install Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ERROR: Python 3.11+ required` | System Python is older or missing `venv` | `sudo apt install python3.12 python3-venv` (Debian/Ubuntu); `brew install python@3.12` (macOS) |
| `Failed to create venv` | `python3-venv` package not installed (Debian-family) | `sudo apt install python3-venv` and re-run |
| `pip upgrade` fails | Network / corporate proxy / outdated CA bundle | Set `https_proxy=...` env, or upgrade your distro CA bundle |
| `No container runtime found` | Neither podman nor docker on `PATH` | Install Podman (`sudo apt install podman` / `brew install podman`) or Docker; or pass `--no-containers` and start them manually |
| Compose fails on Linux with podman | `podman-compose` and the `podman compose` plugin both missing | `pip install --user podman-compose` (or upgrade Podman to 4.x+ for the plugin) |
| Ollama timeout waiting for `/api/tags` | Container started but slow to come up on first run | Wait and re-run with `--update`; check `podman logs ollama` |
| Ollama model pull fails | No network in container, or huge model on slow link | Re-run later with `--update`; or pull manually: `curl -X POST http://localhost:11435/api/pull -d '{"name":"qwen3-embedding:0.6b"}'` |
| `code_embed` container fails on CPU-only host | GPU profile enabled despite no NVIDIA GPU | Run with `--cpu-only` (or `--low-resource`) — these set `CODE_EMBED_BACKEND=ollama` instead |
| Joern download/install fails | Network blocked or JDK install rejected | Skip with `--no-joern`; install separately later from https://docs.joern.io/installation/ |
| Hooks don't fire on Windows | Bash hooks need WSL | Install WSL (`wsl --install`) or run only the MCP/CLI parts |

For deeper issues see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

### Hardware

| Setup | Text Embeddings | Code Embeddings | Notes |
|-------|----------------|-----------------|-------|
| NVIDIA GPU (4GB+ VRAM) | qwen3-embedding via Ollama | CodeSage-Large-v2 (GPU) | Best quality |
| CPU only | qwen3-embedding via Ollama | qwen3-embedding (fallback) | Slower, good quality |
| OpenAI API key | text-embedding-3-small | text-embedding-3-small | Fast, requires API key |
| Apple Silicon | qwen3-embedding (Metal) | qwen3-embedding | Ollama uses Metal natively |

## How It Works

```
You type in Claude Code
        |
        v
[UserPromptSubmit hook]
  -> Searches Knowledge Graph for relevant context
  -> Injects results into Claude's context window
        |
        v
Claude generates response
        |
        v
[PostToolUse hooks]
  -> Auto-syncs edited files to KG / Code Graph
  -> Scans for credential leaks
  -> Runs security checks
```

## Project Structure

```
vibecoded-orchestrator/
  .claude/
    hooks/               # 16 automation hooks (SessionStart → Stop)
    scripts/             # CLI tools for KG and code graph
    settings.json        # Claude Code configuration
  claude_mcp_servers/
    weaviate_mcp/        # Semantic search MCP server
    ollama_mcp/          # Local LLM inference MCP server
    search_mcp/          # Web + code + paper search MCP server
    code_embedding_service/   # GPU code embedding service (CodeSage-Large-v2)
  templates/
    agents/free/         # 16 free-tier agents copied to .claude/agents/ at install time
    agents/mao/          # 10 MAO-tier specialist agents (opt-in)
    skills/              # 29 skills copied to .claude/skills/ at install time
  infrastructure/
    docker-compose.yml       # Weaviate + Ollama containers
    docker-compose.gpu.yml   # NVIDIA GPU overlay
  VCThelpers/           # License validator + telemetry (opt-in)
  knowledge/            # Knowledge graph nodes — your persistent memory
  config/               # Configuration templates
  docs/                 # Documentation
  CLAUDE.md             # Instructions for Claude Code when opening this repo
```

## Comparison

| Feature | Cursor | Copilot | Augment Code | Devin | **VibeCoded Tools** |
|---------|--------|---------|-------------|-------|---------------------|
| Knowledge Graph | No | No | Partial | No | **Yes** |
| Code Graph (AST) | No | No | No | No | **Yes** |
| RL-Scored Retrieval | No | No | No | No | **Pro** |
| Multi-Agent Orchestration | No | No | No | Partial | **MAO** |
| Runs 100% locally | No | No | No | No | **Yes** |
| Open source | No | No | No | No | **Yes (AGPL-3.0)** |

## Tiers

> Pricing is finalized at launch. Numbers below are the current working target, subject to change.

| Tier | Target price | What you get |
|------|-------|-------------|
| **Free** | €0 | Full orchestrator: KG, code graph, hooks, MCP servers, 16 agents, 29 skills. AGPL-3.0 licensed. |
| **Pro** | €19/mo, €149/yr, €199 lifetime (cap 100) | Free + RL-scored retrieval reranking, curated agent packs, auto-updates |
| **MAO** | €99/mo, €799/yr, €999 lifetime (cap 30) | Pro + 10 specialist agents + Tauri desktop UI + multi-agent maestro runtime |
| **Enterprise** | From €500/mo | MAO + SOC 2 compliance track, priority support, commercial AGPL exemption, custom SLAs |

Pro and MAO tiers activate additional features via a license key issued at purchase and validated against our Supabase endpoint. Free tier works fully without a key — RL retrieval simply falls back to cosine ordering. No phone-home, no feature telemetry, no telemetry at all unless you opt in.

## Licensing

This repository uses a split licensing model:

| Scope | License |
|---|---|
| Core orchestrator (this repo, minus `VCThelpers/`) | **[AGPL-3.0-or-later](LICENSE)** |
| Paid modules (`VCThelpers/`, RL pipeline when published) | **[FSL-1.1-Apache-2.0](LICENSE-FSL)** — converts to Apache-2.0 two years after each release |

**Plain-language summary**:
- Individuals and non-commercial users: use freely under AGPL.
- Companies running this in a service or product: either open-source your modifications under AGPL, or buy a commercial license. Email sales@vibecodedtools.it.
- Paid modules are source-available (you can read, audit, and modify the code) but commercial competing uses require a license. Each release automatically converts to Apache-2.0 after two years.

This is the same model used by MongoDB, Grafana, and Sentry.

## Contributing

Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines and [CLA.md](CLA.md) for the Contributor License Agreement (accepted via `git commit -s`).

## Documentation

- [Configuration philosophy](docs/CONFIGURATION.md) — minimal global, max per-project; where each config lives
- [Troubleshooting](docs/TROUBLESHOOTING.md) — bypass-permissions, container/MCP issues, first-run problems
- [User journey](docs/USER_JOURNEY.md) — what using the orchestrator looks like across a first install and multi-project use
- [Positioning](docs/POSITIONING.md) — target market + competitive framing (launch asset, not user docs)
- [Dependency licenses](docs/DEPENDENCY_LICENSES.md) — transitive licensing audit for the AGPL-3.0 release
- [Templates README](templates/README.md) — what agents and skills install.py will drop into `.claude/`

## Links

- [Report Issues](https://github.com/hotak92/vibecoded-orchestrator/issues)
- [VibeCoded Tools](https://vibecodedtools.it)

---

Made with VibeCoded Tools
