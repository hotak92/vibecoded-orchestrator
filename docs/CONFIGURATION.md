# Configuration Philosophy

Config layout follows one rule: **minimal global, maximum per-project**.

## What this means

- **Global `~/.claude/settings.json`**: user preferences only — effort level, output tokens, universal permission denies. No project paths, no MCP server URLs, no environment variables that any specific project depends on.
- **Per-project `.vscode/settings.json`**: where `claude-code.env` lives for the **VS Code extension**. MCP env vars (Weaviate URL, collection names, embedding backend) live here so opening this project in VS Code wires up its MCP servers correctly without affecting any other project you have open.
- **Per-project `.claude/settings.json`**: per-project permissions and hook registrations, plus an `env` block read by the **Claude Code CLI and Desktop app**. The launcher writes both files in lockstep so all three surfaces (VS Code extension / CLI / Desktop) see the same MCP environment.
- **Per-project secrets**: stored in the OS keychain via the VCT Launcher GUI — not in env files, not in JSON configs. The launcher knows about per-project scoping, so an OpenAI key for one project doesn't leak into another.

## Why

It prevents cross-contamination. Global settings apply to every project you open — set `KG_COLLECTION=MyMainProjectKG` globally and every other project will silently reuse that collection and mix knowledge graphs.

## Setup for new users

1. Copy `.vscode/settings.json.example` to `.vscode/settings.json` and adjust if needed (defaults work out of the box for a local Podman+Ollama setup).
2. Copy `.env.example` to `.env` for shell/script use.
3. Let `install.py` wire the rest (venv, containers, KG collection creation).
4. Launch via the VCT Launcher GUI (manages secrets, tier gating, module installs).

## What goes in each file

| Config | Lives in | Scope | Managed by |
|---|---|---|---|
| Effort level, max tokens, OS-level denies | `~/.claude/settings.json` | global | you, manually |
| MCP env (URLs, collection names, paths) — VS Code extension | `.vscode/settings.json` → `claude-code.env` | per-project | VS Code + launcher |
| MCP env (URLs, collection names, paths) — CLI / Desktop app | `.claude/settings.json` → `env` | per-project | launcher (kept in sync with the VS Code copy) |
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

Claude Code can run multiple agents concurrently on independent sub-tasks. Turn it on globally:

```json
// ~/.claude/settings.json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

With this on, asking the orchestrator to refactor 30 files or analyze 5 directories spawns up to 3 parallel agents instead of doing the work sequentially. Typical speedup on multi-file tasks: 3-5x.

This is the only global env var worth setting; everything else is per-project.

