# Claude Code surface compatibility

The orchestrator was originally tested against the VS Code extension,
but every functional piece (hooks, agents, skills, MCP servers, slash
commands, CLAUDE.md) is read by the `claude` CLI binary too. The only
real gap was per-project env injection — and as of Bug 30 the launcher
now writes the canonical `.claude/settings.json` `env` block which is
read by all three surfaces (CLI, Desktop app, VS Code extension).

This doc lists what works on each surface and how the per-project env
files relate to each other.

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
| Per-project env injection | `.claude/settings.json` env (Bug 30) + `.vscode/settings.json` claude-code.env | `.claude/settings.json` env (Bug 30) + `.claude/env` shell file | `.claude/settings.json` env (Bug 30) |
| Stop hooks (`notify-stop.sh` etc.) | ✗ | ✓ | ✓ |

## Per-project env files

When the launcher creates a project it writes **three** files, all
carrying the same env values (`KG_COLLECTION`, `PROJECT_NAME`,
`DEVELOPMENT_COLLECTION`, `CONVERSATION_COLLECTION`):

1. **`.claude/settings.json`** with an `env` block — Anthropic's
   canonical per-project env mechanism. Read by Claude Code CLI, the
   Desktop app, AND the VS Code extension. This is the **primary**
   path; without it, Desktop app users get no per-project KG routing.
   The launcher does a read-merge-write so existing hooks /
   permissions / agents config in the same file are preserved
   untouched — only the top-level `env` key is overwritten.
2. **`.vscode/settings.json`** with a `claude-code.env` block — the
   VS Code-extension-specific path. Kept for compatibility / user
   preference; same values as (1) so there is no precedence conflict
   when both are present.
3. **`.claude/env`** — a plain POSIX env file containing the same
   values. Useful for users who launch `claude` from a shell wrapper
   (see Option A below) or who want to source it manually.

The CLI doesn't auto-source `.claude/env`. With (1) in place this is
no longer required for KG routing, but the wrapper is still useful if
you want extra env vars beyond the four the launcher manages. Three
ways to wire it up:

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

## Why three env files?

Historical: before Bug 30 we only wrote `.vscode/settings.json` (for
the extension) and `.claude/env` (for shell-wrapper CLI users).
Desktop app users had no path to per-project env at all. Bug 30 added
the canonical `.claude/settings.json` `env` block which Anthropic
documents as the cross-surface mechanism (CLI + Desktop + extension).

We keep the other two files for compatibility:
- `.vscode/settings.json` `claude-code.env` is the path users with
  pre-Bug-30 muscle memory will look for first.
- `.claude/env` is useful as a sh-sourceable file for shell wrappers
  and direnv setups, especially when users want to extend it with
  extra env vars beyond the four the launcher manages.

Same values in all three files means there is no precedence conflict
to reason about. If/when the Claude Code surfaces unify on
`.claude/settings.json`, the other two files become redundant.

## Linux Desktop app gap

Anthropic's Desktop app is macOS / Windows only as of v2.1.x. Linux
users without VS Code must use the CLI surface (with the `tools/claude`
wrapper or one of the alternatives below). This is an upstream
limitation we cannot work around from the launcher.
