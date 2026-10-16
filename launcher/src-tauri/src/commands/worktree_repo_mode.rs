// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! Per-project `worktree_repo_mode` — GUI-only tri-state setting backing the
//! subagent-git modal (v0.2.71 Track T-WT).
//!
//! ## What this is
//! When a project's workspace ROOT is not inside any git repo, agents that
//! declare `isolation: worktree` get no isolated worktree: VCO's WorktreeCreate
//! hook (`worktree-guard`, since v0.2.102 W6-Q) returns the folder itself and
//! the agent runs in place with isolation OFF. (A repo with no commits yet is
//! fine — the hook detaches the worktree at a snapshot commit.) The levers
//! are: detect/use an enclosing repo, `git init` a local-only repo (automatic
//! at add / bundle update since v0.2.103 when nothing pre-exists), connect an
//! existing repo, or opt out. The subagent-git modal surfaces the choices
//! that remain the user's; this setting records the decision per-project.
//!
//! Five modes (a string, NOT a bool):
//!   * `"use_existing"` — an enclosing repo was detected (root or a parent
//!     has `.git`); the harness walks up to it, isolation already works.
//!     Recorded automatically when detection succeeds; the user never has
//!     to click it.
//!   * `"local_init"` — a local-only `git init` was run at the workspace
//!     root: automatically at project add / bundle update when the folder
//!     held no repo at all (owner ruling 2026-10-14, see
//!     `ensure_project_local_repo`), or by the user from the modal.
//!   * `"no_repo"` — user opted out; subagents run in the shared cwd (no
//!     isolation). The enforcement (frontmatter strip) is owned by the
//!     project_init / install-bundle flow keyed off this setting — NOT by
//!     this command, which only persists the choice.
//!   * `"use_existing_remote"` — v0.2.91 (#30) "Connect an existing repo",
//!     remote-URL arm: the repo-less root was `git init`ed (main branch) +
//!     `origin` added + fetched — NEVER checked out/merged over existing
//!     content (see `attach_existing_repo_remote`). Requires a `source`
//!     (the remote URL), persisted under `worktree_repo_source`.
//!   * `"use_existing_at"` — v0.2.91 (#30) "Connect an existing repo",
//!     local-folder arm: the user's code already lives in a repo at a
//!     DIFFERENT path (a nested repo, or a browsed folder). Nothing on
//!     disk is mutated; the resolved repo toplevel is recorded as the
//!     `source` under `worktree_repo_source`. NOTE (M7): the root stays
//!     repo-less, so harness worktree isolation remains UNAVAILABLE — the
//!     record is documentation, not enforcement, and the modal's copy +
//!     toast state this explicitly.
//!
//! The `worktree_repo_source` companion key exists only for the two
//! source-bearing modes (the setter deletes it for the other three). It is
//! deliberately write-only today — nothing re-reads it because the modal
//! never re-shows once a mode is recorded; it is the durable record for DB
//! inspection and any future enforcement surface.
//!
//! ## GUI-only, NOT hub-resolved
//! Deliberately stored in `module_settings` (module_id =
//! "orchestrator-core") and read back only inside the launcher process (the
//! page probe `plan_project_git_repo` and the add/update-time
//! `ensure_project_local_repo`). It is NOT
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

/// Module id under which the orchestrator's own per-project settings live.
const MODULE_ID: &str = "orchestrator-core";

/// Setting key for the worktree-repo mode.
const SETTING_KEY: &str = "worktree_repo_mode";

/// Setting key for the connect-existing source (v0.2.91 #30): the remote
/// URL (`use_existing_remote`) or the resolved local repo toplevel
/// (`use_existing_at`). Present ONLY while the mode is source-bearing.
const SOURCE_SETTING_KEY: &str = "worktree_repo_source";

/// The five valid modes. Any other value is rejected by the setter so a
/// GUI bug can't persist garbage the modal then can't interpret. The Python
/// reader `vco_lib/local_repo_pending.py` mirrors this set (and SETTING_KEY /
/// MODULE_ID); both sides pin tests/fixtures/worktree_repo_mode_parity.json.
const VALID_MODES: [&str; 5] = [
    "use_existing",
    "local_init",
    "no_repo",
    "use_existing_at",
    "use_existing_remote",
];

/// The modes that carry a `source` (v0.2.91 #30). Subset of `VALID_MODES`
/// (pinned by test).
const SOURCE_REQUIRED_MODES: [&str; 2] = ["use_existing_at", "use_existing_remote"];

/// Pure mode/source contract check, extracted so it is unit-testable
/// without a `State<Db>`:
///   * mode must be one of `VALID_MODES`;
///   * a source-bearing mode REQUIRES a non-empty source;
///   * a sourceless mode must NOT carry a source (a GUI bug — refusing
///     beats persisting half a contract).
fn validate_mode_and_source(mode: &str, source: Option<&str>) -> Result<(), String> {
    if !VALID_MODES.contains(&mode) {
        return Err(format!(
            "set_worktree_repo_mode: invalid mode '{}' (expected one of {:?})",
            mode, VALID_MODES
        ));
    }
    let src = source.map(str::trim).filter(|s| !s.is_empty());
    if SOURCE_REQUIRED_MODES.contains(&mode) {
        if src.is_none() {
            return Err(format!(
                "set_worktree_repo_mode: mode '{}' requires a source (the remote URL or the local repo path)",
                mode
            ));
        }
    } else if src.is_some() {
        return Err(format!(
            "set_worktree_repo_mode: mode '{}' does not take a source",
            mode
        ));
    }
    Ok(())
}

/// Persist the per-project worktree-repo mode (+ its source, for the two
/// connect-existing modes).
///
/// Rejects an empty `project_id` (caller bug, not a soft-fail) and any
/// mode/source combination outside the contract (`validate_mode_and_source`).
/// `source` is optional at the wire level so pre-#30 callers
/// (`invoke('set_worktree_repo_mode', { projectId, mode })`) are unchanged.
/// For sourceless modes any stale `worktree_repo_source` row is deleted
/// (idempotent) so the DB never carries a source that contradicts the mode.
#[command]
pub async fn set_worktree_repo_mode(
    project_id: String,
    mode: String,
    source: Option<String>,
    db: State<'_, Db>,
) -> Result<(), String> {
    if project_id.is_empty() {
        return Err("set_worktree_repo_mode: project_id required".into());
    }
    validate_mode_and_source(&mode, source.as_deref())?;
    let trimmed_source = source.map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
    db.set_setting(&project_id, MODULE_ID, SETTING_KEY, &Value::String(mode))?;
    match trimmed_source {
        Some(src) => db.set_setting(
            &project_id,
            MODULE_ID,
            SOURCE_SETTING_KEY,
            &Value::String(src),
        ),
        None => db.delete_setting(&project_id, MODULE_ID, SOURCE_SETTING_KEY),
    }
}

/// Read back the per-project worktree-repo mode.
///
/// Returns `None` when no choice has been recorded yet. A non-string or
/// unknown stored value is coerced to `None` (defensive: never act on a value
/// outside the mode contract).
///
/// v0.2.103 (plan item 17): this was the `get_worktree_repo_mode` Tauri
/// command, whose only caller was the project page's modal gate. That gate
/// now asks `plan_project_git_repo` (which reports a recorded mode as
/// `"decided"`), so the command was retired from `generate_handler!` as
/// superseded; its logic lives on here, read by `plan_repo`.
fn recorded_mode(db: &Db, project_id: &str) -> Result<Option<String>, String> {
    let stored = db
        .get_setting(project_id, MODULE_ID, SETTING_KEY)?
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
    let out = run_git(root, &["rev-parse", "--show-toplevel"]).await;
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
/// choice, and the automatic add/update step `ensure_project_local_repo`).
/// SAFETY GUARDS — this MUTATES the user's filesystem, so it refuses
/// anything ambiguous:
///   1. REFUSE if the root is ALREADY inside a git repo (self or a parent) —
///      `git init` there would create a nested/duplicate repo or be a no-op on
///      an existing `.git`. The modal only offers this when detection said
///      NOT inside a repo, but we re-check here (TOCTOU + defense-in-depth).
///   2. Init with `--initial-branch=main`, NO remote (local-only, never pushed).
///   3. Append a guard block to the root `.gitignore` so `git add -A` does NOT
///      swallow NESTED repos (e.g. the `Code/python/<app>/` shape): every
///      nested git repo found under the root (bounded-depth descendant walk,
///      not just immediate children) is ignored, plus the VCO runtime paths.
///      We do NOT auto-commit — the repo starts empty so the user controls what
///      (if anything) they track.
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
    let init = run_git(root, &["init", "--initial-branch=main"]).await?;
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
        tracing::warn!(
            "[worktree_repo_mode] create_local_project_repo: gitignore guard append failed: {}",
            e
        );
    }
    Ok(())
}

