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

> **Note on `.vscode/settings.json`**: the `claude-code.env` block in
> `.vscode/settings.json` does NOT propagate to MCP subprocesses on
> Linux (verified empirically via `/proc/<mcp_pid>/environ` sentinel
> testing) — do not use it for per-project MCP/KG env vars;
> `.claude/settings.json` `env` is the canonical channel.
> `.vscode/settings.json` is still useful for VS Code editor
> preferences (Pylance excludes, file-watcher excludes, formatter
> settings), and the launcher's Python-side
> `_backfill_vscode_excludes_in_project` manages the Pylance/watcher
> exclude block. It does not carry `claude-code.env`.

The CLI doesn't auto-source `.claude/env`. With (1) in place it is
not required for KG routing, but the wrapper is still useful if
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

## Hook command anchoring (`${CLAUDE_PROJECT_DIR}`)

A hook command runs in the session's CURRENT directory, and that directory
follows `cd` and worktrees — a relative hook path (`.claude/hooks/x.sh`,
the form every VCO release before v0.2.97 wrote) fails with "No such file
or directory" as soon as the cwd moves. VCO therefore anchors every shipped
hook command at the project root through Claude Code's
`${CLAUDE_PROJECT_DIR}` "project root where the session started"
placeholder. The mechanism, per OS:

- **Linux/macOS** — `bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/x.sh"`.
  Claude Code substitutes the placeholder in the command where it supports
  that, and exports `CLAUDE_PROJECT_DIR` as an environment variable where
  it does not (shell-form hooks run through `sh -c`, so the double-quoted
  `${CLAUDE_PROJECT_DIR}` expands there). The POSIX `:-` default covers the
  remaining case — a client that provides NEITHER — by degrading to `.`,
  i.e. the session's starting directory, which is exactly what the
  pre-v0.2.97 relative form resolved against. It is never worse than the
  old behaviour and fixes the moved-cwd failure.
- **Windows** — `powershell -NoProfile -ExecutionPolicy Bypass -File
  "${CLAUDE_PROJECT_DIR}/.claude/hooks/x.ps1"` with the EXACT placeholder.
  Placeholder substitution is Claude Code's documented mechanism and works
  whichever shell the command is spawned through (Git Bash, or PowerShell
  when Git Bash is absent). A `:-` default is deliberately NOT used here:
  it is POSIX-only parameter expansion and would break under the
  PowerShell shell fallback.

**Windows caveat** ([claude-code#71924](https://github.com/anthropics/claude-code/issues/71924)):
on Windows / Claude Desktop there are reports of `CLAUDE_PROJECT_DIR` being
absent from hook subprocess environments. VCO's Windows form relies on the
placeholder SUBSTITUTION, not the env var, so it is not affected by that
bug; the exported-variable path is only load-bearing on Linux/macOS, where
the `:-.` fallback additionally covers its absence. The shipped `.ps1`
hooks themselves read `$env:CLAUDE_PROJECT_DIR` (falling back to the
project cwd) when they need the project root at runtime.

Every spelling of one registration — the pre-v0.2.97 relative form, the
exact-placeholder form, and the `:-.` fallback form — is treated as the
same hook by the bundle update and the launcher's Hooks tab (registration
identity in `vco_lib/hook_retirements.py::hook_command_key`), so a project
migrated by an earlier v0.2.97 build is rewritten in place, never
duplicated.

## Known caveats

- **Stop-event hooks**: `Stop`, `StopFailure`, `SessionEnd` are
  documented universally per the
  [hooks reference](https://code.claude.com/docs/en/hooks) — no
  VS Code carve-out, and the
  [VS Code feature-gap table](https://code.claude.com/docs/en/vs-code)
  doesn't list hook events as missing. The orchestrator's
  `notify-stop.sh` and `stop-failure-notify.sh` should fire on every
  surface that loads `.claude/settings.json`. (If the hooks don't
  fire in practice, file via `/feedback` since docs claim parity.)
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
conflict to reason about. `.vscode/settings.json` `claude-code.env`
is not one of them — it does not propagate to MCP subprocesses on
Linux (see the "Per-project env files" section above).

## Linux Desktop app gap

Anthropic's Desktop app is macOS / Windows only as of v2.1.x. Linux
users without VS Code have to use the CLI surface (with the
`tools/claude` wrapper or one of the alternatives above). Upstream
limitation — nothing the launcher can do about it.
