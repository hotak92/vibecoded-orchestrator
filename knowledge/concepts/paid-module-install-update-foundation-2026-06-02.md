---
title: Paid-module install + update foundation (v0.2.45)
type: concept
tags:
- v0.2.45
- paid-modules
- install-py-venv
- binary-swap
- catalog-refresh
- pull-token-placeholder
- status-state-machine
- low-level-implementation
created: 2026-06-02T12:00:00Z
updated: 2026-06-05T00:00:00Z
status: active
---

> **2026-06-05 addendum**: the `pull-token-placeholder` tag refers to the
> v0.2.45 STATE. As of v0.2.46 (`installer_engine.rs:1547-1650`), the
> pull-token gateway is production-wired on the INSTALL path — license
> verification + Supabase Edge Function (`rl-artifact-url`) + per-pull
> authfile + audit-log trio. **But the supervisor's `podman run` does not
> consume that infrastructure** — see [[refinedBy::supervisor-image-resolution-variant-gap-2026-06-04]]
> for the symptom (`:0.2.8-cuda` pulls fine on install, then supervisor
> tries `:0.2.8` anonymously and 401s) + the two-bug analysis + proposed
> fix. Needs to ship in the next release.


# Paid-module install + update foundation (v0.2.45)

v0.2.45 is a hotfix release whose primary motivation was the 3 root causes
surfaced during dogfooding when the user clicked "Update orchestrator" on
the v0.2.43 launcher post-v0.2.44 ship (2026-06-02 ~01:34 UTC).
Beyond fixing those bugs, V45-C through V45-F install the **contracts that
every future paid module relies on**. Per user directive 2026-06-02: "RL
module is not released, so can skip any legacy compatibility issue, but
we need to make sure that — together with it — we build a strong foundation
for every future paid module."

For the post-v0.2.44 audit findings + ship rationale see
[[v0244-shipped-v0245-hotfix-rationale-2026-06-02]].

## The 3 root causes addressed in v0.2.45

### 1. install.py venv mismatch (V45-A) — latent since v0.2.43

Launcher's `update_orchestrator` Tauri command (`installer.rs:3903-3937`)
spawns `install.py` under `detect_python()` → `/usr/bin/python3.12` on Linux.
install.py step 7d (`_migrate_kg_named_vector_slots`) does an **in-process**
`import weaviate` via `vco_lib.project_init._connect_v4_client`.
`weaviate-client` lives only in `claude_mcp_servers/.venv`. ImportError
propagated → 4 `UPDATE_DEFERRED.md` entries per launcher-driven update.
CLI users unaffected (manual venv activation).

**Fix**: `_ensure_running_under_mcp_venv()` at `main()` entry probes
`importlib.util.find_spec("weaviate")` and `os.execv`'s into the MCP
venv when missing. Re-entry-guarded via `VCT_INSTALL_RELAUNCHED=1`.
Soft-fails when the MCP venv interpreter doesn't exist.

### 2. Release-workflow binary-swap race (V45-B) — latent since v0.2.16

Every release ships in two steps separated by ~49 minutes:
1. Source tag (e.g., `f7682a3` at 23:54 UTC, bumps
   `state/install-manifest.json::version` to the new release)
2. `chore(binary): refresh ... for v0.2.X` commit (e.g., `88f9758` at
   00:43 UTC, refreshes `launcher/dist/<arch>/vct-launcher` binaries)

Inside that window, `vco_upstream/main` advertises the new source version
but still carries the OLD dist binaries. User clicks "Update orchestrator"
inside the window → install.py runs → manifest version bumped →
`restart_launcher` faithfully re-execs the OLD binary still on disk.
Post-restart UI reads `installed_version` from the manifest (now bumped)
→ shows "Current v0.2.45" but the running process is still v0.2.44.
No `binary_stale` follow-up signal because manifest = source = displayed
version, even though they're all wrong.

**Fix**: Poll-loop (15s interval, 5min timeout) in `update_orchestrator`
+ `run_post_pull_install_and_restart` checks
`read_on_disk_binary_version == read_source_version` before re-exec.

**Structural fix deferred to v0.2.46-46-1**: tag the binary-refresh
commit automatically, not the source commit. Eliminates the window
entirely.

### 3. Paid-module foundation gaps (V45-C/D/E/F) — stacked sub-bugs

