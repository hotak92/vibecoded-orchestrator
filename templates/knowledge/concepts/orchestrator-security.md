---
title: Orchestrator Security Model
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, security, hooks]
created: 2026-04-27T18:30:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

# Orchestrator Security Model

The orchestrator implements a defense-in-depth security model enforced automatically by hooks, without requiring the user to remember rules. Each layer targets a distinct threat vector. Most layers operate as PreToolUse hooks (blocking) or PostToolUse hooks (detection/alerting). Failing one layer still leaves the others active.

[[implements::Security Patterns]] [[relatedTo::Orchestrator Hook System]] [[relatedTo::Orchestrator Context Management]]

## Layer Summary

| Layer | Mechanism | Threat |
|---|---|---|
| 1 | Env scrubbing | Credential leakage to subprocesses |
| 2 | SSRF guard | Server-Side Request Forgery (WebFetch) |
| 3 | Shell injection scan | Command injection, supply chain |
| 4 | Build anchor protocol | Blind file overwrite (Write) |
| 5 | File backup | Destructive edit recovery |
| 6 | Credential scanning | Accidental credential commits |
| 7 | Settings permissions | Tool allowlist/denylist |

## Layer 1: Environment Variable Scrubbing

**Threat**: Claude Code passes its full environment to subprocesses (hooks, Bash tool commands). Any subprocess that logs its environment or exfiltrates data can capture secrets.

**Implementation**: every shell hook that spawns subprocesses contains a scrubbing header that unsets sensitive variables before any subprocess inherits the environment:

```bash
unset SUPABASE_KEY SUPABASE_URL
unset GITHUB_TOKEN GH_TOKEN
unset OPENAI_API_KEY ANTHROPIC_API_KEY
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
unset TELEGRAM_BOT_TOKEN
unset POSTGRES_PASSWORD
unset VERCEL_TOKEN CLAUDE_API_KEY
```

The scrub happens at the top of each hook script, before any other logic. This is enforced by test suite (`tests/test_hooks_disable_guard.py`) to ensure all hooks maintain parity across OSes (bash scripts on POSIX, PowerShell scripts on Windows). Pattern adopted from the Claude Code leak analysis (see `knowledge/research/`).

## Layer 2: SSRF Guard

**Threat**: a prompt injection or compromised tool call could make Claude issue HTTP requests to internal services (cloud metadata APIs, internal dashboards, localhost services not intended for Claude access).

**Implementation**: the `pre-tool-use.sh` PreToolUse hook inspects the `WebFetch` tool's target URL. `search_papers` reaches OpenAlex/arXiv via its own HTTP path and is not routed through this guard.

Private / internal addresses blocked by default:
- `10.0.0.0/8`
- `172.16.0.0/12`
- `192.168.0.0/16`
- `169.254.0.0/16` (cloud metadata endpoints)
- `127.0.0.0/8` and `0.0.0.0` (loopback / wildcard, unless whitelisted)
- `::1` (IPv6 loopback)

Whitelist (explicitly allowed localhost services):
- `localhost:8081` / `:8082` — Weaviate
- `localhost:11435` — Ollama
- `localhost:11440` — code-embedding service
- `localhost:7860` — Gradio

To allow additional services, add the host:port to the whitelist in your project's `.claude/hooks/pre-tool-use.sh`. Blocked requests exit non-zero with an explanation.

## Layer 3: Shell Injection Scan

**Threat**: prompt-injection attacks that embed shell commands in user content, or supply-chain attacks via downloaded scripts.

**Implementation**: two-stage scan in PreToolUse on `Bash(*)`.

**Stage 1: pattern matching** for classic injection patterns:
- `curl ... | sh` or `curl ... | bash` — downloads and executes
- `eval $(curl ...)` — eval of remote content
- `$(curl ...)` in assignment contexts
- `base64 -d | sh` — obfuscated execution
- `wget ... -O - | sh` — wget pipe variant

**Stage 2: bash_security.py** applies a flat, severity-ordered list of ~24 compiled regex rules. Each rule is a `(name, pattern, explanation)` triple; representative rules include:

| Rule (name) | What it catches |
|---|---|
| `rm_root`, `mkfs`, `dd_device`, `shred`, `fdisk` | Disk / filesystem destruction |
| `curl_pipe_shell`, `eval_network` | Fetch-piped-to-shell, eval of remote content |
| `env_exfil_curl`, `env_exfil_curl_data` | Exfiltrating environment to a network endpoint |
| `read_ssh_keys`, `read_proc_environ`, `read_env_files`, `read_bash_history` | Reading credential / secret files |
| `env_grep_secrets` | `env\|grep KEY/TOKEN/SECRET/PASS/CRED` patterns |
| `chmod_world_writable`, `chown_root`, `ln_s_etc` | Permission / ownership abuse |
| `pip_install_url`, `pip_install_git`, `npm_install_url` | Remote package installs from raw URLs |
| `reverse_shell`, `crontab_write` | Persistence / remote-control |

A matched rule blocks the command (the hook exits non-zero) with the rule's explanation surfaced to Claude via stderr.

## Layer 4: Build Anchor Protocol

**Threat**: Claude overwriting a file it has not read in the current session — a blind `Write` based on stale memory rather than actual current content, clobbering an unseen file.

**Implementation**: the `pre-tool-use.sh` hook, applied to `Write` on an existing file.

The hook maintains a session-scoped ledger of files read this session. Before allowing a `Write` to an existing file, it checks whether that path is in the ledger; if not, it exits non-zero (exit 2) with: "Build Anchor Protocol: '<file>' has not been Read this session. Use the Read tool on this file before overwriting it with Write."

The gate applies to `Write` only — `Edit` is left to Claude Code's built-in read-before-edit rule (an Edit needs an exact `old_string` match, which is unobtainable without reading). Re-enforcing the ledger on `Edit` was redundant and a false-positive source, since the hook's own exact-path ledger can diverge from the harness's internal file-state tracking.

## Layer 5: File Backup

**Threat**: Write/Edit operations that produce incorrect results, with no recovery path.

**Implementation**: the `pre-tool-use.sh` hook backs up any existing file before a `Write` or `Edit`.

Before modifying an existing file, the hook copies its current content to `<session-state>/tool_backups/<timestamp>__<encoded-path>`. Backups older than 24 hours are pruned on the next backup. Recovery is a manual copy from the backup directory.

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

## Layer 7: Settings Permissions

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

- Blocking hooks exit non-zero (exit 2) to abort the operation; Claude receives stderr as context.
- Security events are logged to `.claude/logs/security-scan.jsonl`; failures also land in `~/.claude/metrics/failures.jsonl`.
- The credential-scrubbing pattern (Layer 1) is replicated in every shell hook.
- The SSRF guard, shell-injection scan, Build Anchor, and file backup all live in the single `pre-tool-use.sh` hook; the shell-injection scan delegates to `bash_security.py`.

## Technical Details

- PreToolUse hooks are synchronous and blocking — they must exit before the tool runs.
- Hook scripts run with the Claude Code process's environment (after scrubbing).
- Security hooks target <500ms to minimize latency impact.
- False positive rate tuned conservatively: the build-anchor protocol and kill guard occasionally require explicit overrides — intentional friction.
