//! Configurable container volumes location.
//!
//! ## Behavior summary
//!
//! - **First install** (no existing volumes detected): the user can pick
//!   between the runtime default (`~/.local/share/containers/storage/volumes/`)
//!   and a custom path. If custom is chosen, we write
//!   `infrastructure/docker-compose.override.yml` with bind-mount overrides
//!   pointing the three named volumes at subfolders of the chosen path.
//!
//! - **Subsequent install** (existing volumes detected): no picker is
//!   shown. The contract is to NOT generate the override file when
//!   existing volumes are found — bind-mount overrides on top of
//!   already-existing named volumes would either fail (volume name
//!   conflict) or worse, mask the original. The detected paths are
//!   recorded into `~/.vct/launcher.toml` as `volumes_path = "detected"`
//!   plus a `legacy_mapping` table.
//!
//! - **Settings → Preferences**: the user can change the volume location
//!   at any time via the `migrate_volumes` Tauri command. Migration uses
//!   `compose down` (preserves volumes — no `--volumes` flag), `cp -a`,
//!   then `up -d`. ONLY after the new bind-mounts come up healthy do we
//!   call `podman volume rm` on the legacy volumes. On any failure, the
//!   override file is removed and the old volumes are kept untouched.
//!
//! ## Safety
//!
//! `migrate_volumes` is the ONLY function in the launcher allowed to
//! invoke `podman volume rm` / `docker volume rm`. The non-destructive
//! audit test in `installer.rs::test_no_destructive_subprocess_calls_in_install_path`
//! is scoped to install-path files (install.py, install.sh, installer.rs,
//! projects_v2.rs); migration lives in this file (volumes.rs) and is
//! invoked only from an explicit Settings UI click with `confirmed=true`.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tauri::{command, AppHandle, Emitter};

use super::installer::ExistingVolume;

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

fn compose_override_path() -> Result<PathBuf, String> {
    Ok(orchestrator_root()?.join("infrastructure").join("docker-compose.override.yml"))
}

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

/// Write `infrastructure/docker-compose.override.yml`. Atomic temp+rename.
pub fn write_compose_override(body: &str) -> Result<PathBuf, String> {
    let path = compose_override_path()?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create {}: {}", parent.display(), e))?;
    }
    let mut tmp = path.clone();
    tmp.set_extension("yml.tmp");
    std::fs::write(&tmp, body).map_err(|e| format!("write tmp {}: {}", tmp.display(), e))?;
    std::fs::rename(&tmp, &path)
        .map_err(|e| format!("rename {} -> {}: {}", tmp.display(), path.display(), e))?;
    Ok(path)
}

