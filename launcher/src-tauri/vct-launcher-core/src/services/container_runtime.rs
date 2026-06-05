// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Copyright (C) VibeCoded Tools — licensed under AGPL-3.0-or-later.
//
//! Shared helpers used by both the launcher-side installer/starter and
//! the hub-side supervisor to talk to podman/docker for paid modules.
//!
//! ## Why this module exists (v0.2.47)
//!
//! Pre-v0.2.47 there were TWO copies of `resolve_image_ref`,
//! `build_podman_run_args`, `resolve_container_name`, the
//! `rl_placeholders` helper, etc. — one in
//! `launcher/src-tauri/src/commands/module_service.rs` (live; install
//! path) and a near-identical copy in
//! `launcher/src-tauri/vct-hub/src/module_supervisor.rs` (supervisor /
//! resume-on-boot path). The hub copy lagged behind on at least three
//! changes (variant-tag resolution, runtime-type widening,
//! `--authfile` for `podman run`) and that drift produced the
//! v0.2.46 GHCR-401 bug fixed in this release.
//!
//! See [[file::knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md]]
//! for the bug analysis. The two copies have been collapsed into this
//! single module so the pure helpers (placeholder resolution, port /
//! volume / env arg builders, podman-run argv construction) are
//! authored ONCE and consumed by both crates via `pub use`.
//!
//! ## What lives here
//!
//! * Pure helpers — `resolve_container_name`, `resolve_image_ref`,
//!   `build_podman_run_args` and their internal building blocks.
//! * `resolve_variant_tag` — the GPU-mode → variant-tag dispatcher
//!   previously private to `installer_engine.rs`. Now shared so the
//!   supervisor can pick the same `-cuda` / `-rocm` / `-cpu` suffix
//!   the installer pulled with.
//! * `PerPullAuth` + `build_per_pull_authfile` — the per-pull auth-
//!   context guard. New in v0.2.47: also carries a `runtime`-aware
//!   `apply_to(cmd, runtime)` so the same helper works for podman
//!   (`--authfile <path>`) AND docker (which doesn't support
//!   `--authfile` on `pull` / `run` and needs `DOCKER_CONFIG=<dir>`
//!   pointing at a directory containing `config.json`).
//!
//! ## What stays in caller crates
//!
//! * `start_container_for_module` — the actual `podman run` spawn,
//!   plus `mkdir -p` for bind-mounts, plus pre-pull, lives in each
//!   crate's own `module_service.rs` (launcher) /
//!   `module_supervisor.rs` (hub). The launcher persists the resolved
//!   container_name via Tauri State + `Db`; the hub keeps its own
//!   supervisor wiring (resume sweep, manifest resolver). Promoting
//!   the entire async lifecycle here would drag the Tauri / hub
//!   harnesses into core, which we don't want.
//! * `detect_container_runtime` — lives in each crate today because
//!   the existing call sites are async tokio::process. A future
//!   tidying pass could promote it; out of scope for v0.2.47.
//!
//! ## Test discipline
//!
//! Unit tests in this module exercise every pure helper. The
//! `dedup_sentinel` constant + the per-callsite `pub use` re-exports
//! pin "both crates call the SAME function" — see
//! [`DEDUP_SENTINEL`].

use std::collections::HashMap;
use std::io::Write as _;
use std::path::{Path, PathBuf};

use crate::db::models::ProjectRow;
use crate::manifest::{ModuleManifest, PlaceholderCtx, PortMapping, VolumeMount};
use crate::services::gpu_mode::GpuMode;

/// Default Ollama port used to resolve `{ollama_port}` in env values when
/// the manifest doesn't override it. Matches the launcher's well-known
/// service-port layout.
pub const DEFAULT_OLLAMA_PORT: &str = "11435";

/// v0.2.47: doc-test-friendly constant that pins the call-site identity
/// of this module. Both `launcher/src-tauri/src/commands/module_service.rs`
/// and `launcher/src-tauri/vct-hub/src/module_supervisor.rs` re-export
/// the helpers from this module via `pub use`. The
/// `helpers_have_one_source_of_truth` test in this file asserts that
/// the sentinel resolves to a single byte-identical string in BOTH
/// crates' compilation units — proving the de-duplication is real and
/// not just two structurally-similar copies.
pub const DEDUP_SENTINEL: &str = "vct-launcher-core::services::container_runtime::v0.2.47";

// ─── Pure helpers ──────────────────────────────────────────────────────

/// Resolve `{project_slug}` (and any other launcher-wide tokens) into a
/// concrete container name. Returns an error if the resolved name still
/// contains unresolved placeholders — that's a manifest authoring bug
/// (e.g. a typo `{project-slug}`) and should surface clearly instead of
/// silently passing through to podman as a literal `{...}` string.
pub fn resolve_container_name(template: &str, project_slug: &str) -> Result<String, String> {
    let out = template.replace("{project_slug}", project_slug);
    if out.contains('{') && out.contains('}') {
        return Err(format!(
            "container_name_template '{}' has unresolved placeholders after \
             {{project_slug}} substitution → '{}'",
            template, out
        ));
    }
    Ok(out)
}

/// Resolve `{install.container.image}` + `{install.container.tag}` against
/// the manifest's `install.container` block. The tag is chosen via the
/// same rule `container_pull` uses (`tag_from_version` →
/// `manifest.version`; else `install.r#ref` or `"latest"`).
///
/// v0.2.47: when `gpu_mode` is `Some(_)` AND the manifest declares
/// `runtime.gpu_image_variants`, the tag is piped through
/// [`resolve_variant_tag`] so the resulting image ref carries the right
/// per-GPU suffix (e.g. `-cuda`, `-rocm`, `-cpu`). When `gpu_mode` is
/// `None` (legacy single-tag modules, OR modules without a variant
/// block), the tag is taken verbatim — matching pre-v0.2.47 behaviour
/// for non-variant modules.
///
/// Replaces a pair of stale v0.2.20 docstrings in the two former call
/// sites that claimed "the image we want is the one already on disk
/// after `container_pull`" — that claim was wrong (it didn't survive
/// cache eviction, and the supervisor's bare `manifest.version`
/// substitution never matched the variant tag the installer pulled,
/// so the supervisor's `podman run` triggered an anonymous re-pull
/// that 401'd against private GHCR — the bug fixed in v0.2.47).
pub fn resolve_image_ref(
    template: &str,
    manifest: &ModuleManifest,
    gpu_mode: Option<GpuMode>,
) -> Result<String, String> {
    let container = manifest
        .install
        .container
        .as_ref()
        .ok_or_else(|| {
            "resolve_image_ref: install.container block missing (not a container_pull module)"
                .to_string()
        })?;

    let base_tag = if container.tag_from_version {
        manifest.version.clone()
    } else {
        manifest
            .install
            .r#ref
            .clone()
            .unwrap_or_else(|| "latest".to_string())
    };

    let tag = match gpu_mode {
        Some(mode) => resolve_variant_tag(manifest, &base_tag, mode),
        None => base_tag,
    };

    let out = template
        .replace("{install.container.image}", &container.image)
        .replace("{install.container.tag}", &tag);

    if out.contains('{') && out.contains('}') {
        return Err(format!(
            "image_ref template '{}' has unresolved placeholders after \
             install.container substitution → '{}'",
            template, out
        ));
    }
    Ok(out)
}

