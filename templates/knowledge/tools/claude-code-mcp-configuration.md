---
title: Claude Code MCP Configuration Pattern
type: tool
tags: [claude-code, mcp, configuration, linux, environment-variables, pattern]
created: 2026-02-04T01:15:00Z
updated: 2026-10-18T00:00:00Z
status: active
priority: high
---

# Claude Code MCP Configuration Pattern

## Problem

Per-project MCP servers (weaviate-kg, search, etc.) need per-project environment (KG collection name, project venv, codegraph prefix) without that environment leaking into other projects on the same machine. The wrong config surface is a common footgun: values can look correct in one file yet never reach the MCP subprocess.

## Canonical channel: the project's Claude Code settings `env`

Per-project env that must reach MCP subprocesses goes in the project's Claude Code settings `env`, which is split across two files. Claude Code merges them key by key, and `.claude/settings.local.json` wins on a shared key. The merged env reaches the session, its hooks and its MCP subprocesses (verified on the Claude Code CLI, 2.1.285). The VCO env projection writes both files.

- **`.claude/settings.json`** (committable) holds the portable keys: collection names, project name, shared-KG gates and the embedding profile.
- **`.claude/settings.local.json`** (per-machine, kept out of git) holds keys whose value describes this machine: absolute paths (`KG_BASE_DIR`, `VCT_ORCHESTRATOR_ROOT`), launcher ids and access lists (`VCT_PROJECT_ID`), and service URLs and ports (`WEAVIATE_URL`, `OLLAMA_URL`, …).

```jsonc
// .claude/settings.json
{
  "env": {
    "KG_COLLECTION": "ProjectName_KnowledgeGraph",
    "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
    "DEVELOPMENT_COLLECTION": "ProjectName_Development",
    "CODE_GRAPH_PROJECT": "ProjectName"
  }
}
// .claude/settings.local.json
{
  "env": {
    "KG_BASE_DIR": "/path/to/ProjectName",
    "WEAVIATE_URL": "http://localhost:8081",
    "OLLAMA_URL": "http://localhost:11435"
  }
}
```

If git already tracks `settings.local.json`, VCO writes all keys to `settings.json` instead and records a `settings_local_json_tracked` deferral. If git cannot answer (for example it refuses the folder as unsafe), VCO also keeps all keys in `settings.json`, logs a warning and records a `settings_local_layer_unknown` entry until git answers. A value you put in `settings.local.json` yourself is kept.

MCP server registrations themselves (command, args, machine-invariant env like `WEAVIATE_URL`) live in `~/.claude.json` `mcpServers`, written by the orchestrator install flow (`mcp_registration.rs::build_default_mcp_entries`). Per-project `KG_COLLECTION`-shape keys are intentionally dropped from that surface (`ALLOWED_ENV_KEYS`) so they can't leak across projects; they resolve from the project's settings `env` instead.

## The `.vscode/settings.json` footgun

`.vscode/settings.json` `claude-code.env` **does NOT propagate to MCP subprocesses on Linux** (verified against Claude Code 2.1.143). Values look correct in the workspace settings, but the MCP subprocess sees nothing and falls back to bundled defaults. Editing that key for KG / code-graph / embedding routing is the single most common misconfiguration — always use the project's settings `env` instead.

Verify what the subprocess actually picked up via the MCP startup log line `weaviate-kg: resolved collections (...)`, which shows the resolved values plus the resolution source (env / hub / default). A fallback to bundled defaults is logged at WARNING.

## Resolution precedence (highest to lowest)

1. **vct-hub-resolved values** — when the launcher is running, the MCP queries `vct-hub` on import and the hub returns the per-project config from `launcher.db`.
2. **Project settings `env`** — `.claude/settings.json` overlaid by `.claude/settings.local.json` (local wins), the cross-editor canonical channel.
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
- If the collection is wrong (e.g. reusing another project's KG), the session was opened in the wrong directory, or the env was placed in `.vscode/settings.json` instead of `.claude/settings.json` / `.claude/settings.local.json`.

### Best Practices

1. **Per-project env in the project's settings `env`** — portable keys in `.claude/settings.json`, machine-specific values in `.claude/settings.local.json`; never commit the local file.
2. **Never use `.vscode/settings.json` `claude-code.env`** for KG / code-graph / embedding routing — it does not propagate to MCP subprocesses on Linux.
3. **Machine-invariant keys only in `~/.claude.json`** — `WEAVIATE_URL`, `OLLAMA_URL`; per-project collection keys are dropped from this surface.
4. **Reload after changes** — the VS Code extension needs a window reload for settings changes to take effect.
5. **Verify the resolution source** — the `resolved collections` log line tells you whether the value came from env, hub, or the bundled default.

### Related Patterns

- [[uses::Environment Variables]] - Shell environment variable expansion
- [[relatedTo::Claude Code Settings]] - Other Claude Code configuration options
- [[relatedTo::Project Separation]] - Keeping project-specific configs isolated

---

**Key Insight**: per-project MCP env belongs in the project's settings `env`. Portable keys go in `.claude/settings.json`, and this machine's paths, ids and ports go in the git-excluded `.claude/settings.local.json`, which Claude Code merges over it. The `.vscode/settings.json` `claude-code.env` key is a footgun on Linux — it looks correct but the subprocess never sees it.