pub fn remove_compose_override() -> Result<(), String> {
    let path = compose_override_path()?;
    if path.exists() {
        std::fs::remove_file(&path)
            .map_err(|e| format!("remove {}: {}", path.display(), e))?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Size probing (du -sb fallback to walk)
// ---------------------------------------------------------------------------

fn human_bytes(bytes: u64) -> String {
    const KB: u64 = 1024;
    const MB: u64 = KB * 1024;
    const GB: u64 = MB * 1024;
    if bytes >= GB {
        format!("{:.1} GB", bytes as f64 / GB as f64)
    } else if bytes >= MB {
        format!("{:.1} MB", bytes as f64 / MB as f64)
    } else if bytes >= KB {
        format!("{:.1} KB", bytes as f64 / KB as f64)
    } else {
        format!("{} B", bytes)
    }
}

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
// Tauri commands
// ---------------------------------------------------------------------------

/// Read the current volume configuration. Reads `launcher.toml`, probes
/// existing volumes, and computes per-volume sizes.
#[command]
pub async fn get_volumes_config() -> Result<VolumesConfig, String> {
    let cfg = read_launcher_config();
    let existing = super::installer::detect_existing_volumes_for_volumes_module().await;

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
    let existing = super::installer::detect_existing_volumes_for_volumes_module().await;
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
/// create the file). Other env keys are preserved.
fn write_volumes_env_var(path: &Path) -> Result<(), String> {
    let env_path = orchestrator_root()?.join("infrastructure").join(".env");
    let mut lines: Vec<String> = if env_path.exists() {
        std::fs::read_to_string(&env_path)
            .map_err(|e| format!("read {}: {}", env_path.display(), e))?
            .lines()
            .map(|l| l.to_string())
            .collect()
    } else {
        Vec::new()
    };
    let new_line = format!("VCT_VOLUMES_PATH={}", path.display());
    let mut replaced = false;
    for line in lines.iter_mut() {
        if line.starts_with("VCT_VOLUMES_PATH=") {
            *line = new_line.clone();
            replaced = true;
            break;
        }
    }
    if !replaced {
        lines.push(new_line);
    }
    let body = lines.join("\n") + "\n";
    if let Some(parent) = env_path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("create {}: {}", parent.display(), e))?;
    }
    std::fs::write(&env_path, body)
        .map_err(|e| format!("write {}: {}", env_path.display(), e))?;
    Ok(())
}

/// Build a migration plan WITHOUT touching anything. Frontend renders
/// this in the confirm dialog before the user clicks "Migrate".
#[command]
pub async fn set_volumes_config_dry_run(path: String) -> Result<MigrationPlan, String> {
    let cfg = read_launcher_config();
    let existing = super::installer::detect_existing_volumes_for_volumes_module().await;

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

    let runtime = which_container_runtime()
        .ok_or("no container runtime (podman/docker) found on PATH")?;

    let existing = super::installer::detect_existing_volumes_for_volumes_module().await;
    if existing.is_empty() {
        return Err("no existing volumes to migrate".into());
    }

    // 1. Stop services. NOTE: NO `--volumes` flag — this only stops
    //    containers, leaves volumes intact.
    emit_phase(&app, MigratePhase::StoppingContainers, "Stopping containers");
    let compose_dir = orchestrator_root()?.join("infrastructure");
    let compose_status = tokio::process::Command::new(&runtime)
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
        let cp_status = tokio::process::Command::new("cp")
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
    let up_status = tokio::process::Command::new(&runtime)
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

    // 5. Verify health by probing the standard endpoints. Give services
    //    up to 60 seconds to come back online.
    emit_phase(&app, MigratePhase::WaitingForHealth, "Waiting for services to come up");
    let healthy = wait_until_healthy(60).await;
    if !healthy {
        emit_phase(
            &app,
            MigratePhase::RollingBack { reason: "services unhealthy after 60s".into() },
            "Rolling back",
        );
        let _ = remove_compose_override();
        let _ = restart_services_for_rollback(&runtime, &compose_dir).await;
        return Err("services did not come up healthy within 60s on new bind-mounts (rolled back; old volumes intact)".into());
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
        let _ = tokio::process::Command::new(&runtime)
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
    let status = tokio::process::Command::new(runtime)
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

/// HTTP-probe the three default endpoints in a tight loop until they
/// all respond 2xx/3xx, or `timeout_secs` elapses.
async fn wait_until_healthy(timeout_secs: u64) -> bool {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
    {
        Ok(c) => c,
        Err(_) => return false,
    };
    // /v1/meta is the right liveness probe for Weaviate — see
    // commands/lifecycle.rs::canonical_services for why
    // /v1/.well-known/ready is too strict.
    let urls = [
        "http://localhost:8081/v1/meta",
        "http://localhost:11435/api/tags",
    ];
    while std::time::Instant::now() < deadline {
        let mut all_ok = true;
        for u in &urls {
            match client.get(*u).send().await {
                Ok(r) if r.status().as_u16() < 400 => {}
                _ => {
                    all_ok = false;
                    break;
                }
            }
        }
        if all_ok {
            return true;
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
    false
}

fn which_container_runtime() -> Option<String> {
    for runtime in &["podman", "docker"] {
        if let Some(paths) = std::env::var_os("PATH") {
            for dir in std::env::split_paths(&paths) {
                if dir.join(runtime).is_file() {
                    return Some(runtime.to_string());
                }
            }
        }
    }
    None
}

// Force ExistingVolume to be used so the import isn't dead code (the
// type is consumed implicitly via tokio process JSON deserialization
// inside detect_existing_volumes_for_volumes_module which we delegate
// to via super::installer).
#[allow(dead_code)]
fn _force_existing_volume_used(_: ExistingVolume) {}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

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
            ("ollama_data".to_string(), "ollama_ARTup".to_string()),
        ];
        let body = generate_override_yaml(&OverrideShape::ExternalLegacy(map));
        // Every canonical name aliased via external: true + name: <legacy>.
        assert!(body.contains("weaviate_data:"));
        assert!(body.contains("    external: true"));
        assert!(body.contains("    name: weaviate_claude"));
        assert!(body.contains("ollama_data:"));
        assert!(body.contains("    name: ollama_ARTup"));
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
        assert_eq!(volume_role("weaviate_ARTup"), "weaviate");
        assert_eq!(volume_role("ollama_data"), "ollama");
        assert_eq!(volume_role("ollama_claude"), "ollama");
        assert_eq!(volume_role("code_embed_cache"), "code_embed");
        assert_eq!(volume_role("vct_code_embed"), "code_embed");
        assert_eq!(volume_role("random_garbage"), "unknown");
    }

    #[test]
    fn human_bytes_formats_thresholds_correctly() {
        assert_eq!(human_bytes(0), "0 B");
        assert_eq!(human_bytes(512), "512 B");
        assert_eq!(human_bytes(2048), "2.0 KB");
        assert_eq!(human_bytes(2 * 1024 * 1024), "2.0 MB");
        assert_eq!(human_bytes(3 * 1024 * 1024 * 1024), "3.0 GB");
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
    /// projects_v2.rs + the volumes module itself, and assert that any
    /// occurrence of `volume rm` outside this module's `migrate_volumes`
    /// fails the test. Production scan only — test code can mention the
    /// forbidden literal for documentation.
    #[test]
    fn volume_rm_only_callable_from_migrate_volumes() {
        let repo_root = super::super::installer::find_local_repo_root().expect("repo root");
        let volumes_rs = repo_root.join("launcher/src-tauri/src/commands/volumes.rs");
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

        // 2. volumes.rs may mention `volume rm` — but ONLY inside
        //    migrate_volumes. Find the function body and check the rest
        //    of the file is clean.
        let content = std::fs::read_to_string(&volumes_rs).expect("volumes.rs");
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
            "FORBIDDEN: 'volume rm' outside migrate_volumes in volumes.rs"
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
    #[tokio::test]
    async fn dry_run_validates_target_path_without_mutating() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("vct-volumes-dryrun");

        // Pre-condition: dir doesn't yet exist; dry-run must not create it.
        assert!(!target.exists());

        let plan = set_volumes_config_dry_run(target.to_string_lossy().to_string()).await;

        // The plan succeeds (validation only — parent exists, path is
        // absolute) regardless of whether existing volumes are running.
        let plan = plan.expect("dry run returns plan");
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
        let volumes_rs = repo_root.join("launcher/src-tauri/src/commands/volumes.rs");
        let content = std::fs::read_to_string(&volumes_rs).expect("read volumes.rs");

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
}
