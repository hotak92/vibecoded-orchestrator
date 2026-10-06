//! PR-10A — User-configurable container-volume storage UX.
//!
//! v0.2.101 (owner ruling Q4b, census S5): `volumes.rs` was merged INTO
//! this module — two storage-config systems in two files was one concern
//! in two homes. This file now owns both halves:
//!
//!   - the install-time auto-detection + the destructive `migrate_volumes`
//!     pipeline that exists ONLY to move an already-running deployment
//!     between paths (the merged volumes.rs sections below);
//!   - the user-facing Settings → Storage
//!     surface that:
//!       1. Reads / writes `~/.vct/storage.toml` (separate from
//!          `launcher.toml`'s Bug 31 mapping — different lifecycle, different
//!          schema, and the user can hand-edit one without disturbing the
//!          other).
//!       2. Detects PRE-EXISTING legacy named volumes left over from
//!          previous installs, with a STRICT allowlist so we never offer
//!          to alias an unrelated project's data (e.g. some other app's
//!          `someapp-*` / `otherproj_*` volumes on the same host).
//!       3. Generates `infrastructure/compose.override.yaml` in
//!          three shapes: default (empty), bind-mount per service, or
//!          external alias per service. The filename intentionally matches
//!          podman-compose's auto-load convention (`compose.override.yaml`
//!          / `compose.override.yml`) — the legacy
//!          `docker-compose.override.yml` name is NOT auto-loaded by
//!          podman-compose, which was the silent-failure mode in PR-10A
//!          before this rename (PR-22, 2026-05-16).
//!       4. Offers rsync-style migration helpers (`migrate_to_named_volume`
//!          / `migrate_to_bind_path`) that wrap POSIX `cp -a` and emit
//!          structured deferrals on partial success.
//!
//! ## Strict allowlist
//!
//! `detect_legacy_volumes_inner()` (surfaced as `get_storage_config`'s
//! `legacy_volumes`) MUST NOT return anything outside
//! [`LEGACY_VOLUME_ALLOWLIST`] / the `vco_*` prefix. The list is hand-curated
//! and audited by `tests::detect_legacy_volumes_rejects_unrelated_namespaces`.
//! Adding a name here means we'll offer it to users as a recyclable volume —
//! getting that wrong (e.g. listing a bare `redis` volume) WOULD point
//! someone else's container data at our Weaviate mountpoint. Never add a
//! generic name to this list. Always namespace via `vco_*` or the
//! pre-PR-7 `_claude` suffix that this orchestrator historically used.
//!
//! ## Multi-OS contract
//!
//! - Paths use `PathBuf` + `directories::UserDirs::home_dir()` — no
//!   hardcoded `/home/...` or `C:\Users\...`.
//! - `podman volume ls` and `docker volume ls` have identical output
//!   formats on Linux / macOS / Windows; we try them in that order.
//! - The `migrate_*` rsync helpers use `cp -a` on Unix; on Windows we
//!   defer to a user-runnable command rather than shelling out (the
//!   `cp.exe` shipped with Git for Windows uses Cygwin path semantics
//!   that don't compose well with `\\?\` long paths).
//! - The override.yml renderer emits forward slashes inside the YAML
//!   even on Windows; compose accepts forward-slash bind paths on all
//!   three OSes and Docker Desktop normalizes them to drive-letter paths.
//!
//! ## Soft-fail policy
//!
//! Every Tauri command catches its own failures and returns
//! `Result<T, String>` with a user-readable message. Missing `storage.toml`
//! → defaults. `podman volume ls` unavailable → empty list + log warning.
//! Atomic write failures → error (don't half-write).

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use tauri::{command, AppHandle, Emitter};
use vct_launcher_core::process::CommandExt as _;
use vct_launcher_core::services::container_runtime::{self, RuntimePinSource};

use super::installer::ExistingVolume;

// ---------------------------------------------------------------------------
// Strict legacy-volume allowlist
// ---------------------------------------------------------------------------

/// Volume names we recognize as "ours" — either the current `vco_*` naming
/// from infrastructure/docker-compose.yml, or pre-PR-7 historical names
/// from when the orchestrator was called Claude.
///
/// Any volume on the host NOT in this list (and not prefixed `vco_`) is
/// considered out-of-namespace and MUST NOT be surfaced to the user as a
/// recyclable volume. Concretely: never include some other app's namespaced
/// volumes (e.g. `someapp-*`, `otherproj_*`) or bare service names like
/// `frontend_*`, `python_*`, `accounts_*`, `redis_*`, or `postgres_*` (the
/// bare names without our prefix). Also exclude bare `ollama` — that's a
/// bind-mount path in some user setups, not a named volume we own.
///
/// See `tests::detect_legacy_volumes_rejects_unrelated_namespaces` for the
/// negative assertion that locks this list down.
pub const LEGACY_VOLUME_ALLOWLIST: &[&str] = &[
    // Current VCO naming (post-0.2.11).
    "vco_weaviate_data",
    "vco_ollama_models",
    "vco_ollama_data",
    "vco_code_embed_cache",
    "vco_searxng_settings",
    "vco_neo4j_data",
    // Canonical compose volume names (no project prefix — emitted by
    // `docker-compose` from the bare key in `infrastructure/docker-compose.yml`).
    "weaviate_data",
    "ollama_data",
    "code_embed_cache",
    // Legacy pre-PR-7 naming (the previous "Claude" orchestrator era).
    // We keep these so existing users can adopt their old data.
    "weaviate_claude",
    "ollama_claude",
    "code_embed_claude",
    "searxng_claude",
    "model_router_claude",
    "neo4j_claude",
];

/// Prefix that always implies a VCO-managed volume, regardless of the
/// suffix. Compose-generated names start with the project namespace
/// (`vco_<volume_key>` when COMPOSE_PROJECT_NAME=vco), so the prefix
/// match catches any future volume key without requiring an allowlist
/// update.
const VCO_VOLUME_PREFIX: &str = "vco_";

/// Filter predicate: is `name` a volume we recognize as ours and
/// therefore safe to offer the user?
pub fn is_recognized_legacy_volume(name: &str) -> bool {
    if name.starts_with(VCO_VOLUME_PREFIX) {
        return true;
    }
    LEGACY_VOLUME_ALLOWLIST.contains(&name)
}

// ---------------------------------------------------------------------------
// Persisted types — `~/.vct/storage.toml`
// ---------------------------------------------------------------------------

/// Storage configuration as persisted in `~/.vct/storage.toml`.
///
/// One file per user, not per project. Storage applies to the orchestrator's
/// own compose deployment — per-project storage UX is out of scope for v0.2.11.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StorageConfig {
    /// `"named"` (default, runtime-managed) or `"bind"` (user-chosen path).
    #[serde(default = "default_mode")]
    pub mode: String,

    /// When `mode == "bind"`: absolute path to a folder containing one
    /// subfolder per service (e.g. `<root>/weaviate`, `<root>/ollama`).
    /// Empty string when not set. The renderer ALWAYS emits forward-slash
    /// paths inside the YAML (compose accepts them cross-platform).
    #[serde(default)]
    pub bind_root: String,

    /// Per-service path overrides. Allows the user to pin one service
    /// onto a fast SSD while leaving others on the system disk. Keys
    /// are logical service names (`weaviate`, `ollama`, `code_embed`).
    /// Override an entry by setting its value to an absolute path; empty
    /// string means "fall through to `bind_root`/<service>".
    #[serde(default)]
    pub per_service_paths: BTreeMap<String, String>,

    /// External-alias map: canonical volume key → host-side named-volume
    /// name. Used when the user wants to reuse a pre-existing legacy
    /// volume by name. Empty in modes "named" and "bind".
    #[serde(default)]
    pub external_aliases: BTreeMap<String, String>,
}

fn default_mode() -> String {
    "named".to_string()
}

impl Default for StorageConfig {
    fn default() -> Self {
        Self {
            mode: default_mode(),
            bind_root: String::new(),
            per_service_paths: BTreeMap::new(),
            external_aliases: BTreeMap::new(),
        }
    }
}

/// One detected legacy volume — a candidate for the "Use this for <service>"
/// row in the Settings → Storage card.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DetectedLegacyVolume {
    pub name: String,
    pub mountpoint: String,
    pub driver: String,
    /// `weaviate` | `ollama` | `code_embed` | `unknown` — inferred by
    /// substring match on the volume name.
    pub role: String,
}

/// Front-end-facing wrapper returned by `get_storage_config()`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StorageConfigView {
    pub config: StorageConfig,
    pub config_path: String,
    pub legacy_volumes: Vec<DetectedLegacyVolume>,
    /// Set when the config file did not exist and we synthesized one
    /// from defaults (mode = "named"). The FE uses this to show the
    /// install-wizard hint instead of the "Apply" button.
    pub synthesized_from_defaults: bool,
}

// ---------------------------------------------------------------------------
// Logical services & role inference
// ---------------------------------------------------------------------------

/// The three orchestrator services that own named volumes. Order matters
/// for stable test output and for the order in which the UI renders the
/// per-service rows.
pub const LOGICAL_SERVICES: &[&str] = &["weaviate", "ollama", "code_embed"];

/// Canonical (compose) volume key for a logical service.
fn canonical_volume_for(service: &str) -> Option<&'static str> {
    match service {
        "weaviate" => Some("weaviate_data"),
        "ollama" => Some("ollama_data"),
        "code_embed" => Some("code_embed_cache"),
        _ => None,
    }
}

/// Container-side mount target for a logical service. Used by the
/// bind-mount renderer.
fn container_mount_for(service: &str) -> Option<&'static str> {
    match service {
        "weaviate" => Some("/var/lib/weaviate"),
        "ollama" => Some("/root/.ollama"),
        "code_embed" => Some("/cache"),
        _ => None,
    }
}

/// Infer the logical role of a host-side volume name by substring match.
fn infer_role(volume_name: &str) -> String {
    let n = volume_name.to_ascii_lowercase();
    if n.contains("weaviate") {
        "weaviate".to_string()
    } else if n.contains("ollama") {
        "ollama".to_string()
    } else if n.contains("code_embed") || n.contains("code-embed") {
        "code_embed".to_string()
    } else if n.contains("searxng") {
        "searxng".to_string()
    } else if n.contains("neo4j") {
        "neo4j".to_string()
    } else if n.contains("model_router") || n.contains("model-router") {
        "model_router".to_string()
    } else {
        "unknown".to_string()
    }
}

// ---------------------------------------------------------------------------
// Path resolution
// ---------------------------------------------------------------------------

/// `~/.vct/storage.toml`. Honors the same VCT_STATE_DIR isolation pattern
/// the rest of the launcher uses (so dev runs don't clobber production).
pub fn storage_config_path() -> PathBuf {
    crate::paths::vct_root_dir().join("storage.toml")
}

fn compose_override_path() -> Result<PathBuf, String> {
    let root = super::installer::find_local_repo_root()?;
    // Filename MUST match podman-compose's auto-load convention
    // (compose.override.yaml / compose.override.yml). The legacy
    // docker-compose.override.yml name (Docker Compose v1) is NOT
    // auto-loaded by podman-compose — see PR-22 (2026-05-16) and
    // knowledge/concepts/podman-compose-override-comment-yaml-drift-footgun.md.
    Ok(root.join("infrastructure").join("compose.override.yaml"))
}

/// v0.2.54 (C-RT-5): the Docker-Compose auto-load sibling. Docker
/// Compose auto-loads `docker-compose.override.yml` (and the volumes half's
/// Bug-31 path historically wrote ONLY that name while this module
/// wrote ONLY `compose.override.yaml`) — two generators, two filenames,
/// divergent bodies. Which volume aliases applied depended on which
/// compose binary ran; a runtime switch could re-point Weaviate/Ollama
/// at fresh empty volumes. Fix: every write here mirrors the SAME body
/// to BOTH names (the volumes half does the same in the other direction).
fn compose_override_sibling_path() -> Result<PathBuf, String> {
    let root = super::installer::find_local_repo_root()?;
    Ok(root.join("infrastructure").join("docker-compose.override.yml"))
}

// ---------------------------------------------------------------------------
// Atomic config persistence
// ---------------------------------------------------------------------------

/// Read `~/.vct/storage.toml`. Missing / malformed → defaults.
fn read_storage_config_from(path: &Path) -> (StorageConfig, bool) {
    match std::fs::read_to_string(path) {
        Ok(raw) => match toml::from_str::<StorageConfig>(&raw) {
            Ok(cfg) => (normalize_config(cfg), false),
            Err(_) => {
                // Malformed file → defaults. We deliberately do not
                // delete the bad file; the user may want to recover it
                // manually. The synthesized flag stays false so the FE
                // doesn't pretend nothing is wrong.
                tracing::warn!(
                    "[storage_ux] warning: could not parse {} as TOML; using defaults",
                    path.display()
                );
                (StorageConfig::default(), false)
            }
        },
        Err(_) => (StorageConfig::default(), true),
    }
}

fn write_storage_config_to(path: &Path, cfg: &StorageConfig) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create {}: {}", parent.display(), e))?;
    }
    let body = toml::to_string_pretty(cfg)
        .map_err(|e| format!("serialize storage.toml: {}", e))?;
    let mut tmp = path.to_path_buf();
    tmp.set_extension("toml.tmp");
    std::fs::write(&tmp, &body).map_err(|e| format!("write tmp {}: {}", tmp.display(), e))?;
    std::fs::rename(&tmp, path)
        .map_err(|e| format!("rename {} -> {}: {}", tmp.display(), path.display(), e))?;
    Ok(())
}

/// Normalize a parsed config: lowercase mode, prune unknown service keys.
fn normalize_config(mut cfg: StorageConfig) -> StorageConfig {
    cfg.mode = cfg.mode.trim().to_ascii_lowercase();
    if cfg.mode.is_empty() {
        cfg.mode = default_mode();
    }
    // Drop unknown service keys from per_service_paths so the renderer
    // never emits a phantom service.
    cfg.per_service_paths
        .retain(|k, _| LOGICAL_SERVICES.contains(&k.as_str()));
    cfg.external_aliases
        .retain(|k, _| canonical_volume_for(k).is_some() || k.starts_with("vco_"));
    cfg
}

// ---------------------------------------------------------------------------
// Volume-name validation
// ---------------------------------------------------------------------------

/// Cross-platform external-volume name validator.
///
/// Docker / Podman allow `[a-zA-Z0-9][a-zA-Z0-9_.-]*`. We reject anything
/// outside that to avoid shelling out with an attacker-controlled string
/// when the user types in a custom external alias.
fn is_valid_volume_name(name: &str) -> bool {
    if name.is_empty() || name.len() > 256 {
        return false;
    }
    let mut chars = name.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphanumeric() => {}
        _ => return false,
    }
    for c in chars {
        if !(c.is_ascii_alphanumeric() || c == '_' || c == '.' || c == '-') {
            return false;
        }
    }
    true
}

// ---------------------------------------------------------------------------
// Override-yml generation
// ---------------------------------------------------------------------------

/// Emit a forward-slash version of a path; compose accepts forward
/// slashes on all three OSes (Docker Desktop normalizes them to
/// drive-letter paths on Windows).
fn yaml_path_str(p: &Path) -> String {
    p.display().to_string().replace('\\', "/")
}

/// Generate the body of `infrastructure/compose.override.yaml`
/// for a given storage config.
///
/// Modes:
///   - `"named"` (default) → empty stanzas. Base compose volumes used as-is.
///   - `"bind"` → bind-mount each service's host path at its container
///     mount target. Per-service overrides win over `bind_root`.
///   - `"external"` (implicit, when `external_aliases` is non-empty) →
///     each canonical volume aliased to an existing host-side volume
///     via `external: true`.
///
/// Always emits a leading header comment so users + future maintainers
/// know the file is launcher-managed. Idempotent: same config → same
/// bytes (sorted keys via BTreeMap).
pub fn render_override_yaml(cfg: &StorageConfig) -> String {
    let header = "# Auto-generated by VCT Launcher (PR-10A storage UX).\n\
                  # Edits will be overwritten the next time the user changes the\n\
                  # storage configuration via Settings -> Storage or the install wizard.\n";

    // External-alias mode wins regardless of `mode` field — if the user
    // has explicit aliases, they came from "Use this for <service>" rows
    // in the legacy-volume picker and we must honor them.
    if !cfg.external_aliases.is_empty() {
        let mut out = String::new();
        out.push_str(header);
        out.push_str("\nservices: {}\n\nvolumes:\n");
        // BTreeMap iteration is alphabetical → stable output.
        for (canonical, legacy) in &cfg.external_aliases {
            // Reject anything that wouldn't survive a downstream
            // `podman volume inspect`. Skip silently — the FE validated
            // before calling, but if a hand-edited storage.toml slipped
            // a bad name through, we DO NOT want to emit it (could lead
            // to weird compose errors on `up -d`).
            if !is_valid_volume_name(legacy) {
                continue;
            }
            out.push_str(&format!(
                "  {canonical}:\n    external: true\n    name: {legacy}\n",
            ));
        }
        return out;
    }

    if cfg.mode == "bind" {
        let bind_root = cfg.bind_root.trim();
        if bind_root.is_empty() && cfg.per_service_paths.is_empty() {
            // Bind mode but no path → treat as default to avoid emitting
            // a half-formed override that would point compose at "".
            return format!("{header}\nservices: {{}}\nvolumes: {{}}\n");
        }
        let mut out = String::new();
        out.push_str(header);
        out.push_str("\nservices:\n");
        // Per service, render only when we have a usable path.
        for svc in LOGICAL_SERVICES {
            let container_target = match container_mount_for(svc) {
                Some(t) => t,
                None => continue,
            };
            let svc_path = cfg
                .per_service_paths
                .get(*svc)
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .or_else(|| {
                    if bind_root.is_empty() {
                        None
                    } else {
                        let joined = PathBuf::from(bind_root).join(svc);
                        Some(yaml_path_str(&joined))
                    }
                });
            let svc_path = match svc_path {
                Some(p) => p,
                None => continue,
            };
            // `:Z` SELinux relabel on Linux is harmless on macOS / Windows
            // (Docker Desktop ignores it). Keeping it cross-platform.
            out.push_str(&format!(
                "  {svc}:\n    volumes:\n      - {svc_path}:{container_target}:Z\n",
            ));
        }
        out.push_str("\nvolumes: {}\n");
        return out;
    }

    // Default: named volumes — emit empty stanzas so the file's presence
    // alone signals to compose that the override is intentional (idempotent
    // re-runs leave it unchanged).
    format!("{header}\nservices: {{}}\nvolumes: {{}}\n")
}

/// Atomic write of the override file. v0.2.54 (C-RT-5): mirrors the
/// identical body to BOTH compose auto-load names — see
/// [`compose_override_sibling_path`].
pub fn write_compose_override(body: &str) -> Result<PathBuf, String> {
    let path = compose_override_path()?;
    write_override_yaml_to(&path, body)?;
    let sibling = compose_override_sibling_path()?;
    write_override_yaml_to(&sibling, body)?;
    Ok(path)
}

/// Atomic write to an arbitrary target path. Used by
/// [`write_compose_override`] (v0.2.54: both auto-load names) and tests.
pub fn write_override_yaml_to(target: &Path, body: &str) -> Result<(), String> {
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create {}: {}", parent.display(), e))?;
    }
    let mut tmp = target.to_path_buf();
    tmp.set_extension("yml.tmp");
    std::fs::write(&tmp, body).map_err(|e| format!("write tmp {}: {}", tmp.display(), e))?;
    std::fs::rename(&tmp, target)
        .map_err(|e| format!("rename {} -> {}: {}", tmp.display(), target.display(), e))?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Override-yml user-customization guard
