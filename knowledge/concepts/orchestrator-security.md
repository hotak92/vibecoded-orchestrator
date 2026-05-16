---
title: Orchestrator Security Model
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, security, hooks]
created: 2026-04-27T18:30:00Z
updated: 2026-05-16T03:52:49Z
status: active
---

# Orchestrator Security Model

The orchestrator implements a defense-in-depth security model enforced automatically by hooks, without requiring the user to remember rules. Each layer targets a distinct threat vector. Most layers operate as PreToolUse hooks (blocking) or PostToolUse hooks (detection/alerting). Failing one layer still leaves the others active.

[[implements::Security Patterns]] [[relatedTo::Orchestrator Hook System]] [[relatedTo::Orchestrator Context Management]]

## Layer Summary

| Layer | Mechanism | Threat |
|---|---|---|
| 1 | Env scrubbing | Credential leakage to subprocesses |
| 2 | SSRF guard | Server-Side Request Forgery |
| 3 | Shell injection scan | Command injection, supply chain |
| 4 | Build anchor protocol | Blind file modification |
| 5 | File backup | Destructive edit recovery |
| 6 | Credential scanning | Accidental credential commits |
| 7 | Pre-kill guard | Accidental process termination |
| 8 | Settings permissions | Tool allowlist/denylist |

## Layer 1: Environment Variable Scrubbing

**Threat**: Claude Code passes its full environment to subprocesses (hooks, Bash tool commands). Any subprocess that logs its environment or exfiltrates data can capture secrets.

**Implementation**: every shell hook that spawns subprocesses contains a scrubbing header that unsets sensitive variables before any subprocess inherits the environment:

```bash
unset SUPABASE_KEY SUPABASE_SERVICE_KEY
unset GITHUB_TOKEN GH_TOKEN
unset OPENAI_API_KEY ANTHROPIC_API_KEY
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
unset TELEGRAM_BOT_TOKEN
unset DATABASE_URL POSTGRES_PASSWORD
```

The scrub happens at the top of each hook script, before any other logic. Pattern adopted from the Claude Code leak analysis (see `knowledge/research/`).

## Layer 2: SSRF Guard

**Threat**: a prompt injection or compromised tool call could make Claude issue HTTP requests to internal services (cloud metadata APIs, internal dashboards, localhost services not intended for Claude access).

**Implementation**: PreToolUse hook on `Bash(*)` inspects curl, wget, httpx, and requests calls for target URLs.

Private IP ranges blocked by default:
- `10.0.0.0/8`
- `172.16.0.0/12`
- `192.168.0.0/16`
- `169.254.0.0/16` (cloud metadata endpoints)
- `fd00::/8` (IPv6 private)

Whitelist (explicitly allowed localhost services):
- `localhost:8081` — Weaviate
- `localhost:11435` — Ollama (infrastructure, embeddings)
- `localhost:11440` — code-embedding service

Note: `localhost:8888` (SearXNG) was removed from the whitelist in v0.2.11 along with the SearXNG container itself.

Blocked requests exit 1 with an explanation of which range was matched.

## Layer 3: Shell Injection Scan

**Threat**: prompt-injection attacks that embed shell commands in user content, or supply-chain attacks via downloaded scripts.

**Implementation**: two-stage scan in PreToolUse on `Bash(*)`.

**Stage 1: pattern matching** for classic injection patterns:
- `curl ... | sh` or `curl ... | bash` — downloads and executes
- `eval $(curl ...)` — eval of remote content
- `$(curl ...)` in assignment contexts
- `base64 -d | sh` — obfuscated execution
- `wget ... -O - | sh` — wget pipe variant

**Stage 2: bash_security.py** with 9 rule categories:

| Category | Examples |
|---|---|
| Command injection | Unquoted variable expansion in exec contexts |
| Credential exposure | Echoing env vars that match secret patterns |
| Network exfiltration | Sending data to unexpected external endpoints |
| File system abuse | Writing to /etc, /usr, system paths |
| Privilege escalation | sudo without explicit user confirmation |
| Path traversal | `../../` in file operations |
| Process manipulation | Killing system processes without confirmation |
| Environment pollution | Unsetting critical system variables |
| Eval abuse | eval/exec with dynamic content |

