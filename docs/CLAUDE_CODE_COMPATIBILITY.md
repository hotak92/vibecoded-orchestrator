# Claude Code surface compatibility

The orchestrator was originally tested against the VS Code extension,
but every functional piece — hooks, agents, skills, MCP servers, slash
commands, CLAUDE.md — is read by the `claude` CLI binary too. The
launcher writes the canonical `.claude/settings.json` `env` block, which
is read by all three surfaces (CLI, Desktop app, VS Code extension), so
per-project env routing works uniformly across them.

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
| Per-project env injection | `.claude/settings.json` env | `.claude/settings.json` env + `.claude/env` shell file | `.claude/settings.json` env |
| Stop hooks (`notify-stop.sh` etc.) | ✓ | ✓ | ✓ |

## Per-project env files

The launcher writes **two** files when it creates a project, both
carrying the same env values (`KG_COLLECTION`, `PROJECT_NAME`,
`DEVELOPMENT_COLLECTION`):

1. **`.claude/settings.json`** with an `env` block — Anthropic's
   canonical per-project env mechanism. Read by the Claude Code CLI,
   the Desktop app, AND the VS Code extension, **and** propagated to
   MCP subprocesses on every platform we've tested. This is the
   canonical channel for per-project MCP env. The launcher does a
   read-merge-write so existing hooks, permissions, and agents config
   in the same file stay untouched — only the top-level `env` key is
   overwritten.
2. **`.claude/env`** — a plain POSIX env file containing the same
   values. Useful for users who launch `claude` from a shell wrapper
   (see Option A below) or who want to source it manually.

> **v0.2.12 (PR-27, 2026-05-16) — third surface removed.** Pre-v0.2.12
> the launcher also wrote `.vscode/settings.json` `claude-code.env`
> as a third surface. Empirical sentinel testing on Linux Claude Code
> 2.1.143 confirmed that block does NOT propagate to MCP subprocesses
> on Linux — the chat process didn't see those env vars either. Writing
> the key caused user confusion ("I edited the file but nothing
> changed"). The write was removed; `.claude/settings.json` `env`
> remains the canonical channel. See the PR-27 commit message for the
> full empirical trace, including the `/proc/<mcp_pid>/environ`
> sentinel-test methodology used to verify propagation behaviour.
>
> `.vscode/settings.json` is still useful for VS Code editor
> preferences (Pylance excludes, file-watcher excludes, formatter
> settings), and the launcher's Python-side
> `_backfill_vscode_excludes_in_project` still manages the
> Pylance/watcher exclude block. It just no longer carries
> `claude-code.env`.

The CLI doesn't auto-source `.claude/env`. With (1) in place this is
no longer required for KG routing, but the wrapper is still useful if
you want extra env vars beyond the four the launcher manages. Three
ways to wire it in:

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

- **Stop-event hooks** (refreshed 2026-05-11): `Stop`, `StopFailure`,
  `SessionEnd` are documented universally per the current
  [hooks reference](https://code.claude.com/docs/en/hooks) — no
  VS Code carve-out. The earlier note here claimed VS Code didn't
  fire these in v2.1.x; that claim no longer matches the official
  docs, and the [VS Code feature-gap table](https://code.claude.com/docs/en/vs-code)
  doesn't list hook events as missing. The orchestrator's
  `notify-stop.sh` and `cost-tracker.sh` should fire on every
  surface that loads `.claude/settings.json`. (Empirical probe in
  VS Code v2.1.138+ recommended before relying on this; if the
  hooks don't fire in practice, file via `/feedback` since docs
  claim parity.)
- **Backgrounded subagents**: spawning `run_in_background: true` agents
  works on all three surfaces, but the notification format differs.
- **Effort levels**: `/effort high|max` works on all surfaces but is
  CLI-default; the VS Code extension uses the CLI's setting.

## Why two env files?

`.claude/settings.json` `env` is the canonical cross-surface mechanism
documented by Anthropic — read by the CLI, the Desktop app, AND the VS
Code extension, and propagated to MCP subprocesses on every platform
we've tested. The launcher writes it on every project create. The
second file stays around for shell-wrapper users:

- `.claude/env` is useful as a sh-sourceable file for shell wrappers
  (`tools/claude`) and direnv setups, especially when users want to
  extend it with extra env vars beyond the four the launcher manages.

Because both files carry the same values, there's no precedence
conflict to reason about. v0.2.12 (PR-27, 2026-05-16) dropped the
historical third surface (`.vscode/settings.json` `claude-code.env`)
because it did not propagate to MCP subprocesses on Linux — see the
"Per-project env files" section above for the empirical-trace
reference.

## Linux Desktop app gap

Anthropic's Desktop app is macOS / Windows only as of v2.1.x. Linux
users without VS Code have to use the CLI surface (with the
`tools/claude` wrapper or one of the alternatives above). Upstream
limitation — nothing the launcher can do about it.
