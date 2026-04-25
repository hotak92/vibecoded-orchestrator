use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tauri::{command, Emitter, Window};

const ORCHESTRATOR_REPO: &str = "https://github.com/VibeCoded-Tools/orchestrator.git";

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

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

/// Detect system capabilities: GPU, container runtime, Python, etc.
#[command]
pub async fn detect_system() -> Result<SystemDetection, String> {
    let os = std::env::consts::OS.to_string();
    let arch = std::env::consts::ARCH.to_string();

    // Run detections in parallel via tokio
    let (gpu_result, docker, podman, python_result, claude, git, node) = tokio::join!(
        detect_nvidia_gpu(),
        check_command_exists("docker"),
        check_command_exists("podman"),
        detect_python(),
        check_command_exists("claude"),
        check_command_exists("git"),
        check_command_exists("node"),
    );

    let (has_nvidia, gpu_name) = gpu_result;
    let (has_python, python_version, python_cmd) = python_result;
    let has_apple_silicon = os == "macos" && arch == "aarch64";

    Ok(SystemDetection {
        os,
        arch,
        has_nvidia_gpu: has_nvidia,
        gpu_name,
        has_apple_silicon,
        has_docker: docker,
        has_podman: podman,
        has_python,
        python_version,
        python_cmd,
        has_claude_cli: claude,
        has_git: git,
        has_node: node,
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

/// Install the orchestrator: clone repo + run install.py.
/// Emits "install_progress" events to the window.
#[command]
pub async fn install_orchestrator(
    config: InstallConfig,
    window: Window,
) -> Result<InstallResult, String> {
    let install_path = PathBuf::from(&config.install_path);
    let system = detect_system().await?;

    // Validate prerequisites
    if !system.has_git {
        return Err("Git is required. Install from https://git-scm.com".to_string());
    }
    if !system.has_python {
        return Err(format!(
            "Python 3.11+ is required. Install from https://python.org"
        ));
    }

    // Stage 1: Clone
    emit_progress(&window, "clone", "Cloning orchestrator repository...", 5.0);

    if install_path.join(".git").exists() {
        emit_progress(&window, "clone", "Repository already exists, pulling latest...", 10.0);
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
    } else {
        // Create parent directory
        if let Some(parent) = install_path.parent() {
            tokio::fs::create_dir_all(parent)
                .await
                .map_err(|e| format!("Cannot create directory: {}", e))?;
        }

        let clone = tokio::process::Command::new("git")
            .args(["clone", ORCHESTRATOR_REPO, &install_path.to_string_lossy()])
            .output()
            .await
            .map_err(|e| format!("git clone failed: {}", e))?;

        if !clone.status.success() {
            let stderr = String::from_utf8_lossy(&clone.stderr);
            return Err(format!("git clone failed: {}", stderr));
        }
    }

    emit_progress(&window, "clone", "Repository ready", 20.0);

    // Stage 2: Run install.py
    emit_progress(&window, "install", "Running installer...", 25.0);

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

async fn detect_nvidia_gpu() -> (bool, String) {
    let result = tokio::process::Command::new("nvidia-smi")
        .args(["--query-gpu=name", "--format=csv,noheader,nounits"])
        .output()
        .await;

    match result {
        Ok(output) if output.status.success() => {
            let name = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if name.is_empty() {
                (false, String::new())
            } else {
                // Take first GPU name if multiple
                let first = name.lines().next().unwrap_or("").to_string();
                (true, first)
            }
        }
        _ => (false, String::new()),
    }
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
