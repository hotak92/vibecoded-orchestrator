# VibeCoded Tools — Orchestrator

**Persistent memory, code-graph search, and workflow automation for [Claude Code](https://claude.ai/code).**

Open source. Runs locally. Learns from how you work.

---

## What it does

An infrastructure layer for Claude Code that addresses three things no single tool covers today:

| Problem | What we add |
|---------|----------------|
| Context amnesia between sessions | Knowledge Graph with semantic search — Claude reads it on session start |
| Code blindness | Code graph indexes modules, classes, functions, APIs, and cross-service calls |
| Repetitive setup and ops | 19 free agents + 28 skills installed into `.claude/`; 20 hooks for sync, secret scans, context injection. Optional MAO add-on adds 10 specialist agents. |

You use Claude Code the way you already do. The orchestrator runs in the background via hooks and MCP servers.

## Features

- **Knowledge Graph** — Markdown nodes with typed WikiLinks, indexed in Weaviate with semantic embeddings (qwen3 1024-dim by default; optional OpenAI)
- **Code Graph** — AST analysis via Tree-sitter across 10+ languages: `CodeModule`, `CodeClass`, `CodeFunction`, `CodeAPI`, `CodeInteraction`
- **20 automation hooks** — SessionStart through Stop: context injection, auto-sync on file edits, credential scans, KG/code-graph maintenance. The hooks live in this repo's `.claude/hooks/` and fire when you work inside the orchestrator project. Per-target-project hook distribution is on the roadmap; today, `install.py` copies agents and skills into `.claude/`, but not hooks.
- **4 MCP servers** — Weaviate (semantic search), Ollama (local LLM + embeddings), search (web + code + arXiv), code-embedding (CodeSage-Large-v2 via FastAPI)
- **19 free agents + 28 skills** — shipped via `install.py` templates. The opt-in MAO add-on installs 10 more specialist agents.
- **Workflow plumbing** — session state tracking, plans, memory management, compaction-preserving context replay

## Downloads (Launcher GUI — v0.1.0+)

The launcher GUI ships as a per-OS standalone artifact on [GitHub
Releases](https://github.com/hotak92/vibecoded-orchestrator/releases). It's
the path of least resistance if you'd rather not touch the CLI; the
backend (Python + containers) is installed by the launcher's on-screen
wizard on first run.

| OS | Artifact | Notes |
|---|---|---|
| **Windows 10/11 (x64)** | `vct-launcher-windows-x64.exe` | Portable, no installer. **First run**: SmartScreen will warn "Windows protected your PC" because the build is unsigned. Click **More info** → **Run anyway**. Code signing is on the v0.1.1 backlog. |
| **Linux (x64)** | `*.AppImage` (portable) or `*.deb` (Debian/Ubuntu) | AppImage: `chmod +x VCT_Launcher_*.AppImage && ./VCT_Launcher_*.AppImage`. .deb: `sudo dpkg -i vct-launcher_*.deb`. |
| **macOS (Apple Silicon, experimental)** | `vct-launcher-macos-arm64.dmg` | See "macOS (experimental)" below. We have no Mac to test on, so quality is best-effort. |

**No GUI yet?** The CLI install path (next section) works on all three OSes.

### macOS (experimental)

We can't test on Mac. The .dmg is built unattended in CI. Try at your own risk.

1. Download `vct-launcher-macos-arm64.dmg` from [Releases](https://github.com/hotak92/vibecoded-orchestrator/releases).
2. Open the .dmg, drag **VCT Launcher** into Applications.
3. **First run**: Gatekeeper will say "VCT Launcher is damaged and can't be opened". That's the unsigned-binary warning, not actual damage. Strip the quarantine attribute:
   ```bash
   xattr -cr /Applications/VCT\ Launcher.app
   ```
4. Open the app from /Applications.

If it works, please open an issue saying "v0.1.0 worked on macOS [version] [arch]". That helps us prioritise Apple Developer enrollment.
If it doesn't, please open an issue with macOS version, architecture (`uname -m`), and the error message.

Apple Developer enrollment + notarization are on the post-launch backlog. Intel Mac users: build from source for v0.1.0; a Universal binary is planned for v0.1.1.

## Quick Install

**Time budget**: ~5 min interactive setup + 10–30 min initial container/model
downloads on first run (~5 GB total — Weaviate image + Ollama qwen3 weights;
GPU mode also pulls CodeSage-Large-v2 ~2.5 GB). Subsequent installs reuse the
cached images and finish in seconds.

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
| Existing Weaviate / Ollama on default ports | The installer detects foreign services on `8081` / `11435` / `11440` and runs ours on a free alt-port by default — no collision, no pollution. Override with `--on-conflict adopt\|abort`. See [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md#coexisting-with-other-weaviate-or-ollama-installs). | — |

For deeper issues see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

### Hardware

| Setup | Text Embeddings | Code Embeddings | Notes |
|-------|----------------|-----------------|-------|
| NVIDIA GPU (4GB+ VRAM) | qwen3-embedding via Ollama | CodeSage-Large-v2 (GPU) | Best quality |
| CPU only | qwen3-embedding via Ollama | qwen3-embedding (fallback) | Slower, good quality |
| OpenAI API key | text-embedding-3-small | text-embedding-3-small | Fast, requires API key |
| Apple Silicon | qwen3-embedding (Metal) | qwen3-embedding | Ollama uses Metal natively |

## Compatibility

Works with all three Claude Code surfaces. Hooks fire, agents and skills
load, MCP servers connect regardless of which one you launch from:

- **Claude Code CLI** (`claude` binary): Per-project env via the
  canonical `.claude/settings.json` `env` block (auto-generated by
  the launcher). The shell-sourceable `.claude/env` file is also
  written for users who want to extend env via the bundled
  `tools/claude` wrapper or direnv.
- **VS Code extension**: Per-project env via the same
  `.claude/settings.json` `env` block, plus `.vscode/settings.json`'s
  `claude-code.env` for compatibility (both auto-generated by the
  launcher with identical values).
- **Claude Desktop app** (macOS / Windows): Per-project env via
  `.claude/settings.json` `env` (Bug 30 — the only path that reaches
  Desktop app users). MCP servers connect via `~/.claude.json`.
  Linux Desktop app is not yet shipped by Anthropic; Linux users
  should use the CLI surface.

The launcher writes three files when it creates a project:
`.claude/settings.json` (canonical), `.vscode/settings.json` (extension
compat), and `.claude/env` (shell wrapper). All three carry the same
values, so you can switch surfaces without reconfiguring.

See [`docs/CLAUDE_CODE_COMPATIBILITY.md`](docs/CLAUDE_CODE_COMPATIBILITY.md)
for the full surface matrix, known caveats, and direnv alternative.

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
    hooks/               # 20 automation hooks (SessionStart → Stop)
    scripts/             # CLI tools for KG and code graph
    settings.json        # Claude Code configuration
  claude_mcp_servers/
    weaviate_mcp/        # Semantic search MCP server
    ollama_mcp/          # Local LLM inference MCP server
    search_mcp/          # Web + code + paper search MCP server
    code_embedding_service/   # GPU code embedding service (CodeSage-Large-v2)
  templates/
    agents/free/         # 19 free-tier agents copied to .claude/agents/ at install time
    agents/mao/          # 10 MAO-tier specialist agents (opt-in)
    skills/              # 28 skills copied to .claude/skills/ at install time
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
| **Free** | €0 | Full orchestrator: KG, code graph, hooks, MCP servers, 19 agents, 28 skills. AGPL-3.0 licensed. |
| **Pro** | €19/mo, €149/yr, €199 lifetime (cap 100) | Free + RL-scored retrieval reranking, curated agent packs, auto-updates |
| **MAO** | TBD — sign up for the waitlist | Pro + 10 specialist agents + Tauri desktop UI + multi-agent maestro runtime |
| **Enterprise** | From €500/mo | MAO + SOC 2 compliance track, priority support, commercial AGPL exemption, custom SLAs |

Pro and MAO tiers activate additional features via a license key issued at purchase, validated against our Supabase endpoint. Free tier works fully without a key — RL retrieval falls back to cosine ordering. No phone-home, no feature telemetry, no telemetry at all unless you opt in.

## Licensing

The entire repository is licensed under **[AGPL-3.0-or-later](LICENSE)**. There is no source-level dual licensing.

**Plain-language summary**:
- Individuals and non-commercial users: use freely under AGPL.
- Companies running this in a service or product: either open-source your modifications under AGPL, or buy a commercial license / subscription. Email team@vibecodedtools.com.

**How the commercial model works**: free source under AGPL, plus paid binaries delivered via signed-URL CDN. Pro and MAO are subscriptions distributed as pre-compiled, Ed25519-signed artifacts gated by Lemon Squeezy — subscribers receive binaries, not source. No source license applies to those artifacts because no source is published for them. The license validator and telemetry components in this repo (`VCThelpers/`) are AGPL like the rest; the trust root for paid-module access is server-side (Supabase + Lemon Squeezy + signature verification on download).

## Recommended Companions

**[lean-ctx](https://github.com/yvgude/lean-ctx)** — MIT license, zero telemetry. Wraps common CLI commands (`git`, `npm`, `pip`, `grep`, `ls`, etc.) and compresses their output by 90-97% by stripping boilerplate, progress bars, and redundant lines. This translates directly to shorter Claude context windows, lower token costs, and faster responses. The installer detects lean-ctx automatically and wires it into Claude Code's non-interactive Bash subprocesses via `BASH_ENV`; if you install it later, re-run `install.py` to activate. Install: `cargo install lean-ctx` or `curl -fsSL https://leanctx.com/install.sh | sh`.

## Contributing

Contributions welcome — small fixes and bug reports especially. See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and [CLA.md](CLA.md) for the Contributor License Agreement (accepted via `git commit -s`).

## Documentation

- [Configuration philosophy](docs/CONFIGURATION.md) — minimal global, max per-project; where each config lives
- [Troubleshooting](docs/TROUBLESHOOTING.md) — bypass-permissions, container/MCP issues, first-run problems
- [Getting started](docs/GETTING_STARTED.md) — install, first-session walkthrough, and cross-project setup
- [Positioning](docs/POSITIONING.md) — target market + competitive framing (launch asset, not user docs)
- [Dependency licenses](docs/DEPENDENCY_LICENSES.md) — transitive licensing audit for the AGPL-3.0 release
- [Templates README](templates/README.md) — what agents and skills install.py will drop into `.claude/`

## Links

- [Report Issues](https://github.com/hotak92/vibecoded-orchestrator/issues)
- [VibeCoded Tools](https://vibecodedtools.it)

---

Made with VibeCoded Tools