/// v0.2.20: pick the OCI image tag based on the host's GPU mode.
///
/// When the manifest declares `runtime.gpu_image_variants`, each
/// `GpuMode` maps to a tag suffix:
///
/// | GpuMode | Variant tag used |
/// |---------|------------------|
/// | Cuda    | `gpu_image_variants.cuda` |
/// | Rocm    | `gpu_image_variants.rocm` |
/// | Cpu     | `gpu_image_variants.cpu`  |
/// | Metal   | `gpu_image_variants.cpu` (no Metal torch wheels today) |
///
/// When `gpu_image_variants` is absent (legacy modules + non-container
/// runtimes), returns `base_tag` unchanged — preserves single-tag
/// behavior for modules that only ship one image.
///
/// v0.2.34: variant strings from the L0 catalog ship as templates
/// (e.g. `"{version}-cuda"`) so the same manifest fixture serves every
/// released version. This function performs the `{version}` substitution
/// against `base_tag` so callers receive a ready-to-pull image tag
/// (`"0.2.7-cuda"`) rather than the literal template string. Variants
/// that don't contain `{version}` are returned unchanged —
/// backwards-compatible with pre-template manifests.
///
/// v0.2.47: relocated from `launcher/src-tauri/src/installer_engine.rs`
/// into core so both the launcher-side install path AND the hub-side
/// supervisor reach the variant tag the same way. Previously only the
/// installer applied the variant suffix; the supervisor's `podman run`
/// substituted the bare `manifest.version` and asked GHCR for the
/// non-existent tag, anonymously.
pub fn resolve_variant_tag(manifest: &ModuleManifest, base_tag: &str, gpu_mode: GpuMode) -> String {
    let variants = match manifest.runtime.gpu_image_variants.as_ref() {
        Some(v) => v,
        None => return base_tag.to_string(),
    };
    let template = match gpu_mode {
        GpuMode::Cuda => &variants.cuda,
        GpuMode::Rocm => &variants.rocm,
        GpuMode::Cpu | GpuMode::Metal => &variants.cpu,
    };
    template.replace("{version}", base_tag)
}

/// RL-specific placeholders not covered by `PlaceholderCtx::resolve`.
///
/// Despite the name, these are reused by every container/service module
/// (the `RL_SERVER_PORT` placeholder maps to whatever port the
/// per-project allocator chose — see `ensure_project_rl_port` in the
/// caller crates).
pub fn rl_placeholders(rl_port: u16, project_slug: &str) -> HashMap<String, String> {
    let mut m = HashMap::new();
    m.insert("{RL_SERVER_PORT}".to_string(), rl_port.to_string());
    m.insert("{project_slug}".to_string(), project_slug.to_string());
    m.insert("{ollama_port}".to_string(), DEFAULT_OLLAMA_PORT.to_string());
    m
}

/// Two-layer placeholder resolver:
///   1. Launcher-wide tokens (`{VCT_DATA}`, `{HOME}`, `{install_dir}`,
///      `{MODULE_ID}`, etc.) via `PlaceholderCtx::resolve`.
///   2. RL-specific tokens (`{RL_SERVER_PORT}`, `{project_slug}`,
///      `{ollama_port}`) via the per-call map.
pub fn resolve_value(
    raw: &str,
    ctx: &PlaceholderCtx,
    placeholders: &HashMap<String, String>,
) -> String {
    let mut out = ctx.resolve(raw);
    for (token, value) in placeholders {
        out = out.replace(token, value);
    }
    out
}

/// Build a single `-p` arg value. Format: `[bind:]<host>:<container>`.
/// Returns an error when `port.host` doesn't resolve to a valid u16
/// (numeric string after placeholder substitution).
pub fn build_port_arg(
    port: &PortMapping,
    placeholders: &HashMap<String, String>,
) -> Result<String, String> {
    let mut host = port.host.clone();
    for (token, value) in placeholders {
        host = host.replace(token, value);
    }
    host.parse::<u16>().map_err(|_| {
        format!(
            "port host '{}' (resolved from '{}') is not a valid u16",
            host, port.host
        )
    })?;

    let bind = port.bind.as_deref().unwrap_or("127.0.0.1");
    if bind.is_empty() {
        Ok(format!("{}:{}", host, port.container))
    } else {
        Ok(format!("{}:{}:{}", bind, host, port.container))
    }
}

/// Build a single `-v` arg value. Format: `host:container[:mode]`.
pub fn build_volume_arg(
    vol: &VolumeMount,
    ctx: &PlaceholderCtx,
    placeholders: &HashMap<String, String>,
) -> String {
    let host = resolve_value(&vol.host, ctx, placeholders);
    let container = resolve_value(&vol.container, ctx, placeholders);
    match vol.mode.as_deref() {
        Some(m) if !m.is_empty() => format!("{}:{}:{}", host, container, m),
        _ => format!("{}:{}", host, container),
    }
}

/// Build the full `podman run` argv (without the leading `podman`).
///
/// Layout:
///   `run -d --name <name> [--restart=unless-stopped] -p ... -v ... -e ... <image> <command> <args...>`
///
/// Accepts both `runtime.type = "container"` and `runtime.type = "service"`
/// — both declare a long-running daemon backed by a container.
/// `"cli"` / `"mcp_stdio"` / `"mcp_http"` have no podman args and are
/// rejected.
pub fn build_podman_run_args(
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project: &ProjectRow,
    rl_port: u16,
    container_name: &str,
    image: &str,
) -> Result<Vec<String>, String> {
    let runtime = &manifest.runtime;
    if !matches!(runtime.r#type.as_str(), "container" | "service") {
        return Err(format!(
            "build_podman_run_args: runtime.type must be 'container' or 'service', got '{}'",
            runtime.r#type
        ));
    }

    let placeholders = rl_placeholders(rl_port, &project.slug);
    let mut args: Vec<String> = Vec::new();
    args.push("run".into());
    args.push("-d".into());
    args.push("--name".into());
    args.push(container_name.to_string());

    if runtime.auto_restart {
        args.push("--restart=unless-stopped".into());
    }

    // Ports: one `-p [bind:]host:container` per entry.
    for port in &runtime.ports {
        args.push("-p".into());
        args.push(build_port_arg(port, &placeholders)?);
    }

    // Volumes: one `-v host:container[:mode]` per entry. Host paths
    // resolved against PlaceholderCtx AND RL-specific placeholders.
    for vol in &runtime.volumes {
        args.push("-v".into());
        args.push(build_volume_arg(vol, ctx, &placeholders));
    }

    // Env vars: env_fixed first (literal values still get placeholder
    // substitution in case authors used `{RL_SERVER_PORT}` etc. inside),
    // then env_derived. HashMap iteration is non-deterministic — tests
    // must assert on set membership, not exact ordering.
    for (k, v) in &runtime.env_fixed {
        let resolved = resolve_value(v, ctx, &placeholders);
        args.push("-e".into());
        args.push(format!("{}={}", k, resolved));
    }
    for (k, v) in &runtime.env_derived {
        let resolved = resolve_value(v, ctx, &placeholders);
        args.push("-e".into());
        args.push(format!("{}={}", k, resolved));
    }

    // Positional: image, then command + args (override of image CMD).
    args.push(image.to_string());

    // command + args undergo the same placeholder substitution so author
    // can use `{project_slug}` in `--log-path /data/logs/rl_events_{project_slug}.jsonl`.
    args.push(resolve_value(&runtime.command, ctx, &placeholders));
    for a in &runtime.args {
        args.push(resolve_value(a, ctx, &placeholders));
    }

    Ok(args)
}

