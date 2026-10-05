---
name: orchestrator-installer
description: Diagnoses partially-failed VCO installs (container failures, port conflicts, mid-run exits, deferred items) and advises on install.py flags and pre-install audits. Use when an install broke — not the happy path (first-install.sh/.bat).
short_desc: diagnose partial-fail installs, advise on install.py flags
keywords: ["install failure", "install diagnostics", "partial install", "install VCO", "install.py flags", "cpu-only", "no-containers", "port conflict", "container won't start", "UPDATE_DEFERRED", "pre-install audit"]
argument-hint: "[describe the failure or the flags you need]"
model: opus
---

# Orchestrator Installer

Diagnose a partially-failed VCO install and choose `install.py` flags when
finer-grained control is needed. NOT the primary install path — the canonical
first install is `bash first-install.sh` (Linux/macOS) or `first-install.bat`
(Windows), which drive `install.py` cross-platform.

## When to use this skill

- `install.py` exited mid-run (port conflict, missing container runtime,
  partial container start, network failure during a model pull) and needs
  diagnosis + the right recovery flags.
- The user wants a deliberate choice about install variants (`--cpu-only`,
  `--no-containers`, `--low-resource`, `--skip-models`, `--skip-collections`)
  with the trade-offs explained.
- A pre-install environment audit is needed (Python 3.10+, podman/docker,
  ports free, disk/RAM).
- The install completed but something looks off and the health audit needs
  guidance.

## Platform context — IMPORTANT

Before emitting any shell command, determine the host OS and only emit
commands valid for it. Never recite Linux-only invocations (`sudo apt-get`,
`chmod +x`, `systemctl`) on Windows or macOS — users copy-paste them.

Detection order:
1. `${PLATFORM}` env var (`install.py` exports `Linux`, `Darwin`, `Windows`).
2. One-shot probe: `python3 -c "import platform; print(platform.system())"`
   (fall back to `python` / `py`).
3. Only then proceed.

**Prefer delegating to `install.py`** — it already handles Python detection,
venv creation, container orchestration, and permissions on every OS. Hand-rolled
shell is for diagnostics or fallback only. When a literal command is needed,
show a three-OS block (Linux first, then macOS, then Windows PowerShell/cmd —
never bash builtins on Windows).

## Recovery loop for a partial install

1. Read `install.log` (or whatever the user piped install output to);
   identify the failing step.
2. Check `.claude/context/UPDATE_DEFERRED.md` for deferral entries and triage
   each by its `Disposition:` class.
3. Probe the suspected blocker: port conflict, container daemon state,
   network, disk space.
4. Recommend a targeted re-run (`python install.py --update`,
   `--skip-collections`, `--cpu-only`, etc.) instead of a full reinstall.

## Reference docs (cite, don't rewrite)

All under the orchestrator clone (`{{ORCHESTRATOR_ROOT}}`):

- Install architecture + flag reference: `docs/GETTING_STARTED.md`
- Per-component configuration: `docs/CONFIGURATION.md`
- Post-install health audit: `docs/post-install/POST-INSTALL-HEALTH-AUDIT.md`
- Container recovery: `docs/post-install/CONTAINER-RECOVERY.md`
- Troubleshooting: `docs/TROUBLESHOOTING.md`
- Update flow: `install.py --help` and the project's
  `.claude/context/UPDATE_DEFERRED.md`

## Context to gather

- Operating system (detect, never guess); user home directory path.
- Whether `install.py` already partially ran (`install.log`,
  `claude_mcp_servers/.venv/`, MCP entries in `~/.claude.json`).
- Optional: existing Weaviate/Ollama URLs, Python preference, container
  runtime preference (Podman vs Docker).

## Done means

- The user knows which `install.py` flag(s) match their environment.
- Any partial failure has a concrete recovery command (not a full reinstall).
- The post-install verification path is clear (which health-audit doc to run,
  what `claude mcp list` should show).
