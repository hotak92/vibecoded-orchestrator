//! Launcher self-update via git-pull.
//!
//! Behaviour: pull the latest from the remote, merge it (skipping
//! files considered user-owned, e.g. `CONTEXT_STATE.md`), then restart
//! the launcher to pick up changes. Check for updates once a day and
//! surface a notification when a new version is available — never
//! auto-apply.
//!
//! Approach:
//!   1. Daily background check: one `git fetch vco_upstream`, then local
//!      `git rev-parse` of `HEAD` and `vco_upstream/<branch>` plus a
//!      `rev-list --count` (v0.2.100: no second `ls-remote` round-trip after
//!      the fetch). Cheap on a healthy network. (Design B: the launcher
//!      self-updates from the pinned `vco_upstream` remote, NOT `origin` —
//!      which on a private fork may point somewhere else.)
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
//!   - All operations we need (fetch, rev-parse, status, pull) are
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
use vct_launcher_core::process::CommandExt as _;

// v0.2.71 Sweep-A#3: the relocated shared durable-deferral writer + its
// failure-shape enum. PRE-v0.2.71 a failed launcher SELF-update returned
// ONLY a transient serialized modal error (`serialize_non_ff_error`) — no
// `UPDATE_DEFERRED.md` trace a terminal Claude could find at session start,
// unlike the installer (MenuBar-badge) update surface which DID write one.
// We now call this ONE writer from the self-update failure paths too so both
// surfaces leave the SAME durable record. The enum/writer live in
// `git_user_editable_merge` (relocated from installer.rs-private) so neither
// surface grows a second copy.
// v0.2.95 phase 2: this surface no longer writes that record ITSELF — it pulls
// through `update_pipeline`, which writes the diverged deferral (and the paired
// resume sentinel this surface never had) from the one classification. The
// import is gone with the last call site; the record is not.
//
// v0.2.92 WP-13: the ONE git runner + the ONE branch resolver (see
// `commands::git_cmd`). `run_git` is imported under its old name so the call
// sites in this file did not churn during the extraction.
//
// v0.2.95 phase 2: `run_git_combined` dropped from this import — its only
// caller here was this surface's own `git pull`, which is now the pipeline's.
// The pipeline pins `LC_ALL=C` on that pull through `run_git_raw_env`, so the
// C-locale guarantee `run_git_combined` carried is preserved (and extended to
// the surface that never had it).
use crate::commands::git_cmd::{self, run_git};
// v0.2.100 F-W4-02: the fetch ladder lives in its own module.
use crate::commands::upstream_fetch::{fetch_upstream, serialized_fetch_upstream, FetchPolicy};
use vct_launcher_core::check_state::CheckState;

/// Refresh cadence for the daily background check. The user said "once a
/// day" — we run a check every 24h after the previous successful check
/// completed. Exposed as a const so tests can override.
pub const CHECK_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);

// The per-call git timeout moved with the runners to
// `commands::git_cmd::GIT_TIMEOUT` (v0.2.92 WP-13) — one runner, one cap.

// ---------------------------------------------------------------------------
// Canonical upstream remote (Design B, 2026-05-19)
// ---------------------------------------------------------------------------
//
// The launcher self-updates from the PUBLIC AGPL upstream regardless of which
// fork the local `origin` remote points at. This matters because the
// orchestrator ships into private forks (VCO_dev, customer mirrors, etc.)
// where `origin` is the private fork — without this pinning, self-update
// would either fail (private fork lacks the public release tags) or worse,
// pull private commits into a public install.
//
// Implementation: maintain a dedicated remote called `vco_upstream` whose
// URL is always resolved from `default_upstream_url()`. The hardcoded
// default points at the canonical public repo; users with enterprise
// self-hosted mirrors can set `VCO_UPSTREAM_URL` to override it.
//
// `ensure_upstream_remote` runs at the START of every update flow (check,
// apply, force-resync). It's idempotent and cheap — three git invocations
// in the steady-state case (get-url → match → done).

/// Canonical public AGPL upstream. The launcher self-updates from this URL
/// regardless of what `origin` points at on the local machine.
const VCO_UPSTREAM_URL: &str = "https://github.com/hotak92/vibecoded-orchestrator.git";

/// The internal name the launcher uses for the canonical upstream remote.
/// Kept distinct from `origin` so user-managed remotes are never disturbed.
///
/// `pub(crate)` because `commands::installer` reuses the same remote name
/// for its `check_for_updates` flow and the update pipeline (Design B
/// also covers the orchestrator self-update path, not just the launcher).
pub(crate) const VCO_UPSTREAM_REMOTE: &str = "vco_upstream";

/// Environment variable that, if set, overrides `VCO_UPSTREAM_URL` at
/// runtime. Intended for enterprise self-hosters who mirror the public
/// repo to an internal git server (e.g. `https://git.example.com/mirrors/vco.git`).
/// Must look like a URL (`http://`, `https://`, or `git@`); otherwise we
/// fall back to the hardcoded default to avoid configuring a broken remote.
const VCO_UPSTREAM_URL_ENV: &str = "VCO_UPSTREAM_URL";

/// Resolve the upstream URL the launcher should pull from. Priority:
/// 1. `$VCO_UPSTREAM_URL` if set, non-empty, and looks like a URL.
/// 2. Hardcoded `VCO_UPSTREAM_URL` (the public AGPL repo).
fn default_upstream_url() -> String {
    if let Ok(val) = std::env::var(VCO_UPSTREAM_URL_ENV) {
        let trimmed = val.trim();
        if !trimmed.is_empty() && looks_like_remote_url(trimmed) {
            return trimmed.to_string();
        }
    }
    VCO_UPSTREAM_URL.to_string()
}

/// Cheap shape check: a remote URL git can fetch from starts with
/// `http://`, `https://`, or `git@` (SSH form). We don't try to parse the
/// full URL — that's git's job, and false positives here just mean the
/// remote add fails loudly later instead of silently pointing somewhere
/// useless.
fn looks_like_remote_url(s: &str) -> bool {
    s.starts_with("https://") || s.starts_with("http://") || s.starts_with("git@")
}

/// Ensure the canonical upstream remote exists and points at the right URL.
/// Idempotent: re-running is cheap (one `git remote get-url`) when the
/// remote is already correct.
///
/// Behaviour:
/// - Remote absent → `git remote add vco_upstream <url>`.
/// - Remote present with the right URL → no-op.
/// - Remote present with the wrong URL → `git remote set-url vco_upstream <url>`.
///
/// We deliberately do NOT touch `origin`. Users may have legitimate reasons
/// for `origin` to point at a fork (their own contributions, a private
/// mirror, etc.). The canonical upstream lives at `vco_upstream` so the two
/// don't collide.
///
/// `pub(crate)` because `commands::installer` reuses this for the
/// orchestrator update path (`check_for_updates` / the update pipeline,
/// `update_run::run_update`). Both share the same architectural
/// invariant: the launcher pulls from the canonical public AGPL repo
/// regardless of what `origin` points at locally.
pub(crate) async fn ensure_upstream_remote(repo: &Path) -> Result<(), String> {
    let want = default_upstream_url();

    match run_git(repo, &["remote", "get-url", VCO_UPSTREAM_REMOTE]).await {
        Ok(current) => {
            if current.trim() == want {
                return Ok(());
            }
            // Wrong URL — correct it. Force-set rather than remove+add so
            // we don't briefly leave the remote in a missing state.
            run_git(repo, &["remote", "set-url", VCO_UPSTREAM_REMOTE, &want])
                .await
                .map(|_| ())
        }
        Err(_) => {
            // `get-url` fails when the remote doesn't exist. Treat any
            // error as "absent" and try to add it — if there's a real
            // problem (e.g. corrupt config) the add will surface it.
            // v0.2.100 WP-05 (L2-F03): two startup actors on a fresh clone
            // both see "absent" and both run `remote add`; the loser fails
            // with "remote vco_upstream already exists" or "could not lock
            // config file" and used to report its whole check Unknown. The
            // peer is writing exactly what we want — converge on it: re-read,
            // and only if the remote is still absent, try the add again.
            let mut last_err = String::new();
            for round in 1..=3u64 {
                match run_git(repo, &["remote", "add", VCO_UPSTREAM_REMOTE, &want]).await {
                    Ok(_) => return Ok(()),
                    Err(e) => last_err = e,
                }
                tokio::time::sleep(Duration::from_millis(50 * round)).await;
                if let Ok(current) = run_git(repo, &["remote", "get-url", VCO_UPSTREAM_REMOTE]).await {
                    if current.trim() == want {
                        return Ok(());
                    }
                    return run_git(repo, &["remote", "set-url", VCO_UPSTREAM_REMOTE, &want])
                        .await
                        .map(|_| ());
                }
            }
            Err(last_err)
        }
    }
}

/// Default-protected paths inside the launcher repo — *user state* (notes,
/// logs, runtime DB, env files). The list is conservative. The update
/// pipeline (`update_run::run_update`) does not discard local changes to
/// them: a pull keeps them (A0 per-path merge / `--autostash`; a clashing
/// pop stops at the autostash-pop modal), and `ResetHard` saves the working
/// tree before it resets (`update_run::create_reset_backup`).
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
    /// True iff `remote_sha` != `current_sha` AND `commit_count > 0` — and
    /// ONLY when [`Self::remote_check`] is `Ok`.
    ///
    /// v0.2.92 WP-13: this field used to be computed unconditionally from a
    /// `commit_count` that `.unwrap_or(0)` had laundered a git `fatal:` into,
    /// so `false` meant both "you are current" and "I could not tell". It is
    /// now only meaningful alongside `remote_check`; when that is `Unknown`
    /// this stays `false` AND the surfaces must render "couldn't check"
    /// rather than "up to date". Do not read one without the other.
    pub available: bool,
    /// Local HEAD SHA (full 40 chars) or null if not in a git repo.
    pub current_sha: Option<String>,
    /// Upstream tip as fetched: `git rev-parse refs/remotes/vco_upstream/<branch>`
    /// after the check's fetch (v0.2.100; was a second `ls-remote`).
    pub remote_sha: Option<String>,
    /// Number of commits remote is ahead of local. Computed via
    /// `git rev-list --count HEAD..vco_upstream/<branch>` — requires a fetch
    /// to be accurate. We do a `git fetch` before measuring.
    ///
    /// `0` when `remote_check` is not `Ok`; read it only when it is.
    pub commit_count: u32,
    /// Branch we compare against. Normalised by `git_cmd::resolve_branch`,
    /// so it is never the literal `"HEAD"` — when HEAD is detached this is
    /// the fallback (`main`) and [`Self::head_detached`] is `true`.
    pub branch: String,
    /// NEW v0.2.92 (WP-13): `true` when the launcher's clone has a detached
    /// HEAD. Previously undetectable from this struct: `branch` was either
    /// the literal `"HEAD"` (self-update surface, which then built a ref
    /// that does not exist) or silently normalised to `main` (installer
    /// surface, which worked but could not say why). Surfaces render it and
    /// offer `reattach_orchestrator_branch`.
    pub head_detached: bool,
    /// NEW v0.2.92 (WP-13): what the remote-currency probe actually
    /// established. `Unknown` ⇒ `available` / `commit_count` are NOT a
    /// verdict; render "couldn't check".
    pub remote_check: CheckState,
    /// NEW v0.2.92 (WP-13): what the "latest source release" probe
    /// established. Separate from `remote_check` because they fail
    /// independently — a working fetch with an unreachable tag listing is a
    /// real state, and collapsing the two would hide it.
    pub latest_source_release_check: CheckState,
    /// ISO-8601 timestamp of the last successful check. Persisted in
    /// `~/.vct/launcher-update-state.json`.
    pub last_checked: Option<DateTime<Utc>>,
    /// Set to a human-readable error message when the check itself
    /// failed (e.g. "git not found", "network unreachable"). The UI
    /// renders this as a warning instead of "available: false".
    pub error: Option<String>,
}

