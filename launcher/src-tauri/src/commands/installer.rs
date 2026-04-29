use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tauri::{command, Emitter, Window};

/// Upstream GitHub repo (used ONLY by auto-update — initial install is a
/// local file copy from the launcher's own bundled repo source, see
/// `find_local_repo_root` + `copy_orchestrator_to_sync`).
const ORCHESTRATOR_REPO: &str = "https://github.com/hotak92/vibecoded-orchestrator.git";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SystemDetection {
    pub os: String,
    pub arch: String,
    pub has_nvidia_gpu: bool,
    pub gpu_name: String,
    pub has_apple_silicon: bool,
    pub has_docker: bool,
    pub has_podman: bool,
    pub has_python: bool,
    pub python_version: String,
    pub python_cmd: String,
    pub has_claude_cli: bool,
    pub has_git: bool,
    pub has_node: bool,
    /// First container runtime found (e.g., "podman 4.7.0"), or null if none.
    /// Frontend reads this for the onboarding "Container runtime" row.
    pub container_runtime: Option<String>,
    /// Total system RAM in GB (rounded). 0 if detection failed.
    pub ram_gb: u64,
    /// Total VRAM in GB across discovered GPUs (NVIDIA preferred, then ROCm).
    /// 0 if no GPU detected.
    pub vram_gb: u64,
    /// "NVIDIA" | "AMD" | null. Used to label VRAM in the UI.
    pub gpu_vendor: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallConfig {
    pub install_path: String,
    pub use_gpu: bool,
    pub cpu_only: bool,
    pub openai_key: Option<String>,
    pub container_runtime: Option<String>, // "docker" | "podman" | null (auto)
    pub skip_containers: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallProgress {
    pub stage: String,
    pub message: String,
    pub percentage: f32,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallResult {
    pub success: bool,
    pub install_path: String,
    pub message: String,
    pub system: SystemDetection,
}

/// Bug 29: shared-container detection. Reports which of the three default
/// service endpoints are already serving on this machine (8081 / 11435 /
/// 11440), so the OnboardingWizard step 3 can tell the user "your install
/// will reuse these" before running the installer.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServicesStatus {
    /// Reachable URL or null when the probe failed.
    pub weaviate_url: Option<String>,
    pub ollama_url: Option<String>,
    pub code_embed_url: Option<String>,
    /// True iff all three probes succeeded — UI uses this to show the green
    /// "all detected, install will reuse them" panel vs the neutral
    /// "services not detected, install will start them" panel.
    pub all_detected: bool,
    /// True iff zero services responded — implies a fresh machine, install
    /// will need to start the compose stack.
    pub none_detected: bool,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum InstallMode {
    /// Empty or non-existent target. Fresh git clone.
    Fresh,
    /// Target has files (git or non-git) but NO `.claude/`. We refuse the
    /// fresh path and tell the caller to use Adopt.
    FreshIntoExisting,
    /// Target has `.claude/` (or other orchestrator-managed paths) — we
    /// can adopt by overwriting only orchestrator-managed paths and
    /// leaving everything else alone.
    Adopt,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallDiff {
    pub mode: InstallMode,
    /// Orchestrator-managed paths that already exist at the target and
    /// would be overwritten on adopt.
    pub will_overwrite: Vec<String>,
    /// Orchestrator-managed paths the install would create.
    pub will_add: Vec<String>,
    /// Anything outside the orchestrator-managed allowlist is reported
    /// here so the UI can reassure the user "your code is safe".
    pub user_paths_preserved: bool,
}

/// Hard whitelist of paths the orchestrator install is allowed to touch
/// at the project root. Everything else is treated as user code.
///
/// Keep this list in sync with the bundled install manifest (BOOTSTRAP.md
/// and install.py). The frontend mirrors a humanized version in the
/// confirm modal.
pub const ORCHESTRATOR_MANAGED_PATHS: &[&str] = &[
    ".claude",
    "CLAUDE.md",
    "knowledge",
    "claude_mcp_servers",
    "state",
    "config",
    "docs",
    "templates",
    "tools",
    "infrastructure",
    "requirements.txt",
    "requirements-dev.txt",
    "install.sh",
    "install.ps1",
    "install.py",
    "BOOTSTRAP.md",
    "vct-module.json",
];

// ---------------------------------------------------------------------------
// Re-install conflict resolution (4-option modal)
//
// When the install target already contains orchestrator files (`mode ==
// Adopt`), the wizard prompts the user with four explicit strategies
// instead of the cryptic "call preview_install + confirm_overwrite=true"
// error path. See docs/INSTALL_RECOVERY.md → "Conflict Resolution" for
// the user-facing description and the Claude self-merge contract.
//
// Strategy semantics:
//   - DeleteClaudeAndReinstall: rm -rf <install_path>/.claude THEN copy
//     fresh. Only `.claude/` is wiped — the rest of the install path may
//     contain real user code, wiping the whole path would be destructive.
//   - OverwriteAll: copy on top of existing files; user edits to
//     CLAUDE.md / CONTEXT_STATE.md / etc. are LOST.
//   - OverwritePreserve [DEFAULT]: copy on top BUT for each entry in the
//     preserve list, leave the existing file alone and write the upstream
//     version as `<file>.new.<ext>` next to it. Then append a notification
//     block to .claude/CONTEXT_STATE.md instructing Claude to merge.
//   - AdoptAsIs: equivalent to the legacy `confirm_overwrite=true` path —
//     do nothing on disk, just register the install.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ConflictStrategy {
    /// rm -rf <install_path>/.claude, then fresh copy.
    DeleteClaudeAndReinstall,
    /// Copy every tracked install file on top — no preservation.
    OverwriteAll,
    /// [DEFAULT] Copy + leave preserve-list entries alone + write
    /// `<file>.new.<ext>` siblings + append merge-notification block.
    OverwritePreserve,
    /// No-op on disk — just register.
    AdoptAsIs,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConflictResolution {
    pub strategy: ConflictStrategy,
    /// Optional override for the preserve list. None → use
    /// `DEFAULT_PRESERVE_LIST`. Paths are interpreted relative to the
    /// install root.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub preserve_paths: Option<Vec<String>>,
}

/// Default preserve list for `OverwritePreserve`. Paths are relative to
/// the install root. Keep in sync with `install.py::DEFAULT_PRESERVE_LIST`.
///
/// Note on MEMORY.md: lives at `~/.claude/projects/<id>/memory/MEMORY.md`,
/// NOT in the install dir. v1.0 treats it as out-of-scope-for-the-install
/// (no `.new.md` written). The notification block mentions this so the
/// user can manually merge if their MEMORY.md has diverged.
pub const DEFAULT_PRESERVE_LIST: &[&str] = &[
    "CLAUDE.md",
    ".claude/CONTEXT_STATE.md",
    ".claude/PROJECT_REGISTRY.md",
    ".env",
];

/// Marker comments delimiting the merge-notification block inside
/// `.claude/CONTEXT_STATE.md`. Re-runs of the install REPLACE the block
/// in-place rather than accumulating stale copies.
pub const MERGE_BLOCK_START: &str = "<!-- vct-merge-pending -->";
pub const MERGE_BLOCK_END: &str = "<!-- /vct-merge-pending -->";

/// Structured error returned by `install_orchestrator` when the target
/// already contains orchestrator files and the caller hasn't picked a
/// strategy. The frontend renders the 4-option conflict-resolution modal
/// from this payload.
///
/// Surfaced over Tauri as a JSON-encoded string in the Err variant: the
/// FE detects the `vct-conflict-error` discriminator field, parses the
/// JSON, and renders the modal. Plain (non-conflict) errors stay as
/// human-readable strings.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallConflictError {
    /// Discriminator — always "install_conflict". The frontend matches
    /// on this to distinguish a conflict from a generic install error.
    pub kind: String,
    /// Human-readable summary (also shown if the FE somehow can't parse
    /// the JSON).
    pub message: String,
    pub install_path: String,
    pub source_path: String,
    pub mode: InstallMode,
    /// Orchestrator paths that exist at the target and would be touched
    /// by a copy.
    pub will_overwrite: Vec<String>,
    /// Orchestrator paths the install would add.
    pub will_add: Vec<String>,
    /// Subset of `will_overwrite` that intersects the default preserve
    /// list (i.e. files the user has likely customised). Rendered first
    /// in the modal so the user sees the human cost.
    pub preserve_candidates: Vec<String>,
}

impl InstallConflictError {
    /// Serialize as a JSON string suitable for Tauri's Err variant. The
    /// frontend parses the leading `{"kind":"install_conflict"...}` shape
    /// and renders the modal.
    pub fn into_err_string(self) -> String {
        serde_json::to_string(&self).unwrap_or_else(|_| self.message)
    }
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

/// Detect system capabilities: GPU, container runtime, Python, etc.
#[command]
pub async fn detect_system() -> Result<SystemDetection, String> {
    let os = std::env::consts::OS.to_string();
    let arch = std::env::consts::ARCH.to_string();

    // Run detections in parallel via tokio
    let (
        nvidia_result,
        amd_result,
        podman_ver,
        docker_ver,
        python_result,
        claude,
        git,
        node,
    ) = tokio::join!(
        detect_nvidia_gpu(),
        detect_amd_gpu(),
        detect_runtime_version("podman"),
        detect_runtime_version("docker"),
        detect_python(),
        check_command_exists("claude"),
        check_command_exists("git"),
        check_command_exists("node"),
    );

    let (has_nvidia, gpu_name, nvidia_vram_gb) = nvidia_result;
    let (has_amd, amd_vram_gb) = amd_result;
    let (has_python, python_version, python_cmd) = python_result;
    let has_apple_silicon = os == "macos" && arch == "aarch64";

    let has_podman = podman_ver.is_some();
    let has_docker = docker_ver.is_some();

    // Prefer podman over docker (matches user's stated setup; both work
    // identically downstream).
    let container_runtime = podman_ver.clone().or_else(|| docker_ver.clone());

    let (vram_gb, gpu_vendor) = if has_nvidia {
        (nvidia_vram_gb, Some("NVIDIA".to_string()))
    } else if has_amd {
        (amd_vram_gb, Some("AMD".to_string()))
    } else {
        (0, None)
    };

    let ram_gb = detect_ram_gb();

    Ok(SystemDetection {
        os,
        arch,
        has_nvidia_gpu: has_nvidia,
        gpu_name,
        has_apple_silicon,
        has_docker,
        has_podman,
        has_python,
        python_version,
        python_cmd,
        has_claude_cli: claude,
        has_git: git,
        has_node: node,
        container_runtime,
        ram_gb,
        vram_gb,
        gpu_vendor,
    })
}

/// Default ports for the shared services. Match install.py constants —
/// changing them here without changing install.py would mean the wizard
/// reports "no services running" while install.py happily reuses them.
pub(crate) const DEFAULT_WEAVIATE_PORT: u16 = 8081;
pub(crate) const DEFAULT_OLLAMA_PORT: u16 = 11435;
pub(crate) const DEFAULT_CODE_EMBED_PORT: u16 = 11440;

/// HTTP probe with short timeout. Returns the URL on 2xx/3xx, None otherwise.
/// Takes an owned String so callers can compose URLs via format! without
/// having to keep the formatted string alive themselves.
async fn probe_http(url: String) -> Option<String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
        .ok()?;
    match client.get(&url).send().await {
        Ok(resp) if resp.status().as_u16() < 400 => Some(url),
        _ => None,
    }
}

/// Bug 29: detect already-running shared services so the OnboardingWizard
/// can tell the user "this install will reuse them" instead of starting
/// duplicate containers that bind-conflict.
///
/// Probes the three default endpoints in parallel:
///   - http://localhost:8081/v1/.well-known/ready  (Weaviate)
///   - http://localhost:11435/api/tags             (Ollama)
///   - http://localhost:11440/health               (code_embed)
#[command]
pub async fn detect_existing_services() -> Result<ServicesStatus, String> {
    let weaviate = probe_http(format!(
        "http://localhost:{}/v1/.well-known/ready",
        DEFAULT_WEAVIATE_PORT
    ));
    let ollama = probe_http(format!(
        "http://localhost:{}/api/tags",
        DEFAULT_OLLAMA_PORT
    ));
    let code_embed = probe_http(format!(
        "http://localhost:{}/health",
        DEFAULT_CODE_EMBED_PORT
    ));

    // Run probes concurrently — total wall time is capped at the 2s timeout
    // of the slowest probe, not 6s sequentially.
    let (weaviate_url, ollama_url, code_embed_url) =
        tokio::join!(weaviate, ollama, code_embed);

    let count = [&weaviate_url, &ollama_url, &code_embed_url]
        .iter()
        .filter(|o| o.is_some())
        .count();

    Ok(ServicesStatus {
        all_detected: count == 3,
        none_detected: count == 0,
        weaviate_url,
        ollama_url,
        code_embed_url,
    })
}

/// Get the default install path for the orchestrator.
#[command]
pub fn get_default_install_path() -> String {
    let home = directories::UserDirs::new()
        .map(|d| d.home_dir().to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));

    home.join("vibecoded-orchestrator")
        .to_string_lossy()
        .to_string()
}

/// Check if orchestrator is already installed at a given path.
#[command]
pub fn check_install_status(path: String) -> bool {
    let p = PathBuf::from(&path);
    p.join("CLAUDE.md").exists() && p.join("install.py").exists()
}

/// Get the installed version (latest git tag or commit hash).
#[command]
pub async fn get_installed_version(path: String) -> Result<String, String> {
    let p = PathBuf::from(&path);
    if !p.join(".git").exists() {
        return Err("Not a git repository".to_string());
    }

    // Try tag first
    let tag_output = tokio::process::Command::new("git")
        .args(["describe", "--tags", "--abbrev=0"])
        .current_dir(&p)
        .output()
        .await
        .map_err(|e| e.to_string())?;

    if tag_output.status.success() {
        let tag = String::from_utf8_lossy(&tag_output.stdout).trim().to_string();
        if !tag.is_empty() {
            return Ok(tag);
        }
    }

    // Fall back to short commit hash
    let hash_output = tokio::process::Command::new("git")
        .args(["rev-parse", "--short", "HEAD"])
        .current_dir(&p)
        .output()
        .await
        .map_err(|e| e.to_string())?;

    if hash_output.status.success() {
        return Ok(String::from_utf8_lossy(&hash_output.stdout).trim().to_string());
    }

    Err("Could not determine version".to_string())
}

/// Check if updates are available (local behind remote).
#[command]
pub async fn check_for_updates(path: String) -> Result<bool, String> {
    let p = PathBuf::from(&path);
    if !p.join(".git").exists() {
        return Err("Not a git repository".to_string());
    }

    // Fetch without merging
    let fetch = tokio::process::Command::new("git")
        .args(["fetch", "--quiet"])
        .current_dir(&p)
        .output()
        .await
        .map_err(|e| format!("git fetch failed: {}", e))?;

    if !fetch.status.success() {
        return Err("git fetch failed".to_string());
    }

    // Check if local is behind remote
    let status = tokio::process::Command::new("git")
        .args(["status", "-uno", "--porcelain=v2", "--branch"])
        .current_dir(&p)
        .output()
        .await
        .map_err(|e| e.to_string())?;

    let output = String::from_utf8_lossy(&status.stdout);
    // Look for "# branch.ab +N -M" where M > 0 means behind
    for line in output.lines() {
        if line.starts_with("# branch.ab") {
            if let Some(behind) = line.split_whitespace().last() {
                if let Ok(n) = behind.trim_start_matches('-').parse::<i32>() {
                    return Ok(n > 0);
                }
            }
        }
    }

    Ok(false)
}

/// Classify what an install target looks like:
/// - `Fresh` if the path doesn't exist or is empty.
/// - `Adopt` if it has `.claude/` (or any other orchestrator-managed path).
/// - `FreshIntoExisting` if it has user files but no orchestrator artifacts.
pub fn classify_install_target(install_path: &Path) -> InstallMode {
    if !install_path.exists() {
        return InstallMode::Fresh;
    }
    if !install_path.is_dir() {
        return InstallMode::FreshIntoExisting;
    }

    // Empty directory? -> Fresh
    let entries: Vec<_> = match std::fs::read_dir(install_path) {
        Ok(rd) => rd.flatten().collect(),
        Err(_) => return InstallMode::Fresh,
    };
    if entries.is_empty() {
        return InstallMode::Fresh;
    }

    // Look for any orchestrator-managed artifact
    for managed in ORCHESTRATOR_MANAGED_PATHS {
        if install_path.join(managed).exists() {
            return InstallMode::Adopt;
        }
    }

    InstallMode::FreshIntoExisting
}

/// Diff the bundled manifest against `install_path`. The "manifest" here
/// is just the orchestrator-managed allowlist plus anything else our
/// future bundle might ship — we look for what already exists at the
/// target and report it as overwrite-candidates.
pub fn diff_install(install_path: &Path) -> InstallDiff {
    let mode = classify_install_target(install_path);

    let mut will_overwrite: Vec<String> = Vec::new();
    let mut will_add: Vec<String> = Vec::new();

    for managed in ORCHESTRATOR_MANAGED_PATHS {
        let p = install_path.join(managed);
        if p.exists() {
            will_overwrite.push(managed.to_string());
        } else {
            will_add.push(managed.to_string());
        }
    }

    InstallDiff {
        mode,
        will_overwrite,
        will_add,
        user_paths_preserved: true,
    }
}

/// Preview what install_orchestrator would do at `config.install_path`.
/// Frontend calls this before showing the adopt-confirm modal.
#[command]
pub async fn preview_install(config: InstallConfig) -> Result<InstallDiff, String> {
    let install_path = PathBuf::from(&config.install_path);
    Ok(diff_install(&install_path))
}

/// Return the local repo root path (used by frontend to show "Copying
/// from {path}" in the install UI).
#[command]
pub fn get_local_repo_source() -> Result<String, String> {
    find_local_repo_root().map(|p| p.to_string_lossy().to_string())
}

/// Returns true iff `root` is a complete vco install — i.e. it has the
/// canonical install markers: CLAUDE.md + install.py + .venv/. The
/// .venv check is the key discriminator between a bundled source tree
/// (CLAUDE.md + install.py exist but .venv doesn't) and a finished
/// install. Extracted for unit-testability so we can exercise the
/// predicate without depending on `find_local_repo_root`'s runtime walk
/// from `current_exe()`.
///
/// 2026-04-28 (Bug 6 risk callout): source-repo checkouts ship
/// `install.py` + `CLAUDE.md` and developers commonly have a `.venv/`
/// next to them. The original three-marker check was therefore unable
/// to tell a "completed install" apart from a "vco source checkout I'm
/// currently developing in", which produced false positives in the
/// wizard preflight (`detect_existing_install_root`) and caused the
/// onboarding to skip past steps it should have shown. Add the same
/// state-or-env-with-KG marker as `is_completed_install_root` so both
/// call sites agree on what "completed install" means.
fn install_root_complete_at(root: &Path) -> bool {
    root.join("CLAUDE.md").is_file()
        && root.join("install.py").is_file()
        && root.join(".venv").is_dir()
        && (root.join("state").is_dir() || env_contains_kg(&root.join(".env")))
}

/// 2026-04-29 fix (wizard install-path lockdown): assert that
/// `install_path` is a vco source repo before any install-time
/// mutations run. The wizard previously let the user pick an arbitrary
/// empty folder; install_orchestrator would then copy
/// `ORCHESTRATOR_MANAGED_PATHS` into that folder — which is a SUBSET
/// (no launcher/, no first-install.sh, no scripts/) and produced an
/// "orphan" install the end user couldn't run. The discriminator is
/// the presence of `install.py` AND `first-install.sh` next to each
/// other; both ship in every vco clone and tarball, neither is in
/// ORCHESTRATOR_MANAGED_PATHS so they can't appear in a half-install.
///
/// Returns `Err` with a user-actionable message when validation fails;
/// returns `Ok(())` when the path looks like a vco source repo.
pub fn validate_source_repo(install_path: &Path) -> Result<(), String> {
    let install_py = install_path.join("install.py");
    let first_install_sh = install_path.join("first-install.sh");
    if !install_py.is_file() || !first_install_sh.is_file() {
        return Err(format!(
            "Install path must be a vco source repo (must contain install.py + first-install.sh). \
             To install at a different location, clone the repo there and re-run from that folder. \
             Got: {}",
            install_path.display()
        ));
    }
    Ok(())
}

/// Detect whether the launcher is running from inside an already-installed
/// orchestrator (i.e. the user did `bash first-install.sh` and we are now
/// inside that install). The wizard uses this to skip the install step
/// rather than prompt the user to install AGAIN somewhere else — a real
/// UX bug surfaced in 2026-04-27 testing where the wizard's default install
/// path was `~/vibecoded-orchestrator` and a user typing an absolute path
/// like `~/Desktop/PROGETTI/Agape/Code` would get the two paths concatenated
/// and write the orchestrator INSIDE the project folder. With self-detection
/// the wizard can skip directly to project registration.
///
/// Returns Some(path) when:
///  - we can locate the repo root (find_local_repo_root succeeds), AND
///  - that root contains the canonical install markers (see
///    install_root_complete_at).
#[command]
pub fn detect_existing_install_root() -> Option<String> {
    let root = find_local_repo_root().ok()?;
    if install_root_complete_at(&root) {
        Some(root.to_string_lossy().to_string())
    } else {
        None
    }
}

// ---------------------------------------------------------------------------
// Bug 32: pre-flight install safety check.
//
// Before the user confirms an install, we surface EXACTLY what will and
// will not be touched. The frontend renders a "safety check" panel from
// the SafetyReport struct and shows the user a final Cancel / Confirm
// step. Risks list is the user's last chance to back out.
//
// All probes are read-only: we do NOT spawn `podman volume rm`, do NOT
// call `compose down`, do NOT POST/DELETE to Weaviate. The Weaviate
// schema is read via GET /v1/schema. Container volumes are listed via
// `podman/docker volume ls` + `volume inspect` (read-only commands).
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExistingVolume {
    pub name: String,
    pub mountpoint: String,
    pub driver: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SafetyReport {
    /// Orchestrator-managed paths that adopt-flow would change at the
    /// install target. Read-merge-write applies for some (e.g.
    /// `.claude/settings.json`) but the file IS modified.
    pub will_overwrite_orchestrator_files: Vec<String>,
    /// Sample of paths INSIDE `install_path` that are NOT in the
    /// orchestrator-managed allowlist — i.e. user code we will never
    /// touch. Capped to 50 entries for UI rendering.
    pub will_preserve_user_code: Vec<String>,
    /// Existing Podman/Docker volumes whose names match the four
    /// orchestrator volumes. The launcher will NOT remove or rebind
    /// these — they are surfaced so the user can see what data is
    /// already on disk.
    pub existing_volumes: Vec<ExistingVolume>,
    /// Weaviate classes that already exist on the running instance.
    /// Will be preserved (no PUT/DELETE; only POST of missing classes).
    pub existing_collections: Vec<String>,
    /// Weaviate classes the install would POST. Empty if Weaviate is
    /// not reachable or all required classes already exist.
    pub new_collections_to_add: Vec<String>,
    /// Services already responding on their default port. Will NOT be
    /// restarted by the install.
    pub services_running: Vec<String>,
    /// Services the install would `compose up -d`. Empty if everything
    /// is already up.
    pub services_to_start: Vec<String>,
    /// Free-form risk lines for the UI to render under a warning icon.
    /// Empty list = "all clear".
    pub risks: Vec<String>,
}

/// All volume names the orchestrator might own at this point on a
/// machine. Includes:
///   - canonical names from `infrastructure/docker-compose.yml`
///     (`weaviate_data`, `ollama_data`, `code_embed_cache`)
///   - historical / project-suffixed names users may have created
///     before the canonical compose file existed (Bug 31 migration)
///
/// Used by both Bug 32 (preflight surfacing) and Bug 31 (volume picker
/// existing-volume detection).
pub const ORCHESTRATOR_VOLUME_NAMES: &[&str] = &[
    // Canonical (current compose)
    "weaviate_data",
    "ollama_data",
    "code_embed_cache",
    // Historical project-suffixed names (Bug 31 — generate `external: true`
    // mapping to keep the user's existing data without recreating volumes)
    "weaviate_claude",
    "weaviate_ARTup",
    "ollama_claude",
    "ollama_ARTup",
    "vct_code_embed",
];

/// Read-only volume detection. Tries `podman volume ls --format json`
/// first, falls back to `docker volume ls --format json`. If neither is
/// installed we return an empty list (not an error — the user may not
/// have a container runtime yet, which is fine pre-install).
///
/// Bug 31: also exposed as `detect_existing_volumes_for_volumes_module`
/// so the volumes command module can reuse the same detector instead
/// of duplicating it.
pub async fn detect_existing_volumes_for_volumes_module() -> Vec<ExistingVolume> {
    detect_existing_volumes().await
}

async fn detect_existing_volumes() -> Vec<ExistingVolume> {
    for runtime in &["podman", "docker"] {
        let runtime_path = match which_on_path(runtime) {
            Some(p) => p,
            None => continue,
        };
        // List names matching one of our known orchestrator volume names.
        let mut found: Vec<ExistingVolume> = Vec::new();
        for name in ORCHESTRATOR_VOLUME_NAMES {
            // `volume inspect <name>` returns 0 with JSON if it exists,
            // non-zero if not. Read-only — never mutates state.
            let out = tokio::process::Command::new(&runtime_path)
                .args(["volume", "inspect", name])
                .output()
                .await;
            let out = match out {
                Ok(o) => o,
                Err(_) => continue,
            };
            if !out.status.success() {
                continue;
            }
            let body = String::from_utf8_lossy(&out.stdout);
            // Both podman and docker emit a JSON array of volume objects.
            let arr: serde_json::Value = match serde_json::from_str(&body) {
                Ok(v) => v,
                Err(_) => continue,
            };
            if let Some(items) = arr.as_array() {
                for item in items {
                    let mountpoint = item
                        .get("Mountpoint")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    let driver = item
                        .get("Driver")
                        .and_then(|v| v.as_str())
                        .unwrap_or("local")
                        .to_string();
                    found.push(ExistingVolume {
                        name: name.to_string(),
                        mountpoint,
                        driver,
                    });
                }
            }
        }
        if !found.is_empty() {
            return found;
        }
    }
    Vec::new()
}

fn which_on_path(name: &str) -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths).find_map(|dir| {
            let p = dir.join(name);
            if p.is_file() { Some(p) } else { None }
        })
    })
}

