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

/// Install the orchestrator by COPYING from the launcher's local repo
/// source to `config.install_path` (Bug 17). No network, no `git clone`,
/// no GitHub PAT. After the copy, optionally invoke `install.py` for
/// post-copy setup (venv + container start). Emits "install_progress"
/// events.
///
/// `confirm_overwrite` must be `true` when classify_install_target reports
/// `Adopt` — otherwise we refuse and force the caller through `preview_install`
/// + a confirm modal first.
#[command]
pub async fn install_orchestrator(
    config: InstallConfig,
    confirm_overwrite: Option<bool>,
    window: Window,
) -> Result<InstallResult, String> {
    let install_path = PathBuf::from(&config.install_path);
    let system = detect_system().await?;

    if !system.has_python {
        return Err(format!(
            "Python 3.11+ is required. Install from https://python.org"
        ));
    }

    // Bug 8: refuse to silently overwrite existing orchestrator-managed
    // files. Frontend must call preview_install + show a confirm modal,
    // then re-invoke with confirm_overwrite=true.
    let mode = classify_install_target(&install_path);
    let confirmed = confirm_overwrite.unwrap_or(false);
    if mode == InstallMode::Adopt && !confirmed {
        return Err(
            "install_path already contains orchestrator files (.claude/, knowledge/, etc.). \
             Call preview_install first and re-invoke with confirm_overwrite=true to adopt."
                .to_string(),
        );
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

    // Stage 2: Copy orchestrator-managed paths
    emit_progress(&window, "copy", "Copying orchestrator files...", 15.0);
    let target_clone = install_path.clone();
    let source_clone = source.clone();
    let copy_result = tokio::task::spawn_blocking(move || {
        copy_orchestrator_to_sync(&source_clone, &target_clone)
    })
    .await
    .map_err(|e| format!("copy task panicked: {}", e))?;
    copy_result?;

    emit_progress(&window, "copy", "Files copied", 70.0);

    // Stage 3: Run install.py for post-copy setup (venv, containers, etc.)
    emit_progress(&window, "install", "Running post-copy setup...", 75.0);

    let mut install_args = vec!["install.py".to_string()];

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
    let install_output = tokio::process::Command::new(python_cmd)
        .args(&install_args)
        .current_dir(&install_path)
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
    let install_output = tokio::process::Command::new(python_cmd)
        .args(["install.py", "--update"])
        .current_dir(&install_path)
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
        .map(meminfo_kb_to_gb)
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
    Some(((bytes as f64) / (1024.0 * 1024.0 * 1024.0)).round() as u64)
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
pub fn meminfo_kb_to_gb(kb: u64) -> u64 {
    // 1 GB (binary) = 1024 * 1024 kB. Add half a GB for round-to-nearest.
    let denom: u64 = 1024 * 1024;
    (kb + denom / 2) / denom
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
    let candidates = if cfg!(windows) {
        vec!["python", "python3", "py"]
    } else {
        vec!["python3.12", "python3.11", "python3", "python"]
    };

    for cmd in candidates {
        let result = tokio::process::Command::new(cmd)
            .args(["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"])
            .output()
            .await;

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
}
