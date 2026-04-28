# VibeCoded Tools — Orchestrator

**Persistent memory, code-graph search, and workflow automation for [Claude Code](https://claude.ai/code).**

Open source. Runs locally. Learns from how you work.

> **Requirements at a glance**
> - **Python 3.11 or newer** (3.12 recommended; 3.13 also supported)
> - **Docker** or **Podman**
> - **Claude Code CLI** + **Claude Max** subscription
> - **Node.js 18+** with `npm` (only when building the launcher GUI from source — not needed if using the bundled prebuilt binary)
>
> The `first-install.*` entry points detect and (when missing) auto-install
> the dependencies above plus a few build-time/optional tools: `pnpm` (via
> `npm`), Tauri's Linux build deps (`libwebkit2gtk-4.1-dev`, `libgtk-3-dev`,
> `libayatana-appindicator3-dev`, `librsvg2-dev`, `libsoup-3.0-dev`,
> `libjavascriptcoregtk-4.1-dev`, plus `build-essential curl wget file` —
> apt only; other distros get a manual-install hint), the Claude Code CLI,
> and optionally [Joern](https://docs.joern.io/installation/) (~600 MB
> JVM-based, code-graph CFG/PDG metrics) and [lean-ctx](https://github.com/yvgude/lean-ctx)
> (~95% Claude API token savings). Install ladder: silent with `--yes` →
> interactive prompt → URL fallback. See [Prerequisites](#requirements)
> below.

---

## TL;DR — install + launch

After cloning the repo, run the install entry point for your OS, then double-click the launcher entry point:

| OS | Install (one-time) | Start the launcher |
|---|---|---|
| **Linux** | `bash first-install.sh` (or double-click `first-install.desktop`) | `bash start-launcher.sh` (or `start-launcher.desktop`) |
| **macOS** | Double-click `first-install.command` (or `bash first-install.sh`) | Double-click `start-launcher.command` |
| **Windows** | Double-click `first-install.bat`, **or** from a terminal: `.\first-install.bat` | Double-click `start-launcher.bat`, **or** `.\start-launcher.bat` |

> **Windows terminal note**: cmd.exe and PowerShell don't put the current directory on `PATH` by default, so plain `first-install.bat` fails with "not recognized". Use `.\first-install.bat` (with the leading `.\`) when running from a terminal.

The launcher binary is checked at startup for an embedded SvelteKit frontend; broken builds are skipped with a clear `rebuild with scripts/build-bundled-launcher.sh` hint instead of opening to a blank "Could not connect to localhost" page (regression guard added 2026-04-28).

If the GUI fails to open or shows an empty window, see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) and [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

---

## Quick Start (zero-dependency click-to-install)

Brand-new machine, no Python, no Node, no container runtime? Two ways to start:

> **Platform support**: Linux is the validated path. macOS is **experimental Tier-2** —
> minimal smoke testing on Apple Silicon. Windows is Tier-3 (CI-built but not interactively
> tested). On macOS expect to run `xattr -dr com.apple.quarantine .` after extracting a
> downloaded zip, and to install Homebrew + Python 3.11+ before the installer proceeds. See
> [KNOWN_ISSUES.md](KNOWN_ISSUES.md) for the full list of platform caveats.
>
> If `bash first-install.sh` fails partway through, paste the prompt from
> [`docs/INSTALL_RECOVERY.md`](docs/INSTALL_RECOVERY.md) into Claude Code — it walks Claude
> through diagnosing the failure and finishing the build.

### A. One-liner from a terminal (Linux / macOS)

From the directory you want to install into:

```bash
git clone https://github.com/hotak92/vibecoded-orchestrator.git && cd vibecoded-orchestrator && bash first-install.sh
```

(macOS: same command works in Terminal. Or after cloning, double-click `first-install.command` from Finder — see B.)

### B. Double-click

After cloning or downloading a [Release](https://github.com/hotak92/vibecoded-orchestrator/releases), open the repo folder and double-click the file for your OS:

| OS | Install file | Launcher file |
|---|---|---|
| **Linux** | `first-install.desktop` (or `first-install.sh` from a terminal) | `start-launcher.desktop` (or `start-launcher.sh`) |
| **macOS** | `first-install.command` | `start-launcher.command` |
| **Windows** | `first-install.bat` | `start-launcher.bat` |

The installer auto-handles Python, the container runtime (Podman/Docker), GPU detection, and the launcher binary — interactive prompts only when needed. Allow ~5–10 minutes plus first-run image downloads (~5 GB).

After install: double-click `start-launcher.<ext>` for your OS to start the launcher GUI.

> **Linux note**: Some file managers require enabling "Run executable text files on activation" before `.sh`/`.desktop` files become double-clickable (GNOME Files → Preferences → Behavior). The `.desktop` variants are the most reliable double-click target across DEs (GNOME, KDE, XFCE, Cinnamon).

> **Prefer the CLI?** See [Quick Install](#quick-install) below.

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

### One-click entry points (clone, then double-click)

After cloning the repo, the easiest way to install is to double-click the
file matching your OS:

| OS | First-time install | Start launcher (after install) |
|---|---|---|
| Linux | `first-install.sh` | `start-launcher.sh` |
| macOS | `first-install.command` | `start-launcher.command` |
| Windows | `first-install.bat` | `start-launcher.bat` |

`first-install.*` carries no pre-install dependencies — it auto-installs
Python, prompts to install Podman/Docker if neither is found, and runs the
full setup. `start-launcher.*` runs after install completes.

### Linux / macOS (terminal)
```bash
git clone https://github.com/hotak92/vibecoded-orchestrator.git
cd vibecoded-orchestrator
bash first-install.sh
```

### Windows (PowerShell)
```powershell
git clone https://github.com/hotak92/vibecoded-orchestrator.git
cd vibecoded-orchestrator
# Double-click first-install.bat, or from PowerShell:
.\first-install.bat
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

<a id="requirements"></a>
### Requirements

**Required (the install will halt and ask if missing):**
- **Python 3.11 or newer** — 3.12 is the version we develop and CI-test on; 3.13 is supported. 3.10 and older are rejected (we depend on stdlib `tomllib`, which lands in 3.11).
- **Docker** or **Podman** (for Weaviate + Ollama containers)
- **Claude Code** CLI (`npm install -g @anthropic-ai/claude-code`) — auto-installed via npm if Node is present
- **Claude Max** subscription (for Claude Code access; not auto-installable)
- **Node.js 18+** with `npm` — required for Claude Code itself AND for building the launcher GUI from source. NOT needed if you use the bundled prebuilt at `launcher/dist/<arch>/`.

**Auto-installed when needed (install attempts these without further prompts beyond the per-tool [Y/n]):**
- **`pnpm`** — installed via `npm install -g pnpm` if Node is present and pnpm is missing. Falls back to plain `npm` if the global install fails.
- **Tauri Linux build deps** (apt only) — `libwebkit2gtk-4.1-dev`, `libgtk-3-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev`, `libsoup-3.0-dev`, `libjavascriptcoregtk-4.1-dev`, `build-essential`, `curl`, `wget`, `file`. Other distros get a manual-install hint with the equivalent package names. Only needed when building the launcher from source (skipped when the bundled prebuilt is present).
- **GPU drivers** — detected, not auto-installed. The install prints download URLs for NVIDIA / AMD / Apple Silicon paths if no driver is detected.

**Optional companion tools (the install asks; default Y):**
- **[Joern](https://docs.joern.io/installation/)** — ~600 MB JVM-based code-property-graph tool. Adds CFG (control-flow) and PDG (program-dependence) metrics to the code-graph. Skip with `--no-joern`.
- **[lean-ctx](https://github.com/yvgude/lean-ctx)** — Rust binary that compresses CLI output by ~95%. Wires `BASH_ENV` so non-interactive subshells get the same compression. Auto-installs via Homebrew / Cargo / AUR (yay/paru) when one of those is present. Skip with `--no-lean-ctx`.

**Pre-installed assumptions (the install will fail clearly if missing — no auto-install path):**
- **`bash`** (Linux/macOS) or **`cmd.exe`** + **`PowerShell 5.1+`** (Windows) — POSIX/Windows guarantees.
- **`curl` OR `wget`** — needed for downloading the Joern installer and (when not bundled) the launcher binary from GitHub Releases. macOS always has `curl`; Linux Alpine/NixOS minimal may have neither and need `apt install curl` first.
- **`hdiutil`** (macOS only, for mounting `.dmg`) — ships with macOS.
- **`pkexec`** (Linux only, for graphical sudo prompts during Podman/apt installs) — present on most desktop distros.

> Already running Weaviate or Ollama? See [docs/GETTING_STARTED.md → Coexisting with other Weaviate or Ollama installs](docs/GETTING_STARTED.md#coexisting-with-other-weaviate-or-ollama-installs) — install detects existing services by content and adopts cleanly without polluting host collections.

#### Installing Python 3.12

`first-install.*` detects a missing or too-old Python and installs one automatically (T1 silent with `--yes`, T3 interactive prompt, T4 URL fallback). To install manually:

| OS | Command |
|---|---|
| **Ubuntu / Debian** | `sudo apt install python3.12 python3.12-venv python3-pip` |
| **Fedora / RHEL** | `sudo dnf install python3.12` |
| **Arch** | `sudo pacman -S python python-pip` |
| **macOS** (Homebrew) | `brew install python@3.12` |
| **Windows** (winget) | `winget install Python.Python.3.12` |
| Any | Download from <https://python.org/downloads/> |

If the wrappers can't auto-install (for example, no `winget` on older Windows or no Homebrew on macOS) they fail with a manual hint and the URL above. CI / non-interactive runs (`--quiet` or `VCT_NON_INTERACTIVE=1`) never auto-install — they fail loudly so you can fix it in your CI image.

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
| **Enterprise** | TBD — [contact us](mailto:team@vibecodedtools.com) | MAO + SOC 2 compliance track, priority support, commercial AGPL exemption, custom SLAs |

Pro and MAO tiers activate additional features via a license key issued at purchase, validated against our Supabase endpoint. Free tier works fully without a key — RL retrieval falls back to cosine ordering.

### Telemetry

**Telemetry is OPT-IN.** Nothing is sent unless you explicitly enable it during install (or later via the launcher's Settings → Telemetry panel). When opted in, we collect ONE thing only:

- **Multi-sentence-level retrieval embeddings** — the dense vectors produced by the local embedding model when it indexes your KG / code-graph chunks (~2-8 sentences per chunk), paired with which chunks were retrieved and which the user/agent actually used.

These embeddings are used **exclusively** to refine the RL reranker — the neural model that decides which retrieved nodes are most relevant for a given query. Better signal → better re-ordering for everyone. The training pipeline is part of the open-source codebase you can inspect (`claude_mcp_servers/` + `state/rl_*`).

**What we do NOT collect (ever, even with telemetry on):**
- Raw text content of your KG nodes, code, files, or queries
- File paths, project names, repo identifiers
- API keys, secrets, environment variables
- Personal info, IP, hostname, machine identifiers
- Any payload that could reconstruct your codebase or workspace

Embeddings are dense numeric vectors — they're aggregated, irreversible representations of the chunks that produced them, not the chunks themselves. We can't reconstruct the source text from the vectors we receive.

**Toggle it off anytime**: Settings → Telemetry → Disable, or set `VCT_TELEMETRY=0` in `.env`. Disabling stops collection immediately; previously-collected data isn't deleted retroactively unless you email `privacy@vibecodedtools.it`.

The telemetry endpoint runs on our Supabase project; payload schema is open in `claude_mcp_servers/`. AGPL applies to the collector code — you can self-host it for your own RL fine-tuning if you fork.

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
