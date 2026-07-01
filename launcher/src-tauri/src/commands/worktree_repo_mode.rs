// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Per-project `worktree_repo_mode` — GUI-only tri-state setting backing the
//! subagent-git modal (v0.2.71 Track T-WT).
//!
//! ## What this is
//! When a project's workspace ROOT is not inside any git repo, the
//! closed-source Claude Code harness's `git worktree add` (driven by the 9
//! agents that declare `isolation: worktree`) fails the subagent spawn. VCO
//! cannot intercept that spawn, so its only levers are: detect/use an
//! enclosing repo, offer to `git init` a local-only repo, or strip the
//! `isolation: worktree` frontmatter so the harness never attempts a
//! worktree. The subagent-git modal surfaces those choices; this setting
//! records the user's decision per-project.
//!
//! Tri-state (a string, NOT a bool — three states):
//!   * `"use_existing"` — an enclosing repo was detected (root or a parent
//!     has `.git`); the harness walks up to it, isolation already works.
//!     Recorded automatically when detection succeeds; the user never has
//!     to click it.
//!   * `"local_init"` — user accepted a local-only `git init` at the
//!     workspace root.
//!   * `"no_repo"` — user opted out; subagents run in the shared cwd (no
//!     isolation). The enforcement (frontmatter strip) is owned by the
//!     project_init / install-bundle flow keyed off this setting — NOT by
//!     this command, which only persists the choice.
//!
//! ## GUI-only, NOT hub-resolved
//! Deliberately stored in `module_settings` (module_id =
//! "orchestrator-core") and read back only via the launcher GUI. It is NOT
//! added to `config_api.rs`'s `ProjectConfigResponse` because the harness
//! spawn pathway (the only consumer that would need a hub-resolved value)
//! cannot be intercepted by VCO — there is nothing on the MCP/hook side that
//! resolves this at spawn time. Keeping it GUI-only avoids a hub field that
//! no subprocess reads.
//!
//! Plumbing mirrors the `shared_kg_read_disabled` / RL-flag pattern
//! (`rl_settings.rs:set_rl_use_global`) but with a string value instead of a
//! bool, since this is tri-state.

use serde_json::Value;
use std::path::Path;
use tauri::{command, State};

use crate::db::Db;
use vct_launcher_core::process::CommandExt;

/// Module id under which the orchestrator's own per-project settings live.
const MODULE_ID: &str = "orchestrator-core";

/// Setting key for the tri-state worktree-repo mode.
const SETTING_KEY: &str = "worktree_repo_mode";

/// The three valid modes. Any other value is rejected by the setter so a
/// GUI bug can't persist garbage the modal then can't interpret.
const VALID_MODES: [&str; 3] = ["use_existing", "local_init", "no_repo"];

/// Persist the per-project worktree-repo mode.
///
/// Rejects an empty `project_id` (caller bug, not a soft-fail) and any
/// `mode` outside the tri-state set.
#[command]
pub async fn set_worktree_repo_mode(
    project_id: String,
    mode: String,
    db: State<'_, Db>,
) -> Result<(), String> {
    if project_id.is_empty() {
        return Err("set_worktree_repo_mode: project_id required".into());
    }
    if !VALID_MODES.contains(&mode.as_str()) {
        return Err(format!(
            "set_worktree_repo_mode: invalid mode '{}' (expected one of {:?})",
            mode, VALID_MODES
        ));
    }
    db.set_setting(&project_id, MODULE_ID, SETTING_KEY, &Value::String(mode))
}

/// Read back the per-project worktree-repo mode.
///
/// Returns `None` (serialised as JSON `null`) when no choice has been
/// recorded yet — the modal uses that to know it should prompt. A non-string
/// or unknown stored value is coerced to `None` (defensive: never hand the
/// GUI a value outside the tri-state contract).
#[command]
pub async fn get_worktree_repo_mode(
    project_id: String,
    db: State<'_, Db>,
) -> Result<Option<String>, String> {
    if project_id.is_empty() {
        return Err("get_worktree_repo_mode: project_id required".into());
    }
    let stored = db
        .get_setting(&project_id, MODULE_ID, SETTING_KEY)?
        .and_then(|v| v.as_str().map(|s| s.to_string()));
    Ok(match stored {
        Some(s) if VALID_MODES.contains(&s.as_str()) => Some(s),
        _ => None,
    })
}

/// Result of the git-repo detection probe the modal runs before offering
/// choices. `inside_repo` is true when the workspace root OR any ancestor is
/// already a git worktree (in which case isolation already works and the modal
/// auto-selects `use_existing`, never offering `local_init`).
#[derive(serde::Serialize)]
pub struct GitRepoDetection {
    /// The workspace root IS inside a git worktree (self or a parent).
    pub inside_repo: bool,
    /// The toplevel of the enclosing repo, when `inside_repo` (for display).
    pub toplevel: Option<String>,
}

