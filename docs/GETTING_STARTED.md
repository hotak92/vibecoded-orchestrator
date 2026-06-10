# Getting Started

This guide walks you through installing the orchestrator, configuring your first project, and understanding what runs during a normal Claude Code session.

## Prerequisites

The `first-install.{sh,command,bat}` shims and `install.sh` / `install.ps1` auto-install missing prerequisites (Python 3.11+, Node.js 18+, Podman) via the platform package manager. They prompt before invoking sudo / brew / winget — pass `--yes` or `--non-interactive` to skip prompts (auto-install is disabled in that mode; missing tools just get logged and `install.py` skips the dependent feature). GPU drivers are NEVER auto-installed (out of scope for an unattended installer).

What gets auto-installed when missing:

- **Python 3.11, 3.12, or 3.13.** Older versions are rejected at the shim's version probe — `install.py` uses stdlib `tomllib` (3.11+). Python 3.14 is rejected when wheel coverage is incomplete: the bootstrap prepass dry-runs `pip install --only-binary=:all:` against the dependency set and downgrades the recommendation if any dep would need to build from source. If `first-install.{sh,command,bat}` shows "Python 3.11+ required but not found" or refuses your interpreter, install 3.13 explicitly:
  - Linux: `sudo apt install python3.13 python3.13-venv` (or `sudo dnf install python3.13`, `sudo pacman -S python`, `sudo zypper install python313`, `sudo apk add python3`)
  - macOS: `brew install python@3.13`
  - Windows: `winget install Python.Python.3.13`
  - Or: <https://python.org/downloads/>
- **Node.js 18+** (needed by Playwright MCP and the Tauri launcher build path)
  - Linux: `sudo apt install nodejs npm` (Ubuntu/Debian), `sudo dnf install nodejs npm` (Fedora), `sudo pacman -S nodejs npm` (Arch)
  - macOS: `brew install node`
  - Windows: `winget install OpenJS.NodeJS.LTS`
  - Or: <https://nodejs.org/>
  - Note: on fnm/nvm setups where you've hand-symlinked just `npm` to `~/.local/bin/`, the installer now finds `npx` via `dirname(realpath(npm))/npx` automatically — no manual PATH fiddling needed.
- **Podman** (preferred — no commercial license, native on Linux/macOS/Windows). Docker is accepted as an alternative if already installed.
  - Linux: `sudo apt install podman` (Ubuntu/Debian), `sudo dnf install podman` (Fedora), `sudo pacman -S podman` (Arch), `sudo zypper install podman` (openSUSE), `sudo apk add podman` (Alpine)
  - macOS: `brew install podman`, then `podman machine init && podman machine start` (one-time prereq — both commands must complete before re-running the installer)
  - Windows: `winget install RedHat.Podman` (requires WSL2: `wsl --install`)

Auto-start behaviour for the Podman daemon (v0.2.51+): if Podman is installed but the daemon/socket is not responding, `install.py` attempts `systemctl --user start podman.socket` (Linux) or `podman machine start` (macOS/Windows) before giving up. If the start fails (e.g. machine not yet initialized), a deferral entry is written to `.claude/context/UPDATE_DEFERRED.md` with the exact manual command to run — no destructive auto-init.

On Linux (Fedora/RHEL/CentOS) with SELinux in `Enforcing` mode, the bootstrap prepass detects this and the install adds `:Z` to bind-mount volume args automatically. On hosts with an NVIDIA GPU, the install prints the distro-specific install hint for `nvidia-container-toolkit` when missing — the GPU itself is detected, the toolkit is not auto-installed.

Other prerequisites (not auto-installed; you need them yourself):

- Claude Code CLI (`npm install -g @anthropic-ai/claude-code`) with a Claude Max subscription

## Install

### Recommended: entry-point scripts (Linux / macOS / Windows)

The quickest path from zero to running:

```bash
git clone https://github.com/hotak92/vibecoded-orchestrator.git && cd vibecoded-orchestrator && bash first-install.sh
```

Or, after cloning, double-click the file for your OS:

| OS | File to double-click |
|---|---|
| Linux | `first-install.desktop` |
| macOS | `first-install.command` |
| Windows | `first-install.bat` |