/// Append a guard block to `<root>/.gitignore` that ignores (a) every nested
/// git repo found under `root` (so a stray `git add -A` can't swallow a nested
/// repo like `Code/python/app/`), and (b) the VCO runtime paths. Idempotent:
/// skips if our marker is already present.
///
/// v0.2.71 (MEDIUM-1 widening): the scan walks DESCENDANTS up to
/// `MAX_NESTED_SCAN_DEPTH` levels (not just immediate children), so a repo at
/// `Code/python/app/.git` (two levels down under a non-repo `Code/`) is also
/// ignored — matching the guard's stated intent "don't absorb nested repos".
/// The walk is bounded (depth cap + we never descend INTO a discovered nested
/// repo) so it can't blow up on a deep tree. Each nested repo is written as a
/// root-anchored path (`/Code/python/app/`) relative to `root`.
fn append_local_repo_gitignore_guard(root: &Path) -> std::io::Result<()> {
    use std::io::Write;
    // The repo is created either by the subagent-git modal or automatically
    // at project add / bundle update (`ensure_project_local_repo`), so the
    // written marker names neither. Idempotency matches the shared PREFIX,
    // which also covers the pre-v0.2.103 marker text
    // "(created by the subagent-git modal)" already present in older repos.
    const MARKER_PREFIX: &str = "# --- VCO local-only repo guard";
    const MARKER: &str = "# --- VCO local-only repo guard (created by VCO) ---";
    let gi = root.join(".gitignore");
    if let Ok(existing) = std::fs::read_to_string(&gi) {
        if existing.contains(MARKER_PREFIX) {
            return Ok(());
        }
    }
    // Find nested git repos under root (bounded-depth descendant walk).
    let nested = find_nested_git_repos(root);
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
    for p in [
        ".claude/state/",
        ".claude/worktrees/",
        ".claude/logs/",
        ".claude/backups/",
    ] {
        writeln!(f, "{}", p)?;
    }
    Ok(())
}

/// Max directory depth the nested-repo scan descends from `root` (1 = immediate
/// children). 6 covers the documented deep-nesting shapes (e.g.
/// `Code/python/<app>/`) without risk of walking an unboundedly deep tree.
const MAX_NESTED_SCAN_DEPTH: usize = 6;

/// Collect every nested git repo under `root`, as forward-slash paths RELATIVE
/// to `root` (e.g. `Code/python/app`). Bounded: descends at most
/// `MAX_NESTED_SCAN_DEPTH` levels and NEVER descends into a discovered repo
/// (a repo's own subdirs are its business, and a git repo can't be nested
/// inside another tracked repo without a gitlink anyway). Best-effort: an
/// unreadable dir is skipped, never fatal.
fn find_nested_git_repos(root: &Path) -> Vec<String> {
    let mut found: Vec<String> = Vec::new();
    // Stack of (absolute dir, depth). Start with root's children at depth 1.
    let mut stack: Vec<(std::path::PathBuf, usize)> = vec![(root.to_path_buf(), 0)];
    while let Some((dir, depth)) = stack.pop() {
        if depth >= MAX_NESTED_SCAN_DEPTH {
            continue;
        }
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for e in entries.flatten() {
            let p = e.path();
            if !p.is_dir() {
                continue;
            }
            // Skip symlinked dirs — following them could re-walk the tree (or
            // an out-of-tree target) redundantly. The depth cap already bounds
            // any cycle, but not descending symlinks is cleaner + avoids
            // ignoring a repo by a path that isn't really under `root`.
            if e.file_type().map(|t| t.is_symlink()).unwrap_or(false) {
                continue;
            }
            if p.join(".git").exists() {
                // A nested repo — record it, do NOT descend into it.
                if let Ok(rel) = p.strip_prefix(root) {
                    let rel_str = rel.to_string_lossy().replace('\\', "/");
                    if !rel_str.is_empty() {
                        found.push(rel_str);
                    }
                }
            } else {
                // Plain dir — descend (bounded by the depth cap).
                stack.push((p, depth + 1));
            }
        }
    }
    found
}

// ─── v0.2.91 (#30) — "Connect an existing repo" ─────────────────────────────

// v0.2.92 WP-13: this module's private `run_git` MOVED to
// `commands::git_cmd::run_git_raw` — same contract (`Err` is a SPAWN failure
// only; a non-zero git exit comes back as `Ok(output)` for the caller to
// judge), one home. Imported under the old local name so the call sites in
// this file did not churn.
use crate::commands::git_cmd::run_git_raw as run_git;

/// Shape-validate a git remote URL. Accepted shapes (and ONLY these — a
/// plain local path belongs in the local-folder arm):
///   * scheme+host+path — `http|https|ssh|git` `://` non-empty host segment
///     `/` non-empty path.
///   * scp-like — `user@host:path`, all three parts non-empty.
/// Whitespace anywhere (after trimming) rejects. A leading `-` rejects
/// (M8): such a candidate would reach git as an OPTION
/// (`git remote add origin -t@host:path` parses `-t` as a flag), so it
/// must never pass shape validation — and the refusal happens BEFORE any
/// git process (init included) runs.
///
/// MUST MATCH `isValidGitRemoteUrl` in
/// launcher/src/lib/components/subagent-git-repo-logic.ts (the UI mirror
/// for live feedback; this Rust side is authoritative). The parity
/// contract is executable, not comment-only: BOTH test suites iterate the
/// shared fixture tests/fixtures/git_remote_url_parity.json (see
/// `url_validation_parity_table` below).
fn is_valid_git_remote_url(url: &str) -> bool {
    let u = url.trim();
    if u.is_empty() || u.chars().any(char::is_whitespace) || u.starts_with('-') {
        return false;
    }
    if let Some(idx) = u.find("://") {
        let scheme = &u[..idx];
        let rest = &u[idx + 3..];
        if !matches!(scheme, "http" | "https" | "ssh" | "git") {
            return false;
        }
        return match rest.split_once('/') {
            Some((host, path)) => !host.is_empty() && !path.is_empty(),
            None => false,
        };
    }
    let Some((user, rest)) = u.split_once('@') else {
        return false;
    };
    if user.is_empty() {
        return false;
    }
    match rest.split_once(':') {
        Some((host, path)) => !host.is_empty() && !path.is_empty(),
        None => false,
    }
}

/// Result of the remote-URL connect arm. `fetched=false` is NOT an error:
/// the init + remote stand (deliberately kept — see the fetch-failure note
/// on `attach_remote_at`), and `message` carries the honest status the UI
/// surfaces either way.
#[derive(Debug, serde::Serialize)]
pub struct AttachRemoteOutcome {
    pub fetched: bool,
    pub message: String,
}

/// Connect a REMOTE repo to a repo-less project root (v0.2.91 #30).
///
/// DESTRUCTIVE-EDGE RULE (non-negotiable): this runs `git init` (main
/// branch) + `git remote add origin <url>` + `git fetch origin` ONLY —
/// NEVER checkout/merge/pull over existing content. The working tree is
/// left byte-identical; the success copy tells the user to reconcile
/// manually. Guards:
///   1. REFUSE when the root is already inside a repo (self or parent) —
///      connect is only for the repo-less case (leave-alone).
///   2. REFUSE a URL outside the accepted shapes (`is_valid_git_remote_url`).
#[command]
pub async fn attach_existing_repo_remote(
    project_root: String,
    remote_url: String,
) -> Result<AttachRemoteOutcome, String> {
    if project_root.trim().is_empty() {
        return Err("attach_existing_repo_remote: project_root required".into());
    }
    let url = remote_url.trim().to_string();
    if !is_valid_git_remote_url(&url) {
        return Err(format!(
            "attach_existing_repo_remote: '{}' is not a usable git remote URL \
             (expected https://host/path, ssh://host/path, git://host/path or \
             user@host:path). For a repo in a local folder, use the folder \
             option instead.",
            url
        ));
    }
    let root = Path::new(&project_root);
    if !root.is_dir() {
        return Err(format!(
            "attach_existing_repo_remote: '{}' is not a directory",
            project_root
        ));
    }
    let det = detect_project_git_repo(project_root.clone()).await?;
    if det.inside_repo {
        return Err(format!(
            "attach_existing_repo_remote: '{}' is already inside a git repo ({}). \
             Connect is only for a repo-less root — the existing repo already \
             provides worktree isolation.",
            project_root,
            det.toplevel.as_deref().unwrap_or("unknown toplevel")
        ));
    }
    attach_remote_at(root, &url).await
}