/// Detect whether `project_root` is already inside a git repo (walking UP to
/// any parent), so the modal can offer "use existing" (auto) vs the
/// create-new / opt-out choices. Uses `git rev-parse --show-toplevel` from the
/// project root — correct for the subdir-of-a-bigger-repo and monorepo cases
/// (it returns the REAL toplevel, not an assumed one). Soft-fails to
/// `inside_repo=false` (no git / not a repo) so the modal defaults to offering
/// create-new/opt-out.
#[command]
pub async fn detect_project_git_repo(project_root: String) -> Result<GitRepoDetection, String> {
    if project_root.trim().is_empty() {
        return Err("detect_project_git_repo: project_root required".into());
    }
    let root = Path::new(&project_root);
    let out = tokio::process::Command::new("git")
        .silent()
        .args(["rev-parse", "--show-toplevel"])
        .current_dir(root)
        .output()
        .await;
    match out {
        Ok(o) if o.status.success() => {
            let top = String::from_utf8_lossy(&o.stdout).trim().to_string();
            Ok(GitRepoDetection {
                inside_repo: !top.is_empty(),
                toplevel: if top.is_empty() { None } else { Some(top) },
            })
        }
        // Non-zero (not a repo) or spawn failure → treat as not inside a repo.
        _ => Ok(GitRepoDetection {
            inside_repo: false,
            toplevel: None,
        }),
    }
}

/// Create a LOCAL-ONLY git repo at the project root (the modal's "create new"
/// choice). SAFETY GUARDS — this MUTATES the user's filesystem, so it refuses
/// anything ambiguous:
///   1. REFUSE if the root is ALREADY inside a git repo (self or a parent) —
///      `git init` there would create a nested/duplicate repo or be a no-op on
///      an existing `.git`. The modal only offers this when detection said
///      NOT inside a repo, but we re-check here (TOCTOU + defense-in-depth).
///   2. Init with `--initial-branch=main`, NO remote (local-only, never pushed).
///   3. Append a guard block to the root `.gitignore` so `git add -A` does NOT
///      swallow NESTED repos (e.g. ARTup's `Code/python/ARTup_platform/` shape):
///      each immediate-child dir that is itself a git repo is ignored, plus the
///      VCO runtime paths. We do NOT auto-commit — the repo starts empty so the
///      user controls what (if anything) they track.
/// Cross-OS: pure `git` invocation via the shared `.silent()` wrapper.
#[command]
pub async fn create_local_project_repo(project_root: String) -> Result<(), String> {
    if project_root.trim().is_empty() {
        return Err("create_local_project_repo: project_root required".into());
    }
    let root = Path::new(&project_root);
    if !root.is_dir() {
        return Err(format!(
            "create_local_project_repo: '{}' is not a directory",
            project_root
        ));
    }

    // Guard 1: refuse if already inside a repo (self or parent).
    let det = detect_project_git_repo(project_root.clone()).await?;
    if det.inside_repo {
        return Err(format!(
            "create_local_project_repo: '{}' is already inside a git repo ({}). \
             Use the existing repo instead of creating a nested one.",
            project_root,
            det.toplevel.as_deref().unwrap_or("unknown toplevel")
        ));
    }

    // Guard 2: local-only init, no remote, main branch.
    let init = tokio::process::Command::new("git")
        .silent()
        .args(["init", "--initial-branch=main"])
        .current_dir(root)
        .output()
        .await
        .map_err(|e| format!("git init spawn failed: {}", e))?;
    if !init.status.success() {
        return Err(format!(
            "git init failed: {}",
            String::from_utf8_lossy(&init.stderr).trim()
        ));
    }

    // Guard 3: append the nested-repo + VCO-runtime ignore guard.
    if let Err(e) = append_local_repo_gitignore_guard(root) {
        // Non-fatal: the repo exists; a missing ignore guard is a warning, not
        // a failure (the user can still add it). Log to stderr, don't fail.
        eprintln!(
            "[worktree_repo_mode] create_local_project_repo: gitignore guard append failed: {}",
            e
        );
    }
    Ok(())
}