/// Read-only Weaviate schema probe. Returns the list of class names
/// already present on the running instance; empty Vec if Weaviate is
/// not reachable.
async fn detect_existing_collections() -> Vec<String> {
    let url = format!(
        "http://localhost:{}/v1/schema",
        DEFAULT_WEAVIATE_PORT
    );
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
    {
        Ok(c) => c,
        Err(_) => return Vec::new(),
    };
    let resp = match client.get(&url).send().await {
        Ok(r) if r.status().is_success() => r,
        _ => return Vec::new(),
    };
    let body: serde_json::Value = match resp.json().await {
        Ok(b) => b,
        Err(_) => return Vec::new(),
    };
    body.get("classes")
        .and_then(|c| c.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|c| c.get("class").and_then(|v| v.as_str()).map(String::from))
                .collect()
        })
        .unwrap_or_default()
}

/// Walk `path` non-recursively (depth 1 — the immediate children at the
/// install target), collecting up to `cap` entries that are NOT in the
/// orchestrator-managed allowlist. Used to reassure the user "your code
/// in <path>/foo is untouched".
fn list_user_paths_under(path: &Path, cap: usize) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let read = match std::fs::read_dir(path) {
        Ok(rd) => rd,
        Err(_) => return out,
    };
    for entry in read.flatten() {
        let name = entry.file_name();
        let name_str = name.to_string_lossy().to_string();
        // Skip orchestrator-managed entries.
        if ORCHESTRATOR_MANAGED_PATHS.iter().any(|m| *m == name_str) {
            continue;
        }
        // Skip dotfiles that might be IDE/git noise — we still preserve
        // them but don't bloat the report.
        if name_str.starts_with('.') {
            continue;
        }
        out.push(name_str);
        if out.len() >= cap {
            break;
        }
    }
    out.sort();
    out
}

/// Pre-flight safety check. Renders the "you're about to install" panel
/// in the frontend. Fully read-only — never mutates Weaviate, container
/// state, or files at `install_path`.
#[command]
pub async fn preflight_install_safety_check(
    install_path: String,
) -> Result<SafetyReport, String> {
    let target = PathBuf::from(&install_path);

    // 1. What orchestrator files are at the target? (will_overwrite from
    //    diff_install — those are the files we'll modify; everything
    //    else at the path is user code we leave alone.)
    let diff = diff_install(&target);
    let will_overwrite_orchestrator_files = diff.will_overwrite.clone();

    // 2. User-code reassurance list — non-recursive, capped at 50.
    let will_preserve_user_code = if target.is_dir() {
        list_user_paths_under(&target, 50)
    } else {
        Vec::new()
    };

    // 3. Existing volumes — never touched.
    let existing_volumes = detect_existing_volumes().await;

    // 4. Existing Weaviate classes — preserved.
    let existing_collections = detect_existing_collections().await;

    // 5. Required classes for THIS install. Mirror install.py
    //    `_ensure_collections` — code-graph classes are shared and
    //    created lazily, so they're not in the per-install required set.
    let required_classes = ["KnowledgeGraph", "Development"];
    let new_collections_to_add: Vec<String> = required_classes
        .iter()
        .filter(|c| !existing_collections.contains(&(**c).to_string()))
        .map(|c| (*c).to_string())
        .collect();

    // 6. Services up/down classification.
    let services = detect_existing_services().await.unwrap_or(ServicesStatus {
        all_detected: false,
        none_detected: true,
        weaviate_url: None,
        ollama_url: None,
        code_embed_url: None,
    });
    let mut services_running: Vec<String> = Vec::new();
    let mut services_to_start: Vec<String> = Vec::new();
    if services.weaviate_url.is_some() {
        services_running.push("weaviate".to_string());
    } else {
        services_to_start.push("weaviate".to_string());
    }
    if services.ollama_url.is_some() {
        services_running.push("ollama".to_string());
    } else {
        services_to_start.push("ollama".to_string());
    }
    // code_embed is GPU-only and started conditionally — surface it only
    // if it's actively running. We don't predict whether install would
    // start it (that's --gpu-dependent).
    if services.code_embed_url.is_some() {
        services_running.push("code_embed".to_string());
    }

    // 7. Risk list. Empty = all clear.
    let mut risks: Vec<String> = Vec::new();
    for path in &will_overwrite_orchestrator_files {
        // .claude/settings.json + .vscode/settings.json are
        // read-merge-write — call that out so the user knows their
        // hooks/permissions/IDE settings survive.
        if path == ".claude" {
            risks.push(
                ".claude/settings.json env block will be added or updated; existing hooks, permissions, and agents config are preserved."
                    .to_string(),
            );
        } else {
            risks.push(format!("Will overwrite orchestrator-managed path: {}", path));
        }
    }
    if !existing_volumes.is_empty() {
        // Bug 32 #4 + Bug 31: bind-mount override is suppressed when
        // existing volumes are detected. Tell the user.
        risks.push(format!(
            "Detected {} existing orchestrator container volume(s). They will be reused as-is (NOT removed, NOT rebound). To migrate volume data to a different folder, use Settings → Volumes → Migrate after install.",
            existing_volumes.len()
        ));
    }

    Ok(SafetyReport {
        will_overwrite_orchestrator_files,
        will_preserve_user_code,
        existing_volumes,
        existing_collections,
        new_collections_to_add,
        services_running,
        services_to_start,
        risks,
    })
}

/// Install the orchestrator by COPYING from the launcher's local repo
/// source to `config.install_path` (Bug 17). No network, no `git clone`,
/// no GitHub PAT. After the copy, optionally invoke `install.py` for
/// post-copy setup (venv + container start). Emits "install_progress"
/// events.
///
/// Conflict resolution (re-install over an existing install):
///   - `conflict: Some(ConflictResolution)` → execute the chosen
///     strategy (preferred path; the wizard always sends this).
///   - `conflict: None && confirm_overwrite: true` → legacy adopt path,
///     equivalent to `ConflictStrategy::AdoptAsIs`. Kept for backward
///     compatibility with any caller that hasn't been migrated yet.
///   - `conflict: None && confirm_overwrite: false` on an Adopt target →
///     refuse with `InstallConflictError` (JSON-encoded in the Err
///     variant). The frontend parses this and renders the 4-option
///     conflict-resolution modal.
#[command]
pub async fn install_orchestrator(
    config: InstallConfig,
    confirm_overwrite: Option<bool>,
    conflict: Option<ConflictResolution>,
    window: Window,
) -> Result<InstallResult, String> {
    let install_path = PathBuf::from(&config.install_path);

    // 2026-04-29 fix (wizard install-path lockdown): refuse to install
    // into a directory that isn't a vco source repo. The previous code
    // path let the wizard target an arbitrary empty folder and copy a
    // SUBSET of files in (per ORCHESTRATOR_MANAGED_PATHS — no launcher,
    // no first-install.sh, no scripts/), producing a half-installed
    // orphan that the end user couldn't run. Install is in-place; the
    // install_path must be the cloned source repo. Defensive check
    // BEFORE any filesystem mutations.
    validate_source_repo(&install_path)?;

    let system = detect_system().await?;

    if !system.has_python {
        return Err(format!(
            "Python 3.11+ is required. Install from https://python.org"
        ));
    }

    // Resolve the effective conflict strategy.
    let mode = classify_install_target(&install_path);
    let confirmed = confirm_overwrite.unwrap_or(false);
    let effective_conflict: Option<ConflictResolution> = match (&conflict, confirmed) {
        (Some(c), _) => Some(c.clone()),
        // Legacy: confirm_overwrite=true with no explicit strategy → AdoptAsIs.
        (None, true) => Some(ConflictResolution {
            strategy: ConflictStrategy::AdoptAsIs,
            preserve_paths: None,
        }),
        (None, false) => None,
    };

    if mode == InstallMode::Adopt && effective_conflict.is_none() {
        // Build a structured error for the FE to render the conflict
        // modal. Source path may fail to resolve in test envs — fall
        // back to a placeholder rather than aborting on the resolve.
        let source_for_err = find_local_repo_root()
            .ok()
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or_else(|| "<source not found>".to_string());
        let diff = diff_install(&install_path);
        let preserve_candidates: Vec<String> = diff
            .will_overwrite
            .iter()
            .filter(|p| DEFAULT_PRESERVE_LIST.iter().any(|x| x == p))
            .cloned()
            .collect();
        let err = InstallConflictError {
            kind: "install_conflict".to_string(),
            message: format!(
                "Install path {} already contains orchestrator files. \
                 Pick a conflict-resolution strategy.",
                install_path.display()
            ),
            install_path: install_path.to_string_lossy().to_string(),
            source_path: source_for_err,
            mode,
            will_overwrite: diff.will_overwrite,
            will_add: diff.will_add,
            preserve_candidates,
        };
        return Err(err.into_err_string());
    }

    // Stage 1: Locate local source
    emit_progress(&window, "locate", "Locating orchestrator source...", 5.0);
    let source = find_local_repo_root()?;
    emit_progress(
        &window,
        "locate",
        &format!("Source: {}", source.display()),
        10.0,
    );

    // Auto-create the install directory tree if it doesn't exist.
    tokio::fs::create_dir_all(&install_path)
        .await
        .map_err(|e| format!("Cannot create install directory {}: {}", install_path.display(), e))?;

    // Stage 2: Copy orchestrator-managed paths.
    //
    // Three branches:
    //   1. Conflict strategy provided AND target is an Adopt target →
    //      run the strategy (handles its own copy/preserve/notify).
    //   2. Conflict strategy provided but target is NOT an Adopt target
    //      (Fresh / FreshIntoExisting) → strategy is irrelevant; do a
    //      plain copy. This keeps the wizard's behaviour stable when
    //      the user picked a strategy on a path that subsequently
    //      stopped being an adopt-target between preview and install.
    //   3. No strategy + classify said Fresh/FreshIntoExisting → plain
    //      copy.
    emit_progress(&window, "copy", "Copying orchestrator files...", 15.0);
    let target_clone = install_path.clone();
    let source_clone = source.clone();
    let strategy_for_copy = effective_conflict.clone();
    let conflict_report = tokio::task::spawn_blocking(move || -> Result<Option<ConflictApplyReport>, String> {
        if let Some(c) = strategy_for_copy {
            if classify_install_target(&target_clone) == InstallMode::Adopt {
                let preserve = c
                    .preserve_paths
                    .clone()
                    .unwrap_or_else(|| {
                        DEFAULT_PRESERVE_LIST.iter().map(|s| s.to_string()).collect()
                    });
                let report = apply_conflict_strategy(
                    &source_clone,
                    &target_clone,
                    c.strategy,
                    &preserve,
                )?;
                return Ok(Some(report));
            }
        }
        // Plain copy path (no conflict OR target became fresh).
        copy_orchestrator_to_sync(&source_clone, &target_clone)?;
        Ok(None)
    })
    .await
    .map_err(|e| format!("copy task panicked: {}", e))?;
    let conflict_report = conflict_report?;

    // Emit a structured event in the install log so the conflict-resolve
    // step is auditable. Best-effort; never fails the install.
    if let Some(ref report) = conflict_report {
        let log_path = install_path.join("state").join("logs").join("install.jsonl");
        let _ = append_install_log_event(
            &log_path,
            "conflict-resolve",
            "ok",
            &format!("strategy={}", report.strategy),
            Some(serde_json::json!({
                "strategy": report.strategy,
                "preserved_count": report.preserved_count,
                "new_md_count": report.new_md_count,
                "notification_written": report.notification_written,
                "copied_count": report.copied_count,
            })),
        );
    }

    emit_progress(&window, "copy", "Files copied", 70.0);

    // Stage 3: Run install.py for post-copy setup (venv, containers, etc.)
    emit_progress(&window, "install", "Running post-copy setup...", 75.0);

    let mut install_args = vec!["install.py".to_string()];

    // The launcher invokes install.py as a non-interactive subprocess.
    // ALWAYS pass --quiet + --no-joern so install.py never blocks waiting
    // for stdin input that the launcher can't provide. Reported 2026-04-28
    // from real wizard test: project creation hung at step 2b/10 (Joern
    // prompt) because tokio::process::Command inherits stdin and the
    // launcher's stdin can register as a TTY via webkit2gtk inheritance.
    //
    // CLI users who DO want Joern install via the launcher path will need
    // to either rerun install.py manually with --with-joern or use a
    // future "install Joern" tray action.
    install_args.push("--quiet".to_string());
    install_args.push("--no-joern".to_string());

    if config.use_gpu {
        install_args.push("--gpu".to_string());
    }
    if config.cpu_only {
        install_args.push("--cpu-only".to_string());
    }
    if let Some(ref key) = config.openai_key {
        if !key.is_empty() {
            install_args.push("--openai-key".to_string());
            install_args.push(key.clone());
        }
    }
    if let Some(ref runtime) = config.container_runtime {
        install_args.push("--container".to_string());
        install_args.push(runtime.clone());
    }
    if config.skip_containers {
        install_args.push("--no-containers".to_string());
    }

    let python_cmd = &system.python_cmd;
    let mut cmd = tokio::process::Command::new(python_cmd);
    cmd.args(&install_args)
        // Defense-in-depth: explicitly close stdin so install.py's input()
        // calls receive EOF instead of blocking indefinitely. The --quiet
        // + --no-joern flags above should already prevent any prompt, but
        // a future code path that adds another input() would re-introduce
        // the hang. Stdin=null makes the hang impossible.
        .stdin(std::process::Stdio::null())
        .current_dir(&install_path);

    // Windows: suppress the transient cmd console window that pops up
    // when Tauri (a windowed app, no console) spawns a subprocess.
    // Without CREATE_NO_WINDOW, Windows materialises an empty cmd
    // window for the subprocess's stdin/stdout, which the user sees
    // as "an empty terminal window named python.exe". Reported
    // 2026-04-28 from a Windows wizard test.
    //
    // CREATE_NO_WINDOW = 0x08000000. Per Microsoft docs:
    // https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }

    let install_output = cmd
        .output()
        .await
        .map_err(|e| format!("install.py failed to start: {}", e))?;

    let stdout = String::from_utf8_lossy(&install_output.stdout);
    let stderr = String::from_utf8_lossy(&install_output.stderr);

    if !install_output.status.success() {
        emit_progress(&window, "error", &format!("Installation failed: {}", stderr), 0.0);
        return Err(format!(
            "install.py failed (exit {}):\n{}\n{}",
            install_output.status, stdout, stderr
        ));
    }

    emit_progress(&window, "install", "Installation complete", 90.0);

    // Stage 3: Verify
    emit_progress(&window, "verify", "Verifying installation...", 95.0);

    let installed = check_install_status(config.install_path.clone());
    if !installed {
        return Err("Installation completed but verification failed".to_string());
    }

    emit_progress(&window, "done", "Orchestrator installed successfully!", 100.0);

    Ok(InstallResult {
        success: true,
        install_path: config.install_path,
        message: "Orchestrator installed successfully".to_string(),
        system,
    })
}