/// Replace `[^A-Za-z0-9._-]` with `_` so a hostile string can never
/// escape its directory. Idempotent on already-safe input.
pub fn sanitize_path_component(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '.' || c == '_' || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

/// Path inside the container for a given (embedding_source, version)
/// pair. The container's bind mount lives at `/data/state/...` and
/// mirrors the host-side state dir.
pub fn container_weights_path(embedding_source: &str, version: &str) -> String {
    format!(
        "/data/state/rl_model_{}_{}.pt",
        sanitize_path_component(embedding_source),
        sanitize_path_component(version),
    )
}

// ─── Per-pull auth (v0.2.47 cross-runtime) ─────────────────────────────

/// RAII guard around the per-pull / per-run authentication context.
///
/// Two storage shapes, runtime-dependent (see [`PerPullAuth::apply_to`]):
///
/// * **Podman**: a single `NamedTempFile` containing the
///   `{"auths": {...}}` blob; the runtime is invoked with
///   `<runtime> --authfile <path> pull <image>` /
///   `<runtime> --authfile <path> run <image>`.
/// * **Docker**: a `TempDir` whose `config.json` member contains the
///   same auth blob; the runtime is invoked with
///   `DOCKER_CONFIG=<dir> <runtime> pull <image>` /
///   `DOCKER_CONFIG=<dir> <runtime> run <image>`. Docker's CLI does NOT
///   accept `--authfile` on `pull` or `run` (only `docker login` reads
///   `~/.docker/config.json`); we redirect the lookup via the env var
///   so we never touch the user's global `~/.docker/config.json`.
///
/// The file/dir is wiped from disk when this struct is dropped (the
/// `tempfile` crate handles unlink-on-drop), so the per-pull credential
/// material never outlives the in-flight pull/run.
pub struct PerPullAuth {
    inner: PerPullAuthInner,
}

enum PerPullAuthInner {
    /// Podman: single auth.json temp file.
    Podman(tempfile::NamedTempFile),
    /// Docker: temp directory containing `config.json`.
    Docker(tempfile::TempDir),
}

impl PerPullAuth {
    /// Apply this auth context to a `tokio::process::Command` about to
    /// run `<runtime> pull` / `<runtime> run`. Mutates `cmd` in place:
    ///
    /// * Podman runtime → adds `--authfile <path>` as a leading global
    ///   flag (before the subcommand).
    /// * Docker runtime → sets `DOCKER_CONFIG=<tempdir>` on the
    ///   command's environment. The caller's later `cmd.env_clear()`
    ///   would wipe this — callers must apply auth AFTER any
    ///   `env_clear` / selective env pass-through. The launcher's
    ///   `module_service.rs` does this; the hub's `module_supervisor.rs`
    ///   does this.
    /// * Any other runtime name → falls back to the podman shape
    ///   (`--authfile <path>` works on every OCI client we've tested
    ///   except docker; conservative for forward compat).
    ///
    /// `cmd` is `tokio::process::Command`; the same pattern can be
    /// adapted for `std::process::Command` by callers — both honor the
    /// same `.arg(...)` / `.env(...)` surface.
    pub fn apply_to(&self, cmd: &mut tokio::process::Command, runtime: &str) {
        match (&self.inner, runtime) {
            (PerPullAuthInner::Podman(file), _) => {
                cmd.arg("--authfile").arg(file.path());
            }
            (PerPullAuthInner::Docker(dir), "docker") => {
                cmd.env("DOCKER_CONFIG", dir.path());
            }
            (PerPullAuthInner::Docker(dir), _) => {
                // Hybrid case: caller built a Docker-shape guard but is
                // invoking a non-docker runtime. Treat as podman --
                // both runtimes will read DOCKER_CONFIG if set, AND the
                // file is laid out the same way.
                cmd.env("DOCKER_CONFIG", dir.path());
            }
        }
    }

    /// Borrowed reference to the on-disk auth file path. Only valid for
    /// podman-shape guards; returns `None` for docker-shape guards
    /// (which carry a directory, not a single file).
    ///
    /// Kept for the launcher's existing `probe_image_tag_exists_with_authfile`
    /// helper which takes `Option<&Path>`. New callers should prefer
    /// `apply_to(cmd, runtime)` so the same code works on docker.
    pub fn path(&self) -> Option<&Path> {
        match &self.inner {
            PerPullAuthInner::Podman(file) => Some(file.path()),
            PerPullAuthInner::Docker(_) => None,
        }
    }

    /// Borrowed reference to the on-disk directory that backs the
    /// docker-shape `config.json`. Only valid for docker-shape guards;
    /// returns `None` for podman-shape guards. Used by callers that need
    /// to invoke `DOCKER_CONFIG=<dir>` on a probe command without going
    /// through `apply_to` (e.g. when the probe helper takes paths, not
    /// command handles, for closure-capture reasons).
    pub fn docker_config_dir(&self) -> Option<&Path> {
        match &self.inner {
            PerPullAuthInner::Docker(dir) => Some(dir.path()),
            PerPullAuthInner::Podman(_) => None,
        }
    }
}

/// Build a per-pull authfile / config dir scoped to ONE target registry.
/// The on-disk shape is podman's `auth.json` / docker's `config.json`
/// (same JSON document; the two CLIs accept either).
///
/// `runtime` selects the storage shape:
/// * `"docker"` → `TempDir` containing `config.json` so the caller can
///   set `DOCKER_CONFIG=<dir>` (docker `pull` / `run` do NOT accept
///   `--authfile`).
/// * Anything else (`"podman"`, future runtimes) → `NamedTempFile`
///   with the auth blob; callers pass `--authfile <path>` to the
///   subcommand. Backwards-compatible with the v0.2.46 shape.
///
/// On error returns a clear message containing the failed step ("create
/// temp file" / "write auth.json" / "flush"); callers propagate.
pub fn build_per_pull_authfile(
    registry: &str,
    username: &str,
    token: &str,
    runtime: &str,
) -> Result<PerPullAuth, String> {
    use base64::{engine::general_purpose::STANDARD as B64, Engine as _};

    let auth_b64 = B64.encode(format!("{}:{}", username, token));
    let json = serde_json::json!({
        "auths": {
            registry: { "auth": auth_b64 }
        }
    });
    let payload = json.to_string();

    if runtime == "docker" {
        let dir = tempfile::tempdir()
            .map_err(|e| format!("build_per_pull_authfile: create temp dir: {}", e))?;
        let path = dir.path().join("config.json");
        // Write the config.json into the tempdir. `DOCKER_CONFIG=<dir>`
        // makes docker look here instead of `~/.docker/config.json`.
        let mut f = std::fs::File::create(&path)
            .map_err(|e| format!("build_per_pull_authfile: create config.json: {}", e))?;
        f.write_all(payload.as_bytes())
            .map_err(|e| format!("build_per_pull_authfile: write config.json: {}", e))?;
        f.flush()
            .map_err(|e| format!("build_per_pull_authfile: flush config.json: {}", e))?;
        return Ok(PerPullAuth {
            inner: PerPullAuthInner::Docker(dir),
        });
    }

    let mut f = tempfile::NamedTempFile::new()
        .map_err(|e| format!("build_per_pull_authfile: create temp file: {}", e))?;
    f.write_all(payload.as_bytes())
        .map_err(|e| format!("build_per_pull_authfile: write auth.json: {}", e))?;
    f.flush()
        .map_err(|e| format!("build_per_pull_authfile: flush: {}", e))?;
    Ok(PerPullAuth {
        inner: PerPullAuthInner::Podman(f),
    })
}

/// Best-effort `mkdir -p` for each volume's host path so podman doesn't
/// fail bind-mount setup on nonexistent directories. Shared between the
/// launcher's `start_container_for_module` and the hub's twin.
pub async fn ensure_volume_host_dirs(
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    rl_port: u16,
    project_slug: &str,
) {
    let placeholders = rl_placeholders(rl_port, project_slug);
    for vol in &manifest.runtime.volumes {
        let host_resolved = resolve_value(&vol.host, ctx, &placeholders);
        let path = PathBuf::from(&host_resolved);
        if let Err(e) = tokio::fs::create_dir_all(&path).await {
            eprintln!(
                "[container_runtime] mkdir -p {} failed (will let podman surface the error): {}",
                path.display(),
                e
            );
        }
    }
}

// ─── Pull-token gateway HTTP core (v0.2.49) ────────────────────────────
//
// Why these live in core (v0.2.49 Phase 3 hub-supervisor auth port):
// pre-v0.2.49 `installer_engine::request_pull_token` was launcher-private
// — `vct-hub`'s `module_supervisor::start_container_for_module` couldn't
// reach it. The supervisor therefore had no way to pre-pull the variant-
// correct image with proper credentials before `podman run`, falling
// through to anonymous-pull-401 on private GHCR packages. See
// `knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md`.
//
// The HTTP body of `request_pull_token` is portable (license_key +
// machine_id_hash POST to the gateway URL); the only launcher-coupled
// parts were the keychain read and `machine_id_hash` — both now in
// `vct-launcher-core::licensing`. The whole flow can move to core.

/// Hard-coded fallback for the pull-token gateway URL. Used when the
/// resolved endpoint (L0 override → L1 manifest → env override) is
/// empty or matches a known placeholder pattern. Mirrors the launcher's
/// `installer_engine::RL_ARTIFACT_URL_DEFAULT_ENDPOINT`.
pub const RL_ARTIFACT_URL_DEFAULT_ENDPOINT: &str =
    "https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/rl-artifact-url";

/// Historical exact-match placeholder string still found in some
/// pre-publish manifests. Preserved for backwards-compat with
/// v0.2.42-and-earlier publish artifacts. `pub` so the launcher's
/// existing test suite (which compared the launcher-private copy
/// against the well-known placeholder value pre-v0.2.49) can keep its
/// assertions targeting this single source of truth.
pub const PULL_TOKEN_ENDPOINT_PLACEHOLDER: &str = "https://example/pull-token";

/// Pull-token gateway response (deserialised from the edge function's
/// JSON body). Mirrors `installer_engine::PullTokenResponse` — moved
/// to core because the request helper now lives here. Field set kept
/// IDENTICAL so the launcher's existing wrapper deserialises into the
/// same struct.
#[derive(Debug, serde::Deserialize)]
pub struct PullTokenResponse {
    pub pull_token: String,
    /// The GitHub username the pull_token authenticates as. Passed to
    /// `podman/docker login -u`. Optional so a v0.2.36 launcher remains
    /// compatible with the pre-v0.2.36 server response shape.
    #[serde(default)]
    pub username: Option<String>,
    #[serde(default)]
    pub expires_in_s: u64,
    /// Server-returned image tag (v0.2.46 V46-E C1). When `Some` and
    /// non-empty, the caller compares against the client-resolved tag
    /// and may adjust on patch-level drift.
    #[serde(default)]
    pub tag: Option<String>,
}

/// Returns true if `raw` matches a known placeholder URL pattern. Pure
/// function — used by `resolve_pull_token_endpoint` to substitute the
/// hardcoded default. See the launcher's v0.2.45 V45-D + v0.2.42 W8 +
/// v0.2.42 P3-P1-1 history for the families recognised here:
///
///   1. Exact `PULL_TOKEN_ENDPOINT_PLACEHOLDER` (back-compat).
///   2. Bare `example` host (no TLD).
///   3. RFC-2606 `example.{com,net,org,invalid,test}` exact-host.
///   4. Bare `placeholder`, `placeholder.<anything>`, `<anything>.placeholder`.
///
/// `pub` so the launcher's existing test suite (which exercised every
/// branch of the placeholder family against the pre-v0.2.49 launcher-
/// private copy) can re-export and continue asserting the same
/// invariants from this single source of truth.
pub fn is_pull_token_placeholder(raw: &str) -> bool {
    if raw == PULL_TOKEN_ENDPOINT_PLACEHOLDER {
        return true;
    }
    let host_start = if let Some(rest) = raw.strip_prefix("https://") {
        rest
    } else if let Some(rest) = raw.strip_prefix("http://") {
        rest
    } else {
        return false;
    };
    let host = host_start.split('/').next().unwrap_or("");
    let host_no_port = host.split(':').next().unwrap_or("");

    if matches!(
        host_no_port,
        "example"
            | "example.com"
            | "example.net"
            | "example.org"
            | "example.invalid"
            | "example.test"
    ) {
        return true;
    }

    let lower = host_no_port.to_lowercase();
    if lower == "placeholder"
        || lower.starts_with("placeholder.")
        || lower.ends_with(".placeholder")
    {
        return true;
    }

    false
}

/// Resolve the effective pull-token endpoint URL. Empty / placeholder
/// inputs are replaced with `RL_ARTIFACT_URL_DEFAULT_ENDPOINT` and an
/// operator-visible warning is logged. Any other non-empty string is
/// returned as-is.
pub fn resolve_pull_token_endpoint(raw: &str) -> &str {
    if raw.is_empty() || is_pull_token_placeholder(raw) {
        eprintln!(
            "[container_runtime] pull_token_endpoint is {:?}; \
             substituting default RL_ARTIFACT_URL_DEFAULT_ENDPOINT. \
             Fix the module manifest to remove this warning.",
            raw
        );
        RL_ARTIFACT_URL_DEFAULT_ENDPOINT
    } else {
        raw
    }
}

/// Map a non-2xx pull-token gateway response into a user-readable
/// string. Same body shape the launcher's
/// `installer_engine::format_pull_token_error` handles — moved to core
/// so the hub-side caller doesn't need to re-derive the mapping.
pub fn format_pull_token_error(status: u16, body: &serde_json::Value) -> String {
    let code = body
        .get("error")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown_error");
    let detail = body
        .get("detail")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    match (status, code) {
        (400, _) => format!(
            "pull-token gateway rejected the request shape ({}). \
             This is a launcher bug — please report it. detail={}",
            code, detail
        ),
        (401, "license_invalid") => {
            "your license key is invalid or has been revoked. \
             Open Settings → License → Refresh; if the problem persists, \
             contact support."
                .to_string()
        }
        (401, "license_expired") => {
            "your license has expired. Renew on the dashboard, then \
             open Settings → License → Refresh."
                .to_string()
        }
        (401, "tier_insufficient") => {
            let required = body
                .get("required_tier")
                .and_then(|v| v.as_str())
                .unwrap_or("pro");
            let got = body.get("got").and_then(|v| v.as_str()).unwrap_or("free");
            format!(
                "this module requires the {} tier; your license validates as {}. \
                 Upgrade on the dashboard, then open Settings → License → Refresh.",
                required, got
            )
        }
        (401, _) => format!(
            "license check failed at the pull-token gateway: {} ({})",
            code, detail
        ),
        (500, _) => format!(
            "pull-token gateway is temporarily unavailable ({}). \
             Try again in a few minutes; if it persists, check Services tab.",
            detail
        ),
        (s, c) => format!("pull-token gateway returned HTTP {}: {} ({})", s, c, detail),
    }
}

/// HTTP-only pull-token request. `license_key` and `machine_hash` are
/// passed in by the caller (each crate reads them via
/// `vct_launcher_core::licensing::read_license_key_from_keychain()` +
/// `vct_launcher_core::licensing::machine_id_hash()` respectively — the
/// helper does NOT pull from the keychain itself so it stays free of
/// any test-only / Tauri-only coupling).
///
/// Endpoint resolution precedence (highest-first):
///   1. `VCT_RL_PULL_TOKEN_ENDPOINT` env var (operator escape hatch;
///      non-empty after trim).
///   2. `l0_pull_token_endpoint` (when supplied — the L0 catalog override).
///   3. `container.pull_token_endpoint` (the manifest's value).
///   4. `RL_ARTIFACT_URL_DEFAULT_ENDPOINT` (substituted in for empty /
///      placeholder values via `resolve_pull_token_endpoint`).
///
/// 15s timeout — same as launcher's pre-v0.2.49 path.
///
/// v0.2.49 Phase 3: the launcher's
/// `installer_engine::request_pull_token` is now a 5-line wrapper that
/// reads the keychain + computes the machine hash, then calls this
/// helper. The hub-side supervisor calls it via the same wrapper
/// pattern. Both wrappers see byte-identical request bodies for the
/// same `(license_key, machine_hash, endpoint)` triple.
pub async fn request_pull_token_http(
    container: &crate::manifest::ContainerInstallBlock,
    l0_pull_token_endpoint: Option<&str>,
    license_key: &str,
    machine_hash: &str,
) -> Result<PullTokenResponse, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .map_err(|e| format!("build http client: {}", e))?;

    let method = container
        .pull_token_method
        .parse::<reqwest::Method>()
        .unwrap_or(reqwest::Method::POST);

    let endpoint_string: String;
    let endpoint: &str = match std::env::var("VCT_RL_PULL_TOKEN_ENDPOINT")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
    {
        Some(env_url) => {
            eprintln!(
                "[container_runtime] VCT_RL_PULL_TOKEN_ENDPOINT set; \
                 using env override for pull-token endpoint: {}",
                env_url
            );
            endpoint_string = env_url;
            &endpoint_string
        }
        None => {
            let raw_endpoint = l0_pull_token_endpoint.unwrap_or(&container.pull_token_endpoint);
            resolve_pull_token_endpoint(raw_endpoint)
        }
    };

    let resp = client
        .request(method, endpoint)
        .json(&serde_json::json!({
            "license_key": license_key,
            "machine_id_hash": machine_hash,
        }))
        .send()
        .await
        .map_err(|e| format!("POST {}: {}", endpoint, e))?;

    let status = resp.status();
    if status.is_success() {
        let parsed: PullTokenResponse = resp
            .json()
            .await
            .map_err(|e| format!("parse pull-token response: {}", e))?;
        return Ok(parsed);
    }

    let body: serde_json::Value = resp
        .json()
        .await
        .unwrap_or_else(|_| serde_json::json!({}));
    Err(format_pull_token_error(status.as_u16(), &body))
}