/// The init + remote-add + fetch mechanics, shape-agnostic (the command
/// above owns URL validation; tests drive this with a local bare-repo path
/// to prove the mechanics without network).
///
/// Fetch-failure semantics (chosen, documented, tested): the fresh init +
/// recorded remote are KEPT and reported honestly (`fetched=false`) —
/// never rolled back. Rolling back would delete a `.git` from the user's
/// tree (against the destructive-edge rule), and the recorded remote stays
/// fully useful: isolation now works and `git fetch origin` can simply be
/// retried.
async fn attach_remote_at(root: &Path, remote_url: &str) -> Result<AttachRemoteOutcome, String> {
    let init = run_git(root, &["init", "--initial-branch=main"]).await?;
    if !init.status.success() {
        return Err(format!(
            "git init failed: {}",
            String::from_utf8_lossy(&init.stderr).trim()
        ));
    }
    let add = run_git(root, &["remote", "add", "origin", remote_url]).await?;
    if !add.status.success() {
        // The fresh (empty) init stays — we never delete a .git, and a
        // fresh init with no remote is exactly the harmless `local_init`
        // shape. Honest error so the user knows what exists.
        return Err(format!(
            "git remote add failed: {} (a fresh empty repo was initialised at \
             the root; none of your files were changed)",
            String::from_utf8_lossy(&add.stderr).trim()
        ));
    }
    let fetch = run_git(root, &["fetch", "origin"]).await?;
    if fetch.status.success() {
        Ok(AttachRemoteOutcome {
            fetched: true,
            message: format!(
                "Connected '{}' as origin and fetched it. Your files were NOT \
                 merged or checked out over — review `git status` and \
                 `git log origin --oneline`, then reconcile manually (e.g. \
                 commit your local files first, then merge the remote branch).",
                remote_url
            ),
        })
    } else {
        Ok(AttachRemoteOutcome {
            fetched: false,
            message: format!(
                "Initialised the repo and recorded '{}' as origin, but \
                 `git fetch` failed: {}. None of your files were changed and \
                 the connection stays recorded — check the URL/network and run \
                 `git fetch origin` manually.",
                remote_url,
                String::from_utf8_lossy(&fetch.stderr).trim()
            ),
        })
    }
}