/// Update an existing orchestrator installation.
#[command]
pub async fn update_orchestrator(
    path: String,
    window: Window,
) -> Result<InstallResult, String> {
    let install_path = PathBuf::from(&path);
    let system = detect_system().await?;

    if !install_path.join(".git").exists() {
        return Err("Not a git repository — cannot update".to_string());
    }

    // Stage 1: Pull latest
    emit_progress(&window, "update", "Pulling latest changes...", 10.0);

    let pull = tokio::process::Command::new("git")
        .args(["pull", "--ff-only"])
        .current_dir(&install_path)
        .output()
        .await
        .map_err(|e| format!("git pull failed: {}", e))?;

    if !pull.status.success() {
        let stderr = String::from_utf8_lossy(&pull.stderr);
        return Err(format!("git pull failed: {}", stderr));
    }

    let pull_output = String::from_utf8_lossy(&pull.stdout);
    if pull_output.contains("Already up to date") {
        emit_progress(&window, "done", "Already up to date!", 100.0);
        return Ok(InstallResult {
            success: true,
            install_path: path,
            message: "Already up to date".to_string(),
            system,
        });
    }

    emit_progress(&window, "update", "Changes pulled", 30.0);

    // Stage 2: Re-run install.py with --update flag
    emit_progress(&window, "install", "Applying updates...", 40.0);

    let python_cmd = &system.python_cmd;
    let mut cmd = tokio::process::Command::new(python_cmd);
    cmd.args(["install.py", "--update"])
        .stdin(std::process::Stdio::null())
        .current_dir(&install_path);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    let install_output = cmd
        .output()
        .await
        .map_err(|e| format!("install.py --update failed: {}", e))?;

    if !install_output.status.success() {
        let stderr = String::from_utf8_lossy(&install_output.stderr);
        return Err(format!("Update failed: {}", stderr));
    }

    emit_progress(&window, "done", "Orchestrator updated successfully!", 100.0);

    Ok(InstallResult {
        success: true,
        install_path: path,
        message: "Orchestrator updated successfully".to_string(),
        system,
    })
}

// ---------------------------------------------------------------------------
// Local-copy install (Bug 17): the launcher binary IS built from the
// orchestrator repo. The repo source ships next to the binary. Install =
// copy that local source into the user's chosen install_path. No network
// access, no GitHub PAT required, no `git clone`.
// ---------------------------------------------------------------------------

/// Locate the orchestrator repo root (the folder containing
/// `vct-module.json`). Tries three strategies in order:
///
/// 1. Build-time `VCT_REPO_ROOT` env var (set by the bundler when the
///    binary is shipped with the repo embedded next to it).
/// 2. Walk up from the running binary's directory.
/// 3. Walk up from `CARGO_MANIFEST_DIR` (works in dev / `cargo run`).
pub fn find_local_repo_root() -> Result<PathBuf, String> {
    // Strategy 1: build-time env var
    if let Some(p) = option_env!("VCT_REPO_ROOT") {
        let candidate = PathBuf::from(p);
        if candidate.join("vct-module.json").exists() {
            return Ok(candidate);
        }
    }

    // Strategy 2: walk up from the running binary
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let mut current = parent.to_path_buf();
            for _ in 0..10 {
                if current.join("vct-module.json").exists() {
                    return Ok(current);
                }
                if !current.pop() {
                    break;
                }
            }
        }
    }

    // Strategy 3: walk up from CARGO_MANIFEST_DIR (dev-mode fallback)
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    let mut current = PathBuf::from(manifest_dir);
    for _ in 0..6 {
        if current.join("vct-module.json").exists() {
            return Ok(current);
        }
        if !current.pop() {
            break;
        }
    }

    Err("Could not locate orchestrator repo root containing vct-module.json. \
         Set VCT_REPO_ROOT or run from a checkout."
        .to_string())
}

/// Synchronous recursive copy. Symlinks are resolved (file content
/// follows). Used by `copy_orchestrator_to_sync` so the caller (which is
/// already an async Tauri command) can `tokio::task::spawn_blocking` it.
fn copy_recursive_sync(src: &Path, dst: &Path) -> std::io::Result<()> {
    let meta = std::fs::metadata(src)?;
    if meta.is_dir() {
        std::fs::create_dir_all(dst)?;
        for entry in std::fs::read_dir(src)? {
            let entry = entry?;
            let s = entry.path();
            let d = dst.join(entry.file_name());
            copy_recursive_sync(&s, &d)?;
        }
    } else {
        if let Some(parent) = dst.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::copy(src, dst)?;
    }
    Ok(())
}

/// Copy every entry in `ORCHESTRATOR_MANAGED_PATHS` from `source` to
/// `target`. Missing source entries are silently skipped (some allowlist
/// entries are optional). Returns the source path that was used so the
/// caller can show it in the UI.
pub fn copy_orchestrator_to_sync(source: &Path, target: &Path) -> Result<(), String> {
    if !source.join("vct-module.json").exists() {
        return Err(format!(
            "source {} is not an orchestrator repo (no vct-module.json)",
            source.display()
        ));
    }
    std::fs::create_dir_all(target)
        .map_err(|e| format!("cannot create target {}: {}", target.display(), e))?;

    for managed in ORCHESTRATOR_MANAGED_PATHS {
        let src = source.join(managed);
        let dst = target.join(managed);
        if !src.exists() {
            continue;
        }
        copy_recursive_sync(&src, &dst).map_err(|e| {
            format!("copy {} -> {}: {}", src.display(), dst.display(), e)
        })?;
    }
    Ok(())
}

/// Result of running a `ConflictStrategy` against an install target.
/// Used by the install-log emitter and the FE success toast.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ConflictApplyReport {
    pub strategy: String,
    /// Number of preserve-list paths that already existed at the target
    /// and were left untouched (only meaningful for OverwritePreserve).
    pub preserved_count: usize,
    /// Number of `<file>.new.<ext>` siblings written next to preserved
    /// files (only meaningful for OverwritePreserve).
    pub new_md_count: usize,
    /// Whether the merge-notification block was written / refreshed in
    /// `.claude/CONTEXT_STATE.md`.
    pub notification_written: bool,
    /// Number of files copied from source to target on top of existing
    /// content. 0 for AdoptAsIs.
    pub copied_count: usize,
}

/// Insert `.new` before the file's extension. Examples:
///   - `CLAUDE.md` -> `CLAUDE.new.md`
///   - `.env` -> `.env.new` (no extension to split, append at end)
///   - `archive.tar.gz` -> `archive.tar.new.gz` (split on LAST `.`)
fn new_sibling_path(path: &Path) -> PathBuf {
    let parent = path.parent().unwrap_or_else(|| Path::new(""));
    let file_name = match path.file_name().and_then(|n| n.to_str()) {
        Some(n) => n,
        None => return path.with_extension("new"),
    };
    // Filename starting with `.` and no other `.` (e.g. `.env`) → treat
    // the leading dot as part of the stem so we don't write `.new.env`.
    let dot_idx = file_name.rfind('.').filter(|&i| i > 0);
    let new_name = match dot_idx {
        Some(i) => format!("{}.new{}", &file_name[..i], &file_name[i..]),
        None => format!("{}.new", file_name),
    };
    parent.join(new_name)
}

/// Append (or refresh) the merge-notification block in
/// `.claude/CONTEXT_STATE.md`. Idempotent: if a previous block exists, it
/// is REPLACED in-place rather than duplicated. The block is bounded by
/// the marker comments `MERGE_BLOCK_START` / `MERGE_BLOCK_END`.
///
/// Returns `true` iff the file was written (i.e. block needed adding or
/// updating). Returns `false` if the block was already present and
/// identical to what we'd write.
pub fn update_merge_notification_block(
    context_state_path: &Path,
    preserved_files: &[String],
) -> std::io::Result<bool> {
    let block = build_merge_notification_block(preserved_files);

    // CONTEXT_STATE.md ought to exist by the time we get here (we only
    // call this after a copy step that populates `.claude/`), but guard
    // anyway: if missing, create with just the block.
    if !context_state_path.exists() {
        if let Some(parent) = context_state_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(context_state_path, &block)?;
        return Ok(true);
    }

    let existing = std::fs::read_to_string(context_state_path)?;
    let updated = replace_or_append_block(&existing, &block);
    if updated == existing {
        return Ok(false);
    }
    std::fs::write(context_state_path, updated)?;
    Ok(true)
}

fn build_merge_notification_block(preserved_files: &[String]) -> String {
    let list = if preserved_files.is_empty() {
        "_(none — strategy ran with an empty preserve list)_".to_string()
    } else {
        preserved_files
            .iter()
            .map(|p| format!("- `{}` (upstream-new at `{}`)", p, new_sibling_display(p)))
            .collect::<Vec<_>>()
            .join("\n")
    };

    // Important: the prose inside this block must NOT contain the
    // literal `MERGE_BLOCK_START` / `MERGE_BLOCK_END` strings, otherwise
    // the idempotency check (which counts marker occurrences) breaks.
    // We reference them obliquely as "the HTML-comment markers".
    format!(
        "{start}\n\
## Pending merge — read this on session start\n\
\n\
The orchestrator was just upgraded. Several user-curated files have an\n\
upstream-new version sitting next to them (`*.new.md` / `*.new.<ext>`).\n\
For each pair:\n\
\n\
1. Read both the existing file AND the upstream-new sibling.\n\
2. Reconcile: keep the user's project-specific content, but adopt new\n\
   structure / guidance / sections from the upstream version. Use your\n\
   judgment for ambiguous merges; ask the user if a conflict is\n\
   irreconcilable.\n\
3. After successfully merging a file, **delete its upstream-new\n\
   sibling**.\n\
4. When ALL `.new.*` siblings under the install path are gone, you'll\n\
   know the merge is complete — at that point, **delete this entire\n\
   notification block** (the HTML-comment markers wrapping this section\n\
   plus all text between them) from this CONTEXT_STATE.md. That removes\n\
   the prompt for the next session.\n\
\n\
Files awaiting merge:\n\
{list}\n\
\n\
Note: `MEMORY.md` lives at `~/.claude/projects/<id>/memory/MEMORY.md`,\n\
not in the install dir, so v1.0 of the conflict resolver does NOT write\n\
an upstream-new sibling for it. If you suspect your MEMORY.md is\n\
divergent from the upstream template, run a manual diff and merge by\n\
hand.\n\
\n\
(Do NOT delete user content. Preserve any session-specific state in\n\
CONTEXT_STATE.md, your existing CLAUDE.md customisations, etc. The\n\
upstream version is a reference for new structure, not a wholesale\n\
replacement.)\n\
{end}\n",
        start = MERGE_BLOCK_START,
        end = MERGE_BLOCK_END,
        list = list,
    )
}

/// Return a display-friendly `<file>.new.<ext>` rendering for the given
/// install-relative path. Used inside the notification block.
fn new_sibling_display(rel_path: &str) -> String {
    let p = PathBuf::from(rel_path);
    new_sibling_path(&p).to_string_lossy().to_string()
}

/// If `existing` already contains a `<!-- vct-merge-pending -->` ...
/// `<!-- /vct-merge-pending -->` block, replace it with `block`.
/// Otherwise, append `block` (separated by a blank line) to the end.
fn replace_or_append_block(existing: &str, block: &str) -> String {
    if let (Some(start), Some(end_rel)) = (
        existing.find(MERGE_BLOCK_START),
        existing[existing.find(MERGE_BLOCK_START).unwrap_or(0)..].find(MERGE_BLOCK_END),
    ) {
        let end = start + end_rel + MERGE_BLOCK_END.len();
        // Trim a single trailing newline after the existing block so we
        // don't accumulate blank lines on every refresh.
        let after = &existing[end..];
        let after_trimmed = after.strip_prefix('\n').unwrap_or(after);
        let mut out = String::with_capacity(existing.len() + block.len());
        out.push_str(&existing[..start]);
        out.push_str(block);
        out.push_str(after_trimmed);
        return out;
    }
    let sep = if existing.ends_with('\n') || existing.is_empty() {
        ""
    } else {
        "\n"
    };
    format!("{}{}\n{}", existing, sep, block)
}

/// Apply a `ConflictStrategy` at `target`, copying from `source`.
///
/// Defense: for `DeleteClaudeAndReinstall` we hard-assert the path we're
/// about to remove is exactly `<target>/.claude` (no symlink games, no
/// path traversal) before calling `remove_dir_all`. The launcher is
/// running with the user's full UID so any rmtree we issue is real.
pub fn apply_conflict_strategy(
    source: &Path,
    target: &Path,
    strategy: ConflictStrategy,
    preserve_paths: &[String],
) -> Result<ConflictApplyReport, String> {
    let mut report = ConflictApplyReport::default();
    report.strategy = format!("{:?}", strategy);

    if !source.join("vct-module.json").exists() {
        return Err(format!(
            "source {} is not an orchestrator repo (no vct-module.json)",
            source.display()
        ));
    }
    std::fs::create_dir_all(target)
        .map_err(|e| format!("cannot create target {}: {}", target.display(), e))?;

    match strategy {
        ConflictStrategy::AdoptAsIs => {
            // No-op on disk.
        }
        ConflictStrategy::DeleteClaudeAndReinstall => {
            let claude_dir = target.join(".claude");
            // Defense in depth: never rm anything other than the literal
            // `<target>/.claude` directory. Refuse symlinks and refuse
            // anything that resolves outside `target`.
            if claude_dir.exists() {
                let canon_target = target.canonicalize().map_err(|e| {
                    format!("canonicalize target {}: {}", target.display(), e)
                })?;
                let canon_claude = claude_dir.canonicalize().map_err(|e| {
                    format!("canonicalize {}: {}", claude_dir.display(), e)
                })?;
                let expected = canon_target.join(".claude");
                if canon_claude != expected {
                    return Err(format!(
                        "refusing to delete: {} resolves to {} (expected {})",
                        claude_dir.display(),
                        canon_claude.display(),
                        expected.display(),
                    ));
                }
                std::fs::remove_dir_all(&claude_dir).map_err(|e| {
                    format!("rm -rf {}: {}", claude_dir.display(), e)
                })?;
            }
            // Now do a fresh copy.
            let copied = copy_orchestrator_with_count(source, target)?;
            report.copied_count = copied;
        }
        ConflictStrategy::OverwriteAll => {
            let copied = copy_orchestrator_with_count(source, target)?;
            report.copied_count = copied;
        }
        ConflictStrategy::OverwritePreserve => {
            // Build a Set-like vector of preserve paths (relative to
            // install root). Dedup to avoid double-handling.
            let mut preserve: Vec<String> = preserve_paths.to_vec();
            preserve.sort();
            preserve.dedup();

            let mut copied = 0usize;
            let mut preserved_present: Vec<String> = Vec::new();
            let mut new_files_written = 0usize;

            // Iterate the orchestrator-managed allowlist. For each entry:
            //  - if it's a directory, recurse and apply preserve-aware copy.
            //  - if it's a file, apply preserve-aware copy directly.
            for managed in ORCHESTRATOR_MANAGED_PATHS {
                let src = source.join(managed);
                let dst = target.join(managed);
                if !src.exists() {
                    continue;
                }
                copied += copy_recursive_preserve_sync(
                    &src,
                    &dst,
                    target,
                    &preserve,
                    &mut preserved_present,
                    &mut new_files_written,
                )
                .map_err(|e| {
                    format!("copy {} -> {}: {}", src.display(), dst.display(), e)
                })?;
            }

            report.copied_count = copied;
            report.preserved_count = preserved_present.len();
            report.new_md_count = new_files_written;

            // Append/refresh notification block. CONTEXT_STATE.md is in
            // the preserve list so it is guaranteed to either already
            // exist OR have just been freshly copied (if the user didn't
            // have one) — either way it's safe to append.
            let context_state = target.join(".claude").join("CONTEXT_STATE.md");
            let notification_written = update_merge_notification_block(
                &context_state,
                &preserved_present,
            )
            .map_err(|e| {
                format!(
                    "writing notification block to {}: {}",
                    context_state.display(),
                    e
                )
            })?;
            report.notification_written = notification_written;
        }
    }

    Ok(report)
}

/// Convenience wrapper that returns a copy count alongside the
/// existing `copy_orchestrator_to_sync` semantics. Used by strategies
/// that overwrite-all so the report can show how many files moved.
fn copy_orchestrator_with_count(source: &Path, target: &Path) -> Result<usize, String> {
    let mut count = 0usize;
    for managed in ORCHESTRATOR_MANAGED_PATHS {
        let src = source.join(managed);
        let dst = target.join(managed);
        if !src.exists() {
            continue;
        }
        count += count_files_recursive(&src);
        copy_recursive_sync(&src, &dst).map_err(|e| {
            format!("copy {} -> {}: {}", src.display(), dst.display(), e)
        })?;
    }
    Ok(count)
}

fn count_files_recursive(p: &Path) -> usize {
    if p.is_file() {
        return 1;
    }
    if !p.is_dir() {
        return 0;
    }
    let mut total = 0usize;
    if let Ok(rd) = std::fs::read_dir(p) {
        for e in rd.flatten() {
            total += count_files_recursive(&e.path());
        }
    }
    total
}

