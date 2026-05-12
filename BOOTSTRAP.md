# Bootstrap

**Read this first if you (or your AI assistant) just opened this repo.**

You're looking at **VibeCoded Orchestrator**, an AI-coding orchestrator that sits on top of Claude Code. It adds:
- A **Knowledge Graph** with semantic search across your project
- A **Code Graph** that knows your repo structurally — modules, classes, functions, APIs, cross-service calls
- **23 automation hooks** (KG sync on file edits, secret scans, context injection, post-commit reviews)
- **5 MCP servers** — Weaviate (KG + code graph), Ollama (local LLM), search (web/code/papers), code-embedding (CodeSage), Playwright (browser automation)
- **29 agents and 28 skills** for Claude Code — architects, code reviewers, debuggers, planners

The next steps depend on how you got here.

> **Easier alternative**: If you just want to install and go, see the **Quick Start** section near the top of [README.md](README.md) — double-click `first-install.<ext>` for your OS and skip the manual steps below.

---

## Quick start (recommended)

Download the latest archive for your OS from
[Releases](https://github.com/hotak92/vibecoded-orchestrator/releases):

- Linux x64: `vibecoded-orchestrator-0.2.0-linux-x64.tar.gz`
- macOS arm64: `vibecoded-orchestrator-0.2.0-macos-arm64.tar.gz`
- Windows x64: `vibecoded-orchestrator-0.2.0-windows-x64.zip`

Extract, then double-click `first-install.{sh,command,desktop,bat}` for
your platform. The installer will:
- Detect or auto-install Python 3.11+ (asks before sudo)
- Detect or prompt for Podman/Docker
- Set up the venv and pull container images on first run

System requirements: Python 3.11+, Podman or Docker, ~2GB free disk.
The first-install script handles the Python auto-install via your
platform's package manager (apt/dnf/pacman/brew on Linux/macOS;
winget on Windows). It does NOT install Podman/Docker — you'll be
prompted to install one before the launcher runs.

If you already have a clone from GitHub, the rest of this document
walks you through the standalone path (Path B below). If you opened
this through the VCT Launcher desktop app, see Path A.

---

## Path A: You opened this through the VCT Launcher

If you installed via the **VCT Launcher** desktop app, almost everything is already wired up:

- Container services (Weaviate, Ollama) are running
- Secrets are managed by the launcher's keychain
- KG and code graph collections are bound to your project name
- Hooks are wired into `.claude/settings.json`
- The launcher knows about this project and tracks its agents/skills/permissions matrix

