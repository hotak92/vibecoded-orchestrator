# Bootstrap

**Read this first if you (or your AI assistant) just opened this repo.**

You're looking at **VibeCoded Orchestrator** — an AI coding orchestrator built on top of Claude Code. It adds:
- **Knowledge Graph** with semantic search across your project
- **Code Graph** that understands your repo structurally (modules, classes, functions, APIs, cross-service calls)
- **16 automation hooks** (KG sync on file edits, security scans, context injection, post-commit reviews)
- **MCP servers** for Weaviate (KG + code graph), Ollama (local LLM), web search
- **Curated agent + skill bundle** for Claude Code (architects, code reviewers, debuggers, etc.)

How you got here matters — the next steps depend on it.

---

## Path A: You opened this through the VCT Launcher

If you installed via the **VCT Launcher** desktop app, **almost everything is already done**:

- Container services (Weaviate, Ollama) are running
- Secrets are managed by the launcher's keychain
- KG and code graph collections are bound to your project name
- Hooks are wired into `.claude/settings.json`
- The launcher knows about this project and tracks its agents/skills/permissions matrix

**What you (the human) should do**: just start coding. Claude Code sessions in this folder will pick up the hooks and MCP servers automatically.

**What the AI assistant reading this should do**: nothing special. The orchestrator is ready. Read [`CLAUDE.md`](CLAUDE.md) for the operating manual.

If something looks broken — hooks not firing, MCPs unreachable, secrets missing — open the VCT Launcher, navigate to this project, and run **Doctor** (or the equivalent diagnostic). Don't try to fix it manually first; the launcher knows what it set up.

---

## Path B: You cloned this from GitHub directly (no launcher)

You can absolutely run the orchestrator without the launcher — but you'll miss some integrations and have to set things up by hand. Trade-offs first, then steps.

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
- The orchestrator's **core value** (KG, code graph, hooks, MCPs, agents, skills)
- All four free-tier embedding modes (GPU / CPU / OpenAI / low-resource)
- Cross-platform support (Linux / macOS / Windows + WSL)
- The right to install paid modules later by just installing the launcher then

### Standalone install steps

**Prerequisites** (install before running `install.py`):

| Tool | Why | Install |
|---|---|---|
| Python ≥ 3.11 | The orchestrator + MCP servers run on it | `python3 --version` to check |
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
7. Pull Ollama models (qwen3-embedding, qwen3:0.6b, etc.)
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

Open the project folder in **VS Code**. Start a Claude Code session. The hooks fire automatically. Try editing a file and watch the post-edit hook auto-sync to the code graph.

### When you decide you want the launcher

Install [VCT Launcher](https://github.com/pb992/VCT-Launcher), point it at this folder, and run "Adopt project". The launcher imports the existing setup (KG bindings, hooks, secret references) into its DB without disrupting anything. From that point on, the launcher manages this project the way Path A describes.

---

## What every assistant reading this should know

Beyond paths A/B, the operating manual is in [`CLAUDE.md`](CLAUDE.md). It covers:
- KG-first search policy (use `hybrid_search` before grep for conceptual queries)
- The two-layer memory pattern (`MEMORY.md` for stable facts, `.claude/CONTEXT_STATE.md` for current task)
- Hook events + when each one fires
- Agents/skills + when to spawn them (Opus / Sonnet / Haiku decision tree)
- Communication style: direct, no fluff, no superlatives, no premature validation

If you're an AI assistant and the user dropped you in here without context, your **first action** should be:
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

For deeper issues, see `docs/TROUBLESHOOTING.md` and `docs/CONFIGURATION.md`.
