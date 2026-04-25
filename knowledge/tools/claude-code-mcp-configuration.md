---
title: Claude Code MCP Configuration Pattern
type: tool
tags: [claude-code, mcp, configuration, linux, environment-variables, pattern]
created: 2026-02-04T01:15:00Z
updated: 2026-04-05T14:34:48Z
status: active
priority: high
---

# Claude Code MCP Configuration Pattern

## Problem

Claude Code on **Linux has issues loading MCP servers** from `.mcp.json` (project-level config), even though:
- The CLI correctly reads `.mcp.json` and shows servers as ✓ Connected
- The official documentation suggests `.mcp.json` should work
- Multiple GitHub issues confirm this is a known bug

## Solution: User-Level Config with Environment Variables

### Configuration Location

**Only working configuration for Claude Code on Linux:**

**`~/.claude/settings.json`** with `mcpServers` field (NOT `.vscode/settings.json`, NOT `claude.mcpServers`)

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",

  "mcpServers": {
    "server-name": {
      "command": "${MCP_PYTHON:-/default/path/to/python}",
      "args": ["${MCP_SERVER_PATH:-/default/path/to/server.py}"],
      "env": {
        "VAR_NAME": "${VAR_NAME:-default-value}",
        "PYTHONPATH": "${MCP_PYTHONPATH:-/default/pythonpath}"
      }
    }
  },

  "env": {
    "MCP_PYTHON": "/default/path/to/python",
    "MCP_SERVER_PATH": "/default/path/to/server.py",
    "MCP_PYTHONPATH": "/default/pythonpath",
    "VAR_NAME": "default-value"
  }
}
```

### Pattern: Project-Specific Override

**Challenge**: `~/.claude/settings.json` is user-level (global), but projects need different MCP configurations.

**Solution**: Use environment variables with defaults

**Step 1: User-level defaults** (`~/.claude/settings.json`):
- Define MCP servers with `${VAR:-default}` syntax
- Set default project's values in `env` field
- Works globally when no overrides present

**Step 2: Project-specific overrides** (`.vscode/settings.json`):
```json
{
  "claude-code.env": {
    "MCP_PYTHON": "/path/to/project/.venv/bin/python",
    "MCP_PYTHONPATH": "/path/to/project/src",
    "KG_COLLECTION": "ProjectName_KG"
  }
}
```

### Why This Works

1. **User-level config shared between CLI and extension** - Both read `~/.claude/settings.json`
2. **Environment variables provide flexibility** - Each project can override via VS Code settings
3. **Defaults ensure it works without setup** - Primary project works out of the box
4. **Absolute paths required** - No `${workspaceFolder}` support in user-level config

### Alternative Configurations (DO NOT USE)

These work for CLI but NOT for Claude Code on Linux:

❌ `.mcp.json` in project root - CLI only
❌ `.vscode/settings.json` with `claude.mcpServers` - Not read by Claude Code
❌ Relative paths or `${workspaceFolder}` - Not expanded in user-level config

### Verification

**Test servers work:**
```bash
# CLI should show ✓ Connected
export PATH="$HOME/.local/bin:$PATH"
claude mcp list
```

**Test Claude Code loaded them:**
- Reload: Command Palette → "Developer: Reload Window"
- Ask Claude: "What tools do you have?"
- Should see: `mcp__servername__toolname` tools

### Known Issues

**Linux Claude Code Extension**:
- MCP tools not exposed despite connection
- VS Code extension doesn't use MCP servers
- Native UI prevents MCP server connection

**Workaround**: Use Claude Code CLI (`claude` command in terminal) for full project-scoped MCP support.

### Best Practices

1. **Use absolute paths** - No workspace variables in user-level config
2. **Test with CLI first** - `claude mcp list` should show ✓ Connected before expecting VS Code to work
3. **Reload after changes** - VS Code needs full reload for MCP config changes
4. **One project = defaults** - Set your primary project as defaults in `env` field
5. **Others override** - Secondary projects override via `.vscode/settings.json`

### Related Patterns

- [[uses::Environment Variables]] - Shell environment variable expansion
- [[relatedTo::Claude Code Settings]] - Other Claude Code configuration options
- [[relatedTo::Project Separation]] - Keeping project-specific configs isolated

---

**Key Insight**: VS Code extension has issues with project-scoped MCP on Linux. User-level config with environment variable overrides provides flexibility while working around the bug.

**Status**: Active workaround until the issue is fixed upstream.