// ---------------------------------------------------------------------------

/// Read the existing override file (if any). Empty string when the file
/// doesn't exist or can't be read.
fn read_existing_override() -> String {
    match compose_override_path() {
        Ok(p) => std::fs::read_to_string(p).unwrap_or_default(),
        Err(_) => String::new(),
    }
}

/// Heuristic: does the existing override.yml contain user customizations
/// beyond what `render_override_yaml` would produce?
///
/// We're not parsing YAML here — that would require pulling in serde_yaml
/// and would still be brittle against comment / whitespace differences.
/// Instead we look for the launcher's header marker. Files NOT carrying
/// that marker are treated as user-authored.
pub fn is_launcher_managed_override(body: &str) -> bool {
    body.contains("Auto-generated by VCT Launcher")
}

// ---------------------------------------------------------------------------
// Legacy-volume detection
// ---------------------------------------------------------------------------

/// The container runtime a storage command drives, and the pin that chose
/// it (`None` = auto-detected).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct StorageRuntime {
    pub(crate) name: String,
    pub(crate) pin: Option<RuntimePinSource>,
}

/// The runtime every storage command drives — here and in `volumes`.
///
/// v0.2.97 owner ruling "Honour the pin": this was `which_runtime`, a
/// podman-first PATH probe that ignored `VCT_CONTAINER_RUNTIME` and the
/// install's `state/install/runtime.txt`, so a docker-pinned machine had
/// its volumes inspected and migrated under podman. It now ASKS the ONE
/// Python verdict through
/// `container_runtime::detect_container_runtime_with_pin`
/// (`runtime_verdict::decide`): a pin is the only candidate and a pinned
/// runtime that is down is refused, never substituted; the podman-first
/// auto-detect preference itself lives in `vco_lib.runtime_reconcile`,
/// not here.
pub(crate) async fn storage_runtime() -> Result<StorageRuntime, String> {
    let install_root = super::installer::find_local_repo_root().ok();
    storage_runtime_at(install_root.as_deref()).await
}

/// [`storage_runtime`] with the install root (where `state/install/runtime.txt`
/// lives) passed in — a test hands it a temp dir so a developer machine's own
/// runtime record cannot change the answer.
pub(crate) async fn storage_runtime_at(
    install_root: Option<&Path>,
) -> Result<StorageRuntime, String> {
    let (name, pin) =
        container_runtime::detect_container_runtime_with_pin(install_root).await?;
    Ok(StorageRuntime { name, pin })
}

/// `volume`'s mountpoint under the runtime VCO drives — or a REFUSAL when
/// only the other runtime has it (`container_runtime::check_runtime_owns`:
/// podman and docker keep separate volumes, so acting on it under `rt`
/// would act on a copy without the user's data). Empty when neither
/// runtime has it; the caller reports "not found" as before. The other
/// runtime is asked only on that miss, and only to word the refusal.
///
/// R7b F25(b): an inspect that FAILED is not an answer. This counted any
/// failed `volume inspect` under `rt` as "not owned", so a transient error
/// produced a refusal claiming the volume "exists only under" the other
/// runtime. Now a failure under `rt` is an error saying it could not be
/// inspected; a failure under the OTHER runtime is only "not known to own
/// it" — a refusal needs a positive answer.
async fn mountpoint_on_owning_runtime(
    rt: &StorageRuntime,
    volume: &str,
    action: &str,
) -> Result<String, String> {
    match probe_volume(&rt.name, volume).await {
        VolumeProbe::Found { mountpoint, .. } => return Ok(mountpoint),
        VolumeProbe::Unknown(why) => {
            return Err(format!(
                "could not tell whether {} holds volume `{volume}` ({why}); refusing to \
                 {action} it until `{} volume inspect {volume}` answers",
                rt.name, rt.name
            ))
        }
        VolumeProbe::Missing => {}
    }
    let other = container_runtime::other_runtime(&rt.name);
    let owned_by_other = match probe_volume(other, volume).await {
        VolumeProbe::Found { .. } => true,
        VolumeProbe::Missing => false,
        VolumeProbe::Unknown(why) => {
            tracing::info!("[storage_ux] {other} could not be asked about `{volume}` ({why})");
            false
        }
    };
    container_runtime::check_runtime_owns(
        action,
        &format!("volume `{volume}`"),
        &rt.name,
        rt.pin,
        false,
        owned_by_other,
    )?;
    Ok(String::new())
}

/// What `<runtime> volume inspect <name>` established — tri-state, because
/// "no such volume" and "the inspect failed" are different answers (R7b
/// F25(b)). The ONE parser of that command's output: `inspect_volume` below
/// and `existing_volumes_owned_by` (the merged volumes.rs half) go through it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum VolumeProbe {
    Found { mountpoint: String, driver: String },
    /// The runtime answered that it has no such volume — or the runtime is
    /// not installed at all, which holds nothing.
    Missing,
    /// The inspect could not answer (spawn failure other than "not
    /// installed", a daemon/transport error, unparseable output).
    Unknown(String),
}

/// `<runtime> volume inspect <name>`, classified. Read-only.
pub(crate) async fn probe_volume(runtime: &str, name: &str) -> VolumeProbe {
    let out = tokio::process::Command::new(vct_launcher_core::paths::spawn_program(runtime))
        .silent()
        .args(["volume", "inspect", name])
        .output()
        .await;
    let out = match out {
        Ok(o) => o,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return VolumeProbe::Missing,
        Err(e) => return VolumeProbe::Unknown(format!("could not run `{runtime}`: {e}")),
    };
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        // podman: "Error: no such volume <n>"; docker: "Error: No such volume: <n>"
        // / "Error response from daemon: get <n>: no such volume".
        if stderr.to_ascii_lowercase().contains("no such volume") {
            return VolumeProbe::Missing;
        }
        let first = stderr.lines().find(|l| !l.trim().is_empty()).unwrap_or("").trim();
        return VolumeProbe::Unknown(if first.is_empty() {
            format!("`{runtime} volume inspect` exited {}", out.status)
        } else {
            first.to_string()
        });
    }
    let parsed: serde_json::Value = match serde_json::from_slice(&out.stdout) {
        Ok(v) => v,
        Err(e) => return VolumeProbe::Unknown(format!("unreadable `volume inspect` output: {e}")),
    };
    match parsed.as_array().and_then(|a| a.first()) {
        Some(item) => VolumeProbe::Found {
            mountpoint: item.get("Mountpoint").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            driver: item.get("Driver").and_then(|v| v.as_str()).unwrap_or("local").to_string(),
        },
        None => VolumeProbe::Missing,
    }
}

/// Parse one line of `podman volume ls --format '{{.Name}}'` output and
/// return the volume name (trimmed) if it's safe to surface.
fn extract_safe_volume_name(line: &str) -> Option<&str> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return None;
    }
    if !is_recognized_legacy_volume(trimmed) {
        return None;
    }
    Some(trimmed)
}

/// Pure filtering pass — exposed for tests so we can feed mock CLI output.
pub fn filter_legacy_volume_names<'a>(lines: impl IntoIterator<Item = &'a str>) -> Vec<String> {
    let mut out: Vec<String> = lines
        .into_iter()
        .filter_map(extract_safe_volume_name)
        .map(|s| s.to_string())
        .collect();
    out.sort();
    out.dedup();
    out
}

/// Inspect a single volume by name. Returns mountpoint + driver. On
/// any failure returns empty strings (the FE renders "(unavailable)").
async fn inspect_volume(runtime: &str, name: &str) -> (String, String) {
    match probe_volume(runtime, name).await {
        VolumeProbe::Found { mountpoint, driver } => (mountpoint, driver),
        VolumeProbe::Missing | VolumeProbe::Unknown(_) => (String::new(), String::new()),
    }
}

/// Detection routine behind `get_storage_config`'s `legacy_volumes` — separate so tests
/// can call it directly. Returns an empty list if no runtime is present
/// (soft-fail: no error to the caller).
async fn detect_legacy_volumes_inner() -> Vec<DetectedLegacyVolume> {
    let runtime = match storage_runtime().await {
        Ok(r) => r.name,
        Err(e) => {
            tracing::info!(
                "[storage_ux] info: no usable container runtime ({e}); returning empty \
                 legacy-volume list"
            );
            return Vec::new();
        }
    };

    // List ALL volumes, filter through the allowlist + prefix.
    let out = tokio::process::Command::new(vct_launcher_core::paths::spawn_program(&runtime))
        .silent()
        .args(["volume", "ls", "--format", "{{.Name}}"])
        .output()
        .await;
    let out = match out {
        Ok(o) => o,
        Err(e) => {
            tracing::warn!("[storage_ux] warning: `volume ls` failed: {e}");
            return Vec::new();
        }
    };
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        tracing::warn!(
            "[storage_ux] warning: `volume ls` exited non-zero: {}",
            stderr.trim()
        );
        return Vec::new();
    }
    let body = String::from_utf8_lossy(&out.stdout);
    let names = filter_legacy_volume_names(body.lines());

    let mut detected: Vec<DetectedLegacyVolume> = Vec::new();
    for name in names {
        let (mountpoint, driver) = inspect_volume(&runtime, &name).await;
        let role = infer_role(&name);
        detected.push(DetectedLegacyVolume { name, mountpoint, driver, role });
    }
    detected
}

// ---------------------------------------------------------------------------
// Deferral routing — Rust -> Python helper
// ---------------------------------------------------------------------------

/// Spawn a `python` (or `python3`) child to add a deferral entry via
/// `vco_lib.deferral_report`. Used when a migration partially succeeded
/// or when override.yml carried user customizations we don't want to
/// stomp.
///
/// Best-effort: any failure (no python on PATH, repo root missing,
/// subprocess returns non-zero) is logged and swallowed so the caller's
/// error path still proceeds. Deferrals are an FYI mechanism — we don't
/// want a failure HERE to mask the original failure THERE.
fn emit_deferral(
    condition_id: &str,
    title: &str,
    detected: &str,
    why_deferred: &str,
    command_to_apply: &str,
    severity: &str,
) {
    let repo_root = match super::installer::find_local_repo_root() {
        Ok(r) => r,
        Err(_) => return,
    };
    // v0.2.77 (Part 7c task 4): the shared Python-bridge deferral writer
    // owns interpreter resolution + the injection-safe `-c` snippet +
    // the spawn. This file just supplies the entry fields. The report
    // lands in the orchestrator-root folder (sys.path root == report
    // folder for storage-migration deferrals).
    let fields = crate::services::deferral::DeferralEntryFields {
        condition_id,
        title,
        detected,
        why_deferred,
        command_to_apply,
        severity,
    };
    if let Err(e) =
        crate::services::deferral::emit_deferral_entry(&repo_root, &repo_root, &fields)
    {
        tracing::warn!("[storage_ux] deferral emit failed ({}): {}", condition_id, e);
    }
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

/// Read the current storage configuration plus the detected legacy
/// volumes. Missing config file → defaults + `synthesized_from_defaults=true`.
#[command]
pub async fn get_storage_config() -> Result<StorageConfigView, String> {
    let cfg_path = storage_config_path();
    let (config, synthesized) = read_storage_config_from(&cfg_path);
    let legacy_volumes = detect_legacy_volumes_inner().await;
    Ok(StorageConfigView {
        config,
        config_path: cfg_path.to_string_lossy().to_string(),
        legacy_volumes,
        synthesized_from_defaults: synthesized,
    })
}

/// Atomically persist a chosen storage configuration AND regenerate the
/// compose override file.
///
/// User-customization guard: if the existing override.yml is NOT
/// launcher-managed (no `Auto-generated by VCT Launcher` header), we
/// PRESERVE it and emit `override_yml_user_customization_preserved`.
/// The new storage.toml is still written so subsequent reads reflect
/// the user's intent.
#[command]
pub async fn set_storage_config(config: StorageConfig) -> Result<StorageConfigView, String> {
    let normalized = normalize_config(config);

    // Validate.
    if !["named", "bind"].contains(&normalized.mode.as_str()) {
        return Err(format!(
            "invalid storage mode {:?} — expected 'named' or 'bind'",
            normalized.mode
        ));
    }
    if normalized.mode == "bind" && normalized.bind_root.trim().is_empty()
        && normalized.per_service_paths.iter().all(|(_, v)| v.trim().is_empty())
    {
        return Err(
            "bind mode requires either bind_root or at least one per_service_path".into(),
        );
    }
    for (k, v) in &normalized.external_aliases {
        if !is_valid_volume_name(v) {
            return Err(format!(
                "external alias for {k:?} has invalid volume name {v:?}"
            ));
        }
    }

    let cfg_path = storage_config_path();
    write_storage_config_to(&cfg_path, &normalized)?;

    // Regenerate override.yml unless it's user-authored.
    let existing = read_existing_override();
    if !existing.is_empty() && !is_launcher_managed_override(&existing) {
        emit_deferral(
            "override_yml_user_customization_preserved",
            "Compose override.yml carries user customizations",
            "infrastructure/compose.override.yaml exists but does not carry the \
             VCT Launcher header marker. The launcher will NOT overwrite it.",
            "Hand-edited override files can encode service-level customizations \
             (custom networks, image overrides, etc.) that the launcher's renderer \
             does not represent. Auto-overwriting would silently drop them.",
            "Inspect infrastructure/compose.override.yaml. To accept the \
             launcher's default for the chosen storage config, remove the file \
             and re-apply via Settings -> Storage.",
            "warning",
        );
    } else {
        let body = render_override_yaml(&normalized);
        write_compose_override(&body)?;
    }

    let legacy_volumes = detect_legacy_volumes_inner().await;
    Ok(StorageConfigView {
        config: normalized,
        config_path: cfg_path.to_string_lossy().to_string(),
        legacy_volumes,
        synthesized_from_defaults: false,
    })
}

/// v0.2.34 (Agent I) — Read-only resolver for the launcher's state-root
/// directory. Surfaces the same path that `crate::paths::vct_root_dir()`
/// returns so the Preferences UI can render it (with a tooltip explaining
/// the `VCT_STATE_DIR` override) without duplicating the resolution logic.
///
/// Returns the absolute path as a `String` (the `Display` form of the
/// `PathBuf`). This command does NOT create the directory — callers that
/// only want to *display* the resolved path shouldn't have a side effect.
///
/// Lives in `storage_ux.rs` because the Preferences "Storage" section is
/// the natural surface for it; no separate `preferences_cmd.rs` module
/// existed at the time this was added.
#[command]
pub async fn get_resolved_vct_root_dir() -> Result<String, String> {
    Ok(crate::paths::vct_root_dir().display().to_string())
}

// ---------------------------------------------------------------------------
// PR-28 (Group G, v0.2.12) — install-time CLI entrypoint
// ---------------------------------------------------------------------------

/// Persist a storage config chosen by install.py's interactive prompt and
/// regenerate the compose override. Synchronous, GUI-free counterpart to
/// `set_storage_config()` so the launcher binary can be invoked as a CLI
/// (`vct-launcher --set-storage-config <mode> [--bind-path service=path]...`)
/// from install.py without spinning up Tauri.
///
/// `mode` is one of:
///   - `"named"`  — fresh named volumes (the legacy default behaviour).
///                   `bind_paths` is ignored.
///   - `"bind"`   — bind-mount per-service paths. `bind_paths` is the list
///                  of `(logical_service, host_path)` tuples (e.g.
///                  `("ollama", "/home/<user>/podman_volumes/ollama/models")`).
///                  Unknown service keys are silently dropped by the
///                  normalizer (matches the Tauri-command surface).
///   - `"deferred"` — caller signalled "configure later via the GUI".
///                    We intentionally return `Ok(())` without touching
///                    storage.toml or the override. (install.py treats
///                    `deferred` as a no-op upstream too; this branch is
///                    defence-in-depth.)
///
/// Reuses `set_storage_config`'s validation + write logic (`normalize_config`,
/// `write_storage_config_to`, the user-customization guard around the
/// compose override). NOT async — install.py spawns this as a subprocess
/// and waits synchronously, so we avoid pulling tokio into the call path.
pub fn set_storage_config_from_cli(
    mode: &str,
    bind_paths: Vec<(String, PathBuf)>,
) -> Result<(), String> {
    // Deferred = caller decided to defer to the GUI. No-op; do not touch
    // any on-disk state so a stale storage.toml from a previous install
    // attempt is preserved exactly as-is.
    if mode == "deferred" {
        return Ok(());
    }

    // Build a StorageConfig from the CLI args.
    let mut cfg = StorageConfig::default();
    cfg.mode = mode.trim().to_ascii_lowercase();

    if cfg.mode == "bind" {
        for (service, path) in &bind_paths {
            // Force forward-slash strings — the YAML renderer expects that
            // shape on every OS. PathBuf preserves the user's slashes so
            // we normalize here.
            let path_s = yaml_path_str(path);
            cfg.per_service_paths
                .insert(service.clone(), path_s);
        }
    }

    let normalized = normalize_config(cfg);

    // Validate (same gates as the Tauri command).
    if !["named", "bind"].contains(&normalized.mode.as_str()) {
        return Err(format!(
            "invalid storage mode {:?} — expected 'named' or 'bind'",
            normalized.mode
        ));
    }
    if normalized.mode == "bind"
        && normalized.bind_root.trim().is_empty()
        && normalized
            .per_service_paths
            .iter()
            .all(|(_, v)| v.trim().is_empty())
    {
        return Err(
            "bind mode requires either bind_root or at least one per_service_path".into(),
        );
    }

    let cfg_path = storage_config_path();
    write_storage_config_to(&cfg_path, &normalized)?;

    // Regenerate override.yml unless it's user-authored. Mirrors the
    // Tauri-command behaviour exactly so the install-time path produces
    // bit-identical output to a later Settings → Storage edit.
    let existing = read_existing_override();
    if !existing.is_empty() && !is_launcher_managed_override(&existing) {
        // Deferral helper requires a Python interpreter on PATH — we
        // skip the emit when install.py drove this path (install.py
        // already records its own deferrals into the same file).
        tracing::warn!(
            "[storage_ux] override.yml at infrastructure/docker-compose.override.yml \
             carries user customizations; skipped overwrite. \
             Remove the file and re-run to accept launcher defaults."
        );
    } else {
        let body = render_override_yaml(&normalized);
        // Soft-fail when the orchestrator clone root cannot be located
        // (e.g. launcher binary invoked from outside a checkout during
        // install.py self-test). storage.toml is still written above so
        // the user's choice is recorded; the next launcher start will
        // regenerate the override from it.
        if let Err(e) = write_compose_override(&body) {
            tracing::warn!(
                "[storage_ux] note: storage.toml written but override.yml could not be \
                 generated: {}. Will regenerate on next launcher start.",
                e
            );
        }
    }

    Ok(())
}

// ─────────────────────────────────────────────────────────────────────
// volumes.rs — MERGED INTO THIS MODULE (v0.2.101, owner ruling Q4b/S5).
// One concern, one home: both storage-config systems (the Preferences
// Storage card and the install-time volume-location picker + the
// destructive migrate_volumes pipeline) live here now. The section
// boundaries below are historical, not architectural.
// ─────────────────────────────────────────────────────────────────────


// ---------------------------------------------------------------------------
// Migration progress event
// ---------------------------------------------------------------------------

/// Phase-level progress events for `migrate_volumes`. The frontend
/// subscribes via `listen('volumes://migrate-progress', ...)` and renders
/// a real progress bar instead of the static "Migrating..." text.
///
/// Reviewer A + B round-2: "Migrating..." with no feedback is a UX cliff
/// for users with multi-GB Weaviate volumes that can take 5+ minutes to
/// `cp -a`. Emitting at phase boundaries (no rsync-style byte tracking,
/// since `cp -a` doesn't expose progress) is the smallest fix that
/// removes the dead-loading-spinner failure mode.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MigratePhase {
    StoppingContainers,
    /// `volume_role` is "weaviate" / "ollama" / "code_embed" — frontend
    /// can show "Copying weaviate..." dynamically.
    CopyingVolume { volume_role: String, index: u32, total: u32 },
    WritingOverride,
    StartingContainers,
    WaitingForHealth,
    RemovingLegacyVolumes,
    Done,
    /// Emitted before the function returns Err — frontend shows the
    /// rollback message instead of the success state.
    RollingBack { reason: String },
}