Three stacked bugs in the user's RL "retry install" path silently selected
v0.2.7 from the on-disk manifest instead of v0.2.8 from the L0 catalog,
then 401'd because the v0.2.7 manifest carried a `placeholder.supabase.co`
pull-token endpoint, and the resulting `module_installs` row ended up
in `status='installed' + last_error != NULL + container_name = NULL` —
invisible to the V44-G4 auto-retry sweep.

See section "The 4 foundation contracts" below for the fix details.

## The 4 foundation contracts (paid-module bedrock)

These are the contracts every future paid module relies on. Each was
"sort-of present" before v0.2.45 but had a sharp edge that the RL "retry
install" path drove right into. v0.2.45 makes them load-bearing.

### Contract 1: Manifest version-compare at resolve time (V45-C)

**Rule**: when the L0 paid-module catalog has a strictly newer version
than the on-disk `vct-module.json`, fall through to `L0Synth` Phase 3
instead of honoring the stale on-disk manifest.

**Where**: `launcher/src-tauri/src/commands/modules.rs` —
`resolve_manifest_for_install` Phase 1 → Phase 3 routing. New
`parse_semver` helper with canonical-form acceptance + strict ordering.

**Pre-v0.2.45**: `find_installed_manifest` unconditionally returned
`Installed(path)` if any on-disk manifest existed. Phase 3 (`L0Synth`)
was unreachable for a re-install / retry-from-broken when on-disk was
stale.

**Post-v0.2.45**: every paid-module install path:
1. Looks up the L0 cache before honoring on-disk
2. Falls through to L0Synth when L0 version > on-disk version
3. Audit-logs `module_manifest_resolved` for observability

**Foundation impact**: future paid modules can publish a manifest update
and trust that re-installs will pick it up.

### Contract 2: Placeholder URL detection family (V45-D)

**Rule**: the `placeholder.*` family must catch every "obviously fake" URL
pattern. Future paid-module pre-publish fixtures should be in this family
OR the manifest-hygiene CI (v0.2.46-46-6) must reject them at publish time.

**Where**: `launcher/src-tauri/src/installer_engine.rs` —
`is_pull_token_placeholder`. Now catches `placeholder.<tld>`,
`placeholder` (alone), `<host>.placeholder`, and the existing `example.*`
family, all case-insensitive. HTTP-and-port variants covered.

