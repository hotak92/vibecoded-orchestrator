---
title: Supervisor image-ref resolution skips GPU variant + auth — gap with installer (v0.2.46)
type: concept
tags:
  - paid-modules
  - launcher
  - module-supervisor
  - bug
  - gpu-variants
  - pull-token
  - ghcr
  - low-level-implementation
  - v0.2.46
  - v0.2.47-candidate
  - v0.2.49-resolved
created: 2026-06-05T00:00:00Z
updated: 2026-06-05T00:00:00Z
status: active
---

# Supervisor image-ref resolution skips GPU variant + auth

**Discovered**: 2026-06-05, debugging RL Reranker v0.2.8 install failure on
NVIDIA host.

## Symptom

User installs `vct-rl-reranker` (PRIVATE GHCR package) via launcher.
Install step succeeds (image pulled to local podman cache as
`ghcr.io/hotak92/vct-rl-reranker:0.2.8-cuda`). Container start then fails:

```
podman run failed (exit 125):
Trying to pull ghcr.io/hotak92/vct-rl-reranker:0.2.8...
Error: initializing source docker://ghcr.io/hotak92/vct-rl-reranker:0.2.8:
unable to retrieve auth token: invalid username/password: unauthorized
```

Note the **two divergences** from the install path:
1. Tag is `0.2.8` (no variant), not `0.2.8-cuda`
2. Pull attempted anonymously (no `--authfile`), so 401

`audit_log` confirms install pulled correctly:
```
[pull_token_resolved] effective_tag_with_variant: 0.2.8-cuda, username: vct-bot-rl
[module_install_done] install_dir: ~/.vct/modules/vct-rl-reranker
[module_container_start_failed] ...trying to pull ghcr.io/.../vct-rl-reranker:0.2.8...
```

## Root cause — two bugs

### Bug 1: Variant suffix not applied in supervisor's image_ref

