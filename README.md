# VibeCoded Tools — Orchestrator

**The only AI coding tool with persistent memory, code understanding, and workflow automation.**

Open source. Runs locally. Learns from you.

---

## What It Does

The orchestrator is an invisible infrastructure layer for [Claude Code](https://claude.ai/code) that solves three problems no other tool addresses together:

| Problem | How We Solve It |
|---------|----------------|
| **Context amnesia** | Knowledge Graph with semantic search — Claude remembers across sessions |
| **Wasted tokens** | Automated context injection — relevant info is surfaced proactively |
| **No code understanding** | Code Graph with 5 entity types — Claude understands your codebase structurally |

You use Claude Code normally. The orchestrator works in the background via hooks.

## Features

- **Knowledge Graph** — Markdown nodes with typed WikiLinks, stored in Weaviate with semantic embeddings
- **Code Graph** — AST analysis across 10+ languages: modules, classes, functions, APIs, cross-service calls
- **17+ Hook Scripts** — Automated context injection, security scanning, KG/code graph sync
- **MCP Servers** — Semantic search, local LLM inference, code embedding
- **Workflow Automation** — Session state tracking, plans, memory management, compaction preservation

## Quick Install

### Linux / macOS
```bash
git clone https://github.com/VibeCoded-Tools/orchestrator.git
cd orchestrator
./install.sh
```

### Windows (PowerShell)
```powershell
git clone https://github.com/VibeCoded-Tools/orchestrator.git
cd orchestrator
.\install.ps1
```

**Windows notes**:
- The installer and MCP servers run natively on Windows.
- Automation hooks (`.claude/hooks/*.sh`) are bash scripts and require **WSL2** (Windows Subsystem for Linux) to fire automatically. Without WSL, the core orchestrator still works but session-start container checks, auto-sync on file edits, and context-injection hooks do not run. Install WSL: `wsl --install`.
- PowerShell wrappers (`.ps1`) are provided for the main CLI tools: `kg-search.ps1`, `kg-sync.ps1`, `kg-info.ps1`, `code-graph-query.ps1`, `code-graph-analyze.ps1`.
- Docker Desktop is the recommended container runtime on Windows. Podman Desktop also works.

### Options
```
python install.py --gpu              # Enable NVIDIA GPU acceleration
python install.py --cpu-only         # Force CPU mode
python install.py --openai-key KEY   # Use OpenAI embeddings instead of local
python install.py --no-containers    # Skip Docker/Podman (manual setup)
python install.py --container docker # Force Docker (default: auto-detect)
```

### Requirements

- **Python 3.11+**
- **Docker** or **Podman** (for Weaviate + Ollama containers)
- **Claude Code** CLI (`npm install -g @anthropic-ai/claude-code`)
- **Claude Max** subscription (for Claude Code access)
- **Node.js 18+** (for Claude CLI)

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
orchestrator/
  .claude/
    hooks/          # 16 automation hooks (SessionStart → Stop)
    scripts/        # CLI tools for KG and code graph
    settings.json   # Claude Code configuration
  claude_mcp_servers/
    weaviate_mcp/   # Semantic search MCP server
    ollama_mcp/     # Local LLM inference MCP server
    search_mcp/     # Web + code + paper search MCP server
    code_embedding_service/  # GPU code embedding service
  infrastructure/
    docker-compose.yml       # Weaviate + Ollama containers
    docker-compose.gpu.yml   # NVIDIA GPU overlay
  knowledge/        # Knowledge graph nodes (your persistent memory)
  config/           # Configuration templates
  docs/             # Documentation
  CLAUDE.md         # Instructions for Claude Code
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

| Tier | Price | What You Get |
|------|-------|-------------|
| **Free** | €0 | Everything above. Full orchestrator, KG, code graph, hooks, MCP servers. Watermark on generated files (trivially removable). |
| **Pro** | €19/mo, €149/yr, €199 lifetime (cap 100) | RL-scored retrieval that learns from your usage, curated agent packs, auto-updates, watermark off |
| **MAO** | €99/mo, €799/yr, €999 lifetime (cap 30) | Everything in Pro + multi-agent orchestrator with 32+ specialized agents, Tauri desktop UI |
| **Enterprise** | From €500/mo | MAO + SOC 2 compliance, priority support, commercial AGPL exemption, custom SLAs |

Pro and MAO tiers activate additional features via a license key issued at purchase. Free tier works fully without a key — RL retrieval simply falls back to cosine ordering.

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

## Links

- [Documentation](docs/)
- [Report Issues](https://github.com/VibeCoded-Tools/orchestrator/issues)
- [VibeCoded Tools](https://vibecodedtools.it)

---

Made with VibeCoded Tools
