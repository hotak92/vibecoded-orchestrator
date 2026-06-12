# Container Recovery — Weaviate, Ollama, code-embed, and port wrangling

> **Audience**: anyone whose item 1 of the
> [health audit](./POST-INSTALL-HEALTH-AUDIT.md) showed a red
> container, or who's hitting port conflicts. Covers Podman and Docker;
> macOS, Linux, and native-Windows specifics.

---

## Service map

| Service           | Default port      | Probe                                  | Purpose                       |
|-------------------|-------------------|----------------------------------------|-------------------------------|
| Weaviate (HTTP)   | 8081              | `/v1/.well-known/ready`                | vector DB                     |
| Weaviate (gRPC)   | 50052             | TCP open                               | bulk insert                   |
| Ollama            | 11435             | `/api/tags`                            | embeddings + small LLM        |
| code-embed (GPU)  | 11440             | `/health`                              | CodeSage embeddings           |
| vct-hub           | 7700              | `/api/v1/health`                       | per-project config resolver   |

The first three live in containers. The hub is a native binary.

---

## Quick first-pass

```bash
# all-in-one status (Linux/macOS)
podman ps --format "{{.Names}} {{.State}} {{.Ports}}" 2>/dev/null \
  || docker ps --format "{{.Names}} {{.State}} {{.Ports}}"
```

You should see at least `vco_weaviate`, `vco_ollama`, and (optionally)
`vco_code_embed` rows (names from `infrastructure/docker-compose.yml`).
If everything is "Exited" or absent, jump to the next section.

---

## Restarting the container stack

The canonical compose directory is `<project_root>/infrastructure/`
(this is what the `ensure-containers.sh` hook prefers — its resolution
order is `$VCT_COMPOSE_DIR` → `$VCT_INFRASTRUCTURE_DIR` →
`$VCT_ORCHESTRATOR_ROOT/infrastructure` → `<project>/infrastructure` →
`<project>/claude_mcp_servers` as a legacy fallback). Works whether
your project uses Podman or Docker:

```bash
cd <project_root>/infrastructure
podman-compose up -d 2>/dev/null || docker compose up -d
```

Only if your install predates the `infrastructure/` layout (legacy
orchestrator-clone setups), fall back to:

```bash
cd <project_root>/claude_mcp_servers
podman-compose up -d 2>/dev/null || docker compose up -d
```

The `SessionStart` hook `ensure-containers.sh` runs the same command on
every Claude Code session start — if containers came up via the hook
once, they'll come up again. A persistently-failing hook means a
config issue, not a transient one.

---

## macOS / Windows: podman machine

On macOS and Windows, Podman runs inside a managed VM. The VM has to
be initialised once and started every login.

```bash
podman machine init    # first time only
podman machine start   # every login, unless boot-service registered
```

If you see `Error: cannot connect to Podman` from `podman ps`, the VM
isn't running. `podman machine list` shows its state; `podman machine
restart` cycles it. The launcher's **Services** tab calls these for
you when you click "Start" on a red row.

---

## Podman vs Docker: which runtime is active

Auto-detected at install. Force a runtime with
`VCT_CONTAINER_RUNTIME=podman|docker` in `<project>/.claude/env` or
shell rc — env var wins over auto-probe. Authoritative on-disk source:
`<install_root>/state/install/runtime.txt` (single word). Don't edit
by hand; rerun install to regenerate.

**Daemon-vs-binary**: the CLI binary AND the daemon both need to run.
Symptom: `docker --version` works but `docker ps` errors `Cannot
connect to the Docker daemon`. Fix:

- Docker Linux: `sudo systemctl start docker`
- Docker Desktop (macOS/Win): launch the app
- Podman Linux: `systemctl --user start podman.socket`
- Podman macOS/Win: `podman machine start`

---

## Port-conflict diagnosis

If a container won't start because "port already in use", find what's
holding the port.

**Linux / macOS**:

```bash
# Port 8081 example — substitute the offending port
ss -ltnp 'sport = :8081' 2>/dev/null || lsof -nP -iTCP:8081 -sTCP:LISTEN
```

**Windows (PowerShell)**:

```powershell
Get-NetTCPConnection -LocalPort 8081 | Select-Object OwningProcess
Get-Process -Id <pid-from-above>
```

Common offenders:

- **8081**: another Weaviate instance from a different project, or a
  generic dev server.
- **11435**: a different Ollama install (system-wide vs.
  orchestrator-managed).
- **11440**: the code-embed service from a sibling project.
- **7700**: an old `vct-hub` process that didn't exit cleanly.

Resolution: stop the offender, OR change the orchestrator's port via
env vars, THEN restart the relevant container. `WEAVIATE_PORT`,
`OLLAMA_PORT`, and `VCT_HUB_PORT` are documented in
`docs/CONFIGURATION.md`; `CODE_EMBED_PORT` is read directly by
`infrastructure/docker-compose.yml` (host-side default `11440`).

---

## ghcr.io auth (paid-module pulls fail with 401)

```bash
podman login ghcr.io --username <github-username>   # or: docker login ghcr.io
```

Prompts for a Personal Access Token with `read:packages` scope.

Paid modules also use a token-gated supervisor pull path managed by
the launcher; if the GUI's module-install dialog reports a 401 from
the supervisor specifically, see `<vct_root>/launcher.log` for the
supervisor's image-ref + the authfile path it consulted.

---

## Volume sanity (KG empty after runtime switch)

Switching Podman ↔ Docker can mount a fresh volume instead of the
existing one. Volume locations:

- Podman rootless: `~/.local/share/containers/storage/volumes/vco_weaviate_data/_data`
- Podman rootful: `/var/lib/containers/storage/volumes/vco_weaviate_data/_data`
- Docker: `/var/lib/docker/volumes/vco_weaviate_data/_data`

Before re-embedding (which can take 30+ min on a 10k-node KG):

1. Check which override file your compose loaded
   (`ls infrastructure/docker-compose*.yml*`).
2. Inspect its `volumes:` section — the host-side path should be the
   one populated by your prior runtime.
3. Re-embed ONLY if the volume is genuinely fresh.

DO NOT delete a volume to "fix" empty-KG symptoms before confirming
the path. The data is almost always still on disk under the old
runtime's volume root.

---

## When the hook keeps restarting failing containers

`ensure-containers.sh` runs every session. To debug a persistent
failure without the hook re-firing:

```bash
VCT_DISABLE_HOOKS=1 claude     # opens Claude Code with hooks off
# OR
tail -50 <project>/.claude/logs/$(date +%F)_tool_usage.jsonl
```

Once you've fixed the root cause, remove `VCT_DISABLE_HOOKS=1` —
permanently disabling all hooks is a footgun (silently affects every
PostToolUse / PreToolUse / SessionStart hook, not just containers).

---

## Quick links

- Audit: [`POST-INSTALL-HEALTH-AUDIT.md`](./POST-INSTALL-HEALTH-AUDIT.md)
- Update issues: [`UPDATE-RECOVERY.md`](./UPDATE-RECOVERY.md)
- Native-Windows quirks: [`WINDOWS-FIRST-RUN-CHECK.md`](./WINDOWS-FIRST-RUN-CHECK.md)
- Env vars: `docs/CONFIGURATION.md`
