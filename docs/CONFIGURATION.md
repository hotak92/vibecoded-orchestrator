# Configuration Philosophy

This project follows a **minimal global, maximum per-project** config principle.

## What this means

- **Global `~/.claude/settings.json`**: user preferences only (effort level, output tokens, universal permission denies). No project paths, no MCP server URLs, no environment variables that any given project actually uses.
- **Per-project `.vscode/settings.json`**: where `claude-code.env` lives. MCP env vars (Weaviate URL, collection names, embedding backend, etc.) are set here so opening this project in VS Code automatically wires up its Claude Code MCP servers correctly, without affecting any other project you have open.
- **Per-project `.claude/settings.json`**: per-project permissions and hook registrations.
- **Per-project secrets**: stored in OS keychain via the VCT Launcher GUI (not env files, not JSON configs). The launcher knows about per-project scoping so e.g. an OpenAI key for one project doesn't leak into another.

## Why

Prevents cross-contamination. Global settings default to every project you open — if you set `KG_COLLECTION=MyMainProjectKG` globally, every other project will silently reuse that collection and mix knowledge graphs.

## Setup for new users

1. Copy `.vscode/settings.json.example` to `.vscode/settings.json` and adjust if needed (defaults work out of the box for a local Podman+Ollama setup).
2. Copy `.env.example` to `.env` for shell/script use.
3. Let `install.py` wire the rest (venv, containers, KG collection creation).
4. Launch via the VCT Launcher GUI (manages secrets, tier gating, module installs).

## What goes in each file

| Config | Lives in | Scope | Managed by |
|---|---|---|---|
| Effort level, max tokens, OS-level denies | `~/.claude/settings.json` | global | you, manually |
| MCP env (URLs, collection names, paths) | `.vscode/settings.json` → `claude-code.env` | per-project | VS Code + launcher |
| Shell/script env | `.env` | per-project | you, `.env.example` template |
| Project permissions + hooks | `.claude/settings.json` | per-project | install.py + launcher |
| Secrets (license keys, API tokens) | OS keychain | per-project | launcher GUI only |
| Hooks scripts | `.claude/hooks/` | per-project | install.py |
| Project agents | `.claude/agents/` | per-project | install.py (from `templates/agents/free/`) |
| MAO specialist agents | `.claude/agents/` | per-project | install.py `--with-mao-agents` (MAO license) |
| Project skills | `.claude/skills/` | per-project | install.py (from `templates/skills/`) |
| Generic agents (e.g. `code-migrator`) | `~/.claude/agents/` | global | you, optional |
| Generic skills (e.g. `debug-expert`) | `~/.claude/skills/` | global | you, optional |

## What does NOT go in global

- MCP server definitions (they point at this project's venv + source paths)
- Plugin enable flags (`enabledPlugins`) — plugins are project-specific
- Project paths (`MCP_PYTHON`, `MCP_WEAVIATE_SERVER`, etc.)
- Collection names (`KG_COLLECTION`, etc.)
- Embedding model defaults (differ per project tier)

If you see any of these in your global `~/.claude/settings.json`, move them to the per-project config. They're leaking.

## Agents and skills

See [templates/README.md](../templates/README.md) for the tier split (free vs MAO) and install-flag reference.

## Parallel agents (3-5x speedup)

Claude Code can run multiple agents concurrently on independent sub-tasks. Enable globally:

```json
// ~/.claude/settings.json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

With this on, when you ask the orchestrator to refactor 30 files or analyze 5 directories, it spawns up to 3 parallel agents instead of doing the work sequentially. Typical speedup on multi-file tasks: 3-5x.

This is the only global env var we recommend setting — everything else is per-project.