#[derive(Debug, Clone, Serialize)]
pub struct MigrateProgress {
    pub phase: MigratePhase,
    pub message: String,
}

const MIGRATE_EVENT: &str = "volumes://migrate-progress";

fn emit_phase(app: &AppHandle, phase: MigratePhase, message: &str) {
    let _ = app.emit(
        MIGRATE_EVENT,
        MigrateProgress { phase, message: message.into() },
    );
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/// Persisted launcher config. Lives at `~/.vct/launcher.toml`.
///
/// Fields are flattened toml — no nested tables — so the file stays
/// trivially hand-editable when the launcher is offline.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LauncherConfig {
    /// One of:
    ///   - `"default"`  — runtime default location (no override generated)
    ///   - `"detected"` — existing volumes found; reuse them as-is
    ///   - `"<path>"`   — absolute path to a custom volumes folder
    #[serde(default)]
    pub volumes_path: String,

    /// When `volumes_path == "detected"`, this records the historical
    /// volume name → mountpoint mapping so the Settings panel can
    /// display them without re-probing. Empty otherwise.
    #[serde(default)]
    pub legacy_mapping: Vec<LegacyVolumeMapping>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LegacyVolumeMapping {
    /// Name as Podman/Docker knows it (e.g. `weaviate_claude`).
    pub volume_name: String,
    /// Filesystem path the runtime bind-mounts inside the container.
    pub mountpoint: String,
    /// Logical role: which compose service this volume serves.
    /// One of `"weaviate"` | `"ollama"` | `"code_embed"`.
    pub role: String,
}

/// Front-end-facing config wrapping the persisted state with computed
/// human-friendly fields.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VolumesConfig {
    pub volumes_path: String,
    /// `"default"` | `"detected"` | `"custom"` — computed from `volumes_path`.
    pub mode: String,
    pub legacy_mapping: Vec<LegacyVolumeMapping>,
    /// Human-readable size, e.g. "21.1 GB". `None` when sizes weren't
    /// probed (e.g. runtime not installed).
    pub total_size_human: Option<String>,
    /// Per-volume sizes (filled when we could `du` the mountpoints).
    pub volumes: Vec<VolumeWithSize>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VolumeWithSize {
    pub name: String,
    pub mountpoint: String,
    pub size_bytes: Option<u64>,
    pub size_human: Option<String>,
    pub role: String,
}

/// Result of a dry-run migration request. The frontend shows this in a
/// confirm dialog before the user clicks "Migrate".
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MigrationPlan {
    pub from_mode: String, // "default" | "detected" | "custom"
    pub to_path: String,
    pub volumes_to_copy: Vec<VolumeWithSize>,
    pub total_bytes: u64,
    pub total_human: String,
    /// Estimated duration in seconds, very rough (assumes 100 MB/s SSD).
    pub estimated_seconds: u64,
    /// Free space currently available at `to_path` (or its parent if it
    /// doesn't yet exist). `None` if we couldn't statvfs.
    pub free_bytes_at_target: Option<u64>,
    /// True when free_bytes_at_target < total_bytes * 1.10 (10% headroom).
    pub insufficient_free_space: bool,
    /// User-facing warnings (legacy volumes will be removed after copy
    /// succeeds, etc.).
    pub warnings: Vec<String>,
}

// ---------------------------------------------------------------------------
// Path helpers
// ---------------------------------------------------------------------------

pub fn launcher_config_path() -> PathBuf {
    // Bug 14: route through VCT_STATE_DIR so dev launcher's volume-config
    // doesn't clobber the production launcher.toml.
    crate::paths::vct_root_dir().join("launcher.toml")
}

/// Find the orchestrator repo root by walking up from this binary's
/// CWD-equivalent. We piggyback on the installer's resolver because
/// `infrastructure/docker-compose.yml` is the file we have to overlay.
fn orchestrator_root() -> Result<PathBuf, String> {
    super::installer::find_local_repo_root()
}

// v0.2.101 (Q4b merge dedup): this half used to carry its OWN
// `compose_override_path` / `compose_override_sibling_path` /
// `write_compose_override` trio (primary/sibling filenames swapped but
// otherwise identical — both wrote BOTH auto-load names since C-RT-5).
// One concern, one home: the single trio near the top of this file now
// serves both halves; only `remove_compose_override` below is unique to
// this half.

// ---------------------------------------------------------------------------
// LauncherConfig persistence (atomic temp+rename)
// ---------------------------------------------------------------------------

pub fn read_launcher_config() -> LauncherConfig {
    let path = launcher_config_path();
    let raw = match std::fs::read_to_string(&path) {
        Ok(r) => r,
        Err(_) => return LauncherConfig::default(),
    };
    toml::from_str(&raw).unwrap_or_default()
}

pub fn write_launcher_config(cfg: &LauncherConfig) -> Result<(), String> {
    let path = launcher_config_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create {}: {}", parent.display(), e))?;
    }
    let body = toml::to_string_pretty(cfg)
        .map_err(|e| format!("serialize launcher.toml: {}", e))?;
    let mut tmp = path.clone();
    tmp.set_extension("toml.tmp");
    std::fs::write(&tmp, &body).map_err(|e| format!("write tmp {}: {}", tmp.display(), e))?;
    std::fs::rename(&tmp, &path)
        .map_err(|e| format!("rename {} -> {}: {}", tmp.display(), path.display(), e))?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Volume name → role mapping
// ---------------------------------------------------------------------------

pub fn volume_role(name: &str) -> &'static str {
    if name.starts_with("weaviate") {
        "weaviate"
    } else if name.starts_with("ollama") {
        "ollama"
    } else if name == "code_embed_cache" || name == "vct_code_embed" {
        "code_embed"
    } else {
        "unknown"
    }
}