[[file::launcher/src-tauri/vct-hub/src/module_supervisor.rs#L89-L124]]:

```rust
pub fn resolve_image_ref(template: &str, manifest: &ModuleManifest)
    -> Result<String, String>
{
    let tag = if container.tag_from_version {
        manifest.version.clone()    // ← "0.2.8" — no variant
    } else { ... };
    let out = template
        .replace("{install.container.image}", &container.image)
        .replace("{install.container.tag}", &tag);
    Ok(out)
}
```

Substitutes `manifest.version` raw. **Should** pipe through
`resolve_variant_tag(manifest, &version, gpu_mode)` which exists at
[[file::launcher/src-tauri/src/installer_engine.rs#L1910-L1926]] but lives
in the launcher crate, not the hub crate. The hub-side supervisor never
sees the GPU-variant resolution that the launcher-side installer applied.

### Bug 2: Supervisor `podman run` doesn't use a pull-token authfile

[[file::launcher/src-tauri/vct-hub/src/module_supervisor.rs#L184]]
`build_podman_run_args` emits `podman run -d --name … <image>` with no
`--authfile`. When podman doesn't find the requested tag locally
(cache-miss, eviction, OR Bug 1 above), it falls back to `pull` against
GHCR with no credentials → 401 for a private package.

The install path correctly requests a token + writes per-pull authfile
([[uses::Pull token gateway production path (installer_engine.rs:1547-1650)]]),
but **that infrastructure is launcher-side and not invoked by the hub**.

## Why install succeeded but supervisor failed

Two independent code paths:

| Path | Crate | Variant aware? | Auth aware? | What it pulled |
|---|---|---|---|---|
| **Install** (`container_pull`) | `launcher/src-tauri/src` | ✅ via `resolve_variant_tag` | ✅ via `request_pull_token` + per-pull authfile | `:0.2.8-cuda` (success) |
| **Supervisor** (`podman run`) | `launcher/src-tauri/vct-hub` | ❌ raw `manifest.version` | ❌ no authfile | `:0.2.8` (401, fails) |

So `audit_log` shows BOTH a `pull_token_resolved` (the install succeeded)
AND a `module_container_start_failed` (the supervisor failed minutes
later) — the boundary is invisible to the user, who sees only "install
failed" because the container never started.

## Fixes

### Fix A (minimum viable, ships in next release)
Pipe `manifest.version` through a variant resolver in
`resolve_image_ref()`. Two options:
1. **Duplicate `resolve_variant_tag` into the hub crate** — minimal
   coupling, but two source-of-truth.
2. **Promote `resolve_variant_tag` into a shared crate** (e.g.
   `vct-launcher-core`) — both launcher + hub import it. Cleaner.

The hub also needs to know the GPU mode. It already does (services
status snapshot has it), but the supervisor's `build_podman_run_args`
doesn't currently receive it. Add a `gpu_mode: GpuMode` parameter or
read it from a hub-scoped state.

### Fix B (defense-in-depth, also next release)
Pass `--authfile` to supervisor's `podman run` when
`manifest.install.method == ContainerPull` AND the image is a private
GHCR package. Three sub-options:

1. **Pre-pull before run** — supervisor calls launcher's
   `container_pull` (which already does token + authfile + variant
   correctly) before `podman run`, guaranteeing the image is in local
   cache. `podman run` then never needs to pull. Simplest; closes the
   cache-eviction edge case too.

2. **Token-on-demand** — supervisor calls Edge Function itself when
   `podman run` is about to start. Adds latency to every container
   start; duplicates auth logic.

3. **Image must exist locally; fail loud** — supervisor verifies via
   `podman image exists <ref>` before `run`; if missing, return a
   clear error that says "image not in cache; run install first".
   Best for tests, worst for UX.

Recommended: **A + B1 combined** — Fix A makes the cache hit, B1 makes
the cache reliable. The pre-pull is a single fast no-op when the cache
is warm.

### Fix C (longer term)
Move all container lifecycle into one crate. The launcher-side install
+ hub-side supervise split is exactly the kind of seam that produces
this class of bug. v0.2.46 V46-E hardened the install side; the
supervisor side hasn't been pulled into the same hardening pass.

## Verification reproducer

On a CUDA host with a vct-rl-reranker license configured:

```bash
podman rmi -a --filter reference='ghcr.io/hotak92/vct-rl-reranker*'
# install via launcher GUI → succeeds, populates ":0.2.8-cuda" in cache
# container start → "podman pull ...:0.2.8" → 401 → exit 125
# audit_log confirms split-tag behaviour
```

## Workaround until fix ships

Pre-pull both the variant-correct AND the bare tag manually:

```bash
echo "<github_pat>" | podman login ghcr.io -u <bot_username> --password-stdin
podman pull ghcr.io/hotak92/vct-rl-reranker:0.2.8-cuda
# Then either re-tag or leave the auth in podman's global authfile and
# let the supervisor's anonymous pull fall through to the cached image.
```

This puts credentials in `~/.config/containers/auth.json` (podman) or
`~/.docker/config.json` (docker), which is exactly the global-state
pollution v0.2.46 V46-E C2 was trying to eliminate. Tolerable as a
workaround; not acceptable as the long-term fix.

## Related

- [[refines::Pre-install catalog architecture — L0 public endpoint + post-install on-disk manifest]]
- [[refines::Multi-Module Paid Distribution — Per-Bot-User Architecture (v0.2.36)]]
- [[refines::Paid-module install + update foundation (v0.2.45)]]
- [[uses::resolve_variant_tag at installer_engine.rs:1910-1926]]
- [[contradicts::audit claim "v0.2.46 install path is fully auth-aware end-to-end"]]
  — true only for the install step; container start is a separate
  unaudited code path.

## Memory updates needed

The auto-memory entry `project_v0234_release_2026_05_26.md` says
"Phase 3A pull-token gateway still stub (deferred to v0.2.35)". The
gateway IS production-wired as of v0.2.46 ([[file::installer_engine.rs#L1547-L1650]])
but only on the install path. The supervisor `podman run` path is still
effectively stub-auth-aware (= anonymous). Update memory to reflect:
"Pull-token gateway is production on install path; supervisor `podman
run` is still anonymous-pull (bug, [[supervisor-image-resolution-variant-gap-2026-06-04]])".

## v0.2.49 closure — Phase 3 hub-supervisor auth port

**Status**: ✅ RESOLVED in v0.2.49.

The two bugs in §"Root cause" and the broader Fix C ("Move all
container lifecycle into one crate") are addressed by the Phase 3
hub-supervisor auth port. Implementation summary:

### What moved into `vct-launcher-core`

1. **`vct-launcher-core::licensing`** (new module): hosts
   `read_license_key_from_keychain`, `machine_id_hash`,
   `read_platform_host_id` + per-OS impls (`read_windows_machine_guid`,
   `read_macos_platform_uuid`, `read_linux_machine_id`),
   `MACHINE_ID_OVERRIDE_ENV`, `LICENSE_MODULE_ID`. The launcher's
   `commands::licensing` is now ~30 lines of thin `pub(crate) use`
   re-exports.

2. **`vct-launcher-core::services::container_runtime`** extensions:
   `PullTokenResponse`, `RL_ARTIFACT_URL_DEFAULT_ENDPOINT`,
   `PULL_TOKEN_ENDPOINT_PLACEHOLDER`, `is_pull_token_placeholder`,
   `resolve_pull_token_endpoint`, `format_pull_token_error`,
   `request_pull_token_http` (HTTP-only core), `request_pull_token`
   (convenience wrapper that reads keychain + machine_id_hash via the
   new `licensing` module), and `pre_pull_with_auth_for_start` (the
   full pre-pull flow including fast-path + token-fetch + authfile +
   `<runtime> pull`).

### What the hub-side supervisor gained

- `module_supervisor::start_container_for_module_with_gpu_mode` now
  calls `vct_launcher_core::services::container_runtime::
  pre_pull_with_auth_for_start` immediately after `resolve_image_ref`
  and before `podman run`. Same gate the launcher uses
  (`install.method == ContainerPull` AND `gpu_mode.is_some()`).
  Closes Bug 2 (no `--authfile` on supervisor's run) by ensuring the
  image is cached BEFORE `podman run` is invoked — `run` reads the
  cache, never re-pulls anonymously.

- `module_supervisor::lookup_manifest_by_id` + `real_manifest_resolver`
  (new helpers): walk `<vct_root_dir>/modules/<id>/vct-module.json` +
  `<vct_root_dir>/bundled_manifests/*.json`, return the first match
  for a requested module_id. This is the production resolver `server.rs`
  injects into `resume_containers_on_startup` (was previously
  `|_id| None` from v0.2.40 F4).

### What `lifecycle_api::module_start` does now

Previously returned `501 not_implemented_supervisor_install`. As of
v0.2.49:

- **200** + `{"container_name": String}` on happy-path.
- **404 `project_not_found`** if no row in `projects` for the id.
- **404 `manifest_not_found`** if no on-disk manifest for the module_id.
- **400 `not_container_module`** if the manifest's runtime type isn't
  `container` or `service`.
- **500 `container_start_failed`** if `start_container_after_install`
  returns Err.

### Server-side boot wiring

`server.rs::start_hub_server` now spawns a detached task that calls
`module_supervisor::resume_containers_on_startup(&db,
real_manifest_resolver())` after the HTTP server starts. This is the
PRIMARY production resume path going forward.

### Launcher-side resume: now a documented fallback

`commands::module_service::resume_containers_on_startup` stays in
place as a FALLBACK with two roles documented in its docstring:
(a) hub-unreachable edge case (e.g. during upgrade flows when the hub
binary is mid-swap); (b) idempotency backstop (both layers check
`is_container_running` before starting, so double-resume is a no-op
for the second runner). The two paths share no mutable state — Db
single-writer SQL UPDATEs serialise concurrent writes.

### De-duplication invariants

- `DEDUP_SENTINEL` from
  `vct_launcher_core::services::container_runtime` is re-asserted
  byte-identically by both crates' test suites
  (`module_service::v0247_helpers_have_one_source_of_truth` +
  `module_supervisor::v0247_hub_helpers_have_one_source_of_truth`).
- The new v0.2.49 promoted helpers (`request_pull_token`,
  `pre_pull_with_auth_for_start`, `format_pull_token_error`,
  `resolve_pull_token_endpoint`) follow the same pattern — single
  source in core, `pub(crate) use` re-exports in the launcher.

### PerPullAuth — critical fix: --authfile flag position (v0.2.49)

**Key implementation detail**: The `PerPullAuth::apply_to()` method in the core now uses `REGISTRY_AUTH_FILE` environment variable instead of `--authfile` argv flag. This fixes a critical bug that was latent since v0.2.47: the original implementation put `--authfile` BEFORE the subcommand, producing invalid podman CLI syntax that podman 4.x rejects.

**Why this matters for supervisor fix**: The supervisor's pre-pull-with-auth now inherits the correct env-var approach (position-independent), eliminating the flag-ordering bug that would have plagued the hub-side implementation.

**Full details**: [[refines::Podman --authfile flag position bug (v0.2.47–v0.2.48) → env var fix (v0.2.49)]]

### Crates now sharing the auth path

| Crate | What it does | What it calls |
|---|---|---|
| `vct-launcher-core::licensing` | keychain read + machine_id_hash | `secrets::get`, platform-specific host-id readers |
| `vct-launcher-core::services::container_runtime` | pull-token HTTP + pre-pull-with-auth | `licensing` for credentials, `reqwest` for HTTP |
| `launcher` install path | `container_pull` + post-install start | `installer_engine::request_pull_token` (thin wrapper around core), `module_service::pre_pull_with_auth_for_start` (thin wrapper) |
| `vct-hub` supervisor | resume-on-boot + on-demand start | `module_supervisor::start_container_for_module_with_gpu_mode` → core `pre_pull_with_auth_for_start` |

### Test delta in v0.2.49

- `vct-launcher-core`: 461 → 472 (+11 new licensing + container_runtime tests)
- `vct-hub`: 208 → 215 (+7 new lifecycle_api + module_supervisor tests)
- `vct-launcher-temp` (launcher): unchanged (1330 passing) — existing
  tests now exercise the shared core helpers via the `pub(crate) use`
  re-exports.

### Lesson reinforcement

This node's pre-v0.2.49 status ("Fix A + B1 combined" as next-release
target) understated the right answer. Fix A (variant-aware ref) shipped
in v0.2.47 and was sufficient for cache-hot hosts. Fix B (pre-pull with
auth) was the gap that bit cache-evicted hosts — addressed in v0.2.47
on the LAUNCHER side only, leaving the hub-side supervisor un-armed.
The proper close required Fix C (one crate owns container lifecycle),
which is what Phase 3 delivers. Bullet 4 of the original "Lesson
reinforcement" section of this node was correct: "this class of bug
(launcher-side path hardened, hub-side path not) recurs every time we
ship a fix only to the surface that triggered the user-reported bug;
the structural fix is single-source-of-truth in core." v0.2.49 is that
structural fix.

## v0.2.49 post-Phase3 validation audit (2026-06-06 evening)

**Context**: while validating Phase 3 + the b4830e04 REGISTRY_AUTH_FILE
fix end-to-end on a live RL Reranker install, the install path was
confirmed green (`pull_token_resolved → module_install_done`) but the
start path immediately failed with `manifest unknown` 125 on bare tag
`:0.2.9`. Two NEW bugs surfaced that v0.2.46/v0.2.47/v0.2.49-Phase3 all
missed despite the test surface looking comprehensive.

### Bug B — `ModuleRuntime::resolve_image_ref` shortcircuits to pre-rendered string

**Status**: ✅ FIXED in commit a5327309 (v0.2.49).

`vct-launcher-core/src/manifest.rs:1711-1734` had a fast-path: when
`runtime.image_ref` is unset AND `install.container.tag_from_version
== true`, returned `format!("{}:{}", container.image, module_version)`
instead of the template form `"{install.container.image}:{install.container.tag}"`.

The downstream free-function
`container_runtime::resolve_image_ref(template, manifest, gpu_mode)`
applies the GPU variant suffix via `.replace("{install.container.tag}", &variant_tag)`.
On a pre-rendered string the replace is a no-op. The variant
(`0.2.9-cuda`) is computed correctly but never applied. Output is the
bare `:0.2.9`.

**Why install path got lucky**: install has its own variant dispatch
(`decide_variant_to_pull` calls `probe → fallback` with an explicit
variant tag constructed at the call site, bypassing `resolve_image_ref`
entirely). Install succeeds despite the bug.

**Why start path doesn't get lucky**: start path's
`start_container_for_module_with_gpu_mode` calls `resolve_image_ref`
once and uses whatever comes out — no probe/fallback. Bare tag flows
all the way to `podman pull` which then fails (GHCR only has the
variant-suffixed tags for private paid modules).

**Fix** (commit a5327309): always return the canonical template form when `image_ref` is
unset, regardless of `tag_from_version`. The implementation:
- Removes the fast-path that returned a pre-rendered `"{image}:{version}"` string
- Now ALWAYS returns `"{install.container.image}:{install.container.tag}"` template
- The free function gets to apply both placeholders AND the variant suffix via `.replace()`
- Args `container_install` and `module_version` become unused (`_`-prefixed)

### Bug C — Probe helpers reuse the broken `--authfile`-before-subcommand pattern

`launcher/src-tauri/src/installer_engine.rs:955-1015` defines two
probe helpers (`probe_image_tag_exists_with_auth_context` and
`probe_image_tag_exists_with_authfile`) that both run:

```rust
cmd.arg("--authfile").arg(path);
cmd.args(["manifest", "inspect", &image_ref]);
```

Same `--authfile`-before-subcommand pattern that bit `apply_to`. Podman
4.x rejects `podman --authfile X manifest inspect Y` with "unknown
flag". Probe always errors → `decide_variant_to_pull` degrades to
"blind-pull legacy behaviour" → fallback-to-alternate-variant
mechanism NEVER fires.

**Latent ROCm/Metal bug**: on AMD or Apple Silicon hosts where the
publisher hasn't pushed a matching variant, the fallback logic is the
only thing that picks a working variant. With the probe broken, AMD
users see a hard install failure for any module shipping mixed-arch
variants.

**Fix**: switch probe helpers from argv flag to `REGISTRY_AUTH_FILE`
env var (same shape as `apply_to`'s v0.2.49 fix). Add a live-podman
regression test mirroring `per_pull_auth_podman_env_var_accepted_by_live_podman`.

### Meta-lesson: argv-shape unit tests miss CLI parser rejections

ALL three bugs in this family share a common failure mode:

1. Original `apply_to` `--authfile`-before-subcommand bug (closed
   `b4830e04`)
2. Probe helpers' `--authfile`-before-subcommand bug (Bug C, still open)
3. Manifest's `resolve_image_ref` template-shortcut bug (Bug B, still
   open)

All three were covered by SOME tests. None of those tests caught the
bugs because the tests asserted on **synthesized argv shapes** (e.g.
`format!("{:?}", cmd).contains("--authfile")`) or **synthesized
result strings** (e.g. equality on the formatted ref), not on the
**effect when run against the live binary**.

The b4830e04 fix added `per_pull_auth_podman_env_var_accepted_by_live_podman`
which spawns real `podman --version` and verifies the parser accepts
the env-var shape. That's the test pattern that catches CLI-shape
bugs. Bug C's fix will replicate it for the probe helpers.

**Generalized lesson** (worth its own KG node): when testing code
that builds command-line invocations for an external binary, at
least one test per code path MUST exercise the live binary's parser
end-to-end. Synthesized-string tests miss flag-position bugs and
similar parser-rejection failures. The cost (skipping when binary
absent) is much lower than the cost of a 3-release latent bug.

### Cross-references

- [[refines::Podman --authfile flag position bug (v0.2.47–v0.2.48) → env var fix (v0.2.49)]]
  — same family
- [[refines::Pre-install catalog architecture — L0 public endpoint + post-install on-disk manifest]]
  — L0 catalog drives the variant list; the v0.2.49 publisher contract
  closes the publisher-side gap that the b4830e04 fix paired with
