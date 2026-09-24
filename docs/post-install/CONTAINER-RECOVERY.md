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

The canonical compose home is `<install_root>/infrastructure/` — the
boot/GPU wrapper's default. Do **not** run a bare `compose up -d`
by hand: since v0.2.97 every VCO compose invocation names the
`vco_managed` services explicitly (with `--no-deps`), because a bare
`up` would also try to recreate containers VCO adopted from another
compose project — on the installer's default, empty volumes. The
supported ways to bring the stack back:

```bash
# the session hook (same code path every session start uses)
bash <install_root>/.claude/hooks/ensure-containers.sh

# or a full install/update run — it reconciles the rows and starts
# everything they allow (VCO-managed through compose, adopted by name)
python <install_root>/install.py --update

# afterwards, re-check the rows against what is actually running
python -m vco_lib.service_endpoints plan --json
python -m vco_lib.service_endpoints reconcile
```

`reconcile` re-checks the `service_endpoints` rows against the live
services (and starts ADOPTED containers by name) — it never composes a
`vco_managed` service itself, so a down VCO-managed container needs the
hook or the install/update run above, not `reconcile`. The plan reads
the rows and says what may be started: VCO-managed services through
compose, adopted containers by
name only (never removed, never recreated). The legacy
`claude_mcp_servers/` compose home is no longer composed from — a
container created there is adopted as it stands. The `SessionStart`
hook `ensure-containers.sh` runs the same flow on every Claude Code
session start — if containers came up via the hook once, they'll come
up again. A persistently-failing hook means a config issue, not a
transient one.

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

Auto-detected at the first install and recorded in
`<install_root>/state/install/runtime.txt` (single word). From then on
that record PINS the runtime everywhere — the session hooks, the
launcher's services and storage pages, module containers and the hub —
and a recorded runtime that is down is refused, never swapped for the
other one (the two keep separate volumes, so a swap starts an empty
stack). `VCT_CONTAINER_RUNTIME=podman|docker`, in the environment the
process starts from (shell rc, `<project>/.claude/env`, or the login
session for the launcher), overrides the record. To switch for good:
set `VCT_CONTAINER_RUNTIME` and re-run `python <install_root>/install.py
--update` (it records the runtime it used), or write the one word into
`runtime.txt`; a running launcher keeps the environment it started with,
so quit and relaunch it afterwards.

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

Resolution: stop the offender, OR move the orchestrator's service to a
free port, THEN restart it. Since v0.2.97 a VCO-managed service's port
lives in its `service_endpoints` row — move it with
`python -m vco_lib.service_endpoints move --service <svc> --port <N>`
(from the install root; the same action is on the launcher's Services
page). The move updates the row, rewrites the managed keys in
`infrastructure/.env`, recreates the container with the data-mount
identity checks, and re-projects every registered project, so every
surface follows it — nothing to hand-edit. Adopted services are never
moved by VCO: if the offender is a container you adopted, either move
the row to another candidate (`service_endpoints adopt`) or free the
port at its owner. `VCT_HUB_PORT` (the hub is a native binary, not a
container service) is documented in
[`../CONFIGURATION.md`](../CONFIGURATION.md).

---

## A service on our port belongs to a different compose project

Distinct from a port conflict: the container is *there* and answering,
but another compose project created it (VCO checks the compose labels).
Since v0.2.93 an install refuses to `--force-recreate` or `--build` such
a service — recreating a container you do not own can orphan its volumes
— and leaves a `services_foreign_compose_identity` entry in the
install's `UPDATE_DEFERRED.md` ledger. Until v0.2.96 the entry described
the refusal and stopped there, so the only way forward was manual
compose surgery.

That entry now leads with the command that does it:

```bash
python -m vco_lib.service_adoption adopt-services --root <install root>
python -m vco_lib.service_adoption adopt-services --root <install root> --dry-run   # plan only, changes nothing
```

`--root` defaults to the checkout the module is imported from; add
`--runtime docker` on a Docker install (the default is `podman`).

It reconciles rather than replaces. The live containers are read, an
`infrastructure/compose.override.yaml` is generated carrying their bind
mounts **byte for byte** plus the behaviour-critical environment and
healthchecks, the merged configuration is re-rendered and verified
*before* anything is stopped, and then services move one at a time. It
never removes or prunes a volume, never `down`s the other project, and
never writes that project's files. Any service whose live state cannot
be read positively is refused — substituting a named volume for a bind
mount is refused outright, because that is how a 110 GB model directory
gets silently orphaned. A service that fails to come up is re-created
under its original invocation and the services after it are left alone.
An override file you wrote yourself is never overwritten.

Where adoption is not what you want, the deferral's printed rebuild
command tags the **owning** project's image name, so a manual rebuild
reaches the image actually in use instead of building one nothing runs.

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
# OR inspect recent security-hook events (SSRF/injection/anchor blocks):
tail -50 <project>/.claude/logs/security_events.jsonl
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