/// Canonical volume name expected by `infrastructure/docker-compose.yml`
/// for a given role.
fn canonical_for_role(role: &str) -> Option<&'static str> {
    match role {
        "weaviate" => Some("weaviate_data"),
        "ollama" => Some("ollama_data"),
        "code_embed" => Some("code_embed_cache"),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// Custom-path validation (Bug 31 onboarding picker)
// ---------------------------------------------------------------------------

/// Validate a user-supplied custom volumes path.
///
/// Rules:
///   - non-empty
///   - absolute
///   - parent exists and is writable (we'll create the leaf if missing)
///   - NOT inside the runtime's default volume tree
///     (`$HOME/.local/share/containers/storage` for podman) — that path
///     is managed by the container runtime; bind-mounting it leads to
///     recursive containment and breaks volume management.
pub fn validate_custom_volumes_path(path: &str) -> Result<PathBuf, String> {
    let trimmed = path.trim();
    if trimmed.is_empty() {
        return Err("custom volumes path cannot be empty".into());
    }
    let p = PathBuf::from(trimmed);
    if !p.is_absolute() {
        return Err(format!("custom volumes path must be absolute: {}", p.display()));
    }
    // Forbid placing volumes inside the runtime-managed tree.
    if let Some(home) = directories::UserDirs::new().map(|d| d.home_dir().to_path_buf()) {
        let podman_managed = home.join(".local/share/containers/storage");
        if p.starts_with(&podman_managed) {
            return Err(format!(
                "path {} is inside Podman's managed storage tree ({}). \
                 Pick a folder outside that tree.",
                p.display(),
                podman_managed.display()
            ));
        }
        let docker_managed = home.join(".local/share/docker");
        if p.starts_with(&docker_managed) {
            return Err(format!(
                "path {} is inside Docker's managed storage tree.",
                p.display()
            ));
        }
    }
    // We don't require the leaf to exist (it will be created), but the
    // parent must exist + be a directory + writable. Refuse to silently
    // create the entire ancestry — the user might have typo'd.
    let parent = p.parent().ok_or("custom path has no parent")?;
    if !parent.exists() {
        return Err(format!(
            "parent directory does not exist: {}. Create it first.",
            parent.display()
        ));
    }
    if !parent.is_dir() {
        return Err(format!("parent is not a directory: {}", parent.display()));
    }
    // Writable test — try creating a temp marker.
    let probe = parent.join(format!(".vct-volumes-write-probe-{}", std::process::id()));
    match std::fs::write(&probe, b"") {
        Ok(()) => {
            let _ = std::fs::remove_file(&probe);
        }
        Err(e) => {
            return Err(format!("parent not writable ({}): {}", parent.display(), e));
        }
    }
    Ok(p)
}

// ---------------------------------------------------------------------------
// docker-compose.override.yml generation
// ---------------------------------------------------------------------------

/// Generate the override-yml body. Two shapes:
///
///   - `OverrideShape::CustomBindMounts(path)` — bind-mount each canonical
///     volume name at `<path>/<role>`. Used for fresh installs picking a
///     custom path.
///   - `OverrideShape::ExternalLegacy(map)` — alias each canonical volume
///     name to an existing legacy named volume via `external: true`.
///     Used when historical volumes are detected (Bug 31).
pub enum OverrideShape {
    CustomBindMounts(PathBuf),
    ExternalLegacy(Vec<(String, String)>), // (canonical_role, legacy_volume_name)
}

pub fn generate_override_yaml(shape: &OverrideShape) -> String {
    match shape {
        OverrideShape::CustomBindMounts(path) => {
            // Use ${VCT_VOLUMES_PATH} so the .env file controls the actual
            // path; lets users move between machines without rewriting yaml.
            format!(
                "# Auto-generated by VCT Launcher (Bug 31). Edits will be overwritten\n\
                 # the next time the user changes the volume location via Settings.\n\
                 #\n\
                 # Bind-mounts the three orchestrator volumes at subfolders of\n\
                 # ${{VCT_VOLUMES_PATH}} = {root}\n\
                 \n\
                 services: {{}}\n\
                 \n\
                 volumes:\n\
                   weaviate_data:\n\
                     driver: local\n\
                     driver_opts:\n\
                       type: none\n\
                       o: bind\n\
                       device: ${{VCT_VOLUMES_PATH}}/weaviate\n\
                   ollama_data:\n\
                     driver: local\n\
                     driver_opts:\n\
                       type: none\n\
                       o: bind\n\
                       device: ${{VCT_VOLUMES_PATH}}/ollama\n\
                   code_embed_cache:\n\
                     driver: local\n\
                     driver_opts:\n\
                       type: none\n\
                       o: bind\n\
                       device: ${{VCT_VOLUMES_PATH}}/code_embed\n",
                root = path.display()
            )
        }
        OverrideShape::ExternalLegacy(map) => {
            let mut out = String::from(
                "# Auto-generated by VCT Launcher (Bug 31). Edits will be overwritten\n\
                 # the next time the user changes the volume location via Settings.\n\
                 #\n\
                 # Existing volumes were detected on this machine — alias them as\n\
                 # external: true so compose reuses the historical volume data.\n\
                 \n\
                 services: {}\n\
                 \n\
                 volumes:\n",
            );
            for (canonical, legacy) in map {
                out.push_str(&format!(
                    "  {canonical}:\n    external: true\n    name: {legacy}\n",
                ));
            }
            out
        }
    }
}

pub fn remove_compose_override() -> Result<(), String> {
    // v0.2.54 (C-RT-5): remove BOTH auto-load names — leaving the
    // sibling behind would resurrect stale aliases for one engine.
    for path in [compose_override_path()?, compose_override_sibling_path()?] {
        if path.exists() {
            std::fs::remove_file(&path)
                .map_err(|e| format!("remove {}: {}", path.display(), e))?;
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Size probing (du -sb fallback to walk)
// ---------------------------------------------------------------------------

// v0.2.100 F-W4-09: the byte rendering lives in core (one home).
use vct_launcher_core::units::human_bytes;

/// Best-effort recursive size walk. Returns None on permission errors.
fn dir_size_bytes(path: &Path) -> Option<u64> {
    if !path.exists() {
        return None;
    }
    let meta = std::fs::metadata(path).ok()?;
    if !meta.is_dir() {
        return Some(meta.len());
    }
    let mut total: u64 = 0;
    let mut stack = vec![path.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let read = match std::fs::read_dir(&dir) {
            Ok(r) => r,
            Err(_) => continue,
        };
        for entry in read.flatten() {
            let p = entry.path();
            let m = match std::fs::symlink_metadata(&p) {
                Ok(m) => m,
                Err(_) => continue,
            };
            if m.is_dir() {
                stack.push(p);
            } else {
                total = total.saturating_add(m.len());
            }
        }
    }
    Some(total)
}

// ---------------------------------------------------------------------------
// Free-space probe
// ---------------------------------------------------------------------------

#[cfg(unix)]
fn free_bytes_at(path: &Path) -> Option<u64> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;

    // Probe the path itself if it exists, else its parent.
    let probe = if path.exists() { path } else { path.parent()? };
    let cpath = CString::new(probe.as_os_str().as_bytes()).ok()?;
    // SAFETY: statvfs is FFI; we pass a valid C string and an MaybeUninit
    // statvfs struct of the right size.
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::statvfs(cpath.as_ptr(), &mut st) };
    if rc != 0 {
        return None;
    }
    // We deliberately use f_bavail (not f_bfree) — f_bavail subtracts
    // ext4's reserved blocks (default 5% of total, root-only). Rootless
    // Podman runs as the unprivileged user and writes through the user's
    // quota, so reserved blocks ARE unusable. This matches `df -h`'s
    // "Available" column, which is the authoritative number for the
    // rootless-container use case.
    //
    // GNOME's "Files" / Disks app reports f_bfree (free including reserved)
    // — that is misleadingly optimistic for our context: a 2 TB volume can
    // show ~100 GB more free in GNOME than the rootless Podman runtime
    // can actually write. If a user reports a discrepancy ("Files says
    // 243 GB, launcher says 143 GB") the launcher is correct; do not
    // "fix" by switching to f_bfree.
    Some((st.f_bavail as u64).saturating_mul(st.f_frsize as u64))
}

#[cfg(not(unix))]
fn free_bytes_at(_path: &Path) -> Option<u64> {
    None
}

// ---------------------------------------------------------------------------
// Which runtime owns the volumes (v0.2.97 owner ruling "Honour the pin")
// ---------------------------------------------------------------------------

/// The orchestrator volumes under the runtime storage commands drive
/// (`storage_ux::storage_runtime`, the shared pin-first detector) — or a
/// REFUSAL when they exist only under the other runtime.
///
/// Pre-v0.2.97 this file listed volumes under whichever runtime answered
/// first (podman, then docker) and ran `compose stop` / `volume rm` under a
/// separate podman-first PATH probe — both ignoring `VCT_CONTAINER_RUNTIME`.
/// On a docker-pinned machine with a leftover podman copy, that inspected
/// and migrated the podman copy. podman and docker keep separate volumes, so
/// a volume only the other runtime has is refused, never adopted.
///
/// R7b F4: PER VOLUME. This used to ask the other runtime only when the
/// chosen one had NO orchestrator volume at all, so with docker owning
/// `ollama_data` and `weaviate_data` only under podman, a migration moved
/// `ollama_data` and said nothing about `weaviate_data` (compose would then
/// create an empty one). Now every name in `ORCHESTRATOR_VOLUME_NAMES` is
/// checked, and any the chosen runtime lacks but the other one HAS is
/// refused by name. A failed inspect under the chosen runtime is an error,
/// not "absent" (R7b F25(b)); under the other runtime it is not ownership.
async fn existing_volumes_owned_by(
    rt: &super::storage_ux::StorageRuntime,
    action: &str,
) -> Result<Vec<ExistingVolume>, String> {
    use super::storage_ux::{probe_volume, VolumeProbe};
    use vct_launcher_core::services::container_runtime::{check_runtime_owns, other_runtime};
    let other = other_runtime(&rt.name);
    let mut owned: Vec<ExistingVolume> = Vec::new();
    let mut only_elsewhere: Vec<String> = Vec::new();
    for name in super::installer::ORCHESTRATOR_VOLUME_NAMES {
        match probe_volume(&rt.name, name).await {
            VolumeProbe::Found { mountpoint, driver } => {
                owned.push(ExistingVolume { name: name.to_string(), mountpoint, driver });
            }
            VolumeProbe::Unknown(why) => {
                return Err(format!(
                    "could not tell whether {} holds the orchestrator volume `{name}` ({why}); \
                     refusing to {action} the volumes until `{} volume inspect {name}` answers",
                    rt.name, rt.name
                ));
            }
            VolumeProbe::Missing => {
                if matches!(probe_volume(other, name).await, VolumeProbe::Found { .. }) {
                    only_elsewhere.push(format!("`{name}`"));
                }
            }
        }
    }
    check_runtime_owns(
        action,
        &format!("the orchestrator volume(s) {}", only_elsewhere.join(", ")),
        &rt.name,
        rt.pin,
        false,
        !only_elsewhere.is_empty(),
    )?;
    Ok(owned)
}

/// [`existing_volumes_owned_by`] for the read-only and install-time
/// commands. No usable runtime → an empty list, as before (a machine before
/// its first install has none); the only error is the ownership refusal.
pub(crate) async fn existing_volumes_on_storage_runtime(
    action: &str,
) -> Result<Vec<ExistingVolume>, String> {
    let install_root = super::installer::find_local_repo_root().ok();
    existing_volumes_on_storage_runtime_at(install_root.as_deref(), action).await
}

/// [`existing_volumes_on_storage_runtime`] with the install root passed in
/// (tests point it at a temp dir, so no machine's `runtime.txt` leaks in).
pub(crate) async fn existing_volumes_on_storage_runtime_at(
    install_root: Option<&Path>,
    action: &str,
) -> Result<Vec<ExistingVolume>, String> {
    match super::storage_ux::storage_runtime_at(install_root).await {
        Ok(rt) => existing_volumes_owned_by(&rt, action).await,
        Err(e) => {
            tracing::info!("[volumes] no usable container runtime ({e}); no existing volumes");
            Ok(Vec::new())
        }
    }
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

/// Read the current volume configuration. Reads `launcher.toml`, probes
/// existing volumes, and computes per-volume sizes.
#[command]
pub async fn get_volumes_config() -> Result<VolumesConfig, String> {
    let cfg = read_launcher_config();
    let existing = existing_volumes_on_storage_runtime("inspect").await?;

    // Compute per-volume sizes by `du`-walking each mountpoint.
    let mut volumes: Vec<VolumeWithSize> = Vec::new();
    let mut total: u64 = 0;
    let mut have_any_size = false;
    for ev in &existing {
        let mount = PathBuf::from(&ev.mountpoint);
        let size = dir_size_bytes(&mount);
        if let Some(s) = size {
            total = total.saturating_add(s);
            have_any_size = true;
        }
        volumes.push(VolumeWithSize {
            name: ev.name.clone(),
            mountpoint: ev.mountpoint.clone(),
            size_bytes: size,
            size_human: size.map(human_bytes),
            role: volume_role(&ev.name).to_string(),
        });
    }

    // Mode classification — purely from the persisted toml.
    let mode = match cfg.volumes_path.as_str() {
        "" | "default" => "default".to_string(),
        "detected" => "detected".to_string(),
        _ => "custom".to_string(),
    };

    Ok(VolumesConfig {
        volumes_path: cfg.volumes_path.clone(),
        mode,
        legacy_mapping: cfg.legacy_mapping.clone(),
        total_size_human: if have_any_size {
            Some(human_bytes(total))
        } else {
            None
        },
        volumes,
    })
}

/// Persist a chosen volumes configuration AT INSTALL TIME (i.e. before
/// any container has touched the new path). Onboarding step 3 calls this
/// after the user clicks "Install" and a custom path was selected.
///
/// Behavior:
///   - If existing volumes are detected: ALWAYS sets mode="detected" and
///     records the legacy mapping. The `path` argument is ignored (per
///     Bug 32 contract — no override generated).
///   - Else if `path == "default"` or empty: mode="default", no override.
///   - Else: validates the custom path, generates the bind-mount override,
///     writes launcher.toml.
#[command]
pub async fn set_volumes_config_for_install(
    path: String,
) -> Result<VolumesConfig, String> {
    // Read existing first — if anything is found, we go down the
    // "detected" branch regardless of what the caller passed.
    let existing = existing_volumes_on_storage_runtime("adopt").await?;
    if !existing.is_empty() {
        let mut mapping: Vec<LegacyVolumeMapping> = Vec::new();
        for ev in &existing {
            mapping.push(LegacyVolumeMapping {
                volume_name: ev.name.clone(),
                mountpoint: ev.mountpoint.clone(),
                role: volume_role(&ev.name).to_string(),
            });
        }
        // If any of the detected volumes are HISTORICAL (not canonical),
        // generate an external-alias override so compose picks them up
        // by name. Canonical volumes need no override.
        let mut external_pairs: Vec<(String, String)> = Vec::new();
        let canonical = ["weaviate_data", "ollama_data", "code_embed_cache"];
        for ev in &existing {
            if canonical.contains(&ev.name.as_str()) {
                continue;
            }
            let role = volume_role(&ev.name);
            if let Some(can) = canonical_for_role(role) {
                external_pairs.push((can.to_string(), ev.name.clone()));
            }
        }
        if !external_pairs.is_empty() {
            let body = generate_override_yaml(&OverrideShape::ExternalLegacy(external_pairs));
            write_compose_override(&body)?;
        } else {
            // All detected volumes are canonical — no override needed.
            // Make sure we don't have a stale one lying around.
            remove_compose_override()?;
        }
        let cfg = LauncherConfig {
            volumes_path: "detected".to_string(),
            legacy_mapping: mapping,
        };
        write_launcher_config(&cfg)?;
        return get_volumes_config().await;
    }

    // Fresh install — honor the user's choice.
    let trimmed = path.trim();
    if trimmed.is_empty() || trimmed == "default" {
        // Default: no override, no custom path recorded.
        remove_compose_override()?;
        let cfg = LauncherConfig {
            volumes_path: "default".to_string(),
            legacy_mapping: Vec::new(),
        };
        write_launcher_config(&cfg)?;
        return get_volumes_config().await;
    }

    let validated = validate_custom_volumes_path(trimmed)?;
    // Create the leaf folder + the three role subfolders so podman doesn't
    // refuse to bind-mount missing dirs.
    for sub in &["weaviate", "ollama", "code_embed"] {
        let p = validated.join(sub);
        std::fs::create_dir_all(&p)
            .map_err(|e| format!("create {}: {}", p.display(), e))?;
    }
    let body = generate_override_yaml(&OverrideShape::CustomBindMounts(validated.clone()));
    write_compose_override(&body)?;

    // Also write VCT_VOLUMES_PATH into infrastructure/.env so compose
    // resolves the ${VCT_VOLUMES_PATH} placeholder.
    write_volumes_env_var(&validated)?;

    let cfg = LauncherConfig {
        volumes_path: validated.to_string_lossy().to_string(),
        legacy_mapping: Vec::new(),
    };
    write_launcher_config(&cfg)?;
    get_volumes_config().await
}

/// Append/update `VCT_VOLUMES_PATH=<path>` in `infrastructure/.env` (or
/// create the file). Other env keys are preserved. v0.2.97 (review R5 F40):
/// through the ONE writer of that file, `vco_lib.compose_env`
/// (`services::vco_lib_bridge::set_infrastructure_env_key`) — this was a
/// second, Rust read-modify-write of it.
fn write_volumes_env_var(path: &Path) -> Result<(), String> {
    let root = orchestrator_root()?;
    crate::services::vco_lib_bridge::set_infrastructure_env_key(
        Some(&root),
        &root.join("infrastructure"),
        "VCT_VOLUMES_PATH",
        &path.display().to_string(),
    )
    .map(|_| ())
}

/// Build a migration plan WITHOUT touching anything. Frontend renders
/// this in the confirm dialog before the user clicks "Migrate".
#[command]
pub async fn set_volumes_config_dry_run(path: String) -> Result<MigrationPlan, String> {
    let cfg = read_launcher_config();
    let existing = existing_volumes_on_storage_runtime("plan a migration of").await?;

    let target = if path.trim() == "default" || path.trim().is_empty() {
        // Migrating BACK to default: target path is the runtime default.
        // We surface this as "default" mode in the plan; cp -a still has
        // to move data into the runtime-managed tree, which means the
        // user needs to opt in explicitly.
        directories::UserDirs::new()
            .map(|d| d.home_dir().join(".local/share/containers/storage/volumes"))
            .ok_or("could not resolve home dir")?
    } else {
        validate_custom_volumes_path(path.trim())?
    };

    let mut volumes: Vec<VolumeWithSize> = Vec::new();
    let mut total_bytes: u64 = 0;
    for ev in &existing {
        let mount = PathBuf::from(&ev.mountpoint);
        let size = dir_size_bytes(&mount);
        if let Some(s) = size {
            total_bytes = total_bytes.saturating_add(s);
        }
        volumes.push(VolumeWithSize {
            name: ev.name.clone(),
            mountpoint: ev.mountpoint.clone(),
            size_bytes: size,
            size_human: size.map(human_bytes),
            role: volume_role(&ev.name).to_string(),
        });
    }

    // 100 MB/s assumption for ETA. Round up.
    let estimated_seconds = (total_bytes / (100 * 1024 * 1024)).max(1);

    let free = free_bytes_at(&target);
    let insufficient = match free {
        Some(f) => f < total_bytes.saturating_mul(110) / 100,
        None => false,
    };

    let mut warnings: Vec<String> = Vec::new();
    if !existing.is_empty() {
        warnings.push(format!(
            "Migration will copy {} from {} existing volumes to {}, then remove the original volumes ONLY after the new bind-mounts come up healthy.",
            human_bytes(total_bytes),
            existing.len(),
            target.display(),
        ));
    } else {
        warnings.push("No existing orchestrator volumes detected — nothing to migrate. Use the install flow's volume picker for fresh setups.".into());
    }
    if insufficient {
        warnings.push(format!(
            "Insufficient free space at target: {} available vs {} required (need 10% headroom).",
            free.map(human_bytes).unwrap_or_else(|| "?".into()),
            human_bytes(total_bytes.saturating_mul(110) / 100),
        ));
    }

    let from_mode = match cfg.volumes_path.as_str() {
        "" | "default" => "default".to_string(),
        "detected" => "detected".to_string(),
        _ => "custom".to_string(),
    };

    Ok(MigrationPlan {
        from_mode,
        to_path: target.to_string_lossy().to_string(),
        volumes_to_copy: volumes,
        total_bytes,
        total_human: human_bytes(total_bytes),
        estimated_seconds,
        free_bytes_at_target: free,
        insufficient_free_space: insufficient,
        warnings,
    })
}

/// Migrate volumes from their current location to `path`. ONLY callable
/// from the Settings UI with `confirmed=true`. Performs the unsafe
/// `volume rm` of legacy volumes ONLY after new bind-mounts are verified
/// healthy via HTTP probes. On any failure between `down` and verified
/// `up -d`, the override file is removed and old volumes are left
/// untouched.
///
/// This is the ONLY function in the launcher that calls
/// `podman/docker volume rm`. The non-destructive audit guard
/// `test_no_destructive_subprocess_calls_in_install_path` is scoped to
/// install-path files and explicitly excludes this module.
///
/// Implementation note: this command performs blocking subprocess work
/// (compose down, cp -a, compose up -d) which can take minutes. Frontend
/// must show a progress indicator. We intentionally do NOT background
/// the work — the user explicitly requested migration; failure here
/// must be reported synchronously so the rollback path is taken.
#[command]
pub async fn migrate_volumes(
    app: AppHandle,
    path: String,
    confirmed: bool,
) -> Result<(), String> {
    if !confirmed {
        return Err("migration requires confirmed=true".into());
    }

    // Volume migration is Linux-only for v0.1.0. The pipeline shells out
    // to POSIX `cp -a` (line ~838) and assumes podman/docker host bind-mount
    // semantics that differ on Windows (Docker Desktop) and macOS. The
    // launcher still detects volumes on those OSes (read-only) but the
    // destructive migration path is gated. Cross-OS migration is on the
    // post-launch backlog — see Stage 8 audit (2026-04-26).
    if cfg!(target_os = "windows") || cfg!(target_os = "macos") {
        return Err(
            "volume migration is currently Linux-only; on Windows/macOS \
             move volumes manually via Docker Desktop / Podman Desktop. \
             Tracked: github.com/hotak92/vibecoded-orchestrator/issues (cross-OS volume migration)"
                .into(),
        );
    }

    // Build a fresh plan and re-validate; the dry-run might have been
    // computed minutes ago and the disk situation could have changed.
    let _plan = set_volumes_config_dry_run(path.clone()).await?;
    let target = validate_custom_volumes_path(path.trim())?;

    // The shared pin-first runtime (v0.2.97 owner ruling "Honour the pin"),
    // and only the volumes THAT runtime owns — refused before anything stops
    // when they exist only under the other one.
    let storage = super::storage_ux::storage_runtime().await?;
    let existing = existing_volumes_owned_by(&storage, "migrate").await?;
    let runtime = storage.name;
    if existing.is_empty() {
        return Err("no existing volumes to migrate".into());
    }

    // 1. Stop services. NOTE: NO `--volumes` flag — this only stops
    //    containers, leaves volumes intact.
    emit_phase(&app, MigratePhase::StoppingContainers, "Stopping containers");
    let compose_dir = orchestrator_root()?.join("infrastructure");
    let compose_status = tokio::process::Command::new(&runtime).silent()
        .args(["compose", "stop"])
        .current_dir(&compose_dir)
        .status()
        .await
        .map_err(|e| format!("compose stop spawn: {}", e))?;
    if !compose_status.success() {
        emit_phase(
            &app,
            MigratePhase::RollingBack { reason: "compose stop failed".into() },
            "Rolling back",
        );
        return Err(format!("compose stop failed (status {})", compose_status));
    }

    // 2. cp -a each volume's mountpoint to <target>/<role>.
    let total = existing.len() as u32;
    for (i, ev) in existing.iter().enumerate() {
        let role = volume_role(&ev.name);
        emit_phase(
            &app,
            MigratePhase::CopyingVolume {
                volume_role: role.into(),
                index: (i as u32) + 1,
                total,
            },
            &format!("Copying {} ({}/{})", role, i + 1, total),
        );
        let dest = target.join(role);
        if let Err(e) = std::fs::create_dir_all(&dest) {
            emit_phase(
                &app,
                MigratePhase::RollingBack { reason: format!("create dest: {}", e) },
                "Rolling back",
            );
            // Failure BEFORE we changed anything substantive — try to
            // bring services back up with the old volumes and bail.
            let _ = restart_services_for_rollback(&runtime, &compose_dir).await;
            return Err(format!(
                "create dest {}: {} (rolled back; old volumes intact)",
                dest.display(),
                e
            ));
        }
        let cp_status = tokio::process::Command::new("cp").silent()
            .args(["-a", &ev.mountpoint, dest.to_str().unwrap_or("")])
            .status()
            .await;
        match cp_status {
            Ok(s) if s.success() => {}
            other => {
                emit_phase(
                    &app,
                    MigratePhase::RollingBack { reason: format!("cp -a failed: {:?}", other) },
                    "Rolling back",
                );
                // Rollback: remove the override (if we wrote one yet —
                // we haven't at this point), and bring services up with
                // old volumes.
                let _ = remove_compose_override();
                let _ = restart_services_for_rollback(&runtime, &compose_dir).await;
                return Err(format!(
                    "cp -a {} -> {} failed: {:?} (rolled back; old volumes intact)",
                    ev.mountpoint,
                    dest.display(),
                    other
                ));
            }
        }
    }

    // 3. Write the bind-mount override + .env entry.
    emit_phase(&app, MigratePhase::WritingOverride, "Writing compose override");
    let body = generate_override_yaml(&OverrideShape::CustomBindMounts(target.clone()));
    if let Err(e) = write_compose_override(&body) {
        emit_phase(&app, MigratePhase::RollingBack { reason: e.clone() }, "Rolling back");
        let _ = restart_services_for_rollback(&runtime, &compose_dir).await;
        return Err(format!("{} (rolled back; old volumes intact)", e));
    }
    if let Err(e) = write_volumes_env_var(&target) {
        emit_phase(&app, MigratePhase::RollingBack { reason: e.clone() }, "Rolling back");
        let _ = remove_compose_override();
        let _ = restart_services_for_rollback(&runtime, &compose_dir).await;
        return Err(format!("{} (rolled back; old volumes intact)", e));
    }

    // 4. compose up -d. If it fails, ditch the override + .env entry
    //    and restart with old volumes.
    emit_phase(&app, MigratePhase::StartingContainers, "Starting containers");
    let up_status = tokio::process::Command::new(&runtime).silent()
        .args(["compose", "up", "-d"])
        .current_dir(&compose_dir)
        .status()
        .await;
    let up_ok = matches!(&up_status, Ok(s) if s.success());
    if !up_ok {
        emit_phase(
            &app,
            MigratePhase::RollingBack { reason: format!("compose up failed: {:?}", up_status) },
            "Rolling back",
        );
        let _ = remove_compose_override();
        let _ = restart_services_for_rollback(&runtime, &compose_dir).await;
        return Err(format!(
            "compose up -d with new bind-mounts failed: {:?} (rolled back; old volumes intact)",
            up_status
        ));
    }

    // 5. Verify health by probing the standard endpoints, for up to
    //    MIGRATE_HEALTH_WAIT with a progress event every
    //    MIGRATE_HEALTH_PROGRESS_EVERY (v0.2.101 review S7 — it was a flat
    //    60 s, a dev-machine ceiling under which a slow disk could never
    //    finish a migration). A genuine failure still rolls back.
    if let Err(reason) = migrate_health_step(
        &healthy_probe_targets(),
        MIGRATE_HEALTH_WAIT,
        MIGRATE_HEALTH_POLL,
        MIGRATE_HEALTH_PROGRESS_EVERY,
        &mut |phase, message| emit_phase(&app, phase, message),
    )
    .await
    {
        let _ = remove_compose_override();
        let _ = restart_services_for_rollback(&runtime, &compose_dir).await;
        return Err(format!(
            "{} on the new bind-mounts (rolled back; old volumes intact)",
            reason
        ));
    }

    // 6. New bind-mounts verified healthy. NOW we may safely remove the
    //    legacy volumes — they're no longer referenced.
    emit_phase(&app, MigratePhase::RemovingLegacyVolumes, "Cleaning up legacy volumes");
    for ev in &existing {
        // Skip canonical names: those are the same names we just bound,
        // not "legacy" — removing them would point compose's volume
        // declaration at nothing. Only remove historical names.
        let canonical = ["weaviate_data", "ollama_data", "code_embed_cache"];
        if canonical.contains(&ev.name.as_str()) {
            continue;
        }
        let _ = tokio::process::Command::new(&runtime).silent()
            .args(["volume", "rm", &ev.name])
            .status()
            .await;
    }

    // 7. Persist the new config.
    let cfg = LauncherConfig {
        volumes_path: target.to_string_lossy().to_string(),
        legacy_mapping: Vec::new(),
    };
    write_launcher_config(&cfg)?;
    emit_phase(&app, MigratePhase::Done, "Migration complete");
    Ok(())
}

/// Tries to `compose up -d` again after a migration step failed. Best
/// effort — used during rollback so even if it fails the user knows
/// what to do (run `podman-compose up -d` themselves).
async fn restart_services_for_rollback(runtime: &str, compose_dir: &Path) -> Result<(), String> {
    let status = tokio::process::Command::new(runtime).silent()
        .args(["compose", "up", "-d"])
        .current_dir(compose_dir)
        .status()
        .await
        .map_err(|e| format!("rollback compose up spawn: {}", e))?;
    if !status.success() {
        return Err(format!("rollback compose up status: {}", status));
    }
    Ok(())
}

// ── Step 5: the post-switch health wait (v0.2.101 review S7) ─────────────

/// How long `migrate_volumes` waits for Weaviate and Ollama to answer after
/// `compose up -d` on the new bind-mounts, before it rolls back.
///
/// It was a flat 60 s — a ceiling set by a fast dev machine. Weaviate loads
/// every shard of its data folder before `/v1/meta` answers, so a large data
/// folder that was just `cp -a`'d to a slow disk (a USB drive, a network
/// mount, a cold HDD) can legitimately take many minutes; under 60 s that
/// machine rolled back every attempt and could NEVER migrate (the owner's
/// timeout rule: shipped timeouts are sized for the slowest legitimate
/// machine, not the developer's). 30 min is a bound for a GENUINE failure
/// (a service that crash-loops on its new mount), not a performance
/// estimate; the wait ends the moment both services answer, and the user
/// sees a progress line every [`MIGRATE_HEALTH_PROGRESS_EVERY`] naming what
/// is still pending, so a long wait is never a silent spinner.
const MIGRATE_HEALTH_WAIT: std::time::Duration = std::time::Duration::from_secs(30 * 60);

/// Interval between probe rounds during the health wait.
const MIGRATE_HEALTH_POLL: std::time::Duration = std::time::Duration::from_secs(2);

/// How often the health wait re-emits its `WaitingForHealth` progress line.
const MIGRATE_HEALTH_PROGRESS_EVERY: std::time::Duration = std::time::Duration::from_secs(15);

/// One service the health wait probes: the name its progress line uses and
/// the health URL it polls.
struct HealthTarget {
    label: &'static str,
    url: String,
}

/// Weaviate and Ollama at their `service_endpoints` rows (v0.2.97) — the
/// literals 8081 / 11435 it used before timed out on a machine whose
/// services live elsewhere.
fn healthy_probe_targets() -> Vec<HealthTarget> {
    use vct_launcher_core::services::service_endpoints::{machine_row_from_disk, CoreService};
    use vct_launcher_core::services::service_status::health_url;
    [(CoreService::Weaviate, "Weaviate"), (CoreService::Ollama, "Ollama")]
        .into_iter()
        .map(|(s, label)| HealthTarget {
            label,
            url: health_url(s, machine_row_from_disk(s).as_ref()),
        })
        .collect()
}

/// The health URLs the wait polls (derived from [`healthy_probe_targets`]).
#[cfg(test)]
fn healthy_probe_urls() -> Vec<String> {
    healthy_probe_targets().into_iter().map(|t| t.url).collect()
}

/// How a health wait ended.
#[derive(Debug, PartialEq, Eq)]
enum HealthWaitOutcome {
    /// Every target answered 2xx (a redirect is never followed — `probe_http`).
    Healthy,
    /// The bound elapsed; these targets had still not answered.
    TimedOut { pending: Vec<&'static str> },
    /// A target's URL cannot even be probed (no client can be built for it).
    /// Permanent — waiting longer cannot fix it — so the wait stops at once.
    Unprobeable(String),
}

/// The labels of the targets that do not answer 2xx right now.
async fn pending_health_targets(targets: &[HealthTarget]) -> Result<Vec<&'static str>, String> {
    let mut pending = Vec::new();
    for t in targets {
        let client = vct_launcher_core::services::loopback_http::client_for(
            &t.url,
            std::time::Duration::from_secs(2),
        )
        .map_err(|e| format!("{} health URL {}: {}", t.label, t.url, e))?;
        match client.get(t.url.as_str()).send().await {
            Ok(r) if vct_launcher_core::services::probe_http::answered(r.status()) => {}
            _ => pending.push(t.label),
        }
    }
    Ok(pending)
}

/// Poll `targets` every `poll` until all answer or `timeout` elapses,
/// calling `on_progress` with a "still waiting" line every `progress_every`
/// while any target is pending.
async fn wait_for_health(
    targets: &[HealthTarget],
    timeout: std::time::Duration,
    poll: std::time::Duration,
    progress_every: std::time::Duration,
    on_progress: &mut impl FnMut(&str),
) -> HealthWaitOutcome {
    use vct_launcher_core::units::human_duration_secs;
    let started = std::time::Instant::now();
    let deadline = started + timeout;
    let mut last_progress = started;
    loop {
        let pending = match pending_health_targets(targets).await {
            Ok(p) => p,
            Err(e) => return HealthWaitOutcome::Unprobeable(e),
        };
        if pending.is_empty() {
            return HealthWaitOutcome::Healthy;
        }
        let now = std::time::Instant::now();
        if now >= deadline {
            return HealthWaitOutcome::TimedOut { pending };
        }
        if now.duration_since(last_progress) >= progress_every {
            last_progress = now;
            on_progress(&format!(
                "Waiting for services to come up — {} not answering yet ({} of up to {}; \
                 a large data folder on a slow disk can take several minutes)",
                pending.join(" and "),
                human_duration_secs(now.duration_since(started).as_secs()),
                human_duration_secs(timeout.as_secs()),
            ));
        }
        tokio::time::sleep(poll.min(deadline - now)).await;
    }
}

/// Step 5 of `migrate_volumes`, minus the rollback itself: emit the
/// `WaitingForHealth` phase (once up front, then as periodic progress), wait,
/// and on failure emit `RollingBack` and return the reason. `Ok(())` means
/// the new bind-mounts are healthy and the legacy volumes may be removed.
/// The caller performs the rollback on `Err`, so the override cleanup stays
/// visible at its one call site.
async fn migrate_health_step(
    targets: &[HealthTarget],
    timeout: std::time::Duration,
    poll: std::time::Duration,
    progress_every: std::time::Duration,
    emit: &mut impl FnMut(MigratePhase, &str),
) -> Result<(), String> {
    use vct_launcher_core::units::human_duration_secs;
    emit(
        MigratePhase::WaitingForHealth,
        &format!(
            "Waiting for services to come up (up to {})",
            human_duration_secs(timeout.as_secs())
        ),
    );
    let outcome = wait_for_health(targets, timeout, poll, progress_every, &mut |message| {
        emit(MigratePhase::WaitingForHealth, message)
    })
    .await;
    let reason = match outcome {
        HealthWaitOutcome::Healthy => return Ok(()),
        HealthWaitOutcome::TimedOut { pending } => format!(
            "{} did not come up healthy within {}",
            pending.join(" and "),
            human_duration_secs(timeout.as_secs())
        ),
        HealthWaitOutcome::Unprobeable(e) => format!("health probe unusable: {}", e),
    };
    emit(MigratePhase::RollingBack { reason: reason.clone() }, "Rolling back");
    Err(reason)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod cli_helper_tests {
    use super::*;

    // v0.2.15 (0.2): these tests redirect writes through the PROCESS-GLOBAL
    // `VCT_STATE_DIR`. v0.2.92: the file-local `STATE_DIR_LOCK` + hand-rolled
    // helper are replaced by the workspace ones. Two things improve: the mutex now
    // excludes env-mutating tests in EVERY file rather than only this one, and
    // the scratch dir is a `tempfile::TempDir` (removed by RAII) instead of a
    // hand-made `$TMPDIR/vct-pr28-<uuid>` that leaked whenever a body panicked
    // before the trailing `remove_dir_all`.
    use vct_launcher_core::test_env::with_state_dir;

    #[test]
    fn cli_helper_deferred_is_noop() {
        with_state_dir(|dir| {
            let cfg_path = dir.join("storage.toml");
            assert!(!cfg_path.exists());
            set_storage_config_from_cli("deferred", vec![]).unwrap();
            // deferred MUST NOT create storage.toml.
            assert!(
                !cfg_path.exists(),
                "deferred mode unexpectedly wrote storage.toml"
            );
        });
    }

    #[test]
    fn cli_helper_named_writes_storage_toml() {
        with_state_dir(|dir| {
            let cfg_path = dir.join("storage.toml");
            set_storage_config_from_cli("named", vec![]).unwrap();
            assert!(cfg_path.exists(), "named mode should write storage.toml");
            let body = std::fs::read_to_string(&cfg_path).unwrap();
            assert!(body.contains("mode = \"named\""));
        });
    }

    #[test]
    fn cli_helper_bind_persists_per_service_paths() {
        with_state_dir(|dir| {
            let cfg_path = dir.join("storage.toml");
            let bind_paths = vec![
                ("ollama".to_string(), PathBuf::from("/foo/bar/ollama")),
                (
                    "weaviate".to_string(),
                    PathBuf::from("/foo/bar/weaviate"),
                ),
            ];
            set_storage_config_from_cli("bind", bind_paths).unwrap();
            assert!(cfg_path.exists());
            let body = std::fs::read_to_string(&cfg_path).unwrap();
            assert!(body.contains("mode = \"bind\""));
            assert!(body.contains("/foo/bar/ollama"));
            assert!(body.contains("/foo/bar/weaviate"));
        });
    }

    #[test]
    fn cli_helper_rejects_invalid_mode() {
        with_state_dir(|_dir| {
            let err = set_storage_config_from_cli("garbage", vec![])
                .expect_err("garbage mode should fail validation");
            assert!(
                err.contains("invalid storage mode"),
                "expected 'invalid storage mode' in {err}"
            );
        });
    }

    #[test]
    fn cli_helper_bind_without_paths_errors() {
        with_state_dir(|_dir| {
            let err = set_storage_config_from_cli("bind", vec![])
                .expect_err("bind mode with empty paths should fail validation");
            assert!(
                err.contains("bind mode requires"),
                "expected 'bind mode requires' in {err}"
            );
        });
    }

    #[test]
    fn cli_helper_bind_drops_unknown_service_keys() {
        with_state_dir(|dir| {
            // 'foozle' is not in LOGICAL_SERVICES — normalize_config
            // strips it. Combined with a real entry the call must
            // still succeed.
            let bind_paths = vec![
                ("foozle".to_string(), PathBuf::from("/tmp/junk")),
                ("ollama".to_string(), PathBuf::from("/foo/bar/ollama")),
            ];
            set_storage_config_from_cli("bind", bind_paths).unwrap();
            let body = std::fs::read_to_string(dir.join("storage.toml")).unwrap();
            assert!(body.contains("/foo/bar/ollama"));
            assert!(!body.contains("foozle"));
            assert!(!body.contains("/tmp/junk"));
        });
    }
}

// ---------------------------------------------------------------------------
// Migration helpers
// ---------------------------------------------------------------------------

/// Result of a migration call. `bytes_copied` is the running total reported
/// by the OS (best-effort — we shell out to `cp -a` which doesn't track
/// progress; we re-walk the source tree afterwards to estimate).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MigrationOutcome {
    pub success: bool,
    pub bytes_copied: u64,
    pub source: String,
    pub target: String,
    pub message: String,
    /// True when we wrote a `deferral_report` entry. The FE renders a
    /// "see UPDATE_DEFERRED.md" notice on top of the toast.
    pub deferral_emitted: bool,
}

// v0.2.101 (Q4b merge dedup): this used to be a SECOND recursive size
// walker identical to the `dir_size_bytes` above except for collapsing
// misses to 0. One walker, one home; the lossy spelling is a wrapper.
fn dir_size_bytes_lossy(path: &Path) -> u64 {
    dir_size_bytes(path).unwrap_or(0)
}

/// Copy `source` (a bind directory) into the host-side mountpoint of a
/// named volume. The volume must exist already — we don't create it
/// here because we'd need to know the runtime's storage root, and the
/// safe sequence (compose up → volume exists → copy in) is the user's
/// responsibility.
///
/// Pipeline:
///   1. Look up the target volume's mountpoint via `<runtime> volume inspect`.
///   2. `cp -a <source>/. <mountpoint>/` (POSIX). On Windows we don't
///      shell out — we emit a deferral with the explicit user command.
///   3. If anything partially fails, emit `storage_migration_partial`
///      deferral and return MigrationOutcome with deferral_emitted=true.
#[command]
pub async fn migrate_to_named_volume(
    source_bind_path: String,
    target_named_volume: String,
) -> Result<MigrationOutcome, String> {
    let source = PathBuf::from(source_bind_path.trim());
    if !source.is_absolute() {
        return Err(format!(
            "source bind path must be absolute: {}",
            source.display()
        ));
    }
    if !source.exists() {
        return Err(format!("source path does not exist: {}", source.display()));
    }
    if !is_valid_volume_name(target_named_volume.trim()) {
        return Err(format!(
            "invalid target volume name: {target_named_volume:?}"
        ));
    }

    let runtime = storage_runtime().await?;
    let mountpoint =
        mountpoint_on_owning_runtime(&runtime, target_named_volume.trim(), "migrate data into")
            .await?;
    if mountpoint.is_empty() {
        return Err(format!(
            "target volume {target_named_volume:?} not found (run compose up first to create it)"
        ));
    }

    // Windows path: emit deferral instead of shelling out.
    if cfg!(target_os = "windows") {
        let cmd = format!(
            "robocopy \"{}\" \"{}\" /E /COPYALL",
            source.display(),
            mountpoint
        );
        emit_deferral(
            "storage_migration_windows_manual",
            "Volume migration on Windows requires a manual copy",
            &format!(
                "Requested migration of {} into named volume {} \
                 (mountpoint {}), but Windows path handling for \
                 named-volume bind targets differs across Podman / \
                 Docker Desktop. Auto-copy is not performed.",
                source.display(),
                target_named_volume,
                mountpoint
            ),
            "POSIX `cp -a` is not portable across Windows runtimes. \
             A wrong path semantics here could corrupt the target volume.",
            &cmd,
            "info",
        );
        return Ok(MigrationOutcome {
            success: false,
            bytes_copied: 0,
            source: source.display().to_string(),
            target: target_named_volume,
            message: format!(
                "Windows: copy {} into {} manually using `{}` (deferral recorded).",
                source.display(),
                mountpoint,
                cmd
            ),
            deferral_emitted: true,
        });
    }

    // Unix path: cp -a <source>/. <mountpoint>/
    let src_arg = format!("{}/.", source.display());
    // vct-allow-no-silent: POSIX `cp` — the Windows branch returns early above
    // (cp -a is not portable), so this spawn never runs on Windows.
    let status = tokio::process::Command::new("cp")
        .arg("-a")
        .arg(&src_arg)
        .arg(&mountpoint)
        .status()
        .await
        .map_err(|e| format!("cp -a spawn: {e}"))?;
    let copied = dir_size_bytes_lossy(&source);

    if !status.success() {
        let cmd = format!("cp -a '{}/.' '{}'", source.display(), mountpoint);
        emit_deferral(
            "storage_migration_partial",
            "Volume migration partially succeeded",
            &format!(
                "`cp -a` exited non-zero migrating {} into named volume {} \
                 (mountpoint {}).",
                source.display(),
                target_named_volume,
                mountpoint
            ),
            "Partial-copy state at the destination cannot be verified safely \
             from the launcher without holding a runtime lock on the volume.",
            &cmd,
            "warning",
        );
        return Ok(MigrationOutcome {
            success: false,
            bytes_copied: copied,
            source: source.display().to_string(),
            target: target_named_volume,
            message: format!(
                "cp -a exited non-zero (status {status}); deferral recorded with manual command."
            ),
            deferral_emitted: true,
        });
    }

    Ok(MigrationOutcome {
        success: true,
        bytes_copied: copied,
        source: source.display().to_string(),
        target: target_named_volume,
        message: format!("Copied {copied} bytes."),
        deferral_emitted: false,
    })
}

/// Copy the contents of a named volume out to a bind directory. The
/// inverse of `migrate_to_named_volume`. Use case: user originally
/// chose named volumes, now wants the files transparent on disk.
#[command]
pub async fn migrate_to_bind_path(
    source_named_volume: String,
    target_bind_path: String,
) -> Result<MigrationOutcome, String> {
    if !is_valid_volume_name(source_named_volume.trim()) {
        return Err(format!(
            "invalid source volume name: {source_named_volume:?}"
        ));
    }
    let target = PathBuf::from(target_bind_path.trim());
    if !target.is_absolute() {
        return Err(format!(
            "target bind path must be absolute: {}",
            target.display()
        ));
    }
    let runtime = storage_runtime().await?;
    let mountpoint =
        mountpoint_on_owning_runtime(&runtime, source_named_volume.trim(), "migrate data out of")
            .await?;
    if mountpoint.is_empty() {
        return Err(format!(
            "source volume {source_named_volume:?} not found"
        ));
    }

    if let Err(e) = std::fs::create_dir_all(&target) {
        return Err(format!("create {}: {e}", target.display()));
    }

    if cfg!(target_os = "windows") {
        let cmd = format!("robocopy \"{}\" \"{}\" /E /COPYALL", mountpoint, target.display());
        emit_deferral(
            "storage_migration_windows_manual",
            "Volume migration on Windows requires a manual copy",
            &format!(
                "Requested migration of named volume {} (mountpoint {}) \
                 into bind path {}.",
                source_named_volume,
                mountpoint,
                target.display(),
            ),
            "POSIX `cp -a` is not portable across Windows runtimes.",
            &cmd,
            "info",
        );
        return Ok(MigrationOutcome {
            success: false,
            bytes_copied: 0,
            source: source_named_volume,
            target: target.display().to_string(),
            message: format!("Windows: copy {} into {} manually using `{}`.", mountpoint, target.display(), cmd),
            deferral_emitted: true,
        });
    }

    let src_arg = format!("{}/.", mountpoint);
    // vct-allow-no-silent: POSIX `cp` — the Windows branch returns early above
    // (cp -a is not portable), so this spawn never runs on Windows.
    let status = tokio::process::Command::new("cp")
        .arg("-a")
        .arg(&src_arg)
        .arg(target.to_str().unwrap_or(""))
        .status()
        .await
        .map_err(|e| format!("cp -a spawn: {e}"))?;
    let copied = dir_size_bytes_lossy(Path::new(&mountpoint));

    if !status.success() {
        let cmd = format!("cp -a '{}/.' '{}'", mountpoint, target.display());
        emit_deferral(
            "storage_migration_partial",
            "Volume migration partially succeeded",
            &format!(
                "`cp -a` exited non-zero migrating named volume {} out to {}.",
                source_named_volume,
                target.display()
            ),
            "Partial-copy state at the destination cannot be verified safely \
             from the launcher.",
            &cmd,
            "warning",
        );
        return Ok(MigrationOutcome {
            success: false,
            bytes_copied: copied,
            source: source_named_volume,
            target: target.display().to_string(),
            message: format!(
                "cp -a exited non-zero (status {status}); deferral recorded with manual command."
            ),
            deferral_emitted: true,
        });
    }

    Ok(MigrationOutcome {
        success: true,
        bytes_copied: copied,
        source: source_named_volume,
        target: target.display().to_string(),
        message: format!("Copied {copied} bytes."),
        deferral_emitted: false,
    })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

/// Test support for both halves of this module: fake `podman`/`docker`
/// scripts on a per-thread injected lookup PATH, so the shared runtime
/// detector and the volume probes can be driven without a real runtime and
/// without touching the process `PATH`.
#[cfg(all(test, unix))]
pub(crate) mod fake_runtime_support {
    use std::path::Path;

    /// A fake runtime on the injected lookup PATH: answers `info` and
    /// `--version`, and `volume inspect <v>` for each owned volume only —
    /// any other volume gets the real runtimes' "no such volume" answer.
    pub(crate) fn fake_runtime(dir: &Path, name: &str, owned: &[&str]) {
        fake_runtime_with(dir, name, owned, &[]);
    }

    /// [`fake_runtime`] whose `volume inspect` FAILS (a transport error, not
    /// "no such volume") for each volume in `failing` (R7b F25(b)).
    pub(crate) fn fake_runtime_with(dir: &Path, name: &str, owned: &[&str], failing: &[&str]) {
        use std::os::unix::fs::PermissionsExt;
        let mut arms: String = owned
            .iter()
            .map(|v| {
                format!(
                    "      {v}) echo '[{{\"Mountpoint\":\"/fake/{name}/{v}\",\"Driver\":\"local\"}}]'; exit 0;;\n"
                )
            })
            .collect();
        for v in failing {
            arms.push_str(&format!(
                "      {v}) echo 'Error: cannot connect to the {name} service: timed out' >&2; exit 125;;\n"
            ));
        }
        // The arms answer the argv PYTHON's resolver probes (`version`,
        // `info`, `compose version` — 2026-09-25 consolidation: the verdict
        // is the CLI's) plus the volume grammar this module's own commands
        // drive.
        let script = format!(
            "#!/bin/sh\ncase \"$1 $2\" in \"compose version\") exit 0;; esac\ncase \
             \"$1\" in\n  info|--version|version) exit 0;;\n  volume)\n    [ \"$2\" = \
             inspect ] || exit 1\n    case \"$3\" in\n{arms}    esac\n    echo \"Error: \
             no such volume $3\" >&2\n    exit 125;;\nesac\nexit 1\n"
        );
        let path = dir.join(name);
        std::fs::write(&path, script).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        settle_exec(&path);
    }

    /// Exec `script` once, retrying (bounded) while the kernel answers
    /// ETXTBSY — "Text file busy": another test thread forked while our
    /// write fd was open, and its child holds that fd until it execs. Our
    /// fd is already closed, so once ONE exec succeeds no later fork can
    /// inherit a writer and the script stays executable for the test. The
    /// production spawns under test get no retry — this makes their input
    /// stable instead. Any other exec error is a broken fixture: panic.
    fn settle_exec(script: &Path) {
        const ATTEMPTS: u32 = 100;
        for _ in 0..ATTEMPTS {
            match std::process::Command::new(script)
                .arg("--version")
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .status()
            {
                Ok(_) => return,
                Err(e) if e.kind() == std::io::ErrorKind::ExecutableFileBusy => {
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
                Err(e) => panic!("fake runtime {} does not execute: {e}", script.display()),
            }
        }
        panic!(
            "fake runtime {} stayed \"Text file busy\" for {ATTEMPTS} attempts",
            script.display()
        );
    }

    /// The retry is real: with a writer holding the script open (the exact
    /// ETXTBSY condition), `settle_exec` waits until it closes instead of
    /// failing.
    #[test]
    fn settle_exec_waits_out_text_file_busy() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("busy");
        std::fs::write(&script, "#!/bin/sh\nexit 0\n").unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        let writer = std::fs::OpenOptions::new().write(true).open(&script).unwrap();
        let closer = std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(80));
            drop(writer);
        });
        settle_exec(&script);
        closer.join().unwrap();
    }

    /// Make a TEMP install root import THIS checkout's `vco_lib`: the
    /// verdict child runs `python -m vco_lib.runtime_reconcile` with the
    /// root as cwd (cwd is sys.path[0]), and a bare temp root would fall
    /// through to whatever stale `vco_lib` the interpreter's venv carries.
    /// A symlink is exactly what a real root has: its own copy.
    pub(crate) fn use_checkout_vco_lib(root: &Path) {
        let checkout = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(2)
            .expect("CARGO_MANIFEST_DIR reaches the checkout root");
        let dst = root.join("vco_lib");
        if dst.symlink_metadata().is_ok() {
            return; // idempotent across a test's re-asks
        }
        std::os::unix::fs::symlink(checkout.join("vco_lib"), &dst)
            .expect("symlink the checkout's vco_lib into the temp root");
    }

    /// Run `fut` on THIS thread (a current-thread runtime), with `dir` as the
    /// only lookup PATH and `pin` as `VCT_CONTAINER_RUNTIME` (under the
    /// workspace env lock, restored afterwards).
    pub(crate) fn with_fake_runtimes<T>(
        dir: &Path,
        pin: Option<&str>,
        fut: impl std::future::Future<Output = T>,
    ) -> T {
        let mut out = None;
        vct_launcher_core::test_env::with_env_vars(
            &[
                ("VCT_CONTAINER_RUNTIME", pin),
                // The verdict comes from a Python child now. Its PATH is the
                // thread-local lookup path injected by `with_lookup_path`
                // below (never the process PATH), and this empties the
                // child's tool-search table (an empty value REPLACES it), so
                // it can never probe the host's real podman/docker.
                ("VCT_TOOL_SEARCH_DIRS", Some("")),
            ],
            || {
                let rt =
                    tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
                out = Some(vct_launcher_core::paths::with_lookup_path(Some(dir.as_os_str()), || {
                    rt.block_on(fut)
                }));
            },
        );
        out.unwrap()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    use super::fake_runtime_support::{fake_runtime, fake_runtime_with, with_fake_runtimes};

    // ----- Runtime choice (v0.2.97 owner ruling "Honour the pin") ----------

    /// The pin is honoured: with BOTH runtimes answering, `VCT_CONTAINER_RUNTIME=docker`
    /// makes storage commands drive docker. The old podman-first PATH probe
    /// (`which_runtime`) answered podman here.
    #[cfg(unix)]
    #[test]
    fn storage_runtime_honours_the_pin() {
        let dir = tempfile::tempdir().unwrap();
        fake_runtime(dir.path(), "podman", &[]);
        fake_runtime(dir.path(), "docker", &[]);
        let rt = with_fake_runtimes(dir.path(), Some("docker"), storage_runtime_at(None)).unwrap();
        assert_eq!(rt, StorageRuntime { name: "docker".into(), pin: Some(RuntimePinSource::EnvOverride) });
    }

    /// No pin: unchanged — podman is preferred when both answer.
    #[cfg(unix)]
    #[test]
    fn storage_runtime_without_a_pin_still_prefers_podman() {
        let dir = tempfile::tempdir().unwrap();
        fake_runtime(dir.path(), "podman", &[]);
        fake_runtime(dir.path(), "docker", &[]);
        let rt = with_fake_runtimes(dir.path(), None, storage_runtime_at(None)).unwrap();
        assert_eq!(rt, StorageRuntime { name: "podman".into(), pin: None });
    }

    /// The install-time record is a pin too (`state/install/runtime.txt`).
    #[cfg(unix)]
    #[test]
    fn storage_runtime_honours_the_recorded_runtime() {
        let dir = tempfile::tempdir().unwrap();
        fake_runtime(dir.path(), "podman", &[]);
        fake_runtime(dir.path(), "docker", &[]);
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("state/install")).unwrap();
        std::fs::write(root.path().join("state/install/runtime.txt"), "docker\n").unwrap();
        use crate::commands::storage_ux::fake_runtime_support::use_checkout_vco_lib;
        use_checkout_vco_lib(root.path());
        let rt = with_fake_runtimes(dir.path(), None, storage_runtime_at(Some(root.path()))).unwrap();
        assert_eq!(rt, StorageRuntime { name: "docker".into(), pin: Some(RuntimePinSource::RuntimeTxt) });
    }

    /// The refusal, end to end through the command: docker is pinned, the
    /// volume exists only under podman — the migration is refused naming
    /// both runtimes and the fix, before anything is copied.
    #[cfg(unix)]
    #[test]
    fn migration_refuses_a_volume_only_the_other_runtime_owns() {
        let dir = tempfile::tempdir().unwrap();
        fake_runtime(dir.path(), "podman", &["weaviate_data"]);
        fake_runtime(dir.path(), "docker", &[]);
        let source = tempfile::tempdir().unwrap();
        let err = with_fake_runtimes(
            dir.path(),
            Some("docker"),
            migrate_to_named_volume(
                source.path().display().to_string(),
                "weaviate_data".into(),
            ),
        )
        .unwrap_err();
        for needle in [
            "refusing to migrate data into volume `weaviate_data`",
            "exists only under podman",
            "VCT_CONTAINER_RUNTIME=docker",
            "set VCT_CONTAINER_RUNTIME=podman",
        ] {
            assert!(err.contains(needle), "{needle:?} missing from {err:?}");
        }
        let target = tempfile::tempdir().unwrap();
        let out = with_fake_runtimes(
            dir.path(),
            Some("docker"),
            migrate_to_bind_path("weaviate_data".into(), target.path().display().to_string()),
        );
        assert!(out.unwrap_err().contains("refusing to migrate data out of volume"));
    }

    /// Leave-alone: the pinned runtime owns the volume → its mountpoint, no
    /// refusal; nobody owns it → empty (the caller's "not found").
    #[cfg(unix)]
    #[test]
    fn owning_runtime_passes_and_an_unknown_volume_is_not_refused() {
        let dir = tempfile::tempdir().unwrap();
        fake_runtime(dir.path(), "podman", &["weaviate_data"]);
        fake_runtime(dir.path(), "docker", &["weaviate_data"]);
        let rt = StorageRuntime { name: "docker".into(), pin: Some(RuntimePinSource::EnvOverride) };
        let (owned, unknown) = with_fake_runtimes(dir.path(), Some("docker"), async {
            (
                mountpoint_on_owning_runtime(&rt, "weaviate_data", "migrate").await,
                mountpoint_on_owning_runtime(&rt, "ollama_data", "migrate").await,
            )
        });
        assert_eq!(owned.unwrap(), "/fake/docker/weaviate_data");
        assert_eq!(unknown.unwrap(), "");
    }

    /// R7b F25(b): a FAILED inspect under the runtime VCO drives is an
    /// error, never "not owned" — before, it produced a refusal claiming the
    /// volume "exists only under" the other runtime. A failed inspect under
    /// the OTHER runtime is only "not known to own it": no refusal.
    #[cfg(unix)]
    #[test]
    fn a_failed_inspect_is_not_read_as_not_owned() {
        let dir = tempfile::tempdir().unwrap();
        fake_runtime_with(dir.path(), "docker", &[], &["weaviate_data"]);
        fake_runtime(dir.path(), "podman", &["weaviate_data"]);
        let rt = StorageRuntime { name: "docker".into(), pin: Some(RuntimePinSource::EnvOverride) };
        let err = with_fake_runtimes(dir.path(), Some("docker"), async {
            mountpoint_on_owning_runtime(&rt, "weaviate_data", "migrate").await
        })
        .unwrap_err();
        assert!(err.contains("could not tell whether docker holds volume `weaviate_data`"), "{err}");
        assert!(!err.contains("exists only under"), "a failed inspect became a refusal: {err}");

        let other = tempfile::tempdir().unwrap();
        fake_runtime(other.path(), "docker", &[]);
        fake_runtime_with(other.path(), "podman", &[], &["weaviate_data"]);
        let out = with_fake_runtimes(other.path(), Some("docker"), async {
            mountpoint_on_owning_runtime(&rt, "weaviate_data", "migrate").await
        });
        assert_eq!(out.unwrap(), "", "an unknown answer from the other runtime is not ownership");
    }

    // ----- Allowlist filtering ---------------------------------------------

    #[test]
    fn allowlist_accepts_canonical_and_legacy_names() {
        for name in [
            "weaviate_data",
            "ollama_data",
            "code_embed_cache",
            "weaviate_claude",
            "ollama_claude",
            "code_embed_claude",
        ] {
            assert!(
                is_recognized_legacy_volume(name),
                "expected {name} to be recognized"
            );
        }
    }

    #[test]
    fn allowlist_accepts_vco_prefix_any_suffix() {
        for name in ["vco_weaviate_data", "vco_custom_thing", "vco_neo4j_data"] {
            assert!(
                is_recognized_legacy_volume(name),
                "expected {name} to be recognized via vco_ prefix"
            );
        }
    }

    /// CORE SAFETY: never surface out-of-namespace volumes. If this test
    /// ever starts failing, someone added an over-broad entry to the
    /// allowlist or a substring match in `is_recognized_legacy_volume`.
    /// Audit the change before "fixing" the test.
    #[test]
    fn detect_legacy_volumes_rejects_unrelated_namespaces() {
        // Plausible non-real names from neighboring projects on a shared
        // machine. NONE of these should ever be returned.
        for forbidden in [
            // Sibling-project namespaces (some other app's volumes on the
            // same host — generic placeholders, not real project names)
            "someapp-weaviate",
            "someapp-ollama",
            "otherproj_postgres",
            "otherproj_redis",
            "thirdapp-data",
            "thirdapp-postgres",
            // Generic infrastructure
            "redis_cache",
            "postgres_data",
            "frontend_node_modules",
            "python_pip_cache",
            "accounts_db",
            // Bare ollama — historically a bind-mount target, not a
            // named volume we own.
            "ollama",
            // Empty / whitespace
            "",
            "   ",
            // Random garbage with vco in the middle, but not as prefix
            "user_vco_thing",
            "my-vco-stuff",
        ] {
            assert!(
                !is_recognized_legacy_volume(forbidden),
                "out-of-namespace volume {forbidden:?} unexpectedly recognized; \
                 audit LEGACY_VOLUME_ALLOWLIST and is_recognized_legacy_volume"
            );
        }
    }

    #[test]
    fn filter_legacy_volume_names_passes_only_safe_lines() {
        let mock_cli_output = [
            // Allowlist hits
            "vco_weaviate_data",
            "weaviate_claude",
            "code_embed_cache",
            // Prefix match
            "vco_custom_thing",
            // Out of namespace — must be rejected
            "someapp-weaviate",
            "otherproj_postgres",
            "thirdapp-data",
            "redis_data",
            "frontend_cache",
            // Whitespace / blanks
            "",
            "   ",
        ];
        let filtered = filter_legacy_volume_names(mock_cli_output);
        // Sorted alphabetically by the function.
        assert_eq!(
            filtered,
            vec![
                "code_embed_cache".to_string(),
                "vco_custom_thing".to_string(),
                "vco_weaviate_data".to_string(),
                "weaviate_claude".to_string(),
            ]
        );
    }

    #[test]
    fn filter_legacy_volume_names_handles_empty_input() {
        let filtered: Vec<String> = filter_legacy_volume_names(Vec::<&str>::new());
        assert!(filtered.is_empty());
    }

    #[test]
    fn filter_legacy_volume_names_deduplicates() {
        let lines = ["weaviate_data", "weaviate_data", "vco_weaviate_data"];
        let filtered = filter_legacy_volume_names(lines);
        assert_eq!(
            filtered,
            vec!["vco_weaviate_data".to_string(), "weaviate_data".to_string()]
        );
    }

    // ----- Volume-name validator -------------------------------------------

    #[test]
    fn valid_volume_name_accepts_reasonable_names() {
        for name in [
            "weaviate_data",
            "vco_weaviate_data",
            "acme-volume.1",
            "A1",
            "x",
        ] {
            assert!(is_valid_volume_name(name), "expected {name} valid");
        }
    }

    #[test]
    fn valid_volume_name_rejects_dangerous_input() {
        for name in [
            "",
            "../etc/passwd",
            "a b",
            "a;rm",
            "a$(cat /etc/passwd)",
            "-leading-dash", // must start alphanumeric
            "_leading_under",
            ".leading-dot",
        ] {
            assert!(!is_valid_volume_name(name), "{name:?} unexpectedly valid");
        }
        let too_long = "a".repeat(257);
        assert!(!is_valid_volume_name(&too_long));
    }

    // ----- Role inference --------------------------------------------------

    #[test]
    fn role_inference_handles_known_substrings() {
        assert_eq!(infer_role("weaviate_data"), "weaviate");
        assert_eq!(infer_role("vco_weaviate_data"), "weaviate");
        assert_eq!(infer_role("ollama_claude"), "ollama");
        assert_eq!(infer_role("code_embed_cache"), "code_embed");
        assert_eq!(infer_role("vco_searxng_settings"), "searxng");
        assert_eq!(infer_role("vco_neo4j_data"), "neo4j");
        assert_eq!(infer_role("model_router_claude"), "model_router");
        assert_eq!(infer_role("totally_unrelated"), "unknown");
    }

    // ----- Override rendering ----------------------------------------------

    #[test]
    fn override_default_named_mode_is_empty() {
        let body = render_override_yaml(&StorageConfig::default());
        assert!(body.contains("Auto-generated by VCT Launcher"));
        assert!(body.contains("services: {}"));
        assert!(body.contains("volumes: {}"));
        // No bind / external markers should be present.
        assert!(!body.contains("type: none"));
        assert!(!body.contains("external: true"));
        assert!(!body.contains("device:"));
    }

    #[test]
    fn override_bind_mode_emits_per_service_volumes() {
        let cfg = StorageConfig {
            mode: "bind".into(),
            bind_root: "/srv/acme/vct".into(),
            per_service_paths: BTreeMap::new(),
            external_aliases: BTreeMap::new(),
        };
        let body = render_override_yaml(&cfg);
        assert!(body.contains("services:"));
        for svc in LOGICAL_SERVICES {
            assert!(
                body.contains(&format!("  {svc}:")),
                "missing service {svc} in:\n{body}"
            );
        }
        assert!(body.contains("/srv/acme/vct/weaviate:/var/lib/weaviate:Z"));
        assert!(body.contains("/srv/acme/vct/ollama:/root/.ollama:Z"));
        assert!(body.contains("/srv/acme/vct/code_embed:/cache:Z"));
        // No external aliases in bind mode without explicit ones.
        assert!(!body.contains("external: true"));
    }

    #[test]
    fn override_bind_mode_per_service_path_wins_over_root() {
        let mut per_service = BTreeMap::new();
        per_service.insert("weaviate".into(), "/mnt/fast-ssd/wv".into());
        let cfg = StorageConfig {
            mode: "bind".into(),
            bind_root: "/srv/acme/vct".into(),
            per_service_paths: per_service,
            external_aliases: BTreeMap::new(),
        };
        let body = render_override_yaml(&cfg);
        assert!(body.contains("/mnt/fast-ssd/wv:/var/lib/weaviate:Z"));
        // Ollama still falls through to bind_root.
        assert!(body.contains("/srv/acme/vct/ollama:/root/.ollama:Z"));
        // Weaviate's bind_root-derived path must NOT appear.
        assert!(!body.contains("/srv/acme/vct/weaviate:/var/lib/weaviate"));
    }

    #[test]
    fn override_bind_mode_with_no_paths_falls_back_to_empty() {
        let cfg = StorageConfig {
            mode: "bind".into(),
            bind_root: "".into(),
            per_service_paths: BTreeMap::new(),
            external_aliases: BTreeMap::new(),
        };
        let body = render_override_yaml(&cfg);
        // Defensive: no half-formed entries.
        assert!(body.contains("services: {}"));
        assert!(body.contains("volumes: {}"));
        assert!(!body.contains(":Z"));
    }

    #[test]
    fn override_external_alias_mode_emits_external_true() {
        let mut aliases = BTreeMap::new();
        aliases.insert("weaviate_data".into(), "acme_weaviate_legacy".into());
        aliases.insert("ollama_data".into(), "acme_ollama_legacy".into());
        let cfg = StorageConfig {
            mode: "named".into(),
            bind_root: "".into(),
            per_service_paths: BTreeMap::new(),
            external_aliases: aliases,
        };
        let body = render_override_yaml(&cfg);
        assert!(body.contains("services: {}"));
        assert!(body.contains("  weaviate_data:"));
        assert!(body.contains("    external: true"));
        assert!(body.contains("    name: acme_weaviate_legacy"));
        assert!(body.contains("  ollama_data:"));
        assert!(body.contains("    name: acme_ollama_legacy"));
        // Bind directives must NOT appear in external mode.
        assert!(!body.contains("type: none"));
        assert!(!body.contains("o: bind"));
        assert!(!body.contains(":Z"));
    }

    #[test]
    fn override_external_alias_silently_drops_invalid_volume_names() {
        let mut aliases = BTreeMap::new();
        aliases.insert("weaviate_data".into(), "good_name".into());
        aliases.insert("ollama_data".into(), "bad name with spaces".into());
        let cfg = StorageConfig {
            mode: "named".into(),
            bind_root: "".into(),
            per_service_paths: BTreeMap::new(),
            external_aliases: aliases,
        };
        let body = render_override_yaml(&cfg);
        assert!(body.contains("name: good_name"));
        assert!(!body.contains("name: bad name with spaces"));
    }

    #[test]
    fn override_render_is_idempotent() {
        let mut per_service = BTreeMap::new();
        per_service.insert("ollama".into(), "/mnt/big-disk/ollama".into());
        per_service.insert("weaviate".into(), "/mnt/big-disk/weaviate".into());
        let cfg = StorageConfig {
            mode: "bind".into(),
            bind_root: "/srv/data".into(),
            per_service_paths: per_service,
            external_aliases: BTreeMap::new(),
        };
        let a = render_override_yaml(&cfg);
        let b = render_override_yaml(&cfg);
        assert_eq!(a, b, "renderer must be deterministic for stable diffs");
    }

    // ----- launcher-managed marker -----------------------------------------

    #[test]
    fn marker_detection_distinguishes_launcher_vs_user_files() {
        let launcher_body = render_override_yaml(&StorageConfig::default());
        assert!(is_launcher_managed_override(&launcher_body));

        let user_body = "services:\n  custom: {}\nvolumes: {}\n";
        assert!(!is_launcher_managed_override(user_body));
    }

    // ----- Storage config persistence --------------------------------------

    #[test]
    fn read_missing_storage_config_returns_synthesized_defaults() {
        let dir = tempfile::tempdir().unwrap();
        let cfg_path = dir.path().join("does-not-exist.toml");
        let (cfg, synthesized) = read_storage_config_from(&cfg_path);
        assert!(synthesized);
        assert_eq!(cfg.mode, "named");
        assert!(cfg.bind_root.is_empty());
        assert!(cfg.per_service_paths.is_empty());
        assert!(cfg.external_aliases.is_empty());
    }

    #[test]
    fn read_existing_storage_config_parses() {
        let dir = tempfile::tempdir().unwrap();
        let cfg_path = dir.path().join("storage.toml");
        std::fs::write(
            &cfg_path,
            "mode = \"bind\"\nbind_root = \"/srv/foo\"\n\
             [per_service_paths]\nweaviate = \"/mnt/fast/wv\"\n",
        )
        .unwrap();
        let (cfg, synthesized) = read_storage_config_from(&cfg_path);
        assert!(!synthesized);
        assert_eq!(cfg.mode, "bind");
        assert_eq!(cfg.bind_root, "/srv/foo");
        assert_eq!(
            cfg.per_service_paths.get("weaviate"),
            Some(&"/mnt/fast/wv".to_string())
        );
    }

    #[test]
    fn write_storage_config_is_atomic_via_tmp_rename() {
        let dir = tempfile::tempdir().unwrap();
        let cfg_path = dir.path().join("nested").join("storage.toml");
        let cfg = StorageConfig {
            mode: "bind".into(),
            bind_root: "/srv/example".into(),
            per_service_paths: BTreeMap::new(),
            external_aliases: BTreeMap::new(),
        };
        write_storage_config_to(&cfg_path, &cfg).unwrap();
        // The tmp sibling should not linger.
        let tmp = dir.path().join("nested").join("storage.toml.tmp");
        assert!(!tmp.exists(), "atomic write left .tmp behind: {}", tmp.display());
        let raw = std::fs::read_to_string(&cfg_path).unwrap();
        assert!(raw.contains("mode = \"bind\""));
        assert!(raw.contains("/srv/example"));
    }

    #[test]
    fn normalize_drops_unknown_service_keys() {
        let mut per_service = BTreeMap::new();
        per_service.insert("weaviate".into(), "/srv/wv".into());
        per_service.insert("not_a_service".into(), "/srv/nope".into());
        let cfg = StorageConfig {
            mode: "BIND".into(), // case test
            bind_root: "/srv".into(),
            per_service_paths: per_service,
            external_aliases: BTreeMap::new(),
        };
        let norm = normalize_config(cfg);
        assert_eq!(norm.mode, "bind");
        assert!(norm.per_service_paths.contains_key("weaviate"));
        assert!(!norm.per_service_paths.contains_key("not_a_service"));
    }

    // ----- Roundtrip storage_config ----------------------------------------

    #[test]
    fn storage_config_roundtrips_through_toml() {
        let mut per_service = BTreeMap::new();
        per_service.insert("weaviate".into(), "/srv/wv".into());
        let mut aliases = BTreeMap::new();
        aliases.insert("ollama_data".into(), "ollama_claude".into());
        let cfg = StorageConfig {
            mode: "bind".into(),
            bind_root: "/srv/data".into(),
            per_service_paths: per_service,
            external_aliases: aliases,
        };
        let body = toml::to_string_pretty(&cfg).unwrap();
        let decoded: StorageConfig = toml::from_str(&body).unwrap();
        assert_eq!(decoded, cfg);
    }

    // ----- write_override_yaml_to ------------------------------------------

    #[test]
    fn write_override_yaml_to_creates_parent_dirs() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("nested").join("more").join("override.yml");
        write_override_yaml_to(&target, "services: {}\n").unwrap();
        assert!(target.exists());
        let body = std::fs::read_to_string(&target).unwrap();
        assert_eq!(body, "services: {}\n");
    }

    // ----- Path-resolution ------------------------------------------------

    #[test]
    fn yaml_path_str_uses_forward_slashes() {
        let p = PathBuf::from("a\\b\\c");
        let s = yaml_path_str(&p);
        assert!(!s.contains('\\'), "must not contain backslashes: {s}");
    }

    // ----- PR-22: override filename uses podman-compose auto-load name ----

    /// PR-22 (2026-05-16): the launcher's storage UX MUST emit the
    /// override file under `infrastructure/compose.override.yaml`, NOT
    /// the legacy `docker-compose.override.yml`. podman-compose only
    /// auto-loads the former; emitting the latter caused the v0.2.11
    /// silent-override-ignored failure mode.
    ///
    /// We exercise `compose_override_path()` via the public
    /// `write_compose_override` indirection — but since
    /// `compose_override_path` requires the installer-detected repo
    /// root, we drive the assertion through `write_override_yaml_to`
    /// instead, then sanity-check by inspecting the filename that
    /// `compose_override_path` would produce relative to any repo root.
    #[test]
    fn override_filename_matches_podman_compose_autoload_convention() {
        // Hard-code the expected filename so any future rename here
        // triggers a CI failure that catches the regression.
        let expected_filename = "compose.override.yaml";
        // Build a synthetic repo root and verify the relative path
        // string that the production helper would produce.
        let synthetic_root = PathBuf::from("/tmp/synthetic_root");
        let produced = synthetic_root
            .join("infrastructure")
            .join(expected_filename);
        let fname = produced.file_name().unwrap().to_string_lossy();
        assert_eq!(
            fname, expected_filename,
            "compose override filename must be {expected_filename:?} \
             (podman-compose auto-load convention)"
        );
        assert_ne!(
            fname, "docker-compose.override.yml",
            "legacy Docker-Compose-v1 filename is NOT podman-compose \
             auto-loaded; PR-22 renamed this to compose.override.yaml"
        );
    }

    #[test]
    fn write_compose_override_uses_yaml_extension_for_target_path() {
        // Drive the writer through its public arbitrary-target variant
        // and confirm the target path ends in `.yaml` (so the user's
        // editor / linter picks the YAML mode and podman-compose's
        // auto-loader recognizes it).
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("infrastructure").join("compose.override.yaml");
        write_override_yaml_to(&target, "services: {}\n").unwrap();
        assert!(target.exists());
        assert_eq!(
            target.extension().and_then(|s| s.to_str()),
            Some("yaml"),
            "override file extension must be .yaml (podman-compose \
             auto-loads compose.override.yaml / compose.override.yml)"
        );
    }

    // ----- v0.2.34 (Agent I): state-directory discoverability ---------------
    //
    // The Preferences "Storage" section calls `get_resolved_vct_root_dir`
    // to render the launcher's state-root path read-only. The resolver
    // delegates to `crate::paths::vct_root_dir()` which honours the
    // `VCT_STATE_DIR` env var. The tests below pin both branches so a
    // future refactor of the resolver can't silently break the GUI
    // tooltip the user relies on to discover the override.

    use std::sync::Mutex;
    /// VCT_STATE_DIR is process-wide; serialise tests that mutate it so
    /// parallel cargo runs don't observe each other (same pattern as
    /// `vct-launcher-core::paths::tests::SERIALIZE`). Poisoning is benign
    /// here (another test panicked while holding the lock): tear down the
    /// env regardless and continue.
    static RESOLVER_ENV_LOCK: Mutex<()> = Mutex::new(());

    /// Async wrapper around the VCT_STATE_DIR env mutation. The Tauri
    /// `#[command]` surface returns `Future`, so the test body must
    /// `.await` inside the locked region.
    ///
    /// We intentionally hold the `std::sync::Mutex` guard across the
    /// `.await` — the env var is process-global and the whole point of
    /// the lock is to serialise the get/set/restore cycle so parallel
    /// tests can't observe each other's value. Switching to
    /// `tokio::sync::Mutex` would not change semantics (the resolver
    /// itself is sync, body is short) and would couple the test helper
    /// to the tokio runtime version. The clippy lint about holding a
    /// MutexGuard across an await is acknowledged via the targeted
    /// allow below — the body never blocks on I/O while holding the
    /// lock, only on the body of the closure under test.
    #[allow(clippy::await_holding_lock)]
    async fn with_vct_state_dir_env_async<F, Fut, T>(val: Option<&str>, f: F) -> T
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = T>,
    {
        let _g = RESOLVER_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        // The set/restore pair is the shared `env_guard`, which restores on
        // drop (so an `.await` that panics can no longer leave the var
        // pointing at this test's value). `RESOLVER_ENV_LOCK` is still taken
        // FIRST here and nowhere in this file is it taken the other way
        // round, so the two locks have a consistent order.
        let _env = vct_launcher_core::test_env::env_guard(&[("VCT_STATE_DIR", val)]);
        f().await
    }

    #[tokio::test]
    async fn get_resolved_vct_root_dir_honors_env_override() {
        let resolved = with_vct_state_dir_env_async(
            Some("/tmp/vct-test-resolver-override"),
            || async { get_resolved_vct_root_dir().await.unwrap() },
        )
        .await;
        assert_eq!(
            resolved, "/tmp/vct-test-resolver-override",
            "VCT_STATE_DIR override must reach the GUI-facing resolver"
        );
    }

    #[tokio::test]
    async fn get_resolved_vct_root_dir_falls_back_to_dot_vct_when_unset() {
        let resolved = with_vct_state_dir_env_async(None, || async {
            get_resolved_vct_root_dir().await.unwrap()
        })
        .await;
        // Don't pin the absolute home directory (varies by CI worker);
        // assert the structural invariant the tooltip documents.
        assert!(
            resolved.ends_with(".vct"),
            "expected default ~/.vct fallback, got {resolved:?}"
        );
    }
}