`first-install.{sh,command,bat}` is a thin OS shim (~100 LoC each) — see [`first-install.sh:1-106`](../first-install.sh) and [`first-install.command:1-131`](../first-install.command). The three steps it runs in sequence:

1. **Python detect** — OS-aware candidate cascade. macOS tries Apple Silicon Homebrew under `/opt/homebrew/opt/python@3.13/` first, then PATH; Linux tries PATH then `/home/linuxbrew/.linuxbrew/bin/`; Windows uses the Python Launcher (`py -3.13` first) then PATH. Each candidate is version-probed (`>= 3.11` required). If none qualifies, the shim prints the distro-specific install hint (apt/dnf/pacman/zypper/apk on Linux, `brew install python@3.13` on macOS, `winget install Python.Python.3.13` on Windows) and on interactive runs offers to install via the package manager.

2. **Bootstrap prepass** — `install.py --bootstrap --json` runs as a read-only system-detection probe, writing a versioned JSON envelope to `state/logs/bootstrap-prepass.json`. The envelope contains detected Python/Node/Podman/Docker versions, GPU vendor + VRAM, RAM, OS / distro / package manager, resolved paths (install root, launcher binary, dist subdir), Weaviate / Ollama / code-embed / vct-hub endpoints, and a `missing_prereqs` list with per-tool install hints. Side-effect policy: no file writes, no network, no prompts; every probe has a timeout. Failure here is logged and ignored — the full install runs regardless. Schema: [`docs/INSTALL_ARCHITECTURE_v2.md` §3](INSTALL_ARCHITECTURE_v2.md#3-target-architecture-installpy---bootstrap-mode). The `--bootstrap` flag is mutually exclusive with `--update` / `--lightweight` / `--uninstall` and is invoked alone in its own process by the shim.

3. **Full install** — `install.py <forwarded args>` runs the canonical 10-step flow: Python check, system detection, optional companions (Joern, lean-ctx), venv, dependencies, containers, Ollama models, Weaviate collections + KG seeding, MCP server registration in `~/.claude.json`, and Claude CLI check. The shim forwards every user-supplied flag verbatim (so `bash first-install.sh --update`, `--gpu`, `--cpu-only`, etc. all work).

On `install.py` exit 0 and unless `--no-auto-launch` was passed, the shim then runs [`scripts/post-install-launcher.sh`](../scripts/post-install-launcher.sh) (Linux/macOS) or its inline equivalent in `first-install.bat` (Windows). That step probes for an existing launcher binary, downloads the prebuilt one from GitHub Releases if absent, or offers to build from source. macOS additionally strips `com.apple.quarantine` from any downloaded binary to preempt Gatekeeper. The launcher is then spawned as a detached process.

Flags the shim itself consumes: `--no-auto-launch` (skip the post-install launcher spawn), `--non-interactive` (translated to `install.py --yes`). Every other flag is forwarded to `install.py` verbatim.

After install, double-click `start-launcher.<ext>` for your OS to start the launcher GUI.

**Time budget**: ~5 min of interactive prompts, then 10–30 min for container images and model downloads (~5 GB; GPU mode pulls an additional ~2.5 GB). Re-runs reuse cached images.

#### Diagnosing a failed install

`state/logs/bootstrap-prepass.json` is the first thing to read when an install fails — it captures what the system looked like at the moment the shim ran, before anything mutated. The companion file `state/logs/install.jsonl` records every step `install.py` and `post-install-launcher.sh` attempted (one JSON event per line, schema in [`docs/INSTALL_RECOVERY.md`](INSTALL_RECOVERY.md)). Pasting both into a Claude Code session gets you a working diagnosis without re-running the install.

#### CI smoke

`install-smoke-tri-os.yml` runs the actual shims end-to-end on ubuntu-22.04, ubuntu-24.04, macos-14, windows-latest, and fedora-40 every PR + push to main + daily at 06:00 UTC. The Fedora job exercises SELinux `:Z` mount handling; the Ubuntu jobs exercise libwebkit2gtk-4.0/4.1 fallback; macOS exercises the Apple Silicon Homebrew cascade and bash 3.2; Windows exercises `first-install.bat` in real `cmd.exe` (the older `installer-smoke.yml` Windows job runs under `shell: bash`, which never reaches the BAT code path). Pre-ship gate 22 blocks release tags when this workflow is red on main.

### For advanced users: run install.py directly

If you already have Python 3.11+ and a container runtime, you can call `install.py` directly (skipping the shim — you lose only the Python-detect cascade and the auto-spawn of the launcher GUI):

```bash
cd vibecoded-orchestrator
python3 install.py
```

To preview what the bootstrap prepass would detect without running the install, invoke it standalone:

```bash
python3 install.py --bootstrap --json > /tmp/probe.json   # machine-readable envelope
python3 install.py --bootstrap                            # human-readable summary table
```

`--bootstrap` is read-only — it emits to stdout, performs no file writes, no network calls, and no prompts. It is mutually exclusive with `--update`, `--lightweight`, and `--uninstall`. The shim captures the `--json` output to `state/logs/bootstrap-prepass.json` itself; `install.py --bootstrap` invoked by hand prints to stdout instead.

`install.py` (without `--bootstrap`) does the following:

1. Creates a Python venv at `.venv/` (project root)
2. Detects your hardware (NVIDIA / AMD / CPU / Apple Silicon) and chooses the embedding backend. AMD/ROCm layers `infrastructure/docker-compose.rocm.yml` on top; the precedence order is user override → Apple Silicon → NVIDIA+VRAM → AMD+VRAM → CPU
3. Starts Weaviate and Ollama in containers and waits for them to be ready
4. Pulls embedding models (`qwen3-embedding:0.6b` by default; CodeSage-Large-v2 on GPU installs; the code backend's fallback chain is CodeSage → qwen3 → Jina, picked at construction time)
5. Writes `.env`, `.claude/settings.json` (canonical MCP-env channel — propagates to MCP subprocesses on every Claude Code surface), and `.claude/env` (POSIX shell-sourceable copy). `.vscode/settings.json` is touched only for VS Code editor preferences (Pylance/watcher excludes); `.vscode/tasks.json` is written so VS Code auto-starts `vct-hub` on `folderOpen`
6. Copies 45 agent templates into `.claude/agents/` and 53 skill templates into `.claude/skills/`; renders 31 hooks (both `.sh` and `.ps1` per hook on every OS — cross-OS workflows don't get stale orphans) into `.claude/hooks/`
7. Registers MCP servers in `~/.claude.json`: `weaviate-kg`, `search` (search_papers only), and `playwright` (the latter via `npx -y @playwright/mcp@latest`; opt out with `VCT_SKIP_PLAYWRIGHT=1`). The code-embedding service is a backend HTTP service on `:11440`, not an MCP — it's started in step 3 alongside Weaviate/Ollama. The `vct-ollama` MCP is **not** registered by default — install via launcher → Modules if you want local LLM tool surfaces
8. Deploys the detached `vct-hub` binary alongside the launcher, invokes `vct-hub --start-if-not-running`, probes `/health`. The hub listens on `127.0.0.1:7700` by default (`VCT_HUB_PORT` to override); auth via fresh-per-startup bearer token at `<vct_root>/hub.token` (mode `0o600`)

### Code-embedding backend by host

The code-graph subsystem embeds source code into Weaviate for semantic search. Two backends ship:

- **CodeSage-Large-v2** (2048-dim, Apache 2.0) — the default on NVIDIA hosts with ≥4 GB VRAM. Runs inside `infrastructure/codesage/Dockerfile.cuda` and listens on `:11440`. The bundled image is **CUDA-only by default**; the `Dockerfile.cuda` variant is the one the install pulls when it detects an NVIDIA GPU + drivers + ≥4 GB VRAM.
- **Ollama (`qwen3-embedding:0.6b`, 1024-dim)** — the fallback for hosts without CUDA. The code-embed service exposes the same `:11440` HTTP API but routes embed requests through Ollama on `:11435`. Lower quality than CodeSage but works on every CPU and on Apple Silicon (where the CUDA path is impossible).

Apple Silicon + macOS Intel users get the Ollama-based code embedding via the standard install — no extra flags, no manual Dockerfile swap. The hardware detection step (step 2) classifies the host as "Apple Silicon" and the embedding-backend resolver picks Ollama automatically; the CodeSage Dockerfile is skipped entirely (it would not build on the host's image-pull architecture anyway). A native Metal-accelerated CodeSage variant is on the v0.3.0+ roadmap — tracked upstream against sentence-transformers shipping Metal wheels.

### Common install flags

```
python install.py --gpu               # Enable NVIDIA GPU acceleration
python install.py --cpu-only          # Force CPU-only mode
python install.py --low-resource      # Lightest models for low-RAM machines
python install.py --no-containers     # Skip container management (bring your own)
python install.py --update            # Re-run on an existing install (preserves .env)
```

For CI or non-interactive installs:

```bash
python install.py --quiet --no-joern --no-containers
```

If anything goes wrong during install, see the troubleshooting table in [README.md](../README.md) and [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md).

### Coexisting with other Weaviate or Ollama installs

The installer is safe by default if you already have a Weaviate, Ollama, or other vco-managed service running on the canonical ports (`8081`, `11435`, `11440`) — regardless of who started it.

Before bringing up its own containers, `install.py` probes each port and content-fingerprints the response (it does not rely on container names; the probe inspects the `/v1/schema` and `/api/tags` payloads):

- **Nothing on the port** → start our service on the default port.
- **A prior vco install on the port** → adopt it. No new container, no re-prompt; reuses the running service via the `~/.vct/services.toml` lock file.
- **A foreign service on the port** (e.g. an unrelated Weaviate, an aihive Ollama, a project's own stack) → default action is **alt-port**: pick the next free port, write `infrastructure/docker-compose.override.yml`, and bring our copy up next to the existing service. Your service is never stopped, modified, or written to.

Override the default with `--on-conflict`:

```
python install.py --on-conflict alt-port   # default — run our copy on a free port
python install.py --on-conflict adopt      # advanced — reuse the foreign service IN PLACE
                                           #   (will write our collections into it)
python install.py --on-conflict abort      # bail if any conflict is detected
```

The chosen action per service is recorded in `~/.vct/services.toml` and re-read by both install.py and the launcher, so subsequent runs do not re-prompt.

#### Collection naming in adopt mode

When install adopts an existing Weaviate, it must not pollute the host with bare top-level `KnowledgeGraph` / `Development` collections — many users run Weaviate with per-project namespacing (`MyProject_KnowledgeGraph`, etc.). Adopt mode therefore:

1. **Derives the per-install KG name from the project basename** — installing in `~/projects/myapp/` writes to `Myapp_KnowledgeGraph` and `Myapp_Development`. Hyphens / underscores in the basename are PascalCased; pure-punctuation basenames fall back to `vct_KnowledgeGraph`.
2. **Honors `KG_COLLECTION` / `DEVELOPMENT_COLLECTION` env vars** if set (typically via `.claude/settings.json` `env` — the canonical channel; the historical `.vscode/settings.json` `claude-code.env` source was removed in v0.2.12 / PR-27 because it didn't propagate to MCP subprocesses on Linux) — explicit override wins over the basename derivation.
3. **Skips creation of any collection that already exists** under the resolved name.
4. **Skips a `Development` collection entirely** if the host already has any `<X>_development` (the host's namespacing wins).
5. **Announces every proposed creation and waits for confirmation** in interactive mode. Pass `--yes` for non-interactive runs.
6. **Does not auto-adopt cross-project shared KGs** like an existing `ClaudeKnowledgeGraph`. The orchestrator runs an orphan-prune sync that deletes entries whose `file_path` no longer exists in the active project, so two installs sharing one collection would silently delete each other's entries. vco always creates its own `VibeCodedOrchestrator_KnowledgeGraph` (renamed from `VibeCodedTools_KnowledgeGraph` in v0.2.12; existing installs can use the launcher's Identity tab picker to designate the old class as canonical).

When install starts its own Weaviate (no adoption), bare `KnowledgeGraph` / `Development` defaults are kept — there's nothing else in the instance to namespace against.

#### Skipping collection creation

`--skip-seed` skips both the seed step and the schema bootstrap (no content to seed into anyway). The MCP server creates collections lazily on first write, so subsequent operations still work:

```
python install.py --skip-seed             # skip seed + collection bootstrap
python install.py --skip-collections      # bootstrap-only opt-out (still seeds)
```

#### Gating writes to the shared cross-project KG

Set `SHARED_KG_WRITE_DISABLED=true` in `.env` (or in the install environment) to refuse `store_knowledge_node(scope="shared")` calls from this project. Reads of `VibeCodedOrchestrator_KnowledgeGraph` (renamed from `VibeCodedTools_KnowledgeGraph` in v0.2.12) remain on (asymmetric model since 2026-05-01: every project always reads the shared KG; the gate is write-only). Legacy alias `SHARED_KG_OPT_OUT` kept for ~3 releases.

#### Lock file: `~/.vct/services.toml`

Persists each service's resolved action so installer and launcher agree:

```toml
[[services]]
name = "weaviate"
mode = "adopt"          # or: "parallel", "unresolved", "refuse"
external_url = "http://localhost:8081"
parallel_port = 8082    # only when mode = "parallel"
```

Mode mapping mirrors the launcher's `AdoptionMode` enum (`adoption.rs`): `unresolved | adopt | parallel | refuse`. Delete the file to force a fresh probe on the next run.

#### Manual cleanup of stray collections

If an older install left orphaned collections behind (e.g. a bare `KnowledgeGraph` from before this fix), inspect them via the Weaviate REST API and delete with `curl`:

```
curl -s http://localhost:8081/v1/schema | python -m json.tool
curl -X DELETE http://localhost:8081/v1/schema/KnowledgeGraph
```

The MCP server recreates any collection it actively uses on next write, so deleting an unused one is safe.

## Open the orchestrator in Claude Code

After install, open the `vibecoded-orchestrator` directory in one of the three supported surfaces:

- **CLI**: `cd vibecoded-orchestrator && claude --dangerously-skip-permissions`
- **VS Code extension**: open the folder as a workspace; the extension reads `.claude/settings.json` for MCP env vars (the canonical channel that also propagates to MCP subprocesses). `.vscode/settings.json` is only used for editor preferences
- **Claude Desktop app**: point it at the install directory

On session start, the following hooks fire automatically (`SessionStart` matcher: `startup`):

1. `ensure-containers.sh` — checks that Weaviate, Ollama, and the code-embedding service are running; starts them if not
2. `session-start-ensure-hub.sh` (v0.2.21+) — probes `vct-hub /health`, runs `vct-hub --start-if-not-running` if not. The hub outlives the launcher GUI, so projects opened directly via the CLI or VS Code still get the resolver
3. `session-start-kg-loader.sh` — displays KG resource paths
4. `context-size-check.sh` — warns if `.claude/CONTEXT_STATE.md` is over 200 lines
5. `embedding-failures-surface.sh` (v0.2.18+) — if `.claude/context/EMBEDDING_FAILURES.md` exists (written by `vco_lib.embedding_service` when a backend was unreachable), surfaces a Claude-readable hint on the next chat start. Auto-clears on the next successful embed
6. `compact-context-reinject.sh` (on resume after compaction) — reinjects `CONTEXT_STATE.md`, recent commits, and any active plan

Verify MCP servers connected:

```bash
claude mcp list
# Expected: weaviate-kg ✓ Connected, search ✓ Connected, playwright ✓ Connected
# (`ollama` MCP is opt-in via launcher → Modules → vct-ollama since v0.2.11; Ollama itself still runs as
#  embedding infrastructure — visible in `podman ps` / `docker ps`, just no MCP wrapper by default.)
```

Verify the hub is up (v0.2.21+):

```bash
vct-hub --status                                # CLI helper
curl -s http://127.0.0.1:7700/health            # raw probe (no auth required on /health)
```

## Set up a project

The orchestrator can configure another codebase to use its knowledge graph and code graph. From inside the orchestrator session:

```
You: "Set up my FastAPI project at ~/dev/my-api"
```

Claude will analyze the codebase and write these files into the target project:

- `~/dev/my-api/.claude/settings.json` — permissions, hook registrations, and the canonical per-project MCP env block (`KG_COLLECTION=MyAPI`, etc.) that propagates to MCP subprocesses
- `~/dev/my-api/.claude/env` — POSIX shell-sourceable copy of the same env values, for shell-wrapper users
- `~/dev/my-api/CLAUDE.md` — project instructions tailored to the detected stack
- `~/dev/my-api/.claude/CONTEXT_STATE.md` — initial session state

It also queues a background code graph analysis of the target project.

Alternatively, use the VCT Launcher GUI: it runs the same configuration wizard visually and writes both env files (`.claude/settings.json` env + `.claude/env`) in lockstep so the CLI, VS Code extension, and Claude Desktop app all see the same MCP environment. As of v0.2.12 (PR-27, 2026-05-16) the launcher no longer writes `.vscode/settings.json` `claude-code.env` — empirical sentinel testing showed that block did not propagate to MCP subprocesses on Linux Claude Code 2.1.143.

### Background tasks the launcher fans out on Add project

The `create_project_v2` Tauri command returns the moment bundle install finishes; three background tasks then run in parallel so a project with pre-existing content lands fully indexed without you running CLI commands by hand:

1. **Code graph build** (`commands::codegraph::spawn_initial_build`) — runs `code-graph-analyze` over the project root.
2. **KG sync** (`commands::kg_sync::spawn_initial_sync`, added 0.2.2) — runs `.claude/scripts/kg-sync --all` against `knowledge/**/*.md` and `docs/**/*.md`. Idempotent at the Weaviate layer (UUIDs derived from node title); safe to re-run.
3. **KG summaries** (`commands::kg_summary::spawn_initial_summary`, added 0.2.3) — runs `.claude/scripts/generate-kg-summary.py` per file. Picks the first available backend: `claude` CLI on PATH → Ollama at `KG_SUMMARY_OLLAMA_URL` (default `http://localhost:11435`, model `KG_SUMMARY_OLLAMA_MODEL`, default `qwen3.5:9b`) → `ANTHROPIC_API_KEY` direct → silent skip. Content-hashes each node, so re-runs are a cheap no-op for unchanged nodes.

Each task surfaces its progress in the GUI:

- A **full-width banner** under the project header for the active project. The three banners stack `KgSummaryBanner` → `KgSyncBanner` → `CodeGraphBuildBanner` (newest task on top, matching the add-project spawn order). Banners stay visible in `pending` / `running` / `failed` states; `success` / `skipped` auto-hide 30 s after `finished_at_iso`. Failure detail is inline behind a `Show details` toggle, with a `Retry` button.
- A **compact pill** in the project list row (`/project`). Read-only mirror — the project page is where you click Retry.
- A **header retry button** on the project page: `Re-build code graph`, `Re-sync KG`, `Re-build KG summaries`. Each calls the same Tauri command the banner's Retry button uses (`rebuild_code_graph`, `retry_kg_sync`, `retry_kg_summary`).

The three lifecycles share a `pending → running → (success | failed | skipped)` state machine, persisted to the per-task SQLite table in `~/.vct/launcher.db` (`code_graph_builds`, `kg_syncs`, `kg_summaries`).

#### Crash recovery

If the launcher exits while a task is still running, the row stays in `running` until the next launcher boot, when `lib.rs::setup()` runs a two-phase sweep across all three task types: phase 1 marks any `running` row as `failed` with `"launcher crashed mid-run; click Retry to re-run"` (silent re-spawn would mask the crash, so the broken lifecycle stays visible), and phase 2 re-spawns any `pending` rows. The boot log line reports the counts:

```
[vct] resume-sweep: code-graph (running→failed: N, pending respawned: M); \
                    kg-sync (running→failed: N, pending respawned: M); \
                    kg-summary (running→failed: N, pending respawned: M)
```

#### When the summariser has no backend

If none of `claude` CLI / Ollama / `ANTHROPIC_API_KEY` is reachable when the KG-summary task runs, the launcher detects the script's `KG-summary: no backend available` log line on the first node and hard-stops the walk; the banner goes yellow `skipped` with the install hint under `Show details`. Summaries then backfill incrementally as you edit nodes in Claude Code sessions — the `PostToolUse` hook `kg-summary-generator.{sh,ps1}` runs the same script per file.

## What runs during a session

Once a project is configured, here is what fires on normal use:

**On every prompt** (`UserPromptSubmit` hook):
- Searches the Knowledge Graph for nodes relevant to your query
- Injects matches into the context window before Claude generates a response

**On every file edit** (`PostToolUse` hook on `Edit`/`Write`):
- Files under `knowledge/` sync to Weaviate (per-project `<KG_COLLECTION>`; content-hash skip on unchanged nodes since v0.2.17)
- Files under `docs/` sync to the development collection (per-project `<DEVELOPMENT_COLLECTION>`; gained content-hash + status parity with KG in v0.2.18 — ~820x faster on unchanged content)
- Code files are queued for code graph re-analysis (`code-graph-incremental.{sh,ps1}` — invokes `analyze --incremental --language <lang> --prune-stale` since v0.2.18, scoped per-language so deleted files in one language never wipe rows from another)
- A credential scan runs on the written file (`post-tool-security.sh` — sentinel renamed in v0.2.16 to avoid false positives on docs that quote the marker)

**On session end** (`Stop` hook):
- Cost data appended to `~/.claude/metrics/costs.jsonl`
- Desktop notification fires

## Knowledge Graph

The Knowledge Graph stores notes, decisions, and patterns as Markdown files under `knowledge/`. Nodes use YAML frontmatter and Obsidian-style typed WikiLinks:

```markdown
---
title: JWT Middleware Pattern
type: concept
tags: [auth, python]
status: active
---

Pattern used in my-api for stateless auth.

[[uses::FastAPI]] [[implements::JWT]]
```

Nodes are indexed in Weaviate with 1024-dim embeddings. `hybrid_search` in the MCP finds them by meaning, not just keyword match.

Cross-project reuse works via `SHARED_KG_COLLECTION`: a knowledge node written in one project is available when Claude is working in another. You don't need to re-explain context you've already captured.

## Code Graph

Run `code-graph-analyze` to index a codebase:

```bash
.claude/scripts/code-graph-analyze ~/dev/my-api --project "MyAPI"
```

This extracts `CodeModule`, `CodeClass`, `CodeFunction`, `CodeAPI`, and `CodeInteraction` entities using Tree-sitter and stores them in Weaviate. Claude can then answer structural questions without reading every file:

```bash
.claude/scripts/code-graph-query search "auth middleware"
```

## Pre-installed assumptions

The installer fails clearly (no auto-install path) if any of these are missing — most desktop OSes have them by default, but minimal images (Alpine, NixOS minimal, stripped-down WSL distros) may not.

- **`bash`** (Linux / macOS) or **`cmd.exe`** + **`PowerShell 5.1+`** (Windows) — POSIX / Windows guarantees
- **`curl` OR `wget`** — needed for downloading the Joern installer and (when not bundled) the launcher binary from GitHub Releases. macOS always has `curl`; Linux Alpine / NixOS minimal may have neither and need `apk add curl` / `nix-env -iA curl` first
- **`hdiutil`** (macOS only, for mounting `.dmg`) — ships with macOS
- **`pkexec`** (Linux only, for graphical sudo prompts during Podman / apt installs) — present on most desktop distros, missing on minimal server images

## Recommended companions

Tools the installer detects and integrates with when present, but doesn't require:

### lean-ctx — CLI output compression

[lean-ctx](https://github.com/yvgude/lean-ctx) (MIT license, zero telemetry) wraps common CLI commands (`git`, `npm`, `pip`, `grep`, `ls`, etc.) and compresses their output by 90–97% by stripping boilerplate, progress bars, and redundant lines. This translates directly to:

- shorter Claude context windows
- lower token costs per session
- faster response time on commands that produce verbose output

The orchestrator's installer detects lean-ctx automatically. As of v0.2.11 the wiring is via a PreToolUse hook (`.claude/hooks/lean-ctx-rewrite.{sh,ps1}`) that rewrites each `Bash(<cmd>)` tool call to `lean-ctx -c '<cmd>'`. The hook is a no-op if lean-ctx isn't on `PATH`. The earlier `BASH_ENV` shim approach (which sourced a per-project script into every non-interactive Bash subprocess) was disabled in v0.2.11 after a fork-bomb incident on lean-ctx 3.x — the stub file lives on disk as a `return 0` no-op for defense-in-depth.

Bypass / override matrix:

| Scope | Mechanism |
|---|---|
| Per-call raw output (one command) | `lean-ctx bypass "<cmd>"` |
| Force-compress one command when project default is off | `lean-ctx -c "<cmd>"` |
| Per-project default = off | Add `VCO_LEAN_CTX_DEFAULT=off` to `.claude/env` |
| Disable all VCO hooks (debug only) | `export VCT_DISABLE_HOOKS=1` |

Footgun: lean-ctx in default mode can swallow stderr from `git commit` to zero output when a pre-commit hook fails. If a `git commit` exits non-zero with no message, retry under `lean-ctx bypass "..."`.

Install:
```bash
cargo install lean-ctx
# or
curl -fsSL https://leanctx.com/install.sh | sh
```

Skip with `--no-lean-ctx` if you don't want the auto-detection prompt.

### Joern — control-flow + program-dependence metrics

[Joern](https://docs.joern.io/installation/) is a ~600 MB JVM-based code property graph tool. When installed, the orchestrator's code-graph analyzer can populate CFG (control-flow graph) and PDG (program-dependence graph) metrics on indexed functions. Skip with `--no-joern` if you don't need those metrics.

## vct-hub: project + secrets resolver (v0.2.21+)

`vct-hub` is a small detached `axum` HTTP service that the launcher, the MCP servers, and the bundled scripts all hit instead of reading process-scoped env vars directly. It exists because `os.getenv("KG_COLLECTION")` (and the like) tended to drift between surfaces — different Claude Code surfaces, different shells, different subagent contexts could each end up with a slightly different snapshot of the per-project config.

| Surface | URL / file | Notes |
|---|---|---|
| HTTP listener | `http://127.0.0.1:7700` (default; `VCT_HUB_PORT` to override; falls back to `<vct_root>/hub.port`) | All `/api/v1/*` routes require `Authorization: Bearer <token>`, except `/health` |
| Token | `<vct_root>/hub.token` (mode `0o600`) | Regenerated on every hub startup. Read by every resolver client |
| Lockfile | `<vct_root>/hub.pid` | Single-instance per user |
| CLI | `vct-hub --start-if-not-running` / `--status` / `--stop` / `--foreground` | Also `--register-boot` / `--unregister-boot` / `--boot-status` for the OS auto-start (systemd-user / launchd / Windows Scheduled Task; **default off in v0.2.21**, user opts in via launcher Preferences) |

Key endpoints (auth required):

- `GET /api/v1/projects/{id-or-slug}/config` — resolves KG collection, codegraph prefix, embedding model id, and access-matrix lists for the named project. Use this instead of trusting an inherited env var.
- `GET /api/v1/projects/{id}/env` — resolves secrets from the OS keychain (e.g. shared `github_pat`, `openai_api_key`).
- `GET /api/v1/services/status` — services snapshot (v0.2.21 returns a degraded skeleton; full supervisor relocation lands later).

Resolver client libraries ship in the install bundle: `templates/scripts/vct_project_config.sh` (bash), `templates/scripts/vct_project_config.ps1` (PowerShell 7+), and `vco_lib/project_config.py` (Python). They each discover the hub via `$VCT_HUB_PORT` → `<vct_root>/hub.port` → `7700`, and the token via `$VCT_HUB_TOKEN` → `<vct_root>/hub.token`. The Python client retries on `401` with cache invalidation (so a token rotation mid-session doesn't strand a long-running script).

## Common next steps

- Read [docs/CONFIGURATION.md](CONFIGURATION.md) to understand where each config file lives and why
- Run `/context` inside a Claude session to verify the active workspace path and KG collection name
- Add `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` to `~/.claude/settings.json` to enable parallel agents (3–5x speedup on multi-file tasks)
- If you have an OpenAI key and want it as the default embedding provider, run the OnboardingWizard (Identity → Onboarding) or set it via Preferences → Special Secrets. The orchestrator validates via the free `GET /v1/models/text-embedding-3-small` endpoint — no billing entry created
- Check [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) for container, MCP, hub, and hook issues