**Pre-v0.2.45**: only `example`, `example.com|net|org|invalid|test`
matched. v0.2.7 RL manifest's `pull_token_endpoint =
"https://placeholder.supabase.co/..."` slipped through and 401'd against
private GHCR.

**Foundation impact**: future paid modules publishing pre-release
manifests can use any obviously-fake hostname in the `placeholder.*` /
`example.*` families and the installer will short-circuit, refusing to
attempt a real pull-token request.

**Env override discipline** (V45-D): every URL the launcher resolves from
a manifest should have a `VCT_<MODULE>_<KEY>` env override for production
emergencies. `VCT_RL_PULL_TOKEN_ENDPOINT` is the v0.2.45 instance; the
per-module-id generalization (`VCT_<MODULE_ID>_PULL_TOKEN_ENDPOINT`) is
on the v0.2.46-46-2 backlog. The V45-D shape is intentionally
module-id-flavoured so the upgrade is backwards-compatible.

### Contract 3: Status state-machine completeness (V45-E)

**Rule**: every `module_installs` row reaches
`status IN ('error', 'broken', 'installed', 'running')` deterministically.
No partial-failure states. V44-G4 auto-retry is the recovery channel —
its sweep predicate is `status IN ('error', 'broken')`, so any partial
failure that doesn't flip `status` is invisible to recovery.

**Where**: `launcher/src-tauri/src/commands/modules.rs` —
`start_container_after_install` failure path. New
`backfill_partial_container_start_failures` Db method runs once at
launcher startup to migrate pre-v0.2.45 rows.

**Pre-v0.2.45**: `set_module_last_error` only — did NOT flip `status=error`
— when `start_container_for_module` failed post-pull. Row stuck at
`status='installed' + last_error != NULL + container_name = NULL` →
invisible to retry.

**Post-v0.2.45**: container-start failure → `status='error' +
last_error=<message>` + `container_name=NULL` (which the V44-G4 sweep
recognizes).

**Foundation impact**: every paid module's install lifecycle has a
deterministic state machine. The retry sweep can recover from any
documented failure mode. Future paid modules that introduce new failure
paths must hit one of the 4 terminal states.

### Contract 4: L0 catalog refresh on lifecycle events (V45-F)

**Rule**: the L0 catalog must be refetched on every lifecycle event that
could invalidate it. v0.2.45 wires two of three triggers:
1. **Launcher version bump** (orchestrator update) — `bust_cache_if_
   launcher_version_changed` now spawns a non-blocking
   `cached_module_catalog` refetch.
2. **Per-project update** — `update_module_for_project` warms the cache
   pre-resolve, so V45-C's resolver gets fresh L0 data.
3. **Periodic timer** — deferred to v0.2.46-46-4.

**Where**: `launcher/src-tauri/src/lib.rs` +
`launcher/src-tauri/src/commands/modules.rs`. TTL-bounded warm path to
avoid excessive refetches.

**Foundation impact**: the launcher's view of "what paid modules are
available + at what version" is always within one lifecycle event of
fresh. A user clicking Update on a project gets the latest L0 data.

## v0.2.46+ follow-up backlog

These items were explicitly deferred at v0.2.45 ship time with user
approval (per the no-deferred-fixes rule, deferral is allowed only when
the item is out-of-scope for the current tag's commitment):

- **v0.2.46-46-1**: Release workflow refactor — tag the binary-refresh
  commit, not the source commit. Eliminates the V45-B window entirely
  (currently mitigated by the poll-loop, but the structural fix is
  cleaner).
- **v0.2.46-46-2**: Per-module-id default endpoint registry. Generalize
  `VCT_RL_PULL_TOKEN_ENDPOINT` to `VCT_<MODULE_ID>_PULL_TOKEN_ENDPOINT`
  + `RL_ARTIFACT_URL_DEFAULT_ENDPOINT` style defaults for future
  paid modules.
- **v0.2.46-46-3**: Pull-token retry-on-401 with fresh token (requires
  podman-stderr parser to detect the auth-failure signal).
- **v0.2.46-46-4**: Periodic L0 catalog refresh timer (the third trigger
  for Contract 4).
- **v0.2.46-46-6**: Manifest-hygiene CI for paid-module publishers. Lives
  in the RL chat's publish pipeline rather than this repo.

## Cross-references

- [[v0244-shipped-v0245-hotfix-rationale-2026-06-02]] — post-v0.2.44
  audit findings + v0.2.45 ship rationale
- [[hybrid-sot-resolution-shared-kg-canonical-collection]] — V44-G1
  precedent for "hybrid env-vs-DB resolution" pattern that V45-F's
  L0-vs-on-disk version-compare extends to paid-module manifests
- [[orchestrator-root-kg-collection-identity-2026-06-01]] — V44-A/G1
  context (the 4-release recurring KG re-seeding loop that v0.2.44
  closed; V45-A unblocks the launcher path to actually run that
  rebind logic)
- `.claude/context/plans/v0.2.45-design-2026-06-02.md` — the design
  doc that scoped V45-A through V45-G

## Implementation references

- V45-A: `install.py` `_ensure_running_under_mcp_venv()` + tests at
  `tests/test_v0245_self_relaunch_under_venv.py`
- V45-B: `installer.rs::wait_for_binary_refresh` + tests
  `test_v0245_wait_*` in `installer.rs`
- V45-C: `modules.rs::resolve_manifest_for_install` Phase 1→3 routing
  + tests `test_v0245_on_disk_wins_*` / `test_v0245_l0_wins_*` /
  `test_v0245_parse_semver_*` in `modules.rs`
- V45-D: `installer_engine.rs::is_pull_token_placeholder` +
  `resolve_pull_token_endpoint` + tests `test_v0245_placeholder_*` /
  `test_v0245_env_override_*` in `installer_engine.rs` +
  `docs/CONFIGURATION.md` env var row
- V45-E: `modules.rs::start_container_after_install` failure path +
  `Db::backfill_partial_container_start_failures` + tests in
  `tests/test_v0245_status_state_machine.py`
- V45-F: `lib.rs::bust_cache_if_launcher_version_changed` +
  `modules.rs::update_module_for_project` cache warm + tests
  `test_v0245_v45f_*` in `module_catalog_client.rs`
- V45-G (this node): version pins + CHANGELOG `[0.2.45]` block + KG node
  + `scripts/v0245-pre-ship-check.sh`

[[buildsOn::v0244-shipped-v0245-hotfix-rationale-2026-06-02]]
[[buildsOn::hybrid-sot-resolution-shared-kg-canonical-collection]]
[[relatedTo::launcher-paid-modules-schema]]
