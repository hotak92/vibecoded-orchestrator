# Getting Started

This guide walks you through installing the orchestrator, configuring your first project, and understanding what runs during a normal Claude Code session.

## Prerequisites

- Python 3.11+
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

Before bringing up its own containers, `install.py` probes each port and content-fingerprints the response:

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