/// Append a guard block to `<root>/.gitignore` that ignores (a) each immediate
/// child directory that is ITSELF a git repo (so a stray `git add -A` can't
/// swallow a nested repo like `ARTup_platform/`), and (b) the VCO runtime
/// paths. Idempotent: skips if our marker is already present.
fn append_local_repo_gitignore_guard(root: &Path) -> std::io::Result<()> {
    use std::io::Write;
    const MARKER: &str = "# --- VCO local-only repo guard (created by the subagent-git modal) ---";
    let gi = root.join(".gitignore");
    if let Ok(existing) = std::fs::read_to_string(&gi) {
        if existing.contains(MARKER) {
            return Ok(());
        }
    }
    // Find immediate-child dirs that are themselves git repos.
    let mut nested: Vec<String> = Vec::new();
    if let Ok(entries) = std::fs::read_dir(root) {
        for e in entries.flatten() {
            let p = e.path();
            if p.is_dir() && p.join(".git").exists() {
                if let Some(name) = p.file_name().and_then(|n| n.to_str()) {
                    nested.push(name.to_string());
                }
            }
        }
    }
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&gi)?;
    writeln!(f, "\n{}", MARKER)?;
    writeln!(
        f,
        "# This is a LOCAL-ONLY repo for subagent worktree isolation — never pushed."
    )?;
    writeln!(f, "# Nested git repos (do not absorb them into this repo):")?;
    for n in &nested {
        writeln!(f, "/{}/", n)?;
    }
    writeln!(f, "# VCO runtime / per-machine state:")?;
    for p in [".claude/state/", ".claude/worktrees/", ".claude/logs/"] {
        writeln!(f, "{}", p)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_modes_are_exactly_the_tri_state() {
        // Pins the tri-state contract so a future edit that adds/removes a
        // mode must consciously update both the const and this test (and the
        // modal's option set).
        assert_eq!(VALID_MODES.len(), 3);
        assert!(VALID_MODES.contains(&"use_existing"));
        assert!(VALID_MODES.contains(&"local_init"));
        assert!(VALID_MODES.contains(&"no_repo"));
    }

    #[test]
    fn module_id_is_orchestrator_core() {
        // The setting must land under orchestrator-core (same blob the
        // shared-KG / per-project orchestrator flags use), never under a
        // paid-module id.
        assert_eq!(MODULE_ID, "orchestrator-core");
        assert_eq!(SETTING_KEY, "worktree_repo_mode");
    }

    fn git_available() -> bool {
        std::process::Command::new("git")
            .arg("--version")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }

    #[tokio::test]
    async fn detect_not_a_repo_reports_not_inside() {
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let det = detect_project_git_repo(tmp.path().to_string_lossy().to_string())
            .await
            .unwrap();
        assert!(!det.inside_repo, "a plain temp dir is not inside a repo");
        assert!(det.toplevel.is_none());
    }

    #[tokio::test]
    async fn create_local_repo_then_detects_inside() {
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().to_string_lossy().to_string();
        // Create a nested child repo FIRST to prove the ignore-guard captures it.
        let nested = tmp.path().join("nested_app");
        std::fs::create_dir_all(&nested).unwrap();
        std::process::Command::new("git")
            .args(["init", "--initial-branch=main"])
            .current_dir(&nested)
            .output()
            .unwrap();

        create_local_project_repo(root.clone()).await.unwrap();

        // The root is now a repo.
        let det = detect_project_git_repo(root.clone()).await.unwrap();
        assert!(det.inside_repo, "root should now be a git repo");
        // The nested repo is ignored (not absorbed) + the guard marker present.
        let gi = std::fs::read_to_string(tmp.path().join(".gitignore")).unwrap();
        assert!(gi.contains("VCO local-only repo guard"), "guard marker present");
        assert!(gi.contains("/nested_app/"), "nested repo ignored: {gi}");
        assert!(gi.contains(".claude/worktrees/"), "runtime paths ignored");
    }

    #[tokio::test]
    async fn create_local_repo_refuses_when_already_inside_a_repo() {
        if !git_available() {
            return;
        }
        // The DANGEROUS case: never git-init inside an existing repo (would make
        // a nested/duplicate). Init the parent, then a subdir must be refused.
        let tmp = tempfile::tempdir().unwrap();
        std::process::Command::new("git")
            .args(["init", "--initial-branch=main"])
            .current_dir(tmp.path())
            .output()
            .unwrap();
        let sub = tmp.path().join("subdir");
        std::fs::create_dir_all(&sub).unwrap();
        let res = create_local_project_repo(sub.to_string_lossy().to_string()).await;
        assert!(
            res.is_err(),
            "must REFUSE git init inside an existing repo (parent has .git)"
        );
        assert!(res.unwrap_err().contains("already inside a git repo"));
    }

    #[tokio::test]
    async fn create_local_repo_gitignore_guard_is_idempotent() {
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().to_string_lossy().to_string();
        create_local_project_repo(root.clone()).await.unwrap();
        // Re-running append (via a second helper call) must not duplicate the block.
        append_local_repo_gitignore_guard(tmp.path()).unwrap();
        let gi = std::fs::read_to_string(tmp.path().join(".gitignore")).unwrap();
        let marker_count = gi.matches("VCO local-only repo guard").count();
        assert_eq!(marker_count, 1, "guard block must be idempotent, got {marker_count}");
    }
}
