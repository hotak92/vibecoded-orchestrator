---
title: "Claude Code CLI - Headless & Scripted Usage"
type: tool
tags: [tool, AI, claude-code, CLI, headless, scripting, session-management, orchestration, low-level-implementation]
created: 2026-02-18T00:00:00Z
updated: 2026-06-25T00:00:00Z
valid_until: 2026-08-31T00:00:00Z
status: active
---

# Claude Code CLI - Headless & Scripted Usage

Claude Code CLI (`claude`) supports non-interactive/programmatic use via the `-p` flag. When used with a Claude Max subscription, it runs entirely on the subscription (no per-token API billing) against the active claude.ai login. The Agent SDK, by contrast, requires `ANTHROPIC_API_KEY` and charges per token.

## Auth model

- **Agent SDK** = API-key based, pay-per-token. Not subscription-compatible officially.
- **`claude -p` subprocess** = uses the Claude Max subscription via the active claude.ai login. This is the cost-free path for orchestration that wraps the CLI as a subprocess.
- An OAuth path exists (`claude setup-token`), but it isn't needed when wrapping the subprocess directly.

## Core Headless Command

```bash
claude -p "prompt" \
  --output-format stream-json \       # real-time JSON events (best for UI)
  --include-partial-messages \        # stream tokens as generated
  --resume <session_id> \             # continue conversation
  --append-system-prompt "rules" \    # inject context without losing defaults
  --allowedTools "Read,Edit,Bash" \   # skip permission prompts
  --max-turns 20                      # safety limit on agentic loops
```

## Session Management Flags

| Flag | Purpose |
|------|---------|
| `--continue` / `-c` | Resume most recent conversation in current dir |
| `--resume <id>` / `-r` | Resume specific session by ID or name |
| `--fork-session` | Create new session ID from existing (divergent branch) |
| `--session-id <uuid>` | Use a specific UUID for the session |
| `--no-session-persistence` | Don't save session to disk (ephemeral) |

## Context Injection Flags

| Flag | Behavior | Modes |
|------|----------|-------|
| `--append-system-prompt "text"` | Adds to default prompt, keeps CLI defaults | Both |
| `--append-system-prompt-file ./file` | Same but from file | Print only |
| `--system-prompt "text"` | Replaces entire prompt (loses CLI defaults) | Both |
| `--system-prompt-file ./file` | Replace from file | Print only |
| `--add-dir ../path` | Add additional working directories | Both |

**Recommendation**: Always use `--append-system-prompt` unless you need complete control — it preserves built-in Claude Code behavior.

## Tool Control Flags

| Flag | Purpose |
|------|---------|
| `--tools "Bash,Read,Edit"` | Restrict available tools |
| `--allowedTools "Bash(git*)"` | Auto-approve (supports glob patterns) |
| `--disallowedTools "Edit"` | Remove from model context entirely |
| `--permission-mode plan` | Session-level permission mode |
| `--dangerously-skip-permissions` | Skip all prompts (use with caution) |

## Multi-Agent Flags

```bash
# Define subagents inline per session
claude -p "task" --agents '{
  "researcher": {
    "description": "Research specialist",
    "prompt": "You are a research specialist...",
    "tools": ["Read", "WebSearch"],
    "model": "sonnet"
  },
  "coder": {
    "description": "Implementation specialist",
    "prompt": "You are an expert coder...",
    "model": "haiku"
  }
}'

# Load named agent definition
claude --agent my-agent -p "task"
```

Subagent fields: `description` (required), `prompt` (required), `tools`, `disallowedTools`, `model` (sonnet/opus/haiku/inherit), `skills`, `mcpServers`, `maxTurns`.

## Output Formats

| Format | Use case |
|--------|---------|
| `--output-format text` | Simple text (default) |
| `--output-format json` | Single JSON blob after completion |
| `--output-format stream-json` | Real-time newline-delimited JSON events |

`stream-json` emits typed events: `text`, `tool_use`, `tool_result`, `agent_message`, `session_id`, `usage`. This is the correct format for a streaming UI and for parsing agent sub-messages.

## MCP Configuration per Session

```bash
claude -p "task" --mcp-config ./my-mcp.json          # add servers from file
claude -p "task" --strict-mcp-config --mcp-config ./  # ONLY use specified servers
```

## Session Storage

Sessions stored at `.claude/sessions/` relative to git repo root:
- Full transcript: `session_XXXXX.jsonl`
- Compact summary: `compact-summary.json` (used for efficient resumption)
- Metadata: `metadata.json` (session ID, git branch, compaction state)

Auto-compaction triggers at **65% context window** usage. Full blocks at 98%.

## Context Auto-Loaded at Startup

Every session automatically loads:
1. **CLAUDE.md** files (project, parent dirs, user global)
2. **Auto-memory** at `~/.claude/projects/<git-root-hash>/memory/`
3. **Settings cascade**: enterprise → project → user
4. **Skills/plugins** from `.claude/skills/`, `~/.claude/skills/`

Hooks in `.claude/hooks/` fire normally in CLI mode — no rebuild needed.

## Channels Flag Limitation (Headless Mode)

**Flag**: `--dangerously-load-development-channels`

**Limitation**: In headless (`-p`) mode, Claude Code blocks this flag behind an interactive TTY dialog for safety. The dialog cannot be answered non-interactively.

**Workaround**: Use `DevChannelAuthorizer` to prime a session via PTY, capture the authorized session ID, then resume it in headless mode:

```python
# Orchestrator initialization
auth = DevChannelAuthorizer()
sess = await auth.prime(entry="<channel>", project_dir=project_dir)

# Per-call
argv = await auth.build_resume_argv(entry="<channel>", project_dir=project_dir, cached=sess)
# Prepend to headless call:
# ['--resume', '<sid>', '--dangerously-load-development-channels', '<channel>']
```

See documentation for automated development channels authorization via PTY. Opt-out: set environment variable to disable.

## Other Notable Flags

| Flag | Purpose |
|------|---------|
| `--max-turns N` | Limit agentic loop iterations (print mode) |
| `--max-budget-usd N` | Spending cap (API users only) |
| `--json-schema '{...}'` | Enforce structured JSON output |
| `--model claude-sonnet-4-6` | Override model for session |
| `--fallback-model sonnet` | Auto-fallback if overloaded |
| `--verbose` | Full turn-by-turn output (debugging) |
| `--from-pr 123` | Resume sessions linked to GitHub PR |
| `--remote "task"` | Create web session on claude.ai |
| `--setting-sources user,project` | Control which settings load |

## Links

- [[implements::Claude Orchestrator CLI]] - Target use case
- [[relatedTo::Claude Code MCP Configuration Pattern]] - Per-project MCP env
- [[uses::Weaviate]] - KG backend (via the weaviate-kg MCP, works in CLI)