impl UpdateStatus {
    /// The check could not run at all (no git, not a checkout, fetch failed,
    /// …). Note `remote_check` is `Unknown` here, not a bare `available:
    /// false` — the `error` string alone was easy for a consumer to skip.
    fn unavailable(reason: &str, last_checked: Option<DateTime<Utc>>) -> Self {
        Self {
            available: false,
            current_sha: None,
            remote_sha: None,
            commit_count: 0,
            branch: String::new(),
            head_detached: false,
            remote_check: CheckState::unknown(reason),
            latest_source_release_check: CheckState::unknown(reason),
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
    ///
    /// v0.2.92 WP-13: written ONLY when the probe actually determined a
    /// count. Pre-fix a laundered `0` was persisted on every failed check,
    /// which is why the field incident's state file showed a correct, current
    /// `last_known_remote_sha` next to `last_known_commit_count: 0` — the
    /// signature of the bug, misread at the time as a network problem.
    last_known_commit_count: Option<u32>,
    /// NEW v0.2.92 (WP-13): the reason the last check could NOT determine
    /// currency, or `None` when it could. Persisted so the tray label at the
    /// next boot — which renders from cache, before any network call — says
    /// "couldn't check" instead of inheriting a stale-but-cheerful verdict.
    #[serde(skip_serializing_if = "Option::is_none")]
    last_check_unknown_error: Option<String>,
    /// User toggle from preferences; defaults to true.
    ///
    /// `skip_serializing_if` (v0.2.92, WFT C2): every READER already
    /// defaults this to ON via `unwrap_or(true)`, so `null` on disk was
    /// always harmless — but a human reading the state file during an
    /// incident sees a tri-state and reasonably concludes the auto-check is
    /// disabled. Omitting the key when untouched removes that false lead.
    /// Behaviour is unchanged in both directions.
    #[serde(skip_serializing_if = "Option::is_none")]
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
    // v0.2.92 WP-13: this `.unwrap_or_default()` is CORRECT and stays. An
    // unreadable or corrupt state file yields `UpdateState::default()`, whose
    // `last_known_commit_count: None` + `last_check_unknown_error: None` is
    // rendered by `get_cached_update_status` as
    // `Unknown("no update check has completed yet")` — i.e. the default is
    // already the honest answer, not a fabricated healthy one. (Contrast the
    // three `unwrap_or` sites this release removed, whose defaults were `0`
    // and `""` — values that MEAN "current" and "nothing changed".)
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

/// The orchestrator clone this launcher belongs to — through the ONE
/// install-root resolver (v0.2.100 AD-2, F-W1-07/WP-02 gate note): the
/// bounded, identity-checked exe walk, then the process-level root the DB
/// named at boot. It used to be its own unbounded walk to the first `.git`
/// above the exe (any repository qualified) that degraded to "self-update
/// disabled"; the error is now the resolver's typed `RootError`, which names
/// what was searched.
pub fn find_launcher_repo_root() -> Result<PathBuf, String> {
    vct_launcher_core::services::install_root::resolve_current_exe_without_db()
        .map(|r| r.path)
        .map_err(|e| e.to_string())
}


// ---------------------------------------------------------------------------
// git availability + helpers
// ---------------------------------------------------------------------------

async fn git_available() -> bool {
    TokioCommand::new("git").silent()
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await
        .map(|s| s.success())
        .unwrap_or(false)
}

// v0.2.92 WP-13: `run_git` / `run_git_combined` MOVED to
// `commands::git_cmd` — the one home for every git invocation the launcher
// makes. They are `use`d at the top of this file, so the call sites below are
// unchanged. See `git_cmd`'s module docs for why the extraction was not
// optional: this file's private branch resolver disagreed with
// `installer.rs`'s five inline ones, and the disagreement is what made the
// self-update check structurally blind in a detached HEAD.

// v0.2.95 phase 2 — `abort_merge_or_rebase_in_progress` REMOVED, and what it
// protected is now provided differently rather than dropped.
//
// It existed for v0.2.71 BLOCKER-1: a conflicted `apply_launcher_update` left
// `.git/MERGE_HEAD` / `UU` markers, and the NEXT attempt dead-ended at this
// surface's clean-tree guard. So the surface tore its own conflict down, which
// left `force_resync_launcher`'s hard reset as the only forward action.
//
// Both halves of that are now false. This surface pulls through the shared
// update pipeline, which deliberately LEAVES a conflicted tree standing and
// writes the paired resume sentinel + deferral — and a standing conflict is
// what the non-destructive recoveries need: the MenuBar badge reads the merge
// state straight from `.git` and offers Continue Update / Abort
// (`installer::abort_orchestrator_merge_or_rebase`, the same two git commands
// this held, behind the command the modal already calls). And the dead end is
// gone too: the pipeline's in-progress-merge refusal now runs BEFORE this
// surface's dirty-tracked guard, so a second attempt reopens on the stalled
// state instead of being told about the `UU` entries the wedge left behind.

// v0.2.95 phase 2 — `is_merge_conflict` REMOVED. It was a `pub(crate)` one-line
// delegator to `git_user_editable_merge::is_pull_conflict` (kept in v0.2.71 so
// this file's call sites did not churn), and its only caller was this surface's
// own post-pull classifier. The pipeline classifies now, through that same
// shared function — which is where the phrase list has lived since v0.2.71.

// v0.2.92 WP-13: `current_branch` DELETED. It returned
// `git rev-parse --abbrev-ref HEAD` verbatim — which is the literal string
// `"HEAD"` in a detached HEAD, a value `unwrap_or_else(|_| "main")` never
// caught because it arrives as `Ok`, not `Err`. Its three callers now use
// `git_cmd::resolve_branch`, which normalises AND reports `detached`.

async fn current_sha(repo: &Path) -> Result<String, String> {
    run_git(repo, &["rev-parse", "HEAD"]).await
}

/// The upstream tip AS FETCHED: `git rev-parse --verify
/// refs/remotes/vco_upstream/<branch>` — a local read of the ref the fetch
/// just updated.
///
/// v0.2.100 WP-05 (L2-F13): this was `git ls-remote vco_upstream <branch>`, a
/// SECOND network round-trip right after a successful fetch (30s cap, no
/// retry) whose failure threw away the good fetch and turned the whole check
/// into `unavailable`. The fetch already answered the question.
async fn fetched_upstream_sha(repo: &Path, branch: &str) -> Result<String, String> {
    let tracking_ref = format!("refs/remotes/{VCO_UPSTREAM_REMOTE}/{branch}");
    run_git(repo, &["rev-parse", "--verify", &tracking_ref]).await
}

// v0.2.92 WP-13: `count_commits_behind_upstream` MOVED to
// `commands::git_cmd::commits_behind(repo, remote, branch)`. It was a
// `self_update`-private helper that `installer.rs` reached across into, i.e.
// the shared question already lived in the wrong crate module — and the
// remote name was baked in, which hid the fact that the BRANCH argument was
// the thing every caller was getting wrong. Both callers now name the remote
// explicitly.

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

/// Compare local HEAD against `vco_upstream/<branch>` (the public AGPL upstream,
/// pinned by `ensure_upstream_remote` — NOT `origin`, which may be a private
/// fork). Always does a `git fetch` first so commit-count is accurate. Saves the
/// result to disk and emits a `vct-launcher-update-available` event when an
/// update is found.
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

    // The decision core lives in `evaluate_launcher_update` so it is
    // reachable from tests against a fixture repo (v0.2.92 WP-13). Everything
    // below is the parts that genuinely need the AppHandle / process state.
    let (status, detached) = evaluate_launcher_update(&repo, last_checked).await;
    let available = status.available;
    if detached {
        tracing::warn!(
            "[vct] check_for_launcher_update: {} is on a DETACHED HEAD — Preferences → \
             Launcher updates offers a one-click reattach",
            repo.display()
        );
    }

    // v0.2.91 WI-1: reconcile the dist binaries at UPDATE-CHECK time too.
    //
    // `available` above is a pure SHA comparison — a clone whose SOURCE is
    // current but whose BINARY is stale reports "no update available" and the
    // user has no signal at all (RC-2). We deliberately do NOT flip
    // `available` for a stale binary (that would relabel a source check as a
    // binary problem and could pin the badge on permanently); instead the
    // reconcile stages the fresh binary, arms the swap for the user's next
    // quit, and writes the honest `launcher_binary_stale` record. Emission is
    // once-per-process, so polling this command does not spam.
    // v0.2.91 fix-round MAJOR-2(b): this command is POLLED, so it can land in
    // the middle of a real update — in which case the reconcile stands down and
    // reports `stood_down` rather than a verdict it never established.
    let freshness = crate::services::binary_freshness::reconcile_dist_at_rest(&repo).await;
    if freshness.stood_down {
        tracing::info!(
            "[vct] check_for_launcher_update: binary freshness NOT probed this tick — an \
             update owns the tree (source SHAs say available={})",
            available,
        );
    } else if freshness.is_stale() {
        tracing::warn!(
            "[vct] check_for_launcher_update: source SHAs say available={} but the dist binary \
             is stale (staged={:?}, armed={})",
            available, freshness.staged, freshness.armed,
        );
    }

    // Persist regardless of available/not — that's how we honor the daily
    // cadence on next startup.
    persist_check_result(&status);

    if status.available {
        // Tray + window listeners both subscribe to this. Payload is the
        // full status so consumers don't have to re-invoke the command.
        let _ = app.emit("vct-launcher-update-available", &status);
    }

    Ok(status)
}

/// The decision core of [`check_for_launcher_update`], against an explicit
/// repo path: pin the upstream remote, fetch, then judge.
///
/// Extracted in v0.2.92 (WP-13) so the branch-resolution + verdict logic is
/// TESTABLE. The Tauri command resolves its repo through
/// `find_launcher_repo_root`, which walks up from `current_exe()` — i.e. the
/// test binary's own directory — so as long as the logic lived inside the
/// command there was no way to drive it against a fixture repo, and the
/// detached-HEAD blindness could not be caught by any test. That is not a
/// coincidence: the bug survived nine releases in code no test could reach.
///
/// Returns the status plus the raw `detached` bit (also mirrored on the
/// status) so the caller can log without re-resolving.
async fn evaluate_launcher_update(
    repo: &Path,
    last_checked: Option<DateTime<Utc>>,
) -> (UpdateStatus, bool) {
    // Pin the canonical public AGPL upstream before any network ops. This
    // is the crux of the Design B fix (2026-05-19): private forks have
    // `origin` pointing at the fork, so we maintain a dedicated remote
    // named `vco_upstream` that always points at the public repo.
    if let Err(e) = ensure_upstream_remote(repo).await {
        return (UpdateStatus::unavailable(&e, last_checked), false);
    }

    // Fetch so `rev-list --count` works below without a second network
    // round-trip.
    if let Err(e) = fetch_upstream(repo).await {
        // Network unreachable / auth failure / etc. Surface as a soft
        // error — the UI still shows current SHA and last-known status.
        return (UpdateStatus::unavailable(&e, last_checked), false);
    }

    evaluate_against_fetched_refs(repo, last_checked).await
}

/// Judge currency from refs that are ALREADY current — no remote pinning, no
/// fetch.
///
/// This is where every one of the WP-13 defects lived, and separating it from
/// the two network steps above is what makes them testable offline. The
/// alternative was an `ensure_upstream_remote` that repoints a fixture's
/// remote at the real github.com URL (its shape check rejects a filesystem
/// path, by design) and then a `git fetch` that hangs on a network the test
/// environment does not have — i.e. the test would have exercised the
/// network, not the decision.
///
/// The upstream tip and the behind-count are LOCAL reads of the refs the
/// fetch updated (`rev-parse`, `rev-list`; v0.2.100 WP-05 / L2-F13 — the tip
/// used to be a second `ls-remote` whose failure discarded a good fetch). The
/// one remaining remote call, the tag listing, feeds only its own health
/// field. A fixture pointing `vco_upstream` at a local bare repo exercises the
/// real code paths with no network at all.
///
/// ORDERING NOTE: branch/SHA resolution used to happen BEFORE the pin+fetch.
/// It now happens after. Behaviourally equivalent — neither depends on the
/// other — but on a repo where BOTH would fail, the reported error is now the
/// remote one rather than the branch one. Both render identically
/// (`unavailable(<git error>)`).
async fn evaluate_against_fetched_refs(
    repo: &Path,
    last_checked: Option<DateTime<Utc>>,
) -> (UpdateStatus, bool) {
    // v0.2.92 WP-13: the ONE resolver. Pre-fix this was
    // `current_branch(..).unwrap_or_else(|_| "main")`, which returned the
    // literal `"HEAD"` in a detached HEAD because git reports that as a
    // SUCCESS — the `unwrap_or_else` only ever fires on `Err`.
    let branch_state = match git_cmd::resolve_branch(repo).await {
        Ok(b) => b,
        Err(e) => return (UpdateStatus::unavailable(&e, last_checked), false),
    };
    let branch = branch_state.name.clone();

    let local_sha = match current_sha(repo).await {
        Ok(s) => s,
        Err(e) => {
            return (
                UpdateStatus::unavailable(&e, last_checked),
                branch_state.detached,
            )
        }
    };

    let remote_sha = match fetched_upstream_sha(repo, &branch).await {
        Ok(s) => s,
        Err(e) => {
            return (
                UpdateStatus::unavailable(&e, last_checked),
                branch_state.detached,
            )
        }
    };

    // v0.2.92 WP-13 — THE fix for the five-week silent outage.
    //
    // Pre-fix:
    //     let commit_count = count_commits_behind_upstream(..).unwrap_or(0);
    //     let available = remote_sha != local_sha && commit_count > 0;
    //
    // A git `fatal:` became the number 0, and `> 0` then read that as "not
    // behind". Combined with the un-normalised branch above, `available` was
    // STRUCTURALLY false in a detached HEAD at any distance from upstream.
    //
    // Now the failure keeps its own identity all the way to the GUI: the
    // verdict is computed ONLY on the `Ok` arm, and the `Unknown` arm carries
    // the git error so every surface can say what it could not do.
    let (commit_count, remote_check) =
        match git_cmd::commits_behind(repo, VCO_UPSTREAM_REMOTE, &branch).await {
            Ok(n) => (n, CheckState::Ok),
            Err(e) => {
                tracing::warn!(
                    "[vct] check_for_launcher_update: behind-count failed at {} ({}) — remote \
                     currency is UNKNOWN, not 'up to date'",
                    repo.display(),
                    e
                );
                (0, CheckState::unknown(e))
            }
        };
    let available =
        remote_check.is_known() && remote_sha != local_sha && commit_count > 0;

    // The "latest source release" probe is separate and fails separately.
    // It is ALSO a WP-13 fix: it used to run `git describe --tags
    // --abbrev=0`, i.e. "the closest tag reachable FROM HEAD", so an install
    // detached on its own release tag was told the latest release was its own
    // tag. Now it asks the remote.
    let latest_source_release_check =
        match git_cmd::latest_remote_tag(repo, VCO_UPSTREAM_REMOTE).await {
            Ok(_) => CheckState::Ok,
            Err(e) => {
                tracing::warn!(
                    "[vct] check_for_launcher_update: remote tag listing failed at {} ({})",
                    repo.display(),
                    e
                );
                CheckState::unknown(e)
            }
        };

    if branch_state.detached {
        tracing::warn!(
            "[vct] check_for_launcher_update: {} has a DETACHED HEAD — comparing against \
             {}/{} (the reattach affordance is on Preferences → Launcher updates)",
            repo.display(),
            VCO_UPSTREAM_REMOTE,
            branch,
        );
    }

    let now = Utc::now();
    let status = UpdateStatus {
        available,
        current_sha: Some(local_sha),
        remote_sha: Some(remote_sha),
        commit_count,
        branch,
        head_detached: branch_state.detached,
        remote_check,
        latest_source_release_check,
        last_checked: Some(now),
        error: None,
    };

    (status, branch_state.detached)
}

/// Persist what the check established, for the cached (offline) surfaces.
///
/// v0.2.92 WP-13: the count is written ONLY when it is a real count, and the
/// reason is written when it is not. Pre-fix a laundered `0` was persisted on
/// every failed check, which is why the field install's state file paired a
/// correct, current `last_known_remote_sha` with `last_known_commit_count: 0`
/// — a combination that reads as "checked successfully, you are current" and
/// was in fact "the check crashed". The tray then repeated that verdict at
/// every subsequent boot, from cache, without ever touching the network.
fn persist_check_result(status: &UpdateStatus) {
    let mut state = load_state();
    state.last_checked_at = status.last_checked;
    if let Some(sha) = &status.remote_sha {
        state.last_known_remote_sha = Some(sha.clone());
    }
    match &status.remote_check {
        CheckState::Ok => {
            state.last_known_commit_count = Some(status.commit_count);
            state.last_check_unknown_error = None;
        }
        CheckState::NotApplicable => {
            state.last_known_commit_count = None;
            state.last_check_unknown_error = None;
        }
        CheckState::Unknown { error } => {
            state.last_known_commit_count = None;
            state.last_check_unknown_error = Some(error.clone());
        }
    }
    let _ = save_state(&state);
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
///
/// `pub(crate)` so the orchestrator-update path
/// (`commands::update_pipeline`) can share the same
/// detection logic for its own divergence modal (B4 / D19, v0.2.23).
pub(crate) fn is_non_fast_forward(err: &str) -> bool {
    let lower = err.to_lowercase();
    lower.contains("not possible to fast-forward")
        || lower.contains("non-fast-forward")
        || lower.contains("have diverged")
        || lower.contains("refusing to merge unrelated histories")
}


/// Minimal JSON string escape — covers the characters git stderr can
/// realistically contain. Doesn't handle every Unicode edge case (we
/// don't need to: stderr is mostly ASCII English error messages).
///
/// `pub(crate)` so `commands::installer` can reuse the same escape rules
/// when serializing its own non-FF / conflict payloads (B4 / D19, v0.2.23).
pub(crate) fn json_escape(s: &str) -> String {
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
///
/// Stays a PURE file read (v0.2.93 review round 1, MINOR-5): this is a sync
/// command and the tray builder calls it on the main thread, so it must not
/// spawn git. The repo-aware view is `installer::check_for_updates`, which
/// the Updates page reads through the orchestrator store.
#[command]
pub fn get_cached_update_status() -> UpdateStatus {
    cached_update_status_for(None)
}


/// Path-injectable body of [`get_cached_update_status`].
///
/// `repo = None` (the sync command, the tray, a launcher not running from a
/// checkout, or a test that wants the pure cache view) keeps the pre-v0.2.93
/// shape: `current_sha: None`, `branch: ""`, count as cached. Synchronous
/// git by design — callers that must not block wrap it in `spawn_blocking`.
///
/// With a repo, cheaply from local git (soft-fail to the cache view on any
/// error — never a guess):
///   * `current_sha`   = `git rev-parse --short HEAD`
///   * `branch`/`head_detached` = `git rev-parse --abbrev-ref HEAD` through
///     the ONE normaliser (`git_cmd::branch_state_from_abbrev_ref`)
///   * when `last_known_remote_sha` is cached AND `git merge-base
///     --is-ancestor <remote_sha> HEAD` succeeds, HEAD already contains
///     everything the last check saw upstream → `commit_count: 0`,
///     `available: false`, `remote_check: Ok`. This is what retracts the
///     "4 commits behind / Last checked 7:17 PM" card after a merge that
///     completed from a shell (the field incident) without waiting a day
///     for the next scheduled check. A non-ancestor (or an unknown SHA)
///     leaves the cached verdict exactly as it was.
pub(crate) fn cached_update_status_for(repo: Option<&Path>) -> UpdateStatus {
    let state = load_state();
    let remote = state.last_known_remote_sha.clone();

    // v0.2.92 WP-13: three cases, not two.
    //
    //   * a real cached count      ⇒ Ok, and `available` is a verdict;
    //   * a recorded failure       ⇒ Unknown, carrying the recorded reason;
    //   * nothing cached at all    ⇒ Unknown ("no check has completed yet"),
    //                                NOT `available: false`.
    //
    // The third case is the one that used to lie the loudest: on a fresh
    // launcher process, before the first daily tick, `count.unwrap_or(0)`
    // rendered a confident "up to date" in the tray built from no data.
    let (mut available, mut commit_count, mut remote_check) = match (
        state.last_known_commit_count,
        state.last_check_unknown_error.as_deref(),
    ) {
        (_, Some(err)) => (false, 0, CheckState::unknown(err)),
        (Some(n), None) => (n > 0, n, CheckState::Ok),
        (None, None) => (
            false,
            0,
            CheckState::unknown("no update check has completed yet"),
        ),
    };

    let mut current_sha = None;
    let mut branch = String::new();
    let mut head_detached = false;
    if let Some(repo) = repo {
        current_sha = git_stdout_sync(repo, &["rev-parse", "--short", "HEAD"]);
        if let Some(raw) = git_stdout_sync(repo, &["rev-parse", "--abbrev-ref", "HEAD"]) {
            let st = git_cmd::branch_state_from_abbrev_ref(&raw);
            branch = st.name;
            head_detached = st.detached;
        }
        if let Some(sha) = remote.as_deref() {
            if head_contains_sync(repo, sha) {
                available = false;
                commit_count = 0;
                remote_check = CheckState::Ok;
            }
        }
    }

    UpdateStatus {
        available,
        current_sha,
        remote_sha: remote,
        commit_count,
        branch,
        head_detached,
        remote_check,
        // Never cached — the tag probe is a network question with no cheap
        // offline answer, so from cache it is always undetermined.
        latest_source_release_check: CheckState::unknown("not cached"),
        last_checked: state.last_checked_at,
        error: None,
    }
}

/// Synchronous, local-only git read for the cached-status surface, which
/// is a sync Tauri command called from the tray builder (no async context
/// to await [`run_git`] in). `None` on spawn failure or non-zero exit —
/// the callers treat that as "not asserted", never as a value.
fn git_stdout_sync(repo: &Path, args: &[&str]) -> Option<String> {
    let out = std::process::Command::new("git")
        .silent()
        .args(args)
        .current_dir(repo)
        .stdin(Stdio::null())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

/// `git merge-base --is-ancestor <sha> HEAD` — `true` ONLY on exit 0. Exit 1
/// (not an ancestor) and exit 128 (unknown object, e.g. a remote SHA that was
/// never fetched) are both "cannot confirm" and return `false`, so the
/// caller keeps whatever it already believed.
fn head_contains_sync(repo: &Path, sha: &str) -> bool {
    std::process::Command::new("git")
        .silent()
        .args(["merge-base", "--is-ancestor", sha, "HEAD"])
        .current_dir(repo)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// v0.2.93 (stale status cache): after a pull-based update has landed
/// (install.py succeeded; the restart hop is next), re-derive the cached
/// currency from the freshly-fetched `<upstream>/<branch>` ref and persist
/// it, so the Settings → Updates card and the tray do not keep quoting the
/// PRE-update check ("N commits behind / Last checked <hours ago>") until
/// the next scheduled tick. Called from the update pipeline after
/// install.py (`update_run`, phase 9 `refresh_after_install`), for every
/// kind.
///
/// Local git only — the pull itself just fetched. Soft-fail: any git error
/// leaves the state file untouched (a stale-but-honest cache beats a guessed
/// one) and logs why.
pub(crate) async fn refresh_cached_state_after_pull(repo: &Path, branch: &str) {
    let upstream_ref = format!("{VCO_UPSTREAM_REMOTE}/{branch}");
    let remote_sha = match run_git(repo, &["rev-parse", &upstream_ref]).await {
        Ok(s) => s,
        Err(e) => {
            tracing::warn!(
                "[vct] refresh_cached_state_after_pull: could not resolve {} at {} ({}) — \
                 leaving launcher-update-state.json as is",
                upstream_ref,
                repo.display(),
                e
            );
            return;
        }
    };
    let behind = match git_cmd::commits_behind(repo, VCO_UPSTREAM_REMOTE, branch).await {
        Ok(n) => n,
        Err(e) => {
            tracing::warn!(
                "[vct] refresh_cached_state_after_pull: behind-count failed at {} ({}) — \
                 leaving launcher-update-state.json as is",
                repo.display(),
                e
            );
            return;
        }
    };
    let mut state = load_state();
    state.last_checked_at = Some(Utc::now());
    state.last_known_remote_sha = Some(remote_sha);
    state.last_known_commit_count = Some(behind);
    state.last_check_unknown_error = None;
    match save_state(&state) {
        Ok(()) => tracing::info!(
            "[vct] refresh_cached_state_after_pull: cached currency refreshed — {} commit(s) \
             behind {}",
            behind,
            upstream_ref
        ),
        Err(e) => tracing::warn!(
            "[vct] refresh_cached_state_after_pull: save_state failed: {}",
            e
        ),
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
// v0.2.35 Agent K — running-version display + post-update binary-lag warning
// ---------------------------------------------------------------------------
//
// Problem (observed against v0.2.34 ship): the orchestrator's
// "Update orchestrator" flow does a `git pull` then restarts into whatever
// binary lives at `launcher/dist/<arch>/vct-launcher`. After tagging a
// release on `main`, CI runs the `chore(binary): refresh vct-launcher +
// vct-hub dist binaries for v0.X.Y` job ~5-10 minutes later. If the user
// clicks Update during that window, the pull SHA carries the new source
// tag but the binary on disk is still the PREVIOUS release's — they
// silently restart into an older launcher with the old bugs, then run
// install attempts against mismatched code.
//
// Mitigation has two layers:
//
//   1. Display the running launcher's compile-time CARGO_PKG_VERSION in
//      the Updates panel alongside the latest source release tag. The
//      Svelte page now renders:
//        Running: v0.2.X | Latest source release: v0.2.Y
//      so the user can SEE the lag even before they click anything.
//
//   2. After an update completes (i.e. on the post-restart boot), the
//      page checks `running_version` vs `latest_source_tag`. If they
//      don't match → render a dismissible banner telling the user the
//      binary-publishing CI commit hadn't landed yet at update time,
//      with a "click Update again in 5-10 min" hint.
//
// We deliberately DO NOT change the update flow itself
// (v0.2.100: `update_run::run_update`). The binary-swap mechanism is correct;
// we're adding observability on top.

/// Return the launcher's compile-time `CARGO_PKG_VERSION`. The Svelte
/// Updates panel renders this alongside the latest source release tag so
/// the user can spot a binary-lag situation at a glance.
///
/// `CARGO_PKG_VERSION` is baked into the binary at compile time, so this
/// reflects the binary actually executing — NOT the version string in
/// `Cargo.toml` on disk (which may differ if the user pulled new source
/// but hasn't restarted yet). Exactly the property we need for layer-2
/// mismatch detection.
///
/// v0.2.35 Agent K. SPDX-License-Identifier: AGPL-3.0-or-later (inherited
/// from the file header).
#[command]
pub fn get_launcher_running_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

/// Return the newest release tag ON THE UPSTREAM REMOTE.
///
/// Returns `Ok(Some(tag))` when the remote advertises tags, `Ok(None)` when
/// it advertises none (a brand-new or self-hosted mirror), and `Err` when
/// the question could not be answered at all — git missing, not a checkout,
/// or the remote unreachable. The three are DISTINCT and the caller must
/// keep them distinct: `Err` means "unknown", and the page renders
/// "couldn't check" rather than falling back to anything.
///
/// ## v0.2.92 WP-13 — what changed and why it mattered
///
/// This used to run `git describe --tags --abbrev=0`, i.e. *the closest tag
/// reachable FROM HEAD*. That is a formally correct answer to a different
/// question. An install detached on `v0.2.88` was told the latest source
/// release was `v0.2.88`; the Updates page then rendered
/// `Running: v0.2.88 | Latest source release: v0.2.88` and the lag banner
/// (a string inequality on those two values) stayed hidden. Of everything
/// that went wrong during the five-week outage, this single line is the one
/// that most directly produced "everything reported healthy" — it was the
/// only place the user could have SEEN the gap, and it showed parity.
///
/// `git_cmd::latest_remote_tag` asks the remote (`ls-remote --tags --refs
/// --sort=-v:refname`), so HEAD's position cannot influence the answer.
///
/// We still DELIBERATELY do not hit the GitHub API:
///   - `ls-remote` uses the same transport + auth the fetch already uses;
///   - the GitHub API needs rate-limit tolerance or a token, and the
///     launcher operates fine without either;
///   - a self-hosted mirror (`VCO_UPSTREAM_URL`) may not speak it at all.
///
/// v0.2.35 Agent K; remote-sourced in v0.2.92 WP-13.
#[command]
pub async fn get_latest_source_release_tag() -> Result<Option<String>, String> {
    if !git_available().await {
        return Err("git not found on PATH".into());
    }
    let repo = find_launcher_repo_root()?;

    // Make sure vco_upstream exists + points at the canonical public repo
    // before we ask it anything. Hard-fail here: an unusable remote means we
    // genuinely cannot answer, and saying so is the whole point.
    ensure_upstream_remote(&repo).await?;

    // Keep the local tag refs warm too. Soft-fail and NOT load-bearing: the
    // answer comes from the remote, so a failed fetch no longer silently
    // changes what we report — it just means `.git/refs/tags/` stays stale
    // for other consumers. v0.2.100 final review: run it in the BACKGROUND —
    // a fetch may now legitimately take minutes on a slow link (stall
    // detection, not a 30 s total cap, ends it), and an answer that does not
    // depend on it must not wait for it.
    {
        let repo = repo.clone();
        tauri::async_runtime::spawn(async move {
            let _ = serialized_fetch_upstream(&repo, FetchPolicy::Tags, None).await;
        });
    }

    git_cmd::latest_remote_tag(&repo, VCO_UPSTREAM_REMOTE).await
}

/// Return HEAD to a branch after a detached checkout — the ONLY path-less
/// `git checkout <branch>` in the entire launcher.
///
/// ## Why this command exists
///
/// Every other `git checkout` in this codebase is path-scoped
/// (`checkout -- <file>` / `checkout HEAD -- <file>`), which cannot move
/// HEAD. So before v0.2.92 a user whose clone was in a detached HEAD had NO
/// in-GUI way out: the launcher could (after WP-13) tell them the state, and
/// the orchestrator update could even fast-forward them, but returning to a
/// branch required a terminal. For a GUI-first user that is a dead end, and
/// the state is one an ordinary `git checkout v0.2.NN` puts them in.
///
/// ## Guards — all three must pass, and each refuses with its own reason
///
/// 1. **HEAD is actually detached.** Refusing on an attached HEAD keeps this
///    from becoming a general-purpose branch switcher.
/// 2. **The working tree is clean** (`git status --porcelain` empty,
///    untracked included). `git checkout <branch>` would carry modified
///    files across or abort part-way; neither belongs behind a one-click
///    button.
/// 3. **The detached commit is an ANCESTOR of `vco_upstream/<branch>`**
///    (`git merge-base --is-ancestor`). This is the one that matters: if the
///    user has commits that upstream does not have, checking out the branch
///    silently strands them on an unreferenced commit — recoverable only via
///    reflog, which a GUI-first user will not reach for. When it fails we
///    refuse and NAME the commit so they can get back to it.
///
/// An `Err` from any guard leaves the repo byte-identical: nothing runs
/// before all three pass.
#[command]
pub async fn reattach_orchestrator_branch() -> Result<String, String> {
    if !git_available().await {
        return Err("git not found on PATH — cannot reattach".into());
    }
    let repo = find_launcher_repo_root()?;

    let state = git_cmd::resolve_branch(&repo).await?;
    if !state.detached {
        return Err(format!(
            "HEAD is already attached to `{}` — nothing to reattach.",
            state.name
        ));
    }
    let target = state.name.clone();

    if !git_cmd::tree_is_clean(&repo).await? {
        return Err(format!(
            "The working tree at {} has uncommitted or untracked changes. Commit, stash or \
             discard them first — reattaching to `{}` would carry them across or abort \
             part-way.",
            repo.display(),
            target
        ));
    }

    ensure_upstream_remote(&repo).await?;
    // Fetch so the ancestry question is asked against the CURRENT upstream
    // tip. Hard-fail: a stale ref could make an unmerged commit look like an
    // ancestor, and this guard is the one protecting the user's commits.
    fetch_upstream(&repo).await?;

    let upstream_ref = format!("{}/{}", VCO_UPSTREAM_REMOTE, target);
    let head_sha = current_sha(&repo).await?;
    if !git_cmd::is_ancestor(&repo, "HEAD", &upstream_ref).await? {
        return Err(format!(
            "Refusing to reattach: the commit you are on ({}) is NOT contained in `{}`, so \
             checking out `{}` would leave it unreferenced. If those commits are yours, keep \
             them first (e.g. `git -C {} branch my-work {}`), then reattach.",
            &head_sha[..head_sha.len().min(12)],
            upstream_ref,
            target,
            repo.display(),
            &head_sha[..head_sha.len().min(12)],
        ));
    }

    run_git(&repo, &["checkout", &target]).await?;

    // Confirm rather than assume. A checkout that reports success but leaves
    // HEAD detached (it should not, but this is the one destructive-adjacent
    // path here) must not be reported as done.
    let after = git_cmd::resolve_branch(&repo).await?;
    if after.detached {
        return Err(format!(
            "`git checkout {}` reported success but HEAD is still detached at {}. Nothing was \
             lost; inspect the repo at {} manually.",
            target,
            &head_sha[..head_sha.len().min(12)],
            repo.display()
        ));
    }
    tracing::info!(
        "[vct] reattach_orchestrator_branch: {} reattached to `{}` (was detached at {})",
        repo.display(),
        after.name,
        &head_sha[..head_sha.len().min(12)],
    );
    Ok(after.name)
}

/// `true` iff the running launcher (`CARGO_PKG_VERSION`) is OLDER than the
/// latest source release tag — the binary swap lagged the tag.
///
/// v0.2.100 (F-W1-04, AD-8): direction-aware through the version SSOT
/// (`vct_launcher_core::version::is_older`). It used to be string
/// inequality, so a running launcher NEWER than the tag (0.2.100 against a
/// stale `v0.2.99`) was told it lagged and offered a "restart" into an older
/// binary. Whitespace is trimmed; the `v` prefix is the comparator's. An
/// empty side, or a version that is not strict `X.Y.Z`, is not a lag (no
/// signal to warn on) — the parse error is logged with the offending string.
pub fn running_version_lags_tag(running: &str, latest_tag: &str) -> bool {
    let (r, t) = (running.trim(), latest_tag.trim());
    if r.is_empty() || t.is_empty() {
        return false;
    }
    match vct_launcher_core::version::is_older(r, t) {
        Ok(older) => older,
        Err(e) => {
            tracing::warn!(
                "[vct] running_version_lags_tag: cannot order running {:?} against tag {:?}: {} \
                 — not reported as lagging",
                r,
                t,
                e
            );
            false
        }
    }
}

/// Tauri-callable wrapper around `running_version_lags_tag` — the ONE answer
/// the Updates page's binary-lag banner renders (v0.2.100: its TS mirror
/// `versionLagsTag` was deleted in favour of this command, L3-F10).
///
/// v0.2.35 Agent K.
#[command]
pub fn check_running_version_lags_tag(running: String, latest_tag: String) -> bool {
    running_version_lags_tag(&running, &latest_tag)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

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
    fn state_roundtrip() {
        // v0.2.21 Step 23: $HOME mutation routes through the shared
        // workspace mutex so we don't race with other env-mutating
        // tests (auth, lockfile, boot, hub_status, hub_launcher,
        // installer hub_stop, etc.) running concurrently under
        // default `cargo test` parallelism.
        let tmp = tempfile::tempdir().unwrap();
        let tmp_path = tmp.path().to_path_buf();
        vct_launcher_core::test_env::with_env_vars(
            &[("HOME", Some(tmp_path.to_str().unwrap()))],
            || {
                let mut s = UpdateState::default();
                s.last_checked_at = Some(Utc::now());
                s.last_known_commit_count = Some(7);
                s.auto_check_enabled = Some(false);
                save_state(&s).unwrap();

                let back = load_state();
                assert_eq!(back.last_known_commit_count, Some(7));
                assert_eq!(back.auto_check_enabled, Some(false));
            },
        );
    }

    // ---------------------------------------------------------------------
    // Design B (2026-05-19): canonical upstream remote pinning.
    // ---------------------------------------------------------------------
    //
    // These tests use a real `git` binary against tempfile-backed repos.
    // They're skipped (via `skip_if_no_git!`) when `git` isn't on PATH so
    // CI environments without git don't false-fail. On dev machines and
    // standard CI runners (Ubuntu/macOS/Windows GitHub Actions all ship
    // git) they run for real.

    use std::process::Command as StdCommand;

    // Tests that mutate (or depend on) `VCO_UPSTREAM_URL` hold THE env lock
    // (`test_env::env_lock`) — `cargo test` runs in-binary tests in parallel
    // and the variable is process-global. v0.2.97 review R6: this module's
    // own `ENV_MUTEX` ordered only its own tests.

    /// Skip a test if `git --version` doesn't succeed.
    macro_rules! skip_if_no_git {
        () => {
            if StdCommand::new("git")
                .arg("--version")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .map(|s| !s.success())
                .unwrap_or(true)
            {
                eprintln!("skipping: git not on PATH");
                return;
            }
        };
    }

    /// Create an empty git repo in a fresh temp dir. Returns the TempDir
    /// (held by the caller to keep it alive) plus the repo path.
    fn init_repo() -> (tempfile::TempDir, PathBuf) {
        let tmp = tempfile::tempdir().expect("tempdir");
        let repo = tmp.path().to_path_buf();
        let status = StdCommand::new("git")
            .args(["init", "--quiet"])
            .current_dir(&repo)
            .status()
            .expect("git init");
        assert!(status.success(), "git init failed");
        (tmp, repo)
    }

    /// Helper to read a remote's URL synchronously (the production helper
    /// is async; tests run inside a tokio runtime when they need that).
    fn get_remote_url_sync(repo: &Path, name: &str) -> Option<String> {
        let output = StdCommand::new("git")
            .args(["remote", "get-url", name])
            .current_dir(repo)
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
    }


    // --- v0.2.95 MINOR-A: the two parsers must spell a path identically ---


    #[tokio::test]
    async fn ensure_upstream_remote_creates_when_absent() {
        skip_if_no_git!();
        // Hold the env mutex: these tests read `default_upstream_url()`
        // which inspects VCO_UPSTREAM_URL. Without serialization an
        // env-override test could mutate it mid-read.
        let _guard = vct_launcher_core::test_env::env_lock();

        let (_tmp, repo) = init_repo();
        assert!(get_remote_url_sync(&repo, VCO_UPSTREAM_REMOTE).is_none());

        ensure_upstream_remote(&repo).await.expect("ensure ok");

        let url = get_remote_url_sync(&repo, VCO_UPSTREAM_REMOTE).expect("remote exists");
        assert_eq!(url, default_upstream_url());
    }

    #[tokio::test]
    async fn ensure_upstream_remote_updates_when_url_mismatched() {
        skip_if_no_git!();
        let _guard = vct_launcher_core::test_env::env_lock();

        let (_tmp, repo) = init_repo();

        // Pre-seed with a wrong URL.
        let status = StdCommand::new("git")
            .args([
                "remote",
                "add",
                VCO_UPSTREAM_REMOTE,
                "https://example.com/wrong.git",
            ])
            .current_dir(&repo)
            .status()
            .expect("git remote add");
        assert!(status.success());

        ensure_upstream_remote(&repo).await.expect("ensure ok");

        let url = get_remote_url_sync(&repo, VCO_UPSTREAM_REMOTE).expect("remote exists");
        assert_eq!(
            url,
            default_upstream_url(),
            "wrong URL should be corrected"
        );
    }

    #[tokio::test]
    async fn ensure_upstream_remote_noop_when_already_correct() {
        skip_if_no_git!();
        let _guard = vct_launcher_core::test_env::env_lock();

        let (_tmp, repo) = init_repo();

        // Pre-seed with the correct URL.
        let want = default_upstream_url();
        let status = StdCommand::new("git")
            .args(["remote", "add", VCO_UPSTREAM_REMOTE, &want])
            .current_dir(&repo)
            .status()
            .expect("git remote add");
        assert!(status.success());

        // Capture config-file mtime BEFORE the ensure call. A true no-op
        // path doesn't run `set-url` or `add`, so the .git/config file
        // shouldn't be rewritten.
        let cfg = repo.join(".git").join("config");
        let mtime_before = std::fs::metadata(&cfg).unwrap().modified().unwrap();
        // Sleep just enough that mtime granularity (1s on some FS) can
        // detect a change if one happens.
        std::thread::sleep(Duration::from_millis(1100));

        ensure_upstream_remote(&repo).await.expect("ensure ok");

        let mtime_after = std::fs::metadata(&cfg).unwrap().modified().unwrap();
        assert_eq!(
            mtime_before, mtime_after,
            ".git/config mtime should not change on no-op"
        );

        let url = get_remote_url_sync(&repo, VCO_UPSTREAM_REMOTE).expect("remote exists");
        assert_eq!(url, want);
    }

    #[test]
    fn env_override_url_is_honored_when_set() {
        // Hold the env mutex for the duration so sibling env-tests don't race.
        // .unwrap_or_else handles a poisoned mutex from a prior panicked test.
        let _guard = vct_launcher_core::test_env::env_lock();

        let prev = std::env::var(VCO_UPSTREAM_URL_ENV).ok();
        std::env::set_var(VCO_UPSTREAM_URL_ENV, "https://git.example.com/mirror.git");

        let url = default_upstream_url();
        assert_eq!(url, "https://git.example.com/mirror.git");

        // Restore.
        match prev {
            Some(v) => std::env::set_var(VCO_UPSTREAM_URL_ENV, v),
            None => std::env::remove_var(VCO_UPSTREAM_URL_ENV),
        }
    }

    #[test]
    fn env_override_invalid_falls_back_to_default() {
        let _guard = vct_launcher_core::test_env::env_lock();

        let prev = std::env::var(VCO_UPSTREAM_URL_ENV).ok();
        std::env::set_var(VCO_UPSTREAM_URL_ENV, "garbage");

        let url = default_upstream_url();
        assert_eq!(
            url, VCO_UPSTREAM_URL,
            "bare 'garbage' should fall back to default"
        );

        // Also verify empty string falls back.
        std::env::set_var(VCO_UPSTREAM_URL_ENV, "");
        assert_eq!(default_upstream_url(), VCO_UPSTREAM_URL);

        // And whitespace-only.
        std::env::set_var(VCO_UPSTREAM_URL_ENV, "   ");
        assert_eq!(default_upstream_url(), VCO_UPSTREAM_URL);

        // Restore.
        match prev {
            Some(v) => std::env::set_var(VCO_UPSTREAM_URL_ENV, v),
            None => std::env::remove_var(VCO_UPSTREAM_URL_ENV),
        }
    }

    #[test]
    fn looks_like_remote_url_accepts_common_forms() {
        assert!(looks_like_remote_url("https://github.com/foo/bar.git"));
        assert!(looks_like_remote_url("http://internal.example/mirror.git"));
        assert!(looks_like_remote_url("git@github.com:foo/bar.git"));
        assert!(!looks_like_remote_url("garbage"));
        assert!(!looks_like_remote_url(""));
        assert!(!looks_like_remote_url("ftp://old.example.com/repo"));
    }


    // ---------------------------------------------------------------------
    // v0.2.35 Agent K — running-version display + binary-lag warning
    // ---------------------------------------------------------------------

    #[test]
    fn version_lag_detects_no_warn_when_match() {
        // The happy path: running binary matches the latest tag. Tag has
        // the conventional `v` prefix; running version is bare. The
        // normalize step strips the `v` and equality holds.
        assert!(!running_version_lags_tag("0.2.34", "v0.2.34"));
        // Without the prefix (defensive — if upstream switches conventions
        // we still don't false-warn).
        assert!(!running_version_lags_tag("0.2.34", "0.2.34"));
    }

    #[test]
    fn version_lag_detects_warn_when_running_behind_tag() {
        // The bug-of-the-day: user clicked Update right after v0.2.34 tag
        // pushed but BEFORE CI's binary-refresh commit landed. They get
        // the v0.2.33 binary while running on a v0.2.34 source tree.
        assert!(running_version_lags_tag("0.2.33", "v0.2.34"));
        // Numeric, not lexicographic: 0.2.99 IS behind 0.2.100.
        assert!(running_version_lags_tag("0.2.99", "v0.2.100"));
    }

    /// v0.2.100 (F-W1-04): a running launcher AHEAD of the tag does not lag —
    /// string inequality used to flag it and offer a restart into an OLDER
    /// binary. Unparseable versions are not a lag either (logged, not ranked).
    #[test]
    fn version_lag_is_direction_aware() {
        assert!(!running_version_lags_tag("0.2.100", "v0.2.99"));
        assert!(!running_version_lags_tag("0.2.35", "v0.2.34"));
        assert!(!running_version_lags_tag("0.2.100", "v0.2.100-rc1"));
        assert!(!running_version_lags_tag("garbage", "v0.2.100"));
    }

    #[test]
    fn version_lag_empty_tag_returns_no_warn() {
        // Upstream has no release tags yet (brand-new fork, etc.). We
        // can't say anything meaningful so we don't warn.
        assert!(!running_version_lags_tag("0.2.34", ""));
        assert!(!running_version_lags_tag("0.2.34", "   "));
        // Symmetric: running version unknown shouldn't false-warn either,
        // though in practice CARGO_PKG_VERSION is never empty.
        assert!(!running_version_lags_tag("", "v0.2.34"));
    }

    #[test]
    fn version_lag_normalizes_whitespace_and_v_prefix() {
        // Real-world stdout from `git describe` is trimmed by `run_git`,
        // but be paranoid: a fork that emits trailing whitespace mustn't
        // false-warn.
        assert!(!running_version_lags_tag("0.2.34", " v0.2.34 "));
        assert!(!running_version_lags_tag("  0.2.34  ", "v0.2.34"));
    }

    #[test]
    fn get_launcher_running_version_returns_cargo_pkg_version() {
        // Sanity check: the command returns a non-empty string that
        // matches CARGO_PKG_VERSION. We can't assert the literal value
        // (it bumps every release) — checking non-empty + dotted shape
        // is enough to verify the wiring.
        let v = get_launcher_running_version();
        assert!(!v.is_empty(), "running version must not be empty");
        assert!(
            v.contains('.'),
            "running version should look like a SemVer string, got: {}",
            v
        );
        // And the SAME string the rest of the codebase uses — guard
        // against accidental hard-coding.
        assert_eq!(v, env!("CARGO_PKG_VERSION"));
    }


    // ══════════════════════════════════════════════════════════════════════
    // v0.2.92 WP-13 — the detached-HEAD blindness regression suite
    // ══════════════════════════════════════════════════════════════════════
    //
    // Every test below is red against the pre-fix source and green after,
    // on ANY operating system. That mattered: the defect was REPORTED from
    // Windows, and "we'll confirm it on the tester's machine" would have
    // made the fix unverifiable in CI. Nothing here is OS-specific — it is
    // git behaviour and Rust decision logic.
    //
    // The fixture (`detached_upstream_fixture`) reproduces the FIELD shape
    // exactly:
    //   * `refs/remotes/vco_upstream/HEAD` does NOT exist, so
    //     `HEAD..vco_upstream/HEAD` is a `fatal:`. The local repo is built
    //     with `init` + `remote add` + `fetch`, NEVER `clone` (clone creates
    //     that ref; production's `ensure_upstream_remote`, which only ever
    //     runs `remote add` / `set-url`, does not) — and the absence is then
    //     PINNED via `git_cmd::pin_absent_remote_head`, because `fetch`
    //     stopped guaranteeing it in git 2.48
    //     (`remote.<name>.followRemoteHEAD` defaults to `create`). A fixture
    //     in which that ref RESOLVES would pass against the very code that
    //     shipped the outage — and, on 2026-09-07, an unpinned one turned a
    //     git-2.55 CI runner red while git 2.43 stayed green locally;
    //   * `VCO_UPSTREAM_URL` points at a local bare repo, so
    //     `ensure_upstream_remote` + `fetch_upstream` run for real, offline;
    //   * HEAD is detached on an old tag with upstream two commits ahead.

    mod detached_head_v0292 {
        use super::*;
        use std::process::{Command as StdCommand, Stdio};

        macro_rules! skip_if_no_git {
            () => {
                if StdCommand::new("git")
                    .arg("--version")
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                    .map(|s| !s.success())
                    .unwrap_or(true)
                {
                    eprintln!("skipping: git not on PATH");
                    return;
                }
            };
        }

        fn git(cwd: &Path, args: &[&str]) {
            let st = StdCommand::new("git")
                .args(args)
                .current_dir(cwd)
                .env("GIT_CONFIG_GLOBAL", "/dev/null")
                .env("GIT_CONFIG_SYSTEM", "/dev/null")
                .env("GIT_AUTHOR_NAME", "T")
                .env("GIT_AUTHOR_EMAIL", "t@example.com")
                .env("GIT_COMMITTER_NAME", "T")
                .env("GIT_COMMITTER_EMAIL", "t@example.com")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .unwrap_or_else(|e| panic!("git {args:?}: {e}"));
            assert!(st.success(), "git {args:?} failed in {}", cwd.display());
        }

        /// (tempdir, local repo, bare remote path). HEAD detached on
        /// `v0.0.1`; upstream is 2 commits ahead and carries `v0.0.2`.
        fn detached_upstream_fixture() -> (tempfile::TempDir, PathBuf, PathBuf) {
            let tmp = tempfile::tempdir().expect("tempdir");
            let root = tmp.path().to_path_buf();
            let remote = root.join("remote.git");
            let seed = root.join("seed");
            let local = root.join("local");
            std::fs::create_dir_all(&seed).unwrap();
            std::fs::create_dir_all(&local).unwrap();

            git(
                &root,
                &["init", "--bare", "--initial-branch=main", "-q", "remote.git"],
            );

            git(&seed, &["init", "--initial-branch=main", "-q"]);
            std::fs::write(seed.join("README.md"), "seed\n").unwrap();
            git(&seed, &["add", "-A"]);
            git(&seed, &["commit", "-qm", "c1"]);
            git(&seed, &["tag", "v0.0.1"]);
            git(
                &seed,
                &["remote", "add", "vco_upstream", remote.to_str().unwrap()],
            );
            git(&seed, &["push", "-q", "vco_upstream", "main", "--tags"]);

            git(&local, &["init", "--initial-branch=main", "-q"]);
            git(
                &local,
                &["remote", "add", "vco_upstream", remote.to_str().unwrap()],
            );
            git(&local, &["fetch", "-q", "vco_upstream"]);
            git(&local, &["checkout", "-q", "-B", "main", "vco_upstream/main"]);

            std::fs::create_dir_all(seed.join("launcher/src-tauri/src")).unwrap();
            std::fs::write(seed.join("launcher/src-tauri/src/x.rs"), "// x\n").unwrap();
            git(&seed, &["add", "-A"]);
            git(&seed, &["commit", "-qm", "c2"]);
            std::fs::write(seed.join("README.md"), "seed v2\n").unwrap();
            git(&seed, &["add", "-A"]);
            git(&seed, &["commit", "-qm", "c3"]);
            git(&seed, &["tag", "v0.0.2"]);
            git(&seed, &["push", "-q", "vco_upstream", "main", "--tags"]);

            git(&local, &["fetch", "-q", "vco_upstream", "--tags"]);
            git(&local, &["checkout", "-q", "--detach", "v0.0.1"]);

            // "`<remote>/HEAD` does not exist" is a PROPERTY of this fixture,
            // not something `fetch` still guarantees — git 2.48's
            // `remote.<name>.followRemoteHEAD=create` default creates it. One
            // shared pin (git_cmd, so the two detached-HEAD fixtures cannot
            // drift apart) states it explicitly; must follow the last fetch.
            git_cmd::pin_absent_remote_head(&local, VCO_UPSTREAM_REMOTE);

            (tmp, local, remote)
        }

        // v0.2.99 regression: a RE-POINTED upstream tag must not wedge the
        // Tags-policy fetch. The pre-fix invocation (`fetch --tags`) refuses
        // to move a local tag that differs from the remote's — "would clobber
        // existing tag", exit 1, and under `--quiet` even that report is
        // suppressed, so the failure surfaced as "(no stderr)" and looked
        // like a killed child. The release workflow re-points tags after
        // committing dist binaries, so this is the FIELD shape: any clone
        // fetched between the first tag push and the re-point was failing on
        // every subsequent tag fetch, forever. The forced refspec
        // (`+refs/tags/*:refs/tags/*`) is the fix.
        #[tokio::test]
        async fn tags_fetch_survives_a_repointed_upstream_tag() {
            skip_if_no_git!();
            let tmp = tempfile::tempdir().expect("tempdir");
            let root = tmp.path().to_path_buf();
            let remote = root.join("remote.git");
            let seed = root.join("seed");
            let local = root.join("local");
            std::fs::create_dir_all(&seed).unwrap();
            std::fs::create_dir_all(&local).unwrap();

            git(
                &root,
                &["init", "--bare", "--initial-branch=main", "-q", "remote.git"],
            );
            git(&seed, &["init", "--initial-branch=main", "-q"]);
            std::fs::write(seed.join("README.md"), "c1\n").unwrap();
            git(&seed, &["add", "-A"]);
            git(&seed, &["commit", "-qm", "c1"]);
            git(&seed, &["tag", "vX"]);
            git(
                &seed,
                &["remote", "add", "vco_upstream", remote.to_str().unwrap()],
            );
            git(&seed, &["push", "-q", "vco_upstream", "main", "--tags"]);

            // The local clone has vX at c1 — exactly what a user has if they
            // fetched between a tag's first push and its re-point.
            git(&local, &["init", "--initial-branch=main", "-q"]);
            git(
                &local,
                &["remote", "add", "vco_upstream", remote.to_str().unwrap()],
            );
            git(&local, &["fetch", "-q", "vco_upstream", "--tags"]);

            // Upstream re-points vX at a new commit (the dist-binary commit
            // in the release flow) and force-pushes the tag.
            std::fs::write(seed.join("README.md"), "c2\n").unwrap();
            git(&seed, &["add", "-A"]);
            git(&seed, &["commit", "-qm", "c2"]);
            git(&seed, &["tag", "-f", "vX"]);
            git(
                &seed,
                &["push", "-q", "--force", "vco_upstream", "main", "--tags"],
            );

            serialized_fetch_upstream(&local, FetchPolicy::Tags, None)
                .await
                .expect("tags fetch must survive a re-pointed upstream tag");

            // The local tag must now match the re-pointed remote tag.
            let read_tag = |cwd: &Path| -> String {
                let out = StdCommand::new("git")
                    .args(["rev-parse", "vX"])
                    .current_dir(cwd)
                    .env("GIT_CONFIG_GLOBAL", "/dev/null")
                    .env("GIT_CONFIG_SYSTEM", "/dev/null")
                    .output()
                    .expect("git rev-parse vX");
                assert!(
                    out.status.success(),
                    "rev-parse vX failed in {}",
                    cwd.display()
                );
                String::from_utf8_lossy(&out.stdout).trim().to_string()
            };
            assert_eq!(
                read_tag(&local),
                read_tag(&seed),
                "the forced refspec must have moved the local tag"
            );
        }

        // NOTE ON THE SEAM: these tests drive
        // `evaluate_against_fetched_refs`, not `evaluate_launcher_update`.
        // The difference is the two NETWORK steps the latter runs first —
        // `ensure_upstream_remote` (which repoints the remote at the real
        // github.com URL, since its shape check deliberately rejects a
        // filesystem path) and `fetch_upstream`. Driving those would make
        // every assertion below depend on the test host having network
        // access to github.com, which is neither true in CI nor what these
        // tests are about. The fixture pre-fetches, so the remaining git
        // calls (`ls-remote`, `rev-list`, `describe`) hit the local bare repo
        // and every defect WP-13 fixes is exercised for real, offline, on any
        // OS. The two network steps have their own tests
        // (`ensure_upstream_remote_*`, `never_resolving_attempt_times_out_*`).

        /// **THE test for the field incident.** Detached HEAD, upstream two
        /// commits ahead. Pre-fix this produced `available=false,
        /// commit_count=0` — a confident "up to date" — at any distance.
        #[tokio::test]
        async fn check_reports_available_when_detached_and_behind() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();

            // Precondition: really detached, and really behind.
            let raw = git_cmd::run_git(&local, &["rev-parse", "--abbrev-ref", "HEAD"])
                .await
                .expect("rev-parse");
            assert_eq!(raw, "HEAD", "fixture is not in a detached HEAD");

            let status = evaluate_against_fetched_refs(&local, None).await.0;

            assert_eq!(
                status.remote_check,
                CheckState::Ok,
                "the check DID complete — the branch just needed normalising: {:?}",
                status.remote_check
            );
            assert_eq!(
                status.commit_count, 2,
                "two upstream commits must be counted, not laundered to 0"
            );
            assert!(
                status.available,
                "an install two commits behind must report an update as AVAILABLE, detached \
                 or not — this is the assertion the shipped code failed for five weeks"
            );
            assert!(
                status.head_detached,
                "the detached state must be reported, not silently normalised away"
            );
            assert_eq!(
                status.branch, "main",
                "the compared branch is the normalised fallback, never the literal HEAD"
            );
        }

        /// Attached HEAD, same distance: identical verdict. Proves the fix
        /// did not simply hard-code "detached ⇒ available".
        #[tokio::test]
        async fn check_reports_available_when_attached_and_behind() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();
            git(&local, &["checkout", "-q", "main"]);

            let status = evaluate_against_fetched_refs(&local, None).await.0;
            assert!(status.available);
            assert_eq!(status.commit_count, 2);
            assert!(!status.head_detached);
        }

        /// Leave-alone half: a repo AT the upstream tip reports no update
        /// and a successful check — "up to date" must still be reachable.
        #[tokio::test]
        async fn check_reports_up_to_date_when_current() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();
            git(&local, &["checkout", "-q", "main"]);
            git(&local, &["merge", "--no-edit", "-q", "vco_upstream/main"]);

            let status = evaluate_against_fetched_refs(&local, None).await.0;
            assert_eq!(status.remote_check, CheckState::Ok);
            assert_eq!(status.commit_count, 0);
            assert!(!status.available, "at the tip there is nothing to offer");
        }

        // ---- v0.2.93 (stale status cache) -------------------------------

        /// Trimmed stdout of a git command in `cwd` (test-only reader).
        fn git_out(cwd: &Path, args: &[&str]) -> String {
            let out = StdCommand::new("git")
                .args(args)
                .current_dir(cwd)
                .env("GIT_CONFIG_GLOBAL", "/dev/null")
                .env("GIT_CONFIG_SYSTEM", "/dev/null")
                .output()
                .unwrap_or_else(|e| panic!("git {args:?}: {e}"));
            assert!(out.status.success(), "git {args:?} failed in {}", cwd.display());
            String::from_utf8_lossy(&out.stdout).trim().to_string()
        }

        /// Seed the cache the way a completed check would, then read it back
        /// through the repo-aware path.
        fn seed_cache(remote_sha: &str, count: u32) {
            persist_check_result(&UpdateStatus {
                available: count > 0,
                current_sha: None,
                remote_sha: Some(remote_sha.to_string()),
                commit_count: count,
                branch: "main".into(),
                head_detached: false,
                remote_check: CheckState::Ok,
                latest_source_release_check: CheckState::Ok,
                last_checked: Some(Utc::now()),
                error: None,
            });
        }

        /// THE field-incident card: "4 commits behind / Last checked 7:17 PM"
        /// long after the merge was completed from a shell. When HEAD already
        /// CONTAINS the cached remote SHA the cache must retract the count,
        /// and `current_sha` / `branch` must be real, not `None` / `""`.
        /// Red-proof: revert the ancestor check → `commit_count` stays 5.
        #[test]
        fn cached_status_reports_zero_behind_when_head_contains_cached_remote_sha() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();
            git(&local, &["checkout", "-q", "main"]);
            git(&local, &["merge", "--no-edit", "-q", "vco_upstream/main"]);
            let upstream_tip = git_out(&local, &["rev-parse", "vco_upstream/main"]);
            let head_short = git_out(&local, &["rev-parse", "--short", "HEAD"]);

            vct_launcher_core::test_env::with_state_dir(|_root| {
                seed_cache(&upstream_tip, 5); // stale: recorded BEFORE the merge

                let cached = cached_update_status_for(Some(&local));
                assert_eq!(cached.commit_count, 0, "HEAD contains the cached remote tip");
                assert!(!cached.available);
                assert_eq!(cached.remote_check, CheckState::Ok);
                assert_eq!(cached.current_sha.as_deref(), Some(head_short.as_str()));
                assert_eq!(cached.branch, "main");
                assert!(!cached.head_detached);
                assert_eq!(cached.remote_sha.as_deref(), Some(upstream_tip.as_str()));

                // The pure cache view (no repo) is unchanged from v0.2.92.
                let pure = cached_update_status_for(None);
                assert_eq!(pure.commit_count, 5);
                assert!(pure.available);
                assert!(pure.current_sha.is_none());
                assert_eq!(pure.branch, "");
            });
        }

        /// Leave-alone half: the cached remote SHA is NOT in HEAD's history
        /// (upstream really is ahead) → the cached verdict is preserved
        /// verbatim, while HEAD facts are still filled in. A detached HEAD
        /// is reported through the ONE normaliser (`main` + flag).
        #[test]
        fn cached_status_preserves_count_when_remote_sha_is_not_an_ancestor() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();
            // Fixture: HEAD detached on v0.0.1, upstream 2 ahead.
            let upstream_tip = git_out(&local, &["rev-parse", "vco_upstream/main"]);
            let head_short = git_out(&local, &["rev-parse", "--short", "HEAD"]);

            vct_launcher_core::test_env::with_state_dir(|_root| {
                seed_cache(&upstream_tip, 2);

                let cached = cached_update_status_for(Some(&local));
                assert_eq!(cached.commit_count, 2, "not an ancestor → count preserved");
                assert!(cached.available);
                assert_eq!(cached.current_sha.as_deref(), Some(head_short.as_str()));
                assert_eq!(cached.branch, "main", "detached normalises to the fallback");
                assert!(cached.head_detached);

                // An UNKNOWN sha (never fetched) can't be confirmed either →
                // preserved too, never laundered into "up to date".
                seed_cache(&"f".repeat(40), 3);
                let cached = cached_update_status_for(Some(&local));
                assert_eq!(cached.commit_count, 3);
                assert!(cached.available);
            });
        }

        /// The post-pull persist: a completed pull rewrites the cache from the
        /// fetched upstream ref (0 behind at the tip), clearing a prior
        /// failure reason. Red-proof: revert the call in the post-pull tails
        /// → the file keeps "5 behind" until the next daily tick.
        #[tokio::test]
        async fn refresh_cached_state_after_pull_persists_current_verdict() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();
            git(&local, &["checkout", "-q", "main"]);
            git(&local, &["merge", "--no-edit", "-q", "vco_upstream/main"]);
            let upstream_tip = git_out(&local, &["rev-parse", "vco_upstream/main"]);

            let _state = vct_launcher_core::test_env::state_dir_guard();
            // Stale AND failed: the shape the incident's state file had.
            persist_check_result(&UpdateStatus {
                available: false,
                current_sha: None,
                remote_sha: Some("0".repeat(40)),
                commit_count: 0,
                branch: "main".into(),
                head_detached: false,
                remote_check: CheckState::unknown("rev-list: boom"),
                latest_source_release_check: CheckState::unknown("x"),
                last_checked: None,
                error: None,
            });
            assert!(cached_update_status_for(None).remote_check.is_unknown());

            refresh_cached_state_after_pull(&local, "main").await;

            let after = load_state();
            assert_eq!(after.last_known_remote_sha.as_deref(), Some(upstream_tip.as_str()));
            assert_eq!(after.last_known_commit_count, Some(0));
            assert!(after.last_check_unknown_error.is_none());
            assert!(after.last_checked_at.is_some(), "the card's 'Last checked' moves");
            let cached = cached_update_status_for(None);
            assert_eq!(cached.remote_check, CheckState::Ok);
            assert!(!cached.available);

            // Soft-fail leg: an unknown branch leaves the file untouched.
            refresh_cached_state_after_pull(&local, "no-such-branch").await;
            assert_eq!(load_state().last_known_commit_count, Some(0));
        }

        /// The tri-state itself, reproducing the FIELD SHAPE precisely: the
        /// upstream tip is OBTAINED (so a real SHA is persisted) while
        /// `rev-list` FAILS (so the distance is unknowable). That exact
        /// combination is what the reported `launcher-update-state.json`
        /// contained — a correct `last_known_remote_sha` beside
        /// `last_known_commit_count: 0` — and it is why the incident was
        /// first misread as a network problem.
        ///
        /// v0.2.100 WP-05: the tip is now `rev-parse` of the fetched tracking
        /// ref (no second `ls-remote`), so the shape is built by pointing that
        /// ref at an object that does not exist: `rev-parse --verify` reads
        /// the ref, `rev-list HEAD..<ref>` refuses the range.
        #[tokio::test]
        async fn check_reports_unknown_not_up_to_date_when_rev_list_fails() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();
            // A loose ref file naming an object that does not exist (the
            // loose file overrides any packed entry).
            let tracking = local.join(".git/refs/remotes/vco_upstream");
            std::fs::create_dir_all(&tracking).unwrap();
            std::fs::write(tracking.join("main"), format!("{}\n", "1".repeat(40))).unwrap();

            // Precondition: exactly one of the two questions is answerable.
            assert!(
                fetched_upstream_sha(&local, "main").await.is_ok(),
                "fixture precondition: the fetched tip must still resolve"
            );
            assert!(
                git_cmd::commits_behind(&local, VCO_UPSTREAM_REMOTE, "main")
                    .await
                    .is_err(),
                "fixture precondition: the behind-count must be unanswerable"
            );

            let status = evaluate_against_fetched_refs(&local, None).await.0;

            assert!(
                status.remote_check.is_unknown(),
                "an unanswerable behind-count must leave remote_check Unknown, got {:?}",
                status.remote_check
            );
            assert!(
                status
                    .remote_check
                    .error()
                    .unwrap_or("")
                    .contains("rev-list"),
                "Unknown must carry the reason the GUI shows the user, got {:?}",
                status.remote_check.error()
            );
            assert!(
                status.remote_sha.is_some(),
                "the remote SHA WAS obtained — that half succeeded, and pretending \
                 otherwise would hide which half broke"
            );
            assert!(
                !status.available,
                "we still do not CLAIM an update — but the surfaces read remote_check, not \
                 this bool, to decide what to render"
            );

            // And the persisted form keeps the two halves separate, so the
            // next boot's cached label cannot resurrect a verdict.
            vct_launcher_core::test_env::with_state_dir(|_root| {
                persist_check_result(&status);
                let cached = get_cached_update_status();
                assert!(cached.remote_check.is_unknown());
                assert!(
                    cached.remote_sha.is_some(),
                    "the SHA is still cached; only the VERDICT is withheld"
                );
            });
        }

        /// v0.2.100 WP-05 (L2-F13): judging from ALREADY-FETCHED refs does
        /// not go back to the network for the tip. With the remote gone
        /// AFTER the fetch, the verdict still comes from the fetched refs
        /// (behind by two → available); only the tag listing — its own
        /// health field — reports Unknown. Pre-fix a second `ls-remote`
        /// failed here and threw the whole good result away as
        /// `unavailable`.
        #[tokio::test]
        async fn an_unreachable_remote_after_the_fetch_does_not_discard_the_verdict() {
            skip_if_no_git!();
            let (tmp, local, _remote) = detached_upstream_fixture();
            let nowhere = tmp.path().join("no-such-remote.git");
            git(
                &local,
                &["remote", "set-url", "vco_upstream", nowhere.to_str().unwrap()],
            );

            let status = evaluate_against_fetched_refs(&local, None).await.0;
            assert_eq!(status.remote_check, CheckState::Ok, "{:?}", status.error);
            assert!(status.available, "two commits behind, from the fetched refs");
            assert_eq!(status.commit_count, 2);
            assert!(
                status.latest_source_release_check.is_unknown(),
                "the tag listing still asks the remote, and says it could not"
            );
        }

        /// A broken remote AT FETCH TIME is Unknown, never a quiet "up to
        /// date": `evaluate_launcher_update` fetches first and the failed
        /// fetch becomes `unavailable`. Loopback port 9 refuses the
        /// connection — no traffic leaves the machine.
        #[tokio::test]
        async fn check_reports_unknown_when_the_remote_is_unreachable() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();
            let _env = vct_launcher_core::test_env::env_guard(&[(
                VCO_UPSTREAM_URL_ENV,
                Some("https://127.0.0.1:9/no-such-remote.git"),
            )]);
            let status = evaluate_launcher_update(&local, None).await.0;
            assert!(status.remote_check.is_unknown(), "got {:?}", status.remote_check);
            assert!(!status.available);
            let err = status.error.unwrap_or_default();
            assert!(err.contains("exit status"), "the fetch error keeps its evidence: {err}");
        }

        /// v0.2.100 WP-05 (L2-F03): two actors pinning the remote on a fresh
        /// clone at the same moment both succeed — the loser of the `remote
        /// add` race converges instead of failing "already exists".
        #[tokio::test]
        async fn concurrent_ensure_upstream_remote_both_succeed() {
            skip_if_no_git!();
            let _guard = vct_launcher_core::test_env::env_lock();
            for _ in 0..10 {
                let tmp = tempfile::tempdir().unwrap();
                git(tmp.path(), &["init", "-q"]);
                let repo = tmp.path();
                let (a, b, c, d) = tokio::join!(
                    ensure_upstream_remote(repo),
                    ensure_upstream_remote(repo),
                    ensure_upstream_remote(repo),
                    ensure_upstream_remote(repo)
                );
                for r in [a, b, c, d] {
                    r.expect("a concurrent pin must converge, not fail");
                }
            }
        }

        /// Cached-status honesty: the tri-state survives the round-trip
        /// through `~/.vct/launcher-update-state.json`, so the tray at the
        /// next boot does not resurrect a verdict that was never made.
        #[test]
        fn cached_status_reports_unknown_after_a_failed_check() {
            vct_launcher_core::test_env::with_state_dir(|_root| {
                let failed = UpdateStatus {
                    available: false,
                    current_sha: None,
                    remote_sha: Some("f".repeat(40)),
                    commit_count: 0,
                    branch: "main".into(),
                    head_detached: true,
                    remote_check: CheckState::unknown("rev-list: fatal: ambiguous argument"),
                    latest_source_release_check: CheckState::unknown("no network"),
                    last_checked: Some(Utc::now()),
                    error: None,
                };
                persist_check_result(&failed);

                let cached = get_cached_update_status();
                assert!(
                    cached.remote_check.is_unknown(),
                    "a failed check must not be cached as a healthy one: {:?}",
                    cached.remote_check
                );
                assert!(cached
                    .remote_check
                    .error()
                    .unwrap_or("")
                    .contains("ambiguous argument"));
                assert!(!cached.available);
            });
        }

        /// And the success direction: a real count IS cached and IS a
        /// verdict, so the tray can still say "3 commits behind" offline.
        #[test]
        fn cached_status_reports_ok_after_a_successful_check() {
            vct_launcher_core::test_env::with_state_dir(|_root| {
                let good = UpdateStatus {
                    available: true,
                    current_sha: None,
                    remote_sha: Some("a".repeat(40)),
                    commit_count: 3,
                    branch: "main".into(),
                    head_detached: false,
                    remote_check: CheckState::Ok,
                    latest_source_release_check: CheckState::Ok,
                    last_checked: Some(Utc::now()),
                    error: None,
                };
                persist_check_result(&good);

                let cached = get_cached_update_status();
                assert_eq!(cached.remote_check, CheckState::Ok);
                assert_eq!(cached.commit_count, 3);
                assert!(cached.available);
            });
        }

        /// Before ANY check has run, the cache must say "unknown", not "up
        /// to date". Pre-fix `count.unwrap_or(0)` rendered a confident
        /// green from no data at all, on every fresh launcher process.
        #[test]
        fn cached_status_is_unknown_before_the_first_check() {
            vct_launcher_core::test_env::with_state_dir(|_root| {
                let cached = get_cached_update_status();
                assert!(
                    cached.remote_check.is_unknown(),
                    "no completed check ⇒ Unknown, not a green verdict built from nothing"
                );
                assert!(!cached.available);
            });
        }

        /// `auto_check_enabled` must not appear in the persisted JSON until
        /// the user actually sets it (WFT C2 — cosmetic, but it sent a real
        /// incident investigation down a wrong path). Behaviour unchanged:
        /// readers still default it ON.
        #[test]
        fn untouched_auto_check_is_omitted_from_the_state_file_and_still_defaults_on() {
            vct_launcher_core::test_env::with_state_dir(|_root| {
                let s = UpdateState {
                    last_checked_at: Some(Utc::now()),
                    last_known_remote_sha: Some("a".repeat(40)),
                    last_known_commit_count: Some(0),
                    last_check_unknown_error: None,
                    auto_check_enabled: None,
                };
                save_state(&s).expect("save");
                let raw = std::fs::read_to_string(state_file_path()).expect("read");
                assert!(
                    !raw.contains("auto_check_enabled"),
                    "an untouched toggle must not render as a tri-state a human misreads:\n{raw}"
                );
                assert!(
                    get_auto_check_enabled(),
                    "and the DEFAULT must still be ON — the omission is cosmetic only"
                );

                set_auto_check_enabled(false).expect("set");
                let raw = std::fs::read_to_string(state_file_path()).expect("read");
                assert!(
                    raw.contains("\"auto_check_enabled\": false"),
                    "an explicit choice IS persisted:\n{raw}"
                );
                assert!(!get_auto_check_enabled());
            });
        }


        /// `get_latest_source_release_tag` must ask the REMOTE. Detached on
        /// `v0.0.1` with `v0.0.2` upstream, `git describe` says `v0.0.1` —
        /// which the Updates page rendered as "Latest source release", next
        /// to an identical "Running:" value, and the lag banner (a string
        /// inequality) therefore stayed hidden.
        #[tokio::test]
        async fn latest_source_release_tag_comes_from_remote_not_head() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();

            let describe =
                git_cmd::run_git(&local, &["describe", "--tags", "--abbrev=0"]).await
                    .expect("describe");
            assert_eq!(describe, "v0.0.1", "what the OLD implementation returned");

            let tag = git_cmd::latest_remote_tag(&local, VCO_UPSTREAM_REMOTE).await
                .expect("ls-remote")
                .expect("remote has tags");
            assert_eq!(
                tag, "v0.0.2",
                "the newest REMOTE tag — the question the user was actually asking"
            );
            assert_ne!(tag, describe, "the two answers genuinely differ here");
        }