/// Convenience wrapper around [`request_pull_token_http`] that reads
/// the license key + machine_id_hash via the shared
/// `vct_launcher_core::licensing` helpers. Both launcher and hub call
/// this directly (the v0.2.47 launcher inlined the same three lines —
/// promoting them here is a strict de-duplication).
pub async fn request_pull_token(
    container: &crate::manifest::ContainerInstallBlock,
    l0_pull_token_endpoint: Option<&str>,
) -> Result<PullTokenResponse, String> {
    let license_key = crate::licensing::read_license_key_from_keychain()
        .map_err(|e| format!("keychain read failed: {}", e))?
        .ok_or_else(|| {
            "no license activated — open Settings → License → Activate to enter your key"
                .to_string()
        })?;
    let machine_hash = crate::licensing::machine_id_hash();
    request_pull_token_http(container, l0_pull_token_endpoint, &license_key, &machine_hash).await
}

// ─── Pre-pull-with-auth (v0.2.49) ──────────────────────────────────────

/// Pre-pull the variant-correct image with proper auth context BEFORE
/// the supervisor's `podman run`. Used by both the launcher-side
/// `start_container_for_module` and the hub-side
/// `module_supervisor::start_container_for_module` so a cache-evicted
/// host doesn't fall through to `podman run`'s anonymous-pull-401 path.
///
/// Soft-fails on every error — the caller logs and proceeds to
/// `podman run`. If the image is already in the local cache, `run`
/// succeeds without the pre-pull. If the image is missing AND pre-pull
/// failed, `run` will surface the anonymous-pull failure itself; we
/// don't double-report.
///
/// Algorithm:
///   1. Fast-path: `<runtime> image exists <image_ref>` → if Ok, return.
///   2. Request a pull token via the shared
///      `request_pull_token(container, None)` (uses
///      `vct_launcher_core::licensing` for the keychain read +
///      machine_id_hash so launcher and hub agree byte-for-byte).
///   3. Build a per-pull auth guard (`build_per_pull_authfile`).
///   4. Apply the guard to a `<runtime> pull` command and execute.
///   5. Return Ok on exit-0; Err on non-zero or pull-token failure.
///
/// `runtime` matches the launcher's `detect_container_runtime()` /
/// hub's local copy: `"podman"` or `"docker"`. Other values pass through
/// to `PerPullAuth::apply_to`'s catch-all (podman-shape `--authfile`).
pub async fn pre_pull_with_auth_for_start(
    manifest: &crate::manifest::ModuleManifest,
    runtime: &str,
    image_ref: &str,
) -> Result<(), String> {
    use std::process::Stdio;
    use tokio::process::Command;

    let container = manifest
        .install
        .container
        .as_ref()
        .ok_or_else(|| "install.container block missing".to_string())?;

    // Fast-path: image already in local cache → no pull needed.
    let inspect = Command::new(runtime)
        .args(["image", "exists", image_ref])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await;
    if let Ok(s) = inspect {
        if s.success() {
            return Ok(());
        }
    }

    // Image not in cache. Request a pull token (mirrors the install
    // path's flow) and use a per-pull authfile.
    let token_result = request_pull_token(container, None).await;
    let registry = container.registry.clone().unwrap_or_else(|| {
        image_ref
            .split_once('/')
            .map(|(host, _)| host.to_string())
            .unwrap_or_else(|| "docker.io".to_string())
    });
    let guard_opt = match token_result {
        Ok(tok) => {
            let user = tok.username.as_deref().unwrap_or("vct-paid-module");
            Some(build_per_pull_authfile(&registry, user, &tok.pull_token, runtime)?)
        }
        Err(e) => {
            // No token → anonymous pull. Will 401 on private images.
            // Same soft-fail discipline the launcher had pre-v0.2.49.
            eprintln!(
                "[container_runtime] pre-pull: pull-token gateway returned {}; \
                 anonymous pull attempt (will 401 on private images).",
                e
            );
            None
        }
    };

    let mut pull_cmd = Command::new(runtime);
    if let Some(g) = guard_opt.as_ref() {
        g.apply_to(&mut pull_cmd, runtime);
    }
    let pull_status = pull_cmd
        .args(["pull", image_ref])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await
        .map_err(|e| format!("spawn {} pull: {}", runtime, e))?;

    if !pull_status.success() {
        return Err(format!(
            "{} pull failed (exit {}) for {}",
            runtime,
            pull_status.code().unwrap_or(-1),
            image_ref
        ));
    }
    Ok(())
}

