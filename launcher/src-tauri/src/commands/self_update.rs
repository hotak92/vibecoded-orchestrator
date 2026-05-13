//! Launcher self-update via git-pull.
//!
//! Behaviour: pull the latest from the remote, merge it (skipping
//! files considered user-owned, e.g. `CONTEXT_STATE.md`), then restart
//! the launcher to pick up changes. Check for updates once a day and
//! surface a notification when a new version is available — never
//! auto-apply.
//!
//! Approach:
//!   1. Daily background check: `git ls-remote origin <branch>` + local
//!      `git rev-parse HEAD` to compare SHAs without fetching the full
//!      history. Cheap (<1s on a healthy network).
//!   2. If remote ahead: emit `vct-launcher-update-available` event and
//!      surface it in the tray label. NEVER auto-apply.
//!   3. On user click of "Update now": run `git status --porcelain` to
//!      assert a clean tree on tracked files (untracked files in
//!      user-owned dirs are fine). Then `git pull --ff-only` (conservative —
//!      no merge commits, no rebase, no force). Rebuild only the deltas
//!      that changed (Cargo if Rust touched, npm if frontend touched),
//!      then spawn the new binary and exit current process.
//!
//! Why shell-out to git instead of the `git2` crate:
//!   - `git2` (libgit2) would add ~1MB to the bundle and pull in system
//!     deps. The existing installer.rs already shells out — this matches.
//!   - All operations we need (ls-remote, rev-parse, status, pull) are
//!     trivial single-line invocations. No advanced graph queries.
//!   - If git isn't on PATH we degrade gracefully (`git_available()`
//!     returns false → `check_for_launcher_update` returns a sentinel
//!     status and the UI shows a helpful message).

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Emitter, Runtime};
use tokio::process::Command as TokioCommand;

/// Refresh cadence for the daily background check. The user said "once a
/// day" — we run a check every 24h after the previous successful check
/// completed. Exposed as a const so tests can override.
pub const CHECK_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);

/// Per-call timeout. `git ls-remote` over slow links can stall; cap it
/// so the daily task doesn't pile up.
const GIT_TIMEOUT: Duration = Duration::from_secs(30);

/// Default-protected paths inside the launcher repo. NEVER overwritten by
/// `apply_launcher_update`. The list is conservative: anything that
/// represents *user state* (notes, logs, runtime DB, env files) goes here.
/// Bundled state files (e.g. `state/` in a fresh clone) are also covered
/// because we run `git status` first and bail if any tracked file in
/// these dirs has uncommitted changes.
///
/// Note: paths are repo-relative. The frontend uses this list to render
/// "what's protected" in the update preferences page, so it's worth
/// keeping the list short and explanatory.
pub const USER_OWNED_PATHS: &[&str] = &[
    ".claude/CONTEXT_STATE.md",
    ".claude/context",
    ".claude/logs",
    ".env",
    ".env.local",
    "knowledge/.node_formats.json",
    "state",
];

/// Documented but outside-the-repo user-owned dirs. Surfaced to the UI
/// for transparency only — git won't touch these regardless.
pub const USER_OWNED_EXTERNAL: &[&str] = &["~/.vct"];

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateStatus {
    /// True iff `remote_sha` != `current_sha` AND `commit_count > 0`.
    pub available: bool,
    /// Local HEAD SHA (full 40 chars) or null if not in a git repo.
    pub current_sha: Option<String>,
    /// Remote HEAD SHA from `git ls-remote origin <branch>`.
    pub remote_sha: Option<String>,
    /// Number of commits remote is ahead of local. Computed via
    /// `git rev-list --count HEAD..origin/<branch>` — requires a fetch
    /// to be accurate. We do a `git fetch --quiet` before measuring.
    pub commit_count: u32,
    /// Branch we're tracking. Defaults to whatever the launcher repo's
    /// HEAD currently points to.
    pub branch: String,
    /// ISO-8601 timestamp of the last successful check. Persisted in
    /// `~/.vct/launcher-update-state.json`.
    pub last_checked: Option<DateTime<Utc>>,
    /// Set to a human-readable error message when the check itself
    /// failed (e.g. "git not found", "network unreachable"). The UI
    /// renders this as a warning instead of "available: false".
    pub error: Option<String>,
}

