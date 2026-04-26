# Troubleshooting

Common issues during install and first-run. If none of these help, open an issue on [GitHub](https://github.com/hotak92/vibecoded-orchestrator/issues) with the output of `python install.py --help` and your OS/Python version.

## Bypass permissions mode

By default, Claude Code asks for approval on every tool call. With 30+ approvals per setup session, this gets painful. The orchestrator ships with `"defaultMode": "bypassPermissions"` in `.claude/settings.json`. How you opt in depends on which surface you use:

**Claude Code CLI** (or Claude Desktop app): pass the flag directly, no extra config:

```bash
claude --dangerously-skip-permissions
```

The CLI/Desktop app honour `.claude/settings.json` directly, so this flag is the only thing you usually need.

**VS Code extension**: the extension wraps the CLI and gates bypass mode behind an extra setting:

1. Open VS Code Settings (`Ctrl+,` on Windows/Linux, `Cmd+,` on macOS)
2. Search for **"claude bypass"**
3. Enable **"Claude Code: Allow Bypass Permissions Mode"**
4. Restart the VS Code window

When active, you'll see **"Bypass permissions"** in the Claude Code status bar at the bottom of VS Code. Claude will not prompt for individual tool approvals.

You can disable bypass permissions again later by removing the `"defaultMode"` line from `.claude/settings.json`.

## Weaviate container won't start

**Port 8081 already in use**:

```bash
# Linux / macOS
sudo lsof -i :8081
# Or on Linux
sudo netstat -tulpn | grep 8081

# Windows
netstat -ano | findstr :8081
```

Stop whatever is using the port, or change `WEAVIATE_PORT` in `.env` and re-run `python install.py --update`.

**Container runtime not running**:

```bash
# Linux (Podman)
systemctl --user start podman.socket

# Linux / macOS (Docker)
sudo systemctl start docker              # Linux
open -a Docker                           # macOS (Docker Desktop)

# Windows
# Start Docker Desktop from the Start menu
```

**Insufficient memory**: Weaviate needs ~512 MB RAM minimum, 1-2 GB comfortable. On Docker Desktop, raise the memory limit under Settings → Resources.

## Ollama container won't start

**GPU not detected**:

```bash
nvidia-smi                                                # verify NVIDIA driver
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi   # verify Docker GPU access
```

If the second command fails, install the NVIDIA Container Toolkit: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html>

**Port 11435 in use**: same pattern as Weaviate — check with `lsof` / `netstat`, stop the conflicting process, or change `OLLAMA_PORT` in `.env`.

**Model pull fails**:

```bash
df -h                          # check disk space (need ~10-15 GB free)
curl -I https://ollama.com     # check network
```

If stuck, manually pull inside the container:

```bash
podman exec ollama_claude ollama pull qwen3-embedding:0.6b
```

## MCP connection failures in Claude Code

Symptom: Claude says "MCP server weaviate-kg is not connected" or tool calls like `hybrid_search` fail.

**Check MCP status**:

```bash
claude mcp list
# Expected: weaviate-kg ✓ Connected, ollama ✓ Connected
```

**Common causes**:

1. **Editor opened before containers started**: restart your Claude Code session (VS Code window reload, restart the CLI, or reopen Claude Desktop) once `docker ps` / `podman ps` shows Weaviate + Ollama running.
2. **Wrong Python in MCP config**: `MCP_PYTHON` must point at the `install.py`-created venv, not system Python — check `.vscode/settings.json` → `claude-code.env` (VS Code extension) **or** `.claude/settings.json` → `env` (CLI / Desktop app).
3. **Embedding model mismatch**: `ACTIVE_EMBEDDING` must match a model actually loaded by Ollama. Default is `qwen3` with model `qwen3-embedding:0.6b`. Verify with `podman exec ollama_claude ollama list`.

## Scripts in `.claude/scripts/` don't run

**Not executable**:

```bash
chmod +x .claude/scripts/*
```

(On Windows, use the `.ps1` variants — they're shipped alongside shell scripts.)

**Venv not found**: the scripts auto-detect `.venv` at the repo root. If you installed elsewhere, export `VCO_VENV` to point at your venv's root.

## Post-install, Claude doesn't read the knowledge graph

Most common cause: the editor was opened in a different directory than the orchestrator's project root. `KG_BASE_DIR` resolves relative to the working directory of the Claude Code session, so make sure your VS Code workspace root, your CLI's `cwd`, or the folder Claude Desktop has open matches the orchestrator's install dir (or the project dir you configured with the orchestrator).

Verify what Claude sees:

```
In Claude Code: run the skill /context
```

It prints the active workspace path, KG collection name, and recent state.

## Getting more help

- GitHub Issues: <https://github.com/hotak92/vibecoded-orchestrator/issues>
- Community channel: (TBD — linked from vibecodedtools.it at launch)
- Commercial support: Pro and MAO tiers include email support.