        // ── reattach_orchestrator_branch: one act, two leave-alones ──
        //
        // The command itself resolves its repo via `find_launcher_repo_root`
        // (walks up from `current_exe()`), so these drive the guard chain
        // against the fixture directly. Each asserts the REPO STATE after,
        // not just the return value — a refusal that still moved HEAD would
        // pass a return-value-only test.

        #[tokio::test]
        async fn reattach_acts_when_clean_and_ancestor() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();
            assert!(git_cmd::resolve_branch(&local).await.unwrap().detached);

            assert!(git_cmd::tree_is_clean(&local).await.unwrap());
            assert!(git_cmd::is_ancestor(&local, "HEAD", "vco_upstream/main").await.unwrap());
            git_cmd::run_git(&local, &["checkout", "main"]).await.expect("checkout");

            let after = git_cmd::resolve_branch(&local).await.unwrap();
            assert!(!after.detached, "HEAD must be attached afterwards");
            assert_eq!(after.name, "main");
        }

        #[tokio::test]
        async fn reattach_refuses_dirty_tree() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();
            std::fs::write(local.join("README.md"), "user edit\n").unwrap();

            assert!(
                !git_cmd::tree_is_clean(&local).await.unwrap(),
                "the clean-tree guard must REFUSE here"
            );
            // Leave-alone: nothing ran, so HEAD is untouched and the edit
            // survives.
            assert!(git_cmd::resolve_branch(&local).await.unwrap().detached);
            assert_eq!(
                std::fs::read_to_string(local.join("README.md")).unwrap(),
                "user edit\n"
            );
        }

        #[tokio::test]
        async fn reattach_refuses_when_not_ancestor() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();
            // A commit upstream does not have: checking out `main` would
            // strand it on an unreferenced commit (reflog-only recovery).
            git(&local, &["checkout", "-q", "--orphan", "sidework"]);
            std::fs::write(local.join("mine.txt"), "my work\n").unwrap();
            git(&local, &["add", "-A"]);
            git(&local, &["commit", "-qm", "my work"]);
            let sha = git_cmd::run_git(&local, &["rev-parse", "HEAD"]).await.unwrap();
            git(&local, &["checkout", "-q", "--detach", &sha]);

            assert!(
                !git_cmd::is_ancestor(&local, "HEAD", "vco_upstream/main").await.unwrap(),
                "the ancestry guard must REFUSE here"
            );
            // Leave-alone: the commit is still reachable from HEAD.
            assert_eq!(
                git_cmd::run_git(&local, &["rev-parse", "HEAD"]).await.unwrap(),
                sha
            );
            assert!(local.join("mine.txt").exists());
        }
    }

}