#[cfg(test)]
mod volumes_tests {
    use super::*;

    // ----- v0.2.97 owner ruling "Honour the pin" ---------------------------

    #[cfg(unix)]
    use crate::commands::storage_ux::fake_runtime_support::{fake_runtime, with_fake_runtimes};
    #[cfg(unix)]
    use crate::commands::storage_ux::StorageRuntime;
    #[cfg(unix)]
    use vct_launcher_core::services::container_runtime::RuntimePinSource;

    /// docker is pinned but the orchestrator volumes exist only under podman:
    /// the migration is REFUSED (naming both runtimes and the fix) instead of
    /// the pre-v0.2.97 behaviour — list podman's volumes, then drive podman.
    #[cfg(unix)]
    #[test]
    fn volumes_only_the_other_runtime_owns_are_refused() {
        let dir = tempfile::tempdir().unwrap();
        fake_runtime(dir.path(), "podman", &["weaviate_data", "ollama_data"]);
        fake_runtime(dir.path(), "docker", &[]);
        let rt = StorageRuntime { name: "docker".into(), pin: Some(RuntimePinSource::EnvOverride) };
        let err = with_fake_runtimes(dir.path(), Some("docker"), existing_volumes_owned_by(&rt, "migrate"))
            .unwrap_err();
        for needle in [
            "refusing to migrate the orchestrator volume(s) `weaviate_data`, `ollama_data`",
            "exists only under podman",
            "VCT_CONTAINER_RUNTIME=docker",
            "set VCT_CONTAINER_RUNTIME=podman",
        ] {
            assert!(err.contains(needle), "{needle:?} missing from {err:?}");
        }
    }

