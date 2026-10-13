---
title: Orchestrator Security Model
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, security, hooks]
created: 2026-04-27T18:30:00Z
updated: 2026-10-13T00:00:00Z
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
| 3 | Shell-command scan | Command injection, credential reads, supply chain |
| 4 | Build anchor protocol | Blind file overwrite (Write) |
| 5 | File backup | Destructive edit recovery |
| 6 | Credential scan of edits | Credentials written into files |
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

The canonical list lives in `.claude/hooks/_lib/scrub-env.sh` (`VCT_SCRUB_SECRET_KEYS`) and its `.ps1` sibling; each hook carries the same list inline on its first code line, before the `VCT_DISABLE_HOOKS` check, so a disabled run still scrubs. Two repo tests lock this: `tests/test_scrub_env_parity_v0273.py` (the `.sh` and `.ps1` lists match, and every hook's inline list equals the canonical set) and `tests/test_hooks_disable_guard.py` (scrub first, then the disable guard, then work). The pattern was adopted from an analysis of the leaked Claude Code source.

## Layer 2: SSRF Guard

**Threat**: a prompt injection or compromised tool call could make Claude issue HTTP requests to internal services (cloud metadata APIs, internal dashboards, localhost services not intended for Claude access).

**Implementation**: the `pre-tool-use.sh` / `.ps1` PreToolUse hook checks the `WebFetch` tool's target URL. The decision itself is ONE Python module, `vco_lib/ssrf_url.py` (`python -m vco_lib.ssrf_url verdict`), called from both OSes through `_lib/ssrf-allowlist.{sh,ps1}`; it answers `allow` (one of this machine's own VCO services), `block` (a private / internal address, or a URL it cannot read) or `pass` (a public address).

Blocked as internal: `localhost` / `*.localhost`, and IPv4 `0/8`, `10/8`, `127/8`, `100.64/10` (CGNAT), `169.254/16` (cloud metadata), `172.16/12`, `192.0.0/24`, `192.168/16`, `198.18/15` and `224/4` upward; IPv6 `::` / `::1`, `fc00::/7`, `fe80::/10`, `fec0::/10`, `ff00::/8`, plus an IPv4 address embedded in IPv4-mapped, NAT64 or 6to4 form, judged as IPv4. A non-ASCII host that cannot be converted is blocked too.

