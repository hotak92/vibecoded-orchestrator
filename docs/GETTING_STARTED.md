# Getting Started

This guide walks you through installing the orchestrator, configuring your first project, and understanding what runs during a normal Claude Code session.

## Prerequisites

- **Python 3.11 or newer** (3.12 recommended; 3.13 supported). Older versions fail at the install.py sentinel — we use stdlib `tomllib`, which is 3.11+.
  - Linux: `sudo apt install python3.12 python3.12-venv` (or `sudo dnf install python3.12`, `sudo pacman -S python`)
  - macOS: `brew install python@3.12`
  - Windows: `winget install Python.Python.3.12`
  - Or: <https://python.org/downloads/>
  - The `install.sh` / `install.ps1` wrappers can do this for you interactively if Python is missing.
- Docker or Podman (for Weaviate + Ollama containers)
- Claude Code CLI (`npm install -g @anthropic-ai/claude-code`) with a Claude Max subscription
- Node.js 18+

## Install

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

`install.py` (called by both scripts) does the following:

1. Creates a Python venv at `.venv/`
2. Detects your hardware (NVIDIA GPU / CPU / Apple Silicon) and sets the embedding backend
3. Starts Weaviate and Ollama in containers and waits for them to be ready
4. Pulls embedding models (`qwen3-embedding:0.6b` by default; CodeSage-Large-v2 on GPU installs)
5. Writes `.env`, `.claude/settings.json`, and `.vscode/settings.json`
6. Copies 19 agent templates into `.claude/agents/` and 28 skill templates into `.claude/skills/`

**Time budget**: ~5 min of interactive prompts, then 10–30 min for container images and model downloads (~5 GB; GPU mode pulls an additional ~2.5 GB). Re-runs reuse cached images.

### Common install flags

```
python install.py --gpu               # Enable NVIDIA GPU acceleration
python install.py --cpu-only          # Force CPU-only mode
python install.py --low-resource      # Lightest models for low-RAM machines
python install.py --with-mao-agents   # Install 10 MAO-tier specialist agents (MAO license)
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

When install adopts an existing Weaviate, it must not pollute the host with bare top-level `KnowledgeGraph` / `Development` collections — many users run Weaviate with per-project namespacing (`ARTup_KnowledgeGraph`, `ClaudeKnowledgeGraph`, etc.). Adopt mode therefore:

1. **Derives the per-install KG name from the project basename** — installing in `~/projects/myapp/` writes to `Myapp_KnowledgeGraph` and `Myapp_Development`. Hyphens / underscores in the basename are PascalCased; pure-punctuation basenames fall back to `vct_KnowledgeGraph`.
2. **Honors `KG_COLLECTION` / `DEVELOPMENT_COLLECTION` env vars** if set (typically via `.vscode/settings.json` `claude-code.env`) — explicit override wins over the basename derivation.
3. **Skips creation of any collection that already exists** under the resolved name.
4. **Skips a `Development` collection entirely** if the host already has any `<X>_development` (the host's namespacing wins).
5. **Announces every proposed creation and waits for confirmation** in interactive mode. Pass `--yes` for non-interactive runs.
6. **Does not auto-adopt cross-project shared KGs** like an existing `ClaudeKnowledgeGraph`. The orchestrator runs an orphan-prune sync that deletes entries whose `file_path` no longer exists in the active project, so two installs sharing one collection would silently delete each other's entries. vco always creates its own `VibeCodedTools_KnowledgeGraph` (or skips creation if it already exists).

When install starts its own Weaviate (no adoption), bare `KnowledgeGraph` / `Development` defaults are kept — there's nothing else in the instance to namespace against.

#### Skipping collection creation

`--skip-seed` skips both the seed step and the schema bootstrap (no content to seed into anyway). The MCP server creates collections lazily on first write, so subsequent operations still work:

```
python install.py --skip-seed             # skip seed + collection bootstrap
python install.py --skip-collections      # bootstrap-only opt-out (still seeds)
```

#### Opting out of the shared cross-project KG

Set `SHARED_KG_OPT_OUT=true` in `.env` (or in the install environment) to disable the `VibeCodedTools_KnowledgeGraph` shared collection per-project. Useful for users who want hard isolation between projects.

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
- **VS Code extension**: open the folder as a workspace; the extension reads `.vscode/settings.json` for MCP env vars
- **Claude Desktop app**: point it at the install directory

On session start, three things happen automatically (via `SessionStart` hooks):

1. `ensure-containers.sh` — checks that Weaviate and Ollama are running; starts them if not
2. `context-size-check.sh` — warns if `.claude/CONTEXT_STATE.md` is over 200 lines
3. `compact-context-reinject.sh` (on resume after compaction) — reinjects `CONTEXT_STATE.md`, recent commits, and any active plan

Verify MCP servers connected:

```bash
claude mcp list
# Expected: weaviate-kg ✓ Connected, ollama ✓ Connected
```

## Set up a project

The orchestrator can configure another codebase to use its knowledge graph and code graph. From inside the orchestrator session:

```
You: "Set up my FastAPI project at ~/dev/my-api"
```

Claude will analyze the codebase and write four files into the target project:

- `~/dev/my-api/.claude/settings.json` — permissions and hook registrations
- `~/dev/my-api/.vscode/settings.json` — MCP env with `KG_COLLECTION=MyAPI`
- `~/dev/my-api/CLAUDE.md` — project instructions tailored to the detected stack
- `~/dev/my-api/.claude/CONTEXT_STATE.md` — initial session state

It also queues a background code graph analysis of the target project.

Alternatively, use the VCT Launcher GUI: it runs the same configuration wizard visually and writes all three config files (`.claude/settings.json`, `.vscode/settings.json`, `.claude/env`) in lockstep so the CLI, VS Code extension, and Claude Desktop app all see the same MCP environment.

## What runs during a session

Once a project is configured, here is what fires on normal use:

**On every prompt** (`UserPromptSubmit` hook):
- Searches the Knowledge Graph for nodes relevant to your query
- Injects matches into the context window before Claude generates a response

**On every file edit** (`PostToolUse` hook on `Edit`/`Write`):
- Files under `knowledge/` sync to Weaviate (`ClaudeKnowledgeGraph` collection)
- Files under `docs/` sync to the development collection
- Code files are queued for code graph re-analysis
- A credential scan runs on the written file

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

## Common next steps

- Read [docs/CONFIGURATION.md](CONFIGURATION.md) to understand where each config file lives and why
- Run `/context` inside a Claude session to verify the active workspace path and KG collection name
- Add `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` to `~/.claude/settings.json` to enable parallel agents (3–5x speedup on multi-file tasks)
- Check [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) for container, MCP, and hook issues