impl UpdateStatus {
    fn unavailable(reason: &str, last_checked: Option<DateTime<Utc>>) -> Self {
        Self {
            available: false,
            current_sha: None,
            remote_sha: None,
            commit_count: 0,
            branch: String::new(),
            last_checked,
            error: Some(reason.to_string()),
        }
    }
}

/// Persisted state — survives launcher restarts so the daily timer is
/// honored across sessions. Schema kept minimal so we don't have to
/// version it; missing fields fall back to defaults on read.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct UpdateState {
    last_checked_at: Option<DateTime<Utc>>,
    last_known_remote_sha: Option<String>,
    /// Cached so the tray can show "N commits behind" without re-running
    /// the check on every startup.
    last_known_commit_count: Option<u32>,
    /// User toggle from preferences; defaults to true.
    auto_check_enabled: Option<bool>,
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

fn state_file_path() -> PathBuf {
    crate::paths::vct_root_dir().join("launcher-update-state.json")
}

fn load_state() -> UpdateState {
    let path = state_file_path();
    if !path.exists() {
        return UpdateState::default();
    }
    std::fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn save_state(state: &UpdateState) -> Result<(), String> {
    let path = state_file_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let body = serde_json::to_string_pretty(state).map_err(|e| e.to_string())?;
    std::fs::write(&path, body).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Repo location
// ---------------------------------------------------------------------------

/// Locate the launcher's git repo root — the *enclosing* repo, NOT the
/// orchestrator install path. Strategy mirrors `installer::find_local_repo_root`
/// but stops at the first `.git/` we find walking up from the binary.
///
/// We only support self-update from a git checkout. A bundled (non-git)
/// release would either ship its own updater or rely on the OS package
/// manager — out of scope.
pub fn find_launcher_repo_root() -> Result<PathBuf, String> {
    // Walk up from the running binary looking for a `.git/`. This handles
    // every release-binary scenario the launcher cares about (binary
    // shipped at `<clone>/launcher/dist/<arch>/vct-launcher`, walking up
    // four levels to the clone root).
    //
    // Privacy note (2026-05-06): an earlier implementation also tried
    // `option_env!("CARGO_MANIFEST_DIR")` as a fallback for `cargo run`
    // dev launches. That macro embeds the build-host's absolute manifest
    // path as a static string in the binary, which `--remap-path-prefix`
    // does NOT rewrite — it leaked the developer's path on every release
    // shipped from a dev box. Dev launches via `cargo run` are now
    // expected to pre-set `current_exe()` correctly via the binary's
    // location under `target/release/`, which still lives inside the
    // clone, so Strategy 1 finds the repo root the same way.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(found) = walk_up_for_git(&exe) {
            return Ok(found);
        }
    }
    Err("Launcher is not running from a git checkout — self-update disabled".into())
}

fn walk_up_for_git(start: &Path) -> Option<PathBuf> {
    let mut cur = start.to_path_buf();
    if cur.is_file() {
        cur = cur.parent()?.to_path_buf();
    }
    loop {
        if cur.join(".git").exists() {
            return Some(cur);
        }
        if !cur.pop() {
            return None;
        }
    }
}

// ---------------------------------------------------------------------------
// git availability + helpers
// ---------------------------------------------------------------------------

async fn git_available() -> bool {
    TokioCommand::new("git")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await
        .map(|s| s.success())
        .unwrap_or(false)
}