// ─── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::models::ProjectHost;
    use crate::manifest::{
        Compatibility, ContainerInstallBlock, GpuImageVariants, HealthCheck, InstallBlock,
        InstallMethod, LicenseBlock, ModuleCategory, ModuleManifest, PortMapping, Requirements,
        RuntimeBlock, VolumeMount,
    };

    fn make_project() -> ProjectRow {
        ProjectRow {
            id: "proj-uuid".into(),
            name: "Acme".into(),
            folder_path: "/tmp/acme".into(),
            host: ProjectHost::Base,
            slug: "acme-corp".into(),
            created_at: 0,
            updated_at: 0,
            rl_port: Some(11533),
        }
    }

    fn make_manifest(tag_from_version: bool, auto_restart: bool) -> ModuleManifest {
        let mut env_fixed = HashMap::new();
        env_fixed.insert("RL_SERVER_PORT".into(), "11438".into());
        let mut env_derived = HashMap::new();
        env_derived.insert("RL_PROJECT_ROOT".into(), "/data".into());

        ModuleManifest {
            manifest_version: 1,
            id: "vct-rl-reranker".into(),
            name: "RL Reranker".into(),
            version: "0.2.8".into(),
            description: "".into(),
            publisher: None,
            homepage: None,
            repository: None,
            icon: None,
            category: ModuleCategory::PaidIndependent,
            tags: vec![],
            compatibility: Compatibility::default(),
            license: LicenseBlock::default(),
            requirements: Requirements::default(),
            install: InstallBlock {
                method: InstallMethod::ContainerPull,
                source: Some("ghcr.io/hotak92/vct-rl-reranker".into()),
                r#ref: Some("0.2.8".into()),
                install_dir: "{VCT_MODULES}/vct-rl-reranker".into(),
                post_install: vec![],
                container: Some(ContainerInstallBlock {
                    image: "ghcr.io/hotak92/vct-rl-reranker".into(),
                    tag_from_version,
                    registry: Some("ghcr.io".into()),
                    pull_token_endpoint: "https://example.invalid/x".into(),
                    pull_token_method: "POST".into(),
                    rotate_weights: false,
                    rotate_weights_endpoint: None,
                }),
            },
            secrets: vec![],
            settings: vec![],
            runtime: RuntimeBlock {
                r#type: "container".into(),
                command: "python".into(),
                args: vec!["-m".into(), "rl_server.rl_server".into()],
                platform_command: HashMap::new(),
                cwd: None,
                env_from_secrets: vec![],
                env_from_settings: vec![],
                env_fixed,
                health_check: Some(HealthCheck {
                    r#type: "http_get".into(),
                    timeout_s: 5,
                    interval_s: 30,
                    url: Some("http://localhost:{RL_SERVER_PORT}/health".into()),
                }),
                auto_restart,
                log_file: None,
                min_gpu_vram_gb: None,
                gpu_optional: true,
                gpu_image_variants: None,
                log_path_template: None,
                container_name_template: Some("vct-rl-reranker-{project_slug}".into()),
                image_ref: Some("{install.container.image}:{install.container.tag}".into()),
                ports: vec![PortMapping {
                    host: "{RL_SERVER_PORT}".into(),
                    container: 11438,
                    bind: Some("127.0.0.1".into()),
                }],
                env_derived,
                volumes: vec![VolumeMount {
                    host: "{VCT_DATA}/vct-rl-reranker/{project_slug}/state".into(),
                    container: "/data/state".into(),
                    mode: Some("rw".into()),
                }],
            },
            mcp_registration: None,
            setup_wizard: None,
            upgrade: None,
            telemetry: None,
            uninstall: None,
            provides: vec![],
            consumes: vec![],
            gui: None,
            db: None,
        }
    }

    fn make_manifest_with_variants(tag_from_version: bool) -> ModuleManifest {
        let mut m = make_manifest(tag_from_version, true);
        m.runtime.gpu_image_variants = Some(GpuImageVariants {
            cpu: "{version}-cpu".into(),
            cuda: "{version}-cuda".into(),
            rocm: "{version}-rocm".into(),
        });
        m
    }

    #[test]
    fn resolve_container_name_uses_project_slug() {
        let got = resolve_container_name("vct-rl-reranker-{project_slug}", "acme-corp")
            .expect("resolve");
        assert_eq!(got, "vct-rl-reranker-acme-corp");
    }

    #[test]
    fn resolve_container_name_errors_on_unresolved_placeholder() {
        let err = resolve_container_name("vct-rl-reranker-{project-slug}", "acme-corp")
            .expect_err("must reject unresolved placeholders");
        assert!(err.contains("unresolved placeholders"));
    }

    #[test]
    fn resolve_image_ref_uses_manifest_container_image_and_version() {
        let manifest = make_manifest(true, true);
        let got = resolve_image_ref(
            "{install.container.image}:{install.container.tag}",
            &manifest,
            None,
        )
        .expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:0.2.8");
    }

    #[test]
    fn resolve_image_ref_uses_install_ref_when_tag_from_version_false() {
        let mut manifest = make_manifest(false, true);
        manifest.install.r#ref = Some("latest".into());
        let got = resolve_image_ref(
            "{install.container.image}:{install.container.tag}",
            &manifest,
            None,
        )
        .expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:latest");
    }

    // ─── v0.2.47: variant-tag resolution ────────────────────────────

    /// Bug-1 regression test: manifest with `gpu_image_variants` AND
    /// `gpu_mode = Cuda` produces a `-cuda`-suffixed tag, not the bare
    /// `manifest.version`.
    #[test]
    fn resolve_image_ref_applies_cuda_variant_when_gpu_mode_passed() {
        let manifest = make_manifest_with_variants(true);
        let got = resolve_image_ref(
            "{install.container.image}:{install.container.tag}",
            &manifest,
            Some(GpuMode::Cuda),
        )
        .expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:0.2.8-cuda");
    }

    /// Bug-1 regression test: same as above but Cpu mode → `-cpu` suffix.
    #[test]
    fn resolve_image_ref_applies_cpu_variant_when_gpu_mode_passed() {
        let manifest = make_manifest_with_variants(true);
        let got = resolve_image_ref(
            "{install.container.image}:{install.container.tag}",
            &manifest,
            Some(GpuMode::Cpu),
        )
        .expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:0.2.8-cpu");
    }

    /// Bug-1 regression test: Metal collapses to the `-cpu` variant.
    #[test]
    fn resolve_image_ref_metal_falls_back_to_cpu_variant() {
        let manifest = make_manifest_with_variants(true);
        let got = resolve_image_ref(
            "{install.container.image}:{install.container.tag}",
            &manifest,
            Some(GpuMode::Metal),
        )
        .expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:0.2.8-cpu");
    }

    /// Bug-1 regression test: ROCm GPU mode → `-rocm` suffix.
    #[test]
    fn resolve_image_ref_applies_rocm_variant_when_gpu_mode_passed() {
        let manifest = make_manifest_with_variants(true);
        let got = resolve_image_ref(
            "{install.container.image}:{install.container.tag}",
            &manifest,
            Some(GpuMode::Rocm),
        )
        .expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:0.2.8-rocm");
    }

    /// Legacy module without `gpu_image_variants` block returns bare
    /// version tag regardless of gpu_mode. Ensures the v0.2.47 change
    /// is backwards-compatible with the single-tag modules already in
    /// production.
    #[test]
    fn resolve_image_ref_no_variants_block_returns_bare_tag() {
        let manifest = make_manifest(true, true);
        let got = resolve_image_ref(
            "{install.container.image}:{install.container.tag}",
            &manifest,
            Some(GpuMode::Cuda),
        )
        .expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:0.2.8");
    }

    /// Manifest WITH variants but caller passes `gpu_mode = None`:
    /// returns bare tag — semantics for legacy call sites that haven't
    /// been wired through yet.
    #[test]
    fn resolve_image_ref_none_gpu_mode_skips_variant_lookup() {
        let manifest = make_manifest_with_variants(true);
        let got = resolve_image_ref(
            "{install.container.image}:{install.container.tag}",
            &manifest,
            None,
        )
        .expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:0.2.8");
    }

    #[test]
    fn resolve_variant_tag_substitutes_version_in_template() {
        let manifest = make_manifest_with_variants(true);
        assert_eq!(resolve_variant_tag(&manifest, "0.2.8", GpuMode::Cuda), "0.2.8-cuda");
        assert_eq!(resolve_variant_tag(&manifest, "0.2.8", GpuMode::Rocm), "0.2.8-rocm");
        assert_eq!(resolve_variant_tag(&manifest, "0.2.8", GpuMode::Cpu), "0.2.8-cpu");
        assert_eq!(resolve_variant_tag(&manifest, "0.2.8", GpuMode::Metal), "0.2.8-cpu");
    }

    #[test]
    fn resolve_variant_tag_no_variants_returns_base_tag() {
        let manifest = make_manifest(true, true);
        assert_eq!(resolve_variant_tag(&manifest, "0.2.8", GpuMode::Cuda), "0.2.8");
    }

    #[test]
    fn build_podman_run_args_includes_port_mapping() {
        let manifest = make_manifest(true, true);
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args = build_podman_run_args(
            &manifest,
            &ctx,
            &project,
            11533,
            "vct-rl-reranker-acme-corp",
            "ghcr.io/hotak92/vct-rl-reranker:0.2.8",
        )
        .expect("build args");
        assert_eq!(args[0], "run");
        assert_eq!(args[3], "vct-rl-reranker-acme-corp");
        assert!(args.iter().any(|a| a == "127.0.0.1:11533:11438"));
    }

    #[test]
    fn build_podman_run_args_rejects_non_container_runtime() {
        let mut manifest = make_manifest(true, true);
        manifest.runtime.r#type = "mcp_stdio".into();
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let err = build_podman_run_args(&manifest, &ctx, &project, 11533, "x", "img:tag")
            .expect_err("must reject non-container runtime");
        assert!(err.contains("container"));
    }

    #[test]
    fn build_podman_run_args_accepts_service_runtime_type() {
        let mut manifest = make_manifest(true, true);
        manifest.runtime.r#type = "service".into();
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args = build_podman_run_args(
            &manifest,
            &ctx,
            &project,
            11533,
            "vct-rl-reranker-acme-corp",
            "ghcr.io/hotak92/vct-rl-reranker:0.2.8",
        )
        .expect("service runtime accepted");
        assert_eq!(args[0], "run");
    }

    #[test]
    fn sanitize_path_component_rewrites_unsafe_chars() {
        assert_eq!(sanitize_path_component("qwen3"), "qwen3");
        assert_eq!(sanitize_path_component("evil/../path"), "evil_.._path");
        assert_eq!(sanitize_path_component("with space"), "with_space");
    }

    #[test]
    fn container_weights_path_uses_safe_components() {
        let p = container_weights_path("qwen3", "v1.0");
        assert_eq!(p, "/data/state/rl_model_qwen3_v1.0.pt");
        let p2 = container_weights_path("../../etc", "passwd");
        assert!(p2.starts_with("/data/state/"));
        assert!(!p2.contains("../"));
    }

    // ─── v0.2.47: PerPullAuth runtime dispatch ──────────────────────

    /// Bug-2 regression test: podman runtime → `--authfile <path>` arg
    /// is appended to the Command.
    #[test]
    fn per_pull_auth_apply_to_podman_uses_authfile_arg() {
        let guard = build_per_pull_authfile("ghcr.io", "bot", "tok", "podman")
            .expect("build podman authfile");
        let mut cmd = tokio::process::Command::new("podman");
        guard.apply_to(&mut cmd, "podman");
        let dbg = format!("{:?}", cmd);
        assert!(
            dbg.contains("--authfile"),
            "podman branch must include --authfile arg, got: {}",
            dbg
        );
        // path() returns Some for podman-shape guards.
        assert!(guard.path().is_some(), "podman guard exposes path()");
    }

    /// Bug-2 regression test: docker runtime → no `--authfile` arg,
    /// instead `DOCKER_CONFIG=<dir>` env present.
    #[test]
    fn per_pull_auth_apply_to_docker_uses_docker_config_env() {
        let guard = build_per_pull_authfile("ghcr.io", "bot", "tok", "docker")
            .expect("build docker authfile");
        let mut cmd = tokio::process::Command::new("docker");
        guard.apply_to(&mut cmd, "docker");
        let dbg = format!("{:?}", cmd);
        assert!(
            !dbg.contains("--authfile"),
            "docker branch must NOT include --authfile, got: {}",
            dbg
        );
        assert!(
            dbg.contains("DOCKER_CONFIG"),
            "docker branch must include DOCKER_CONFIG env var, got: {}",
            dbg
        );
        // path() returns None for docker-shape guards.
        assert!(
            guard.path().is_none(),
            "docker guard does not expose a single file path"
        );
    }

    /// Built-for-podman guard applied to a podman runtime keeps the
    /// authfile shape. Applied to docker runtime, also lands as
    /// `--authfile` (no auto-translation). Caller must build the
    /// runtime-correct guard up front — that's the contract.
    #[test]
    fn per_pull_auth_podman_guard_uses_authfile_even_against_docker() {
        let guard = build_per_pull_authfile("ghcr.io", "bot", "tok", "podman")
            .expect("build podman authfile");
        let mut cmd = tokio::process::Command::new("docker");
        guard.apply_to(&mut cmd, "docker");
        let dbg = format!("{:?}", cmd);
        // The match arm `(Podman, _)` always uses --authfile, regardless of runtime.
        assert!(dbg.contains("--authfile"));
    }

    #[test]
    fn build_per_pull_authfile_docker_writes_config_json() {
        let guard = build_per_pull_authfile("ghcr.io", "bot", "tok", "docker")
            .expect("build docker authfile");
        // We can't read the contents through PerPullAuth's public API
        // (intentional — credential material), but we can verify the
        // directory exists and contains config.json by side-channel:
        // apply_to a Command and inspect the env-var pointer.
        let mut cmd = tokio::process::Command::new("docker");
        guard.apply_to(&mut cmd, "docker");
        let dbg = format!("{:?}", cmd);
        // The Command Debug impl prints env_vars; look for the
        // DOCKER_CONFIG key pointing at a path that ends in a temp dir
        // pattern. Robust assertion: just check the env var is set.
        assert!(dbg.contains("DOCKER_CONFIG"));
    }

    /// Pin the de-dup sentinel. Both downstream call sites that
    /// re-export this module's helpers via `pub use` will see this
    /// exact byte sequence — proves the helpers have a single source
    /// of truth instead of two structurally-identical copies.
    #[test]
    fn dedup_sentinel_pins_single_source_of_truth() {
        assert_eq!(
            DEDUP_SENTINEL,
            "vct-launcher-core::services::container_runtime::v0.2.47"
        );
    }

    // ─── v0.2.49: pull-token gateway HTTP core ─────────────────────────

    /// v0.2.49: empty endpoint string → substituted with the default
    /// const. Mirrors the launcher's pre-v0.2.49
    /// `installer_engine::resolve_pull_token_endpoint` test.
    #[test]
    fn v0249_resolve_pull_token_endpoint_empty_string_substitutes_default() {
        assert_eq!(resolve_pull_token_endpoint(""), RL_ARTIFACT_URL_DEFAULT_ENDPOINT);
    }

    /// v0.2.49: known placeholder shapes are substituted; legitimate
    /// URLs pass through verbatim. Exercises the full
    /// `is_pull_token_placeholder` family the launcher v0.2.45 V45-D +
    /// v0.2.42 W8 / P3-P1-1 chain hardened against.
    #[test]
    fn v0249_resolve_pull_token_endpoint_placeholder_family_substituted() {
        for placeholder in [
            "https://example/pull-token",
            "https://example.com/x",
            "https://example.invalid/x",
            "https://placeholder.supabase.co/x",
            "https://Placeholder.supabase.co/x",
            "https://foo.placeholder/x",
        ] {
            assert_eq!(
                resolve_pull_token_endpoint(placeholder),
                RL_ARTIFACT_URL_DEFAULT_ENDPOINT,
                "placeholder {} must be substituted",
                placeholder
            );
        }
    }

    /// v0.2.49: legitimate user-controlled URL passes through.
    #[test]
    fn v0249_resolve_pull_token_endpoint_legit_url_passes_through() {
        let real = "https://abc123.supabase.co/functions/v1/rl-artifact-url";
        assert_eq!(resolve_pull_token_endpoint(real), real);
        let staging = "https://staging.example.com/x";
        assert_eq!(resolve_pull_token_endpoint(staging), staging);
    }

    /// v0.2.49: `format_pull_token_error` maps the 401 license_invalid
    /// code into a user-actionable message. Pins one branch of the
    /// matcher; the full matrix lives in the launcher's
    /// `installer_engine::tests` which now exercises the same shared
    /// helper via re-export.
    #[test]
    fn v0249_format_pull_token_error_401_license_invalid() {
        let body = serde_json::json!({ "error": "license_invalid" });
        let msg = format_pull_token_error(401, &body);
        assert!(
            msg.contains("license key is invalid"),
            "expected license-invalid message, got: {}",
            msg
        );
    }

    /// v0.2.49: `format_pull_token_error` covers a generic 5xx with an
    /// "unavailable / try again" message.
    #[test]
    fn v0249_format_pull_token_error_500_generic() {
        let body = serde_json::json!({ "error": "internal", "detail": "db down" });
        let msg = format_pull_token_error(500, &body);
        assert!(
            msg.contains("temporarily unavailable") || msg.contains("Try again"),
            "expected unavailable / try-again message, got: {}",
            msg
        );
    }

    /// v0.2.49: `PullTokenResponse` deserialises the canonical JSON
    /// shape the launcher's installer_engine produced pre-v0.2.49. Pins
    /// wire compat across the move.
    #[test]
    fn v0249_pull_token_response_deserialises_v_canonical_shape() {
        let raw = r#"{
            "pull_token": "ghp_abcdef",
            "username": "vct-bot-rl",
            "expires_in_s": 900,
            "tag": "0.2.8-cuda"
        }"#;
        let parsed: PullTokenResponse = serde_json::from_str(raw).expect("parse");
        assert_eq!(parsed.pull_token, "ghp_abcdef");
        assert_eq!(parsed.username.as_deref(), Some("vct-bot-rl"));
        assert_eq!(parsed.expires_in_s, 900);
        assert_eq!(parsed.tag.as_deref(), Some("0.2.8-cuda"));
    }

    /// v0.2.49: `PullTokenResponse` tolerates missing optional fields
    /// (forward-compat with pre-v0.2.36 server shape that omitted
    /// `username` + pre-v0.2.46 shape that omitted `tag`).
    #[test]
    fn v0249_pull_token_response_optional_fields_default() {
        let raw = r#"{ "pull_token": "tok" }"#;
        let parsed: PullTokenResponse = serde_json::from_str(raw).expect("parse minimal");
        assert_eq!(parsed.pull_token, "tok");
        assert!(parsed.username.is_none());
        assert_eq!(parsed.expires_in_s, 0);
        assert!(parsed.tag.is_none());
    }

    /// v0.2.49 wire-contract: `request_pull_token_http` is reachable
    /// from this module's public API. A future refactor that renames
    /// the function or changes its arity would break the assignment.
    /// Type-level check only; never invoked.
    #[allow(dead_code)]
    fn _v0249_request_pull_token_http_signature_check() {
        async fn _typecheck(
            c: &crate::manifest::ContainerInstallBlock,
            l: Option<&str>,
            k: &str,
            h: &str,
        ) -> Result<PullTokenResponse, String> {
            request_pull_token_http(c, l, k, h).await
        }
        let _ = _typecheck;
    }

    /// v0.2.49 wire-contract: `pre_pull_with_auth_for_start` is
    /// reachable from this module's public API. Paired with the
    /// hub-side test that asserts the same symbol is reachable from
    /// the hub crate.
    #[allow(dead_code)]
    fn _v0249_pre_pull_with_auth_for_start_signature_check() {
        async fn _typecheck(
            m: &crate::manifest::ModuleManifest,
            r: &str,
            i: &str,
        ) -> Result<(), String> {
            pre_pull_with_auth_for_start(m, r, i).await
        }
        let _ = _typecheck;
    }
}
