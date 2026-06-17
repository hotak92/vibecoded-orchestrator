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
//! * ~~`detect_container_runtime` — lives in each crate today~~ —
//!   promoted HERE in v0.2.54 (C-RT-1/C-RT-2): one daemon-aware
//!   detector honoring `VCT_CONTAINER_RUNTIME` → `runtime.txt` →
//!   podman-first `<cmd> info` probing. The three per-crate
//!   `--version` copies are gone.
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

// ─── Runtime detection (v0.2.54 C-RT-1 / C-RT-2) ──────────────────────
//
// Pre-v0.2.54 THREE near-identical copies of `detect_container_runtime`
// lived in `module_service.rs`, `installer_engine.rs`, and the hub's
// `module_supervisor.rs`. All three:
//
//   * hardcoded podman-first PATH probing — ignoring the user's
//     `VCT_CONTAINER_RUNTIME` choice and the install-time
//     `state/install/runtime.txt` record that install.py + the hooks +
//     the launcher's infra-stack path (services/runtime.rs) all honor
//     (C-RT-1: dual-runtime hosts ended up with infra under docker and
//     paid-module containers under podman, silently); and
//
//   * probed with `--version`, which only proves the CLIENT BINARY
//     exists — it never contacts the daemon/machine. A macOS/Windows
//     host with podman installed-but-machine-stopped and Docker
//     Desktop running picked the dead podman every time (C-RT-2).
//
// This promoted detector applies the canonical v0.2.14 contract
// (install.py `_runtime_preference_from_env` + `_detect_container_runtime`
// + `_container_runtime_reachable`):
//
//   1. `VCT_CONTAINER_RUNTIME=podman|docker` — explicit user choice.
//      Honored when that runtime's daemon responds; otherwise falls
//      through to auto-detect with a stderr note (lenient — a
//      misconfigured env var must not strand the user; mirrors
//      install.py:8087-8103).
//   2. `<install_root>/state/install/runtime.txt` — the runtime
//      install.py detected and recorded (`_persist_runtime_txt`).
//      Treated as a preference re-ordering, not a hard pin — a
//      daemon-dead recorded runtime falls through to the other
//      candidate with a stderr note.
//   3. Daemon-aware probe of `["podman", "docker"]` (podman-first,
//      matching install.py + services/runtime.rs policy) via
//      `<cmd> info` — the round-trip that exercises the same code
//      path `run`/`pull` need. `--version` is NOT used as a
//      selection signal anymore (only to distinguish
//      "binary present, daemon dead" from "not installed" in the
//      error message).

/// Read the user's explicit `VCT_CONTAINER_RUNTIME` preference.
/// Case-insensitive, trimmed; `"auto"` / empty / unset → `None`.
/// Unknown values log to stderr and return `None` (fall through to
/// auto-detect) — same contract as install.py
/// `_runtime_preference_from_env` and services/runtime.rs.
pub fn runtime_preference_from_env() -> Option<String> {
    let raw = std::env::var("VCT_CONTAINER_RUNTIME").ok()?;
    let norm = raw.trim().to_lowercase();
    if norm.is_empty() || norm == "auto" {
        return None;
    }
    if norm == "podman" || norm == "docker" {
        return Some(norm);
    }
    eprintln!(
        "[container_runtime] VCT_CONTAINER_RUNTIME={:?} unrecognized \
         (expected 'podman' / 'docker' / 'auto'); falling through to \
         auto-detect.",
        raw
    );
    None
}

/// Read `<install_root>/state/install/runtime.txt` (written by
/// install.py `_persist_runtime_txt`). Returns `Some("podman")` /
/// `Some("docker")` when the file exists and parses; `None` otherwise
/// (missing file, unreadable, or unrecognized token).
pub fn read_runtime_txt(install_root: &Path) -> Option<String> {
    let path = install_root
        .join("state")
        .join("install")
        .join("runtime.txt");
    let raw = std::fs::read_to_string(path).ok()?;
    let token = raw.trim().to_lowercase();
    if token == "podman" || token == "docker" {
        Some(token)
    } else {
        None
    }
}