async fn run_git(repo: &Path, args: &[&str]) -> Result<String, String> {
    let fut = TokioCommand::new("git")
        .args(args)
        .current_dir(repo)
        .output();
    let output = tokio::time::timeout(GIT_TIMEOUT, fut)
        .await
        .map_err(|_| format!("git {} timed out", args.join(" ")))?
        .map_err(|e| format!("git {} failed: {}", args.join(" "), e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("git {}: {}", args.join(" "), stderr.trim()));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

async fn current_branch(repo: &Path) -> Result<String, String> {
    run_git(repo, &["rev-parse", "--abbrev-ref", "HEAD"]).await
}

async fn current_sha(repo: &Path) -> Result<String, String> {
    run_git(repo, &["rev-parse", "HEAD"]).await
}

async fn ls_remote_sha(repo: &Path, branch: &str) -> Result<String, String> {
    // `git ls-remote origin <branch>` returns `<sha>\trefs/heads/<branch>`.
    let raw = run_git(repo, &["ls-remote", "origin", branch]).await?;
    raw.split_whitespace()
        .next()
        .map(|s| s.to_string())
        .ok_or_else(|| format!("ls-remote returned empty output for {}", branch))
}

async fn fetch_origin(repo: &Path) -> Result<(), String> {
    run_git(repo, &["fetch", "--quiet", "origin"]).await.map(|_| ())
}

async fn count_commits_ahead(repo: &Path, branch: &str) -> Result<u32, String> {
    let raw = run_git(
        repo,
        &["rev-list", "--count", &format!("HEAD..origin/{}", branch)],
    )
    .await?;
    raw.parse::<u32>()
        .map_err(|e| format!("count parse failed: {}", e))
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

/// Compare local HEAD against `origin/<branch>`. Always does a `git fetch`
/// first so commit-count is accurate. Saves the result to disk and emits
/// a `vct-launcher-update-available` event when an update is found.
#[command]
pub async fn check_for_launcher_update<R: Runtime>(
    app: AppHandle<R>,
) -> Result<UpdateStatus, String> {
    let last_checked = load_state().last_checked_at;

    if !git_available().await {
        return Ok(UpdateStatus::unavailable(
            "git not found on PATH — install git to enable self-update",
            last_checked,
        ));
    }

    let repo = match find_launcher_repo_root() {
        Ok(p) => p,
        Err(e) => return Ok(UpdateStatus::unavailable(&e, last_checked)),
    };

    let branch = current_branch(&repo)
        .await
        .unwrap_or_else(|_| "main".to_string());

    let local_sha = match current_sha(&repo).await {
        Ok(s) => s,
        Err(e) => return Ok(UpdateStatus::unavailable(&e, last_checked)),
    };

    // Fetch + ls-remote both bring back the remote SHA. Fetch is required
    // so `rev-list --count` works without doing a second network round-trip.
    if let Err(e) = fetch_origin(&repo).await {
        // Network unreachable / auth failure / etc. Surface as a soft
        // error — the UI still shows current SHA and last-known status.
        return Ok(UpdateStatus::unavailable(&e, last_checked));
    }

    let remote_sha = match ls_remote_sha(&repo, &branch).await {
        Ok(s) => s,
        Err(e) => return Ok(UpdateStatus::unavailable(&e, last_checked)),
    };

    let commit_count = count_commits_ahead(&repo, &branch).await.unwrap_or(0);
    let available = remote_sha != local_sha && commit_count > 0;
    let now = Utc::now();

    // Persist regardless of available/not — that's how we honor the daily
    // cadence on next startup.
    let mut state = load_state();
    state.last_checked_at = Some(now);
    state.last_known_remote_sha = Some(remote_sha.clone());
    state.last_known_commit_count = Some(commit_count);
    let _ = save_state(&state);

    let status = UpdateStatus {
        available,
        current_sha: Some(local_sha),
        remote_sha: Some(remote_sha),
        commit_count,
        branch,
        last_checked: Some(now),
        error: None,
    };

    if available {
        // Tray + window listeners both subscribe to this. Payload is the
        // full status so consumers don't have to re-invoke the command.
        let _ = app.emit("vct-launcher-update-available", &status);
    }

    Ok(status)
}

/// User-triggered apply. Refuses if:
///   - git is not available
///   - launcher is not running from a git checkout
///   - tracked files have uncommitted changes (would be clobbered by pull)
///
/// Does NOT refuse on untracked files in user-owned dirs (e.g. an actively
/// edited `.claude/CONTEXT_STATE.md`) — git won't overwrite those.
///
/// Non-fast-forward handling (Option γ, 2026-05-07): if `git pull --ff-only`
/// fails because the local clone diverged from upstream (the case after the
/// 2026-05-06 history rewrite), we don't auto-recover. We return a JSON
/// payload the frontend recognizes and renders as a "Resync" modal. See
/// `force_resync_launcher` for the recovery path the user opts into from
/// that modal.
#[command]
pub async fn apply_launcher_update<R: Runtime>(app: AppHandle<R>) -> Result<(), String> {
    if !git_available().await {
        return Err("git not found on PATH — cannot apply update".into());
    }

    let repo = find_launcher_repo_root()?;

    // Step 1: clean-tree assertion. `git status --porcelain` lists every
    // path with an unstaged or staged change; we filter out untracked-in-
    // user-owned-dirs and only block on actual conflicts.
    let dirty = run_git(&repo, &["status", "--porcelain"]).await?;
    if let Some(blocker) = first_blocking_change(&dirty) {
        return Err(format!(
            "Uncommitted changes on tracked file '{}' would be lost. Commit, stash, \
             or revert before updating.",
            blocker
        ));
    }

    // Step 2: detect what changed BEFORE pulling so we can decide what
    // to rebuild. We diff the current HEAD against origin/<branch>.
    let branch = current_branch(&repo)
        .await
        .unwrap_or_else(|_| "main".to_string());
    let pre_diff = run_git(
        &repo,
        &[
            "diff",
            "--name-only",
            &format!("HEAD..origin/{}", branch),
        ],
    )
    .await
    .unwrap_or_default();
    let needs_cargo = changed_paths_need_cargo(&pre_diff);
    let needs_npm = changed_paths_need_npm(&pre_diff);

    // Step 3: ff-only pull. Conservative — never rewrites history, never
    // creates merge commits. If we're on a diverged branch we surface a
    // structured error the frontend recognizes as a non-FF event so it
    // can render the resync modal instead of a raw error string.
    if let Err(e) = run_git(&repo, &["pull", "--ff-only", "origin", &branch]).await {
        if is_non_fast_forward(&e) {
            // Best-effort: capture local + remote SHAs so the modal can
            // show users what their clone has vs. what upstream has.
            let local = current_sha(&repo).await.ok();
            let remote = ls_remote_sha(&repo, &branch).await.ok();
            return Err(serialize_non_ff_error(
                &branch,
                local.as_deref(),
                remote.as_deref(),
                &e,
            ));
        }
        return Err(e);
    }

    finish_apply_after_pull(app, &repo, needs_cargo, needs_npm).await
}

/// Recovery path after a non-fast-forward detection. Hard-resets the
/// launcher's tracked files to `origin/<branch>`. **Destructive** —
/// untracked files (user state, `.env`, `state/`, `~/.vct/`, etc.) are
/// left untouched, but any tracked-file edits the user made locally
/// are lost.
///
/// We deliberately do NOT re-assert clean tree here (unlike
/// `apply_launcher_update`): the whole point is to override divergence
/// the user has already opted into via the modal. The frontend modal
/// makes the "your tracked-file changes will be lost" warning explicit.
///
/// Sequence:
///   1. fetch origin/<branch>
///   2. compute pre-reset diff for rebuild gating (HEAD..origin/<branch>)
///   3. reset --hard origin/<branch>
///   4. rebuild + restart (shared with `apply_launcher_update`)
#[command]
pub async fn force_resync_launcher<R: Runtime>(app: AppHandle<R>) -> Result<(), String> {
    if !git_available().await {
        return Err("git not found on PATH — cannot resync".into());
    }
    let repo = find_launcher_repo_root()?;
    let branch = current_branch(&repo)
        .await
        .unwrap_or_else(|_| "main".to_string());

    // Fetch first so origin/<branch> is fresh.
    fetch_origin(&repo).await?;

    // Diff BEFORE reset so we know which builds to run. After the reset
    // HEAD == origin/<branch> and the diff would be empty.
    let pre_diff = run_git(
        &repo,
        &[
            "diff",
            "--name-only",
            &format!("HEAD..origin/{}", branch),
        ],
    )
    .await
    .unwrap_or_default();
    let needs_cargo = changed_paths_need_cargo(&pre_diff);
    let needs_npm = changed_paths_need_npm(&pre_diff);

    // Destructive step. After this point local divergent commits are gone.
    run_git(&repo, &["reset", "--hard", &format!("origin/{}", branch)]).await?;

    finish_apply_after_pull(app, &repo, needs_cargo, needs_npm).await
}

/// Shared post-pull / post-reset rebuild + restart sequence. Extracted so
/// `apply_launcher_update` and `force_resync_launcher` can't drift apart.
async fn finish_apply_after_pull<R: Runtime>(
    app: AppHandle<R>,
    repo: &Path,
    needs_cargo: bool,
    needs_npm: bool,
) -> Result<(), String> {
    // Step 4: rebuild. We do this synchronously (the user clicked "Update
    // now" / "Resync now" — they're waiting). Failures bubble up and the
    // launcher stays on the old binary, which is the safe behavior.
    if needs_cargo {
        rebuild_cargo(repo).await?;
    }
    if needs_npm {
        rebuild_frontend(repo).await?;
    }

    // Step 5: restart. Spawn the same binary path as a new process, then
    // exit the current one. On all three platforms `current_exe()` returns
    // the path that was used to launch us, which is what we want post-
    // rebuild because the new binary lives at the same path.
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;

    // C3 (v0.2.6): refresh the desktop shortcut so it picks up any
    // change in binary path/contents post-rebuild. The launcher repo is
    // the install path here (self-update operates on the launcher's
    // enclosing checkout). Soft-fail: never block restart.
    if let Err(e) = crate::commands::desktop_shortcut::refresh_desktop_shortcut(repo, &exe) {
        eprintln!(
            "[apply_launcher_update] desktop shortcut refresh failed (non-fatal): {}",
            e
        );
    }

    std::process::Command::new(&exe)
        .spawn()
        .map_err(|e| format!("failed to spawn new launcher: {}", e))?;
    // Programmatic shutdown: bypass the Quit confirmation dialog (the
    // user already approved the action; a second confirm here would be
    // confusing and could leave the new launcher orphaned if dismissed).
    crate::quit_dialog::force_quit();
    app.exit(0);
    Ok(())
}

/// Expose the protected list to the UI. The frontend renders it on the
/// updates page so the user knows what won't be touched.
#[command]
pub fn get_user_owned_paths() -> Vec<String> {
    let mut out: Vec<String> = USER_OWNED_PATHS.iter().map(|s| s.to_string()).collect();
    out.extend(USER_OWNED_EXTERNAL.iter().map(|s| s.to_string()));
    out
}

// ---------------------------------------------------------------------------
// Helpers (clean-tree assertion + rebuild gating)
// ---------------------------------------------------------------------------

/// Detect whether a `run_git` error string came from a non-fast-forward
/// `git pull --ff-only`. We match on the canonical phrases git emits in
/// English locales — the launcher does not run git with a forced locale
/// (would risk breaking other diagnostics) so this is best-effort. False
/// negatives just mean the user sees the raw error string instead of the
/// resync modal; no harm.
///
/// Phrases observed on git 2.34+ across Linux/macOS/Windows:
///   - "Not possible to fast-forward, aborting."
///   - "fatal: Not possible to fast-forward, aborting."
///   - "hint: ... non-fast-forward updates were rejected"   (push, but git
///     sometimes echoes 'non-fast-forward' inside hints during pull too)
///   - "fatal: refusing to merge unrelated histories"
///   - "have diverged" / "and have N and M different commits each"
///
/// We err on the side of including 'diverged' since the post-rewrite case
/// is exactly that.
fn is_non_fast_forward(err: &str) -> bool {
    let lower = err.to_lowercase();
    lower.contains("not possible to fast-forward")
        || lower.contains("non-fast-forward")
        || lower.contains("have diverged")
        || lower.contains("refusing to merge unrelated histories")
}

/// Serialize a non-FF error as a JSON string the Svelte side can parse.
/// Frontend tries `JSON.parse(err)` and falls back to displaying the raw
/// string if it doesn't look like JSON. The `kind` field is the
/// discriminator.
///
/// Schema (kept inline so the .rs file is self-documenting; if this grows
/// we'll lift it into a `serde::Serialize` struct):
///   {
///     "kind": "non_fast_forward",
///     "branch": "main",
///     "local_sha":  "abc..." | null,
///     "remote_sha": "def..." | null,
///     "git_stderr": "<raw error>"
///   }
fn serialize_non_ff_error(
    branch: &str,
    local: Option<&str>,
    remote: Option<&str>,
    git_stderr: &str,
) -> String {
    // Manual JSON: the four values are short, controllable strings; pulling
    // serde_json in for a one-shot serialize would be heavier than the
    // string concat. Escape only the stderr (the only field that can
    // contain quotes / backslashes / newlines).
    let stderr_esc = json_escape(git_stderr);
    let local_field = match local {
        Some(s) => format!("\"{}\"", s),
        None => "null".to_string(),
    };
    let remote_field = match remote {
        Some(s) => format!("\"{}\"", s),
        None => "null".to_string(),
    };
    format!(
        "{{\"kind\":\"non_fast_forward\",\"branch\":\"{}\",\"local_sha\":{},\"remote_sha\":{},\"git_stderr\":\"{}\"}}",
        branch, local_field, remote_field, stderr_esc
    )
}

/// Minimal JSON string escape — covers the characters git stderr can
/// realistically contain. Doesn't handle every Unicode edge case (we
/// don't need to: stderr is mostly ASCII English error messages).
fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

/// Returns the first tracked-file change that would be clobbered by `git
/// pull --ff-only`. Untracked files (status code `??`) are ignored —
/// they're not at risk during a fast-forward merge.
fn first_blocking_change(porcelain: &str) -> Option<String> {
    for line in porcelain.lines() {
        if line.len() < 4 {
            continue;
        }
        // `?? path` → untracked, safe.
        // ` M path` / `M  path` / `MM path` / `A  path` / etc. → blocking.
        let code = &line[..2];
        if code == "??" {
            continue;
        }
        let path = line[3..].to_string();
        return Some(path);
    }
    None
}

/// True if any path in the diff lives under `src-tauri/` — we need a
/// `cargo build --release` in that case. Includes `Cargo.toml` /
/// `Cargo.lock` at any depth.
fn changed_paths_need_cargo(diff: &str) -> bool {
    diff.lines().any(|p| {
        p.starts_with("launcher/src-tauri/")
            || p.starts_with("src-tauri/")
            || p.ends_with("Cargo.toml")
            || p.ends_with("Cargo.lock")
    })
}

/// True if any path in the diff is part of the Svelte frontend.
fn changed_paths_need_npm(diff: &str) -> bool {
    diff.lines().any(|p| {
        p.starts_with("launcher/src/")
            || p.starts_with("src/")
            || p.starts_with("launcher/static/")
            || p.starts_with("static/")
            || p.ends_with("package.json")
            || p.ends_with("package-lock.json")
            || p.ends_with("vite.config.js")
            || p.ends_with("svelte.config.js")
    })
}

async fn rebuild_cargo(repo: &Path) -> Result<(), String> {
    // Build dir lives at `<repo>/launcher/src-tauri` when the launcher is
    // bundled inside the orchestrator monorepo. Fall back to `<repo>/
    // src-tauri` for standalone clones.
    let dir = if repo.join("launcher/src-tauri/Cargo.toml").exists() {
        repo.join("launcher/src-tauri")
    } else {
        repo.join("src-tauri")
    };

    // Windows-specific: cargo writes the new .exe over the old one, but
    // the old one is OUR own running process — Windows refuses with
    // "Access is denied" (os error 5). Workaround: rename the running
    // .exe to <name>.old.exe before building. Windows DOES allow
    // renaming a running file (just not deleting/overwriting), so the
    // build then writes the new .exe at the canonical path. We delete
    // the .old.exe on next launcher start (cleanup_stale_old_exe in
    // lib.rs setup). Reported 2026-04-28 from a Windows rebuild attempt.
    #[cfg(windows)]
    {
        if let Ok(running_exe) = std::env::current_exe() {
            // Walk up from running_exe to find the matching target/release/
            // path; only rename if it's the cargo target (not e.g. a copy
            // staged in launcher/dist/ that the user double-clicked from).
            let target_release = dir.join("target").join("release");
            if running_exe.starts_with(&target_release) {
                let old_path = running_exe.with_extension("old.exe");
                let _ = std::fs::remove_file(&old_path); // best-effort
                std::fs::rename(&running_exe, &old_path)
                    .map_err(|e| format!(
                        "rename running launcher to .old.exe (Windows lock workaround): {}",
                        e
                    ))?;
            }
        }
    }

    let fut = TokioCommand::new("cargo")
        .args(["build", "--release"])
        .current_dir(&dir)
        .output();
    // Cargo can be slow on cold builds — give it 15 minutes.
    let output = tokio::time::timeout(Duration::from_secs(900), fut)
        .await
        .map_err(|_| "cargo build timed out (>15min)".to_string())?
        .map_err(|e| format!("cargo build failed to start: {}", e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("cargo build failed: {}", stderr.trim()));
    }
    Ok(())
}

async fn rebuild_frontend(repo: &Path) -> Result<(), String> {
    let dir = if repo.join("launcher/package.json").exists() {
        repo.join("launcher")
    } else {
        repo.to_path_buf()
    };

    let fut = TokioCommand::new("npm")
        .args(["run", "build"])
        .current_dir(&dir)
        .output();
    let output = tokio::time::timeout(Duration::from_secs(600), fut)
        .await
        .map_err(|_| "npm build timed out (>10min)".to_string())?
        .map_err(|e| format!("npm build failed to start: {}", e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("npm build failed: {}", stderr.trim()));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Background daily check
// ---------------------------------------------------------------------------

/// Spawned from `lib.rs::run` setup. Runs forever (until app exit). Honors
/// the user's `auto_check_enabled` toggle on each tick.
pub fn spawn_daily_check<R: Runtime>(app: AppHandle<R>) {
    tauri::async_runtime::spawn(async move {
        // Catch-up logic: if we never checked, or last check was >24h ago,
        // run one immediately. Otherwise sleep until the next slot.
        loop {
            let state = load_state();

            // Honor user toggle. Default ON when the field is missing.
            let enabled = state.auto_check_enabled.unwrap_or(true);
            if !enabled {
                tokio::time::sleep(Duration::from_secs(60 * 60)).await;
                continue;
            }

            let due = match state.last_checked_at {
                None => true,
                Some(ts) => {
                    let age = Utc::now().signed_duration_since(ts);
                    age.num_seconds() as u64 >= CHECK_INTERVAL.as_secs()
                }
            };

            if due {
                // We don't care about the result here — the command itself
                // emits the event and persists state. Errors are silent;
                // the next tick retries.
                let _ = check_for_launcher_update(app.clone()).await;
            }

            // Sleep until the next slot. We wake up every hour to pick up
            // toggle changes, but only run a real check when due.
            tokio::time::sleep(Duration::from_secs(60 * 60)).await;
        }
    });
}

/// Read the cached "last known" status without doing a network call.
/// Used by the tray to decide whether to render the "Update available"
/// label on startup before the first daily check runs.
#[command]
pub fn get_cached_update_status() -> UpdateStatus {
    let state = load_state();
    let count = state.last_known_commit_count.unwrap_or(0);
    let remote = state.last_known_remote_sha.clone();
    UpdateStatus {
        available: count > 0,
        current_sha: None,
        remote_sha: remote,
        commit_count: count,
        branch: String::new(),
        last_checked: state.last_checked_at,
        error: None,
    }
}

/// Persist the user's auto-check toggle. Called from the preferences UI.
#[command]
pub fn set_auto_check_enabled(enabled: bool) -> Result<(), String> {
    let mut state = load_state();
    state.auto_check_enabled = Some(enabled);
    save_state(&state)
}

#[command]
pub fn get_auto_check_enabled() -> bool {
    load_state().auto_check_enabled.unwrap_or(true)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blocking_change_ignores_untracked() {
        let porcelain = "?? .claude/CONTEXT_STATE.md\n?? state/runtime.db\n";
        assert_eq!(first_blocking_change(porcelain), None);
    }

    #[test]
    fn blocking_change_catches_modified_tracked() {
        let porcelain = " M Cargo.toml\n?? .claude/CONTEXT_STATE.md\n";
        assert_eq!(first_blocking_change(porcelain), Some("Cargo.toml".into()));
    }

    #[test]
    fn blocking_change_catches_staged() {
        let porcelain = "M  src-tauri/src/lib.rs\n";
        assert_eq!(
            first_blocking_change(porcelain),
            Some("src-tauri/src/lib.rs".into())
        );
    }

    #[test]
    fn cargo_gating_detects_rust_change() {
        assert!(changed_paths_need_cargo(
            "launcher/src-tauri/src/lib.rs\nREADME.md\n"
        ));
        assert!(changed_paths_need_cargo("Cargo.lock\n"));
        assert!(!changed_paths_need_cargo("README.md\nlauncher/src/app.css\n"));
    }

    #[test]
    fn npm_gating_detects_frontend_change() {
        assert!(changed_paths_need_npm(
            "launcher/src/routes/+page.svelte\nREADME.md\n"
        ));
        assert!(changed_paths_need_npm("launcher/package.json\n"));
        assert!(!changed_paths_need_npm(
            "launcher/src-tauri/src/lib.rs\nREADME.md\n"
        ));
    }

    #[test]
    fn user_owned_paths_includes_critical_files() {
        let v = get_user_owned_paths();
        assert!(v.iter().any(|p| p == ".claude/CONTEXT_STATE.md"));
        assert!(v.iter().any(|p| p == "state"));
        assert!(v.iter().any(|p| p == "~/.vct"));
    }

    #[test]
    fn non_ff_detection_matches_git_phrases() {
        // Real stderr samples from git 2.34+.
        assert!(is_non_fast_forward(
            "git pull --ff-only origin main: fatal: Not possible to fast-forward, aborting."
        ));
        assert!(is_non_fast_forward(
            "fatal: refusing to merge unrelated histories"
        ));
        assert!(is_non_fast_forward(
            "hint: Updates were rejected because the tip of your current branch is behind\n\
             hint: its remote counterpart. (non-fast-forward)"
        ));
        // The post-rewrite case: git often phrases it as "have diverged".
        assert!(is_non_fast_forward(
            "Your branch and 'origin/main' have diverged,\n\
             and have 12 and 47 different commits each, respectively."
        ));
    }

    #[test]
    fn non_ff_detection_ignores_unrelated_errors() {
        assert!(!is_non_fast_forward("fatal: not a git repository"));
        assert!(!is_non_fast_forward("Could not resolve host: github.com"));
        assert!(!is_non_fast_forward(
            "error: Your local changes to the following files would be overwritten"
        ));
        assert!(!is_non_fast_forward(""));
    }

    #[test]
    fn non_ff_detection_is_case_insensitive() {
        // Some packagings shout. Make sure we still match.
        assert!(is_non_fast_forward(
            "FATAL: NOT POSSIBLE TO FAST-FORWARD, ABORTING."
        ));
    }

    #[test]
    fn serialize_non_ff_produces_parseable_json() {
        let s = serialize_non_ff_error(
            "main",
            Some("abc1234"),
            Some("def5678"),
            "fatal: Not possible to fast-forward, aborting.",
        );
        // Must start with {"kind":"non_fast_forward" so the frontend's
        // try/catch fast-path recognizes it.
        assert!(s.starts_with("{\"kind\":\"non_fast_forward\""));
        assert!(s.contains("\"branch\":\"main\""));
        assert!(s.contains("\"local_sha\":\"abc1234\""));
        assert!(s.contains("\"remote_sha\":\"def5678\""));
        // serde_json must be able to parse it (sanity — we hand-rolled
        // the writer, parser does the validation).
        let v: serde_json::Value = serde_json::from_str(&s).expect("valid JSON");
        assert_eq!(v["kind"], "non_fast_forward");
        assert_eq!(v["branch"], "main");
    }

    #[test]
    fn serialize_non_ff_handles_null_shas() {
        // current_sha / ls_remote_sha can fail (offline, etc.) — we still
        // want to emit a usable payload.
        let s = serialize_non_ff_error("main", None, None, "boom");
        assert!(s.contains("\"local_sha\":null"));
        assert!(s.contains("\"remote_sha\":null"));
        let v: serde_json::Value = serde_json::from_str(&s).unwrap();
        assert!(v["local_sha"].is_null());
    }

    #[test]
    fn serialize_non_ff_escapes_stderr_special_chars() {
        // Real git stderr can contain quotes, backslashes, newlines.
        let s = serialize_non_ff_error(
            "main",
            None,
            None,
            "fatal: \"weird\" error\nwith newline\\and backslash",
        );
        // Roundtrip via serde_json — if escaping is wrong, this throws.
        let v: serde_json::Value = serde_json::from_str(&s).expect("escapes correctly");
        let stderr = v["git_stderr"].as_str().unwrap();
        assert!(stderr.contains("\"weird\""));
        assert!(stderr.contains("\nwith newline"));
        assert!(stderr.contains("\\and backslash"));
    }

    #[test]
    fn state_roundtrip() {
        // Use a temp dir to avoid clobbering the real ~/.vct file.
        let tmp = tempfile::tempdir().unwrap();
        std::env::set_var("HOME", tmp.path());

        let mut s = UpdateState::default();
        s.last_checked_at = Some(Utc::now());
        s.last_known_commit_count = Some(7);
        s.auto_check_enabled = Some(false);
        save_state(&s).unwrap();

        let back = load_state();
        assert_eq!(back.last_known_commit_count, Some(7));
        assert_eq!(back.auto_check_enabled, Some(false));
    }
}
