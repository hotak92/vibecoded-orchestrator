---
title: Orchestrator-Root KG Collection Identity
type: concept
tags:
- orchestrator-root
- kg-collection
- database-consistency
- design-contract
- v0.2.44
created: 2026-06-01T03:30:00Z
updated: 2026-06-01T03:30:00Z
status: active
---

# Orchestrator-Root KG Collection Identity

## Contract (v0.2.44+)

For orchestrator-root installs (projects where `projects.host='orchestrator_root'`):

1. `project_kg_bindings.role='primary'.collection_name`
   == `project_kg_bindings.role='shared'.collection_name`
   == canonical = `SHARED_KG_COLLECTION` env value.
2. Both env keys `KG_COLLECTION` and `SHARED_KG_COLLECTION` hold the same canonical name.
3. The shared-seed sync step is skipped — one physical collection serves both roles.
4. The `kg_collection_access` row granting orchestrator-root WRITE access to its
   primary collection is **structural** — cannot be revoked via the access-matrix UI.

## Why

Orchestrator-root projects are the orchestrator clone itself. Their KG IS:
- Their own per-project KG (role=primary)
- The global shared KG for every other project (role=shared)

Both roles point at one physical Weaviate collection. Names should match
to eliminate ambiguity across env-channel, binding-table, and access-matrix
surfaces.

## How install.py decides on update (`adopt-and-route`)

1. Read `_is_orchestrator_root_install()` (file-marker detection — see
   `vct-module.json` + `install.py` + `vco_lib/` triplet).
2. If true:
   a. Read both bindings from launcher.db via [[vco_lib.launcher_db_reader]].
      Fall back to env if DB unavailable (logged WARNING).
   b. Canonical = `SHARED_KG_COLLECTION` (predictable, public-shipping name).
   c. Rebind both bindings + both env keys to canonical via
      `_rebind_orchestrator_root_to_canonical()`.
   d. Skip the shared-seed sync.
3. If false (per-project install):
   - No rebind. KG_COLLECTION and SHARED_KG_COLLECTION resolve as usual.

## The 4-release recurring loop this contract ends

| Release | Attempt | Why it failed |
|---|---|---|
| v0.2.28 | `manual_override` sentinel `"v0.2.28-recovery"` | Sentinel written but no guard read it. |
| v0.2.29/30 | settings.json env preservation | Fixed env wipe but install.py still wrote both collections. |
| v0.2.42 | CI-10 content-hash diff gate | Made re-seeding visible but didn't prevent it on divergent collections. |
| v0.2.43 | V0243-0 string-equality guard | `KG_COLLECTION == SHARED_KG_COLLECTION` never true for VCO_dev's legacy-migrated state. |
| **v0.2.44** | **adopt-and-route** | Architectural fix: detect orchestrator-root as a category, rebind to single canonical. |

## Related

- [[launcher-hub-single-writer-principle]] — SoT discipline
- [[kg-binding-clobber-bug-and-seed-guard]] — v0.2.28 history
- `vco_lib/launcher_db_reader.py` — read-only SoT helper (V44-B)
- `install.py::_seed_weaviate_shared_kg_only` — entry point for the rebind (V44-A)
- `install.py::_rebind_orchestrator_root_to_canonical` — helper writes back to launcher.db + env (V44-A)
- `launcher/src-tauri/src/commands/kg.rs::kg_set_collection_access_mode` — structural-row guard (V44-C)
- `launcher/src/lib/project-state/CrossProjectAccessTab.svelte` — UI disable for structural row (V44-C)