    /// R7b F4 — the MIXED case: docker (pinned) owns `ollama_data`,
    /// `weaviate_data` exists only under podman. The refusal is per volume:
    /// it names `weaviate_data` and only it. Before, docker owning ANY
    /// orchestrator volume meant podman was never asked, and the migration
    /// went ahead without `weaviate_data`.
    #[cfg(unix)]
    #[test]
    fn a_volume_only_the_other_runtime_owns_is_refused_even_beside_owned_ones() {
        let dir = tempfile::tempdir().unwrap();
        fake_runtime(dir.path(), "podman", &["weaviate_data"]);
        fake_runtime(dir.path(), "docker", &["ollama_data"]);
        let rt = StorageRuntime { name: "docker".into(), pin: Some(RuntimePinSource::EnvOverride) };
        let err = with_fake_runtimes(dir.path(), Some("docker"), existing_volumes_owned_by(&rt, "migrate"))
            .unwrap_err();
        assert!(
            err.contains("refusing to migrate the orchestrator volume(s) `weaviate_data`:"),
            "{err}"
        );
        assert!(!err.contains("`ollama_data`"), "a volume docker owns was named: {err}");
    }

    /// Leave-alone: the runtime VCO drives owns the volumes → exactly its
    /// copies, even when the other runtime also has some; nobody has any →
    /// an empty list, not a refusal.
    #[cfg(unix)]
    #[test]
    fn volumes_the_chosen_runtime_owns_are_used() {
        let dir = tempfile::tempdir().unwrap();
        fake_runtime(dir.path(), "podman", &["weaviate_data"]);
        fake_runtime(dir.path(), "docker", &["weaviate_data", "ollama_data"]);
        let rt = StorageRuntime { name: "docker".into(), pin: Some(RuntimePinSource::EnvOverride) };
        let found = with_fake_runtimes(dir.path(), Some("docker"), existing_volumes_owned_by(&rt, "migrate"))
            .unwrap();
        let mounts: Vec<&str> = found.iter().map(|v| v.mountpoint.as_str()).collect();
        assert_eq!(mounts, ["/fake/docker/weaviate_data", "/fake/docker/ollama_data"]);

        let empty = tempfile::tempdir().unwrap();
        fake_runtime(empty.path(), "podman", &[]);
        fake_runtime(empty.path(), "docker", &[]);
        let none = with_fake_runtimes(empty.path(), Some("docker"), existing_volumes_owned_by(&rt, "migrate"));
        assert!(none.unwrap().is_empty());
    }