**Expect three background tasks to start running** the moment the launcher registers a project that has pre-existing content under `knowledge/`, `docs/`, or any source-code directories: a code graph build, a KG sync (knowledge/ + docs/ → Weaviate), and a KG-summary backfill (knowledge/ → `.node_formats.json` sidecar). Each surfaces a status banner under the project header in the launcher GUI; `success` / `skipped` banners auto-hide 30 s after they complete. Failures expose `Show details` + `Retry`. See [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md#background-tasks-the-launcher-fans-out-on-add-project) for the full lifecycle.

**What you (the human) should do**: just start coding. Claude Code sessions in this folder will pick up the hooks and MCP servers automatically.

**What the AI assistant reading this should do**: nothing special. The orchestrator is ready. Read [`CLAUDE.md`](CLAUDE.md) for the operating manual.

If something looks broken (hooks not firing, MCPs unreachable, secrets missing), open the VCT Launcher, navigate to this project, and run **Doctor** or the equivalent diagnostic. Don't try to fix it manually first; the launcher knows what it set up.

---

## Path B: You cloned this from GitHub directly (no launcher)

You can run the orchestrator without the launcher; you just miss some integrations and have to set a few things up by hand. Trade-offs first, then steps.

### What you lose without the launcher

| Feature | With launcher | Without launcher |
|---|---|---|
| **Centralized secrets** | One UI for all API keys, scoped per-project | Manual `~/.vct-secrets/` setup or `.env` files |
| **Multi-project tracking** | Launcher tracks every project's agents/skills/hooks/permissions/secrets/KG/codegraph in a local DB | You manage each project independently; nothing tracks the cross-project state |
| **Easy install/update flow** | One-click install, version upgrades handled | You re-run `install.py` manually; pulling new versions = `git pull` + re-install |
| **Service lifecycle** | Launcher starts/stops Weaviate/Ollama on demand | You run `podman-compose up -d` manually |
| **License-gated paid modules** | Launcher fetches + verifies (RL retrieval, Coordination, etc.) | You can't use paid modules without the launcher |
| **MCP discovery** | New MCP servers appear in Claude Code automatically | You edit `~/.claude.json` by hand |

What you keep without the launcher:
- The whole orchestrator core: KG, code graph, hooks, MCPs, agents, skills
- All four free-tier embedding modes (GPU / CPU / OpenAI / low-resource)
- Cross-platform support (Linux / macOS / Windows + WSL)
- The option to install the launcher later when you want paid modules

### Standalone install steps

**Prerequisites** (install before running `install.py`):

| Tool | Why | Install |
|---|---|---|
| **Python 3.11 or newer** (3.12 recommended) | The orchestrator + MCP servers run on it. We depend on stdlib `tomllib` (3.11+). | `python3 --version` to check. Linux: `sudo apt install python3.12 python3.12-venv` (or `dnf` / `pacman`). macOS: `brew install python@3.12`. Windows: `winget install Python.Python.3.12`. The `install.sh` / `install.ps1` wrappers will auto-install via these same commands if Python is missing — interactive, never `-y`. |
| Claude Code CLI | The orchestrator hooks into Claude Code | `npm install -g @anthropic-ai/claude-code` |
| Docker or Podman | Runs Weaviate + Ollama containers | `docker --version` or `podman --version` |
| (Optional) NVIDIA GPU + nvidia-container-toolkit | For CodeSage code embeddings (best quality) | https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html |
| (Optional) `cargo install lean-ctx` | Context compression (token savings) | `cargo install lean-ctx` |

**Then**:

```bash
cd vibecoded-orchestrator   # this directory
python3 install.py          # or `python install.py` on Windows
```

`install.py` will:
1. Check Python version
2. Detect your system (OS, GPU, container runtime, optional companions like lean-ctx)
3. Pick an embedding mode automatically (GPU if NVIDIA detected, CPU otherwise — override with `--gpu` / `--cpu-only` / `--openai-key KEY` / `--low-resource`)
4. Create a Python venv at `.venv` (project root)
5. Install Python deps
6. Bring up Docker/Podman containers (Weaviate + Ollama)
7. Pull Ollama models (qwen3-embedding, qwen3.5:9b, gemma4:e4b)
8. Create `state/` directory
9. Write `.env`
10. Copy bundled agents + skills into `.claude/agents/` and `.claude/skills/`

If anything fails, the installer prints what it tried and what failed. Re-run with `--update` to skip the steps that already succeeded.

**After install**:

```bash
# Verify services
curl -s http://localhost:8081/v1/.well-known/ready    # Weaviate
curl -s http://localhost:11435/api/tags               # Ollama

# Set up GitHub access (optional, for the search MCP):
mkdir -p ~/.vct-secrets/{shared,projects}
chmod 700 ~/.vct-secrets
cp tools/vct-secrets/vct ~/.vct-secrets/vct
chmod 755 ~/.vct-secrets/vct
echo "ghp_yourtokenhere" | ~/.vct-secrets/vct set --project SHARED --key github_pat

# Sanity check the orchestrator
.claude/scripts/kg-search list           # should print empty list (no KG nodes yet)
.claude/scripts/kg-sync --all            # syncs the bundled knowledge/ to Weaviate
```

Open the project folder in any Claude Code surface: **VS Code extension**, the **Claude Code CLI** (`cd <project> && claude`), or the **Claude Desktop app**. Start a session. Hooks fire on their own. Edit a file and watch the post-edit hook sync the change into the code graph.

### Shared services across multiple installs

The orchestrator uses three local services — **Weaviate**, **Ollama**, and an optional **code_embed** — and they're shared across every orchestrator install on the machine. Isolation between projects happens at the collection level inside the shared Weaviate (each project gets its own `KG_COLLECTION` and `DEVELOPMENT_COLLECTION`), not at the container level.

- **First install**: services start automatically via `podman-compose up -d` (or the docker equivalent).
- **Subsequent installs**: `install.py` probes ports 8081 / 11435 / 11440 first; if the services are already up, it reuses them and bootstraps only the Weaviate collections this project needs.

Verify what's running:
```bash
podman ps   # or `docker ps`
# Should show one weaviate, one ollama, optionally one vct_code_embed.
```

**Advanced**: set `VCT_FORCE_SEPARATE_CONTAINERS=1` and override `WEAVIATE_PORT` / `OLLAMA_PORT` / `CODE_EMBED_PORT` to give an install its own containers. Useful for hard isolation between (e.g.) work and personal setups, but it costs extra disk and RAM per install.

### Container volumes location

The three orchestrator volumes — `weaviate_data`, `ollama_data`, `code_embed_cache` — default to the container engine's standard location (`~/.local/share/containers/storage/volumes/` for rootless Podman). Move them if `$HOME` has limited disk.

**Fresh install** (no existing volumes): the launcher's onboarding step 3 shows a picker. Pick Default or Custom; if Custom, the launcher writes `infrastructure/docker-compose.override.yml` (gitignored) with bind-mount overrides pointing the three volumes at `<chosen>/weaviate`, `<chosen>/ollama`, `<chosen>/code_embed`. The actual root path is indirected through `${VCT_VOLUMES_PATH}` in `infrastructure/.env` so the override yaml is portable across machines.

**Subsequent install** (existing volumes detected): the launcher and `install.py` BOTH detect existing orchestrator volumes (canonical names + historical names like `weaviate_claude`) via `podman volume inspect` (read-only). When found, the launcher skips the picker and shows a read-only info panel; `install.py` prints the existing mountpoints. **No override is generated** — bind-mount overrides on top of already-existing named volumes either fail or mask the original. Historical names get an `external: true` alias in the override so compose reuses the data without rebinding.

**Move volumes later** (Settings → Preferences → Volume location):
1. Pick a target path → Change…
2. Read-only dry-run: total size, ETA, free-space check, warnings
3. Confirm migration:
   - `compose stop` (preserves volumes — no `--volumes` flag)
   - `cp -a` each mountpoint to `<target>/<role>`
   - Write the bind-mount override + `${VCT_VOLUMES_PATH}` in `.env`
   - `compose up -d` and HTTP-probe Weaviate + Ollama for up to 60 s
   - **Only if all probes pass**: `podman volume rm` the legacy volumes

**Safe-rollback guarantee**: every error branch past the override-write removes the override and brings the OLD volumes back up. The legacy volumes are removed only after the new bind-mounts are verified healthy. If the migration fails halfway, your data stays where it was.

`migrate_volumes` is the ONLY function in the launcher allowed to invoke `podman volume rm`. The non-destructive audit guard `test_no_destructive_subprocess_calls_in_install_path` enforces this at the source level — no install-path file may shell out to `volume rm`.

### When you decide you want the launcher

Install [VCT Launcher](https://github.com/pb992/VCT-Launcher), point it at this folder, and run "Adopt project". The launcher imports the existing setup — KG bindings, hooks, secret references — into its DB without disturbing anything. After that, the launcher manages this project the way Path A describes.

---

## What every assistant reading this should know

Beyond paths A/B, the operating manual is [`CLAUDE.md`](CLAUDE.md). It covers:
- KG-first search policy (`hybrid_search` before grep for conceptual queries)
- The two-layer memory pattern (`MEMORY.md` for stable facts, `.claude/CONTEXT_STATE.md` for current task)
- Hook events and when each fires
- Agents and skills, with an Opus / Sonnet / Haiku decision tree for when to spawn them
- House communication style: direct, no fluff, no superlatives, no premature validation

If you're an AI assistant and the user dropped you in here without context, your first three actions are:
1. Read `CLAUDE.md` (operating manual)
2. Read `.claude/CONTEXT_STATE.md` if it exists (current work state)
3. Run `hybrid_search("what is this project")` to surface relevant KG nodes

Then ask the user what they want to work on.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `hybrid_search` returns nothing | Weaviate not running or KG not synced | `cd infrastructure && podman-compose -f docker-compose.yml up -d` (or `docker compose up -d`) then `.claude/scripts/kg-sync --all` |
| Hooks don't fire | `VCT_DISABLE_HOOKS=1` set in shell | `unset VCT_DISABLE_HOOKS` |
| Search MCP errors on GitHub queries | `~/.vct-secrets/shared/github_pat` missing or wrong perms | `vct doctor` |
| `code-graph-query search` returns nothing | Code graph not analyzed yet | `.claude/scripts/code-graph-analyze . --project "MyProject"` |
| Ollama models slow/missing | Models not pulled | `ollama list` to check; `ollama pull qwen3-embedding:0.6b` if missing |
| Container runtime not detected | Neither podman nor docker on PATH | Install one, or set `VCT_CONTAINER_RUNTIME=podman` (or `docker`) |
| `bind: address already in use` on 8081/11435/11440 | Another install already runs the shared services | Expected — `install.py` should detect this and reuse them. If it didn't, check that the services respond to `curl localhost:8081/v1/.well-known/ready` etc. To force separate containers per install, set `VCT_FORCE_SEPARATE_CONTAINERS=1` plus distinct ports |

For deeper issues, see `docs/TROUBLESHOOTING.md` and `docs/CONFIGURATION.md`.