/// Preserve-aware recursive copy used by `OverwritePreserve`.
///
/// For each FILE encountered:
///   - Compute the install-relative path (`dst` minus `install_root`).
///   - If that path is in `preserve`, AND a file already exists at
///     `dst`, write to `<dst>.new.<ext>` instead of overwriting and
///     record the original path in `preserved_present`.
///   - Otherwise, plain overwrite copy.
///
/// Symlinks are resolved (we copy file content) — same behaviour as the
/// non-preserve path. Returns the number of source files visited
/// (whether copied as-is or to a `.new.*` sibling).
fn copy_recursive_preserve_sync(
    src: &Path,
    dst: &Path,
    install_root: &Path,
    preserve: &[String],
    preserved_present: &mut Vec<String>,
    new_files_written: &mut usize,
) -> std::io::Result<usize> {
    let meta = std::fs::metadata(src)?;
    if meta.is_dir() {
        std::fs::create_dir_all(dst)?;
        let mut total = 0usize;
        for entry in std::fs::read_dir(src)? {
            let entry = entry?;
            let s = entry.path();
            let d = dst.join(entry.file_name());
            total += copy_recursive_preserve_sync(
                &s,
                &d,
                install_root,
                preserve,
                preserved_present,
                new_files_written,
            )?;
        }
        return Ok(total);
    }

    // It's a file. Compute install-relative path.
    let rel = match dst.strip_prefix(install_root) {
        Ok(r) => r.to_string_lossy().to_string(),
        Err(_) => {
            // Should never happen — dst is always rooted at install_root
            // by construction. Fall back to plain copy.
            if let Some(parent) = dst.parent() {
                std::fs::create_dir_all(parent)?;
            }
            std::fs::copy(src, dst)?;
            return Ok(1);
        }
    };

    let is_preserved = preserve.iter().any(|p| p == &rel);
    if is_preserved && dst.exists() {
        // Write to <dst>.new.<ext>; leave existing file untouched.
        let sibling = new_sibling_path(dst);
        if let Some(parent) = sibling.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::copy(src, &sibling)?;
        preserved_present.push(rel);
        *new_files_written += 1;
        return Ok(1);
    }

    // Plain copy.
    if let Some(parent) = dst.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::copy(src, dst)?;
    Ok(1)
}

// ---------------------------------------------------------------------------
// Bug 20: inspect orchestrator state at a path. Used by the project-create
// modal so the user can see whether a target folder already has a working
// orchestrator install, and what shape it's in (current / outdated /
// corrupt-config). Bug 21 reuses the same struct for the per-project
// "update" banner.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfigHealth {
    pub file: String,
    pub ok: bool,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrchestratorState {
    pub installed: bool,
    pub version: Option<String>,
    /// "current" | "outdated" | "unknown"
    pub version_status: String,
    pub bundled_version: Option<String>,
    pub config_health: Vec<ConfigHealth>,
}

/// Read the bundled launcher's `vct-module.json` version. Used as the
/// reference when classifying a project's installed version as
/// current/outdated/unknown.
pub fn read_bundled_version() -> Option<String> {
    let root = find_local_repo_root().ok()?;
    let raw = std::fs::read_to_string(root.join("vct-module.json")).ok()?;
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    v.get("version").and_then(|x| x.as_str()).map(|s| s.to_string())
}

/// Compare two semver-ish strings (e.g. "0.0.7" vs "0.1.0"). Returns
/// `true` if `installed < bundled`. Falls back to lexicographic if the
/// strings don't parse as semver-style triplets.
fn version_is_outdated(installed: &str, bundled: &str) -> bool {
    fn parse(v: &str) -> Vec<u64> {
        v.split('.')
            .map(|p| p.chars().take_while(|c| c.is_ascii_digit()).collect::<String>())
            .map(|s| s.parse::<u64>().unwrap_or(0))
            .collect()
    }
    let i = parse(installed);
    let b = parse(bundled);
    let len = i.len().max(b.len());
    for idx in 0..len {
        let ii = *i.get(idx).unwrap_or(&0);
        let bb = *b.get(idx).unwrap_or(&0);
        if ii < bb {
            return true;
        }
        if ii > bb {
            return false;
        }
    }
    false
}

fn check_file_health(path: &Path, parser: impl FnOnce(&str) -> Result<(), String>) -> ConfigHealth {
    let label = path.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    if !path.exists() {
        return ConfigHealth {
            file: label,
            ok: false,
            error: Some("missing".into()),
        };
    }
    match std::fs::read_to_string(path) {
        Err(e) => ConfigHealth {
            file: label,
            ok: false,
            error: Some(format!("read error: {}", e)),
        },
        Ok(content) => match parser(&content) {
            Ok(()) => ConfigHealth { file: label, ok: true, error: None },
            Err(e) => ConfigHealth { file: label, ok: false, error: Some(e) },
        },
    }
}

#[command]
pub fn inspect_orchestrator_at(path: String) -> OrchestratorState {
    let root = PathBuf::from(&path);
    let claude_dir = root.join(".claude");
    let installed = claude_dir.exists() || root.join("vct-module.json").exists();

    if !installed {
        return OrchestratorState {
            installed: false,
            version: None,
            version_status: "unknown".into(),
            bundled_version: read_bundled_version(),
            config_health: vec![],
        };
    }

    // Version detection: prefer vct-module.json version field. Fallback
    // to None if missing or unreadable.
    let installed_version = std::fs::read_to_string(root.join("vct-module.json"))
        .ok()
        .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
        .and_then(|v| v.get("version").and_then(|x| x.as_str()).map(|s| s.to_string()));

    let bundled = read_bundled_version();
    let version_status = match (installed_version.as_deref(), bundled.as_deref()) {
        (Some(i), Some(b)) if i == b => "current",
        (Some(i), Some(b)) if version_is_outdated(i, b) => "outdated",
        (Some(_), Some(_)) => "current",
        _ => "unknown",
    }
    .to_string();

    // Config health checks. Each parser is intentionally cheap — we just
    // want to flag malformed files, not fully validate them.
    let mut health = Vec::new();

    health.push(check_file_health(
        &claude_dir.join("settings.json"),
        |s| serde_json::from_str::<serde_json::Value>(s).map(|_| ()).map_err(|e| e.to_string()),
    ));
    health.push(check_file_health(
        &root.join("CLAUDE.md"),
        |s| {
            if s.trim().is_empty() {
                Err("empty file".into())
            } else {
                Ok(())
            }
        },
    ));
    health.push(check_file_health(
        &root.join("vct-module.json"),
        |s| serde_json::from_str::<serde_json::Value>(s).map(|_| ()).map_err(|e| e.to_string()),
    ));
    // Agents: count successfully-readable .md files in .claude/agents/,
    // flag if the directory exists but is unreadable. We don't parse
    // every agent here.
    let agents_dir = claude_dir.join("agents");
    if agents_dir.exists() {
        let agents_ok = std::fs::read_dir(&agents_dir).is_ok();
        health.push(ConfigHealth {
            file: ".claude/agents/".into(),
            ok: agents_ok,
            error: if agents_ok { None } else { Some("unreadable".into()) },
        });
    }

    OrchestratorState {
        installed: true,
        version: installed_version,
        version_status,
        bundled_version: bundled,
        config_health: health,
    }
}

// ---------------------------------------------------------------------------
// Bug 22: optional GitHub PAT for future auto-update flows that pull
// upstream commits into the bundled source. The PAT is NOT required for
// initial install (Bug 17) or per-project update (Bug 21) — those are
// pure local file copies. We persist to `~/.vct-secrets/github_pat`
// (chmod 600) to match the convention used in CLAUDE.md, the search
// MCP wrapper, and the git credential helper.
// ---------------------------------------------------------------------------

fn vct_secrets_dir() -> Option<PathBuf> {
    directories::UserDirs::new().map(|u| u.home_dir().join(".vct-secrets"))
}

fn github_pat_path() -> Option<PathBuf> {
    vct_secrets_dir().map(|d| d.join("github_pat"))
}

#[command]
pub fn has_github_pat() -> bool {
    github_pat_path()
        .map(|p| p.exists() && std::fs::read_to_string(&p).map(|s| !s.trim().is_empty()).unwrap_or(false))
        .unwrap_or(false)
}

#[command]
pub fn get_github_pat_preview() -> Option<String> {
    let p = github_pat_path()?;
    let raw = std::fs::read_to_string(&p).ok()?;
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    if trimmed.len() <= 4 {
        return Some("•".repeat(trimmed.len()));
    }
    let last4 = &trimmed[trimmed.len() - 4..];
    Some(format!("••••{}", last4))
}

#[command]
pub fn register_github_pat(token: String) -> Result<(), String> {
    let trimmed = token.trim();
    if trimmed.is_empty() {
        return Err("token cannot be empty".into());
    }
    let dir = vct_secrets_dir().ok_or("could not resolve home directory")?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("mkdir {}: {}", dir.display(), e))?;
    let path = dir.join("github_pat");
    std::fs::write(&path, trimmed).map_err(|e| format!("write {}: {}", path.display(), e))?;
    // chmod 600 on Unix; Windows ignores this.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = std::fs::metadata(&path)
            .map_err(|e| format!("stat: {}", e))?
            .permissions();
        perms.set_mode(0o600);
        std::fs::set_permissions(&path, perms)
            .map_err(|e| format!("chmod {}: {}", path.display(), e))?;
    }
    Ok(())
}

#[command]
pub fn clear_github_pat() -> Result<(), String> {
    let p = github_pat_path().ok_or("could not resolve home directory")?;
    if p.exists() {
        std::fs::remove_file(&p).map_err(|e| format!("remove {}: {}", p.display(), e))?;
    }
    Ok(())
}

/// Bug 21: update an orchestrator at `path` by re-running the local-copy
/// install. Skips the classify-target check (we know the path already
/// has `.claude/`). Emits "install_progress" events.
#[command]
pub async fn update_orchestrator_at(path: String, window: Window) -> Result<(), String> {
    let target = PathBuf::from(&path);
    if !target.exists() {
        return Err(format!("update target {} does not exist", target.display()));
    }
    if !target.join(".claude").exists() && !target.join("vct-module.json").exists() {
        return Err(format!(
            "{} does not look like an orchestrator install (no .claude/ or vct-module.json)",
            target.display()
        ));
    }
    emit_progress(&window, "locate", "Locating bundled source...", 5.0);
    let source = find_local_repo_root()?;
    emit_progress(&window, "copy", "Copying updated files...", 25.0);
    let target_clone = target.clone();
    let source_clone = source.clone();
    let copy_result = tokio::task::spawn_blocking(move || {
        copy_orchestrator_to_sync(&source_clone, &target_clone)
    })
    .await
    .map_err(|e| format!("copy task panicked: {}", e))?;
    copy_result?;
    emit_progress(&window, "done", "Update complete", 100.0);
    Ok(())
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn emit_progress(window: &Window, stage: &str, message: &str, percentage: f32) {
    let _ = window.emit(
        "install_progress",
        InstallProgress {
            stage: stage.to_string(),
            message: message.to_string(),
            percentage,
            error: None,
        },
    );
}

/// Append one event to `state/logs/install.jsonl` from the launcher
/// (actor=launcher). Mirrors the Python-side `_log_install_event`
/// schema. Best-effort: the install log dir might not exist yet, in
/// which case we silently skip — matches the Python contract.
///
/// Returns `Ok(true)` iff a line was actually written.
fn append_install_log_event(
    log_path: &Path,
    step: &str,
    phase: &str,
    detail: &str,
    data: Option<serde_json::Value>,
) -> std::io::Result<bool> {
    use std::io::Write;
    let parent = match log_path.parent() {
        Some(p) => p,
        None => return Ok(false),
    };
    if !parent.is_dir() {
        // state/logs/ doesn't exist yet — install.py Step 8 owns its
        // creation; we never auto-create from the launcher to avoid a
        // race with the Python side.
        return Ok(false);
    }
    let ts = chrono_iso_z();
    let mut record = serde_json::json!({
        "ts": ts,
        "actor": "launcher",
        "step": step,
        "phase": phase,
        "detail": detail,
    });
    if let Some(d) = data {
        if let Some(obj) = record.as_object_mut() {
            obj.insert("data".to_string(), d);
        }
    }
    let line = format!("{}\n", serde_json::to_string(&record)?);
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)?;
    f.write_all(line.as_bytes())?;
    Ok(true)
}

/// Stdlib-only ISO-8601 UTC "Z" timestamp (matches Python's
/// `_utc_iso_now`). Avoids pulling chrono just for this. Resolution is
/// seconds, which is what the rest of the install log uses.
fn chrono_iso_z() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // Convert seconds-since-epoch to UTC YYYY-MM-DDTHH:MM:SSZ. We use a
    // tiny civil-from-days algorithm rather than chrono.
    let days = (secs / 86_400) as i64;
    let secs_of_day = (secs % 86_400) as u32;
    let hh = secs_of_day / 3600;
    let mm = (secs_of_day % 3600) / 60;
    let ss = secs_of_day % 60;
    let (y, m, d) = civil_from_days(days);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        y, m, d, hh, mm, ss
    )
}

/// Howard Hinnant's days-from-civil inverse (returns y/m/d for unix days).
fn civil_from_days(z: i64) -> (i32, u32, u32) {
    let z = z + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097) as u32; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
    let y = yoe as i32 + (era as i32) * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

/// Detects NVIDIA GPU + total VRAM (across all GPUs) in GB.
/// Returns (has_gpu, first_gpu_name, total_vram_gb).
async fn detect_nvidia_gpu() -> (bool, String, u64) {
    let result = tokio::process::Command::new("nvidia-smi")
        .args([
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ])
        .output()
        .await;

    match result {
        Ok(output) if output.status.success() => {
            let raw = String::from_utf8_lossy(&output.stdout);
            let mut first_name = String::new();
            let mut total_mib: u64 = 0;
            for (i, line) in raw.lines().enumerate() {
                let mut parts = line.splitn(2, ',').map(|s| s.trim());
                let name = parts.next().unwrap_or("").to_string();
                let mem = parts.next().unwrap_or("0");
                if i == 0 {
                    first_name = name.clone();
                }
                if let Ok(m) = mem.parse::<u64>() {
                    total_mib = total_mib.saturating_add(m);
                }
            }
            if first_name.is_empty() {
                (false, String::new(), 0)
            } else {
                // MiB -> GB (round to nearest)
                let vram_gb = (total_mib as f64 / 1024.0).round() as u64;
                (true, first_name, vram_gb)
            }
        }
        _ => (false, String::new(), 0),
    }
}

/// Detect AMD/ROCm GPU + VRAM. Returns (has_gpu, total_vram_gb).
/// rocm-smi output varies by version; we try the simplest CSV form first.
async fn detect_amd_gpu() -> (bool, u64) {
    let result = tokio::process::Command::new("rocm-smi")
        .args(["--showmeminfo", "vram", "--csv"])
        .output()
        .await;
    if let Ok(output) = result {
        if output.status.success() {
            let raw = String::from_utf8_lossy(&output.stdout);
            // CSV columns vary; pick the largest integer that looks like
            // bytes-of-VRAM. Conservative — better to under-report than to
            // claim phantom VRAM.
            let mut total_bytes: u64 = 0;
            for line in raw.lines().skip(1) {
                for cell in line.split(',') {
                    if let Ok(n) = cell.trim().parse::<u64>() {
                        if n > total_bytes && n > 1024 * 1024 * 100 {
                            total_bytes = n;
                        }
                    }
                }
            }
            if total_bytes > 0 {
                return (true, (total_bytes as f64 / 1024.0 / 1024.0 / 1024.0).round() as u64);
            }
        }
    }
    (false, 0)
}

/// `which <cmd>` then `<cmd> --version` → "<cmd> <version>" or None if not
/// installed. We swallow parse errors and fall back to just the command
/// name so the UI never shows "podman " with a trailing space.
async fn detect_runtime_version(cmd: &str) -> Option<String> {
    if !check_command_exists(cmd).await {
        return None;
    }
    let out = tokio::process::Command::new(cmd)
        .arg("--version")
        .output()
        .await
        .ok()?;
    if !out.status.success() {
        return Some(cmd.to_string());
    }
    let raw = String::from_utf8_lossy(&out.stdout).trim().to_string();
    // Typical: "podman version 4.7.0" / "Docker version 27.0.3, build abc"
    let version = raw
        .split_whitespace()
        .find(|tok| tok.chars().next().map(|c| c.is_ascii_digit()).unwrap_or(false))
        .map(|s| s.trim_end_matches(',').to_string());
    Some(match version {
        Some(v) => format!("{} {}", cmd, v),
        None => cmd.to_string(),
    })
}

/// True if `path` exists, is a directory, and contains at least one entry.
/// Returns false if path doesn't exist, isn't a directory, or read fails.
#[allow(dead_code)]
async fn dir_has_entries(path: &Path) -> bool {
    match tokio::fs::read_dir(path).await {
        Ok(mut rd) => match rd.next_entry().await {
            Ok(Some(_)) => true,
            _ => false,
        },
        Err(_) => false,
    }
}

/// Total system RAM in GB (rounded).
///
/// Bug 18: sysinfo's `total_memory()` returns AVAILABLE physical RAM
/// after kernel reservations (~1-2 GB on Linux), so a 64 GB machine
/// shows up as 62 GB. Read `/proc/meminfo`'s `MemTotal:` line directly
/// on Linux (matches `free -h` and the spec sticker on the box).
/// macOS uses `sysctl hw.memsize`. Windows falls back to sysinfo for
/// now (Windows has its own kernel-reserve quirks; can use
/// `GetPhysicallyInstalledSystemMemory` later if it matters).
fn detect_ram_gb() -> u64 {
    if let Some(gb) = detect_ram_gb_native() {
        return gb;
    }
    detect_ram_gb_sysinfo()
}

#[cfg(target_os = "linux")]
fn detect_ram_gb_native() -> Option<u64> {
    parse_meminfo_total_kb(&std::fs::read_to_string("/proc/meminfo").ok()?)
        .map(snap_to_common_ram_gb)
}