Allowed local services are DERIVED at run time from the projected service env, never from a hand-edited list: Weaviate, Ollama, code-embed and vct-hub at their configured ports, the fixed historical pairs `:8082` and Gradio `:7860`, and any loopback ports the project declares for its own services (`VCT_SSRF_EXTRA_LOCAL_PORTS`, set on the launcher's Identity tab and allowed for that project only). There is nothing to edit in the hook.

The guard fails CLOSED: no Python, an unparseable payload, a missing helper or an unrecognised answer blocks the call with the fix named. A blocked call exits 2 and is logged to `.claude/logs/security_events.jsonl`. Details: the "WebFetch SSRF guard" section of `docs/CONFIGURATION.md`.

## Layer 3: Shell-Command Scan

**Threat**: prompt-injection attacks that embed shell commands in user content, commands that read or exfiltrate credentials, or supply-chain attacks via downloaded scripts.

**Implementation**: a two-stage scan in the `pre-tool-use` hook, for the `Bash` tool and the `PowerShell` tool.

**Stage 1: inline pattern matching** in the hook for three classic injection shapes:
- `curl ... | sh` / `wget ... | bash` — a network fetch piped to a shell
- `eval curl ...` / `eval $(curl ...)` — eval of a network fetch
- `base64 -d ... | sh` — a base64-decoded pipe to a shell

**Stage 2: `.claude/scripts/bash_security.py`** applies a flat, severity-ordered list of 27 compiled regex rules, each a `(name, pattern, explanation)` triple (a `PowerShell` command first goes through 11 PowerShell-syntax rules, then the same list). The rules run on a neutralised copy of the command (here-doc bodies and single-quoted text stripped) so prose that merely mentions a dangerous command does not block; the quoted body of `bash -c '…'`, `eval '…'`, `ssh host '…'` and similar is scanned as a command of its own, and a `python -c` one-liner that reads a secret-shaped env var is blocked too. Representative rules:

| Rule (name) | What it catches |
|---|---|
| `rm_root`, `mkfs`, `dd_device`, `shred`, `fdisk` | Disk / filesystem destruction |
| `curl_pipe_shell`, `eval_network`, `base64_pipe_shell` | Fetch-piped-to-shell, eval of remote content, decoded payloads |
| `env_exfil_curl`, `env_exfil_curl_data`, `env_exfil_multihop` | Exfiltrating environment to a network endpoint |
| `read_ssh_keys`, `read_proc_environ`, `read_env_files`, `read_bash_history` | Reading credential / secret files |
| `env_grep_secrets`, `printenv_secret`, `echo_secret_var`, `secret_var_expansion` | Reading secret-shaped env vars (`env \| grep TOKEN`, `printenv`, `echo $..._TOKEN`) |
| `chmod_world_writable`, `chown_root`, `ln_s_etc` | Permission / ownership abuse |
| `pip_install_url`, `pip_install_git`, `npm_install_url` | Remote package installs from raw URLs |
| `reverse_shell`, `crontab_write` | Persistence / remote-control |

A matched rule blocks the command (the hook exits 2) with the rule's explanation surfaced to Claude via stderr; credential-read rules add a pointer to the sanctioned secrets resolver. Blocks are logged to `.claude/logs/security_events.jsonl`. Stage 1 runs on the raw command text, so it is the stricter of the two: a here-doc or quoted string that merely mentions a fetch piped to a shell is still blocked there. Without a Python interpreter the Bash scans cannot run, and the hook says so (a broken install) instead of passing silently.

## Layer 4: Build Anchor Protocol

**Threat**: Claude overwriting a file it has not read in the current session — a blind `Write` based on stale memory rather than actual current content, clobbering an unseen file.

**Implementation**: the `pre-tool-use.sh` hook, applied to `Write` on an existing file.

The hook maintains a session-scoped ledger of files read this session. Before allowing a `Write` to an existing file, it checks whether that path is in the ledger; if not, it exits non-zero (exit 2) with: "Build Anchor Protocol: '<file>' has not been Read this session. Use the Read tool on this file before overwriting it with Write."

The gate applies to `Write` only — `Edit` is left to Claude Code's built-in read-before-edit rule (an Edit needs an exact `old_string` match, which is unobtainable without reading). Re-enforcing the ledger on `Edit` was redundant and a false-positive source, since the hook's own exact-path ledger can diverge from the harness's internal file-state tracking.

## Layer 5: File Backup

**Threat**: Write/Edit operations that produce incorrect results, with no recovery path.

**Implementation**: the `pre-tool-use.sh` hook backs up any existing file before a `Write` or `Edit`.

Before modifying an existing file, the hook copies its current content to `.claude/state/tool_backups/<timestamp>__<encoded-path>`. Backups older than 24 hours are pruned on the next backup. Recovery is a manual copy from the backup directory.

## Layer 6: Credential Scan of Edits

**Threat**: Claude writing an API key, private key or other credential into a file (copied from a config, pasted into a test fixture).

**Implementation**: the `post-tool-security.sh` / `.ps1` PostToolUse hook, registered on `Edit|Write`. Non-blocking: it always exits 0 and alerts instead.

- **One vocabulary.** The credential shapes come from the SSOT `vco_lib/credential_shapes.py`: one table with a pattern per precision context (`content_scan` for an edited file, `repo_scan` for a whole tree, `command_scan` for a command line). The hook reads the `content_scan` list through the shell mirrors `_lib/credshapes.{sh,ps1}`, which a parity fixture locks to the SSOT; it defines no pattern of its own. The shapes are listed in `docs/CONFIGURATION.md`, "Credential scan of your edits".
- **What is scanned: what the edit INTRODUCED.** For an `Edit`, the full lines the new text occupies afterwards; for a `Write`, the content written. A credential already in the file, on a line the call did not touch, is not reported again. One helper, `.claude/scripts/credscan_introduced.py`, does this for both OSes. Files written by Bash commands are not covered.
- **Alerts name path, line and shape, never the value**: the project-relative path, the line number(s) and the shape label, in a desktop notification (deduped for 6 h per identical finding), in a `[Security alert]` note to the model through the PostToolUse `additionalContext` envelope, and as one row per event in `.claude/logs/credential_alerts.jsonl` (`file`, `patterns`, `findings: [{line, id, label}]`, plus the session and agent that made the edit).
- **One silencer**: the marker `vco-secret-scan: test-placeholder` on the same line as the value. Nothing is inferred from how a value looks or how short it is. The SubagentStop reconciler's scan of a subagent's changed files (`_lib/credscan.{sh,ps1}`) and the repo scanner `scripts/check-no-secrets.sh` honour the same marker.
- **Loud when it cannot run**: a missing vocabulary, a missing or failing helper, or no Python interpreter raises a `credential scanner UNAVAILABLE` alert on every edit, so an unscanned edit never looks like a clean one.

## Layer 7: Settings Permissions

**Threat**: Claude using dangerous or unintended tools.

**Implementation**: `.claude/settings.json` `permissions` block — Claude Code's built-in declarative allow/deny list, evaluated before any PreToolUse hooks run:

```json
{
  "permissions": {
    "allow": ["Bash(git *)", "Bash(pytest *)", "mcp__weaviate-kg__*", "..."],
    "deny": ["Read(./.env)", "Read(./secrets/**)", "Bash(rm -rf /)", "Bash(mkfs*)", "..."]
  }
}
```

The shipped default (`templates/settings.json.*.template`) denies reading or writing `.env` files, reading `secrets/`, root- and home-wide `rm -rf`, `dd` to a device, `mkfs`, `shred`, and a `curl` / `wget` fetch piped to a shell.

## Integration Points

- Blocking hooks exit non-zero (exit 2) to abort the operation; Claude receives stderr as context.
- Blocking events (SSRF guard, shell-command scan, Build Anchor) are logged to `.claude/logs/security_events.jsonl`; credential findings to `.claude/logs/credential_alerts.jsonl`. Failed turns (StopFailure) land in `failures.jsonl` under the metrics dir (`$VCT_STATE_DIR/metrics`, default `~/.vct/metrics`).
- The credential-scrubbing pattern (Layer 1) is replicated in every shell hook.
- The SSRF guard, shell-command scan, Build Anchor, and file backup all live in the single `pre-tool-use` hook (`.sh` and `.ps1`); the SSRF decision delegates to `vco_lib.ssrf_url` and the stage-2 shell scan to `bash_security.py`.

## Technical Details

- PreToolUse hooks are synchronous and blocking — they must exit before the tool runs.
- Hook scripts run with the Claude Code process's environment (after scrubbing).
- These hooks fire on every matching tool call, so they are kept cheap: each decodes its stdin payload's fields in a single Python pass rather than one interpreter per field (`pre-tool-use` runs on every tool call).
- Friction is intentional where it buys safety: the Build Anchor protocol makes Claude `Read` a file before overwriting it with `Write`, and both the SSRF guard and the shell scan fail closed when they cannot decide.
