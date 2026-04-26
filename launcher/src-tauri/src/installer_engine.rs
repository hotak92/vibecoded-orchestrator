//! Module installation engine.
//!
//! Takes a parsed `ModuleManifest` + resolved `PlaceholderCtx` and executes
//! the install steps: git clone / tarball / pypi / npm, followed by
//! `post_install` commands in order. Each command is tokenized via shlex —
//! never passed to a shell — so manifest authors can't inject `&&`, `;`,
//! `$(..)`, etc.
//!
//! Progress is reported through Tauri events (`module://install-progress`)
//! so the React UI can show a progress bar per step.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use tauri::{AppHandle, Emitter};
use tokio::process::Command;

use crate::manifest::{CommandSpec, InstallMethod, ModuleManifest, PlaceholderCtx};

#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InstallStage {
    Clone,
    PostInstall,
    Done,
    Failed,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct InstallProgress {
    pub project_id: String,
    pub module_id: String,
    pub stage: InstallStage,
    pub step_index: u32,
    pub step_total: u32,
    pub percent: u8,
    pub message: String,
}

/// Perform the install for a module into `install_dir`.
///
/// Returns the resolved install directory path on success. On failure,
/// leaves the partially-installed directory in place (caller can inspect
/// or delete it) and returns the error.
pub async fn run_install(
    app: &AppHandle,
    manifest: &ModuleManifest,
    ctx: &PlaceholderCtx,
    project_id: &str,
) -> Result<PathBuf, String> {
    let install_dir = ctx.resolve_install_dir(&manifest.install.install_dir);
    let allowed_root = ctx.vct_modules.clone();

    // Security: refuse paths outside ~/.vct/modules/.
    crate::manifest::validate_install_dir(&install_dir, &allowed_root)?;

    let total_steps: u32 = 1 + manifest.install.post_install.len() as u32;
    let mut step_index: u32 = 0;

    // ─── Step 1: fetch source ────────────────────────────────────────────
    emit_progress(app, project_id, &manifest.id, InstallStage::Clone, step_index, total_steps, "Fetching source");
    step_index += 1;

    match manifest.install.method {
        InstallMethod::GitClone => {
            git_clone(
                manifest.install.source.as_deref().ok_or("install.source required for git_clone")?,
                manifest.install.r#ref.as_deref().unwrap_or("main"),
                &install_dir,
            )
            .await?;
        }
        InstallMethod::Local => {
            // Local: install_dir must already exist; skip fetch.
            if !install_dir.exists() {
                return Err(format!(
                    "install.method=local requires install_dir to exist: {}",
                    install_dir.display()
                ));
            }
        }
    }

    // ─── Step 2+: post_install commands ──────────────────────────────────
    let ctx_with_dir = ctx.clone().with_install_dir(install_dir.clone());
    for (i, cmd_spec) in manifest.install.post_install.iter().enumerate() {
        let message = format!("Running setup step {}/{}", i + 1, manifest.install.post_install.len());
        emit_progress(
            app,
            project_id,
            &manifest.id,
            InstallStage::PostInstall,
            step_index,
            total_steps,
            &message,
        );
        step_index += 1;
        run_post_install_command(cmd_spec, &ctx_with_dir).await?;
    }

    emit_progress(
        app,
        project_id,
        &manifest.id,
        InstallStage::Done,
        step_index,
        total_steps,
        "Install complete",
    );

    Ok(install_dir)
}

async fn git_clone(source: &str, git_ref: &str, dest: &Path) -> Result<(), String> {
    if let Some(parent) = dest.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|e| format!("create parent {}: {}", parent.display(), e))?;
    }

    let status = Command::new("git")
        .args(["clone", "--depth", "1", "--branch", git_ref, source])
        .arg(dest)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .status()
        .await
        .map_err(|e| format!("spawn git clone: {}", e))?;

    if !status.success() {
        return Err(format!(
            "git clone failed (exit {}): {} {} -> {}",
            status.code().unwrap_or(-1),
            source,
            git_ref,
            dest.display()
        ));
    }
    Ok(())
}

async fn run_post_install_command(
    spec: &CommandSpec,
    ctx: &PlaceholderCtx,
) -> Result<(), String> {
    let raw = pick_platform_cmd(spec);
    let resolved = ctx.resolve(&raw);

    let tokens = shlex::split(&resolved)
        .ok_or_else(|| format!("cannot tokenize command: {}", resolved))?;
    let (program, args) = tokens
        .split_first()
        .ok_or_else(|| format!("empty command: {}", resolved))?;

    // Resolve cwd
    let cwd_str = spec
        .cwd
        .as_ref()
        .map(|s| ctx.resolve(s))
        .unwrap_or_else(|| {
            ctx.install_dir
                .as_ref()
                .map(|p| p.display().to_string())
                .unwrap_or_else(|| ".".to_string())
        });
    let cwd = PathBuf::from(&cwd_str);

    let mut cmd = Command::new(program);
    cmd.args(args);
    cmd.current_dir(&cwd);
    // Scrubbed env: only pass through PATH, HOME, USER, and platform essentials.
    cmd.env_clear();
    for key in ["PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL"] {
        if let Ok(v) = std::env::var(key) {
            cmd.env(key, v);
        }
    }
    #[cfg(target_os = "windows")]
    for key in ["SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"] {
        if let Ok(v) = std::env::var(key) {
            cmd.env(key, v);
        }
    }

    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());

    let timeout = Duration::from_secs(spec.timeout_s);
    let output = tokio::time::timeout(timeout, cmd.output())
        .await
        .map_err(|_| format!("command timed out after {}s: {}", spec.timeout_s, resolved))?
        .map_err(|e| format!("spawn {}: {}", program, e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "command failed (exit {}): {}\nstderr: {}",
            output.status.code().unwrap_or(-1),
            resolved,
            stderr.chars().take(500).collect::<String>()
        ));
    }

    Ok(())
}

fn pick_platform_cmd(spec: &CommandSpec) -> String {
    let platform = if cfg!(target_os = "windows") {
        "windows"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else {
        "linux"
    };
    spec.platform_cmd
        .get(platform)
        .cloned()
        .unwrap_or_else(|| spec.cmd.clone())
}

fn emit_progress(
    app: &AppHandle,
    project_id: &str,
    module_id: &str,
    stage: InstallStage,
    step_index: u32,
    step_total: u32,
    message: &str,
) {
    let percent = if step_total == 0 {
        0
    } else {
        ((step_index as f32 / step_total as f32) * 100.0).round() as u8
    };
    let payload = InstallProgress {
        project_id: project_id.to_string(),
        module_id: module_id.to_string(),
        stage,
        step_index,
        step_total,
        percent,
        message: message.to_string(),
    };
    // Emit via Tauri's event system; swallow errors (UI-only signal).
    let _ = app.emit("module://install-progress", payload);
}