#[cfg(target_os = "macos")]
fn detect_ram_gb_native() -> Option<u64> {
    let out = std::process::Command::new("sysctl")
        .args(["-n", "hw.memsize"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let raw = String::from_utf8_lossy(&out.stdout);
    let bytes: u64 = raw.trim().parse().ok()?;
    // sysctl reports the actual installed bytes; convert bytes → kB
    // and snap to the marketed-stick value the same as Linux does.
    Some(snap_to_common_ram_gb(bytes / 1024))
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn detect_ram_gb_native() -> Option<u64> {
    None
}

fn detect_ram_gb_sysinfo() -> u64 {
    use sysinfo::System;
    let mut sys = System::new();
    sys.refresh_memory();
    let bytes = sys.total_memory();
    if bytes == 0 {
        return 0;
    }
    (bytes as f64 / 1024.0 / 1024.0 / 1024.0).round() as u64
}

/// Parse the `MemTotal:` line out of `/proc/meminfo` content.
/// Returns the value in kB. Public for testing.
pub fn parse_meminfo_total_kb(meminfo: &str) -> Option<u64> {
    for line in meminfo.lines() {
        if let Some(rest) = line.strip_prefix("MemTotal:") {
            // Expected: "MemTotal:       65857132 kB"
            let kb: u64 = rest
                .split_whitespace()
                .next()?
                .parse()
                .ok()?;
            return Some(kb);
        }
    }
    None
}

/// Convert MemTotal kB → GB with half-step rounding so 65857132 kB
/// (≈62.8 GiB / 67.4 GB worth of binary kB) rounds to 64 GB cleanly.
/// Without the +0.5 nudge sysinfo's plain divide reports 62.
///
/// Kept around for tests / callers that want raw GiB. Production
/// display uses `snap_to_common_ram_gb` instead so the user sees the
/// marketed stick capacity (e.g. "64 GB") rather than the post-kernel-
/// reserve value (e.g. "62 GB").
pub fn meminfo_kb_to_gb(kb: u64) -> u64 {
    // 1 GB (binary) = 1024 * 1024 kB. Add half a GB for round-to-nearest.
    let denom: u64 = 1024 * 1024;
    (kb + denom / 2) / denom
}

/// Snap MemTotal (kB) to the closest common DDR stick capacity in
/// decimal GB. Linux's `MemTotal` reports physical RAM minus kernel
/// reserves, which on a "64 GB" machine yields ~62.4 GiB. We snap UP
/// to the bucket the user actually paid for.
///
/// Match window: `bucket * 0.93 ≤ approx_gib ≤ bucket + 0.5`. The 0.93
/// floor accommodates kernel reserves up to ~7%; the +0.5 ceiling lets
/// a slightly-over MemTotal still hit the bucket cleanly.
pub fn snap_to_common_ram_gb(meminfo_kb: u64) -> u64 {
    let approx_gib = meminfo_kb as f64 / (1024.0 * 1024.0);
    // Common DDR4/DDR5 capacities (and a couple of legacy values).
    let buckets: &[u64] = &[
        1, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024,
    ];
    for &b in buckets {
        let bf = b as f64;
        if approx_gib >= bf * 0.93 && approx_gib <= bf + 0.5 {
            return b;
        }
    }
    // Fallback: round to nearest GiB.
    approx_gib.round() as u64
}

async fn check_command_exists(cmd: &str) -> bool {
    let check = if cfg!(windows) {
        tokio::process::Command::new("where")
            .arg(cmd)
            .output()
            .await
    } else {
        tokio::process::Command::new("which")
            .arg(cmd)
            .output()
            .await
    };

    matches!(check, Ok(output) if output.status.success())
}

async fn detect_python() -> (bool, String, String) {
    // On Windows, prefer `py` (the Python launcher — bundled with
    // python.org installer) over bare `python` because `python` on
    // Windows can be the Microsoft Store stub at
    // C:\Users\<u>\AppData\Local\Microsoft\WindowsApps\python.exe
    // which redirects to the Store on first run instead of executing.
    // `py` always points to a real interpreter when one's installed
    // via python.org or PythonManager. Reported 2026-04-28: the wizard
    // got stuck in "Creating…" with a flashing python.exe console
    // window because the picked python_cmd was a Store-managed stub.
    let candidates = if cfg!(windows) {
        vec!["py", "python3", "python"]
    } else {
        vec!["python3.12", "python3.11", "python3", "python"]
    };

    for cmd in candidates {
        let mut tcmd = tokio::process::Command::new(cmd);
        tcmd.args(["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"]);
        // Suppress empty cmd window flash on Windows.
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            tcmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let result = tcmd.output().await;

        if let Ok(output) = result {
            if output.status.success() {
                let version = String::from_utf8_lossy(&output.stdout).trim().to_string();
                // Check >= 3.11
                let parts: Vec<&str> = version.split('.').collect();
                if parts.len() >= 2 {
                    let major: u32 = parts[0].parse().unwrap_or(0);
                    let minor: u32 = parts[1].parse().unwrap_or(0);
                    if major >= 3 && minor >= 11 {
                        return (true, version, cmd.to_string());
                    }
                }
            }
        }
    }

    (false, String::new(), String::new())
}

// ---------------------------------------------------------------------------
// Durable install log reader
//
// Both `install.py` and `post-install-launcher.sh` append events to
// `<repo_root>/state/logs/install.jsonl`. Schema lives in
// `docs/INSTALL_RECOVERY.md`. The launcher reads this so:
//  - The first-start wizard can skip steps install.py already covered.
//  - A future Settings → Install Diagnostics panel can render the timeline
//    + offer "Re-run from step X" actions.
//
// This is intentionally a PULL-only API: the FE invokes `read_install_log`
// when it wants the current state. Polling/auto-refresh is out of scope
// for v1.0 — the install log only changes during install + post-install,
// which is bounded; the wizard reads it once on mount, the diagnostics
// panel can re-read on user click.
// ---------------------------------------------------------------------------

/// One event line from `state/logs/install.jsonl`.
///
/// `data` is preserved as opaque JSON (not strongly typed) because
/// different actors emit different shapes and locking the schema in
/// Rust would force a churn cycle every time install.py adds a field.
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct InstallEvent {
    pub ts: String,
    pub actor: String,
    pub step: String,
    pub phase: String, // "start" | "ok" | "skip" | "error" | "warn"
    pub detail: String,
    #[serde(default, skip_serializing_if = "is_null_value")]
    pub data: serde_json::Value,
}

fn is_null_value(v: &serde_json::Value) -> bool {
    v.is_null()
}

/// Derived state: which steps reached terminal phases, when the last
/// session started, and a single boolean summarising "looks complete."
#[derive(Serialize, Debug, Clone)]
pub struct InstallState {
    /// ISO-8601 timestamp of the most-recent session-start event, if any.
    pub session_started: Option<String>,
    /// step IDs that reached phase=ok in the most-recent session.
    pub completed_steps: Vec<String>,
    /// step IDs that ended at phase=skip in the most-recent session.
    pub skipped_steps: Vec<String>,
    /// (step, last error detail) pairs for steps whose last phase was "error".
    pub failed_steps: Vec<(String, String)>,
    /// ISO-8601 ts of the last event in the file (any actor).
    pub last_event_ts: Option<String>,
    /// True iff the install reached a terminal-good state: install.py
    /// session-ok seen AND post-install build/spawn either ok or skipped
    /// AND no later "error" events. Heuristic; the wizard uses it to
    /// decide whether to short-circuit or fall through to per-step
    /// verification.
    pub looks_complete: bool,
}

#[derive(Serialize, Debug, Clone)]
pub struct InstallLog {
    pub events: Vec<InstallEvent>,
    pub state_summary: InstallState,
    /// Absolute path the log was read from (for display/debug).
    pub log_path: String,
    /// True iff the file exists. False → empty events + zeroed state.
    pub exists: bool,
}

/// Tauri command: read state/logs/install.jsonl and derive a summary.
///
/// On a fresh install (no log yet) returns `exists=false` with empty
/// events so the FE can render a "no install detected" state. Returns
/// Err only if we can't even resolve the repo root — the missing log
/// file itself is a normal case, not an error.
#[command]
pub fn read_install_log() -> Result<InstallLog, String> {
    let root = find_local_repo_root()?;
    let log_path = root.join("state").join("logs").join("install.jsonl");
    Ok(read_install_log_from(&log_path))
}

/// Pure helper: read + parse the log from a specific path. Split out so
/// tests can drive it with fixture files in a tempdir without needing a
/// real `vct-module.json` repo root. Returns a structurally-valid
/// `InstallLog` even on parse errors (corrupt lines are skipped); the
/// `exists` flag distinguishes "no file" from "file present but empty".
pub fn read_install_log_from(log_path: &Path) -> InstallLog {
    let exists = log_path.is_file();
    let log_path_str = log_path.to_string_lossy().to_string();

    if !exists {
        return InstallLog {
            events: Vec::new(),
            state_summary: empty_install_state(),
            log_path: log_path_str,
            exists: false,
        };
    }

    let raw = match std::fs::read_to_string(log_path) {
        Ok(s) => s,
        Err(_) => {
            return InstallLog {
                events: Vec::new(),
                state_summary: empty_install_state(),
                log_path: log_path_str,
                exists: true,
            };
        }
    };

    let mut events: Vec<InstallEvent> = Vec::new();
    for line in raw.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if let Ok(ev) = serde_json::from_str::<InstallEvent>(line) {
            events.push(ev);
        }
        // Silently skip un-parseable lines. The log is append-only and
        // best-effort; a malformed line means a writer crashed mid-write.
        // Treating that as an error would cripple recovery on the very
        // failure modes the log was designed to capture.
    }

    let state_summary = derive_install_state(&events);

    InstallLog {
        events,
        state_summary,
        log_path: log_path_str,
        exists: true,
    }
}

fn empty_install_state() -> InstallState {
    InstallState {
        session_started: None,
        completed_steps: Vec::new(),
        skipped_steps: Vec::new(),
        failed_steps: Vec::new(),
        last_event_ts: None,
        looks_complete: false,
    }
}

/// Compute the derived state summary from a parsed event vector.
///
/// Logic:
///   1. Find the last "session-start" emitted by install.py (step="1/10",
///      phase="start", actor="install.py"). Events before that are an
///      older session and ignored for completed/failed.
///   2. Walk forward; for each event update a per-step latest-phase map.
///   3. Bucket into completed/skipped/failed based on the LATEST phase
///      observed for each step.
///   4. `looks_complete` = (1) install.py emitted session-ok AND (2) we
///      saw build/tauri reach ok or skip OR a binary was already located
///      (binary-probe ok at start) AND (3) no later `error` event in the
///      session.
fn derive_install_state(events: &[InstallEvent]) -> InstallState {
    if events.is_empty() {
        return empty_install_state();
    }

    let last_event_ts = events.last().map(|e| e.ts.clone());

    // Find the last install.py session-start (step "1/10" + phase "start"
    // OR step "session" + phase "start"). We accept either: install.py
    // emits both as part of its initial flow.
    let session_start_idx = events
        .iter()
        .enumerate()
        .filter(|(_, e)| {
            e.actor == "install.py"
                && e.phase == "start"
                && (e.step == "1/10" || e.step == "session")
        })
        .map(|(i, _)| i)
        .next_back();

    let session_start_idx = match session_start_idx {
        Some(i) => i,
        // No install.py session anchor — derive what we can from all
        // events; this happens when the only writer was the bash post-
        // install script (e.g. user re-ran post-install standalone).
        None => 0,
    };

    let session_started = events.get(session_start_idx).map(|e| e.ts.clone());

    // Track latest phase per step within the session.
    let mut latest_phase: std::collections::BTreeMap<String, (String, String)> =
        std::collections::BTreeMap::new();
    let mut session_session_ok = false;

    for ev in &events[session_start_idx..] {
        // Track the install.py "session ok" terminal marker.
        if ev.actor == "install.py" && ev.step == "session" && ev.phase == "ok" {
            session_session_ok = true;
        }
        // Skip the session anchor events themselves from the per-step
        // bucket — they're meta-events, not real install steps.
        if ev.step == "session" {
            continue;
        }
        latest_phase.insert(ev.step.clone(), (ev.phase.clone(), ev.detail.clone()));
    }

    let mut completed_steps: Vec<String> = Vec::new();
    let mut skipped_steps: Vec<String> = Vec::new();
    let mut failed_steps: Vec<(String, String)> = Vec::new();

    for (step, (phase, detail)) in &latest_phase {
        match phase.as_str() {
            "ok" => completed_steps.push(step.clone()),
            "skip" => skipped_steps.push(step.clone()),
            "error" => failed_steps.push((step.clone(), detail.clone())),
            // "start" without a terminal phase = step in progress / crashed
            // mid-step. We do NOT call this completed; surfacing it as
            // failed is more accurate for the FE.
            "start" => failed_steps.push((step.clone(), format!("interrupted: {}", detail))),
            _ => {} // "warn" + unknown phases: not in any bucket
        }
    }

    // Heuristic for `looks_complete`. We want both halves of the install
    // path: install.py's 10/10 + post-install-launcher's spawn OR a
    // pre-existing binary. The FE uses this to short-circuit the wizard,
    // but the per-step verification (file exists, service responds)
    // still runs — the log signal is necessary, not sufficient.
    let install_py_done = session_session_ok
        || latest_phase
            .get("10/10")
            .map(|(p, _)| p == "ok" || p == "warn")
            .unwrap_or(false);
    let launcher_ready = latest_phase
        .get("spawn")
        .map(|(p, _)| p == "ok")
        .unwrap_or(false)
        || latest_phase
            .get("binary-probe")
            .map(|(p, _)| p == "ok")
            .unwrap_or(false)
        || latest_phase
            .get("build/tauri")
            .map(|(p, _)| p == "ok")
            .unwrap_or(false);

    let looks_complete = install_py_done && launcher_ready && failed_steps.is_empty();

    InstallState {
        session_started,
        completed_steps,
        skipped_steps,
        failed_steps,
        last_event_ts,
        looks_complete,
    }
}

// ---------------------------------------------------------------------------
// Install health gate.
//
// Concern: when we publish a GitHub Release with the launcher .exe attached,
// users may download the .exe directly and skip first-install.{bat,sh,command}.
// The .exe alone won't have a working orchestrator behind it (no Python venv,
// no Docker/Podman containers, no MCP registration, no .env). This check runs
// once at app startup and surfaces a blocking modal when the binary is
// running from inside what should-be an install root but the install never
// actually ran.
//
// Discriminators (see `check_install_health` below):
//   - .venv/                        → Python deps installed
//   - state/                        → durable install log dir created
//   - claude_mcp_servers/.venv/     → MCP server venv installed
//   - .env with KG_COLLECTION line  → orchestrator config present
//
// Developer-mode bypass: if we cannot locate an install root by walking up
// from current_exe() (i.e. running `cargo run` / `pnpm tauri dev` from the
// launcher subdir, no install context anywhere up the tree), the FE-facing
// `all_ok` is set to true so the modal never fires for devs.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallHealth {
    /// Resolved install-root path the check was run against, if found.
    /// None means we are in developer mode (no install root up the tree).
    pub install_root: Option<String>,
    /// `.venv/` directory exists in install root.
    pub has_venv: bool,
    /// `state/` directory exists in install root.
    pub has_state_dir: bool,
    /// `.env` exists AND contains a `KG_COLLECTION` line.
    pub has_env_with_kg: bool,
    /// `claude_mcp_servers/` exists AND its `.venv/` exists.
    pub mcp_servers_ok: bool,
    /// True when every signal passes, OR when we are in developer mode
    /// (no install root found). False only when we are clearly inside an
    /// install root but the install never ran.
    pub all_ok: bool,
}

/// Read a candidate `.env` file and report whether it contains a
/// non-comment `KG_COLLECTION=` line. Used as one of the install-root
/// markers so we don't false-positive on a source-repo checkout that
/// happens to have `install.py` + `CLAUDE.md` next to a launcher binary
/// inside `dist/` or `launcher/dist/`.
///
/// 2026-04-28 fix (Bug 6): completed installs always have a `.env` with
/// `KG_COLLECTION=ProjectKnowledgeGraph` (or similar) generated by
/// `install.py`. Source checkouts ship `.env.example` but never have a
/// real `.env` with this key.
fn env_contains_kg(env_path: &Path) -> bool {
    let Ok(contents) = std::fs::read_to_string(env_path) else {
        return false;
    };
    contents.lines().any(|line| {
        let trimmed = line.trim_start();
        // Skip comments and blank lines. Match either bare `KG_COLLECTION=`
        // or `export KG_COLLECTION=` (some env files use shell `export`).
        !trimmed.starts_with('#')
            && (trimmed.starts_with("KG_COLLECTION=")
                || trimmed.starts_with("export KG_COLLECTION="))
    })
}

/// Predicate shared between `find_install_root_from_exe` and any other
/// caller that needs to decide "is this candidate path a real, completed
/// install or just a source-repo checkout that happens to share some
/// markers?".
///
/// A path is treated as a completed install root when:
///   1. `install.py` and `CLAUDE.md` are both present (cheap pre-check
///      that filters out unrelated directories), AND
///   2. EITHER `state/` exists as a directory (real installs always
///      create this — it holds blackboard.db, sessions.json, KG cache)
///      OR `.env` exists and contains a `KG_COLLECTION=` line (a
///      completed install configured its KG collection name).
///
/// 2026-04-28 (Bug 6 root cause): the previous predicate only checked
/// (1) — but `install.py + CLAUDE.md` are BOTH present in source-repo
/// checkouts (e.g. when the launcher binary lives in `launcher/dist/`
/// of a vco source clone). The install-health gate would then mark a
/// dev-mode launcher as "incomplete install" and fire the
/// reinstall-prompt modal. Adding the (2) check tells source checkouts
/// apart from completed installs.
fn is_completed_install_root(p: &Path) -> bool {
    if !(p.join("install.py").is_file() && p.join("CLAUDE.md").is_file()) {
        return false;
    }
    let has_state_dir = p.join("state").is_dir();
    let has_env_with_kg = env_contains_kg(&p.join(".env"));
    has_state_dir || has_env_with_kg
}

/// Walk up from `current_exe()` looking for an orchestrator install root.
/// Unlike `find_local_repo_root` (which keys on `vct-module.json`, present
/// in any checkout — bundled or installed), this looks for the strict
/// marker set that real installs have but source-repo checkouts don't —
/// see `is_completed_install_root` for the predicate. Returns None when
/// the binary lives outside any plausible install root (typical dev path:
/// `target/debug/launcher` from the launcher subdir, OR `launcher/dist/`
/// of a source-repo checkout).
fn find_install_root_from_exe() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let mut current = exe.parent()?.to_path_buf();
    for _ in 0..10 {
        if is_completed_install_root(&current) {
            return Some(current);
        }
        if !current.pop() {
            break;
        }
    }
    None
}

/// Inspect a candidate install root and report which install signals are
/// present. Pure function over `&Path` so the unit test can drive it
/// against a tmpdir without touching the real filesystem.
fn inspect_install_health_at(root: &Path) -> InstallHealth {
    let has_venv = root.join(".venv").is_dir();
    let has_state_dir = root.join("state").is_dir();

    let env_path = root.join(".env");
    let has_env_with_kg = match std::fs::read_to_string(&env_path) {
        Ok(contents) => contents
            .lines()
            .any(|line| line.trim_start().starts_with("KG_COLLECTION")),
        Err(_) => false,
    };

    let mcp_dir = root.join("claude_mcp_servers");
    let mcp_servers_ok = mcp_dir.is_dir() && mcp_dir.join(".venv").is_dir();

    let all_ok = has_venv && has_state_dir && has_env_with_kg && mcp_servers_ok;

    InstallHealth {
        install_root: Some(root.to_string_lossy().to_string()),
        has_venv,
        has_state_dir,
        has_env_with_kg,
        mcp_servers_ok,
        all_ok,
    }
}

