# Claude Code surface compatibility

The orchestrator was originally tested against the VS Code extension,
but every functional piece (hooks, agents, skills, MCP servers, slash
commands, CLAUDE.md) is read by the `claude` CLI binary too. The only
real gap was per-project env injection — VS Code uses
`.vscode/settings.json`'s `claude-code.env` block, which the CLI can't
see.

This doc lists what works on each surface and how to set up
per-project env for the CLI / Desktop surfaces.

## Surface matrix

| Feature | VS Code extension | Claude Code CLI | Claude Desktop |
|---|---|---|---|
| `.claude/settings.json` hooks | ✓ | ✓ | ✓ |
| `~/.claude/settings.json` user hooks | ✓ | ✓ | ✓ |
| `~/.claude.json` MCP servers | ✓ | ✓ | ✓ |
| `.mcp.json` project MCP servers | ✓ | ✓ | partial |
| Agents (`.claude/agents/*.md`) | ✓ | ✓ | ✓ |
| Skills (`~/.claude/skills/`) | ✓ | ✓ | ✓ |
| CLAUDE.md auto-load | ✓ | ✓ | ✓ |
| Slash commands | ✓ | ✓ | ✓ |
| Per-project env injection | `.vscode/settings.json` | `.claude/env` (manual) | `.claude/env` (manual) |
| Stop hooks (`notify-stop.sh` etc.) | ✗ | ✓ | ✓ |

## Per-project env for CLI / Desktop

When the launcher creates a project it writes **both**:

1. `.vscode/settings.json` with a `claude-code.env` block — read by the
   VS Code extension automatically.
2. `.claude/env` — a plain POSIX env file containing the same values
   (`KG_COLLECTION`, `PROJECT_NAME`, `DEVELOPMENT_COLLECTION`,
   `CONVERSATION_COLLECTION`).

The CLI doesn't auto-source `.claude/env`. Three ways to wire it up:

### Option A: bundled wrapper script (recommended)

Use `tools/claude` from this repo. It auto-sources
`$PWD/.claude/env` before exec'ing the real `claude` binary.

Install one of:

```bash
# 1. Symlink into ~/.local/bin BEFORE the real claude:
ln -s /path/to/vibecoded-orchestrator/tools/claude ~/.local/bin/claude
# Make sure ~/.local/bin is earlier on $PATH than /usr/local/bin.

# 2. Or alias in your shell rc:
alias claude='/path/to/vibecoded-orchestrator/tools/claude'
```

The wrapper finds the real `claude` binary by scanning `$PATH` and
skipping itself (so symlinking is safe).

### Option B: direnv

If you use [direnv](https://direnv.net/), add a `.envrc` next to
`.claude/env`:

```bash
# .envrc — auto-sourced by direnv on cd
[[ -f .claude/env ]] && source .claude/env
```

Then run `direnv allow` once per project.

### Option C: manual sourcing

Run `source .claude/env` in the shell before launching `claude`. Lowest
ceremony, easiest to forget.

## Known caveats

- **Stop-event hooks**: `Stop`, `StopFailure`, `SessionEnd` don't fire in
  the VS Code extension as of v2.1.x — use the CLI or Desktop for any
  Stop-event automation. The orchestrator's `notify-stop.sh` and
  `cost-tracker.sh` are affected.
- **Backgrounded subagents**: spawning `run_in_background: true` agents
  works on all three surfaces, but the notification format differs.
- **Effort levels**: `/effort high|max` works on all surfaces but is
  CLI-default; the VS Code extension uses the CLI's setting.

## Why two env files?

VS Code's `claude-code.env` block is read by the extension at session
start and injected into the agent runtime. The CLI doesn't have a
moral equivalent — it inherits the parent shell's env unchanged. So
the launcher writes a plain POSIX `.claude/env` file that any sh-family
shell can source, plus a wrapper that does the sourcing for users who
don't want to fiddle with shell rc.

If/when the Claude Code CLI gains a built-in per-project env mechanism
(e.g. reading `.claude/env` natively), the wrapper becomes a no-op and
we can deprecate it.
