---
title: Claude Code MCP Configuration Pattern
type: tool
tags: [claude-code, mcp, configuration, linux, environment-variables, pattern]
created: 2026-02-04T01:15:00Z
updated: 2026-06-25T00:00:00Z
status: active
priority: high
---

# Claude Code MCP Configuration Pattern

## Problem

Per-project MCP servers (weaviate-kg, search, etc.) need per-project environment (KG collection name, project venv, codegraph prefix) without that environment leaking into other projects on the same machine. The wrong config surface is a common footgun: values can look correct in one file yet never reach the MCP subprocess.

## Canonical channel: `.claude/settings.json` `env`

Per-project env that must reach MCP subprocesses goes in the project's **`.claude/settings.json`** `env` block. This is the one channel that propagates to MCP subprocesses on every Claude Code surface — CLI, Desktop app, and the VS Code extension — on every OS. The launcher's per-project Identity tab writes this file.

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "env": {
    "KG_COLLECTION": "ProjectName_KnowledgeGraph",
    "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
    "DEVELOPMENT_COLLECTION": "ProjectName_Development",
    "CODE_GRAPH_PROJECT": "ProjectName",
    "WEAVIATE_URL": "http://localhost:8081",
    "OLLAMA_URL": "http://localhost:11435"
  }
}
```

MCP server registrations themselves (command, args, machine-invariant env like `WEAVIATE_URL`) live in `~/.claude.json` `mcpServers`, written by the orchestrator install flow (`mcp_registration.rs::build_default_mcp_entries`). Per-project `KG_COLLECTION`-shape keys are intentionally dropped from that surface (`ALLOWED_ENV_KEYS`) so they can't leak across projects; they resolve from `.claude/settings.json` `env` instead.

## The `.vscode/settings.json` footgun

`.vscode/settings.json` `claude-code.env` **does NOT propagate to MCP subprocesses on Linux** (verified against Claude Code 2.1.143). Values look correct in the workspace settings, but the MCP subprocess sees nothing and falls back to bundled defaults. Editing that key for KG / code-graph / embedding routing is the single most common misconfiguration — always use `.claude/settings.json` `env` instead.

Verify what the subprocess actually picked up via the MCP startup log line `weaviate-kg: resolved collections (...)`, which shows the resolved values plus the resolution source (env / hub / default). A fallback to bundled defaults is logged at WARNING.

## Resolution precedence (highest to lowest)

1. **vct-hub-resolved values** — when the launcher is running, the MCP queries `vct-hub` on import and the hub returns the per-project config from `launcher.db`.
2. **`.claude/settings.json` `env`** — the cross-editor canonical channel.
3. **`.claude/env`** — shell-sourced, for CLI users who `source` it from their rc.
4. **`~/.claude.json mcpServers.<name>.env`** — restricted to machine-invariant keys.
5. **Bundled defaults** in `server.py` — last-resort, logged at WARNING when reached.

### Verification

**Servers connect:**
```bash
claude mcp list   # expect weaviate-kg ✓ Connected, search ✓ Connected
```

**Per-project env reached the subprocess:**
- Inside a session, run `/context` — it prints the active workspace path and KG collection name.
- Read the MCP startup log line `weaviate-kg: resolved collections (...)` for the resolved values + source.
- If the collection is wrong (e.g. reusing another project's KG), the session was opened in the wrong directory, or the env was placed in `.vscode/settings.json` instead of `.claude/settings.json`.

### Best Practices

1. **Per-project env in `.claude/settings.json` `env`** — the only channel that reaches MCP subprocesses on every surface.
2. **Never use `.vscode/settings.json` `claude-code.env`** for KG / code-graph / embedding routing — it does not propagate to MCP subprocesses on Linux.
3. **Machine-invariant keys only in `~/.claude.json`** — `WEAVIATE_URL`, `OLLAMA_URL`; per-project collection keys are dropped from this surface.
4. **Reload after changes** — the VS Code extension needs a window reload for settings changes to take effect.
5. **Verify the resolution source** — the `resolved collections` log line tells you whether the value came from env, hub, or the bundled default.

### Related Patterns

- [[uses::Environment Variables]] - Shell environment variable expansion
- [[relatedTo::Claude Code Settings]] - Other Claude Code configuration options
- [[relatedTo::Project Separation]] - Keeping project-specific configs isolated

---

**Key Insight**: per-project MCP env belongs in `.claude/settings.json` `env`, the one channel that propagates to MCP subprocesses across CLI, Desktop, and the VS Code extension. The `.vscode/settings.json` `claude-code.env` key is a footgun on Linux — it looks correct but the subprocess never sees it.