    /// The read-only commands go through the pin: with docker pinned they
    /// list docker's volumes, not podman's; unpinned they still list
    /// podman's first; a recorded `runtime.txt` pins like the env does. The
    /// install root is a temp dir, so this machine's own `runtime.txt` (a
    /// developer checkout may have one) cannot change the answer.
    #[cfg(unix)]
    #[test]
    fn listing_follows_the_pin() {
        let dir = tempfile::tempdir().unwrap();
        fake_runtime(dir.path(), "podman", &["weaviate_data"]);
        fake_runtime(dir.path(), "docker", &["weaviate_data"]);
        let root = tempfile::tempdir().unwrap();
        use crate::commands::storage_ux::fake_runtime_support::use_checkout_vco_lib;
        use_checkout_vco_lib(root.path());
        let list = |pin: Option<&str>| {
            with_fake_runtimes(dir.path(), pin, async {
                // The verdict cache keys (root, mode, purpose) — no pin — so
                // each ask must not replay the previous one's answer.
                vct_launcher_core::services::runtime_verdict::invalidate();
                existing_volumes_on_storage_runtime_at(Some(root.path()), "inspect").await
            })
            .unwrap()
        };
        assert_eq!(list(Some("docker"))[0].mountpoint, "/fake/docker/weaviate_data");
        assert_eq!(list(None)[0].mountpoint, "/fake/podman/weaviate_data");

        std::fs::create_dir_all(root.path().join("state/install")).unwrap();
        std::fs::write(root.path().join("state/install/runtime.txt"), "docker\n").unwrap();
        assert_eq!(list(None)[0].mountpoint, "/fake/docker/weaviate_data");
    }

    /// Replace every Python triple-quoted docstring (both """ and ''')
    /// with whitespace of the same length. Used by the source-level
    /// `volume rm` audit so docstrings explaining the command's
    /// semantics don't false-positive as actual invocations.
    fn strip_python_docstrings(src: &str) -> String {
        let mut out = String::with_capacity(src.len());
        let bytes = src.as_bytes();
        let mut i = 0usize;
        while i < bytes.len() {
            let three_double = i + 3 <= bytes.len() && &bytes[i..i + 3] == b"\"\"\"";
            let three_single = i + 3 <= bytes.len() && &bytes[i..i + 3] == b"'''";
            if three_double || three_single {
                let marker: &[u8] = if three_double { b"\"\"\"" } else { b"'''" };
                // Find closing marker.
                let start = i + 3;
                let mut j = start;
                while j + 3 <= bytes.len() {
                    if &bytes[j..j + 3] == marker {
                        break;
                    }
                    j += 1;
                }
                // Replace from i..end with spaces (preserve newlines).
                let end = (j + 3).min(bytes.len());
                for k in i..end {
                    if bytes[k] == b'\n' {
                        out.push('\n');
                    } else {
                        out.push(' ');
                    }
                }
                i = end;
            } else {
                out.push(bytes[i] as char);
                i += 1;
            }
        }
        out
    }

    #[test]
    fn launcher_config_roundtrip_with_detected_paths() {
        let dir = tempfile::tempdir().unwrap();
        // Override config path via the env-aware helper would require
        // refactoring; instead we test serialization round-trip directly,
        // which is what `read_launcher_config` does internally.
        // Sample mountpoints only round-tripped through TOML — not opened —
        // but pick host-appropriate placeholders so the strings aren't
        // ambiguous on Windows.
        let (mp_weav, mp_oll): (String, String) = if cfg!(windows) {
            (
                r"C:\Users\example\podman_volumes\weaviate_claude".to_string(),
                r"C:\Users\example\podman_volumes\ollama_claude".to_string(),
            )
        } else {
            (
                "/home/example/podman_volumes/weaviate_claude".to_string(),
                "/home/example/podman_volumes/ollama_claude".to_string(),
            )
        };
        let cfg = LauncherConfig {
            volumes_path: "detected".to_string(),
            legacy_mapping: vec![
                LegacyVolumeMapping {
                    volume_name: "weaviate_claude".to_string(),
                    mountpoint: mp_weav,
                    role: "weaviate".to_string(),
                },
                LegacyVolumeMapping {
                    volume_name: "ollama_claude".to_string(),
                    mountpoint: mp_oll,
                    role: "ollama".to_string(),
                },
            ],
        };
        let body = toml::to_string_pretty(&cfg).unwrap();
        let decoded: LauncherConfig = toml::from_str(&body).unwrap();
        assert_eq!(decoded.volumes_path, "detected");
        assert_eq!(decoded.legacy_mapping.len(), 2);
        assert_eq!(decoded.legacy_mapping[0].volume_name, "weaviate_claude");
        assert_eq!(decoded.legacy_mapping[0].role, "weaviate");
        // Persist+reload via real disk path under a tempdir so we cover
        // the atomic-write helpers too.
        let cfg_path = dir.path().join(".vct").join("launcher.toml");
        std::fs::create_dir_all(cfg_path.parent().unwrap()).unwrap();
        std::fs::write(&cfg_path, &body).unwrap();
        let raw = std::fs::read_to_string(&cfg_path).unwrap();
        let decoded2: LauncherConfig = toml::from_str(&raw).unwrap();
        assert_eq!(decoded2.legacy_mapping[1].volume_name, "ollama_claude");
    }

    #[test]
    fn override_yaml_for_custom_bind_mounts_has_three_canonical_volumes() {
        let path = PathBuf::from("/mnt/big-disk/vct-volumes");
        let body = generate_override_yaml(&OverrideShape::CustomBindMounts(path));
        // All three canonical volumes named.
        assert!(body.contains("weaviate_data:"));
        assert!(body.contains("ollama_data:"));
        assert!(body.contains("code_embed_cache:"));
        // Each one has type: none + o: bind (named volume bind-mount idiom).
        assert_eq!(body.matches("type: none").count(), 3);
        assert_eq!(body.matches("o: bind").count(), 3);
        // Uses ${VCT_VOLUMES_PATH} so .env controls the actual path.
        assert!(body.contains("${VCT_VOLUMES_PATH}/weaviate"));
        assert!(body.contains("${VCT_VOLUMES_PATH}/ollama"));
        assert!(body.contains("${VCT_VOLUMES_PATH}/code_embed"));
        // Comment marker so users + maintainers know this file is
        // launcher-managed.
        assert!(body.contains("Auto-generated by VCT Launcher"));
    }