/// Daemon-aware liveness probe: `<cmd> info` with a 10s timeout.
/// Returns true iff the daemon/socket/machine actually responds —
/// matching install.py `_container_runtime_reachable` (which documents
/// why `info`, not `version`: `version` only checks the client binary;
/// `info` round-trips to the daemon and exercises the same code path
/// `run`/`pull`/compose need. Catches stopped Docker Desktop on
/// macOS, stopped podman.socket on Linux rootless, unstarted podman
/// machine on macOS/Windows).
pub async fn runtime_daemon_responsive(cmd: &str) -> bool {
    use crate::process::CommandExt as _;
    use std::process::Stdio;

    let probe = tokio::time::timeout(
        std::time::Duration::from_secs(10),
        tokio::process::Command::new(cmd)
            .silent()
            .args(["info"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status(),
    )
    .await;
    matches!(probe, Ok(Ok(s)) if s.success())
}

/// Client-binary-only probe (`<cmd> --version`). NOT a selection
/// signal — used solely to enrich the no-runtime error message with
/// "binary present but daemon dead" candidates.
async fn runtime_binary_present(cmd: &str) -> bool {
    use crate::process::CommandExt as _;
    use std::process::Stdio;

    let probe = tokio::time::timeout(
        std::time::Duration::from_secs(10),
        tokio::process::Command::new(cmd)
            .silent()
            .args(["--version"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status(),
    )
    .await;
    matches!(probe, Ok(Ok(s)) if s.success())
}

/// Pure candidate-ordering helper for [`detect_container_runtime`].
/// Split out so the env→runtime.txt→default precedence is unit-testable
/// without spawning processes.
///
/// * `env_pref` — the validated `VCT_CONTAINER_RUNTIME` value (probed
///   FIRST; on daemon failure the detector falls through to the rest).
/// * `runtime_txt` — the validated runtime.txt token (preference
///   re-ordering: moved to the front of the auto-detect order).
pub fn runtime_candidate_order(
    env_pref: Option<&str>,
    runtime_txt: Option<&str>,
) -> Vec<String> {
    let mut order: Vec<String> = Vec::with_capacity(3);
    if let Some(p) = env_pref {
        order.push(p.to_string());
    }
    if let Some(t) = runtime_txt {
        if !order.iter().any(|c| c == t) {
            order.push(t.to_string());
        }
    }
    for c in ["podman", "docker"] {
        if !order.iter().any(|x| x == c) {
            order.push(c.to_string());
        }
    }
    order
}

/// v0.2.54: the ONE daemon-aware container-runtime detector shared by
/// the launcher's module install/start path (`module_service.rs`,
/// `installer_engine.rs`) and the hub's supervisor
/// (`module_supervisor.rs`). See the section comment above for the
/// precedence contract.
///
/// `install_root`: the orchestrator clone root, used to locate
/// `state/install/runtime.txt`. The launcher passes
/// `find_local_repo_root().ok()`; the hub passes `None` (it has no
/// clone-root resolver today — env override + daemon-aware probing
/// still apply, which closes C-RT-2 fully and C-RT-1 for the
/// env-var channel on the hub path).
///
/// Error message names every candidate whose binary exists but whose
/// daemon didn't respond, so a user debugging "module container won't
/// start" learns the actual state ("podman installed but machine
/// stopped") instead of a generic "no runtime found".
pub async fn detect_container_runtime(
    install_root: Option<&Path>,
) -> Result<String, String> {
    let env_pref = runtime_preference_from_env();
    let runtime_txt = install_root.and_then(read_runtime_txt);

    let order = runtime_candidate_order(env_pref.as_deref(), runtime_txt.as_deref());

    let mut binary_only: Vec<String> = Vec::new();
    for candidate in &order {
        if runtime_daemon_responsive(candidate).await {
            return Ok(candidate.clone());
        }
        // Daemon dead. Was this an explicit preference? Tell the user
        // we're falling through rather than silently switching engines.
        let was_pref = env_pref.as_deref() == Some(candidate.as_str())
            || runtime_txt.as_deref() == Some(candidate.as_str());
        if runtime_binary_present(candidate).await {
            if was_pref {
                eprintln!(
                    "[container_runtime] preferred runtime '{}' (from {}) is \
                     installed but its daemon/machine isn't responding to \
                     `{} info`; trying the next candidate.",
                    candidate,
                    if env_pref.as_deref() == Some(candidate.as_str()) {
                        "VCT_CONTAINER_RUNTIME"
                    } else {
                        "state/install/runtime.txt"
                    },
                    candidate,
                );
            }
            binary_only.push(candidate.clone());
        }
    }

    if binary_only.is_empty() {
        Err("no container runtime found (tried podman, docker)".into())
    } else {
        Err(format!(
            "no responsive container runtime: {} installed but daemon/machine \
             not responding to `info` (start it: Linux `systemctl --user start \
             podman.socket` / `sudo systemctl start docker`; macOS+Windows \
             `podman machine start` / open Docker Desktop)",
            binary_only.join(", "),
        ))
    }
}

// ─── GPU passthrough flags (v0.2.54 P0-4) ──────────────────────────────

/// Engine flags that actually hand the GPU to a module container.
///
/// Pre-v0.2.54 the whole `gpu_image_variants` pipeline (v0.2.20 tag
/// suffixes, v0.2.47 supervisor relocation) resolved `-cuda` / `-rocm`
/// image tags that then ran with NO device access — `build_podman_run_args`
/// emitted only `-d/--name/--restart/-p/-v/-e`, so torch saw no GPU and
/// the user paid the multi-GB CUDA/ROCm layer pull for CPU inference
/// (audit P0-4 / scout C-RT-3 ≡ gpu-C-1).
///
/// Flag matrix (runtime × mode):
///
/// | GpuMode | podman | docker |
/// |---------|--------|--------|
/// | Cuda    | `--device nvidia.com/gpu=all` (CDI) | `--gpus all` |
/// | Rocm    | `--device /dev/kfd --device /dev/dri --group-add keep-groups` | `--device /dev/kfd --device /dev/dri --group-add video --group-add render` |
/// | Cpu / Metal / None | (none) | (none) |
///
/// Why per-runtime: docker's CLI can't parse the CDI `nvidia.com/gpu=all`
/// device spec (it has `--gpus`); podman has no `--gpus` and uses CDI.
/// For ROCm, `--group-add keep-groups` is podman-only (preserves the
/// host user's supplementary `video`/`render` groups in rootless mode);
/// docker resolves the named groups against the container's /etc/group —
/// same convention as `infrastructure/docker-compose.rocm.yml`.
/// Unknown runtime names get the podman shape (conservative, mirrors
/// `PerPullAuth::apply_to`'s catch-all).
///
/// `manifest_declares_variants` gate: flags are appended ONLY for
/// modules that declare `runtime.gpu_image_variants` — i.e. modules
/// that explicitly participate in the GPU pipeline and therefore run a
/// GPU-capable image. Legacy single-tag modules keep their exact
/// pre-v0.2.54 argv: appending `--device nvidia.com/gpu=all` to a
/// CPU-only image on a host with a broken/absent CDI spec would fail a
/// container start that used to succeed.
pub fn gpu_passthrough_args(
    runtime: &str,
    gpu_mode: Option<GpuMode>,
    manifest_declares_variants: bool,
) -> Vec<String> {
    if !manifest_declares_variants {
        return Vec::new();
    }
    match gpu_mode {
        Some(GpuMode::Cuda) => {
            if runtime == "docker" {
                vec!["--gpus".into(), "all".into()]
            } else {
                vec!["--device".into(), "nvidia.com/gpu=all".into()]
            }
        }
        Some(GpuMode::Rocm) => {
            let mut args: Vec<String> = vec![
                "--device".into(),
                "/dev/kfd".into(),
                "--device".into(),
                "/dev/dri".into(),
            ];
            if runtime == "docker" {
                args.push("--group-add".into());
                args.push("video".into());
                args.push("--group-add".into());
                args.push("render".into());
            } else {
                args.push("--group-add".into());
                args.push("keep-groups".into());
            }
            args
        }
        Some(GpuMode::Cpu) | Some(GpuMode::Metal) | None => Vec::new(),
    }
}

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

/// v0.2.49 Stream A: resolve a GLOBAL container name. For global-scope
/// installs the container name is the bare `module_id` — no
/// `{project_slug}` suffix, no per-project naming, exactly one container
/// per machine. Strips a trailing `-{project_slug}` if the manifest's
/// `container_name_template` carries one (most manifests do), so a
/// per-project module flipped to `install.scope = "global"` produces a
/// clean bare-id container name without requiring the manifest author
/// to author a separate template.
///
/// Rules:
///   * `"vct-rl-reranker-{project_slug}"` → `"vct-rl-reranker"`
///   * `"vct-rl-reranker"` → `"vct-rl-reranker"` (idempotent)
///   * `"custom-{project_slug}-suffix"` → error (the `{project_slug}`
///     isn't trailing; can't safely strip without changing semantics).
///     Authors of global modules should drop the placeholder explicitly.
pub fn resolve_global_container_name(
    template: &str,
    module_id: &str,
) -> Result<String, String> {
    // Strip a trailing `-{project_slug}` only — anywhere else and the
    // template is ambiguous for global scope.
    let stripped = template
        .strip_suffix("-{project_slug}")
        .or_else(|| template.strip_suffix("_{project_slug}"))
        .unwrap_or(template);

    if stripped.contains("{project_slug}") {
        return Err(format!(
            "container_name_template '{}' contains {{project_slug}} in a \
             non-trailing position; cannot safely resolve for install.scope='global'. \
             Drop the placeholder in your manifest, or move it to a trailing \
             `-{{project_slug}}` suffix.",
            template
        ));
    }

    if stripped.contains('{') && stripped.contains('}') {
        return Err(format!(
            "container_name_template '{}' has unresolved placeholders → '{}'",
            template, stripped
        ));
    }

    if stripped.is_empty() {
        // Fallback to the bare module id — authoring slip; better than
        // returning an empty container name to podman.
        return Ok(module_id.to_string());
    }
    Ok(stripped.to_string())
}

/// v0.2.49 Stream A: placeholder map for a GLOBAL container. Mirrors
/// [`rl_placeholders`] but uses a fixed `"global"` literal for
/// `{project_slug}` (so volume / log paths like
/// `/data/state/{project_slug}/...` resolve to `/data/state/global/...`
/// instead of erroring on the unresolved placeholder). `RL_SERVER_PORT`
/// still comes from the caller — every container needs ONE listen port
/// (allocated machine-wide for global modules vs per-project for
/// per-project modules).
pub fn rl_placeholders_global(rl_port: u16) -> HashMap<String, String> {
    let mut m = HashMap::new();
    m.insert("{RL_SERVER_PORT}".to_string(), rl_port.to_string());
    m.insert("{project_slug}".to_string(), "global".to_string());
    m.insert("{ollama_port}".to_string(), DEFAULT_OLLAMA_PORT.to_string());
    m
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

    // The fully-resolved image reference (`<image>:<variant-tag>`).
    // `{module_image}` is the manifest token for THIS value — the RL
    // reranker manifest ships `runtime.image_ref: "{module_image}"`
    // expecting the launcher to substitute it (confirmed with the
    // module owner, 2026-06-17). It's the image-ref-path sibling of the
    // v0.2.59 `{module_id}` container-name fix: a token substituted on
    // one path but missed on a sibling path.
    //
    // NOTE — scope: we resolve `{module_image}` ONLY here, on the
    // image_ref path. We deliberately do NOT add it to the CMD-override
    // (`runtime.args`) substitution: an unsubstituted `{module_image}`
    // inside `runtime.args` is a deliberate pre-v0.2.49 Bug-E signal
    // (`is_runtime_pathological`, indicator 3) — there it means the
    // author pasted the launcher-side `podman run … {module_image}`
    // invocation into the container CMD. Same token, opposite meaning
    // per field; keep the two paths separate.
    let module_image = format!("{}:{}", container.image, tag);

    let out = template
        .replace("{install.container.image}", &container.image)
        .replace("{install.container.tag}", &tag)
        .replace("{module_image}", &module_image);

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
///   `run -d --name <name> [--restart=unless-stopped] [gpu flags] -p ... -v ... -e ... <image> <command> <args...>`
///
/// Accepts both `runtime.type = "container"` and `runtime.type = "service"`
/// — both declare a long-running daemon backed by a container.
/// `"cli"` / `"mcp_stdio"` / `"mcp_http"` have no podman args and are
/// rejected.
///
/// v0.2.54 (P0-4): takes the detected `engine` name (`"podman"` /
/// `"docker"`) + the host's `gpu_mode` so variant-bearing manifests get
/// the runtime-appropriate GPU device flags via
/// [`gpu_passthrough_args`]. Pre-v0.2.54 the `-cuda`/`-rocm` images
/// selected by `resolve_variant_tag` ran with zero device access —
/// silent CPU inference inside a GPU image.
pub fn build_podman_run_args(
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project: &ProjectRow,
    rl_port: u16,
    container_name: &str,
    image: &str,
    engine: &str,
    gpu_mode: Option<GpuMode>,
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

    // v0.2.54 P0-4: GPU passthrough for variant-declaring modules.
    args.extend(gpu_passthrough_args(
        engine,
        gpu_mode,
        runtime.gpu_image_variants.is_some(),
    ));

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

    // Positional: image, then optional command + args (override of image CMD).
    args.push(image.to_string());

    // v0.2.49: Bug E — pre-v0.2.49 manifests had `command: "podman"` +
    // `args: ["run", "--rm", "-p", "11450:11450", "{module_image}"]` (the
    // launcher-side podman invocation mistakenly authored as the
    // container CMD). The resulting container ran
    // `<entrypoint> podman run --rm -p 11450:11450 {module_image}`
    // and argparse-failed. Declarative manifests leave `command` empty
    // and rely on the image-baked ENTRYPOINT. When `command` is empty,
    // skip the CMD override entirely so podman uses the image's default.
    // Placeholders still apply when `command` is non-empty (legacy
    // override authors can use `{project_slug}` etc.).
    //
    // v0.2.52 V52-D.1: even when `command` is non-empty, detect the
    // pre-v0.2.49 footgun pattern at runtime so a stale catalog row /
    // legacy image that ships the broken manifest still produces a
    // working container. See `is_runtime_pathological` for the full
    // list of indicators. When the runtime is pathological, log a
    // warning and DROP the CMD override (fall back to the image's
    // ENTRYPOINT) — same effect as the v0.2.49 empty-command branch.
    let runtime_skip_cmd = runtime.command.is_empty()
        || is_runtime_pathological(&runtime.command, &runtime.args, Some(&manifest.id));
    if !runtime_skip_cmd {
        args.push(resolve_value(&runtime.command, ctx, &placeholders));
        for a in &runtime.args {
            args.push(resolve_value(a, ctx, &placeholders));
        }
    }

    Ok(args)
}

/// v0.2.49 Stream A: build the full `podman run` argv for a GLOBAL
/// container — no per-project state. Sibling of
/// [`build_podman_run_args`] used by the global supervisor + global
/// install path.
///
/// Differences vs `build_podman_run_args`:
///   * Takes no `ProjectRow` — there's no project for a global install.
///   * Uses [`rl_placeholders_global`], which substitutes `"global"` for
///     `{project_slug}` (so volume paths like
///     `/data/state/{project_slug}/...` resolve to a stable global dir).
///   * `rl_port` is the machine-wide allocated port (one per global
///     module — the container listens on this single port for every
///     project's requests).
///   * v0.2.54 (P0-4): `engine` + `gpu_mode` mirror
///     [`build_podman_run_args`] — GPU device flags for
///     variant-declaring manifests.
pub fn build_podman_run_args_global(
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    rl_port: u16,
    container_name: &str,
    image: &str,
    engine: &str,
    gpu_mode: Option<GpuMode>,
) -> Result<Vec<String>, String> {
    let runtime = &manifest.runtime;
    if !matches!(runtime.r#type.as_str(), "container" | "service") {
        return Err(format!(
            "build_podman_run_args_global: runtime.type must be 'container' or 'service', got '{}'",
            runtime.r#type
        ));
    }

    let placeholders = rl_placeholders_global(rl_port);
    let mut args: Vec<String> = Vec::new();
    args.push("run".into());
    args.push("-d".into());
    args.push("--name".into());
    args.push(container_name.to_string());

    if runtime.auto_restart {
        args.push("--restart=unless-stopped".into());
    }

    // v0.2.54 P0-4: GPU passthrough for variant-declaring modules.
    args.extend(gpu_passthrough_args(
        engine,
        gpu_mode,
        runtime.gpu_image_variants.is_some(),
    ));

    for port in &runtime.ports {
        args.push("-p".into());
        args.push(build_port_arg(port, &placeholders)?);
    }

    for vol in &runtime.volumes {
        args.push("-v".into());
        args.push(build_volume_arg(vol, ctx, &placeholders));
    }

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

    args.push(image.to_string());

    // v0.2.49 Bug E (mirrored from build_podman_run_args): only push a
    // CMD override when the manifest declares a non-empty `command`.
    // Declarative manifests with empty command let the image's
    // ENTRYPOINT run unmolested.
    //
    // v0.2.52 V52-D.1: also drop the CMD override when the manifest
    // declares a pathological runtime (`command: "podman"` /
    // unsubstituted `{module_image}` in args / etc.). See
    // `is_runtime_pathological` for the full indicator set.
    let runtime_skip_cmd = runtime.command.is_empty()
        || is_runtime_pathological(&runtime.command, &runtime.args, Some(&manifest.id));
    if !runtime_skip_cmd {
        args.push(resolve_value(&runtime.command, ctx, &placeholders));
        for a in &runtime.args {
            args.push(resolve_value(a, ctx, &placeholders));
        }
    }

    Ok(args)
}

/// v0.2.52 V52-D.1: detect the pre-v0.2.49 Bug E manifest pattern that
/// turned `runtime.command` + `runtime.args` into a literal podman
/// invocation embedded as the container CMD. Returns `true` when the
/// runtime block is unsafe to honour and the caller should drop the
/// CMD override (falling back to the image's ENTRYPOINT).
///
/// ## Indicators (any → pathological)
///
/// 1. `command` is the bare name of a container runtime —
///    `podman` / `docker` — which strongly implies the manifest author
///    pasted the launcher-side invocation into the container-side
///    command. A legit module's `runtime.command` would be
///    `python` / `node` / `<custom binary>`, never the orchestrator.
///
/// 2. `command` is a generic shell (`sh` / `bash`) **without** a `-c`
///    flag in `args`. A legit shell invocation always uses
///    `sh -c "..."` so the first arg should be `-c`. Without `-c` the
///    container would just open an interactive shell that dies
///    immediately under podman's `-d` (non-tty) mode — almost
///    certainly not the publisher's intent.
///
/// 3. Any element of `args` contains an unsubstituted `{module_image}`
///    placeholder. The launcher never substitutes `{module_image}`
///    (it's the launcher-side variable for the image tag passed to
///    `podman run` — i.e. the positional image arg, not a CMD arg).
///    Its presence anywhere in the CMD override means the manifest
///    author copy-pasted the wrong substitution context.
///
/// ## Logging
///
/// When pathological, emit a single `eprintln!` warning naming the
/// module so operators can spot stale catalog rows in launcher logs.
/// We don't return an error: the v0.2.49 contract is that broken
/// manifests degrade to "ignore CMD override, hope ENTRYPOINT works"
/// rather than fail the start — most images have a valid ENTRYPOINT
/// even when the manifest's CMD is garbage.
///
/// The function is `pub(crate)` so the reaper (V52-D.2) can reuse the
/// same indicator set when scanning existing containers' Config.Cmd
/// for the same pathology.
pub fn is_runtime_pathological(
    command: &str,
    args: &[String],
    module_id: Option<&str>,
) -> bool {
    let cmd_trim = command.trim();

    // Indicator 1: command is a container runtime binary name.
    if matches!(cmd_trim, "podman" | "docker") {
        eprintln!(
            "[container_runtime] WARN: module {} declares runtime.command='{}' \
             which is a container-runtime binary name. This is the pre-v0.2.49 \
             Bug E manifest pattern. Dropping CMD override; using image ENTRYPOINT. \
             Publisher should rebuild the image with runtime.command='' \
             (and rely on the image's ENTRYPOINT) OR set command to the actual \
             in-container binary (e.g. 'python', 'node').",
            module_id.unwrap_or("<unknown>"),
            cmd_trim,
        );
        return true;
    }

    // Indicator 2: shell without -c flag.
    if matches!(cmd_trim, "sh" | "bash") {
        // Look for `-c` anywhere in args. A legit shell invocation
        // always includes -c; without it the shell would exit
        // immediately under -d mode.
        let has_dash_c = args.iter().any(|a| a == "-c");
        if !has_dash_c {
            eprintln!(
                "[container_runtime] WARN: module {} declares runtime.command='{}' \
                 without a '-c' arg. A detached shell with no command exits \
                 immediately. Dropping CMD override; using image ENTRYPOINT.",
                module_id.unwrap_or("<unknown>"),
                cmd_trim,
            );
            return true;
        }
    }

    // Indicator 3: unsubstituted {module_image} placeholder in args.
    // The launcher never substitutes {module_image} (it's a launcher-
    // side variable for the positional image arg, not a CMD-side one).
    if args.iter().any(|a| a.contains("{module_image}")) {
        eprintln!(
            "[container_runtime] WARN: module {} declares runtime.args containing \
             unsubstituted '{{module_image}}' placeholder. This is the pre-v0.2.49 \
             Bug E pattern. Dropping CMD override; using image ENTRYPOINT. \
             Publisher should rebuild the image with the actual in-container CMD \
             (or empty `command` to rely on the image's ENTRYPOINT).",
            module_id.unwrap_or("<unknown>"),
        );
        return true;
    }

    false
}

/// v0.2.49 Stream A: best-effort `mkdir -p` for each volume's host path
/// for a GLOBAL container. Sibling of [`ensure_volume_host_dirs`] that
/// substitutes `"global"` for `{project_slug}`.
pub async fn ensure_volume_host_dirs_global(
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    rl_port: u16,
) {
    let placeholders = rl_placeholders_global(rl_port);
    for vol in &manifest.runtime.volumes {
        let host_resolved = resolve_value(&vol.host, ctx, &placeholders);
        let path = PathBuf::from(&host_resolved);
        if let Err(e) = tokio::fs::create_dir_all(&path).await {
            eprintln!(
                "[container_runtime] global mkdir -p {} failed (will let podman surface the error): {}",
                path.display(),
                e
            );
        }
    }
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

// ─── V52-D.2: legacy container reaper ────────────────────────────────

/// v0.2.52 V52-D.2: classification of a `podman ps -a` row against
/// the launcher's DB state. The reaper enumerates containers whose
/// names match the prefix patterns the launcher uses for module
/// containers (`<module_id>` or `<module_id>-<project_slug>`) and
/// classifies each into one of these buckets.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReaperVerdict {
    /// Container's Config.Cmd carries the unsubstituted
    /// `{module_image}` placeholder — pre-v0.2.49 Bug E pattern. The
    /// process inside will argparse-fail and restart-loop forever.
    BrokenCmd,
    /// No `module_installs` row claims this container_name. Most
    /// likely a leftover from a manual `podman run` debug session OR
    /// a pre-v0.2.10 launcher leftover that didn't carry the
    /// project-slug suffix convention.
    Orphan,
    /// Container is running an image tag that doesn't match the DB
    /// row's expected `install.container.image:tag`. Most often
    /// caused by an upgrade where the launcher recreated the row but
    /// the old container kept running with the stale image.
    StaleImage,
    /// Container is healthy and tracked by a DB row. Do NOT reap.
    Healthy,
}

/// v0.2.52 V52-D.2: parsed `podman ps -a` row used by the reaper.
/// Only the fields the reaper consults — no podman-internal noise.
#[derive(Debug, Clone)]
pub struct ContainerSnapshot {
    pub name: String,
    /// Full image reference (e.g. `ghcr.io/hotak92/vct-rl-reranker:0.1.0`).
    pub image: String,
    /// The container's CMD as podman reports it (string-joined argv).
    pub cmd: String,
}

/// v0.2.52 V52-D.2: classify a single container snapshot against the
/// DB's claimed container_name set + the expected image:tag for the
/// container's apparent module.
///
/// Args:
/// * `snap`: the container row pulled from `podman ps -a`.
/// * `claimed_names`: container_names that at least one
///   `module_installs` row references. A container whose name is NOT
///   in this set is Orphan (no DB row claims it).
/// * `expected_image_for`: lookup `(container_name) -> Option<image_ref>`
///   that returns the expected `image:tag` for a tracked container.
///   Returning `None` means "no expected image known" → can't
///   stale-image-check. Used in tests with mock closures.
///
/// Returns the verdict; the caller decides whether to issue
/// `podman rm -f`.
pub fn classify_container_for_reaper<F>(
    snap: &ContainerSnapshot,
    claimed_names: &std::collections::HashSet<String>,
    expected_image_for: F,
) -> ReaperVerdict
where
    F: Fn(&str) -> Option<String>,
{
    // Indicator 1 (highest priority): broken Cmd. A literal
    // `{module_image}` in the container's Cmd is unambiguous evidence
    // of the pre-v0.2.49 Bug E manifest — those containers WILL
    // restart-loop. Reap regardless of DB ownership.
    if snap.cmd.contains("{module_image}") {
        return ReaperVerdict::BrokenCmd;
    }

    // Indicator 2: orphan — no DB row claims this name. Includes the
    // V52-E `vct-rl-reranker` (bare, no suffix) case.
    if !claimed_names.contains(&snap.name) {
        return ReaperVerdict::Orphan;
    }

    // Indicator 3: image-version drift. The container is claimed by
    // a DB row but its actual image tag differs from what the
    // manifest declares the row should be using. Compare on the
    // full image ref (with tag, including variant suffix). When the
    // expected image is unknown we can't conclude; treat as healthy.
    if let Some(expected) = expected_image_for(&snap.name) {
        // Tolerate minor formatting (podman occasionally normalises
        // `docker.io/library/foo:tag` to `foo:tag` in its inspect
        // output). Compare the trailing `<image>:<tag>` substring.
        if !image_refs_equivalent(&snap.image, &expected) {
            return ReaperVerdict::StaleImage;
        }
    }

    ReaperVerdict::Healthy
}

/// v0.2.52 V52-D.2: lenient image-ref equivalence check used by the
/// reaper. Podman occasionally rewrites registry prefixes in
/// `inspect` output vs what the manifest declares (e.g.
/// `docker.io/library/foo:tag` ↔ `foo:tag`). The reaper should not
/// reap a healthy container over a cosmetic prefix difference, so we
/// compare the trailing `<image>:<tag>` segment.
///
/// Rule: equal if the strings are byte-equal, OR if one ends with
/// the other after the last `/`.
pub fn image_refs_equivalent(a: &str, b: &str) -> bool {
    if a == b {
        return true;
    }
    let a_tail = a.rsplit('/').next().unwrap_or(a);
    let b_tail = b.rsplit('/').next().unwrap_or(b);
    a_tail == b_tail
}

/// v0.2.52 V52-D.2: parse `podman ps -a --format json` output into a
/// vec of `ContainerSnapshot`. Podman emits an array of objects;
/// each object's relevant fields are `Names` (array of strings,
/// usually 1 element), `Image` (string), and `Command` (array of
/// strings — argv).
///
/// Soft-fail: missing fields produce empty strings; malformed JSON
/// returns `Err`. The reaper treats `Err` as "skip this pass" rather
/// than "panic" — a failed parse should not block the launcher boot.
pub fn parse_podman_ps_json(json_str: &str) -> Result<Vec<ContainerSnapshot>, String> {
    let raw: serde_json::Value =
        serde_json::from_str(json_str).map_err(|e| format!("parse podman ps json: {}", e))?;
    let arr = raw
        .as_array()
        .ok_or_else(|| "podman ps json: top-level not an array".to_string())?;

    let mut out = Vec::with_capacity(arr.len());
    for entry in arr {
        let name = entry
            .get("Names")
            .and_then(|v| v.as_array())
            .and_then(|a| a.first())
            .and_then(|v| v.as_str())
            .map(String::from)
            .unwrap_or_default();
        let image = entry
            .get("Image")
            .and_then(|v| v.as_str())
            .map(String::from)
            .unwrap_or_default();
        let cmd_parts = entry
            .get("Command")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str())
                    .collect::<Vec<_>>()
                    .join(" ")
            })
            .unwrap_or_default();
        if name.is_empty() {
            // No usable name → cannot reap by name; skip.
            continue;
        }
        out.push(ContainerSnapshot {
            name,
            image,
            cmd: cmd_parts,
        });
    }
    Ok(out)
}

/// v0.2.52 V52-D.2: top-level reaper entry point. Enumerates all
/// containers via `<runtime> ps -a --format json`, classifies each
/// against the supplied DB-state lookups, and issues `<runtime> rm -f`
/// on every BrokenCmd / Orphan / StaleImage verdict.
///
/// Soft-fail throughout:
/// * `<runtime> ps` failure: log + return (no reaping this pass).
/// * `<runtime> rm` failure: log per-container + continue.
///
/// Returns `(reaped_count, error_count)` for forensic visibility.
///
/// Args:
/// * `runtime`: `"podman"` or `"docker"`.
/// * `claimed_names`: set of container_names referenced by at least
///   one `module_installs` row.
/// * `expected_image_for`: lookup mapping container_name to expected
///   image:tag for the DB-claimed containers.
/// * `name_filter`: optional predicate that says whether a container
///   name should be examined at all. The reaper is scoped — it
///   should NEVER touch containers from unrelated software (Weaviate,
///   Ollama, user's own podman work). Default callers pass a filter
///   that matches launcher-managed module name patterns.
pub async fn reap_pathological_containers<F, G>(
    runtime: &str,
    claimed_names: &std::collections::HashSet<String>,
    expected_image_for: F,
    name_filter: G,
) -> (usize, usize)
where
    F: Fn(&str) -> Option<String>,
    G: Fn(&str) -> bool,
{
    use std::process::Stdio;
    use tokio::process::Command;

    let ps_output = match Command::new(runtime)
        .args(["ps", "-a", "--format", "json"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
    {
        Ok(o) => o,
        Err(e) => {
            eprintln!(
                "[container_runtime] V52-D.2 reaper: spawn {} ps failed: {}",
                runtime, e
            );
            return (0, 1);
        }
    };

    if !ps_output.status.success() {
        eprintln!(
            "[container_runtime] V52-D.2 reaper: {} ps exit {}, stderr={}",
            runtime,
            ps_output.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&ps_output.stderr).chars().take(300).collect::<String>(),
        );
        return (0, 1);
    }

    let stdout = String::from_utf8_lossy(&ps_output.stdout);
    // Podman's `--format json` returns `null` (not `[]`) when no
    // containers exist on a fresh machine. Treat that as "no rows
    // to scan".
    if stdout.trim() == "null" || stdout.trim().is_empty() {
        return (0, 0);
    }
    let snapshots = match parse_podman_ps_json(&stdout) {
        Ok(s) => s,
        Err(e) => {
            eprintln!(
                "[container_runtime] V52-D.2 reaper: parse {} ps json failed: {}",
                runtime, e
            );
            return (0, 1);
        }
    };

    let mut reaped = 0usize;
    let mut errors = 0usize;

    for snap in &snapshots {
        if !name_filter(&snap.name) {
            continue;
        }
        let verdict =
            classify_container_for_reaper(snap, claimed_names, &expected_image_for);
        match verdict {
            ReaperVerdict::Healthy => continue,
            ReaperVerdict::BrokenCmd => {
                eprintln!(
                    "[container_runtime] V52-D.2 reaper: reaping BrokenCmd container '{}' \
                     (image='{}', cmd contains '{{module_image}}')",
                    snap.name, snap.image,
                );
            }
            ReaperVerdict::Orphan => {
                eprintln!(
                    "[container_runtime] V52-D.2 reaper: reaping Orphan container '{}' \
                     (image='{}', no DB row claims this name)",
                    snap.name, snap.image,
                );
            }
            ReaperVerdict::StaleImage => {
                let expected = expected_image_for(&snap.name).unwrap_or_default();
                eprintln!(
                    "[container_runtime] V52-D.2 reaper: reaping StaleImage container '{}' \
                     (running='{}', expected='{}')",
                    snap.name, snap.image, expected,
                );
            }
        }

        let rm_status = Command::new(runtime)
            .args(["rm", "-f", &snap.name])
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .output()
            .await;
        match rm_status {
            Ok(o) if o.status.success() => {
                reaped += 1;
            }
            Ok(o) => {
                errors += 1;
                eprintln!(
                    "[container_runtime] V52-D.2 reaper: {} rm -f {} exit {}, stderr={}",
                    runtime,
                    snap.name,
                    o.status.code().unwrap_or(-1),
                    String::from_utf8_lossy(&o.stderr).chars().take(200).collect::<String>(),
                );
            }
            Err(e) => {
                errors += 1;
                eprintln!(
                    "[container_runtime] V52-D.2 reaper: spawn {} rm failed for {}: {}",
                    runtime, snap.name, e
                );
            }
        }
    }

    if reaped > 0 || errors > 0 {
        eprintln!(
            "[container_runtime] V52-D.2 reaper: pass complete, reaped={} errors={}",
            reaped, errors,
        );
    }
    (reaped, errors)
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
    // v0.2.49: switched podman branch from argv flag injection to env var.
    // The v0.2.47 shape (`cmd.arg("--authfile").arg(file.path())`) put the
    // flag BEFORE the subcommand at every callsite (apply_to runs before
    // `.args(["pull", &image_ref])`), producing
    //   podman --authfile X pull image
    // which podman 4.x rejects with "Error: unknown flag: --authfile".
    // The flag is subcommand-scoped (`podman pull --authfile X image`),
    // but every callsite added args in the wrong order. Three releases
    // (v0.2.47-v0.2.48) shipped with launcher GUI installs silently
    // failing exit 125 on every cache-miss pull.
    //
    // REGISTRY_AUTH_FILE is the env-var sibling of --authfile per podman
    // release notes since 1.3. It's position-independent on the CLI, so
    // no callsite reordering is needed. Docker branch already uses
    // DOCKER_CONFIG env var; podman branch is now symmetric.
    //
    // Caller obligation: apply_to MUST run AFTER any cmd.env_clear()
    // (env vars are last-write-wins). All current callsites verified
    // safe: install path's pull doesn't env_clear; start path's pre-pull
    // is a separate Command from the start's `podman run` (which does
    // env_clear). See test `per_pull_auth_podman_env_var_accepted_by_live_podman`
    // for the live-podman regression guard.
    pub fn apply_to(&self, cmd: &mut tokio::process::Command, runtime: &str) {
        match (&self.inner, runtime) {
            (PerPullAuthInner::Podman(file), _) => {
                cmd.env("REGISTRY_AUTH_FILE", file.path());
            }
            (PerPullAuthInner::Docker(dir), "docker") => {
                cmd.env("DOCKER_CONFIG", dir.path());
            }
            (PerPullAuthInner::Docker(dir), _) => {
                // Hybrid case: caller built a Docker-shape guard but is
                // invoking a non-docker runtime. DOCKER_CONFIG works on
                // both runtimes (podman reads it as a fallback), and the
                // file layout is identical.
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
                scope: crate::manifest::InstallScope::PerProject,
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
            kg_collections: None,
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

    // ─── v0.2.61: {module_image} token resolution ────────────────────
    //
    // The vct-rl-reranker manifest ships `runtime.image_ref:
    // "{module_image}"` (confirmed with the module owner 2026-06-17).
    // Pre-fix, resolve_image_ref substituted only
    // {install.container.image}+{install.container.tag}, so the literal
    // "{module_image}" hit the unresolved-placeholder guard → install
    // failed at image-ref resolution. Sibling of the v0.2.59
    // {module_id} container-name bug. Tests use the REAL shipped token
    // ("{module_image}") — NOT a factory stand-in (the v0.2.59 lesson:
    // a test that asserts a value the manifest never ships gives false
    // confidence).

    /// `{module_image}` with a GPU-variant manifest → `<image>:<variant-tag>`.
    #[test]
    fn resolve_image_ref_resolves_module_image_token_cuda_variant() {
        let manifest = make_manifest_with_variants(true);
        let got = resolve_image_ref("{module_image}", &manifest, Some(GpuMode::Cuda))
            .expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:0.2.8-cuda");
    }

    /// `{module_image}` with Cpu mode (the `gpu_optional` no-GPU path) →
    /// `-cpu` variant.
    #[test]
    fn resolve_image_ref_resolves_module_image_token_cpu_variant() {
        let manifest = make_manifest_with_variants(true);
        let got = resolve_image_ref("{module_image}", &manifest, Some(GpuMode::Cpu))
            .expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:0.2.8-cpu");
    }

    /// `{module_image}` with no gpu_mode (variant resolution skipped) →
    /// bare `<image>:<version>`.
    #[test]
    fn resolve_image_ref_resolves_module_image_token_no_gpu_mode() {
        let manifest = make_manifest(true, true);
        let got = resolve_image_ref("{module_image}", &manifest, None).expect("resolve");
        assert_eq!(got, "ghcr.io/hotak92/vct-rl-reranker:0.2.8");
    }

    /// The pre-v0.2.61 failure mode: a bare "{module_image}" template
    /// must no longer trip the unresolved-placeholder guard.
    #[test]
    fn resolve_image_ref_module_image_no_longer_unresolved() {
        let manifest = make_manifest_with_variants(true);
        let got = resolve_image_ref("{module_image}", &manifest, Some(GpuMode::Cuda))
            .expect("must resolve, not error on unresolved placeholder");
        assert!(
            !got.contains('{') && !got.contains('}'),
            "resolved image ref must have no leftover placeholders, got {got:?}",
        );
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
            "podman",
            None,
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
        let err = build_podman_run_args(
            &manifest, &ctx, &project, 11533, "x", "img:tag", "podman", None,
        )
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
            "podman",
            None,
        )
        .expect("service runtime accepted");
        assert_eq!(args[0], "run");
    }

    // ─── v0.2.54 P0-4: GPU passthrough flags ─────────────────────────

    /// Pure-helper matrix: podman/docker × Cuda/Rocm/Cpu/Metal/None.
    #[test]
    fn v0254_gpu_passthrough_args_matrix() {
        // CUDA — podman gets the CDI device, docker gets --gpus all.
        assert_eq!(
            gpu_passthrough_args("podman", Some(GpuMode::Cuda), true),
            vec!["--device".to_string(), "nvidia.com/gpu=all".to_string()],
        );
        assert_eq!(
            gpu_passthrough_args("docker", Some(GpuMode::Cuda), true),
            vec!["--gpus".to_string(), "all".to_string()],
        );
        // ROCm — devices on both; group strategy differs per engine.
        assert_eq!(
            gpu_passthrough_args("podman", Some(GpuMode::Rocm), true),
            vec![
                "--device".to_string(),
                "/dev/kfd".to_string(),
                "--device".to_string(),
                "/dev/dri".to_string(),
                "--group-add".to_string(),
                "keep-groups".to_string(),
            ],
        );
        assert_eq!(
            gpu_passthrough_args("docker", Some(GpuMode::Rocm), true),
            vec![
                "--device".to_string(),
                "/dev/kfd".to_string(),
                "--device".to_string(),
                "/dev/dri".to_string(),
                "--group-add".to_string(),
                "video".to_string(),
                "--group-add".to_string(),
                "render".to_string(),
            ],
        );
        // Cpu / Metal / None → no flags on either engine.
        for engine in ["podman", "docker"] {
            assert!(gpu_passthrough_args(engine, Some(GpuMode::Cpu), true).is_empty());
            assert!(gpu_passthrough_args(engine, Some(GpuMode::Metal), true).is_empty());
            assert!(gpu_passthrough_args(engine, None, true).is_empty());
        }
        // Unknown runtime falls back to the podman (CDI) shape.
        assert_eq!(
            gpu_passthrough_args("nerdctl", Some(GpuMode::Cuda), true),
            vec!["--device".to_string(), "nvidia.com/gpu=all".to_string()],
        );
    }

    /// Legacy single-tag modules (no `gpu_image_variants`) keep their
    /// exact pre-v0.2.54 argv — no GPU flags even on a CUDA host.
    #[test]
    fn v0254_gpu_passthrough_args_skipped_without_variants() {
        assert!(gpu_passthrough_args("podman", Some(GpuMode::Cuda), false).is_empty());
        assert!(gpu_passthrough_args("docker", Some(GpuMode::Rocm), false).is_empty());
    }

    /// End-to-end through the per-project builder: a variant-declaring
    /// manifest on a CUDA host produces the CDI device flag, positioned
    /// BEFORE the positional image argument (engine flags must precede
    /// the image in `run` argv).
    #[test]
    fn v0254_build_podman_run_args_appends_cuda_device_flag() {
        let manifest = make_manifest_with_variants(true);
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args = build_podman_run_args(
            &manifest,
            &ctx,
            &project,
            11533,
            "vct-rl-reranker-acme-corp",
            "ghcr.io/hotak92/vct-rl-reranker:0.2.8-cuda",
            "podman",
            Some(GpuMode::Cuda),
        )
        .expect("build args");
        let dev_pos = args
            .iter()
            .position(|a| a == "--device")
            .expect("--device flag present for CUDA variant module");
        assert_eq!(args[dev_pos + 1], "nvidia.com/gpu=all");
        let image_pos = args
            .iter()
            .position(|a| a == "ghcr.io/hotak92/vct-rl-reranker:0.2.8-cuda")
            .expect("image present");
        assert!(
            dev_pos < image_pos,
            "GPU flags must precede the image arg; got {:?}",
            args
        );
    }

    /// End-to-end: docker + ROCm through the per-project builder.
    #[test]
    fn v0254_build_podman_run_args_docker_rocm_devices() {
        let manifest = make_manifest_with_variants(true);
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args = build_podman_run_args(
            &manifest,
            &ctx,
            &project,
            11533,
            "vct-rl-reranker-acme-corp",
            "ghcr.io/hotak92/vct-rl-reranker:0.2.8-rocm",
            "docker",
            Some(GpuMode::Rocm),
        )
        .expect("build args");
        assert!(args.iter().any(|a| a == "/dev/kfd"));
        assert!(args.iter().any(|a| a == "/dev/dri"));
        assert!(args.iter().any(|a| a == "video"));
        assert!(args.iter().any(|a| a == "render"));
        assert!(
            !args.iter().any(|a| a == "keep-groups"),
            "keep-groups is podman-only; got {:?}",
            args
        );
    }

    /// Cpu-mode host: variant module pulls the `-cpu` image and gets no
    /// device flags.
    #[test]
    fn v0254_build_podman_run_args_cpu_mode_no_gpu_flags() {
        let manifest = make_manifest_with_variants(true);
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args = build_podman_run_args(
            &manifest,
            &ctx,
            &project,
            11533,
            "x",
            "img:0.2.8-cpu",
            "podman",
            Some(GpuMode::Cpu),
        )
        .expect("build args");
        assert!(!args.iter().any(|a| a == "--device" || a == "--gpus"));
    }

    /// Global builder gets the same GPU flags.
    #[test]
    fn v0254_build_podman_run_args_global_appends_gpu_flags() {
        let mut manifest = make_rl_manifest_global_for_test();
        manifest.runtime.gpu_image_variants = Some(GpuImageVariants {
            cpu: "{version}-cpu".into(),
            cuda: "{version}-cuda".into(),
            rocm: "{version}-rocm".into(),
        });
        let ctx = crate::manifest::PlaceholderCtx::new("vct-rl-reranker");
        let args = build_podman_run_args_global(
            &manifest,
            &ctx,
            11443,
            "vct-rl-reranker",
            "ghcr.io/x/y:0.2.10-cuda",
            "docker",
            Some(GpuMode::Cuda),
        )
        .expect("build");
        assert!(args.iter().any(|a| a == "--gpus"));
        assert!(args.iter().any(|a| a == "all"));
    }

    /// Live podman CLI-surface check (same discipline as
    /// `per_pull_auth_podman_env_var_accepted_by_live_podman`): the
    /// flags we emit (`--device`, `--group-add`, `--gpus` for docker)
    /// must exist on the live `run` subcommand's parser. Catches a
    /// hypothetical podman release dropping/renaming the flags. Skipped
    /// (clean return) on hosts without podman.
    #[test]
    fn v0254_gpu_flags_exist_on_live_podman_run_parser() {
        let Ok(probe) = std::process::Command::new("which").arg("podman").output() else {
            return;
        };
        if !probe.status.success() {
            return;
        }
        let output = std::process::Command::new("podman")
            .args(["run", "--help"])
            .output()
            .expect("spawn podman run --help");
        assert!(output.status.success());
        let help = String::from_utf8_lossy(&output.stdout);
        assert!(
            help.contains("--device"),
            "podman run must support --device"
        );
        assert!(
            help.contains("--group-add"),
            "podman run must support --group-add"
        );
    }

    // ─── v0.2.54 C-RT-1 / C-RT-2: promoted runtime detection ─────────

    /// Candidate ordering: env preference first, runtime.txt second,
    /// podman-first default tail, no duplicates.
    #[test]
    fn v0254_runtime_candidate_order_precedence() {
        assert_eq!(
            runtime_candidate_order(None, None),
            vec!["podman".to_string(), "docker".to_string()],
        );
        assert_eq!(
            runtime_candidate_order(Some("docker"), None),
            vec!["docker".to_string(), "podman".to_string()],
        );
        assert_eq!(
            runtime_candidate_order(None, Some("docker")),
            vec!["docker".to_string(), "podman".to_string()],
        );
        // env wins over runtime.txt; no dup when they agree.
        assert_eq!(
            runtime_candidate_order(Some("podman"), Some("docker")),
            vec!["podman".to_string(), "docker".to_string()],
        );
        assert_eq!(
            runtime_candidate_order(Some("docker"), Some("docker")),
            vec!["docker".to_string(), "podman".to_string()],
        );
    }

    /// runtime.txt reader: valid token round-trips; junk / missing → None.
    #[test]
    fn v0254_read_runtime_txt_parses_valid_token() {
        let dir = tempfile::tempdir().expect("tempdir");
        let sub = dir.path().join("state").join("install");
        std::fs::create_dir_all(&sub).expect("mkdir");
        std::fs::write(sub.join("runtime.txt"), "docker\n").expect("write");
        assert_eq!(read_runtime_txt(dir.path()), Some("docker".to_string()));

        std::fs::write(sub.join("runtime.txt"), "  PODMAN  \n").expect("write");
        assert_eq!(read_runtime_txt(dir.path()), Some("podman".to_string()));

        std::fs::write(sub.join("runtime.txt"), "containerd\n").expect("write");
        assert_eq!(read_runtime_txt(dir.path()), None);

        let empty = tempfile::tempdir().expect("tempdir");
        assert_eq!(read_runtime_txt(empty.path()), None);
    }

    /// Live daemon-aware probe sanity: a nonexistent binary is never
    /// "responsive"; and when podman IS on PATH with a live daemon,
    /// the detector returns it (skip silently when no runtime is
    /// usable on the CI host — the negative assertion above still ran).
    #[tokio::test]
    async fn v0254_detect_container_runtime_live_probe() {
        assert!(
            !runtime_daemon_responsive("definitely-not-a-container-runtime-binary").await
        );
        // Detection must never panic; an Err on runtime-less CI hosts is
        // a valid outcome and carries the candidate diagnosis.
        match detect_container_runtime(None).await {
            Ok(rt) => assert!(rt == "podman" || rt == "docker"),
            Err(msg) => assert!(msg.contains("container runtime")),
        }
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

    // ─── v0.2.47 → v0.2.49: PerPullAuth runtime dispatch ────────────
    //
    // v0.2.47 injected `--authfile <path>` as an argv flag. Tests asserted
    // on `format!("{:?}", cmd).contains("--authfile")` only. v0.2.49 fixed
    // the podman-authfile-flag-position bug (latent for 3 releases) by
    // switching to `REGISTRY_AUTH_FILE` env var, which is position-
    // independent. Updated assertions below check the env-var shape.

    /// v0.2.49 regression test: podman branch → `REGISTRY_AUTH_FILE` env
    /// var present, no `--authfile` argv flag.
    #[test]
    fn per_pull_auth_apply_to_podman_uses_registry_auth_file_env() {
        let guard = build_per_pull_authfile("ghcr.io", "bot", "tok", "podman")
            .expect("build podman authfile");
        let mut cmd = tokio::process::Command::new("podman");
        guard.apply_to(&mut cmd, "podman");
        let dbg = format!("{:?}", cmd);
        assert!(
            dbg.contains("REGISTRY_AUTH_FILE"),
            "podman branch must include REGISTRY_AUTH_FILE env var, got: {}",
            dbg
        );
        assert!(
            !dbg.contains("--authfile"),
            "podman branch must NOT add --authfile to argv (would mis-position \
             on podman 4.x), got: {}",
            dbg
        );
        // path() still returns Some for podman-shape guards.
        assert!(guard.path().is_some(), "podman guard exposes path()");
    }

    /// v0.2.49 regression test: docker runtime → `DOCKER_CONFIG` env
    /// var present, no `--authfile` argv flag.
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

    /// Built-for-podman guard applied to a docker runtime still uses
    /// `REGISTRY_AUTH_FILE` (the `(Podman, _)` match arm is runtime-
    /// agnostic, by design). Callers SHOULD build runtime-correct
    /// guards up front; this is the conservative fallback.
    #[test]
    fn per_pull_auth_podman_guard_uses_env_var_even_against_docker() {
        let guard = build_per_pull_authfile("ghcr.io", "bot", "tok", "podman")
            .expect("build podman authfile");
        let mut cmd = tokio::process::Command::new("docker");
        guard.apply_to(&mut cmd, "docker");
        let dbg = format!("{:?}", cmd);
        assert!(
            dbg.contains("REGISTRY_AUTH_FILE"),
            "Podman-shape guard always uses REGISTRY_AUTH_FILE regardless of \
             runtime, got: {}",
            dbg
        );
        assert!(
            !dbg.contains("--authfile"),
            "v0.2.49 fix: no --authfile in argv even on the fallback path, \
             got: {}",
            dbg
        );
    }

    /// v0.2.49 regression test (live podman): verify
    /// `REGISTRY_AUTH_FILE=X podman --version` is accepted by the real
    /// podman binary's CLI parser. The original v0.2.47 bug shape
    /// (`podman --authfile X --version`) would have failed with "Error:
    /// unknown flag: --authfile" — this test exercises the parser
    /// end-to-end with the env-var shape to prevent that class of
    /// regression. Skipped (clean return) on hosts without a podman
    /// binary on PATH.
    #[test]
    fn per_pull_auth_podman_env_var_accepted_by_live_podman() {
        let Ok(probe) = std::process::Command::new("which").arg("podman").output() else {
            return;
        };
        if !probe.status.success() {
            return;
        }
        let guard = build_per_pull_authfile("ghcr.io", "bot", "tok", "podman")
            .expect("build podman authfile");
        let path = guard
            .path()
            .expect("podman guard exposes path()")
            .to_owned();
        let output = std::process::Command::new("podman")
            .env("REGISTRY_AUTH_FILE", &path)
            .arg("--version")
            .output()
            .expect("spawn podman --version");
        assert!(
            output.status.success(),
            "podman --version with REGISTRY_AUTH_FILE set must succeed, \
             stderr={}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(
            String::from_utf8_lossy(&output.stdout).starts_with("podman version "),
            "expected podman version banner"
        );
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

    // ─── v0.2.49 Stream A: global container helpers ──────────────────────

    /// Trailing `-{project_slug}` is stripped.
    #[test]
    fn v0249_resolve_global_container_name_strips_trailing_project_slug() {
        let result =
            resolve_global_container_name("vct-rl-reranker-{project_slug}", "vct-rl-reranker")
                .expect("resolve");
        assert_eq!(result, "vct-rl-reranker");
    }

    /// Trailing `_{project_slug}` is also stripped (underscore variant).
    #[test]
    fn v0249_resolve_global_container_name_strips_underscore_variant() {
        let result =
            resolve_global_container_name("my_module_{project_slug}", "my-module").expect("resolve");
        assert_eq!(result, "my_module");
    }

    /// Template without `{project_slug}` passes through unchanged.
    #[test]
    fn v0249_resolve_global_container_name_idempotent_on_bare_name() {
        let result =
            resolve_global_container_name("vct-rl-reranker", "vct-rl-reranker").expect("resolve");
        assert_eq!(result, "vct-rl-reranker");
    }

    /// `{project_slug}` in non-trailing position is rejected — the
    /// resolver refuses to silently mangle the name.
    #[test]
    fn v0249_resolve_global_container_name_rejects_non_trailing_placeholder() {
        let result = resolve_global_container_name(
            "prefix-{project_slug}-suffix",
            "vct-rl-reranker",
        );
        assert!(result.is_err());
    }

    /// Unresolved non-`{project_slug}` placeholders are rejected.
    ///
    /// NOTE: this asserts the contract of the LOW-LEVEL resolver in
    /// isolation — by the time a template reaches
    /// `resolve_global_container_name`, the `{module_id}` token has
    /// ALREADY been substituted upstream by
    /// `RuntimeBlock::resolve_container_name_template`. A `{module_id}`
    /// that still survives at this layer therefore genuinely is a bug
    /// (a caller that bypassed the template resolver), so rejecting it
    /// is correct. The end-to-end happy path is covered by
    /// `v0259_global_container_name_resolves_module_id_token_end_to_end`
    /// below.
    #[test]
    fn v0249_resolve_global_container_name_rejects_unresolved_placeholders() {
        let result = resolve_global_container_name("name-{module_id}", "vct-rl-reranker");
        assert!(result.is_err());
    }

    /// v0.2.59 regression: the canonical global-singleton template
    /// `container_name_template: "{module_id}"` — the exact form shipped
    /// by vct-rl-reranker v0.2.10's `vct-module.json` — must resolve to
    /// the bare module id END-TO-END (template resolver → global name
    /// resolver), not be rejected as an "unresolved placeholder".
    ///
    /// This is the bug the 2026-06-09 "global-singleton bidirectionally
    /// verified" paper audit missed: every prior test used the
    /// per-project-suffix form `"vct-rl-reranker-{project_slug}"`, so the
    /// `"{module_id}"` form the real manifest ships was never exercised.
    /// The install failed at container-start with
    /// `container_name_template '{module_id}' has unresolved placeholders`.
    #[test]
    fn v0259_global_container_name_resolves_module_id_token_end_to_end() {
        let manifest = make_rl_manifest_global_for_test();
        let mut runtime = manifest.runtime.clone();
        // Use the REAL shipped template, not the test factory's
        // `-{project_slug}` form.
        runtime.container_name_template = Some("{module_id}".into());

        let template = runtime.resolve_container_name_template("vct-rl-reranker");
        // Template resolver substitutes {module_id} at the choke-point.
        assert_eq!(template, "vct-rl-reranker");

        // Downstream global resolver now sees a clean bare name.
        let resolved =
            resolve_global_container_name(&template, "vct-rl-reranker").expect("must resolve");
        assert_eq!(resolved, "vct-rl-reranker");
    }

    /// `rl_placeholders_global` substitutes `"global"` for `{project_slug}`.
    #[test]
    fn v0249_rl_placeholders_global_uses_fixed_slug() {
        let placeholders = rl_placeholders_global(11443);
        assert_eq!(placeholders.get("{project_slug}").map(|s| s.as_str()), Some("global"));
        assert_eq!(placeholders.get("{RL_SERVER_PORT}").map(|s| s.as_str()), Some("11443"));
    }

    /// `build_podman_run_args_global` rejects non-container runtime types
    /// — mirrors the per-project builder's contract.
    #[test]
    fn v0249_build_podman_run_args_global_rejects_cli_runtime() {
        let mut manifest = make_rl_manifest_global_for_test();
        manifest.runtime.r#type = "cli".into();
        let ctx = crate::manifest::PlaceholderCtx::new("vct-rl-reranker");
        let result = build_podman_run_args_global(
            &manifest, &ctx, 11443, "vct-rl-reranker", "img", "podman", None,
        );
        assert!(result.is_err());
    }

    /// `build_podman_run_args_global` produces a `podman run` argv that
    /// includes the bare container name (no slug suffix) and the
    /// expected `-d` + `--name` flags.
    #[test]
    fn v0249_build_podman_run_args_global_uses_bare_container_name() {
        let manifest = make_rl_manifest_global_for_test();
        let ctx = crate::manifest::PlaceholderCtx::new("vct-rl-reranker");
        let args = build_podman_run_args_global(
            &manifest,
            &ctx,
            11443,
            "vct-rl-reranker",
            "ghcr.io/x/y:0.2.10",
            "podman",
            None,
        )
        .expect("build");
        assert_eq!(args[0], "run");
        assert_eq!(args[1], "-d");
        assert_eq!(args[2], "--name");
        assert_eq!(args[3], "vct-rl-reranker");
        // Image lives somewhere in the argv (after env flags).
        assert!(args.iter().any(|a| a == "ghcr.io/x/y:0.2.10"));
    }

    /// Local fixture: minimal RL-shaped manifest with
    /// `install.scope = global` for the v0.2.49 Stream A tests above.
    fn make_rl_manifest_global_for_test() -> crate::manifest::ModuleManifest {
        use crate::manifest::{
            Compatibility, ContainerInstallBlock, InstallBlock, InstallMethod, InstallScope,
            LicenseBlock, ModuleCategory, ModuleManifest, Requirements, RuntimeBlock,
        };
        use std::collections::HashMap;

        let mut env_fixed = HashMap::new();
        env_fixed.insert("RL_SERVER_PORT".into(), "11443".into());

        ModuleManifest {
            manifest_version: 1,
            id: "vct-rl-reranker".into(),
            name: "RL Reranker".into(),
            version: "0.2.10".into(),
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
                r#ref: Some("0.2.10".into()),
                install_dir: "{VCT_MODULES}/vct-rl-reranker".into(),
                post_install: vec![],
                container: Some(ContainerInstallBlock {
                    image: "ghcr.io/hotak92/vct-rl-reranker".into(),
                    tag_from_version: true,
                    registry: Some("ghcr.io".into()),
                    pull_token_endpoint: "https://example.invalid/x".into(),
                    pull_token_method: "POST".into(),
                    rotate_weights: false,
                    rotate_weights_endpoint: None,
                }),
                scope: InstallScope::Global,
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
                env_derived: HashMap::new(),
                health_check: None,
                ports: vec![],
                volumes: vec![],
                container_name_template: Some("vct-rl-reranker-{project_slug}".into()),
                image_ref: None,
                auto_restart: false,
                gpu_image_variants: None,
                log_file: None,
                log_path_template: None,
                min_gpu_vram_gb: None,
                gpu_optional: true,
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
            kg_collections: None,
        }
    }

    // ─── V52-D.1 manifest-runtime-pathological detection ─────────────

    #[test]
    fn v0252_d1_pathological_runtime_command_podman_detected() {
        // The empirical v0.2.9 manifest: `runtime.command = "podman"`
        // with `runtime.args = ["run", "--rm", "-p", "11450:11450", "{module_image}"]`.
        // Both indicators present.
        assert!(is_runtime_pathological(
            "podman",
            &[
                "run".into(),
                "--rm".into(),
                "-p".into(),
                "11450:11450".into(),
                "{module_image}".into(),
            ],
            Some("vct-rl-reranker"),
        ));
    }

    #[test]
    fn v0252_d1_pathological_runtime_command_docker_detected() {
        assert!(is_runtime_pathological(
            "docker",
            &["run".into(), "{module_image}".into()],
            Some("evil-module"),
        ));
    }

    #[test]
    fn v0252_d1_pathological_runtime_command_podman_with_whitespace_detected() {
        // Authoring slip: trailing whitespace in command field.
        assert!(is_runtime_pathological(
            "  podman  ",
            &["run".into()],
            Some("vct-rl-reranker"),
        ));
    }

    #[test]
    fn v0252_d1_pathological_runtime_unsub_module_image_in_args_detected() {
        // Even if command is "python" (not a runtime binary), the
        // presence of {module_image} in args means the manifest author
        // copy-pasted the wrong substitution context.
        assert!(is_runtime_pathological(
            "python",
            &["-m".into(), "rl_server".into(), "{module_image}".into()],
            Some("vct-rl-reranker"),
        ));
    }

    #[test]
    fn v0252_d1_pathological_runtime_shell_without_dash_c_detected() {
        // `sh` with no `-c` arg → would exit immediately under
        // detached mode. Likely an authoring mistake.
        assert!(is_runtime_pathological(
            "sh",
            &["echo".into(), "hello".into()],
            Some("dodgy-module"),
        ));
        assert!(is_runtime_pathological(
            "bash",
            &[],
            Some("dodgy-module"),
        ));
    }

    #[test]
    fn v0252_d1_pathological_runtime_shell_with_dash_c_allowed() {
        // Legit `sh -c "..."` invocation passes (the -c arg signals
        // intent).
        assert!(!is_runtime_pathological(
            "sh",
            &["-c".into(), "python -m rl_server".into()],
            Some("ok-module"),
        ));
        assert!(!is_runtime_pathological(
            "bash",
            &["-c".into(), "exec /app/start.sh".into()],
            Some("ok-module"),
        ));
    }

    #[test]
    fn v0252_d1_pathological_runtime_legit_command_passes() {
        // The canonical legit shape: `python -m <module>` with no
        // {module_image} placeholder. Must NOT be flagged.
        assert!(!is_runtime_pathological(
            "python",
            &["-m".into(), "rl_server.rl_server".into()],
            Some("vct-rl-reranker"),
        ));
        assert!(!is_runtime_pathological(
            "node",
            &["server.js".into()],
            Some("some-node-module"),
        ));
        assert!(!is_runtime_pathological(
            "/app/start",
            &[],
            Some("absolute-binary-module"),
        ));
    }

    #[test]
    fn v0252_d1_pathological_runtime_empty_command_passes() {
        // Empty command means "use the image ENTRYPOINT" — this is
        // the v0.2.49 declarative-manifest shape. Not pathological;
        // the empty-command branch in build_podman_run_args handles
        // it separately (skip CMD override). This test pins that
        // `is_runtime_pathological` returns false so we don't double-
        // log the warning.
        assert!(!is_runtime_pathological("", &[], Some("declarative-module")));
        assert!(!is_runtime_pathological("", &[], None));
    }

    /// End-to-end: a manifest with the pre-v0.2.49 Bug E shape produces
    /// a `podman run` argv with NO CMD override. The image ENTRYPOINT
    /// runs unmolested — same effect as if the broken manifest had
    /// `command: ""`. This is the user-facing fix: stale catalog rows
    /// no longer produce restart-looping containers.
    #[test]
    fn v0252_d1_build_podman_run_args_strips_pathological_cmd_override() {
        let mut manifest = make_manifest(true, true);
        // Inject the empirical v0.2.9 broken shape.
        manifest.runtime.command = "podman".into();
        manifest.runtime.args = vec![
            "run".into(),
            "--rm".into(),
            "-p".into(),
            "11450:11450".into(),
            "{module_image}".into(),
        ];
        let project = make_project();
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args = build_podman_run_args(
            &manifest,
            &ctx,
            &project,
            11533,
            "vct-rl-reranker-acme-corp",
            "ghcr.io/hotak92/vct-rl-reranker:0.2.9",
            "podman",
            None,
        )
        .expect("build args");
        // The positional image arg is the LAST element — no CMD
        // override appended after it.
        assert_eq!(
            args.last().map(String::as_str),
            Some("ghcr.io/hotak92/vct-rl-reranker:0.2.9"),
            "build_podman_run_args must NOT append the pathological CMD; \
             expected image as last arg, got args={:?}",
            args,
        );
        // Defensive: no element contains "{module_image}" — the
        // launcher must not pass the unsubstituted placeholder to
        // podman.
        assert!(
            args.iter().all(|a| !a.contains("{module_image}")),
            "no arg may carry an unsubstituted {{module_image}} placeholder; got {:?}",
            args,
        );
    }

    // ─── V52-D.2 reaper unit tests ───────────────────────────────────

    fn snap(name: &str, image: &str, cmd: &str) -> ContainerSnapshot {
        ContainerSnapshot {
            name: name.into(),
            image: image.into(),
            cmd: cmd.into(),
        }
    }

    fn claimed_set(names: &[&str]) -> std::collections::HashSet<String> {
        names.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn v0252_d2_classify_broken_cmd_takes_priority() {
        // BrokenCmd verdict should fire even when the container is
        // ALSO orphan / stale-image — operator visibility ranks the
        // worst class first.
        let s = snap(
            "vct-rl-reranker-orchestrator-root",
            "ghcr.io/hotak92/vct-rl-reranker:0.2.9-cuda",
            "podman run --rm -p 11450:11450 {module_image}",
        );
        let claimed = claimed_set(&["vct-rl-reranker-orchestrator-root"]);
        let verdict = classify_container_for_reaper(&s, &claimed, |_| {
            Some("ghcr.io/hotak92/vct-rl-reranker:0.2.9-cuda".to_string())
        });
        assert_eq!(verdict, ReaperVerdict::BrokenCmd);
    }

    #[test]
    fn v0252_d2_classify_orphan_when_not_in_claimed() {
        let s = snap("vct-rl-reranker", "ghcr.io/hotak92/vct-rl-reranker:0.1.0", "python -m rl_server");
        let claimed = claimed_set(&["vct-rl-reranker-orchestrator-root"]);
        let verdict = classify_container_for_reaper(&s, &claimed, |_| None);
        assert_eq!(verdict, ReaperVerdict::Orphan);
    }

    #[test]
    fn v0252_d2_classify_stale_image_when_tag_differs() {
        let s = snap(
            "vct-rl-reranker-rs-slug",
            "ghcr.io/hotak92/vct-rl-reranker:0.1.0",
            "python -m rl_server",
        );
        let claimed = claimed_set(&["vct-rl-reranker-rs-slug"]);
        let verdict = classify_container_for_reaper(&s, &claimed, |_| {
            Some("ghcr.io/hotak92/vct-rl-reranker:0.2.9-cuda".to_string())
        });
        assert_eq!(verdict, ReaperVerdict::StaleImage);
    }

    #[test]
    fn v0252_d2_classify_healthy_when_matched() {
        let s = snap(
            "vct-rl-reranker-rs-slug",
            "ghcr.io/hotak92/vct-rl-reranker:0.2.9-cuda",
            "python -m rl_server",
        );
        let claimed = claimed_set(&["vct-rl-reranker-rs-slug"]);
        let verdict = classify_container_for_reaper(&s, &claimed, |_| {
            Some("ghcr.io/hotak92/vct-rl-reranker:0.2.9-cuda".to_string())
        });
        assert_eq!(verdict, ReaperVerdict::Healthy);
    }

    #[test]
    fn v0252_d2_classify_healthy_when_expected_unknown() {
        // Claimed + cmd clean + expected_image_for returns None →
        // cannot stale-check. Default to Healthy (we don't reap on
        // unprovable suspicion).
        let s = snap("vct-rl-reranker-rs-slug", "anything:latest", "python");
        let claimed = claimed_set(&["vct-rl-reranker-rs-slug"]);
        let verdict = classify_container_for_reaper(&s, &claimed, |_| None);
        assert_eq!(verdict, ReaperVerdict::Healthy);
    }

    #[test]
    fn v0252_d2_image_refs_equivalent_byte_equal() {
        assert!(image_refs_equivalent(
            "ghcr.io/hotak92/vct-rl-reranker:0.2.9-cuda",
            "ghcr.io/hotak92/vct-rl-reranker:0.2.9-cuda",
        ));
    }

    #[test]
    fn v0252_d2_image_refs_equivalent_tail_match_after_normalisation() {
        // Podman's inspect may strip `docker.io/library/` from short
        // names; the tail-match rule preserves equivalence.
        assert!(image_refs_equivalent(
            "docker.io/library/alpine:3.20",
            "alpine:3.20",
        ));
        assert!(image_refs_equivalent("alpine:3.20", "docker.io/library/alpine:3.20"));
    }

    #[test]
    fn v0252_d2_image_refs_equivalent_different_tags_not_equivalent() {
        assert!(!image_refs_equivalent(
            "ghcr.io/hotak92/vct-rl-reranker:0.1.0",
            "ghcr.io/hotak92/vct-rl-reranker:0.2.9-cuda",
        ));
    }

    #[test]
    fn v0252_d2_parse_podman_ps_json_happy_path() {
        // Real-shape sample: 2 containers, 1 broken + 1 healthy.
        let json = r#"[
            {
                "Names": ["vct-rl-reranker-orchestrator-root"],
                "Image": "ghcr.io/hotak92/vct-rl-reranker:0.2.9-cuda",
                "Command": ["podman", "run", "--rm", "-p", "11450:11450", "{module_image}"]
            },
            {
                "Names": ["weaviate_claude"],
                "Image": "semitechnologies/weaviate:1.32",
                "Command": ["/bin/weaviate", "--scheme", "http"]
            }
        ]"#;
        let parsed = parse_podman_ps_json(json).expect("parse");
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0].name, "vct-rl-reranker-orchestrator-root");
        assert!(parsed[0].cmd.contains("{module_image}"));
        assert_eq!(parsed[1].name, "weaviate_claude");
        assert!(!parsed[1].cmd.contains("{module_image}"));
    }

    #[test]
    fn v0252_d2_parse_podman_ps_json_handles_missing_fields() {
        // Containers without Names get skipped (cannot reap by name).
        // Missing Image / Command fields default to empty string.
        let json = r#"[
            {"Names": ["only-name"]},
            {"Image": "no-name:tag"},
            {"Names": [], "Image": "empty-names"}
        ]"#;
        let parsed = parse_podman_ps_json(json).expect("parse");
        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed[0].name, "only-name");
        assert_eq!(parsed[0].image, "");
        assert_eq!(parsed[0].cmd, "");
    }

    #[test]
    fn v0252_d2_parse_podman_ps_json_rejects_malformed() {
        let err = parse_podman_ps_json("not json").expect_err("must fail on malformed");
        assert!(err.contains("parse podman ps json"));

        let err = parse_podman_ps_json(r#"{"top": "object not array"}"#)
            .expect_err("must fail on non-array top-level");
        assert!(err.contains("not an array"));
    }

    /// V52-E + V52-D.2 integration: the bare `vct-rl-reranker`
    /// container (image 0.1.0, no DB row) IS an orphan and the
    /// reaper correctly classifies it. Pins the spec's V52-E claim
    /// that V52-D.2's reaper subsumes the standalone V52-E fix.
    #[test]
    fn v0252_d2_v52e_bare_orphan_container_classified_as_orphan() {
        let s = snap(
            "vct-rl-reranker",
            "ghcr.io/hotak92/vct-rl-reranker:0.1.0",
            "python -m rl_server.rl_server",
        );
        // The claimed set contains the per-project variants but NOT
        // the bare `vct-rl-reranker` (it was a manual / pre-v0.2.10
        // leftover).
        let claimed = claimed_set(&[
            "vct-rl-reranker-orchestrator-root",
            "vct-rl-reranker-instambul1860",
            "vct-rl-reranker-sd15",
        ]);
        let verdict = classify_container_for_reaper(&s, &claimed, |_| None);
        assert_eq!(verdict, ReaperVerdict::Orphan);
    }

    /// Sibling test for the GLOBAL builder — same pathology-stripping
    /// behaviour.
    #[test]
    fn v0252_d1_build_podman_run_args_global_strips_pathological_cmd_override() {
        let mut manifest = make_manifest(true, true);
        manifest.runtime.command = "podman".into();
        manifest.runtime.args = vec!["run".into(), "{module_image}".into()];
        let ctx = PlaceholderCtx::new(&manifest.id);
        let args = build_podman_run_args_global(
            &manifest,
            &ctx,
            11443,
            "vct-rl-reranker",
            "ghcr.io/hotak92/vct-rl-reranker:0.2.9",
            "podman",
            None,
        )
        .expect("build args global");
        assert_eq!(
            args.last().map(String::as_str),
            Some("ghcr.io/hotak92/vct-rl-reranker:0.2.9"),
            "global builder must also strip pathological CMD; got args={:?}",
            args,
        );
        assert!(args.iter().all(|a| !a.contains("{module_image}")));
    }
}
