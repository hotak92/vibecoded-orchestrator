---
title: Safe-Install — Content-Based Service Detection
type: concept
tags: [install, weaviate, ollama, podman, services, low-level-implementation, vibecoded-orchestrator]
created: 2026-04-27T18:30:00Z
updated: 2026-09-24T00:00:00Z
status: active
---

# Safe-Install — Content-Based Service Detection

`install.py` probes each backing service (Weaviate, Ollama, code-embed) by **fingerprinting the response** rather than checking container names. This lets the orchestrator coexist safely with foreign services on the canonical ports without modifying them.

**Not to be confused with "Safe add"** — a distinct per-project protection mechanism in the project-add flow (`vco_lib/project_init.py`): with Safe add ON, VCO leaves the project-root `.env` untouched (it may be VCS-tracked), writing the full env to `.claude/settings.json` `env` + `.claude/env` instead, and mirroring the intended `.env` keys to an inert `.env.vco.reference` sidecar. Safe-*install* is about backing SERVICES on ports; Safe *add* is about a project's `.env` FILE.

**Where the logic lives**: the service probe/decision logic described here runs in `vco_lib/service_detection.py` (probes) and `vco_lib/service_reconcile.py` (decisions), driven by `install.py` step [5b]. The orchestrator-root `.claude/` content install itself is delegated (install.py Step 5b) to the one bundle engine — `vco_lib/self_install.py::run_root_bundle_install` runs `python -m vco_lib.project_init install-bundle --json` in a subprocess; there is no separate root-install code path.

## What it is

Before bringing up its own Podman/Docker containers, install.py issues HTTP probes to ports 8081 (Weaviate), 11435 (Ollama), 11440 (code-embed). Each probe inspects the response body and classifies the service into one of four states.

## Decision matrix

| State | Detection | Action |
|---|---|---|
| **not running** | connect refused / timeout | start the orchestrator's container on the port the `service_endpoints` row names |
| **vct-managed** | response matches AND holds VCO data (marker classes / models), or the launcher.db `service_endpoints` row already points here | auto-adopt, no prompt |
| **foreign Ollama** | response matches, no VCO marker | adopted unattended (an informational record names it and gives the one command to switch to a VCO copy) |
| **foreign Weaviate** | response matches, no VCO data, no row | never adopted or duplicated unattended: interactive runs prompt; unattended runs record the `service_adoption_confirmation_required` deferral until you answer |
| **incompatible** | port responds but content doesn't match (e.g. Postgres on 8081) | refuse with a clear error |

## How probing works

- **Weaviate**: `GET /v1/.well-known/ready` + `GET /v1/schema`. Foreign vs vct-managed is decided by whether the schema contains any vct-prefixed collections.
- **Ollama**: `GET /api/tags`. No vct-specific marker exists, so a live third-party Ollama is adopted unattended (v0.2.97) and named in an informational record.
- **code-embed**: `GET /health`. Returns `{"model": "CodeSage-Large-v2"}` (or the configured fallback) if it's our service; anything else is foreign.

Probes never depend on container name (`docker ps`, `podman ps`). A user might run Weaviate via Helm, brew, systemd, or a different compose project — the orchestrator only cares about wire-protocol behavior.

## --on-conflict flag

When a foreign service is detected:

```
python install.py --on-conflict alt-port   # default; safest
python install.py --on-conflict adopt      # advanced; writes vco collections into the foreign service
python install.py --on-conflict abort      # bail
```

`alt-port` writes `infrastructure/docker-compose.override.yml` with the next free port (8082, 11436, 11439), propagates the choice to `.env`, `.claude/settings.json`, and `.vscode/settings.json::claude-code.env`, and brings up the orchestrator's containers next to the existing ones. The user's original service is never touched.

`adopt` is the dangerous mode: the orchestrator writes its own collection schema into the user's running Weaviate. Only safe if the user knows the foreign Weaviate has spare capacity and won't conflict on collection names.

## Where the decisions live — launcher.db `service_endpoints` rows

Since v0.2.97 each service's resolved endpoint is a row in the launcher.db
`service_endpoints` table (migration 047): mode (`vco_managed` /
`adopted_container` / `adopted_external`), host/port, container identity,
data mount. Every surface — install.py, the hooks, the launcher, the MCP
registration — reads the rows; they are written only through
`python -m vco_lib.service_endpoints` verbs (`adopt`, `use-vco-copy`,
`move`, `hand-to-vco`, `reconcile`).

The pre-v0.2.97 adoption lock `~/.vct/services.toml` is RETIRED: the first
v0.2.97 install/update imports it once (`vco_lib/service_reconcile.py`) and
renames it `services.toml.migrated-v0297` — never deleted, but no longer
read by anything.

## Per-install collection naming

When install adopts an existing Weaviate, the bare top-level `KnowledgeGraph` / `Development` names would pollute users' per-project namespacing scheme. Adopt mode therefore:

1. **Derives names from project basename**: `~/projects/myapp/` → `Myapp_KnowledgeGraph`, `Myapp_Development`. Hyphens / underscores are PascalCased; pure-punctuation falls back to `vct_KnowledgeGraph`.
2. **Honors explicit `KG_COLLECTION` / `DEVELOPMENT_COLLECTION` env vars** (typically from `.vscode/settings.json::claude-code.env`).
3. **Skips creation** if the resolved collection already exists.
4. **Skips a bare `Development` collection entirely** if the host already has any `<X>_development`.
5. **Asks for confirmation per proposed creation** in interactive mode; honors `--yes` for non-interactive runs.
6. **Does not auto-adopt cross-project shared KGs**. The orchestrator's orphan-prune sync deletes entries whose `file_path` no longer exists in the active project; two installs sharing one collection would silently delete each other's entries. Always create your own per-project collection (or skip if present).

## --skip-collections / --skip-seed

- `--skip-seed` skips both seed step AND collection bootstrap (no content to seed into anyway). MCP creates collections lazily on first write.
- `--skip-collections` is bootstrap-only opt-out: still seeds existing collections, just doesn't create new ones.

Useful when the user manages their own Weaviate schema or runs in a hermetic CI environment.

## Container naming

Compose container names are namespaced (`vco_weaviate`, `vco_ollama`) for collision-free naming when the user already runs other compose stacks.

## Why it matters

**Safety**: a developer with an unrelated Weaviate at port 8081 should not have their schema mutated by an OSS install. The orchestrator's "adopt mode" requires explicit `--on-conflict adopt` opt-in for exactly this reason.

**Multi-machine reuse**: developers with several projects using the orchestrator can share one Weaviate. The vct-managed branch detects "we already started this" through the `service_endpoints` rows (and the VCO data markers) and skips the prompt.

**Foreign-service operators**: someone running Ollama for personal LLM use shouldn't be blocked from using the orchestrator — a third-party Ollama is adopted unattended (v0.2.97). A third-party Weaviate without VCO data waits for an explicit choice (`service_adoption_confirmation_required`), and alt-port remains the lowest-risk answer.

## Files

- `install.py` — entry point; the probe/decision logic lives in `vco_lib/service_detection.py` + `vco_lib/service_reconcile.py`
- `vco_lib/service_endpoints.py` — the `service_endpoints` rows: the one writer and every verb
- `infrastructure/docker-compose.override.yml` — generated when alt-port chosen
- `~/.vct/services.toml` — RETIRED adoption lock (imported once on the first v0.2.97 run, then renamed `.migrated-v0297`)
- `tests/test_v0297_service_reconcile.py` — the reconcile/adoption decision tests

## Lesson: probe choice for "is Weaviate usable?" — `/v1/meta` not `/v1/.well-known/ready` (added 2026-05-06)

The launcher's "shared services detected" panel and the in-process
`services_already_running()` guard both probe Weaviate to decide
"adopt" vs "alt-port" vs "fresh install". Until 2026-05-06 they used
`/v1/.well-known/ready` — Weaviate's strict readiness gate.

**The bug**: `/v1/.well-known/ready` can return 503 during legitimate
operation (write-readiness gating, post-recovery, disk-pressure checks)
even when Weaviate is fully usable for queries. `hybrid_search` /
`/v1/graphql` / `/v1/meta` all answer correctly while
`/well-known/ready` is still 503. Result: the install wizard reports
"Weaviate not running" → user clicks Custom path or fresh-install →
existing volumes get bypassed.

Confirmed twice (2026-05-05 evening + 2026-05-06 14:00) — both times
the false-negative was misdiagnosed as "Podman rootlessport stall under
disk pressure," with destructive recovery (force-remove + recreate
container). Real bug was the probe URL choice; recoveries were unnecessary.

**Fix**: `/v1/meta` at all 5 launcher detection sites
(`commands/lifecycle.rs::canonical_services` + `services_already_running`,
`tray.rs::probe_services`, `commands/volumes.rs::wait_until_healthy`,
`hub/cli_api.rs` services-running check). PR
`fix/launcher-detection-correctness` (#141, commit `c94602b`).

**`/v1/meta` semantics**: returns 200 with version + module list as soon
as the HTTP server can answer. Strictly weaker signal than "ready for
writes" (which is what `.well-known/ready` checks) — for the install-time
adopt-vs-not question, the weaker signal is correct.

## See also

- `docs/GETTING_STARTED.md` "Coexisting with other Weaviate or Ollama installs"
- [[Cross-OS Hook Portability]]
- [[buildsOn::Launcher Container Lifecycle]]
- [[relatedTo::Shared Knowledge Graph (Cross-Project)]]
- [[relatedTo::vct-infrastructure-bugs-2026-05-05]]
- [[uses::Podman]]