The Python script returns a JSON result with matched rules and severity. High severity → block (exit 1). Medium → warn (exit 0 with message).

## Layer 4: Build Anchor Protocol

**Threat**: Claude modifying a file it has not read in the current session — "blind edits" based on stale memory rather than actual current content. This can corrupt files or apply changes to wrong line numbers.

**Implementation**: PreToolUse hook on `Edit(*)`.

The hook maintains a session-scoped set of read files (populated by PostToolUse on Read tool calls). Before allowing an Edit:

1. Has this file been read in the current session?
2. Was the read recent enough (within last 30 minutes)?

If not, the hook exits 1 with: "File not recently read. Use the Read tool first to see current content."

## Layer 5: File Backup

**Threat**: Edit/Write operations that produce incorrect results, with no recovery path.

**Implementation**: PreToolUse hook on `Edit(*)` (and optionally `Write(*)`).

Before any file modification:

1. Compute SHA-256 hash of file path + timestamp.
2. Copy current file content to `${TMPDIR:-/tmp}/.claude_backups/<hash>`.
3. Write a manifest entry: `{original_path, backup_path, timestamp}`.

Backups expire after 24 hours via cleanup at SessionStart. Recovery:

```bash
ls ${TMPDIR:-/tmp}/.claude_backups/
cp ${TMPDIR:-/tmp}/.claude_backups/<hash> original/path
```

## Layer 6: Credential Scanning

**Threat**: Claude accidentally writing files containing API keys, passwords, or tokens (e.g., copying from a config, including in test fixtures).

**Implementation**: PostToolUse hook on `Edit(*)|Write(*)` — non-blocking (detection only).

Scans written file content for patterns:

- API keys: `sk-[a-zA-Z0-9]{32,}`, `ghp_[a-zA-Z0-9]{36}`, `AIza[a-zA-Z0-9]{35}`
- AWS: `AKIA[A-Z0-9]{16}`, `aws_secret_access_key`
- Generic secrets: `password\s*=\s*["'][^"']{8,}`, `token\s*=\s*["'][^"']{16,}`
- Telegram: `[0-9]+:AA[a-zA-Z0-9-_]{33}`

On detection:

1. Logs the finding to `.claude/logs/security-scan.jsonl` (file path and pattern matched, NOT the actual value).
2. Sends a desktop notification: "Credential detected in <filename>".
3. Does NOT block — post-tool hooks are non-blocking by design. False positives in test fixtures should not halt work; the notification gives the developer opportunity to review before committing.

## Layer 7: Pre-Kill Guard

**Threat**: Claude accidentally killing OS processes that are critical for the development environment.

**Implementation**: PreToolUse hook on `Bash(*)`.

Protected process list includes file managers, desktop compositors, browsers, editors, init systems, and display servers. If a `kill`/`pkill`/`killall` command targets any protected process, the hook exits 1 with explanation. The user must explicitly override.

## Layer 8: Settings Permissions

**Threat**: Claude using dangerous or unintended tools.

**Implementation**: `.claude/settings.json` `permissions` block — Claude Code's built-in declarative allow/deny list, evaluated before any PreToolUse hooks run:

```json
{
  "permissions": {
    "allow": ["Bash(git *)", "Bash(pytest *)", "..."],
    "deny": ["Bash(rm -rf *)", "Bash(sudo rm *)", "..."]
  }
}
```

## Integration Points

- All blocking hooks exit 1 to abort the operation; Claude receives stderr as context.
- Security events are logged to `.claude/logs/security-scan.jsonl` and `~/.claude/metrics/failures.jsonl`.
- The credential-scrubbing pattern (Layer 1) is replicated in every shell hook.
- Layer 3's `bash_security.py` is shared by both the shell-injection scan and the SSRF guard.

## Technical Details

- PreToolUse hooks are synchronous and blocking — they must exit before the tool runs.
- Hook scripts run with the Claude Code process's environment (after scrubbing).
- Security hooks target <500ms to minimize latency impact.
- False positive rate tuned conservatively: the build-anchor protocol and kill guard occasionally require explicit overrides — intentional friction.
