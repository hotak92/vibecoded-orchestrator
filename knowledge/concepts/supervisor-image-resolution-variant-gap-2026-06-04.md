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
[module_install_done] install_dir: /home/martino/.vct/modules/vct-rl-reranker
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
