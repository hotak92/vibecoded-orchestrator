---
title: Safe-Install — Content-Based Service Detection
type: concept
tags: [install, weaviate, ollama, podman, services, low-level-implementation, vibecoded-orchestrator]
created: 2026-04-27T18:30:00Z
updated: 2026-05-16T20:30:00Z
status: active
---

# Safe-Install — Content-Based Service Detection

`install.py` probes each backing service (Weaviate, Ollama, code-embed) by **fingerprinting the response** rather than checking container names. This lets the orchestrator coexist safely with foreign services on the canonical ports without modifying them.

## What it is

Before bringing up its own Podman/Docker containers, install.py issues HTTP probes to ports 8081 (Weaviate), 11435 (Ollama), 11440 (code-embed). Each probe inspects the response body and classifies the service into one of four states.

## Decision matrix

| State | Detection | Action |
|---|---|---|
| **not running** | connect refused / timeout | start the orchestrator's container on the default port |
| **vct-managed** | response matches AND `~/.vct/services.toml` has a matching entry | auto-adopt, no prompt |
| **foreign** | response matches but no services.toml entry | interactive prompt: alt-port (default), adopt, abort |
| **incompatible** | port responds but content doesn't match (e.g. Postgres on 8081) | refuse with a clear error |

## How probing works

- **Weaviate**: `GET /v1/.well-known/ready` + `GET /v1/schema`. Foreign vs vct-managed is decided by whether the schema contains any vct-prefixed collections.
- **Ollama**: `GET /api/tags`. Always foreign-vs-managed by services.toml since Ollama has no vct-specific marker.
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

## Adoption lock — `~/.vct/services.toml`

Persists each service's resolved action so the launcher and install.py agree:

```toml
[[services]]
name = "weaviate"
mode = "adopt"          # or: "parallel", "unresolved", "refuse"
external_url = "http://localhost:8081"
parallel_port = 8082    # only when mode = "parallel"
```

Schema mirrors `launcher/src-tauri/src/services/adoption.rs::AdoptionMode`. install.py uses a hand-rolled TOML writer because it runs **before** pip-install (no dependency on `tomli_w`). A cross-compat test pins the schema both sides agree on.

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

**Multi-machine reuse**: developers with several projects using the orchestrator can share one Weaviate. The vct-managed branch detects "we already started this" via services.toml and skips the prompt.

**Foreign-service operators**: someone running Ollama for personal LLM use shouldn't be blocked from using the orchestrator. Alt-port is the default action precisely because it's the lowest-risk option.

## Files

- `install.py` — probe + decision logic + TOML writer
- `launcher/src-tauri/src/services/adoption.rs` — Rust mirror of the schema
- `infrastructure/docker-compose.override.yml` — generated when alt-port chosen
- `~/.vct/services.toml` — runtime adoption lock
- `tests/test_install_shared_containers.py` — 12+ tests covering probe / decision / TOML round-trip

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
- [[relatedTo::Shared Knowledge Graph Cross-Project]]
- [[relatedTo::vct-infrastructure-bugs-2026-05-05]]
- [[uses::Podman]]