/// Connect an existing LOCAL repo (a detected nested candidate or a
/// browsed folder) to a repo-less project root (v0.2.91 #30). Mutates
/// NOTHING — validates that `repo_path` really is inside a git repo and
/// returns the resolved toplevel, which the caller persists as the
/// `use_existing_at` source. The root stays repo-less afterwards, so
/// worktree isolation remains unavailable at this root (M7: the UI copy
/// says so — this is a durable record, not an enforcement). Refuses when
/// the project root is already inside a repo (same leave-alone guard as
/// the remote arm — the modal only offers connect for the repo-less case).
#[command]
pub async fn attach_existing_repo_local(
    project_root: String,
    repo_path: String,
) -> Result<String, String> {
    if project_root.trim().is_empty() {
        return Err("attach_existing_repo_local: project_root required".into());
    }
    if repo_path.trim().is_empty() {
        return Err("attach_existing_repo_local: repo_path required".into());
    }
    let det = detect_project_git_repo(project_root.clone()).await?;
    if det.inside_repo {
        return Err(format!(
            "attach_existing_repo_local: '{}' is already inside a git repo ({}). \
             Connect is only for a repo-less root.",
            project_root,
            det.toplevel.as_deref().unwrap_or("unknown toplevel")
        ));
    }
    let repo = Path::new(&repo_path);
    if !repo.is_dir() {
        return Err(format!(
            "attach_existing_repo_local: '{}' is not a directory",
            repo_path
        ));
    }
    let out = run_git(repo, &["rev-parse", "--show-toplevel"]).await?;
    if !out.status.success() {
        return Err(format!(
            "attach_existing_repo_local: '{}' is not inside a git repository \
             ({})",
            repo_path,
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    let top = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if top.is_empty() {
        return Err(format!(
            "attach_existing_repo_local: could not resolve the repo toplevel \
             for '{}'",
            repo_path
        ));
    }
    Ok(top)
}

/// A nested repo detected under the project root — a candidate for the
/// connect-existing dropdown. `rel_path` is forward-slash relative (for
/// display), `abs_path` is what the attach command consumes.
#[derive(serde::Serialize)]
pub struct NestedRepoCandidate {
    pub rel_path: String,
    pub abs_path: String,
}

/// Enumerate nested git repos under the project root for the modal's
/// candidate dropdown. REUSES `find_nested_git_repos` — the same walk the
/// local_init gitignore guard uses — so the two surfaces can never drift.
/// Soft-fails to an empty list for a missing root (the dropdown just
/// doesn't render).
#[command]
pub async fn list_nested_repo_candidates(
    project_root: String,
) -> Result<Vec<NestedRepoCandidate>, String> {
    if project_root.trim().is_empty() {
        return Err("list_nested_repo_candidates: project_root required".into());
    }
    let root = Path::new(&project_root);
    if !root.is_dir() {
        return Ok(Vec::new());
    }
    Ok(find_nested_git_repos(root)
        .into_iter()
        .map(|rel| {
            // rel is forward-slash; rebuild the abs path segment-by-segment
            // so the separator is native on every OS.
            let abs = rel
                .split('/')
                .fold(root.to_path_buf(), |p, seg| p.join(seg));
            NestedRepoCandidate {
                rel_path: rel,
                abs_path: abs.to_string_lossy().to_string(),
            }
        })
        .collect())
}

/// Root entries that a freshly-scaffolded VCO project may contain. A root
/// whose entries ALL come from this set (or that is empty) counts as
/// "created empty — no user files yet", which drives the modal's honest
/// "this folder was created empty" copy. `.env` is included because the
/// standard (non-safe) add merges VCO config into a root `.env`;
/// `.env.vco.reference` is the safe-add sidecar.
const SCAFFOLD_ONLY_ENTRIES: [&str; 8] = [
    ".claude",
    "CLAUDE.md",
    "MEMORY.md",
    "knowledge",
    "infrastructure",
    ".vscode",
    ".env.vco.reference",
    ".env",
];

/// Cheap probe: does the project root hold ONLY VCO scaffolding (no user
/// files)? One `read_dir` of the root, no recursion. Soft-fails to `false`
/// (the copy is informational; never block or overclaim on an unreadable
/// root).
#[command]
pub async fn detect_scaffold_only_root(project_root: String) -> Result<bool, String> {
    if project_root.trim().is_empty() {
        return Err("detect_scaffold_only_root: project_root required".into());
    }
    let root = Path::new(&project_root);
    if !root.is_dir() {
        return Ok(false);
    }
    let entries = match std::fs::read_dir(root) {
        Ok(e) => e,
        Err(_) => return Ok(false),
    };
    for e in entries.flatten() {
        let name = e.file_name();
        let name = name.to_string_lossy();
        if !SCAFFOLD_ONLY_ENTRIES.contains(&name.as_ref()) {
            return Ok(false);
        }
    }
    Ok(true)
}

// ─── v0.2.103 (plan item 17) — automatic local repo at add / update ────────
//
// Owner ruling 2026-10-14: "no need for approval if there's no pre-existing
// repo in the project, just create a local one when the project gets added
// to VCO". The decision lives HERE, once, and is consumed by three callers:
//   * `create_project_v2` (synchronous phase, before the bundle install so
//     the bundle's `.git/info/exclude` pass covers the fresh repo);
//   * `update_project_v2` (before `install-bundle --update`, same reason) —
//     the catch-up for projects registered before this release;
//   * the project page, through `plan_project_git_repo`, which opens the
//     subagent-git modal ONLY for the real-choice case.
//
// What counts as "a pre-existing repo in the project":
//   * the folder IS a repo, or sits inside one (`git rev-parse` succeeds) —
//     never touched; `use_existing` is recorded (the harness walks up to it);
//   * the folder is repo-less but HOLDS nested repos (`Code/python/<app>/.git`)
//     — the user's code already lives in a repo, so wrapping it in a new root
//     repo vs. pointing at the nested one is a real choice: nothing is done
//     and the modal asks.
// Only a folder with no repo anywhere around or under it is initialised.
// A recorded mode is always respected (`no_repo` included), which also makes
// the update-time catch-up run at most once per project.

/// What the add/update-time step found and did. Each variant maps to at most
/// one user notice (info) or one warning (problem) — see `notice`/`warning`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum AutoLocalRepoOutcome {
    /// A mode was already recorded; nothing probed or changed.
    AlreadyDecided { mode: String },
    /// The folder is, or is inside, an existing repo. Untouched;
    /// `use_existing` recorded.
    ExistingRepo { toplevel: String },
    /// Repo-less folder holding nested repos: the modal asks. Nothing done.
    NeedsChoice { nested: Vec<String> },
    /// A local-only repo was created (main branch, no remote, no commit).
    /// `record_error` is set when persisting `local_init` failed — the repo
    /// still stands (and the next probe records `use_existing` for it).
    Created { record_error: Option<String> },
    /// `git` could not be run. Nothing created or recorded, so the next
    /// bundle update retries once git is installed.
    GitUnavailable { detail: String },
    /// `git init` failed. Nothing recorded (retried on the next update).
    Failed { detail: String },
}

/// The user-facing notice text for a created repo. Pinned by tests and the
/// GUI wiring test, so the wording the owner asked for cannot drift.
pub(crate) const CREATED_LOCAL_REPO_NOTICE: &str =
    "Created a local git repository (no remote, nothing committed). \
     Subagents now work in isolated worktrees (snapshots of your files until your first commit).";

impl AutoLocalRepoOutcome {
    /// Informational notice for the GUI (success/info toast), if any.
    pub(crate) fn notice(&self) -> Option<String> {
        match self {
            AutoLocalRepoOutcome::Created { .. } => Some(CREATED_LOCAL_REPO_NOTICE.to_string()),
            _ => None,
        }
    }

    /// A problem the user should see (warning toast), if any. A missing git
    /// or a failed init never fails the add/update itself.
    pub(crate) fn warning(&self) -> Option<String> {
        match self {
            AutoLocalRepoOutcome::GitUnavailable { detail } => Some(format!(
                "git could not be run ({}), so VCO did not create a local git repository \
                 for this project: subagents that use worktree isolation run in the \
                 project folder itself, without isolation, until there is one. Install git, then run Update bundle — VCO creates \
                 the repository then.",
                detail
            )),
            AutoLocalRepoOutcome::Failed { detail } => Some(format!(
                "Creating a local git repository for this project failed: {}. \
                 The project was set up anyway; Update bundle retries the step.",
                detail
            )),
            AutoLocalRepoOutcome::Created {
                record_error: Some(e),
            } => Some(format!(
                "A local git repository was created, but recording that choice failed: {}.",
                e
            )),
            _ => None,
        }
    }
}

/// The side-effect-free part of the decision, shared by the add/update step
/// and the page probe.
#[derive(Debug, Clone, PartialEq, Eq)]
enum RepoPlan {
    Decided(String),
    Existing(String),
    NeedsChoice(Vec<String>),
    AutoInit,
    GitUnavailable(String),
}

/// `Ok(())` when a `git` binary answers `--version`; the error text otherwise.
async fn git_runnable(root: &Path) -> Result<(), String> {
    match run_git(root, &["--version"]).await {
        Ok(o) if o.status.success() => Ok(()),
        Ok(o) => Err(format!(
            "`git --version` exited with {}",
            o.status.code().map_or("a signal".to_string(), |c| c.to_string())
        )),
        Err(e) => Err(e),
    }
}

async fn plan_repo(db: &Db, project_id: &str, root: &Path) -> Result<RepoPlan, String> {
    if let Some(mode) = recorded_mode(db, project_id)? {
        return Ok(RepoPlan::Decided(mode));
    }
    if !root.is_dir() {
        return Err(format!("'{}' is not a directory", root.display()));
    }
    // Probe git FIRST: `detect_project_git_repo` soft-fails a spawn error to
    // "not inside a repo", which would otherwise read as "init me".
    if let Err(e) = git_runnable(root).await {
        return Ok(RepoPlan::GitUnavailable(e));
    }
    let det = detect_project_git_repo(root.to_string_lossy().to_string()).await?;
    if det.inside_repo {
        return Ok(RepoPlan::Existing(det.toplevel.unwrap_or_default()));
    }
    let nested = find_nested_git_repos(root);
    if !nested.is_empty() {
        return Ok(RepoPlan::NeedsChoice(nested));
    }
    Ok(RepoPlan::AutoInit)
}

/// Record `use_existing` for a folder found inside a repo. Best-effort: a
/// failed write only means the next probe repeats the (read-only) detection.
fn record_use_existing(db: &Db, project_id: &str) {
    if let Err(e) = db.set_setting(
        project_id,
        MODULE_ID,
        SETTING_KEY,
        &Value::String("use_existing".into()),
    ) {
        tracing::warn!(
            "[worktree_repo_mode] could not record use_existing for {}: {}",
            project_id,
            e
        );
    }
}

/// Add/update-time step: create a local-only repo when the project folder has
/// no repo anywhere around or under it (see the section comment above).
/// Never returns an error — every condition is an outcome the caller turns
/// into a notice or a warning, so a project add/update never fails on git.
pub(crate) async fn ensure_project_local_repo(
    db: &Db,
    project_id: &str,
    root: &Path,
) -> AutoLocalRepoOutcome {
    let plan = match plan_repo(db, project_id, root).await {
        Ok(p) => p,
        Err(e) => return AutoLocalRepoOutcome::Failed { detail: e },
    };
    match plan {
        RepoPlan::Decided(mode) => AutoLocalRepoOutcome::AlreadyDecided { mode },
        RepoPlan::GitUnavailable(detail) => {
            tracing::warn!(
                "[worktree_repo_mode] {}: git unavailable, no local repo created: {}",
                root.display(),
                detail
            );
            AutoLocalRepoOutcome::GitUnavailable { detail }
        }
        RepoPlan::Existing(toplevel) => {
            record_use_existing(db, project_id);
            AutoLocalRepoOutcome::ExistingRepo { toplevel }
        }
        RepoPlan::NeedsChoice(nested) => {
            tracing::info!(
                "[worktree_repo_mode] {}: repo-less but holds nested repos {:?}; leaving the choice to the user",
                root.display(),
                nested
            );
            AutoLocalRepoOutcome::NeedsChoice { nested }
        }
        RepoPlan::AutoInit => {
            if let Err(e) = create_local_project_repo(root.to_string_lossy().to_string()).await {
                tracing::warn!(
                    "[worktree_repo_mode] {}: automatic local repo creation failed: {}",
                    root.display(),
                    e
                );
                return AutoLocalRepoOutcome::Failed { detail: e };
            }
            tracing::info!(
                "[worktree_repo_mode] {}: created a local-only git repository (main, no remote, no commit)",
                root.display()
            );
            let record_error = db
                .set_setting(
                    project_id,
                    MODULE_ID,
                    SETTING_KEY,
                    &Value::String("local_init".into()),
                )
                .err();
            let _ = db.audit(
                "project_git_local_init",
                Some(project_id),
                None,
                &serde_json::json!({
                    "folder": root.display().to_string(),
                    "branch": "main",
                    "remote": null,
                    "committed": false,
                }),
            );
            AutoLocalRepoOutcome::Created { record_error }
        }
    }
}

/// Run [`ensure_project_local_repo`] and route its outcome into the caller's
/// notice/warning lists — the ONE adapter `create_project_v2` and
/// `update_project_v2` both call, so add and update report it identically.
pub(crate) async fn collect_local_repo_outcome(
    db: &Db,
    project_id: &str,
    root: &Path,
    notices: &mut Vec<String>,
    warnings: &mut Vec<String>,
) {
    let outcome = ensure_project_local_repo(db, project_id, root).await;
    if let Some(n) = outcome.notice() {
        notices.push(n);
    }
    if let Some(w) = outcome.warning() {
        warnings.push(w);
    }
}

/// Page probe (no filesystem mutation): which subagent-git state is this
/// project in? Returns one of `"decided"`, `"existing"` (and records
/// `use_existing`, as the page always did), `"needs_choice"` (the ONLY state
/// in which the page opens the modal), `"auto_init_pending"` (repo-less with
/// nothing nested — the next bundle update creates the repo; no prompt) or
/// `"git_unavailable"`. The string contract MUST MATCH `GitRepoPlan` in
/// launcher/src/lib/components/subagent-git-repo-logic.ts.
#[command]
pub async fn plan_project_git_repo(
    project_id: String,
    project_root: String,
    db: State<'_, Db>,
) -> Result<String, String> {
    if project_id.is_empty() {
        return Err("plan_project_git_repo: project_id required".into());
    }
    if project_root.trim().is_empty() {
        return Err("plan_project_git_repo: project_root required".into());
    }
    plan_label(&db, &project_id, Path::new(&project_root)).await
}

async fn plan_label(db: &Db, project_id: &str, root: &Path) -> Result<String, String> {
    Ok(match plan_repo(db, project_id, root).await? {
        RepoPlan::Decided(_) => "decided",
        RepoPlan::Existing(_) => {
            record_use_existing(db, project_id);
            "existing"
        }
        RepoPlan::NeedsChoice(_) => "needs_choice",
        RepoPlan::AutoInit => "auto_init_pending",
        RepoPlan::GitUnavailable(_) => "git_unavailable",
    }
    .to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_modes_are_exactly_the_five_modes() {
        // Pins the mode contract so a future edit that adds/removes a mode
        // must consciously update the const, this test, the modal's option
        // set AND every match/read site (an unhandled arm is the recurring
        // bug class here). v0.2.91 #30 added the two connect-existing modes.
        assert_eq!(VALID_MODES.len(), 5);
        assert!(VALID_MODES.contains(&"use_existing"));
        assert!(VALID_MODES.contains(&"local_init"));
        assert!(VALID_MODES.contains(&"no_repo"));
        assert!(VALID_MODES.contains(&"use_existing_at"));
        assert!(VALID_MODES.contains(&"use_existing_remote"));
        // The source-bearing set is exactly the two connect modes, and a
        // subset of VALID_MODES.
        assert_eq!(
            SOURCE_REQUIRED_MODES,
            ["use_existing_at", "use_existing_remote"]
        );
        for m in SOURCE_REQUIRED_MODES {
            assert!(VALID_MODES.contains(&m));
        }
    }

    #[test]
    fn mode_source_contract() {
        // Legacy sourceless modes: fine without a source…
        for m in ["use_existing", "local_init", "no_repo"] {
            assert!(validate_mode_and_source(m, None).is_ok());
            // …and REFUSED with one (GUI bug, never persist half a contract).
            assert!(validate_mode_and_source(m, Some("/some/path")).is_err());
        }
        // Connect modes REQUIRE a non-empty source.
        for m in SOURCE_REQUIRED_MODES {
            assert!(validate_mode_and_source(m, Some("/repo/or/url")).is_ok());
            assert!(validate_mode_and_source(m, None).is_err());
            assert!(validate_mode_and_source(m, Some("   ")).is_err());
        }
        // Unknown mode always refused.
        assert!(validate_mode_and_source("bogus", None).is_err());
        assert!(validate_mode_and_source("bogus", Some("x")).is_err());
    }

    // ── URL-validator parity (M5): the Rust side of the shared fixture
    // `tests/fixtures/git_remote_url_parity.json`. The vitest suite
    // subagent-git-repo-logic.test.ts consumes the SAME fixture for the
    // TS UI mirror — comment-only "MUST MATCH" parity is a fork risk, so
    // the contract is executable. Fixture path resolved from
    // `CARGO_MANIFEST_DIR` (= `launcher/src-tauri/`) → two parents up to
    // the repo root (same walk as tests/project_naming_parity.rs and
    // env_secrets_migrate.rs's parity loader).

    #[derive(serde::Deserialize)]
    struct UrlParityFixture {
        #[serde(rename = "_format_version", default)]
        format_version: u32,
        case_count: usize,
        cases: Vec<(String, bool)>,
    }

    fn load_url_parity_fixture() -> UrlParityFixture {
        let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest_dir
            .parent() // <repo>/launcher
            .and_then(|p| p.parent()) // <repo>
            .expect("CARGO_MANIFEST_DIR doesn't have two parents — unexpected build layout");
        let fixture_path = repo_root
            .join("tests")
            .join("fixtures")
            .join("git_remote_url_parity.json");
        assert!(
            fixture_path.exists(),
            "Parity fixture missing: {} — shared with \
             launcher/src/lib/components/subagent-git-repo-logic.test.ts",
            fixture_path.display()
        );
        let raw = std::fs::read_to_string(&fixture_path)
            .unwrap_or_else(|e| panic!("read {}: {}", fixture_path.display(), e));
        let fix: UrlParityFixture = serde_json::from_str(&raw)
            .unwrap_or_else(|e| panic!("parse {}: {}", fixture_path.display(), e));
        assert_eq!(
            fix.format_version, 1,
            "Fixture _format_version != 1 — coordinate the bump with the TS side"
        );
        // Silent-truncation guard: the fixture declares its own row count;
        // both suites assert it so a partial parse / accidental trim fails
        // loudly on BOTH sides.
        assert_eq!(
            fix.cases.len(),
            fix.case_count,
            "Fixture case_count ({}) != actual rows ({}) — update both in the same edit",
            fix.case_count,
            fix.cases.len()
        );
        assert!(!fix.cases.is_empty(), "Fixture has no cases");
        fix
    }

    #[test]
    fn url_validation_parity_table() {
        let fix = load_url_parity_fixture();
        // The M8 argument-injection rows must stay pinned: at least one
        // leading-dash candidate expected INVALID.
        assert!(
            fix.cases.iter().any(|(u, v)| u.starts_with('-') && !v),
            "fixture must keep the leading-dash (option-injection) rejection rows"
        );
        for (url, expected) in &fix.cases {
            assert_eq!(
                is_valid_git_remote_url(url),
                *expected,
                "verdict mismatch for {url:?}"
            );
        }
    }

    /// v0.2.103 (review R15): the mode contract is shared with the Python
    /// reader (`vco_lib/local_repo_pending.py`) through
    /// tests/fixtures/worktree_repo_mode_parity.json — a new or renamed mode
    /// fails here until the fixture says so, then fails the Python test
    /// (tests/test_local_repo_pending.py) until Python knows it.
    #[test]
    fn worktree_repo_mode_parity_fixture() {
        #[derive(serde::Deserialize)]
        struct ModeFixture {
            #[serde(rename = "_format_version")]
            format_version: u32,
            module_id: String,
            setting_key: String,
            valid_modes: Vec<String>,
        }
        let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let path = manifest_dir
            .parent()
            .and_then(|p| p.parent())
            .expect("two parents above CARGO_MANIFEST_DIR")
            .join("tests")
            .join("fixtures")
            .join("worktree_repo_mode_parity.json");
        let raw = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));
        let fix: ModeFixture = serde_json::from_str(&raw)
            .unwrap_or_else(|e| panic!("parse {}: {}", path.display(), e));
        assert_eq!(fix.format_version, 1);
        assert_eq!(fix.module_id, MODULE_ID);
        assert_eq!(fix.setting_key, SETTING_KEY);
        let mut ours: Vec<&str> = VALID_MODES.to_vec();
        let mut theirs: Vec<&str> = fix.valid_modes.iter().map(String::as_str).collect();
        ours.sort_unstable();
        theirs.sort_unstable();
        assert_eq!(ours, theirs, "VALID_MODES and the parity fixture diverged");
    }

    #[tokio::test]
    async fn attach_remote_rejects_leading_dash_before_any_git_runs() {
        // M8: a leading-dash "URL" would reach git as an OPTION
        // (`git remote add origin -t@host:path`). Shape validation must
        // refuse it BEFORE any git process runs — proven by the root
        // staying .git-less (git init is the FIRST git step of the attach
        // flow, so no .git == no git ran).
        let tmp = tempfile::tempdir().unwrap();
        for bad in ["-t@host.example:path", "--mirror=fetch@host.example:path"] {
            let res = attach_existing_repo_remote(
                tmp.path().to_string_lossy().to_string(),
                bad.into(),
            )
            .await;
            assert!(res.is_err(), "must refuse leading-dash candidate {bad:?}");
        }
        assert!(
            !tmp.path().join(".git").exists(),
            "refusal must happen before git init — no .git may exist"
        );
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
    async fn gitignore_guard_ignores_deeply_nested_repo() {
        // v0.2.71 MEDIUM-1: a repo TWO levels down (under a non-repo parent),
        // the `Code/python/app/.git` shape, must also be ignored — the
        // immediate-child-only scan missed it before the widening.
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let deep = tmp.path().join("Code").join("python").join("app");
        std::fs::create_dir_all(&deep).unwrap();
        std::process::Command::new("git")
            .args(["init", "--initial-branch=main"])
            .current_dir(&deep)
            .output()
            .unwrap();

        create_local_project_repo(tmp.path().to_string_lossy().to_string())
            .await
            .unwrap();

        let gi = std::fs::read_to_string(tmp.path().join(".gitignore")).unwrap();
        // Forward-slash, root-anchored, regardless of host path separator.
        assert!(
            gi.contains("/Code/python/app/"),
            "deep nested repo ignored: {gi}"
        );
    }

    #[test]
    fn find_nested_git_repos_does_not_descend_into_a_repo() {
        // A repo that itself contains a sub-repo: we record the OUTER repo and
        // do NOT descend into it (the inner one is the outer repo's concern).
        let tmp = tempfile::tempdir().unwrap();
        let outer = tmp.path().join("outer");
        let inner = outer.join("inner");
        std::fs::create_dir_all(&inner).unwrap();
        std::fs::create_dir_all(outer.join(".git")).unwrap();
        std::fs::create_dir_all(inner.join(".git")).unwrap();

        let found = find_nested_git_repos(tmp.path());
        assert!(found.contains(&"outer".to_string()), "outer recorded: {found:?}");
        assert!(
            !found.iter().any(|p| p.contains("inner")),
            "must NOT descend into a discovered repo: {found:?}"
        );
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

    // ── v0.2.91 (#30) — connect-existing tests ──────────────────────────

    /// A sentinel with non-trivial bytes; the attach flow must leave it
    /// byte-identical (the destructive-edge rule's proof obligation).
    const SENTINEL_BYTES: &[u8] = b"user data \xf0\x9f\x9a\x80 line1\nline2\r\n\x00binary tail";

    fn write_sentinel(root: &Path) -> std::path::PathBuf {
        let p = root.join("user-file.dat");
        std::fs::write(&p, SENTINEL_BYTES).unwrap();
        p
    }

    #[tokio::test]
    async fn attach_remote_act_init_remote_fetch_tree_untouched() {
        // ACT: repo-less root + reachable remote → init + origin recorded +
        // fetched, and the user's file survives byte-identical. Drives the
        // shape-agnostic inner fn with a LOCAL BARE repo path as the remote
        // so the fetch-success path is deterministic and offline (the
        // command wrapper's URL-shape gate is covered separately).
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let bare = tmp.path().join("remote-bare.git");
        std::fs::create_dir_all(&bare).unwrap();
        std::process::Command::new("git")
            .args(["init", "--bare", "--initial-branch=main"])
            .current_dir(&bare)
            .output()
            .unwrap();
        let root = tmp.path().join("project");
        std::fs::create_dir_all(&root).unwrap();
        let sentinel = write_sentinel(&root);

        let outcome = attach_remote_at(&root, &bare.to_string_lossy())
            .await
            .unwrap();
        assert!(outcome.fetched, "local bare remote must fetch: {}", outcome.message);
        assert!(root.join(".git").exists(), "init must have created .git");
        let remote = std::process::Command::new("git")
            .args(["remote", "get-url", "origin"])
            .current_dir(&root)
            .output()
            .unwrap();
        assert!(remote.status.success(), "origin must be recorded");
        // Tree untouched: sentinel byte-identical.
        assert_eq!(
            std::fs::read(&sentinel).unwrap(),
            SENTINEL_BYTES,
            "user file must survive byte-identical"
        );
        // Honest copy: the message must say the tree was not merged over.
        assert!(outcome.message.contains("NOT"), "message: {}", outcome.message);
    }

    #[tokio::test]
    async fn attach_remote_fetch_failure_keeps_init_and_remote_with_honest_status() {
        // Chosen + documented fetch-failure semantics: init + origin KEPT
        // (never rolled back — no deletes on the user's tree), fetched=false,
        // honest message. 127.0.0.1:1 refuses instantly — deterministic,
        // offline, and a VALID URL shape so it exercises the real command.
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let sentinel = write_sentinel(tmp.path());
        let url = "http://127.0.0.1:1/example/example-repo.git";

        let outcome =
            attach_existing_repo_remote(tmp.path().to_string_lossy().to_string(), url.into())
                .await
                .unwrap();
        assert!(!outcome.fetched, "fetch to a closed port must fail");
        assert!(tmp.path().join(".git").exists(), "init must be KEPT on fetch failure");
        let remote = std::process::Command::new("git")
            .args(["remote", "get-url", "origin"])
            .current_dir(tmp.path())
            .output()
            .unwrap();
        assert_eq!(
            String::from_utf8_lossy(&remote.stdout).trim(),
            url,
            "origin must stay recorded on fetch failure"
        );
        assert_eq!(std::fs::read(&sentinel).unwrap(), SENTINEL_BYTES);
        assert!(
            outcome.message.contains("git fetch"),
            "honest status must name the failed step: {}",
            outcome.message
        );
    }

    #[tokio::test]
    async fn attach_remote_refuses_when_root_already_inside_a_repo() {
        // LEAVE-ALONE: never init/remote-add inside an existing repo.
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        std::process::Command::new("git")
            .args(["init", "--initial-branch=main"])
            .current_dir(tmp.path())
            .output()
            .unwrap();
        let res = attach_existing_repo_remote(
            tmp.path().to_string_lossy().to_string(),
            "https://host.example/org/repo.git".into(),
        )
        .await;
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("already inside a git repo"));
    }

    #[tokio::test]
    async fn attach_remote_refuses_invalid_url_and_touches_nothing() {
        // LEAVE-ALONE: a rejected URL must not create a .git.
        let tmp = tempfile::tempdir().unwrap();
        for bad in ["not a url", "/plain/local/path", ""] {
            let res = attach_existing_repo_remote(
                tmp.path().to_string_lossy().to_string(),
                bad.into(),
            )
            .await;
            assert!(res.is_err(), "must refuse {bad:?}");
        }
        assert!(
            !tmp.path().join(".git").exists(),
            "refusals must leave the root untouched"
        );
    }

    #[tokio::test]
    async fn attach_local_resolves_toplevel_and_mutates_nothing() {
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let nested = tmp.path().join("nested_app");
        std::fs::create_dir_all(&nested).unwrap();
        std::process::Command::new("git")
            .args(["init", "--initial-branch=main"])
            .current_dir(&nested)
            .output()
            .unwrap();
        let sentinel = write_sentinel(tmp.path());

        let top = attach_existing_repo_local(
            tmp.path().to_string_lossy().to_string(),
            nested.to_string_lossy().to_string(),
        )
        .await
        .unwrap();
        // git prints the toplevel with forward slashes / resolved symlinks
        // (macOS /tmp → /private/tmp); canonicalize both sides to compare.
        assert_eq!(
            std::fs::canonicalize(&top).unwrap(),
            std::fs::canonicalize(&nested).unwrap(),
            "must resolve the nested repo's toplevel"
        );
        // No mutation anywhere: root stays repo-less, sentinel intact.
        assert!(!tmp.path().join(".git").exists());
        assert_eq!(std::fs::read(&sentinel).unwrap(), SENTINEL_BYTES);
    }

    #[tokio::test]
    async fn attach_local_refuses_non_repo_and_covered_root() {
        if !git_available() {
            return;
        }
        // A plain (non-repo) folder is refused.
        let tmp = tempfile::tempdir().unwrap();
        let plain = tmp.path().join("plain");
        std::fs::create_dir_all(&plain).unwrap();
        let res = attach_existing_repo_local(
            tmp.path().to_string_lossy().to_string(),
            plain.to_string_lossy().to_string(),
        )
        .await;
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("not inside a git repository"));
        // A missing folder is refused.
        let res = attach_existing_repo_local(
            tmp.path().to_string_lossy().to_string(),
            tmp.path().join("missing").to_string_lossy().to_string(),
        )
        .await;
        assert!(res.is_err());
        // LEAVE-ALONE: a root already inside a repo is refused (connect is
        // only offered for the repo-less case; re-check here is TOCTOU
        // defense like create_local_project_repo's).
        let repo_root = tempfile::tempdir().unwrap();
        std::process::Command::new("git")
            .args(["init", "--initial-branch=main"])
            .current_dir(repo_root.path())
            .output()
            .unwrap();
        let inner = repo_root.path().join("inner");
        std::fs::create_dir_all(&inner).unwrap();
        let res = attach_existing_repo_local(
            repo_root.path().to_string_lossy().to_string(),
            inner.to_string_lossy().to_string(),
        )
        .await;
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("already inside a git repo"));
    }

    #[tokio::test]
    async fn nested_repo_candidates_reuse_the_shared_walk() {
        let tmp = tempfile::tempdir().unwrap();
        let deep = tmp.path().join("Code").join("app");
        std::fs::create_dir_all(deep.join(".git")).unwrap();

        let found = list_nested_repo_candidates(tmp.path().to_string_lossy().to_string())
            .await
            .unwrap();
        assert_eq!(found.len(), 1, "one nested repo expected");
        assert_eq!(found[0].rel_path, "Code/app");
        assert_eq!(
            std::path::Path::new(&found[0].abs_path),
            deep.as_path(),
            "abs_path must point at the nested repo"
        );
        // A missing root soft-fails to an empty list (dropdown just absent).
        let gone = list_nested_repo_candidates(
            tmp.path().join("nope").to_string_lossy().to_string(),
        )
        .await
        .unwrap();
        assert!(gone.is_empty());
    }

    #[tokio::test]
    async fn scaffold_only_detection() {
        // Scaffold-only: every allowlisted entry present, nothing else.
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(tmp.path().join(".claude")).unwrap();
        std::fs::create_dir_all(tmp.path().join("knowledge")).unwrap();
        std::fs::create_dir_all(tmp.path().join("infrastructure")).unwrap();
        std::fs::create_dir_all(tmp.path().join(".vscode")).unwrap();
        for f in ["CLAUDE.md", "MEMORY.md", ".env.vco.reference", ".env"] {
            std::fs::write(tmp.path().join(f), "x").unwrap();
        }
        let root = tmp.path().to_string_lossy().to_string();
        assert!(detect_scaffold_only_root(root.clone()).await.unwrap());

        // One user file flips it (leave the copy off — folder is not empty).
        std::fs::write(tmp.path().join("main.py"), "print()").unwrap();
        assert!(!detect_scaffold_only_root(root).await.unwrap());

        // A truly empty folder counts as scaffold-empty.
        let empty = tempfile::tempdir().unwrap();
        assert!(
            detect_scaffold_only_root(empty.path().to_string_lossy().to_string())
                .await
                .unwrap()
        );

        // A missing folder soft-fails to false (never overclaim).
        assert!(
            !detect_scaffold_only_root(
                empty.path().join("missing").to_string_lossy().to_string()
            )
            .await
            .unwrap()
        );
    }

    // ── v0.2.103 (plan item 17) — automatic local repo at add / update ──

    fn db_with_project(folder: &Path) -> (Db, String) {
        let db = Db::open_in_memory().expect("in-memory db");
        let id = uuid::Uuid::new_v4().to_string();
        db.insert_project(
            &id,
            "Auto Repo Project",
            &folder.to_string_lossy(),
            crate::db::models::ProjectHost::Base,
            &format!("auto-repo-{}", &id[..8]),
        )
        .expect("insert project");
        (db, id)
    }

    fn recorded_mode(db: &Db, id: &str) -> Option<String> {
        db.get_setting(id, MODULE_ID, SETTING_KEY)
            .unwrap()
            .and_then(|v| v.as_str().map(str::to_string))
    }

    fn git_out(dir: &Path, args: &[&str]) -> std::process::Output {
        std::process::Command::new("git")
            .args(args)
            .current_dir(dir)
            .output()
            .unwrap()
    }

    #[tokio::test]
    async fn auto_repo_created_when_folder_has_no_repo() {
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let sentinel = write_sentinel(tmp.path());
        let (db, id) = db_with_project(tmp.path());

        let out = ensure_project_local_repo(&db, &id, tmp.path()).await;
        assert_eq!(out, AutoLocalRepoOutcome::Created { record_error: None });
        assert_eq!(out.notice().as_deref(), Some(CREATED_LOCAL_REPO_NOTICE));
        assert!(out.warning().is_none());
        assert!(
            CREATED_LOCAL_REPO_NOTICE
                == "Created a local git repository (no remote, nothing committed). Subagents now work in isolated worktrees (snapshots of your files until your first commit).",
            "owner-facing wording pinned"
        );

        // Local-only shape: .git at the root, branch main, no remote, no commit.
        assert!(tmp.path().join(".git").is_dir());
        let head = git_out(tmp.path(), &["symbolic-ref", "HEAD"]);
        assert_eq!(String::from_utf8_lossy(&head.stdout).trim(), "refs/heads/main");
        let remotes = git_out(tmp.path(), &["remote"]);
        assert!(remotes.status.success());
        assert!(String::from_utf8_lossy(&remotes.stdout).trim().is_empty(), "no remote");
        let commit = git_out(tmp.path(), &["rev-parse", "--verify", "-q", "HEAD"]);
        assert!(!commit.status.success(), "nothing may be committed");
        let gi = std::fs::read_to_string(tmp.path().join(".gitignore")).unwrap();
        assert!(gi.contains("VCO local-only repo guard"));
        assert!(gi.contains(".claude/state/"));
        assert!(gi.contains(".claude/backups/"), "backups ignored: {gi}");
        assert_eq!(std::fs::read(&sentinel).unwrap(), SENTINEL_BYTES);

        // Recorded once — a second run (the next bundle update) is a no-op.
        assert_eq!(recorded_mode(&db, &id).as_deref(), Some("local_init"));
        let again = ensure_project_local_repo(&db, &id, tmp.path()).await;
        assert_eq!(
            again,
            AutoLocalRepoOutcome::AlreadyDecided { mode: "local_init".into() }
        );
        assert!(again.notice().is_none());
        let gi2 = std::fs::read_to_string(tmp.path().join(".gitignore")).unwrap();
        assert_eq!(gi, gi2, "the second run must not touch the repo");
    }

    #[tokio::test]
    async fn auto_repo_leaves_an_existing_repo_untouched() {
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        git_out(tmp.path(), &["init", "--initial-branch=trunk"]);
        let sentinel = write_sentinel(tmp.path());
        let config_before = std::fs::read(tmp.path().join(".git").join("config")).unwrap();
        let (db, id) = db_with_project(tmp.path());

        let out = ensure_project_local_repo(&db, &id, tmp.path()).await;
        assert!(
            matches!(out, AutoLocalRepoOutcome::ExistingRepo { .. }),
            "got {out:?}"
        );
        assert!(out.notice().is_none() && out.warning().is_none());
        assert!(!tmp.path().join(".gitignore").exists(), "no guard written into a user repo");
        assert_eq!(
            std::fs::read(tmp.path().join(".git").join("config")).unwrap(),
            config_before
        );
        let head = git_out(tmp.path(), &["symbolic-ref", "HEAD"]);
        assert_eq!(String::from_utf8_lossy(&head.stdout).trim(), "refs/heads/trunk");
        assert_eq!(std::fs::read(&sentinel).unwrap(), SENTINEL_BYTES);
        assert_eq!(recorded_mode(&db, &id).as_deref(), Some("use_existing"));
    }

    #[tokio::test]
    async fn auto_repo_never_nests_inside_an_enclosing_repo() {
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        git_out(tmp.path(), &["init", "--initial-branch=main"]);
        let sub = tmp.path().join("sub").join("project");
        std::fs::create_dir_all(&sub).unwrap();
        let (db, id) = db_with_project(&sub);

        let out = ensure_project_local_repo(&db, &id, &sub).await;
        match &out {
            AutoLocalRepoOutcome::ExistingRepo { toplevel } => assert_eq!(
                std::fs::canonicalize(toplevel).unwrap(),
                std::fs::canonicalize(tmp.path()).unwrap()
            ),
            other => panic!("expected ExistingRepo, got {other:?}"),
        }
        assert!(!sub.join(".git").exists(), "no nested repo may be created");
        assert_eq!(recorded_mode(&db, &id).as_deref(), Some("use_existing"));
    }

    #[tokio::test]
    async fn auto_repo_nested_repos_are_a_real_choice_for_the_modal() {
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let nested = tmp.path().join("Code").join("app");
        std::fs::create_dir_all(&nested).unwrap();
        git_out(&nested, &["init", "--initial-branch=main"]);
        let (db, id) = db_with_project(tmp.path());

        assert_eq!(
            plan_label(&db, &id, tmp.path()).await.unwrap(),
            "needs_choice"
        );
        let out = ensure_project_local_repo(&db, &id, tmp.path()).await;
        assert_eq!(
            out,
            AutoLocalRepoOutcome::NeedsChoice { nested: vec!["Code/app".into()] }
        );
        assert!(out.notice().is_none() && out.warning().is_none());
        assert!(!tmp.path().join(".git").exists(), "nothing initialised");
        assert!(!tmp.path().join(".gitignore").exists());
        assert_eq!(recorded_mode(&db, &id), None, "undecided, so the modal still asks");
    }

    #[tokio::test]
    async fn auto_repo_respects_a_recorded_opt_out() {
        let tmp = tempfile::tempdir().unwrap();
        let (db, id) = db_with_project(tmp.path());
        db.set_setting(&id, MODULE_ID, SETTING_KEY, &Value::String("no_repo".into()))
            .unwrap();
        let out = ensure_project_local_repo(&db, &id, tmp.path()).await;
        assert_eq!(out, AutoLocalRepoOutcome::AlreadyDecided { mode: "no_repo".into() });
        assert!(!tmp.path().join(".git").exists());
        assert_eq!(plan_label(&db, &id, tmp.path()).await.unwrap(), "decided");
    }

    #[tokio::test]
    async fn plan_label_reports_pending_init_and_records_existing() {
        if !git_available() {
            return;
        }
        let empty = tempfile::tempdir().unwrap();
        let (db, id) = db_with_project(empty.path());
        assert_eq!(
            plan_label(&db, &id, empty.path()).await.unwrap(),
            "auto_init_pending"
        );
        assert!(!empty.path().join(".git").exists(), "the probe never mutates");
        assert_eq!(recorded_mode(&db, &id), None);

        let repo = tempfile::tempdir().unwrap();
        git_out(repo.path(), &["init", "--initial-branch=main"]);
        let (db2, id2) = db_with_project(repo.path());
        assert_eq!(plan_label(&db2, &id2, repo.path()).await.unwrap(), "existing");
        assert_eq!(recorded_mode(&db2, &id2).as_deref(), Some("use_existing"));
    }

    /// Run `fut` on a current-thread runtime while THIS thread's git lookup
    /// PATH is `dir` (`with_lookup_path` is thread-local; the process PATH is
    /// never touched).
    fn with_git_path<T>(dir: &Path, fut: impl std::future::Future<Output = T>) -> T {
        vct_launcher_core::paths::with_lookup_path(Some(dir.as_os_str()), || {
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap()
                .block_on(fut)
        })
    }

    #[test]
    fn auto_repo_git_missing_is_a_clear_warning_and_changes_nothing() {
        let tmp = tempfile::tempdir().unwrap();
        let no_git = tempfile::tempdir().unwrap();
        let (db, id) = db_with_project(tmp.path());

        let out = with_git_path(no_git.path(), ensure_project_local_repo(&db, &id, tmp.path()));
        assert!(
            matches!(out, AutoLocalRepoOutcome::GitUnavailable { .. }),
            "got {out:?}"
        );
        let warning = out.warning().expect("git missing must warn");
        assert!(warning.contains("Install git"), "{warning}");
        assert!(out.notice().is_none());
        assert!(!tmp.path().join(".git").exists());
        assert_eq!(recorded_mode(&db, &id), None, "unrecorded, so the next update retries");
        let label = with_git_path(no_git.path(), plan_label(&db, &id, tmp.path())).unwrap();
        assert_eq!(label, "git_unavailable");
    }

    #[cfg(unix)]
    #[test]
    fn auto_repo_without_a_git_identity_still_inits_and_never_commits() {
        use std::os::unix::fs::PermissionsExt;
        if !git_available() {
            return;
        }
        let real_git = vct_launcher_core::paths::which_on_path("git").expect("git on PATH");
        let bin = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        // A `git` with NO identity anywhere: no global/system config, a fresh
        // HOME, identity env vars unset, and no hostname fallback.
        let wrapper = bin.path().join("git");
        std::fs::write(
            &wrapper,
            format!(
                "#!/bin/sh\nexport HOME='{}' GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1\n\
                 unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL EMAIL\n\
                 exec '{}' -c user.useConfigOnly=true \"$@\"\n",
                home.path().display(),
                real_git.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&wrapper, std::fs::Permissions::from_mode(0o755)).unwrap();
        let w = |args: &[&str], dir: &Path| {
            std::process::Command::new(&wrapper)
                .args(args)
                .current_dir(dir)
                .output()
                .unwrap()
        };
        // Sanity: the fixture really cannot commit.
        let scratch = tempfile::tempdir().unwrap();
        assert!(w(&["init"], scratch.path()).status.success());
        assert!(
            !w(&["commit", "--allow-empty", "-m", "x"], scratch.path()).status.success(),
            "fixture must have no git identity"
        );

        let tmp = tempfile::tempdir().unwrap();
        let (db, id) = db_with_project(tmp.path());
        let out = with_git_path(bin.path(), ensure_project_local_repo(&db, &id, tmp.path()));
        assert_eq!(out, AutoLocalRepoOutcome::Created { record_error: None });
        let notice = out.notice().unwrap();
        assert!(notice.contains("nothing committed"), "{notice}");
        assert!(
            notice.contains("snapshots of your files until your first commit"),
            "{notice}"
        );
        assert!(tmp.path().join(".git").is_dir());
        assert!(
            !w(&["rev-parse", "--verify", "-q", "HEAD"], tmp.path()).status.success(),
            "no commit"
        );
        assert_eq!(recorded_mode(&db, &id).as_deref(), Some("local_init"));
    }

    #[tokio::test]
    async fn adapter_routes_created_to_notices_and_existing_to_nothing() {
        if !git_available() {
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let (db, id) = db_with_project(tmp.path());
        let (mut notices, mut warnings) = (Vec::new(), vec!["earlier".to_string()]);
        collect_local_repo_outcome(&db, &id, tmp.path(), &mut notices, &mut warnings).await;
        assert_eq!(notices, vec![CREATED_LOCAL_REPO_NOTICE.to_string()]);
        assert_eq!(warnings, vec!["earlier".to_string()], "a notice is never a warning");

        let repo = tempfile::tempdir().unwrap();
        git_out(repo.path(), &["init", "--initial-branch=main"]);
        let (db2, id2) = db_with_project(repo.path());
        let (mut n2, mut w2) = (Vec::new(), Vec::new());
        collect_local_repo_outcome(&db2, &id2, repo.path(), &mut n2, &mut w2).await;
        assert!(n2.is_empty() && w2.is_empty(), "existing repo: silent");
    }

    #[test]
    fn adapter_routes_git_missing_to_warnings() {
        let tmp = tempfile::tempdir().unwrap();
        let no_git = tempfile::tempdir().unwrap();
        let (db, id) = db_with_project(tmp.path());
        let (mut notices, mut warnings) = (Vec::new(), Vec::new());
        with_git_path(
            no_git.path(),
            collect_local_repo_outcome(&db, &id, tmp.path(), &mut notices, &mut warnings),
        );
        assert!(notices.is_empty());
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].contains("Install git"), "{}", warnings[0]);
    }

    #[test]
    fn gitignore_guard_recognises_the_pre_v0_2_103_marker() {
        let tmp = tempfile::tempdir().unwrap();
        let old = "\n# --- VCO local-only repo guard (created by the subagent-git modal) ---\n.claude/state/\n";
        std::fs::write(tmp.path().join(".gitignore"), old).unwrap();
        append_local_repo_gitignore_guard(tmp.path()).unwrap();
        assert_eq!(
            std::fs::read_to_string(tmp.path().join(".gitignore")).unwrap(),
            old,
            "an existing guard block (old marker) must not be duplicated"
        );
    }
}