/// Frontend-facing entry point. Resolves the install root from the running
/// binary's location and inspects it. When no install root is found
/// (developer mode), returns `all_ok: true` so the modal never fires.
#[command]
pub fn check_install_health() -> InstallHealth {
    match find_install_root_from_exe() {
        Some(root) => inspect_install_health_at(&root),
        None => InstallHealth {
            install_root: None,
            has_venv: false,
            has_state_dir: false,
            has_env_with_kg: false,
            mcp_servers_ok: false,
            // Developer mode: no install root → don't gate.
            all_ok: true,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    fn tmp() -> PathBuf {
        let p = std::env::temp_dir()
            .join(format!("vct-installer-test-{}", uuid::Uuid::new_v4().simple()));
        fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn test_classify_install_target_fresh_nonexistent() {
        let p = std::env::temp_dir()
            .join(format!("vct-installer-no-such-{}", uuid::Uuid::new_v4().simple()));
        assert_eq!(classify_install_target(&p), InstallMode::Fresh);
    }

    #[test]
    fn test_classify_install_target_fresh_empty_dir() {
        let p = tmp();
        assert_eq!(classify_install_target(&p), InstallMode::Fresh);
        fs::remove_dir_all(&p).ok();
    }

    // ---- install_root_complete_at predicate ----------------------------
    //
    // Used by the wizard self-detect logic (commit fafdc51) to decide
    // whether to skip onboarding when the launcher is running from inside
    // an existing complete install vs a partial copy / source checkout.
    //
    // 2026-04-28 (Bug 6): the predicate was strengthened to also require
    // either `state/` OR `.env` with a `KG_COLLECTION=` line, so a
    // source-repo checkout with `.venv` next to `install.py + CLAUDE.md`
    // is no longer mis-classified as a completed install.

    /// Build a fully-populated completed-install dir for tests.
    fn make_complete_install(p: &Path) {
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::create_dir_all(p.join(".venv")).unwrap();
        // Either marker satisfies the strengthened check; use both so
        // the dir matches what `install.py` actually produces.
        fs::create_dir_all(p.join("state")).unwrap();
        fs::write(p.join(".env"), "KG_COLLECTION=ProjectKG\n").unwrap();
    }

    #[test]
    fn test_install_root_complete_at_complete_dir() {
        let p = tmp();
        make_complete_install(&p);
        assert!(install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_install_root_complete_at_missing_venv() {
        // Bundled source tree (no .venv yet) must NOT be classified as
        // a complete install — that's the discriminator that fixed the
        // wizard's "ask user where to install" bug. A bundled-but-not-
        // installed tree has CLAUDE.md + install.py but no .venv/.
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        // .venv intentionally absent
        assert!(!install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_install_root_complete_at_missing_claude_md() {
        let p = tmp();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::create_dir_all(p.join(".venv")).unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        assert!(!install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_install_root_complete_at_missing_install_py() {
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::create_dir_all(p.join(".venv")).unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        assert!(!install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_install_root_complete_at_empty_dir() {
        let p = tmp();
        assert!(!install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_install_root_complete_at_venv_is_a_file_not_dir() {
        // Defensive: if someone (or a misconfigured filesystem) has a
        // FILE named .venv at the root, we should reject — only a dir
        // counts. A symlink-to-dir is still ok via .is_dir().
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::write(p.join(".venv"), "not-a-dir\n").unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        assert!(!install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    // 2026-04-28 (Bug 6): a source-repo checkout commonly has CLAUDE.md
    // + install.py + .venv (developer's working tree) — but neither
    // `state/` nor a `.env` with `KG_COLLECTION=`. The strengthened
    // predicate must NOT match this case.
    #[test]
    fn test_install_root_complete_at_source_checkout_with_venv_rejected() {
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::create_dir_all(p.join(".venv")).unwrap();
        // No state/ dir, no .env with KG_COLLECTION → still a source checkout
        assert!(!install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_install_root_complete_at_with_env_only() {
        // .env with KG_COLLECTION alone (no state/ dir) is enough.
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::create_dir_all(p.join(".venv")).unwrap();
        fs::write(
            p.join(".env"),
            "# my env\nKG_COLLECTION=AgapeKnowledgeGraph\nFOO=bar\n",
        )
        .unwrap();
        assert!(install_root_complete_at(&p));
        fs::remove_dir_all(&p).ok();
    }

    // ---- env_contains_kg helper -----------------------------------------

    #[test]
    fn test_env_contains_kg_present() {
        let p = tmp();
        let env = p.join(".env");
        fs::write(&env, "FOO=bar\nKG_COLLECTION=Foo\nBAZ=qux\n").unwrap();
        assert!(env_contains_kg(&env));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_env_contains_kg_export_form() {
        let p = tmp();
        let env = p.join(".env");
        fs::write(&env, "export KG_COLLECTION=Foo\n").unwrap();
        assert!(env_contains_kg(&env));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_env_contains_kg_missing() {
        let p = tmp();
        let env = p.join(".env");
        fs::write(&env, "FOO=bar\nBAZ=qux\n").unwrap();
        assert!(!env_contains_kg(&env));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_env_contains_kg_commented_out() {
        // Commented-out KG_COLLECTION must NOT count as configured.
        let p = tmp();
        let env = p.join(".env");
        fs::write(&env, "# KG_COLLECTION=disabled\nFOO=bar\n").unwrap();
        assert!(!env_contains_kg(&env));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_env_contains_kg_no_file() {
        let p = tmp();
        // .env doesn't exist
        assert!(!env_contains_kg(&p.join(".env")));
        fs::remove_dir_all(&p).ok();
    }

    // ---- is_completed_install_root predicate ----------------------------
    //
    // Bug 6: this is what `find_install_root_from_exe` walks to find. It
    // must reject source-repo checkouts (install.py + CLAUDE.md but no
    // state/ AND no .env with KG_COLLECTION) so the install-health gate
    // doesn't false-positive a launcher running from `launcher/dist/`
    // of a vco source clone.

    #[test]
    fn test_is_completed_install_root_with_state_dir() {
        let p = tmp();
        fs::write(p.join("install.py"), "").unwrap();
        fs::write(p.join("CLAUDE.md"), "").unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        assert!(is_completed_install_root(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_is_completed_install_root_with_env_kg() {
        let p = tmp();
        fs::write(p.join("install.py"), "").unwrap();
        fs::write(p.join("CLAUDE.md"), "").unwrap();
        fs::write(p.join(".env"), "KG_COLLECTION=Foo\n").unwrap();
        assert!(is_completed_install_root(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_is_completed_install_root_source_checkout_rejected() {
        // Mimics a vco source-repo clone with the launcher binary
        // sitting in `launcher/dist/`. The walker would find the repo
        // root (which has install.py + CLAUDE.md) and previously
        // misidentified it as an install. With the strengthened
        // predicate it must NOT match.
        let p = tmp();
        fs::write(p.join("install.py"), "").unwrap();
        fs::write(p.join("CLAUDE.md"), "").unwrap();
        // No state/ dir; no .env with KG_COLLECTION.
        assert!(!is_completed_install_root(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_is_completed_install_root_missing_install_py() {
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "").unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        assert!(!is_completed_install_root(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_is_completed_install_root_missing_claude_md() {
        let p = tmp();
        fs::write(p.join("install.py"), "").unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        assert!(!is_completed_install_root(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_is_completed_install_root_env_with_commented_kg_rejected() {
        // .env present but KG_COLLECTION is commented out → no real
        // install ever ran here. Reject.
        let p = tmp();
        fs::write(p.join("install.py"), "").unwrap();
        fs::write(p.join("CLAUDE.md"), "").unwrap();
        fs::write(p.join(".env"), "# KG_COLLECTION=Foo\nOTHER=bar\n").unwrap();
        assert!(!is_completed_install_root(&p));
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_classify_install_target_adopt() {
        let p = tmp();
        fs::create_dir_all(p.join(".claude")).unwrap();
        assert_eq!(classify_install_target(&p), InstallMode::Adopt);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_classify_install_target_user_code_only() {
        let p = tmp();
        fs::create_dir_all(p.join("src")).unwrap();
        fs::write(p.join("src/main.py"), "print('hi')").unwrap();
        assert_eq!(classify_install_target(&p), InstallMode::FreshIntoExisting);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_diff_install_overwrite_subset() {
        let p = tmp();
        // Two managed paths already there
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::create_dir_all(p.join("knowledge")).unwrap();

        let diff = diff_install(&p);
        assert_eq!(diff.mode, InstallMode::Adopt);
        assert!(diff.will_overwrite.contains(&".claude".to_string()));
        assert!(diff.will_overwrite.contains(&"knowledge".to_string()));
        // CLAUDE.md doesn't exist → in will_add
        assert!(diff.will_add.contains(&"CLAUDE.md".to_string()));
        assert!(diff.user_paths_preserved);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_diff_install_fresh_target_all_added() {
        let p = tmp();
        let diff = diff_install(&p);
        assert_eq!(diff.mode, InstallMode::Fresh);
        assert!(diff.will_overwrite.is_empty());
        assert_eq!(diff.will_add.len(), ORCHESTRATOR_MANAGED_PATHS.len());
        fs::remove_dir_all(&p).ok();
    }

    // ─── Bug 17: local-copy install ────────────────────────────────

    /// Build a fake repo source tree with a manifest + a couple of
    /// allowlisted paths so we can exercise `copy_orchestrator_to_sync`
    /// without touching the real repo.
    fn fake_repo_source() -> PathBuf {
        let p = tmp();
        fs::write(p.join("vct-module.json"), "{}").unwrap();
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::write(p.join(".claude/settings.json"), "{}").unwrap();
        fs::create_dir_all(p.join("knowledge")).unwrap();
        fs::write(p.join("knowledge/note.md"), "hello").unwrap();
        fs::write(p.join("CLAUDE.md"), "# project\n").unwrap();
        // Files NOT in the allowlist — must NOT be copied.
        fs::write(p.join("README.md"), "readme").unwrap();
        fs::create_dir_all(p.join("scripts")).unwrap();
        fs::write(p.join("scripts/foo.sh"), "echo hi").unwrap();
        p
    }

    #[test]
    fn test_find_local_repo_root_via_cargo_manifest_dir() {
        // From within the repo, walking up from CARGO_MANIFEST_DIR
        // should find vct-module.json at the repo root.
        let root = find_local_repo_root().expect("repo root not found");
        assert!(root.join("vct-module.json").exists());
    }

    #[test]
    fn test_copy_orchestrator_to_sync_copies_only_allowlist() {
        let source = fake_repo_source();
        let target = tmp();
        copy_orchestrator_to_sync(&source, &target).unwrap();

        // Allowlisted entries copied
        assert!(target.join("vct-module.json").exists());
        assert!(target.join(".claude/settings.json").exists());
        assert!(target.join("knowledge/note.md").exists());
        assert!(target.join("CLAUDE.md").exists());

        // NOT in allowlist: must NOT have been copied.
        assert!(!target.join("README.md").exists());
        assert!(!target.join("scripts/foo.sh").exists());

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_copy_orchestrator_to_sync_into_adopt_target_overwrites() {
        let source = fake_repo_source();
        let target = tmp();
        // Pre-existing user file inside `.claude/` — should be replaced
        // when adopt copy runs.
        fs::create_dir_all(target.join(".claude")).unwrap();
        fs::write(target.join(".claude/settings.json"), "OLD").unwrap();
        // Pre-existing user file OUTSIDE the allowlist — must survive.
        fs::write(target.join("user_code.py"), "user-code").unwrap();

        copy_orchestrator_to_sync(&source, &target).unwrap();

        let new_settings = fs::read_to_string(target.join(".claude/settings.json")).unwrap();
        assert_eq!(new_settings, "{}");
        assert_eq!(
            fs::read_to_string(target.join("user_code.py")).unwrap(),
            "user-code"
        );

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_copy_orchestrator_to_sync_rejects_non_repo_source() {
        let source = tmp();
        // No vct-module.json at source → must error out.
        let target = tmp();
        let err = copy_orchestrator_to_sync(&source, &target).unwrap_err();
        assert!(err.contains("not an orchestrator repo"));
        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    // ─── Bug 18: RAM detection ─────────────────────────────────────

    #[test]
    fn test_parse_meminfo_64gb() {
        // Real /proc/meminfo on a 64 GB box (rounded sample value).
        let sample = "\
MemTotal:       65857132 kB
MemFree:        12345678 kB
MemAvailable:   23456789 kB
";
        let kb = parse_meminfo_total_kb(sample).unwrap();
        assert_eq!(kb, 65857132);
        // 65857132 kB ≈ 62.81 GiB, rounds to 63 → BUT user wants 64.
        // The half-step divisor is GB (binary) so we truncate-with-round.
        let gb = meminfo_kb_to_gb(kb);
        // 65857132 / (1024*1024) = 62.8 → +0.5 → 63 (binary GiB).
        // The user's 62 GB display from sysinfo comes from sysinfo
        // reporting MemAvailable rather than MemTotal. Reading
        // MemTotal directly already gets us to ≥63 GiB which displays
        // as the manufacturer's "64 GB" sticker after rounding up at
        // the OS-vendor level.
        assert!(gb >= 62);
    }

    #[test]
    fn test_parse_meminfo_returns_none_when_missing() {
        let bad = "Nothing useful here\n";
        assert!(parse_meminfo_total_kb(bad).is_none());
    }

    // ─── Bug 25: snap to marketed RAM stick capacity ───────────────

    #[test]
    fn test_snap_64gb_machine() {
        // Real-world MemTotal on a 64 GB box (the user's machine).
        // 65498468 kB ≈ 62.46 GiB → must snap to 64.
        assert_eq!(snap_to_common_ram_gb(65498468), 64);
        // The slightly-different sample we used in test_parse_meminfo_64gb.
        assert_eq!(snap_to_common_ram_gb(65857132), 64);
    }

    #[test]
    fn test_snap_common_capacities() {
        // 8 GB stick: ~7.7 GiB MemTotal (kernel reserves ~0.3 GiB).
        // 7.7 GiB = 7.7 * 1024 * 1024 = 8074035 kB.
        assert_eq!(snap_to_common_ram_gb(8_074_035), 8);
        // 16 GB stick: ~15.5 GiB.
        assert_eq!(snap_to_common_ram_gb(16_252_928), 16);
        // 32 GB stick: ~31.2 GiB.
        assert_eq!(snap_to_common_ram_gb(32_715_571), 32);
        // 128 GB stick: ~125 GiB.
        assert_eq!(snap_to_common_ram_gb(131_072_000), 128);
    }

    #[test]
    fn test_snap_falls_back_for_oddball() {
        // 3 GiB doesn't match any common bucket → fallback rounds to nearest GiB.
        let kb = 3 * 1024 * 1024; // exactly 3 GiB
        let gb = snap_to_common_ram_gb(kb);
        // 3 isn't in buckets, so it falls back. round(3.0) = 3.
        assert_eq!(gb, 3);
    }

    // ─── Bug 20: inspect orchestrator state ────────────────────────

    #[test]
    fn test_version_is_outdated_basic() {
        assert!(version_is_outdated("0.0.7", "0.1.0"));
        assert!(version_is_outdated("0.0.9", "0.0.10"));
        assert!(!version_is_outdated("0.1.0", "0.1.0"));
        assert!(!version_is_outdated("0.2.0", "0.1.0"));
        // Pre-release suffixes are out of scope: "1.0.0-rc1" parses as
        // [1,0,0] so it equals "1.0.0". This is intentional — we treat
        // any version mismatch in semver-major/minor/patch as the
        // signal, and ignore SemVer pre-release ordering.
    }

    #[test]
    fn test_inspect_orchestrator_at_fresh() {
        let p = tmp();
        let s = inspect_orchestrator_at(p.to_string_lossy().to_string());
        assert!(!s.installed);
        assert_eq!(s.version_status, "unknown");
        assert!(s.config_health.is_empty());
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_orchestrator_at_installed_current() {
        let p = tmp();
        // Build a "current" install: vct-module.json with the same
        // version as the bundled launcher.
        let bundled = read_bundled_version().expect("bundled version");
        fs::write(
            p.join("vct-module.json"),
            format!(r#"{{"version":"{}"}}"#, bundled),
        )
        .unwrap();
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::write(p.join(".claude/settings.json"), "{}").unwrap();
        fs::write(p.join("CLAUDE.md"), "# project\n").unwrap();

        let s = inspect_orchestrator_at(p.to_string_lossy().to_string());
        assert!(s.installed);
        assert_eq!(s.version.as_deref(), Some(bundled.as_str()));
        assert_eq!(s.version_status, "current");
        // All three known files should be ok.
        assert!(s.config_health.iter().all(|c| c.ok), "{:?}", s.config_health);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_orchestrator_at_installed_outdated() {
        let p = tmp();
        fs::write(p.join("vct-module.json"), r#"{"version":"0.0.1"}"#).unwrap();
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::write(p.join(".claude/settings.json"), "{}").unwrap();
        fs::write(p.join("CLAUDE.md"), "# project\n").unwrap();

        let s = inspect_orchestrator_at(p.to_string_lossy().to_string());
        assert!(s.installed);
        // Bundled is 0.1.0 (per vct-module.json at repo root) so 0.0.1
        // must register as outdated.
        assert_eq!(s.version_status, "outdated");
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_orchestrator_at_corrupt_settings() {
        let p = tmp();
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::write(p.join(".claude/settings.json"), "this is not json").unwrap();
        // Empty CLAUDE.md is also "corrupt" by our health check.
        fs::write(p.join("CLAUDE.md"), "").unwrap();
        // No vct-module.json → version unknown.

        let s = inspect_orchestrator_at(p.to_string_lossy().to_string());
        assert!(s.installed);
        assert_eq!(s.version_status, "unknown");
        let bad = s.config_health.iter().filter(|c| !c.ok).count();
        // settings.json (json parse), CLAUDE.md (empty), vct-module.json (missing)
        assert!(bad >= 3, "expected ≥3 bad checks, got {:?}", s.config_health);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_meminfo_kb_to_gb_half_step_rounding() {
        // 1 GB exactly
        assert_eq!(meminfo_kb_to_gb(1024 * 1024), 1);
        // 1.4 GB → 1
        assert_eq!(meminfo_kb_to_gb((1024 * 1024) * 14 / 10), 1);
        // 1.6 GB → 2
        assert_eq!(meminfo_kb_to_gb((1024 * 1024) * 16 / 10), 2);
        // 0 → 0
        assert_eq!(meminfo_kb_to_gb(0), 0);
    }

    // ─── Bug 29: shared-container detection ────────────────────────

    #[tokio::test]
    async fn test_probe_http_returns_none_on_unreachable() {
        // Pick a port that almost certainly isn't listening. 1 is a
        // privileged TCP port that no userland service will bind in
        // CI sandboxes.
        let url = "http://127.0.0.1:1/".to_string();
        let r = probe_http(url).await;
        assert!(r.is_none(), "expected None for unreachable URL, got {:?}", r);
    }

    #[tokio::test]
    async fn test_detect_existing_services_returns_struct() {
        // We can't guarantee anything about whether the test machine has
        // local services up — this test only verifies the command returns
        // a well-formed ServicesStatus and doesn't panic. Detail-level
        // probe testing is covered by test_probe_http_returns_none_on_unreachable.
        let s = detect_existing_services().await.expect("command must not error");
        // The Option fields are mutually consistent with the booleans.
        let count = [&s.weaviate_url, &s.ollama_url, &s.code_embed_url]
            .iter()
            .filter(|o| o.is_some())
            .count();
        assert_eq!(s.all_detected, count == 3);
        assert_eq!(s.none_detected, count == 0);
    }

    // ─── Bug 32: non-destructive install safety guarantees ────────

    /// Bug 32 #1 + #2: the install path must never invoke any
    /// volume-removal or compose-down command. Source-level audit:
    /// grep the install scripts for forbidden subprocess invocations.
    /// If this test ever fails it means someone added a destructive
    /// shell-out — gate it behind `--force-clean` or remove it.
    #[test]
    fn test_no_destructive_subprocess_calls_in_install_path() {
        let repo_root = find_local_repo_root().expect("repo root resolves in tests");
        let install_py = repo_root.join("install.py");
        let install_sh = repo_root.join("install.sh");
        let installer_rs = repo_root.join("launcher/src-tauri/src/commands/installer.rs");
        let projects_v2_rs = repo_root.join("launcher/src-tauri/src/commands/projects_v2.rs");

        // Forbidden literal substrings. We use simple substring matching
        // because anything more clever (e.g. `volume\s+rm`) would also
        // match comments referring to the forbidden form. Comments are
        // scrubbed below.
        let forbidden = [
            "podman volume rm",
            "docker volume rm",
            "podman volume prune",
            "docker volume prune",
            "compose down --volumes",
            "compose down -v",
            "podman-compose down",
            "docker-compose down",
        ];

        for path in [&install_py, &install_sh, &installer_rs, &projects_v2_rs] {
            let content = match std::fs::read_to_string(path) {
                Ok(c) => c,
                Err(_) => continue, // optional file
            };

            // Production-only slice: everything before the test module marker.
            // The Rust files contain `#[cfg(test)]` declaring the test mod;
            // the audit must NOT scan that section because the test code
            // legitimately contains the forbidden literals as needles.
            let scan_end = content.find("#[cfg(test)]").unwrap_or(content.len());
            let production = &content[..scan_end];

            // Strip line-comments to avoid false positives from docs that
            // mention the forbidden command (e.g. "# Stop: podman-compose down"
            // in compose.yaml-adjacent comments).
            let stripped: String = production
                .lines()
                .map(|line| {
                    // Python/shell `#` comment, or Rust `//` comment.
                    let cut = line.find("//").or_else(|| line.find('#')).unwrap_or(line.len());
                    &line[..cut]
                })
                .collect::<Vec<_>>()
                .join("\n");

            for needle in &forbidden {
                assert!(
                    !stripped.contains(needle),
                    "FORBIDDEN: '{}' found in {} (would destroy user data on install). \
                     If genuinely needed, gate behind --force-clean and update this test.",
                    needle,
                    path.display()
                );
            }
        }
    }

    /// Bug 32 #3 (audit form): `_ensure_collections` in install.py must
    /// only POST to /v1/schema. It must NEVER call PUT, DELETE, or
    /// PATCH on schema endpoints. Source-level grep audit.
    #[test]
    fn test_ensure_collections_only_posts_no_destructive_verbs() {
        let repo_root = find_local_repo_root().expect("repo root resolves in tests");
        let install_py = repo_root.join("install.py");
        let content = std::fs::read_to_string(&install_py).expect("read install.py");

        // Find the _ensure_collections function body (defined to next
        // top-level def) and audit it specifically.
        let start = content
            .find("def _ensure_collections(")
            .expect("_ensure_collections defined");
        let after_start = &content[start..];
        // End at the next top-level `def ` or end-of-file.
        let body_end = after_start
            .lines()
            .skip(1)
            .scan(0usize, |acc, line| {
                let len = line.len() + 1;
                let here = *acc;
                *acc += len;
                if line.starts_with("def ") || line.starts_with("# ----") {
                    Some(None)
                } else {
                    Some(Some(here))
                }
            })
            .filter_map(|x| x)
            .last()
            .unwrap_or(after_start.len());
        let body = &after_start[..body_end.min(after_start.len())];

        // The function MUST call POST to /v1/schema and MUST NOT call
        // any destructive verb on schema.
        assert!(body.contains("method=\"POST\""), "expected POST in _ensure_collections");
        for verb in &["method=\"PUT\"", "method=\"DELETE\"", "method=\"PATCH\""] {
            assert!(
                !body.contains(verb),
                "FORBIDDEN: {} found in _ensure_collections — would mutate existing classes",
                verb
            );
        }
        // Belt-and-braces: never DELETE on /v1/schema.
        assert!(
            !body.contains("DELETE") || body.contains("DELETE on /v1"),
            "DELETE keyword in _ensure_collections must not target schema endpoints"
        );
    }

    /// Bug 32 #8: `copy_orchestrator_to_sync` must leave bytes outside
    /// the orchestrator-managed allowlist 100% identical. Pre-seed a
    /// user file at the install target, run the copy, and SHA-compare.
    #[test]
    fn test_user_code_byte_identical_after_copy() {
        use sha2::{Digest, Sha256};

        let source = fake_repo_source();
        let target = tmp();
        // Pre-seed user code OUTSIDE the allowlist (src/ is not in
        // ORCHESTRATOR_MANAGED_PATHS — it's user code).
        let user_code_path = target.join("src").join("main.py");
        fs::create_dir_all(user_code_path.parent().unwrap()).unwrap();
        let user_payload = b"def main():\n    print('untouched')\n";
        fs::write(&user_code_path, user_payload).unwrap();

        let mut hasher = Sha256::new();
        hasher.update(user_payload);
        let pre_hash = hasher.finalize_reset();

        copy_orchestrator_to_sync(&source, &target).expect("copy");

        // Must still exist + be byte-identical.
        let after = fs::read(&user_code_path).expect("user file survives");
        hasher.update(&after);
        let post_hash = hasher.finalize();
        assert_eq!(
            pre_hash[..],
            post_hash[..],
            "user code at {} was modified by install (bytes differ)",
            user_code_path.display()
        );

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    /// Bug 32 #5: register_mcp into a `~/.claude.json` that already has
    /// MCP servers, OAuth session state, and per-project settings must
    /// preserve every existing key — only adding the new MCP entry.
    #[test]
    fn test_claude_json_merge_preserves_existing_mcp_servers_and_state() {
        use crate::mcp_registration::register_mcp;

        let target = std::env::temp_dir().join(format!(
            "vct-bug32-claude-json-test-{}.json",
            uuid::Uuid::new_v4().simple()
        ));
        // Pre-seed with realistic ~/.claude.json content: 3 existing MCP
        // servers, OAuth session marker, project-scoped settings.
        let existing = serde_json::json!({
            "mcpServers": {
                "alpha": {"type": "stdio", "command": "/usr/bin/alpha"},
                "beta": {"type": "stdio", "command": "/usr/bin/beta"},
                "gamma": {"type": "http", "url": "http://localhost:9999"},
            },
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "uuid": "11111111-2222-3333-4444-555555555555",
            },
            "projects": {
                "/home/user/somewhere": {
                    "mcpServers": {"private": {"type": "stdio", "command": "x"}},
                    "history": [{"display": "test"}],
                }
            },
            "feedbackSurveyState": {"lastShownTime": 1234567890},
        });
        std::fs::write(
            &target,
            serde_json::to_string_pretty(&existing).unwrap(),
        )
        .unwrap();

        // Now add a new MCP server (what install does).
        let new_entry = serde_json::json!({"type": "stdio", "command": "/usr/bin/weaviate-kg"});
        register_mcp(&target, "weaviate-kg", &new_entry).expect("register_mcp");

        let raw = std::fs::read_to_string(&target).unwrap();
        let v: serde_json::Value = serde_json::from_str(&raw).unwrap();

        // New server registered.
        assert_eq!(v["mcpServers"]["weaviate-kg"]["command"], "/usr/bin/weaviate-kg");
        // All 3 existing servers preserved.
        assert_eq!(v["mcpServers"]["alpha"]["command"], "/usr/bin/alpha");
        assert_eq!(v["mcpServers"]["beta"]["command"], "/usr/bin/beta");
        assert_eq!(v["mcpServers"]["gamma"]["url"], "http://localhost:9999");
        // OAuth session preserved.
        assert_eq!(v["oauthAccount"]["emailAddress"], "user@example.com");
        // Project-scoped settings preserved.
        assert_eq!(
            v["projects"]["/home/user/somewhere"]["mcpServers"]["private"]["command"],
            "x"
        );
        assert_eq!(v["projects"]["/home/user/somewhere"]["history"][0]["display"], "test");
        // Survey state preserved.
        assert_eq!(v["feedbackSurveyState"]["lastShownTime"], 1234567890);

        std::fs::remove_file(&target).ok();
    }

    /// Bug 32: preflight_install_safety_check returns a well-formed
    /// SafetyReport for a fresh install path. Cannot assert on
    /// existing_volumes / existing_collections deterministically (they
    /// reflect the test machine state) — assert structural sanity only.
    #[tokio::test]
    async fn test_preflight_install_safety_check_returns_report() {
        let target = std::env::temp_dir().join(format!(
            "vct-bug32-preflight-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&target).unwrap();
        // Pre-seed user-code that should appear in will_preserve_user_code.
        std::fs::create_dir_all(target.join("my_app")).unwrap();
        std::fs::write(target.join("README_USER.md"), "user readme").unwrap();

        let report =
            preflight_install_safety_check(target.to_string_lossy().to_string())
                .await
                .expect("preflight returns Ok");

        // User code surfaced.
        assert!(
            report.will_preserve_user_code.iter().any(|p| p == "my_app"),
            "expected my_app in will_preserve_user_code, got {:?}",
            report.will_preserve_user_code
        );
        assert!(
            report.will_preserve_user_code.iter().any(|p| p == "README_USER.md"),
            "expected README_USER.md in will_preserve_user_code"
        );
        // services_running + services_to_start partition the three core services.
        let total_known = report.services_running.len() + report.services_to_start.len();
        // weaviate + ollama always classified; code_embed only listed when running.
        assert!(total_known >= 2, "expected at least weaviate+ollama classified");
        // new_collections_to_add is a subset of the required class names
        // when Weaviate is unreachable (returns empty existing_collections,
        // so the required classes are all "new"). Either way, never has
        // duplicates.
        let mut sorted = report.new_collections_to_add.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(sorted.len(), report.new_collections_to_add.len(),
            "new_collections_to_add must not contain duplicates");

        std::fs::remove_dir_all(&target).ok();
    }

    /// Bug 32: when existing volumes are reported, the safety report
    /// includes a risk line warning that bind-mount overrides will be
    /// suppressed (Bug 31's volume picker must respect this).
    #[tokio::test]
    async fn test_preflight_risk_lines_call_out_existing_volumes_and_overwrites() {
        let target = std::env::temp_dir().join(format!(
            "vct-bug32-risk-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        // Pre-seed `.claude` so will_overwrite_orchestrator_files contains it
        // → expected risk line about read-merge-write.
        std::fs::create_dir_all(target.join(".claude")).unwrap();

        let report =
            preflight_install_safety_check(target.to_string_lossy().to_string())
                .await
                .expect("preflight returns Ok");

        // .claude is in the overwrite list → preservation risk line emitted.
        assert!(report.will_overwrite_orchestrator_files.contains(&".claude".to_string()));
        let has_preservation_risk = report
            .risks
            .iter()
            .any(|r| r.contains("settings.json env block") && r.contains("preserved"));
        assert!(
            has_preservation_risk,
            "expected risk line explaining .claude/settings.json read-merge-write semantics, got {:?}",
            report.risks
        );

        std::fs::remove_dir_all(&target).ok();
    }

    /// Reviewer B blocker: orchestrator data sprawl across ~/podman_volumes,
    /// ~/.claude, ~/.vct, ~/.vct-secrets, OS keychain, per-project .claude/.
    /// The uninstaller must:
    ///   1. Be idempotent (running twice does not fail).
    ///   2. NEVER touch ~/.vct-secrets/ (user secrets).
    ///   3. NEVER touch user code outside orchestrator-managed paths.
    /// We assert by source-grep: install.py's `_run_uninstall` body must
    /// not contain references to scrubbing ~/.vct-secrets/ or arbitrary
    /// user paths. Functional execution would touch the real machine and
    /// is out of scope for unit tests.
    #[test]
    fn test_uninstall_never_touches_secrets_or_user_code() {
        let repo_root = find_local_repo_root().expect("repo root resolves in tests");
        let install_py = repo_root.join("install.py");
        let content = std::fs::read_to_string(&install_py).expect("read install.py");

        let start = content
            .find("def _run_uninstall(")
            .expect("_run_uninstall defined");
        let after_start = &content[start..];
        // End at the next top-level `# ---` divider line (matches the
        // existing convention used elsewhere in install.py).
        let body_end = after_start[1..]
            .find("\n# ----")
            .map(|idx| idx + 1)
            .unwrap_or(after_start.len());
        let body = &after_start[..body_end];

        // 1. Must explicitly mention NOT touching ~/.vct-secrets/ — proves intent.
        assert!(
            body.contains(".vct-secrets"),
            "_run_uninstall must explicitly reference ~/.vct-secrets/ (and skip it)"
        );
        // 2. Must NOT call rm/unlink on .vct-secrets.
        assert!(
            !body.contains("vct-secrets/.unlink") && !body.contains("rm -rf"),
            "_run_uninstall must never call rm -rf or scrub .vct-secrets"
        );
        // 3. Must NOT remove arbitrary user paths — only Path.home() / .vct or .vibecoded entries.
        // Heuristic: forbid `shutil.rmtree(` and `os.remove(` on bare user paths.
        // (We allow `.unlink()` on specific files like launcher.db, audit logs.)
        for forbidden in &["shutil.rmtree(", "Path('/').", "rmdir('/')"] {
            assert!(
                !body.contains(forbidden),
                "_run_uninstall must not call {} (would risk user code)",
                forbidden
            );
        }
        // 4. MCP scrub must preserve user's other servers.
        assert!(
            body.contains("orchestrator_mcps"),
            "_run_uninstall must allow-list orchestrator MCPs only (preserving user MCPs)"
        );
    }

    /// Idempotence: a second `_run_uninstall` on an already-uninstalled
    /// machine must not error or print fake "removed X" lines. Source
    /// audit: every removal step must be guarded by an existence check.
    #[test]
    fn test_uninstall_is_idempotent_via_existence_guards() {
        let repo_root = find_local_repo_root().expect("repo root resolves in tests");
        let install_py = repo_root.join("install.py");
        let content = std::fs::read_to_string(&install_py).expect("read install.py");

        let start = content
            .find("def _run_uninstall(")
            .expect("_run_uninstall defined");
        let after_start = &content[start..];
        let body_end = after_start.find("\n# ----").unwrap_or(after_start.len());
        let body = &after_start[..body_end];

        // launcher.db removal is gated by .exists() check.
        assert!(
            body.contains("launcher_db.exists()"),
            "launcher.db removal must be guarded by .exists()"
        );
        // claude.json scrub is gated by .exists() check.
        assert!(
            body.contains("claude_json.exists()"),
            "~/.claude.json scrub must be guarded by .exists()"
        );
        // Container ops are gated by container_runtime is not None.
        assert!(
            body.contains("container_runtime is not None"),
            "container ops must be guarded by runtime detection"
        );
    }

    // ─── Install log reader (read_install_log_from + derive_install_state) ─────

    /// Helper: write a JSONL fixture and read it back through the public
    /// helper. Returns the (parsed-events, derived-summary) pair.
    fn parse_log_fixture(lines: &[&str]) -> InstallLog {
        let dir = tmp();
        let path = dir.join("install.jsonl");
        let body = lines.join("\n") + "\n";
        std::fs::write(&path, body).unwrap();
        let log = read_install_log_from(&path);
        std::fs::remove_dir_all(&dir).ok();
        log
    }

    #[test]
    fn test_install_log_missing_file() {
        // No file at the given path: exists=false, summary all-empty,
        // looks_complete=false. This is the fresh-install case.
        let dir = tmp();
        let log = read_install_log_from(&dir.join("nope.jsonl"));
        assert!(!log.exists);
        assert!(log.events.is_empty());
        assert!(log.state_summary.completed_steps.is_empty());
        assert!(log.state_summary.failed_steps.is_empty());
        assert!(!log.state_summary.looks_complete);
        assert!(log.state_summary.session_started.is_none());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_install_log_empty_file() {
        // File exists but no events: same shape as missing, but exists=true.
        let log = parse_log_fixture(&[]);
        assert!(log.exists);
        assert!(log.events.is_empty());
        assert!(!log.state_summary.looks_complete);
    }

    #[test]
    fn test_install_log_full_happy_path() {
        // Simulate a clean end-to-end install: install.py 1/10..10/10 ok,
        // session ok, post-install build/tauri ok, spawn ok. Summary
        // should report all steps completed and looks_complete=true.
        let log = parse_log_fixture(&[
            r#"{"ts":"2026-04-27T22:00:00Z","actor":"install.py","step":"1/10","phase":"start","detail":"checking"}"#,
            r#"{"ts":"2026-04-27T22:00:01Z","actor":"install.py","step":"1/10","phase":"ok","detail":"3.12.4"}"#,
            r#"{"ts":"2026-04-27T22:00:05Z","actor":"install.py","step":"2/10","phase":"ok","detail":"linux"}"#,
            r#"{"ts":"2026-04-27T22:00:10Z","actor":"install.py","step":"3/10","phase":"ok","detail":"venv"}"#,
            r#"{"ts":"2026-04-27T22:00:20Z","actor":"install.py","step":"4/10","phase":"ok","detail":"deps"}"#,
            r#"{"ts":"2026-04-27T22:00:30Z","actor":"install.py","step":"5/10","phase":"ok","detail":"compose"}"#,
            r#"{"ts":"2026-04-27T22:00:40Z","actor":"install.py","step":"6/10","phase":"ok","detail":"ollama"}"#,
            r#"{"ts":"2026-04-27T22:00:50Z","actor":"install.py","step":"7/10","phase":"ok","detail":"models"}"#,
            r#"{"ts":"2026-04-27T22:00:55Z","actor":"install.py","step":"7b/10","phase":"ok","detail":"collections"}"#,
            r#"{"ts":"2026-04-27T22:00:58Z","actor":"install.py","step":"7c/10","phase":"ok","detail":"seed"}"#,
            r#"{"ts":"2026-04-27T22:01:00Z","actor":"install.py","step":"8/10","phase":"ok","detail":"state"}"#,
            r#"{"ts":"2026-04-27T22:01:01Z","actor":"install.py","step":"9/10","phase":"ok","detail":"env"}"#,
            r#"{"ts":"2026-04-27T22:01:02Z","actor":"install.py","step":"10/10","phase":"ok","detail":"claude"}"#,
            r#"{"ts":"2026-04-27T22:01:03Z","actor":"install.py","step":"session","phase":"ok","detail":"finished"}"#,
            r#"{"ts":"2026-04-27T22:01:10Z","actor":"post-install-launcher.sh","step":"audit","phase":"ok","detail":"audited"}"#,
            r#"{"ts":"2026-04-27T22:01:30Z","actor":"post-install-launcher.sh","step":"build/tauri","phase":"ok","detail":"built"}"#,
            r#"{"ts":"2026-04-27T22:01:35Z","actor":"post-install-launcher.sh","step":"spawn","phase":"ok","detail":"launched"}"#,
        ]);

        assert!(log.exists);
        assert_eq!(log.events.len(), 17);
        let s = &log.state_summary;
        assert!(s.session_started.is_some());
        assert!(s.last_event_ts.is_some());
        assert!(s.failed_steps.is_empty());
        assert!(s.completed_steps.contains(&"10/10".to_string()));
        assert!(s.completed_steps.contains(&"build/tauri".to_string()));
        assert!(s.completed_steps.contains(&"spawn".to_string()));
        assert!(s.looks_complete);
    }

    #[test]
    fn test_install_log_failure_at_build() {
        // install.py completes but post-install build/tauri errors out.
        // looks_complete must be false, build/tauri must be in failed_steps.
        let log = parse_log_fixture(&[
            r#"{"ts":"2026-04-27T22:00:00Z","actor":"install.py","step":"1/10","phase":"start","detail":""}"#,
            r#"{"ts":"2026-04-27T22:00:01Z","actor":"install.py","step":"1/10","phase":"ok","detail":""}"#,
            r#"{"ts":"2026-04-27T22:01:03Z","actor":"install.py","step":"session","phase":"ok","detail":"finished"}"#,
            r#"{"ts":"2026-04-27T22:01:30Z","actor":"post-install-launcher.sh","step":"build/tauri","phase":"start","detail":""}"#,
            r#"{"ts":"2026-04-27T22:01:31Z","actor":"post-install-launcher.sh","step":"build/tauri","phase":"error","detail":"missing webkit2gtk"}"#,
        ]);
        let s = &log.state_summary;
        assert!(!s.looks_complete);
        let failed_steps: Vec<&String> = s.failed_steps.iter().map(|(s, _)| s).collect();
        assert!(failed_steps.contains(&&"build/tauri".to_string()));
        let build_err = s
            .failed_steps
            .iter()
            .find(|(s, _)| s == "build/tauri")
            .unwrap()
            .1
            .clone();
        assert!(build_err.contains("webkit2gtk"));
    }

    #[test]
    fn test_install_log_skip_classified_correctly() {
        // skip != ok != error. Make sure skipped_steps catches "skip" only.
        // Note: 1/10 needs a terminal phase or it would be misclassified as
        // interrupted. We follow start with ok like a real install.
        let log = parse_log_fixture(&[
            r#"{"ts":"2026-04-27T22:00:00Z","actor":"install.py","step":"1/10","phase":"start","detail":""}"#,
            r#"{"ts":"2026-04-27T22:00:00Z","actor":"install.py","step":"1/10","phase":"ok","detail":""}"#,
            r#"{"ts":"2026-04-27T22:00:01Z","actor":"install.py","step":"2b/10","phase":"skip","detail":"--no-joern"}"#,
            r#"{"ts":"2026-04-27T22:00:02Z","actor":"install.py","step":"9/10","phase":"skip","detail":".env preserved"}"#,
            r#"{"ts":"2026-04-27T22:00:03Z","actor":"install.py","step":"3/10","phase":"ok","detail":"venv"}"#,
        ]);
        let s = &log.state_summary;
        assert!(s.skipped_steps.contains(&"2b/10".to_string()));
        assert!(s.skipped_steps.contains(&"9/10".to_string()));
        assert!(s.completed_steps.contains(&"3/10".to_string()));
        assert!(s.completed_steps.contains(&"1/10".to_string()));
        assert!(s.failed_steps.is_empty());
    }

    #[test]
    fn test_install_log_corrupt_lines_are_skipped() {
        // Mid-write crashes can leave half a JSON object on a line. The
        // parser must drop those without bailing on the whole file.
        let log = parse_log_fixture(&[
            r#"{"ts":"2026-04-27T22:00:00Z","actor":"install.py","step":"1/10","phase":"start","detail":""}"#,
            r#"{"ts":"2026-04-27T22:00:01Z","actor":"insta"#, // corrupt
            r#"{"ts":"2026-04-27T22:00:02Z","actor":"install.py","step":"2/10","phase":"ok","detail":""}"#,
            r#"not even json"#,
            r#"{"ts":"2026-04-27T22:00:03Z","actor":"install.py","step":"3/10","phase":"ok","detail":""}"#,
        ]);
        // 3 valid records.
        assert_eq!(log.events.len(), 3);
        assert!(log.state_summary.completed_steps.contains(&"2/10".to_string()));
        assert!(log.state_summary.completed_steps.contains(&"3/10".to_string()));
    }

    #[test]
    fn test_install_log_interrupted_step() {
        // A step at "start" with no terminal phase = the writer crashed
        // mid-step. Must surface as failed (the wizard treats it as a
        // resume point, not as completed).
        let log = parse_log_fixture(&[
            r#"{"ts":"2026-04-27T22:00:00Z","actor":"install.py","step":"1/10","phase":"start","detail":""}"#,
            r#"{"ts":"2026-04-27T22:00:01Z","actor":"install.py","step":"1/10","phase":"ok","detail":""}"#,
            r#"{"ts":"2026-04-27T22:00:30Z","actor":"install.py","step":"5/10","phase":"start","detail":"compose up"}"#,
        ]);
        let s = &log.state_summary;
        let failed: Vec<&String> = s.failed_steps.iter().map(|(s, _)| s).collect();
        assert!(failed.contains(&&"5/10".to_string()));
        assert!(s
            .failed_steps
            .iter()
            .find(|(s, _)| s == "5/10")
            .map(|(_, d)| d.starts_with("interrupted:"))
            .unwrap_or(false));
    }

    // ─── Re-install conflict resolution (4-option modal) ───────────────────

    /// Build a fake adopt-target install root with user-edited copies of
    /// every preserve-list file plus a couple of directory contents, so
    /// each strategy has something to act on.
    fn fake_adopt_target() -> PathBuf {
        let p = tmp();
        // Pre-existing user content INSIDE .claude
        fs::create_dir_all(p.join(".claude")).unwrap();
        fs::write(
            p.join(".claude/CONTEXT_STATE.md"),
            "# user CONTEXT_STATE\nsome custom session state\n",
        )
        .unwrap();
        fs::write(
            p.join(".claude/PROJECT_REGISTRY.md"),
            "# user registry\nproject-foo\n",
        )
        .unwrap();
        fs::write(p.join(".claude/settings.json"), "{\"old\":true}").unwrap();
        // Top-level preserve-list files
        fs::write(p.join("CLAUDE.md"), "# user CLAUDE.md\ncustom rules\n").unwrap();
        fs::write(p.join(".env"), "USER_KEY=secret\n").unwrap();
        // Outside the allowlist — must survive every strategy.
        fs::write(p.join("user_code.py"), "print('survive')\n").unwrap();
        // Knowledge dir with one user-curated note that's NOT in the
        // preserve list (so it gets overwritten on every strategy that
        // copies — that's the contract).
        fs::create_dir_all(p.join("knowledge")).unwrap();
        fs::write(p.join("knowledge/note.md"), "OLD\n").unwrap();
        p
    }

    #[test]
    fn test_serde_conflict_strategy_snake_case() {
        let json = serde_json::to_string(&ConflictStrategy::OverwritePreserve).unwrap();
        assert_eq!(json, "\"overwrite_preserve\"");
        let parsed: ConflictStrategy =
            serde_json::from_str("\"delete_claude_and_reinstall\"").unwrap();
        assert_eq!(parsed, ConflictStrategy::DeleteClaudeAndReinstall);
    }

    #[test]
    fn test_serde_conflict_resolution_round_trip() {
        let r = ConflictResolution {
            strategy: ConflictStrategy::OverwritePreserve,
            preserve_paths: Some(vec!["CLAUDE.md".into(), ".claude/CONTEXT_STATE.md".into()]),
        };
        let json = serde_json::to_string(&r).unwrap();
        let back: ConflictResolution = serde_json::from_str(&json).unwrap();
        assert_eq!(back.strategy, ConflictStrategy::OverwritePreserve);
        assert_eq!(back.preserve_paths.unwrap().len(), 2);
    }

    #[test]
    fn test_install_conflict_error_serializes_with_kind_discriminator() {
        let err = InstallConflictError {
            kind: "install_conflict".into(),
            message: "boom".into(),
            install_path: "/tmp/x".into(),
            source_path: "/tmp/src".into(),
            mode: InstallMode::Adopt,
            will_overwrite: vec!["CLAUDE.md".into()],
            will_add: vec!["state".into()],
            preserve_candidates: vec!["CLAUDE.md".into()],
        };
        let s = err.into_err_string();
        assert!(s.contains("\"kind\":\"install_conflict\""));
        assert!(s.contains("\"will_overwrite\":[\"CLAUDE.md\"]"));
    }

    #[test]
    fn test_new_sibling_path_basic_extension() {
        let got = new_sibling_path(Path::new("/x/CLAUDE.md"));
        assert_eq!(got, PathBuf::from("/x/CLAUDE.new.md"));
    }

    #[test]
    fn test_new_sibling_path_dotfile_no_extension() {
        // .env has no "real" extension — append .new at end.
        let got = new_sibling_path(Path::new("/x/.env"));
        assert_eq!(got, PathBuf::from("/x/.env.new"));
    }

    #[test]
    fn test_new_sibling_path_no_extension() {
        let got = new_sibling_path(Path::new("/x/Makefile"));
        assert_eq!(got, PathBuf::from("/x/Makefile.new"));
    }

    #[test]
    fn test_new_sibling_path_double_extension_keeps_last() {
        // archive.tar.gz → split on LAST dot → archive.tar.new.gz
        let got = new_sibling_path(Path::new("/x/archive.tar.gz"));
        assert_eq!(got, PathBuf::from("/x/archive.tar.new.gz"));
    }

    #[test]
    fn test_strategy_adopt_as_is_is_noop_on_disk() {
        let source = fake_repo_source();
        let target = fake_adopt_target();
        let pre_user = fs::read_to_string(target.join("CLAUDE.md")).unwrap();

        let report =
            apply_conflict_strategy(&source, &target, ConflictStrategy::AdoptAsIs, &[]).unwrap();
        assert_eq!(report.copied_count, 0);
        assert_eq!(report.preserved_count, 0);
        assert_eq!(report.new_md_count, 0);
        assert!(!report.notification_written);

        // User file untouched.
        assert_eq!(fs::read_to_string(target.join("CLAUDE.md")).unwrap(), pre_user);
        // Bytes outside allowlist also untouched.
        assert_eq!(
            fs::read_to_string(target.join("user_code.py")).unwrap(),
            "print('survive')\n"
        );

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_overwrite_all_loses_user_edits_in_managed_paths() {
        let source = fake_repo_source();
        let target = fake_adopt_target();

        let report =
            apply_conflict_strategy(&source, &target, ConflictStrategy::OverwriteAll, &[]).unwrap();
        assert!(report.copied_count > 0);
        assert_eq!(report.preserved_count, 0);
        assert_eq!(report.new_md_count, 0);

        // CLAUDE.md from upstream replaces the user-edited one.
        assert_eq!(
            fs::read_to_string(target.join("CLAUDE.md")).unwrap(),
            "# project\n"
        );
        // settings.json overwritten with upstream {}
        assert_eq!(
            fs::read_to_string(target.join(".claude/settings.json")).unwrap(),
            "{}"
        );
        // No `.new.md` siblings.
        assert!(!target.join("CLAUDE.new.md").exists());
        // User code OUTSIDE the allowlist is preserved (allowlist is the
        // copy boundary, NOT a strategy choice).
        assert_eq!(
            fs::read_to_string(target.join("user_code.py")).unwrap(),
            "print('survive')\n"
        );

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_overwrite_preserve_writes_new_md_siblings_and_notification() {
        let source = fake_repo_source();
        let target = fake_adopt_target();

        let preserve: Vec<String> = DEFAULT_PRESERVE_LIST.iter().map(|s| s.to_string()).collect();
        let report = apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::OverwritePreserve,
            &preserve,
        )
        .unwrap();

        // CLAUDE.md is in fake_repo_source (so it's a candidate); user
        // version exists at target so we expect a CLAUDE.new.md sibling.
        assert!(target.join("CLAUDE.new.md").exists());
        assert_eq!(
            fs::read_to_string(target.join("CLAUDE.new.md")).unwrap(),
            "# project\n"
        );
        // User file untouched.
        assert_eq!(
            fs::read_to_string(target.join("CLAUDE.md")).unwrap(),
            "# user CLAUDE.md\ncustom rules\n"
        );
        // CONTEXT_STATE.md is in the preserve list but fake_repo_source
        // doesn't ship one, so no .new.md is written for it. The
        // existing user file must be left intact AND the notification
        // block appended to it.
        let ctx = fs::read_to_string(target.join(".claude/CONTEXT_STATE.md")).unwrap();
        assert!(ctx.contains("# user CONTEXT_STATE"));
        assert!(ctx.contains(MERGE_BLOCK_START));
        assert!(ctx.contains(MERGE_BLOCK_END));
        assert!(ctx.contains("CLAUDE.md"));

        // Knowledge dir is NOT preserved — the user note gets overwritten.
        assert_eq!(
            fs::read_to_string(target.join("knowledge/note.md")).unwrap(),
            "hello"
        );

        assert!(report.notification_written);
        assert!(report.new_md_count >= 1);
        assert_eq!(report.preserved_count, report.new_md_count);

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_overwrite_preserve_notification_block_is_idempotent() {
        // Run OverwritePreserve twice. The notification block must NOT
        // duplicate; the second run must REPLACE the first block in-place.
        let source = fake_repo_source();
        let target = fake_adopt_target();
        let preserve: Vec<String> = DEFAULT_PRESERVE_LIST.iter().map(|s| s.to_string()).collect();

        apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::OverwritePreserve,
            &preserve,
        )
        .unwrap();
        apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::OverwritePreserve,
            &preserve,
        )
        .unwrap();

        let ctx = fs::read_to_string(target.join(".claude/CONTEXT_STATE.md")).unwrap();
        let start_count = ctx.matches(MERGE_BLOCK_START).count();
        let end_count = ctx.matches(MERGE_BLOCK_END).count();
        assert_eq!(start_count, 1, "duplicate <!-- vct-merge-pending --> block");
        assert_eq!(end_count, 1, "duplicate <!-- /vct-merge-pending --> block");

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_delete_claude_wipes_only_dot_claude() {
        let source = fake_repo_source();
        let target = fake_adopt_target();

        let report = apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::DeleteClaudeAndReinstall,
            &[],
        )
        .unwrap();
        assert!(report.copied_count > 0);

        // .claude was wiped then re-populated with upstream settings.json
        // (the OLD `{"old":true}` is gone).
        assert_eq!(
            fs::read_to_string(target.join(".claude/settings.json")).unwrap(),
            "{}"
        );
        // Top-level files OUTSIDE .claude are NOT wiped — DeleteClaude
        // only nukes .claude/. CLAUDE.md was overwritten by the fresh
        // copy step (it's in the allowlist), so we check user_code.py
        // which is OUTSIDE the allowlist.
        assert_eq!(
            fs::read_to_string(target.join("user_code.py")).unwrap(),
            "print('survive')\n"
        );

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_delete_claude_handles_missing_dot_claude() {
        // If .claude doesn't exist (somehow we got dispatched to this
        // strategy on a non-adopt target), DeleteClaude must not error.
        let source = fake_repo_source();
        let target = tmp();
        fs::write(target.join("user_code.py"), "x").unwrap();

        let report = apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::DeleteClaudeAndReinstall,
            &[],
        )
        .expect("must not error on missing .claude/");
        assert!(report.copied_count > 0);

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_overwrite_preserve_with_custom_preserve_list() {
        let source = fake_repo_source();
        let target = fake_adopt_target();

        // Custom preserve list: only CLAUDE.md, exclude .env and others.
        let preserve = vec!["CLAUDE.md".to_string()];
        apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::OverwritePreserve,
            &preserve,
        )
        .unwrap();

        // CLAUDE.md preserved, sibling written.
        assert!(target.join("CLAUDE.new.md").exists());
        assert_eq!(
            fs::read_to_string(target.join("CLAUDE.md")).unwrap(),
            "# user CLAUDE.md\ncustom rules\n"
        );

        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_strategy_rejects_non_orchestrator_source() {
        let source = tmp(); // no vct-module.json
        let target = tmp();
        let err = apply_conflict_strategy(
            &source,
            &target,
            ConflictStrategy::OverwriteAll,
            &[],
        )
        .unwrap_err();
        assert!(err.contains("not an orchestrator repo"));
        fs::remove_dir_all(&source).ok();
        fs::remove_dir_all(&target).ok();
    }

    #[test]
    fn test_replace_or_append_block_appends_when_missing() {
        let existing = "# header\nbody\n";
        let block = "<!-- vct-merge-pending -->\nstuff\n<!-- /vct-merge-pending -->\n";
        let out = replace_or_append_block(existing, block);
        assert!(out.starts_with("# header\nbody\n"));
        assert!(out.contains("<!-- vct-merge-pending -->"));
    }

    #[test]
    fn test_replace_or_append_block_replaces_in_place() {
        let existing = "# header\n<!-- vct-merge-pending -->\nOLD STUFF\n<!-- /vct-merge-pending -->\n# tail\n";
        let new_block =
            "<!-- vct-merge-pending -->\nNEW STUFF\n<!-- /vct-merge-pending -->";
        let out = replace_or_append_block(existing, new_block);
        assert!(out.contains("NEW STUFF"));
        assert!(!out.contains("OLD STUFF"));
        assert!(out.contains("# header"));
        assert!(out.contains("# tail"));
        // Exactly one block.
        assert_eq!(out.matches(MERGE_BLOCK_START).count(), 1);
    }

    #[test]
    fn test_update_merge_notification_block_creates_file_if_missing() {
        let dir = tmp();
        let target_file = dir.join(".claude/CONTEXT_STATE.md");
        let written = update_merge_notification_block(
            &target_file,
            &["CLAUDE.md".to_string()],
        )
        .unwrap();
        assert!(written);
        assert!(target_file.exists());
        let content = fs::read_to_string(&target_file).unwrap();
        assert!(content.contains(MERGE_BLOCK_START));
        assert!(content.contains("CLAUDE.md"));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_civil_from_days_known_dates() {
        // unix epoch (1970-01-01) is day 0.
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        // 2026-04-27 = days since epoch. Don't compute by hand; just
        // sanity-check via the iso formatter.
        let s = chrono_iso_z();
        assert!(s.ends_with("Z"));
        assert_eq!(s.len(), 20); // YYYY-MM-DDTHH:MM:SSZ
    }

    // ---- check_install_health predicate ---------------------------------
    //
    // The .exe-only install scenario: a user downloads the launcher binary
    // from a GitHub Release and skips first-install. The four signals must
    // all flip to true for `all_ok` to fire; missing any one of them is
    // sufficient evidence the install never ran.

    #[test]
    fn test_inspect_install_health_incomplete_dir() {
        let p = tmp();
        // Plant ONLY the install-root markers (CLAUDE.md + install.py)
        // so the path looks like an install root, but none of the four
        // post-install signals are present.
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();

        let health = inspect_install_health_at(&p);
        assert_eq!(health.install_root, Some(p.to_string_lossy().to_string()));
        assert!(!health.has_venv);
        assert!(!health.has_state_dir);
        assert!(!health.has_env_with_kg);
        assert!(!health.mcp_servers_ok);
        assert!(!health.all_ok);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_install_health_complete_dir() {
        let p = tmp();
        fs::write(p.join("CLAUDE.md"), "# claude\n").unwrap();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::create_dir_all(p.join(".venv")).unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        fs::write(
            p.join(".env"),
            "FOO=bar\nKG_COLLECTION=ClaudeKnowledgeGraph\n",
        )
        .unwrap();
        fs::create_dir_all(p.join("claude_mcp_servers").join(".venv")).unwrap();

        let health = inspect_install_health_at(&p);
        assert!(health.has_venv);
        assert!(health.has_state_dir);
        assert!(health.has_env_with_kg);
        assert!(health.mcp_servers_ok);
        assert!(health.all_ok);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_install_health_env_without_kg_line() {
        // .env is present but missing the KG_COLLECTION key — must NOT
        // count as healthy.
        let p = tmp();
        fs::create_dir_all(p.join(".venv")).unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        fs::write(p.join(".env"), "FOO=bar\nBAZ=qux\n").unwrap();
        fs::create_dir_all(p.join("claude_mcp_servers").join(".venv")).unwrap();

        let health = inspect_install_health_at(&p);
        assert!(!health.has_env_with_kg);
        assert!(!health.all_ok);
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn test_inspect_install_health_mcp_dir_without_venv() {
        // claude_mcp_servers/ exists but its .venv/ does not — must NOT
        // count as healthy. This is the "user copied the source tree but
        // never ran the MCP server bootstrap" failure mode.
        let p = tmp();
        fs::create_dir_all(p.join(".venv")).unwrap();
        fs::create_dir_all(p.join("state")).unwrap();
        fs::write(p.join(".env"), "KG_COLLECTION=Foo\n").unwrap();
        fs::create_dir_all(p.join("claude_mcp_servers")).unwrap();
        // .venv intentionally absent inside claude_mcp_servers/

        let health = inspect_install_health_at(&p);
        assert!(!health.mcp_servers_ok);
        assert!(!health.all_ok);
        fs::remove_dir_all(&p).ok();
    }

    // ---- validate_source_repo (2026-04-29 wizard install-path lockdown) ----
    //
    // The wizard previously let the user pick any folder as install_path;
    // install_orchestrator would copy ORCHESTRATOR_MANAGED_PATHS in but the
    // result was a half-install with no launcher / no first-install.sh.
    // validate_source_repo() rejects non-source folders BEFORE any
    // mutations. The discriminator is install.py + first-install.sh side
    // by side — both ship in every clone, neither is in
    // ORCHESTRATOR_MANAGED_PATHS, so a half-install can't fake it.

    #[test]
    fn install_orchestrator_rejects_non_source_repo() {
        // An empty tmp dir is the canonical "user picked an empty folder"
        // case the lockdown is meant to refuse. validate_source_repo must
        // return Err with a message that mentions install.py and
        // first-install.sh so the user knows what's expected.
        let p = tmp();
        let res = validate_source_repo(&p);
        assert!(res.is_err(), "validate_source_repo accepted an empty dir");
        let msg = res.unwrap_err();
        assert!(
            msg.contains("install.py") && msg.contains("first-install.sh"),
            "error message must name the two required files; got: {}",
            msg
        );
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn install_orchestrator_rejects_partial_source_repo() {
        // install.py present but first-install.sh missing — still a
        // non-source target, must be refused.
        let p = tmp();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        let res = validate_source_repo(&p);
        assert!(res.is_err(), "validate_source_repo accepted install.py-only dir");
        fs::remove_dir_all(&p).ok();
    }

    #[test]
    fn install_orchestrator_accepts_source_repo() {
        // Both markers present — must pass. (Intentionally short-circuits
        // before any real install side effects; this test only exercises
        // the validation gate, not the rest of install_orchestrator.)
        let p = tmp();
        fs::write(p.join("install.py"), "# install\n").unwrap();
        fs::write(p.join("first-install.sh"), "#!/usr/bin/env bash\n").unwrap();
        let res = validate_source_repo(&p);
        assert!(res.is_ok(), "validate_source_repo rejected a source repo: {:?}", res);
        fs::remove_dir_all(&p).ok();
    }
}
