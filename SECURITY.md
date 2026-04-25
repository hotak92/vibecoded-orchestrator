# Security Policy

## Reporting a vulnerability

**Do not open a public issue for security problems.** Use one of the
private channels below so we can fix the issue before details are made
public.

### Preferred: GitHub Security Advisories

Open a draft advisory at:
<https://github.com/hotak92/vibecoded-orchestrator/security/advisories/new>

This keeps the discussion private until we publish a fix and a CVE
(when applicable).

### Alternative: email

If you cannot use GitHub Security Advisories, email:

**security@vibecodedtools.it**

Use plain text or a PGP-encrypted message; we'll respond from the same
address.

### What to include

- A clear description of the issue
- Steps to reproduce, or a proof-of-concept
- The commit hash / release version where you observed it
- Your assessment of impact (data exposure, RCE, DoS, …)
- Whether you'd like credit in the advisory and under what name

## What to expect

- **Acknowledgement**: within 3 business days
- **Initial assessment**: within 10 business days
- **Fix or mitigation plan**: within 30 days for high/critical, 90 days
  for medium/low
- **Public disclosure**: coordinated with the reporter, after a fix is
  available; default embargo 90 days

We may ask for clarification or reproduction help during triage.

## Scope

In scope:

- The Python code in this repository (`claude_mcp_servers/`, `launcher/`,
  `tools/`, `VCThelpers/`, `infrastructure/`)
- The shipped agents, skills, and hooks in `templates/`
- The `install.sh` / `install.ps1` / `install.py` installers and
  `BOOTSTRAP.md` flow
- The `tools/vct-secrets/` CLI and its credential helper
- Default configuration files committed to the repo

Out of scope:

- Vulnerabilities in upstream dependencies (Weaviate, Ollama, Claude
  Code, Tauri, Python stdlib, etc.) — please report those upstream.
  We'll bump the dependency once a fix lands upstream.
- Vulnerabilities that require an attacker to already control the user's
  shell, filesystem, or running Claude Code session.
- Issues in third-party MCP servers a user installs alongside this
  project.
- Misconfiguration that the documentation explicitly warns against.

## Hardening notes

The project ships several defence-in-depth layers; if you find a way
around any of them, that's a valid report:

- **Credential scrubbing** — hooks that scan written files for
  high-entropy strings and known token formats before letting an edit
  through.
- **Env scrubbing** — hooks scrub `SUPABASE_KEY`, `GITHUB_TOKEN`,
  `OPENAI_API_KEY`, AWS credentials, etc. before spawning subprocesses.
- **Bash injection guards** — pre-tool-use hook blocks shell-injection
  patterns before bash commands run.
- **Per-project secret isolation** — `tools/vct-secrets/` enforces
  project-scoped resolution; one project cannot read another's secrets
  unless explicitly placed in `shared/`.
- **Local-first** — by default no telemetry, no phone-home; all
  knowledge graph and code graph data stays on the user's machine.

## Coordinated disclosure

We follow standard coordinated disclosure. We'll credit reporters in
release notes and, where applicable, in the GitHub Security Advisory.
If you'd prefer to remain anonymous, say so in your initial report.