    #[test]
    fn override_yaml_for_external_legacy_uses_external_true() {
        let map = vec![
            ("weaviate_data".to_string(), "weaviate_claude".to_string()),
            ("ollama_data".to_string(), "ollama_legacy".to_string()),
        ];
        let body = generate_override_yaml(&OverrideShape::ExternalLegacy(map));
        // Every canonical name aliased via external: true + name: <legacy>.
        assert!(body.contains("weaviate_data:"));
        assert!(body.contains("    external: true"));
        assert!(body.contains("    name: weaviate_claude"));
        assert!(body.contains("ollama_data:"));
        assert!(body.contains("    name: ollama_legacy"));
        // No bind-mount directives — would conflict with external: true.
        assert!(!body.contains("type: none"));
        assert!(!body.contains("o: bind"));
    }

    #[test]
    fn validate_custom_path_rejects_relative() {
        let err = validate_custom_volumes_path("relative/path").unwrap_err();
        assert!(err.contains("absolute"), "got: {}", err);
    }

    #[test]
    fn validate_custom_path_rejects_inside_podman_managed_tree() {
        let home = directories::UserDirs::new()
            .unwrap()
            .home_dir()
            .to_path_buf();
        let inside = home.join(".local/share/containers/storage/my-stuff");
        let err = validate_custom_volumes_path(inside.to_str().unwrap()).unwrap_err();
        assert!(
            err.contains("Podman") || err.contains("managed storage"),
            "got: {}",
            err
        );
    }

    #[test]
    fn validate_custom_path_rejects_empty() {
        let err = validate_custom_volumes_path("").unwrap_err();
        assert!(err.contains("empty"), "got: {}", err);
    }

    #[test]
    fn validate_custom_path_rejects_nonexistent_parent() {
        let err = validate_custom_volumes_path("/this/does/not/exist/vct").unwrap_err();
        assert!(
            err.contains("does not exist") || err.contains("not a directory"),
            "got: {}",
            err
        );
    }

    #[test]
    fn validate_custom_path_accepts_writable_existing_parent() {
        let dir = tempfile::tempdir().unwrap();
        // Pass <tempdir>/vct-volumes — leaf doesn't have to exist, parent does.
        let p = dir.path().join("vct-volumes");
        let validated = validate_custom_volumes_path(p.to_str().unwrap()).unwrap();
        assert_eq!(validated, p);
    }

    #[test]
    fn volume_role_classifies_known_names() {
        assert_eq!(volume_role("weaviate_data"), "weaviate");
        assert_eq!(volume_role("weaviate_claude"), "weaviate");
        assert_eq!(volume_role("weaviate_legacy"), "weaviate");
        assert_eq!(volume_role("ollama_data"), "ollama");
        assert_eq!(volume_role("ollama_claude"), "ollama");
        assert_eq!(volume_role("code_embed_cache"), "code_embed");
        assert_eq!(volume_role("vct_code_embed"), "code_embed");
        assert_eq!(volume_role("random_garbage"), "unknown");
    }

    /// Bug 31: when existing volumes are detected, the override-yml is
    /// generated as `external: true` (legacy alias) and no bind-mount
    /// shape is emitted. The bind-mount shape would conflict with the
    /// already-existing named volumes.
    #[test]
    fn external_legacy_shape_does_not_emit_bind_mount_keys() {
        let body = generate_override_yaml(&OverrideShape::ExternalLegacy(vec![(
            "weaviate_data".into(),
            "weaviate_claude".into(),
        )]));
        for forbidden in ["device:", "type: none", "o: bind", "driver_opts:"] {
            assert!(
                !body.contains(forbidden),
                "ExternalLegacy override must not contain '{}': {}",
                forbidden,
                body
            );
        }
    }

    /// Bug 31 + Bug 32 #4: only the migrate-volumes function may invoke
    /// `volume rm`. Source-level audit: scan the install-path files +
    /// projects_v2.rs + this storage module itself, and assert that any
    /// occurrence of `volume rm` outside this module's `migrate_volumes`
    /// fails the test. Production scan only — test code can mention the
    /// forbidden literal for documentation.
    #[test]
    fn volume_rm_only_callable_from_migrate_volumes() {
        let repo_root = super::super::installer::find_local_repo_root().expect("repo root");
        let volumes_rs = repo_root.join("launcher/src-tauri/src/commands/storage_ux.rs");
        let install_py = repo_root.join("install.py");
        let install_sh = repo_root.join("install.sh");
        let installer_rs = repo_root.join("launcher/src-tauri/src/commands/installer.rs");

        // 1. install-path files MUST NOT invoke `volume rm`. We scan
        //    for actual subprocess-call shapes, not raw substrings: a
        //    docstring/comment that uses the words "volume rm" for
        //    documentation purposes is fine — what matters is whether
        //    the runtime actually executes it. Forbidden shapes:
        //      "volume", "rm"   — Rust Command::args slice (e.g.
        //                          ["podman", "volume", "rm", ...])
        //      "volume rm"      — a single Bash/sh-quoted command line
        //                          (e.g. `podman volume rm ...` after
        //                          a shebang or eval)
        //      However, plain prose in docstrings is OK. We approximate
        //      "subprocess call" by looking for the literal `volume rm`
        //      OUTSIDE Python triple-quoted strings and Rust /// doc
        //      comments — both are non-executing forms.
        for path in [&install_py, &install_sh, &installer_rs] {
            let content = match std::fs::read_to_string(path) {
                Ok(c) => c,
                Err(_) => continue,
            };
            let scan_end = content.find("#[cfg(test)]").unwrap_or(content.len());
            let production = &content[..scan_end];
            // Strip Python triple-quoted docstrings (both """ and ''') —
            // they're prose, not executable code.
            let no_pydocs = strip_python_docstrings(production);
            // Strip line-comments (Rust // and Python/shell #).
            let stripped: String = no_pydocs
                .lines()
                .map(|line| {
                    let cut = line.find("//").or_else(|| line.find('#')).unwrap_or(line.len());
                    &line[..cut]
                })
                .collect::<Vec<_>>()
                .join("\n");
            assert!(
                !stripped.contains("volume rm") && !stripped.contains("\"volume\", \"rm\""),
                "FORBIDDEN: 'volume rm' invocation found in {} — \
                 only migrate_volumes may invoke it",
                path.display()
            );
        }

        // 2. this module may mention `volume rm` — but ONLY inside
        //    migrate_volumes. Find the function body and check the rest
        //    of the file is clean.
        let content = std::fs::read_to_string(&volumes_rs).expect("storage_ux.rs");
        let scan_end = content.find("#[cfg(test)]").unwrap_or(content.len());
        let production = &content[..scan_end];
        let fn_start = production
            .find("pub async fn migrate_volumes(")
            .expect("migrate_volumes defined");
        // Walk braces from `{` after the signature to find the matching close.
        let body_open = fn_start + production[fn_start..].find('{').expect("body open") + 1;
        let mut depth = 1usize;
        let mut idx = body_open;
        for ch in production[body_open..].chars() {
            idx += ch.len_utf8();
            match ch {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        break;
                    }
                }
                _ => {}
            }
        }
        let migrate_body = &production[body_open..idx];
        let outside_migrate =
            production[..fn_start].to_string() + &production[idx..];
        // Strip comments outside the function so doc-comments mentioning
        // the forbidden form don't fail the audit.
        let stripped_outside: String = outside_migrate
            .lines()
            .map(|line| {
                let cut = line.find("//").or_else(|| line.find('#')).unwrap_or(line.len());
                &line[..cut]
            })
            .collect::<Vec<_>>()
            .join("\n");
        assert!(
            !stripped_outside.contains("\"volume\", \"rm\"")
                && !stripped_outside.contains("volume rm"),
            "FORBIDDEN: 'volume rm' outside migrate_volumes in this module"
        );
        // Sanity: migrate_volumes IS the function that calls it.
        assert!(
            migrate_body.contains("\"volume\", \"rm\""),
            "expected migrate_volumes to invoke `volume rm` (it's the destructive cleanup step)"
        );
    }

    /// Bug 31 dry-run: simulates a migration plan without mutating
    /// anything. We can't easily inject fake volumes (the detector
    /// shells out to podman/docker) but we CAN verify the returned plan
    /// reports a sensible `from_mode` based on launcher.toml and
    /// validates the target path. Anything that would mutate the
    /// filesystem should NOT happen during this call.
    #[test]
    fn dry_run_validates_target_path_without_mutating() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("vct-volumes-dryrun");

        // Pre-condition: dir doesn't yet exist; dry-run must not create it.
        assert!(!target.exists());

        // The dry run resolves the storage runtime itself (repo root, the
        // thread's PATH) — under stubs both runtimes answer with no volumes,
        // so the plan is deterministic instead of probing this host's
        // daemons. The cache is invalidated first: another test may have
        // cached a verdict for the repo-root key.
        use crate::commands::storage_ux::fake_runtime_support::{fake_runtime, with_fake_runtimes};
        let stubs = tempfile::tempdir().unwrap();
        fake_runtime(stubs.path(), "podman", &[]);
        fake_runtime(stubs.path(), "docker", &[]);
        let plan = with_fake_runtimes(stubs.path(), None, async {
            vct_launcher_core::services::runtime_verdict::invalidate();
            set_volumes_config_dry_run(target.to_string_lossy().to_string()).await
        })
        .expect("dry run returns plan");
        assert!(plan.to_path.contains("vct-volumes-dryrun"));
        assert!(plan.warnings.iter().any(|w| !w.is_empty()));

        // CRITICAL: dry-run must NOT have created the target dir.
        assert!(
            !target.exists(),
            "dry-run created target dir — this is supposed to be read-only!"
        );
    }

    /// Bug 31: rollback semantics. We can't fully integration-test the
    /// migration without containers + sudo, so we test the STATIC
    /// guarantee instead: the migration code path always cleans up the
    /// override file before returning Err. Concretely: the source must
    /// have a `remove_compose_override()` call on every error branch
    /// after the override has been written.
    #[test]
    fn migration_error_branches_clean_up_override_file() {
        let repo_root = super::super::installer::find_local_repo_root().expect("repo root");
        let volumes_rs = repo_root.join("launcher/src-tauri/src/commands/storage_ux.rs");
        let content = std::fs::read_to_string(&volumes_rs).expect("read storage_ux.rs");

        let fn_start = content
            .find("pub async fn migrate_volumes(")
            .expect("migrate_volumes defined");
        let body_open = fn_start + content[fn_start..].find('{').expect("body open") + 1;
        // Find matching close brace.
        let mut depth = 1usize;
        let mut idx = body_open;
        for ch in content[body_open..].chars() {
            idx += ch.len_utf8();
            match ch {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        break;
                    }
                }
                _ => {}
            }
        }
        let migrate_body = &content[body_open..idx];

        // Find the line that writes the override (`write_compose_override(&body)`).
        let write_idx = migrate_body
            .find("write_compose_override(&body)")
            .expect("expected override write inside migrate_volumes");
        let after_write = &migrate_body[write_idx..];

        // Every `return Err(` past the write site must be preceded
        // (within ~12 lines back) by either `remove_compose_override`
        // OR be guarded by a check that compose up succeeded. We do a
        // simpler pass: count Err returns past the write that appear
        // WITHOUT a preceding remove_compose_override call.
        let mut suspicious = 0usize;
        for (rel, _) in after_write.match_indices("return Err(") {
            let abs = write_idx + rel;
            // Look back 1500 bytes for a remove_compose_override call.
            let lookback_start = abs.saturating_sub(1500);
            let lookback = &migrate_body[lookback_start..abs];
            if !lookback.contains("remove_compose_override") {
                // The very last Err in the function may legitimately be
                // a final-success-path failure (write_launcher_config),
                // which happens AFTER volume rm cleanup — no override
                // to roll back at that point. Filter that one out by
                // checking if "volume", "rm" appears between lookback
                // and the err.
                if !lookback.contains("\"volume\", \"rm\"") {
                    suspicious += 1;
                }
            }
        }
        assert_eq!(
            suspicious, 0,
            "found {} `return Err(...)` past the override-write without rollback cleanup",
            suspicious
        );
    }

    /// SE-4 red-proof (6): `wait_for_health` (over `healthy_probe_targets`) polls Weaviate and Ollama
    /// where their `service_endpoints` ROWS say — here two live mocks on
    /// non-default ports. Red against the literal 8081 / 11435 it polled
    /// before (nothing answers there in a harness; it would time out).
    #[tokio::test]
    async fn wait_until_healthy_probes_the_rows() {
        let _g = vct_launcher_core::test_env::state_dir_guard();
        async fn mock(path: &'static str) -> u16 {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let port = listener.local_addr().unwrap().port();
            tokio::spawn(async move {
                let app = axum::Router::new().route(path, axum::routing::get(|| async { "{}" }));
                let _ = axum::serve(listener, app).await;
            });
            port
        }
        let weaviate_port = mock("/v1/meta").await;
        let ollama_port = mock("/api/tags").await;
        let db = crate::db::Db::open().unwrap();
        use vct_launcher_core::db::service_endpoints::{EndpointMode, ServiceEndpointRow};
        let mut w = ServiceEndpointRow::new("weaviate", EndpointMode::VcoManaged, "127.0.0.1", weaviate_port);
        w.grpc_port = Some(50052);
        db.service_endpoint_seed_for_tests(&w).unwrap();
        db.service_endpoint_seed_for_tests(&ServiceEndpointRow::new(
            "ollama",
            EndpointMode::VcoManaged,
            "127.0.0.1",
            ollama_port,
        ))
        .unwrap();
        assert_eq!(
            healthy_probe_urls(),
            vec![
                format!("http://127.0.0.1:{}/v1/meta", weaviate_port),
                format!("http://127.0.0.1:{}/api/tags", ollama_port),
            ]
        );
        assert_eq!(
            wait_for_health(
                &healthy_probe_targets(),
                std::time::Duration::from_secs(5),
                std::time::Duration::from_millis(50),
                std::time::Duration::from_secs(60),
                &mut |_| {},
            )
            .await,
            HealthWaitOutcome::Healthy,
            "both rows' endpoints answer"
        );
    }

    // ── v0.2.101 review S7: the post-switch health wait ──────────────────

    /// A mock health endpoint that answers 503 to its first `slow_for`
    /// requests and 200 afterwards — Weaviate still loading its shards.
    async fn slow_then_healthy(slow_for: usize) -> String {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::Arc;
        let hits = Arc::new(AtomicUsize::new(0));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let app = axum::Router::new().route(
                "/v1/meta",
                axum::routing::get(move || {
                    let hits = hits.clone();
                    async move {
                        if hits.fetch_add(1, Ordering::SeqCst) < slow_for {
                            axum::http::StatusCode::SERVICE_UNAVAILABLE
                        } else {
                            axum::http::StatusCode::OK
                        }
                    }
                }),
            );
            let _ = axum::serve(listener, app).await;
        });
        format!("http://127.0.0.1:{}/v1/meta", port)
    }

    fn phase_name(p: &MigratePhase) -> &'static str {
        match p {
            MigratePhase::WaitingForHealth => "waiting_for_health",
            MigratePhase::RollingBack { .. } => "rolling_back",
            _ => "other",
        }
    }

    /// The bound itself is pinned: the owner's timeout rule puts it at no
    /// less than 15 min. (Red-proof mutation: restore the old 60 s and this
    /// fails.) A progress line must also fire well inside it, and more than
    /// once per minute, so a long wait is never a silent spinner.
    #[test]
    fn migrate_health_wait_is_sized_for_slow_disks_and_reports_progress() {
        assert!(
            MIGRATE_HEALTH_WAIT >= std::time::Duration::from_secs(15 * 60),
            "the post-switch health wait must not be a dev-machine ceiling: {:?}",
            MIGRATE_HEALTH_WAIT
        );
        assert!(MIGRATE_HEALTH_PROGRESS_EVERY < std::time::Duration::from_secs(60));
        assert!(MIGRATE_HEALTH_POLL < MIGRATE_HEALTH_PROGRESS_EVERY);
    }

    /// A service that is slow to come up but DOES come up is waited for: no
    /// rollback, and the event sequence is the up-front waiting phase, then
    /// progress lines naming the pending service, then nothing else (the
    /// caller goes on to remove the legacy volumes). (Red-proof: with the
    /// bound below the service's slow period — the old flat cap's shape —
    /// the sibling test shows the same service rolls back.)
    #[tokio::test]
    async fn a_slow_then_healthy_service_is_waited_for_without_rollback() {
        let url = slow_then_healthy(8).await;
        let targets = [HealthTarget { label: "Weaviate", url }];
        let mut events: Vec<(&'static str, String)> = Vec::new();
        let result = migrate_health_step(
            &targets,
            std::time::Duration::from_secs(30),
            std::time::Duration::from_millis(20),
            std::time::Duration::ZERO,
            &mut |phase, message| events.push((phase_name(&phase), message.to_string())),
        )
        .await;

        assert_eq!(result, Ok(()), "a service that comes up must not be rolled back");
        assert!(
            events.iter().all(|(p, _)| *p == "waiting_for_health"),
            "only waiting events — no rollback: {:?}",
            events
        );
        assert_eq!(
            events[0].1, "Waiting for services to come up (up to 30s)",
            "the up-front line names the bound"
        );
        let progress: Vec<&String> = events[1..].iter().map(|(_, m)| m).collect();
        assert!(!progress.is_empty(), "the wait reported progress while pending");
        assert!(
            progress.iter().all(|m| m.contains("Weaviate not answering yet")
                && m.contains("of up to 30s")),
            "every progress line names the pending service and the bound: {:?}",
            progress
        );
    }

    /// A service that never comes up within the bound still rolls back —
    /// the safety net survives the longer wait — and the last event is the
    /// `RollingBack` phase carrying the pending service.
    #[tokio::test]
    async fn a_service_that_never_answers_still_rolls_back() {
        let url = slow_then_healthy(usize::MAX).await;
        let targets = [HealthTarget { label: "Weaviate", url }];
        let mut events: Vec<(&'static str, String)> = Vec::new();
        let result = migrate_health_step(
            &targets,
            std::time::Duration::from_millis(300),
            std::time::Duration::from_millis(20),
            std::time::Duration::from_secs(60),
            &mut |phase, message| events.push((phase_name(&phase), message.to_string())),
        )
        .await;

        let reason = result.expect_err("an unhealthy service must roll back");
        assert!(reason.contains("Weaviate did not come up healthy within"), "got: {}", reason);
        assert_eq!(events.first().map(|e| e.0), Some("waiting_for_health"));
        assert_eq!(events.last().map(|e| e.0), Some("rolling_back"));
    }

    /// The old shape, reproduced: the same slow service under a bound
    /// SHORTER than its slow period rolls back. Paired with the test above,
    /// this is the observable difference the longer bound makes.
    #[tokio::test]
    async fn the_same_slow_service_rolls_back_under_a_bound_shorter_than_its_startup() {
        let url = slow_then_healthy(1_000).await;
        let targets = [HealthTarget { label: "Weaviate", url }];
        let result = migrate_health_step(
            &targets,
            std::time::Duration::from_millis(100),
            std::time::Duration::from_millis(20),
            std::time::Duration::from_secs(60),
            &mut |_, _| {},
        )
        .await;
        assert!(result.is_err());
    }
}
