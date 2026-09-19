//! Launcher self-update via git-pull.
//!
//! Behaviour: pull the latest from the remote, merge it (skipping
//! files considered user-owned, e.g. `CONTEXT_STATE.md`), then restart
//! the launcher to pick up changes. Check for updates once a day and
//! surface a notification when a new version is available — never
//! auto-apply.
//!
//! Approach:
//!   1. Daily background check: `git ls-remote vco_upstream <branch>` + local
//!      `git rev-parse HEAD` to compare SHAs without fetching the full
//!      history. Cheap (<1s on a healthy network). (Design B: the launcher
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
//!   - All operations we need (ls-remote, rev-parse, status, pull) are
//!     trivial single-line invocations. No advanced graph queries.
//!   - If git isn't on PATH we degrade gracefully (`git_available()`
//!     returns false → `check_for_launcher_update` returns a sentinel
//!     status and the UI shows a helpful message).

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{LazyLock, OnceLock};
use std::time::Duration;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tauri::{command, AppHandle, Emitter, Manager, Runtime};
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
/// for its `check_for_updates` / `update_orchestrator` flows (Design B
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
/// orchestrator self-update path (`check_for_updates` /
/// `update_orchestrator`). Both surfaces share the same architectural
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
            run_git(repo, &["remote", "add", VCO_UPSTREAM_REMOTE, &want])
                .await
                .map(|_| ())
        }
    }
}

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
    /// Remote HEAD SHA from `git ls-remote vco_upstream <branch>`.
    pub remote_sha: Option<String>,
    /// Number of commits remote is ahead of local. Computed via
    /// `git rev-list --count HEAD..vco_upstream/<branch>` — requires a fetch
    /// to be accurate. We do a `git fetch --quiet` before measuring.
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

async fn ls_remote_sha(repo: &Path, branch: &str) -> Result<String, String> {
    // `git ls-remote vco_upstream <branch>` returns `<sha>\trefs/heads/<branch>`.
    // Caller MUST have run `ensure_upstream_remote` first.
    let raw = run_git(repo, &["ls-remote", VCO_UPSTREAM_REMOTE, branch]).await?;
    raw.split_whitespace()
        .next()
        .map(|s| s.to_string())
        .ok_or_else(|| format!("ls-remote returned empty output for {}", branch))
}

// ---------------------------------------------------------------------------
// v0.2.83 A-F1 / D5: one serialized fetch home.
// ---------------------------------------------------------------------------
//
// Root cause A-RC3 (INVESTIGATION-v0283): two independent startup actors —
// the orchestrator badge check (`installer::check_for_updates`) and the
// launcher self-update daily check (`check_for_launcher_update`) — `git fetch`
// the SAME repo concurrently. Concurrent fetches contend on
// `.git/FETCH_HEAD.lock`; the loser errors and soft-fails to a false
// "no update available" (the historical first-start-after-release bug). This
// helper is the SINGLE production fetch invocation: every caller funnels
// through it (A>B>C rule — no second `git fetch` implementation), it serializes
// our own two callers behind a process-wide mutex, and it appends
// `--no-write-fetch-head` (git >=2.29) so FETCH_HEAD is never written at all —
// immune to that whole lock class even against EXTERNAL fetchers (VS Code
// autofetch, a CLI in another terminal). The retry ladder still covers residual
// transient failures (index.lock from external processes, network blips).

/// Process-wide serialization for upstream fetches. Our two startup actors
/// (badge check + self-update daily check) fetch the SAME install-root repo;
/// without this lock they race on `.git/FETCH_HEAD.lock` and the loser
/// soft-fails to a false "no update" (A-RC3). A single mutex for the whole
/// process is sufficient because both actors operate on the one install-root
/// clone; holding it across a fetch merely queues the (rare) concurrent second
/// fetch behind the first rather than letting them collide.
static UPSTREAM_FETCH_LOCK: LazyLock<tokio::sync::Mutex<()>> =
    LazyLock::new(|| tokio::sync::Mutex::new(()));

/// Cached result of the `git --version` >=2.29 probe. `None` until first
/// probed; `Some(true)` when git supports `--no-write-fetch-head`. Probed once
/// per process (D4) — the git binary can't change under a running launcher.
static GIT_SUPPORTS_NO_WRITE_FETCH_HEAD: OnceLock<bool> = OnceLock::new();

/// Fetch retry policy (D5). Selects the backoff ladder + extra fetch flags.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum FetchPolicy {
    /// One retry with a short (~2s) backoff. Used by the interactive /
    /// startup-latency-sensitive surfaces (badge check, pre-merge/rebase
    /// fetches) where a long 156s ladder would stall the UI. FETCH_HEAD
    /// contention is already covered by `--no-write-fetch-head` + the mutex,
    /// so a single quick retry is enough for the residual index.lock case.
    Quick,
    /// The existing v0.2.32 UB1 ladder (1/5/30/120s, 5 attempts, 156s upper
    /// bound). Used by the launcher self-update paths that must absorb a
    /// transient network blip at boot rather than surface a false negative.
    Persistent,
    /// `Persistent` + `--tags`. Used by `get_latest_source_release_tag` so the
    /// local `.git/refs/tags/` reflects the newest release tag.
    Tags,
}

/// Quick-policy backoff: a single retry after ~2s. Under `cfg(test)` the unit
/// is milliseconds (matching `FETCH_RETRY_DELAYS_MS`) so tests don't burn
/// wall-time; production interprets it as seconds.
#[cfg(not(test))]
const QUICK_FETCH_DELAYS_MS: [u64; 1] = [2_000];
#[cfg(test)]
const QUICK_FETCH_DELAYS_MS: [u64; 1] = [2];

/// Parse a `git --version` line ("git version X.Y[.Z][ (extra)]") and return
/// whether the version is >= 2.29 (the release that added
/// `--no-write-fetch-head`). Any parse failure returns `false` — we omit the
/// flag conservatively rather than pass an option an older git rejects.
fn git_version_supports_no_write_fetch_head(version_line: &str) -> bool {
    // Expect a token literally equal to "version" followed by the number.
    let mut toks = version_line.split_whitespace();
    // Skip up to and including the "version" word so a prefix like
    // "git version 2.43.5" or a vendored "git version 2.43.5 (Apple Git-...)"
    // both work.
    let ver = loop {
        match toks.next() {
            Some("version") => match toks.next() {
                Some(v) => break v,
                None => return false,
            },
            Some(_) => continue,
            None => return false,
        }
    };
    let mut parts = ver.split('.');
    let major: u32 = match parts.next().and_then(|s| s.parse().ok()) {
        Some(m) => m,
        None => return false,
    };
    let minor: u32 = match parts.next().and_then(|s| s.parse().ok()) {
        Some(m) => m,
        None => return false,
    };
    (major, minor) >= (2, 29)
}

/// Probe `git --version` ONCE per process and cache whether
/// `--no-write-fetch-head` is supported (git >= 2.29, D4). Cheap: a single
/// short-lived subprocess the first time, cached thereafter.
async fn supports_no_write_fetch_head() -> bool {
    if let Some(cached) = GIT_SUPPORTS_NO_WRITE_FETCH_HEAD.get() {
        return *cached;
    }
    let supported = match TokioCommand::new("git")
        .silent()
        .arg("--version")
        .output()
        .await
    {
        Ok(out) if out.status.success() => {
            let line = String::from_utf8_lossy(&out.stdout);
            git_version_supports_no_write_fetch_head(line.trim())
        }
        _ => false,
    };
    // First writer wins; a concurrent probe computes the same value.
    let _ = GIT_SUPPORTS_NO_WRITE_FETCH_HEAD.set(supported);
    *GIT_SUPPORTS_NO_WRITE_FETCH_HEAD.get().unwrap_or(&supported)
}

/// The ONE production upstream fetch (D5). Serializes our own callers behind
/// `UPSTREAM_FETCH_LOCK`, appends `--no-write-fetch-head` when git supports it,
/// and retries per `policy`. Caller MUST have run `ensure_upstream_remote`
/// first (unchanged contract).
///
/// `refspec`: `None` fetches the remote's default refspecs (`vco_upstream`);
/// `Some(branch)` fetches exactly that branch (`vco_upstream <branch>`) —
/// matching the pre-existing invocation shapes of the migrated call-sites.
///
/// Returns `Ok(())` on the first successful attempt; on exhaustion returns the
/// last non-empty git stderr line (or a sentinel when git drained stderr).
pub(crate) async fn serialized_fetch_upstream(
    repo: &Path,
    policy: FetchPolicy,
    refspec: Option<&str>,
) -> Result<(), String> {
    let no_write_fetch_head = supports_no_write_fetch_head().await;

    let attempt = || async {
        let mut args: Vec<&str> = vec!["fetch", "--quiet"];
        if matches!(policy, FetchPolicy::Tags) {
            args.push("--tags");
        }
        if no_write_fetch_head {
            args.push("--no-write-fetch-head");
        }
        args.push(VCO_UPSTREAM_REMOTE);
        if let Some(branch) = refspec {
            args.push(branch);
        }
        let fetch = TokioCommand::new("git")
            .silent()
            .args(&args)
            .current_dir(repo)
            .output()
            .await
            .map_err(|e| format!("git fetch spawn: {}", e))?;
        if fetch.status.success() {
            return Ok(());
        }
        let stderr = String::from_utf8_lossy(&fetch.stderr).to_string();
        // Surface the last non-empty stderr line — git pipes one final
        // human-readable summary there; preceding lines are usually progress
        // noise.
        let last = stderr
            .lines()
            .filter(|l| !l.trim().is_empty())
            .last()
            .unwrap_or("")
            .to_string();
        Err(last)
    };

    let delays: &[u64] = match policy {
        FetchPolicy::Quick => &QUICK_FETCH_DELAYS_MS,
        FetchPolicy::Persistent | FetchPolicy::Tags => &FETCH_RETRY_DELAYS_MS,
    };
    locked_fetch_with_retry(repo, delays, attempt).await
}

/// Acquire the process-wide `UPSTREAM_FETCH_LOCK` for the WHOLE retry sequence
/// (so a second caller queues behind us rather than racing on FETCH_HEAD —
/// A-RC3), then run `fetch_with_retry`. Factored out of
/// `serialized_fetch_upstream` so the concurrent-serialization regression test
/// can inject a fake attempt (that flips an in-flight flag) and prove no two
/// attempts overlap under the lock. The fetch is a short op; the (rare)
/// concurrent second fetch simply waits.
async fn locked_fetch_with_retry<F, Fut>(
    repo: &Path,
    delays: &[u64],
    attempt_fn: F,
) -> Result<(), String>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<(), String>>,
{
    let _lock = UPSTREAM_FETCH_LOCK.lock().await;
    fetch_with_retry(repo, delays, attempt_fn).await
}

/// Retry delays for the `Persistent` fetch policy. Total wall-time across all
/// retries is 1+5+30+120 = 156 seconds — long enough to absorb transient
/// network blips at boot (Wi-Fi reconnect, VPN handshake, DNS stagger) but
/// short enough that a check truly stuck on a dead network surfaces as an
/// error to the UI within a few minutes rather than silently hanging.
///
/// Under `cfg(test)` the unit is milliseconds so the retry tests don't
/// burn 156s of CI wall-time. Production code interprets the same values
/// as seconds.
#[cfg(not(test))]
const FETCH_RETRY_DELAYS_MS: [u64; 4] = [1_000, 5_000, 30_000, 120_000];
#[cfg(test)]
const FETCH_RETRY_DELAYS_MS: [u64; 4] = [1, 5, 30, 120];

/// M-2 (v0.2.83): per-ATTEMPT timeout for the serialized upstream fetch. A
/// single `git fetch` runs under `.output().await` with NO cap; because
/// `locked_fetch_with_retry` holds `UPSTREAM_FETCH_LOCK` across the whole ladder,
/// one hung fetch (dead network, hung credential helper, stuck DNS) would stall
/// the badge check, the daily check, AND every update/merge/rebase behind the
/// lock forever. The plan required keeping `run_git`'s 30s cap semantics on this
/// path — only `.silent()` had survived the D5 extraction. Each attempt is now
/// wrapped in `tokio::time::timeout`; a timeout is a RETRYABLE error (the ladder
/// re-tries, the lock is released on the outer future's drop). Production: 30s.
/// Under `cfg(test)` it is milliseconds so the never-resolving-attempt
/// regression test settles fast.
#[cfg(not(test))]
const FETCH_ATTEMPT_TIMEOUT: Duration = Duration::from_secs(30);
#[cfg(test)]
const FETCH_ATTEMPT_TIMEOUT: Duration = Duration::from_millis(50);

/// Fetch the canonical upstream (NOT `origin`) with retry-on-failure.
/// Caller MUST have run `ensure_upstream_remote` first.
///
/// Retry policy: first attempt immediate, then back off at 1s / 5s / 30s
/// / 120s (5 attempts total, 156s upper bound). Each non-zero git exit
/// is treated as a retryable error — we don't try to discriminate "DNS
/// failure" from "auth rejected" because the cheapest, most reliable
/// signal is "did it succeed yet". Surfaces the last git stderr line as
/// the error message after all attempts exhausted.
///
/// v0.2.32 UB1 (2026-05-23): replaces the single-shot `git fetch` that
/// left the launcher stuck on stale state after a transient network
/// hiccup at boot — symptom: badge never refreshes without restart.
///
/// v0.2.83 (D5): now a thin wrapper over `serialized_fetch_upstream` with the
/// `Persistent` policy (the same 156s ladder, preserving UB1) — the actual
/// fetch + retry + serialization live in the single shared home.
async fn fetch_upstream(repo: &Path) -> Result<(), String> {
    serialized_fetch_upstream(repo, FetchPolicy::Persistent, None).await
}

/// Inner retry loop, parametrised over the actual fetch attempt so unit
/// tests can swap in a closure that simulates failures without invoking
/// a real `git` binary. The first attempt is immediate; subsequent
/// attempts sleep for `delays[i-1]` before retrying (the `delays` slice
/// selects the policy's backoff ladder — Quick vs Persistent).
///
/// `repo` is passed through for diagnostic logging only — the closure
/// already captures the directory it needs.
async fn fetch_with_retry<F, Fut>(
    repo: &Path,
    delays: &[u64],
    mut attempt_fn: F,
) -> Result<(), String>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<(), String>>,
{
    let mut last_err: Option<String> = None;
    // First attempt is index 0 (no delay); subsequent attempts wait
    // delays[attempt - 1].
    for attempt in 0..=delays.len() {
        if attempt > 0 {
            let delay = Duration::from_millis(delays[attempt - 1]);
            tokio::time::sleep(delay).await;
        }
        // M-2 (v0.2.83): cap each attempt so one hung fetch can't stall the
        // whole ladder (and everything queued behind UPSTREAM_FETCH_LOCK)
        // forever. A timeout is treated as a retryable error — the ladder
        // continues, and on exhaustion the timeout message is surfaced to the
        // UI. The lock is held by the OUTER `locked_fetch_with_retry` future;
        // returning here releases it on drop, so a subsequent caller proceeds.
        let attempt_result = match tokio::time::timeout(
            FETCH_ATTEMPT_TIMEOUT,
            attempt_fn(),
        )
        .await
        {
            Ok(inner) => inner,
            Err(_elapsed) => Err(format!(
                "git fetch timed out after {}s",
                FETCH_ATTEMPT_TIMEOUT.as_secs().max(1)
            )),
        };
        match attempt_result {
            Ok(()) => {
                if attempt > 0 {
                    tracing::info!(
                        "[vct] check_for_updates: git fetch succeeded after {} retries at {}",
                        attempt,
                        repo.display()
                    );
                }
                return Ok(());
            }
            Err(e) => {
                tracing::warn!(
                    "[vct] check_for_updates: git fetch attempt {} failed at {}: {}",
                    attempt + 1,
                    repo.display(),
                    if e.is_empty() { "(no stderr)" } else { &e }
                );
                // Only retain non-empty errors — empty stderr is useless
                // for the UI, so falling through to the sentinel below
                // gives a more honest message.
                if !e.is_empty() {
                    last_err = Some(e);
                }
            }
        }
    }
    Err(last_err.unwrap_or_else(|| "git fetch failed (no stderr)".to_string()))
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
/// The remaining git calls here (`ls-remote`, `rev-list`) work against
/// whatever `vco_upstream` points at, so a fixture pointing it at a local
/// bare repo exercises the real code paths with no network at all.
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

    let remote_sha = match ls_remote_sha(repo, &branch).await {
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

    // Ledger step 40 (v0.2.95 phase 2): neither update surface was
    // single-flighted. The frontend disables its own button, which is exactly
    // the reasoning v0.2.91 decision #26 rejected — a second window, a reopened
    // modal or a button double-fire all reach the command again, and here that
    // means two git pulls plus two `install.py --update` runs interleaving on
    // ONE tree. The claim is taken by the COMMAND, not inside the pipeline, so
    // it is still held across install.py and the restart hop; it releases on
    // every exit path including a panic (RAII).
    let _flight = crate::commands::single_flight::begin_orchestrator_update_or_refuse()?;

    // Step 0: pin the canonical public upstream (Design B). Must happen
    // BEFORE any fetch/diff/pull so we never accidentally pull from a
    // private fork's `origin`.
    ensure_upstream_remote(&repo).await?;

    // Step 2: detect what changed BEFORE pulling so we can decide what
    // to rebuild. We diff the current HEAD against vco_upstream/<branch>.
    // v0.2.92 WP-13: through the ONE resolver — pre-fix this was the
    // un-normalised `current_branch`, so a detached HEAD diffed against
    // `vco_upstream/HEAD` (a ref that does not exist).
    let branch_state = git_cmd::resolve_branch(&repo).await?;
    let branch = branch_state.name.clone();
    if branch_state.detached {
        tracing::warn!(
            "[vct] apply_launcher_update: {} has a DETACHED HEAD — pulling {}/{}. The pull \
             fast-forwards fine, but HEAD stays detached afterwards; use the Reattach action \
             on Preferences → Launcher updates to return to a branch.",
            repo.display(),
            VCO_UPSTREAM_REMOTE,
            branch,
        );
    }

    // Fetch upstream so the local refs (vco_upstream/<branch>) are current for
    // the dirty-tracked pre-flight and for the rebuild-gating diff below.
    // Without this, a fresh `vco_upstream` remote has no tracking refs yet and
    // the diff returns empty.
    //
    // v0.2.95 phase 2 (ledger step 7): through the SAME serialized home the
    // installer surface uses. The plain `fetch_upstream` this used to call was
    // the last caller still exposed to the A-RC3 FETCH_HEAD race with the
    // startup badge check that v0.2.83 closed on the other surface — the mutex
    // plus `--no-write-fetch-head` is the whole fix, and it was never a
    // surface-specific one. The pipeline fetches again with the same policy;
    // that second call is a cheap no-op against a just-fetched remote and it is
    // what makes the pipeline correct for a caller that did NOT pre-fetch.
    serialized_fetch_upstream(&repo, FetchPolicy::Quick, Some(&branch)).await?;

    // v0.2.92 WP-13: `.unwrap_or_default()` here was the THIRD laundering of
    // the same missing ref. An empty diff because `vco_upstream/HEAD` does not
    // exist is indistinguishable from an empty diff because nothing changed —
    // so `needs_cargo` and `needs_npm` both came out `false` and the launcher
    // PULLED NEW SOURCE AND SILENTLY SKIPPED THE REBUILD, leaving the user on
    // the old binary with new source on disk.
    //
    // Unknown now means REBUILD EVERYTHING. That is the conservative
    // direction: the cost of an unnecessary `cargo` + `npm` build is minutes
    // of the user's time on a button they explicitly pressed; the cost of a
    // skipped necessary build is a launcher that reports a version it is not
    // running.
    //
    // v0.2.95 phase 2 — these two now gate the FALLBACK only. When `install.py`
    // is available it applies the artefacts (including refreshing the dist
    // binary from the tracked ones the pull just landed) and no local toolchain
    // is touched. See `ArtefactSource`.
    let (needs_cargo, needs_npm) = match run_git(
        &repo,
        &[
            "diff",
            "--name-only",
            &format!("HEAD..{}/{}", VCO_UPSTREAM_REMOTE, branch),
        ],
    )
    .await
    {
        Ok(pre_diff) => (
            changed_paths_need_cargo(&pre_diff),
            changed_paths_need_npm(&pre_diff),
        ),
        Err(e) => {
            tracing::warn!(
                "[vct] apply_launcher_update: pre-pull diff against {}/{} failed ({}) — \
                 rebuilding BOTH cargo and npm rather than assuming nothing changed",
                VCO_UPSTREAM_REMOTE,
                branch,
                e
            );
            (true, true)
        }
    };

    // ---------------------------------------------------------------------
    // The pull, through the SHARED pipeline.
    // ---------------------------------------------------------------------
    //
    // v0.2.95 phase 2. Everything between "the user clicked Update now" and
    // "the tree is at the upstream tip" is `update_pipeline`'s, because it is
    // the same git clone the MenuBar badge updates and the two had drifted in
    // twelve places (review §2). This call is what closes them — in-progress
    // merge refusal, MCP kill-sweep + update gate, upstream pinning, HUB STOP,
    // both pre-pull binary renames, serialized fetch, the A0 user-editable
    // pre-merge (and with it the RENDERED_LOCAL reconcile this surface used to
    // reach by its own direct call), F1, the generated-file reconcile, the
    // shared pull plan, every failure classification INCLUDING the
    // untracked-collision one, the resume sentinel, the autostash-pop
    // backstop, the "already up to date" heal and the HEAD-advance guard.
    //
    // The hub stop is the one worth naming: `launcher/dist/*/vct-hub*` are
    // TRACKED files, so this surface has been pulling over a RUNNING hub. On
    // Windows that aborts the pull atomically or silently skips the binary; on
    // POSIX the hub keeps serving old code from a deleted inode for the rest of
    // the session. The pipeline stops it (hard-fail), renames it aside, and
    // every failure path inside restores both and brings it back up.
    //
    // `first_change_at_risk` STAYS this surface's own refusal — see
    // `ExtraPreflight`. It is passed IN rather than run before the call so the
    // in-progress-merge refusal can front it: a tree wedged mid-merge must
    // reopen on that state, not be told about the `UU` entries the wedge left.
    let update_start_ms = chrono::Utc::now().timestamp_millis();
    let head_sha_before = current_sha(&repo).await.ok();
    let repo_label = repo.display().to_string();
    write_self_update_audit(
        &app,
        "apply_launcher_update_start",
        serde_json::json!({
            "old_version": env!("CARGO_PKG_VERSION"),
            "source_commit": head_sha_before,
            "branch": branch,
            "install_path": repo_label,
        }),
    );

    let prepared = match crate::commands::update_pipeline::prepare_and_pull_orchestrator_repo(
        &repo,
        crate::commands::update_pipeline::UpdatePipelineOptions {
            surface: "apply_launcher_update",
            // No progress modal on this surface — the Preferences page renders a
            // spinner on its own button. A surface that cannot show sub-progress
            // must not ask install.py to emit it.
            emit_progress_to: None,
            install_path_label: &repo_label,
            start_branch: &branch,
            head_sha_before: head_sha_before.clone(),
            update_start_ms,
            extra_preflight:
                crate::commands::update_pipeline::ExtraPreflight::RefuseDirtyTrackedAtRisk {
                    branch: &branch,
                },
        },
    )
    .await
    {
        Ok(outcome) => outcome,
        Err(err) => return Err(render_pipeline_error(&app, &repo, &branch, err).await),
    };

    let crate::commands::update_pipeline::UpdatePipelineOutcome {
        already_up_to_date,
        dist_binary_stale,
        pull_branch,
        pre_pull_renamed,
        pre_pull_renamed_hub,
        gate_guard: mut update_gate_guard,
        db_audit,
    } = prepared;
    // The same two paths the abort tails below restore, in the shape the
    // rebuild-failure recovery in `finish_apply_after_pull` takes. Built once
    // so the pair cannot be swapped at one call site and not another.
    let renames = crate::commands::update_pipeline::PrePullRenames {
        hub: pre_pull_renamed_hub.clone(),
        launcher: pre_pull_renamed.clone(),
    };
    // The pipeline DECIDED these rows; the Db handle is this command's, so the
    // write is this command's. Ledger step 39 — pre-v0.2.95 a launcher update
    // was forensically invisible except for a single clobber-averted row.
    for (operation, detail) in db_audit {
        write_self_update_audit(&app, &operation, detail);
    }

    if already_up_to_date {
        // Nothing was pulled, so there are no artefacts to apply: skip
        // install.py AND the rebuild. The pipeline has already run the
        // at-rest dist reconcile (WI-2), which is the whole value of this
        // branch — the restart below is how the user picks up anything it
        // staged, so it is deliberately NOT skipped.
        tracing::info!(
            "[vct] apply_launcher_update: already up to date{}",
            if dist_binary_stale {
                " — the dist binary on disk was newer than the running one; relaunching"
            } else {
                ""
            }
        );
        return finish_apply_after_pull(
            app,
            &repo,
            false,
            false,
            // v0.2.95 ship-gate MAJOR-2: `Unchanged`, not `SourceOnly`. HEAD
            // did not move on this branch, so there is no source advance to
            // record and the manifest must be left exactly as the last real
            // installer run wrote it. Passing `SourceOnly` here stamped
            // `post_source_only: true` on an untouched tree, which
            // `check_for_updates` turns into `install_stale` — a badge
            // demanding a full re-install after a click that changed nothing.
            ArtefactSource::Unchanged,
            Some(&mut update_gate_guard),
            &renames,
        )
        .await;
    }

    // ---------------------------------------------------------------------
    // Apply the artefacts. `install.py --update` FIRST, always, when it can run.
    // ---------------------------------------------------------------------
    //
    // v0.2.95 phase 2 — this is the §4.1 fix, the highest-impact one in the
    // review. This surface pulled the WHOLE orchestrator repo and then rebuilt
    // only the launcher: hooks under `.claude/`, MCP registrations in
    // `~/.claude.json`, the venv, `templates/**` propagation, KG seeds and
    // schema migrations were all left at the OLD version, and the manifest was
    // then stamped as a completed install at the NEW one. The half-updated
    // state was durable AND invisible, because after the pull the badge's
    // commits-behind count is zero and the surface that would repair it stops
    // offering itself.
    //
    // The cargo/npm rebuild is now the FALLBACK, taken only when install.py
    // cannot run. That also settles §4.2: a release-binary user with no Rust
    // toolchain used to get "cargo build failed to start" AFTER the pull had
    // landed, leaving new source with old artefacts and no way forward.
    let system = crate::commands::installer::detect_system().await?;
    let install_py_available = system.has_python && repo.join("install.py").is_file();

    let artefacts = if install_py_available {
        update_gate_guard.advance_phase(crate::commands::update_gate::Phase::InstallPy);
        // Hold the launcher.db writer lock open for install.py exactly as the
        // installer surface does — on Windows SQLite holds it exclusively and
        // install.py cannot take it while we have it. RAII: reopens on every
        // exit path below, force-restarting if the reopen fails.
        let mut db_close_guard = crate::commands::installer::DbUpdateClosedGuard::new(app.clone());
        let run = crate::commands::update_pipeline::run_install_py_update(
            &repo,
            &system.python_cmd,
            "apply_launcher_update",
            None,
        )
        .await;
        let run = match run {
            Ok(run) => run,
            Err(msg) => {
                crate::commands::installer::abort_update_restore_binaries_and_hub(
                    &repo,
                    pre_pull_renamed.as_deref(),
                    pre_pull_renamed_hub.as_deref(),
                );
                return Err(msg);
            }
        };
        if !run.success {
            crate::commands::installer::abort_update_restore_binaries_and_hub(
                &repo,
                pre_pull_renamed.as_deref(),
                pre_pull_renamed_hub.as_deref(),
            );
            return Err(format!("Update failed: {}", run.stderr));
        }
        db_close_guard.reopen();
        ArtefactSource::InstallPy
    } else {
        tracing::warn!(
            "[vct] apply_launcher_update: install.py is not runnable here (python detected: {}, \
             install.py present: {}) — falling back to rebuilding the launcher from source. \
             Hooks, MCP registrations, templates, the venv, the KG seed and schema migrations \
             stay at the OLD version until `python install.py --update` runs in {}.",
            system.has_python,
            repo.join("install.py").is_file(),
            repo.display(),
        );
        ArtefactSource::SourceOnly
    };

    update_gate_guard.advance_phase(crate::commands::update_gate::Phase::BinaryRefresh);

    // v0.2.93 (stale status cache): the pull + apply landed — refresh
    // `~/.vct/launcher-update-state.json` NOW, before the restart hop kills
    // this process, so the Updates card does not keep reporting the pre-update
    // "N commits behind". Soft-fail; the daily check would repair it anyway.
    refresh_cached_state_after_pull(&repo, &pull_branch).await;

    let (rebuild_cargo, rebuild_npm) = match artefacts {
        // install.py refreshed the dist binary from the tracked ones the pull
        // just landed, so there is nothing for a local toolchain to do.
        ArtefactSource::InstallPy => (false, false),
        // `Unchanged` cannot arrive here — the already-up-to-date branch
        // returned above. It is named rather than caught by a wildcard so a
        // future variant has to be classified deliberately instead of
        // inheriting whichever default `_` happened to sit next to it.
        ArtefactSource::SourceOnly | ArtefactSource::Unchanged => (needs_cargo, needs_npm),
    };
    finish_apply_after_pull(
        app,
        &repo,
        rebuild_cargo,
        rebuild_npm,
        artefacts,
        Some(&mut update_gate_guard),
        &renames,
    )
    .await
}

/// Which path applied the artefacts before [`finish_apply_after_pull`] runs.
///
/// It decides exactly two things, and both are honesty about the install
/// record: whether the launcher may write `install-manifest.json` (install.py
/// is the only writer of its `version` — see `commands::manifest`), and which
/// [`crate::commands::installer::HubRestartContext`] the hub restart may use.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum ArtefactSource {
    /// `install.py --update` ran and exited 0. It has already written the
    /// install manifest, truthfully; this tail must not overwrite that record
    /// with a launcher-path one.
    InstallPy,
    /// Only the source tree moved (and possibly a local launcher rebuild):
    /// `force_resync_launcher`'s hard reset, or the fallback taken when
    /// install.py cannot run. venv / hooks / templates / MCP registrations / KG
    /// seed / schema are all still at the old version.
    SourceOnly,
    /// NOTHING moved. The tree was already at the upstream tip, so there were
    /// no artefacts to apply and no source advance to record — the tail runs
    /// only for the restart hop (and whatever the at-rest dist reconcile
    /// staged).
    ///
    /// v0.2.95 ship-gate MAJOR-2. This branch used to pass `SourceOnly`, whose
    /// own doc says "only the source tree MOVED" and which
    /// `manifest::refresh_install_manifest` turns into `post_source_only:
    /// true` — the durable flag meaning "a path advanced the source tree
    /// WITHOUT running install.py". On an already-up-to-date pull no path
    /// advanced anything, so the flag was a false statement, and
    /// `check_for_updates` reads it as an unconditional `install_stale`: a
    /// click that changed nothing lit a badge demanding a full
    /// `apply_pending_install`. Pre-phase-2 the same call was harmless (the
    /// refresh only re-read `version`, to the same value); the flag is what
    /// made the no-op observable, so the variant that says "nothing moved" is
    /// the fix rather than a special case inside the writer.
    ///
    /// The hub restart still uses `AbortRecovery` — install.py did not run, so
    /// there is no cutover sentinel to trust and the /health poll must
    /// actually happen.
    Unchanged,
}

/// Does this tail owe `state/install-manifest.json` a refresh?
///
/// Split out of [`finish_apply_after_pull`] for one reason: the tail itself
/// takes an `AppHandle` and is unreachable from a unit test, so as an inline
/// `if` the decision could only ever be checked by reading the source. As a
/// function it is driven for real — the tests below run the actual writer
/// under each variant against a temp root and compare BYTES, which is what
/// "the manifest is untouched" actually means.
///
/// Every variant is spelled out rather than `_ => false`: which paths may
/// write this file is the whole of WP-1, and a wildcard would let a future
/// variant inherit an answer nobody chose for it.
pub(crate) fn owes_manifest_refresh(artefacts: ArtefactSource) -> bool {
    match artefacts {
        // install.py already wrote it, truthfully, seconds ago.
        ArtefactSource::InstallPy => false,
        // The tree moved and the installer did not run — the one path whose
        // whole job is to record that.
        ArtefactSource::SourceOnly => true,
        // Nothing moved: no advance to record, and writing anyway would stamp
        // `post_source_only: true` on an untouched tree (ship-gate MAJOR-2).
        ArtefactSource::Unchanged => false,
    }
}

/// Write one audit row through the app's Db, if it is registered. Soft-fail:
/// audit is forensics, never a reason to fail a user-initiated update.
fn write_self_update_audit<R: Runtime>(
    app: &AppHandle<R>,
    operation: &str,
    detail: serde_json::Value,
) {
    use tauri::Manager as _;
    if let Some(db) = app.try_state::<crate::db::Db>() {
        let _ = db.audit(operation, None, None, &detail);
    }
}

/// Render one [`crate::commands::update_pipeline::UpdatePipelineError`] into
/// THIS surface's error string, and write the audit rows that belong to the
/// classification.
///
/// The pipeline classifies once; each surface renders into the shape its own
/// frontend parses. This page parses exactly one structured shape —
/// `kind:"non_fast_forward"`, which opens the resync modal — so the variants
/// that used to reach that modal still reach it, and the ones that never had a
/// modal here become worded messages instead of the raw git stderr they were.
///
/// What changes for the user is NOT the modal: it is that every one of these
/// now leaves the durable trail the installer surface leaves. A wedged update
/// writes the paired resume sentinel + `update_resume_required` deferral, so
/// the MenuBar offers "Continue Update" and a terminal Claude sees it at
/// session start — a NON-destructive route out, where this surface previously
/// offered only `force_resync_launcher`'s hard reset.
async fn render_pipeline_error<R: Runtime>(
    app: &AppHandle<R>,
    repo: &Path,
    branch: &str,
    err: crate::commands::update_pipeline::UpdatePipelineError,
) -> String {
    use crate::commands::update_pipeline::UpdatePipelineError as PipelineErr;

    // Shared by the two arms that render the resync modal: its payload carries
    // the SHAs so the user can see what their clone has vs. what upstream has.
    async fn shas(repo: &Path, branch: &str) -> (Option<String>, Option<String>) {
        (
            current_sha(repo).await.ok(),
            ls_remote_sha(repo, branch).await.ok(),
        )
    }

    match err {
        PipelineErr::MergeInProgress { at_preflight, .. } => {
            // The payload is the installer surface's conflict-modal shape and
            // this page does not parse it; relaying the JSON as a toast would
            // be worse than saying what happened. The state is durable and the
            // MenuBar badge reads it directly from `.git`, so the honest
            // message is the one that points there.
            if at_preflight {
                write_self_update_audit(
                    app,
                    "apply_launcher_update_refused_merge_in_progress",
                    serde_json::json!({
                        "install_path": repo.display().to_string(),
                        "branch": branch,
                    }),
                );
            }
            format!(
                "A merge or rebase is already in progress in {} — the update cannot start a \
                 second one. Finish or abort it from the launcher's update badge (it offers \
                 Continue Update / Abort), or resolve it in a terminal.",
                repo.display()
            )
        }
        PipelineErr::DirtyTrackedAtRisk { path } => format!(
            "Uncommitted changes on tracked file '{}' would be lost — this update also \
             changes it. Commit, stash, or revert it before updating.",
            path
        ),
        PipelineErr::UntrackedCollision { .. } => format!(
            "The update was aborted before merging: untracked local files sit at paths this \
             release adds, so git refused rather than overwrite them. The colliding paths are \
             listed in {}/.claude/context/UPDATE_DEFERRED.md, and the launcher's update badge \
             offers a one-click resolve.",
            repo.display()
        ),
        PipelineErr::Conflict {
            branch: b,
            detail,
            record_binary_clobber_averted,
            ..
        } => {
            if record_binary_clobber_averted {
                write_self_update_audit(
                    app,
                    "update_binary_clobber_averted",
                    serde_json::json!({
                        "surface": "apply_launcher_update",
                        "branch": b,
                        "pop_conflict_after_success": false,
                        "note": "abort tail kept the freshly-pulled binary (WI-3)",
                    }),
                );
            }
            let (local, remote) = shas(repo, &b).await;
            serialize_non_ff_error(&b, local.as_deref(), remote.as_deref(), &detail)
        }
        PipelineErr::AutostashPopConflict {
            branch: b,
            detail,
            record_binary_clobber_averted,
            ..
        } => {
            if record_binary_clobber_averted {
                write_self_update_audit(
                    app,
                    "update_binary_clobber_averted",
                    serde_json::json!({
                        "surface": "apply_launcher_update",
                        "branch": b,
                        "pop_conflict_after_success": true,
                        "note": "abort tail kept the freshly-pulled binary (WI-3)",
                    }),
                );
            }
            let (local, remote) = shas(repo, &b).await;
            serialize_non_ff_error(&b, local.as_deref(), remote.as_deref(), &detail)
        }
        PipelineErr::NonFastForward {
            branch: b,
            local_sha,
            remote_sha,
            detail,
            ..
        } => serialize_non_ff_error(&b, local_sha.as_deref(), remote_sha.as_deref(), &detail),
        PipelineErr::HeadDidNotAdvance { detail } => {
            write_self_update_audit(
                app,
                "apply_launcher_update_complete",
                serde_json::json!({
                    "success": false,
                    "note": "head_did_not_advance_post_pull",
                    "branch": branch,
                }),
            );
            detail
        }
        PipelineErr::Raw(message) => message,
    }
}

/// `force_resync_launcher`'s destructive core: stop the hub, rename the two
/// binaries aside, hard-reset the tree — and put both back if the reset fails.
///
/// v0.2.95 phase 3. Split from the `#[command]` above it for the reason phase 1
/// split `reconcile_and_pull` from the pipeline: a `#[command]` taking
/// `AppHandle` is unreachable from a unit test, and this is the span that must
/// be pinned. Everything it touches is drivable over a temp clone with
/// `VCT_STATE_DIR` redirected — the hub stop reads `<vct_root_dir()>/hub.pid`,
/// and its process-identity sweep already refuses under a test harness
/// (v0.2.92, `update_gate::pre_update_hub_kill_sweep`).
///
/// WHY THE HUB STOP IS HERE AT ALL (the gap this closes). `git reset --hard`
/// writes every tracked file whose content differs from the target, and
/// `launcher/dist/<arch>/vct-hub{,.exe}` IS tracked — so this path carried the
/// exact hazard `07101d30` closed for `update_orchestrator` in v0.2.21 Step 12
/// (Reviewer B blocker B1) and phase 2 closed for the launcher-update surface.
/// It was never a decision to omit it: `force_resync_launcher` was written on
/// 2026-05-07 (`b5b3f7ad`), and the hub binary did not become a tracked file
/// until `120b921c` two weeks later. The hazard arrived UNDER this function.
///
/// ORDER IS THE POINT, not the presence: stopping the hub after the reset would
/// protect nothing. The observable a test keys on is that the hub stop's own
/// side effect (a stale `hub.pid` is removed) is visible EVEN WHEN THE RESET
/// FAILS.
///
/// On reset failure the pre-pull renames are reverted and the hub restarted
/// through `abort_update_restore_binaries_and_hub` — the shared tail every
/// other surface's failure path uses. Without it a failed resync would leave
/// the user with a perma-stopped hub, which is the failure `07101d30` names.
///
/// Returns the renames on success: the caller's tail (`finish_apply_after_pull`)
/// owes the hub RESTART, and on Windows the staging tail owes the swap.
async fn stop_hub_then_hard_reset(
    repo: &Path,
    reset_target: &str,
) -> Result<crate::commands::update_pipeline::PrePullRenames, String> {
    // v0.2.95 ship-gate MINOR-4 — abort BEFORE the reset.
    //
    // `git reset --hard` rewrites the tree and moves the branch, and it does
    // NOT clear `.git/MERGE_HEAD` or `.git/rebase-merge`/`rebase-apply`.
    // Resetting a mid-operation tree therefore leaves the clone believing it
    // is still in a merge or rebase — one whose recorded ONTO/HEAD no longer
    // describes anything on disk. Every later `git pull` is then refused
    // ("you have not concluded your merge"), and the surface the user would
    // reach for next is this one, which does the same thing again. "Resync
    // now" is the wedged-clone rescue; it must not be able to wedge it.
    //
    // Reachability, honestly stated: NOT reachable today. The pipeline's B
    // cannot take the `RebaseAutostash` arm for the inputs `blocking_changes`
    // refuses, so the resync modal and a mid-rebase tree do not co-occur in
    // any flow currently shipped. Phase 2 made the merge case co-occur, and
    // one narrowing of that refusal makes the rebase case co-occur too. This
    // is two git commands that no-op when nothing is in progress, run on the
    // path whose entire job is rescuing a clone; that is the right side to be
    // wrong on.
    //
    // The CLAIM-FREE helper, deliberately: `force_resync_launcher` is already
    // holding the orchestrator-clone claim by the time it gets here, so the
    // `#[command]` (which takes one) would refuse against its own caller.
    if let Err(e) = crate::commands::installer::abort_merge_or_rebase_unclaimed(repo).await {
        // Soft-fail by design. The helper returns Err only when an abort was
        // genuinely in progress and git refused it; the reset is still the
        // user's explicitly chosen recovery, and refusing to run it would
        // strand exactly the tree this button exists for. Log loudly instead.
        tracing::warn!(
            "[vct] force_resync_launcher: could not abort the in-progress \
             merge/rebase before the hard reset ({}) — resetting anyway; if \
             the clone still reports an unconcluded merge afterwards, run \
             `git merge --abort` (or `git rebase --abort`) in {} by hand",
            e,
            repo.display(),
        );
    }

    let renames = crate::commands::update_pipeline::stop_hub_and_rename_binaries_aside(
        repo,
        "resync",
        Some("git reset --hard"),
        |_stage: &str, _message: &str, _pct: f32| {
            // This surface has no progress modal — `force_resync_launcher`
            // takes only an `AppHandle`. A surface with no modal must not
            // claim one; the events are simply not emitted.
        },
    )?;

    if let Err(e) = run_git(repo, &["reset", "--hard", reset_target]).await {
        // The tree was NOT written. Put the binaries back and restart the hub
        // we stopped — the same shared tail every other failure path uses.
        crate::commands::installer::abort_update_restore_binaries_and_hub(
            repo,
            renames.launcher.as_deref(),
            renames.hub.as_deref(),
        );
        return Err(e);
    }

    Ok(renames)
}

/// Recovery path after a non-fast-forward detection. Hard-resets the
/// launcher's tracked files to `vco_upstream/<branch>`. **Destructive** —
/// untracked files (user state, `.env`, `state/`, `~/.vct/`, etc.) are
/// left untouched, but any tracked-file edits the user made locally
/// are lost.
///
/// Design B (load-bearing — do NOT "fix" the code to match an older doc):
/// the reset target is `VCO_UPSTREAM_REMOTE` (`vco_upstream`), NOT `origin`.
/// On a private fork `origin` may point at the fork's own remote; resetting
/// to it would NOT recover the public release. The doc previously said
/// `origin/<branch>` (a stale pre-Design-B comment) — corrected here so a
/// future maintainer doesn't "make the code match the doc" and reintroduce
/// the wrong-ref bug. See `update-project-own-git-repo` audit §2.
///
/// We deliberately do NOT re-assert clean tree here (unlike
/// `apply_launcher_update`): the whole point is to override divergence
/// the user has already opted into via the modal. The frontend modal
/// makes the "your tracked-file changes will be lost" warning explicit.
/// v0.2.71: with `apply_launcher_update` now auto-merging conflict-free
/// committed divergence (Piece 4), this destructive path is reached ONLY
/// for a genuine conflict the user explicitly opts into via the modal — no
/// longer the default forward action for any committed divergence.
///
/// Sequence:
///   1. claim the shared orchestrator-update single-flight (ledger 40)
///   2. fetch vco_upstream/<branch>
///   3. compute pre-reset diff for rebuild gating (HEAD..vco_upstream/<branch>)
///   4. stop the hub + rename both binaries aside, then reset --hard
///      vco_upstream/<branch> — see `stop_hub_then_hard_reset`
///   5. rebuild + restart, hub restart included (shared with
///      `apply_launcher_update`)
#[command]
pub async fn force_resync_launcher<R: Runtime>(app: AppHandle<R>) -> Result<(), String> {
    if !git_available().await {
        return Err("git not found on PATH — cannot resync".into());
    }
    let repo = find_launcher_repo_root()?;

    // Ledger step 40, the last update surface to take it (v0.2.95 phase 3).
    // The SAME claim `update_orchestrator` and `apply_launcher_update` hold,
    // because this acts on the SAME clone and its act is the most destructive
    // of the three: a `git reset --hard` interleaved with another surface's
    // `git pull` or `install.py --update` is §4.8's catastrophic case with the
    // sharpest edge. Held for the reset, the rebuild and the restart hop;
    // released by RAII on every exit path. No self-deadlock: the resync modal
    // is opened only AFTER `apply_launcher_update` has returned and dropped
    // its own claim.
    let _flight = crate::commands::single_flight::begin_orchestrator_update_or_refuse()?;
    // v0.2.92 WP-13: through the ONE resolver. Pre-fix a detached HEAD made
    // this the literal `"HEAD"`, so the `git reset --hard vco_upstream/HEAD`
    // below hard-errored on a ref that does not exist — i.e. "Resync now"
    // could not work AT ALL in the very state the user needed it for.
    let branch_state = git_cmd::resolve_branch(&repo).await?;
    let branch = branch_state.name.clone();

    // Pin the canonical public upstream (Design B). Must precede the fetch.
    ensure_upstream_remote(&repo).await?;

    // Fetch first so vco_upstream/<branch> is fresh.
    fetch_upstream(&repo).await?;

    // Diff BEFORE reset so we know which builds to run. After the reset
    // HEAD == vco_upstream/<branch> and the diff would be empty.
    // Unknown ⇒ rebuild everything (same reasoning as
    // `apply_launcher_update`: a skipped necessary build is invisible, an
    // unnecessary one is merely slow).
    let (needs_cargo, needs_npm) = match run_git(
        &repo,
        &[
            "diff",
            "--name-only",
            &format!("HEAD..{}/{}", VCO_UPSTREAM_REMOTE, branch),
        ],
    )
    .await
    {
        Ok(pre_diff) => (
            changed_paths_need_cargo(&pre_diff),
            changed_paths_need_npm(&pre_diff),
        ),
        Err(e) => {
            tracing::warn!(
                "[vct] force_resync_launcher: pre-reset diff against {}/{} failed ({}) — \
                 rebuilding BOTH cargo and npm",
                VCO_UPSTREAM_REMOTE,
                branch,
                e
            );
            (true, true)
        }
    };

    // Destructive step. After this point local divergent commits are gone.
    //
    // v0.2.95 phase 3: the hub is stopped and both binaries renamed aside
    // first — `launcher/dist/*/vct-hub*` are TRACKED, so this reset writes them
    // exactly as a pull would. See `stop_hub_then_hard_reset`. The RESTART is
    // owed by `finish_apply_after_pull` below, which already performs it
    // (`ArtefactSource::SourceOnly` -> `HubRestartContext::AbortRecovery`,
    // which polls /health rather than trusting a cutover sentinel install.py
    // never wrote on this path).
    let renames = stop_hub_then_hard_reset(
        &repo,
        &format!("{}/{}", VCO_UPSTREAM_REMOTE, branch),
    )
    .await?;

    // v0.2.95 phase 2: the reset SUPERSEDES any half-finished update, so clear
    // the paired resume sentinel + deferral it leaves behind.
    //
    // This is new only because the state is new. Pre-phase-2 a conflict on this
    // surface aborted the merge and wrote no sentinel; now the shared pipeline
    // leaves the tree conflicted and writes the paired record, which is what
    // gives the user a NON-destructive way out (the MenuBar's Continue Update).
    // A user who instead opts into this destructive one must not be left with a
    // badge still offering to resume an update that no longer exists — the
    // sentinel would otherwise outlive the thing it describes. Same paired
    // helper the pipeline itself clears with, so the two cannot be written or
    // cleared apart (v0.2.51 Bug A / v0.2.53 DEDUP-14).
    crate::commands::installer::clear_update_resume_sentinel(&repo);
    crate::commands::installer::clear_update_resume_deferral_if_solo(&repo);

    // `SourceOnly`: this path resets the tree and rebuilds the launcher. It has
    // never run install.py and still does not — so hooks, templates, MCP
    // registrations, the venv, the KG seed and the schema stay at the old
    // version, and the manifest must say so rather than claim a completed
    // install at the new one (the H2 half-state).
    // No update gate on this path: `force_resync_launcher` arms none.
    finish_apply_after_pull(
        app,
        &repo,
        needs_cargo,
        needs_npm,
        ArtefactSource::SourceOnly,
        None,
        &renames,
    )
    .await
}

/// A rebuild failed with the hub stopped and the binaries renamed aside: put
/// them back, restart the hub, and hand the caller its error unchanged.
///
/// v0.2.95 phase 3. One home for the two rebuild legs so they cannot disagree
/// about whether the recovery runs — the shape this project keeps finding is
/// "same failure, one leg short" (phase 2 §3 item 3 was that exact defect on
/// the install.py spawn legs).
fn restore_after_failed_rebuild(
    repo: &Path,
    renames: &crate::commands::update_pipeline::PrePullRenames,
    err: String,
) -> String {
    tracing::warn!(
        "[vct] finish_apply_after_pull: rebuild failed ({}) — restoring the \
         pre-update binaries and restarting vct-hub before reporting",
        err
    );
    crate::commands::installer::abort_update_restore_binaries_and_hub(
        repo,
        renames.launcher.as_deref(),
        renames.hub.as_deref(),
    );
    err
}

/// Shared post-pull / post-reset rebuild + restart sequence. Extracted so
/// `apply_launcher_update` and `force_resync_launcher` can't drift apart.
///
/// `artefacts` says which path applied the artefacts before this tail ran, and
/// it is NOT a stylistic parameter — see [`ArtefactSource`]. Two things in here
/// are only correct for one of its values, and both used to be done
/// unconditionally on the assumption that install.py never runs on this
/// surface. Since v0.2.95 phase 2 it usually does.
async fn finish_apply_after_pull<R: Runtime>(
    app: AppHandle<R>,
    repo: &Path,
    needs_cargo: bool,
    needs_npm: bool,
    artefacts: ArtefactSource,
    gate: Option<&mut crate::commands::update_gate::UpdateInProgressGuard>,
    renames: &crate::commands::update_pipeline::PrePullRenames,
) -> Result<(), String> {
    // Step 4: rebuild. We do this synchronously (the user clicked "Update
    // now" / "Resync now" — they're waiting). Failures bubble up and the
    // launcher stays on the old binary, which is the safe behavior.
    //
    // v0.2.95 phase 3 — but they no longer bubble up BARE. Both callers reach
    // here with the hub STOPPED and (on Windows) both binaries renamed aside:
    // the pipeline stops it for `apply_launcher_update`, and
    // `stop_hub_then_hard_reset` for `force_resync_launcher`. A bare `?` here
    // returned with the hub still down and the binaries still aside — a
    // perma-stopped hub after a failed `cargo build`, which is precisely the
    // failure `07101d30` added the revert-on-every-early-return for on the
    // installer surface. Same shared tail, same reason.
    if needs_cargo {
        if let Err(e) = rebuild_cargo(repo).await {
            return Err(restore_after_failed_rebuild(repo, renames, e));
        }
    }
    if needs_npm {
        if let Err(e) = rebuild_frontend(repo).await {
            return Err(restore_after_failed_rebuild(repo, renames, e));
        }
    }

    // v0.2.91 WI-4: Surface B parity — route through the SHARED staging +
    // stage1-handoff tail before the restart hop.
    //
    // Pre-v0.2.91 this surface had NO staging and NO handoff: on Windows a
    // dist binary the pull skipped (mandatory lock) stayed stale, and the
    // `current_exe()` respawn below re-executed the SAME old binary — the
    // stale-binary relaunch loop, on this surface, by construction. The tail
    // lives in `services::binary_freshness` and is byte-identical to the one
    // `installer::finalize_update_and_restart` runs, so the two surfaces
    // cannot drift.
    //
    // No-op on POSIX (nothing to stage; the handoff reports "non-windows"),
    // so Linux/macOS behaviour is unchanged.
    // ORDERING (load-bearing): staged + armed HERE, but the exit hop happens
    // at the very bottom — AFTER the desktop-shortcut / install-manifest /
    // hardware-redetect bookkeeping below. Exiting straight from here would
    // skip all three on the handoff path.
    //
    // CORRECTED v0.2.95 phase 2: this used to add "and unlike the installer
    // surface this flow never runs install.py, so nothing else would record
    // the new version". On the `ArtefactSource::InstallPy` path install.py DID
    // run and DID record it, and the manifest write below now stands down for
    // exactly that reason. The ordering argument stands on its own: the
    // shortcut and the hardware-redetect flag are still only written here.
    // V52-AI: explicit lockfile cleanup BEFORE the restart/exit hop, and
    // before staging — the SAME ordering, for the same reason, that
    // `installer::finalize_update_and_restart` documents at length.
    //
    // This arrives with v0.2.95 phase 2 because the guard does: this surface
    // never armed one before it started pulling through the shared pipeline.
    // Relying on `Drop` here would be the C-2 bug class verbatim — this
    // function ends in `app.exit(0)`, which on Windows can terminate the
    // process before the guard's `Drop` runs, leaving a
    // `.update-in-progress.json` with a fresh 15-minute deadline that makes
    // every MCP spawn exit 75 until it lapses.
    //
    // Before staging, not after: `binary_freshness` treats an armed gate as
    // "an update owns the tree" and stands its at-rest pass down, and the
    // staging below is this update's own delivery step.
    //
    // `None` for `force_resync_launcher`, which arms no gate.
    if let Some(guard) = gate {
        guard.disarm_and_cleanup();
    }

    let handoff = crate::services::binary_freshness::stage_and_handoff_after_update(
        repo,
        &repo.display().to_string(),
    )
    .await;

    // The pipeline STOPPED the hub before the pull (`launcher/dist/*/vct-hub*`
    // are tracked files), so whoever stopped it owes the restart. The installer
    // surface does this inside `finalize_update_and_restart`; this is the same
    // call with the same C-1 ordering constraint — AFTER staging, so a freshly
    // restarted hub cannot hold a Windows lock on `vct-hub.exe` while
    // `vct-updater` tries to swap it.
    //
    // Skipped entirely on the handoff path for that reason: the updater owns
    // the swap and will start from a clean slate, and the next launcher boot
    // runs `hub_launcher::ensure_hub_running` anyway.
    //
    // The context is not a detail. `PostInstall` trusts install.py's own
    // /health probe via the cutover sentinel; on a source-only path install.py
    // never wrote that sentinel, so reading its absence as "health validated"
    // is the v0.2.89 §7.2 hole — `AbortRecovery` refuses the skip and actually
    // polls. Soft-fail by contract: never block the restart.
    if !handoff.handoff_active {
        let ctx = match artefacts {
            ArtefactSource::InstallPy => crate::commands::installer::HubRestartContext::PostInstall,
            // `Unchanged` shares `SourceOnly`'s context for the same reason
            // and not by accident: install.py did not run on either, so there
            // is no cutover sentinel whose absence could be read as "health
            // already validated", and the /health poll must really happen.
            ArtefactSource::SourceOnly | ArtefactSource::Unchanged => {
                crate::commands::installer::HubRestartContext::AbortRecovery
            }
        };
        if let Err(e) = crate::commands::installer::ensure_hub_started_after_update(repo, ctx) {
            tracing::warn!(
                "[apply_launcher_update] vct-hub restart after update reported: {} \
                 (non-fatal; the next launcher boot retries)",
                e
            );
        }
    }

    // Step 5: restart. Spawn the same binary path as a new process, then
    // exit the current one. On all three platforms `current_exe()` returns
    // the path that was used to launch us, which is what we want post-
    // rebuild because the new binary lives at the same path.
    //
    // v0.2.91 WI-4 exception: on Windows the pre-pull rename may have moved
    // US to `<name>.old-<pid>`, and `current_exe()` follows the rename — so
    // respawning it verbatim would launch the OLD binary we just moved aside.
    // Recover the canonical sibling when it exists.
    let exe = {
        let running = std::env::current_exe().map_err(|e| e.to_string())?;
        match crate::services::binary_freshness::canonical_path_for_backup(&running) {
            Some(canonical) if canonical.is_file() => {
                tracing::info!(
                    "[apply_launcher_update] running from a pre-pull backup ({}); relaunching \
                     the canonical binary at {} instead",
                    running.display(),
                    canonical.display(),
                );
                canonical
            }
            _ => running,
        }
    };

    // C3 (v0.2.6): refresh the desktop shortcut so it picks up any
    // change in binary path/contents post-rebuild. The launcher repo is
    // the install path here (self-update operates on the launcher's
    // enclosing checkout). Soft-fail: never block restart.
    if let Err(e) = crate::commands::desktop_shortcut::refresh_desktop_shortcut(repo, &exe) {
        tracing::warn!(
            "[apply_launcher_update] desktop shortcut refresh failed (non-fatal): {}",
            e
        );
    }

    // Bug G (v0.2.8): refresh the install-manifest so the next session reports
    // the right source. `repo` here is the launcher's enclosing install root
    // (find_launcher_repo_root returns the dir containing launcher/).
    // Soft-fail: never block restart.
    //
    // v0.2.95 phase 2 (WP-1) — this is now conditional, and which way it goes
    // is the whole point:
    //
    // * `InstallPy` — install.py already wrote the manifest, truthfully, a few
    //   seconds ago: `install_method` "update", `version` re-read from the new
    //   tree, `completed_at` now. Writing over it here would replace an honest
    //   installer record with a launcher-path one and make
    //   `doctor.probe_install_completeness` explain a healthy install with
    //   "the marker was last written by the launcher's `launcher_update` path,
    //   which advances the source tree without running install.py" — a
    //   sentence that would then be false.
    // * `SourceOnly` — the source tree moved and the installer did NOT run, so
    //   the refresh is what records that honestly. `refresh_install_manifest`
    //   advances `source_commit` and stamps `post_source_only`, and
    //   deliberately does NOT advance `version`; see `commands::manifest` for
    //   why that single choice is what makes the state both visible and
    //   repairable.
    // * `Unchanged` — NOTHING moved, so there is nothing to record and the
    //   manifest must come out of this byte-identical (v0.2.95 ship-gate
    //   MAJOR-2). The write is not merely redundant here: it would stamp
    //   `post_source_only: true`, which is a claim about an advance that did
    //   not happen, and `check_for_updates` turns that claim into an
    //   `install_stale` badge demanding a full re-install.
    if owes_manifest_refresh(artefacts) {
        if let Err(e) = crate::commands::manifest::refresh_install_manifest(repo, "launcher_update")
        {
            tracing::warn!(
                "[apply_launcher_update] install-manifest refresh failed (non-fatal): {}",
                e
            );
        }
    }

    // v0.2.34 (Agent B): mark the next launcher boot as needing a
    // hardware re-detect. The launcher process is about to exit and
    // respawn; spawning a `redetect_hardware` task HERE would be
    // killed before completion. Instead we set an `app_state` flag
    // that the NEW launcher process reads on boot via
    // `consume_pending_hardware_redetect_if_set` and turns into a
    // background redetect job. Catches the v0.2.20-style "new field
    // added to HardwareSnapshot" case: every launcher update that
    // ships a snapshot-schema change automatically refreshes the
    // user's persisted snapshot on next boot, regardless of what
    // shape was on disk before. Soft-fail.
    if let Some(db) = app.try_state::<crate::db::Db>() {
        crate::commands::installer::mark_hardware_redetect_pending_after_update(
            db.inner(),
        );
    } else {
        tracing::warn!(
            "[apply_launcher_update] could not acquire Db State to mark hardware-redetect-pending; the next boot will skip the post-update redetect (Preferences button remains available)."
        );
    }

    // v0.2.91 WI-4: when the stage1 handoff fired, `vct-updater` owns both the
    // swap and the relaunch — spawning `exe` ourselves here would start the
    // OLD binary (the very file the updater is waiting to replace) and race it.
    // Exit and let the updater do its job.
    if handoff.handoff_active {
        tracing::info!(
            "[apply_launcher_update] stage1 handoff active (lock={:?}); exiting so vct-updater \
             can swap the locked binaries and relaunch",
            handoff.lock_path,
        );
        crate::quit_dialog::force_quit();
        app.exit(0);
        return Ok(());
    }

    std::process::Command::new(&exe).silent()
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
///
/// `pub(crate)` so the orchestrator-update path
/// (`commands::installer::update_orchestrator`) can share the same
/// detection logic for its own divergence modal (B4 / D19, v0.2.23).
pub(crate) fn is_non_fast_forward(err: &str) -> bool {
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
pub(crate) fn serialize_non_ff_error(
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

/// Returns the first tracked-file change that would be clobbered by `git
/// pull --ff-only`. Untracked files (status code `??`) are ignored —
/// they're not at risk during a fast-forward merge.
///
/// v0.2.91 WI-4 — GENERATED / release-controlled paths are ignored too.
///
/// Why: this guard runs at Step 1 of `apply_launcher_update`, BEFORE the F1
/// byte-identical restore and BEFORE `resolve_generated_files_to_upstream`.
/// A dirty `launcher/dist/<arch>/vct-launcher.exe` — precisely what a failed
/// Windows binary swap leaves behind — therefore hard-blocked this surface with
/// "Uncommitted changes on tracked file … would be lost", and the reconcile
/// built to auto-resolve that exact class was unreachable. The user's only
/// remaining forward action on this surface was the DESTRUCTIVE resync.
///
/// The excluded set is the shared `GENERATED_RELEASE_CONTROLLED_PATTERNS`
/// allowlist, classified with the shared globset builder (one home — the
/// pattern list and its glob semantics are not restated here). Everything else
/// still blocks: a hand-edited `Cargo.toml` / `*.rs` / `*.py` is a real signal
/// and must not be silently pulled over.
///
/// v0.2.95 — RENDERED root files are ignored too, for the SAME reason and by
/// the same precedent. `resolve_rendered_files_keep_local` was wired into this
/// surface (`apply_launcher_update`, just after the pre-pull rename) so a
/// rendered path would stop forcing the resync modal — but it sits BELOW this
/// Step-1 guard, so for the one path that class exists to protect it was
/// unreachable exactly as the generated reconcile had been. `install.py`
/// RENDERS `CLAUDE.md` over its tracked blob at first install and at every
/// `--update`, so **every orchestrator-root install is permanently dirty on it
/// by construction** and this guard refused the launcher self-update for all of
/// them, with no forward action but the destructive resync. That is the blunt
/// proxy v0.2.58 removed from the `update_orchestrator` surface and never
/// removed from this one (see
/// `knowledge/concepts/update-gate-pop-conflict-risk-not-dirty-tree-2026-06-14.md`).
///
/// SCOPE, and why it stops here. Only classes this surface actually RESOLVES
/// downstream are exempted. `USER_EDITABLE_PATTERNS` is deliberately NOT
/// exempted: its 3-way merge (`run_pre_merge_user_editable`) has exactly two
/// call sites, both in `commands::installer` — this surface has no A0
/// pre-merge step, so a dirty `knowledge/**/*.md` here has nothing downstream
/// to protect it. Exempting it would trade a clear, actionable refusal for an
/// opaque `git` abort routed to the resync modal, whose only forward action is
/// `reset --hard`. A refusal the user can act on is better than a modal that
/// offers to delete their work.
/// The one-path view of [`blocking_changes`], for the tests that pin the
/// CLASSIFICATION half of this guard (which dirty paths a downstream leg
/// claims) independently of the upstream-overlap half.
///
/// `#[cfg(test)]` on purpose: since v0.2.95 production refuses through
/// [`first_change_at_risk`], which needs the whole list. Leaving this callable
/// from production would invite a future caller back onto the blunt gate this
/// release removed.
#[cfg(test)]
fn first_blocking_change(status_z: &[u8]) -> Option<String> {
    blocking_changes(status_z).into_iter().next()
}

/// Every tracked-modified path that no downstream leg of THIS surface
/// resolves, in `git status` order.
///
/// `first_blocking_change` is the one-path view of this, kept because a
/// refusal names one path. The caller needs the FULL list: it intersects it
/// with the upstream-changed set, and "the first unresolved path" and "the
/// first path that can actually conflict" are not the same path.
fn blocking_changes(status_z: &[u8]) -> Vec<String> {
    let mut out = Vec::new();
    // Built once per call; four patterns. On a (never-observed) malformed
    // pattern, fall back to "exclude nothing" — the pre-v0.2.91 behaviour,
    // which blocks rather than silently pulling over a dirty file.
    let generated =
        crate::commands::git_user_editable_merge::build_generated_release_controlled_globset().ok();
    // v0.2.95 MINOR-A: the SHARED `-z` walk, not a second porcelain parser.
    // The result is intersected with `tracked_modified_overlapping_upstream`'s,
    // so the two must produce the same SPELLING of a path — see that function's
    // docs for the rename / quoted-path divergence this removes.
    for path in crate::commands::git_user_editable_merge::parse_tracked_modified_z(status_z) {
        if let Some(gs) = generated.as_ref() {
            if crate::commands::git_user_editable_merge::is_generated_release_controlled(&path, gs)
            {
                // Handled downstream by F1 + the take-upstream reconcile.
                continue;
            }
        }
        // Handled downstream by the rendered reconcile, which still runs
        // BEFORE the pull — but no longer by a call from this surface.
        // v0.2.95 phase 2 moved it into the shared pipeline's A0 step, so the
        // reach is transitive: `prepare_and_pull_orchestrator_repo` →
        // `reconcile_and_pull` → `run_pre_merge_user_editable` →
        // `git_user_editable_merge::pre_merge_user_editable` →
        // `resolve_rendered_files_keep_local_at`. Grepping THIS file for the
        // reconcile finds nothing, which is why the sentence is worth
        // correcting rather than deleting: a reader who checks the old claim,
        // finds no call, and concludes the exemption is unbacked would delete
        // an exemption that is still earned.
        // Table-driven (`is_rendered_root_file` reads
        // `vco_lib/rendered_root_files.toml`), so adding a rendered path there
        // exempts it here with no second list to keep in step.
        if crate::commands::git_user_editable_merge::is_rendered_root_file(&path) {
            continue;
        }
        out.push(path);
    }
    out
}

/// The path this surface must refuse on, or `None` when nothing can conflict.
///
/// v0.2.95 — the second half of removing the blunt proxy. `blocking_changes`
/// answers "which dirty tracked paths has no downstream leg claimed"; this
/// answers the question that actually decides a refusal: *can the pull hurt any
/// of them*. It can only hurt a path upstream ALSO changed — the v0.2.58 risk
/// set `tracked-modified ∩ upstream-changed`, reused here through the SAME
/// helper `update_orchestrator` uses rather than a second intersection.
/// A tracked-modified file upstream did not touch survives both arms of the
/// pull untouched: `--ff-only` only rewrites entries whose merged value
/// differs, and an `--autostash` pop replays cleanly onto unchanged content.
///
/// CONSERVATIVE ON EVERY UNKNOWN. If the merge base or the upstream tip cannot
/// be resolved, or the helper reports it could not read status, we refuse on
/// the first unresolved path exactly as before. "I could not prove this is
/// safe" must read as "block", never as "proceed" — the whole point of the
/// guard is that the user's uncommitted work is unrecoverable if we are wrong.
///
/// v0.2.95 phase 2: the CALL moved into the shared pipeline (as
/// `update_pipeline::ExtraPreflight::RefuseDirtyTrackedAtRisk`) so it runs
/// AFTER the in-progress-merge refusal and still before anything mutates. The
/// decision stays here, and stays this surface's alone — see that enum's doc
/// for why the installer surface must NOT have it.
pub(crate) async fn first_change_at_risk(
    repo: &Path,
    branch: &str,
    status_z: &[u8],
) -> Option<String> {
    let candidates = blocking_changes(status_z);
    let first = candidates.first()?.clone();

    use crate::commands::git_user_editable_merge as gum;
    let (Ok(Some(base)), Ok(Some(theirs))) = (
        gum::compute_base_sha(repo, branch).await,
        gum::compute_theirs_sha(repo, branch).await,
    ) else {
        tracing::warn!(
            "[vct] apply_launcher_update: could not resolve the merge base / upstream tip for \
             {} — refusing on '{}' without narrowing (conservative)",
            branch,
            first
        );
        return Some(first);
    };

    let risky = match gum::tracked_modified_overlapping_upstream(repo, &base, &theirs).await {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!(
                "[vct] apply_launcher_update: could not compute the pop-conflict-risk set ({}) \
                 — refusing on '{}' without narrowing (conservative)",
                e,
                first
            );
            return Some(first);
        }
    };
    // The helper signals "I could not read `git status`" with this sentinel
    // rather than an Err. It is not a path, so a naive intersection would
    // silently come out EMPTY and UNBLOCK — the dangerous direction.
    if risky.iter().any(|p| p == "<status-read-failed>") {
        tracing::warn!(
            "[vct] apply_launcher_update: the risk set could not be read — refusing on '{}' \
             without narrowing (conservative)",
            first
        );
        return Some(first);
    }

    let blocker = candidates.into_iter().find(|c| risky.contains(c));
    if blocker.is_none() {
        tracing::info!(
            "[vct] apply_launcher_update: {} dirty tracked path(s) upstream did not touch — not \
             a pop-conflict risk, proceeding (v0.2.58 model, now on this surface too)",
            risky.len().max(1)
        );
    }
    blocker
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

    let fut = TokioCommand::new("cargo").silent()
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

    let fut = TokioCommand::new("npm").silent()
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
///
/// Stays a PURE file read (v0.2.93 review round 1, MINOR-5): this is a sync
/// command and the tray builder calls it on the main thread, so it must not
/// spawn git. The repo-aware view lives on
/// [`get_cached_update_status_refreshed`] for the Updates page.
#[command]
pub fn get_cached_update_status() -> UpdateStatus {
    cached_update_status_for(None)
}

/// v0.2.93 (stale status cache): the Updates page's variant. Consults the
/// install root's git HEAD (three local `git rev-parse`/`merge-base` calls,
/// no network) so `current_sha` / `branch` are real and a cached "N commits
/// behind" is retracted once HEAD already contains the cached remote SHA —
/// the field card that said "4 commits behind / Last checked 7:17 PM" long
/// after the merge completed from a shell. Async, and the git spawns run on
/// the blocking pool, so neither the IPC thread nor the tray is blocked. Not
/// from a checkout ⇒ the pure cache view. See [`cached_update_status_for`].
#[command]
pub async fn get_cached_update_status_refreshed() -> UpdateStatus {
    let repo = find_launcher_repo_root().ok();
    match tokio::task::spawn_blocking(move || cached_update_status_for(repo.as_deref())).await {
        Ok(status) => status,
        Err(e) => {
            tracing::warn!(
                "[vct] get_cached_update_status_refreshed: blocking probe panicked ({}) — \
                 returning the pure cache view",
                e
            );
            cached_update_status_for(None)
        }
    }
}

/// Path-injectable body of [`get_cached_update_status`] /
/// [`get_cached_update_status_refreshed`].
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
/// the next scheduled tick. Called from `installer::update_orchestrator`'s
/// inline tail and from `run_post_pull_install_and_restart` (merge / rebase
/// / resume).
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
// (`finish_apply_after_pull`). The binary-swap mechanism is correct;
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
    // for other consumers.
    let _ = serialized_fetch_upstream(&repo, FetchPolicy::Tags, None).await;

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
/// `update_orchestrator` could even fast-forward them, but returning to a
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

/// Compare a running launcher version (from `CARGO_PKG_VERSION`) with
/// the latest source release tag. Returns `true` iff the two differ in a
/// way that indicates the binary swap lagged the source tag — i.e. the
/// user is running an OLDER binary than the latest tagged release.
///
/// Comparison rules (kept deliberately permissive — see tests):
///   - Tag string is normalized by stripping a leading `v` if present
///     (`v0.2.34` → `0.2.34`). `CARGO_PKG_VERSION` is bare.
///   - Trailing whitespace stripped from both sides.
///   - String equality after normalization is the success path. We do
///     NOT do SemVer-aware comparison — the only producers of these
///     strings are `Cargo.toml` and `git tag`, both of which we control,
///     and a mismatch in either direction (running > latest, running <
///     latest) deserves a warning. Strict equality keeps the test matrix
///     small and avoids a SemVer dep.
///   - Empty / whitespace-only `latest_tag` → returns `false` (no signal
///     to warn on; the upstream might genuinely have no tags yet).
///
/// `pub` (not `pub(crate)`) so the test module can reach it without
/// declaring a sibling, and so a future MCP-side caller could reuse it.
pub fn running_version_lags_tag(running: &str, latest_tag: &str) -> bool {
    let r = running.trim();
    let t = latest_tag.trim().trim_start_matches('v');
    if t.is_empty() || r.is_empty() {
        return false;
    }
    r != t
}

/// Tauri-callable wrapper around `running_version_lags_tag`. The Svelte
/// page mirrors the same logic client-side for snappy banner rendering,
/// but exposing a server-side answer here lets a future caller (CLI
/// subcommand, MCP query, an installer hook that wants to skip
/// follow-up work when the binary is known-stale) reach the same
/// decision without re-implementing the comparison.
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

    // -----------------------------------------------------------------
    // v0.2.95 ship-gate MAJOR-2 — a no-op "Update now" must not stamp the
    // manifest.
    // -----------------------------------------------------------------

    /// A temp install root carrying the manifest a REAL install.py run wrote.
    /// Returns the guard (kept alive by the caller) and the manifest path.
    fn root_with_installer_written_manifest() -> (tempfile::TempDir, PathBuf) {
        let td = tempfile::tempdir().expect("tempdir");
        let root = td.path().to_path_buf();
        std::fs::create_dir_all(root.join("state")).unwrap();
        std::fs::write(
            root.join("state").join("install-manifest.json"),
            "{\n  \"schema_version\": 1,\n  \"installed\": true,\n  \
             \"installed_at\": \"2026-09-01T10:00:00Z\",\n  \
             \"completed_at\": \"2026-09-01T10:05:00Z\",\n  \
             \"version\": \"0.2.95\",\n  \
             \"install_method\": \"update\",\n  \
             \"source_commit\": \"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"\n}\n",
        )
        .unwrap();
        std::fs::write(root.join("vct-module.json"), "{\"version\":\"0.2.95\"}").unwrap();
        let manifest = root.join("state").join("install-manifest.json");
        (td, manifest)
    }

    /// Run the tail's manifest step exactly as `finish_apply_after_pull` does
    /// — the decision function plus the one writer it gates — and hand back
    /// the manifest bytes afterwards.
    fn manifest_bytes_after_tail(root: &Path, manifest: &Path, artefacts: ArtefactSource) -> Vec<u8> {
        if owes_manifest_refresh(artefacts) {
            crate::commands::manifest::refresh_install_manifest(root, "launcher_update").unwrap();
        }
        std::fs::read(manifest).unwrap()
    }

    /// THE ship-gate MAJOR-2 assertion, as bytes rather than as a claim.
    ///
    /// `apply_launcher_update`'s already-up-to-date branch reaches the tail
    /// with `Unchanged`. HEAD did not move, so `state/install-manifest.json`
    /// must come out of the click exactly as it went in — byte for byte,
    /// `post_source_only` included (its absence is what keeps
    /// `check_for_updates` from raising `install_stale` and demanding a full
    /// `apply_pending_install` after a click that changed nothing).
    ///
    /// Revert the call site to `ArtefactSource::SourceOnly`, or widen
    /// `owes_manifest_refresh` to answer `true` for `Unchanged`, and this goes
    /// red on the byte comparison.
    #[test]
    fn an_already_up_to_date_update_leaves_the_install_manifest_byte_identical() {
        let (_td, manifest) = root_with_installer_written_manifest();
        let root = manifest.parent().unwrap().parent().unwrap().to_path_buf();
        let before = std::fs::read(&manifest).unwrap();

        let after = manifest_bytes_after_tail(&root, &manifest, ArtefactSource::Unchanged);

        assert_eq!(
            before, after,
            "an already-up-to-date update moved nothing, so it must not \
             rewrite the install manifest at all",
        );
        // And specifically: the flag that lights `install_stale` was not added.
        let v: serde_json::Value = serde_json::from_slice(&after).unwrap();
        assert!(
            v.get("post_source_only").is_none(),
            "a no-op update must not claim the source tree advanced without \
             install.py: {v}",
        );
    }

    /// The LEAVE-ALONE arm's counterpart: the variant that DOES mean "the tree
    /// moved without install.py" still writes. Without this, the fix above
    /// could be "never refresh", which would silently retire the WP-1 repair
    /// affordance (the whole point of `post_source_only`).
    #[test]
    fn a_source_only_update_still_records_the_advance_in_the_manifest() {
        let (_td, manifest) = root_with_installer_written_manifest();
        let root = manifest.parent().unwrap().parent().unwrap().to_path_buf();
        let before = std::fs::read(&manifest).unwrap();

        let after = manifest_bytes_after_tail(&root, &manifest, ArtefactSource::SourceOnly);

        assert_ne!(
            before, after,
            "a source-only advance must be recorded, or the half-updated \
             state goes back to being invisible",
        );
        let v: serde_json::Value = serde_json::from_slice(&after).unwrap();
        assert_eq!(
            v.get("post_source_only").and_then(|b| b.as_bool()),
            Some(true),
            "the source-only path names the state it left behind",
        );
        // …and `version` is still install.py's alone (WP-1).
        assert_eq!(v.get("version").and_then(|s| s.as_str()), Some("0.2.95"));
    }

    /// The third arm. install.py wrote the manifest itself moments ago; the
    /// tail overwriting it with a launcher-path record is the state WP-1
    /// exists to prevent.
    #[test]
    fn an_install_py_update_leaves_the_manifest_to_install_py() {
        let (_td, manifest) = root_with_installer_written_manifest();
        let root = manifest.parent().unwrap().parent().unwrap().to_path_buf();
        let before = std::fs::read(&manifest).unwrap();

        let after = manifest_bytes_after_tail(&root, &manifest, ArtefactSource::InstallPy);

        assert_eq!(
            before, after,
            "install.py is the only writer of its own completion record",
        );
    }

    /// Build `git status --porcelain -z` stdout from logical rows.
    ///
    /// `-z` is NUL-separated with no trailing newline; a rename/copy row is
    /// written here as two entries (new path first, then the old one) exactly
    /// as git emits it.
    fn z(rows: &[&str]) -> Vec<u8> {
        let mut out = Vec::new();
        for r in rows {
            out.extend_from_slice(r.as_bytes());
            out.push(0);
        }
        out
    }

    #[test]
    fn blocking_change_ignores_untracked() {
        let porcelain = z(&["?? .claude/CONTEXT_STATE.md", "?? state/runtime.db"]);
        assert_eq!(first_blocking_change(&porcelain), None);
    }

    #[test]
    fn blocking_change_catches_modified_tracked() {
        let porcelain = z(&[" M Cargo.toml", "?? .claude/CONTEXT_STATE.md"]);
        assert_eq!(first_blocking_change(&porcelain), Some("Cargo.toml".into()));
    }

    #[test]
    fn blocking_change_catches_staged() {
        let porcelain = z(&["M  src-tauri/src/lib.rs"]);
        assert_eq!(
            first_blocking_change(&porcelain),
            Some("src-tauri/src/lib.rs".into())
        );
    }

    /// v0.2.91 WI-4 RED-PROOF: a dirty `launcher/dist/<arch>/vct-launcher.exe`
    /// is EXACTLY what a failed Windows binary swap (or a hand-copied recovery
    /// binary) leaves behind. On `bd8f6836` this returned `Some(path)` and the
    /// self-update surface hard-refused with "Uncommitted changes on tracked
    /// file … would be lost" — BEFORE the F1 restore and BEFORE the
    /// take-upstream reconcile that exists to auto-resolve this exact class.
    /// The only forward action left to the user was the destructive resync.
    #[test]
    fn blocking_change_ignores_generated_release_controlled_dist_binaries() {
        let porcelain = z(&[" M launcher/dist/windows-x64/vct-launcher.exe"]);
        assert_eq!(
            first_blocking_change(&porcelain),
            None,
            "a dirty dist binary must not block the self-update surface — F1 + the \
             take-upstream reconcile downstream own it"
        );
    }

    /// The rest of the shared generated/release-controlled allowlist is
    /// excluded too (one home: the same globset the installer surface uses).
    #[test]
    fn blocking_change_ignores_lockfiles_and_package_json() {
        for p in [
            "launcher/package.json",
            "launcher/package-lock.json",
            "launcher/src-tauri/Cargo.lock",
            "launcher/dist/linux-x64/vct-hub",
            "launcher/dist/windows-x64/vct-launcher.exe.metadata.json",
        ] {
            assert_eq!(
                first_blocking_change(&z(&[&format!(" M {}", p)])),
                None,
                "{} is release-controlled and must not block",
                p
            );
        }
    }

    /// Both-sides discipline: the guard must STILL block on a hand-authored
    /// source file. Widening the exclusion past the allowlist would silently
    /// pull over a user's real edit — the failure this guard exists to prevent.
    #[test]
    fn blocking_change_still_blocks_hand_authored_sources() {
        for p in [
            "launcher/src-tauri/Cargo.toml",
            "launcher/src-tauri/tauri.conf.json",
            "install.py",
            "launcher/src-tauri/src/lib.rs",
            "vct-module.json",
            // Near-miss paths that must NOT be swallowed by the glob.
            "launcher/distX/foo",
            "launcher/package.json.bak",
        ] {
            assert_eq!(
                first_blocking_change(&z(&[&format!(" M {}", p)])),
                Some(p.to_string()),
                "{} is hand-authored — a local edit there is a real signal",
                p
            );
        }
    }

    /// Mixed porcelain: the excluded dist rows are skipped but a real blocker
    /// later in the listing is still reported (the loop must not stop at the
    /// first excluded row).
    #[test]
    fn blocking_change_scans_past_excluded_rows() {
        let porcelain = z(&[
            " M launcher/dist/windows-x64/vct-launcher.exe",
            "?? .claude/CONTEXT_STATE.md",
            "M  launcher/src-tauri/src/lib.rs",
        ]);
        assert_eq!(
            first_blocking_change(&porcelain),
            Some("launcher/src-tauri/src/lib.rs".into())
        );
    }

    // -----------------------------------------------------------------
    // v0.2.95 — the Step-1 guard is no longer a blunt "is the tree dirty"
    // proxy. Two halves, tested separately:
    //   * CLASSIFICATION (`blocking_changes`) — which dirty tracked paths
    //     has no downstream leg of THIS surface claimed;
    //   * HAZARD (`first_change_at_risk`) — of those, can the pull hurt any.
    // -----------------------------------------------------------------

    /// `install.py` renders CLAUDE.md over its tracked blob on every run, so
    /// EVERY orchestrator-root install is permanently dirty here. It used to
    /// hard-refuse this surface for all of them while
    /// `resolve_rendered_files_keep_local` — wired in below the guard,
    /// precisely to handle it — was unreachable.
    #[test]
    fn a_dirty_rendered_file_is_not_a_blocking_change() {
        assert_eq!(first_blocking_change(&z(&[" M CLAUDE.md"])), None);
        // Case-folded + backslash-separated, as Windows `git status` can emit.
        assert_eq!(first_blocking_change(&z(&[" M claude.md"])), None);
    }

    /// The exemption is table-driven, not a second hardcoded list: it must
    /// cover exactly what `vco_lib/rendered_root_files.toml` declares.
    #[test]
    fn the_rendered_exemption_reads_the_shared_table() {
        for entry in crate::commands::git_user_editable_merge::rendered_root_files() {
            assert_eq!(
                first_blocking_change(&z(&[&format!(" M {}", entry.path)])),
                None,
                "{} is declared RENDERED in rendered_root_files.toml, so the Step-1 guard must \
                 defer to resolve_rendered_files_keep_local instead of refusing",
                entry.path
            );
        }
    }

    /// The caller intersects with the upstream-changed set, so it needs every
    /// unresolved path — not just the first.
    #[test]
    fn blocking_changes_lists_every_unresolved_path_in_order() {
        let porcelain = z(&[
            " M CLAUDE.md",
            "?? scratch.txt",
            "M  vco_lib/a.py",
            "M  launcher/dist/linux-x64/vct-launcher",
            "M  vco_lib/b.py",
        ]);
        assert_eq!(
            blocking_changes(&porcelain),
            vec!["vco_lib/a.py".to_string(), "vco_lib/b.py".to_string()],
            "rendered + untracked + generated are resolved downstream; the two hand-authored \
             files are not"
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
    use std::sync::Mutex;

    /// Tests that mutate `VCO_UPSTREAM_URL` must hold this mutex — `cargo
    /// test` runs in-binary tests in parallel and the env var is process-
    /// global. Without serialization the override tests race.
    static ENV_MUTEX: Mutex<()> = Mutex::new(());

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

    /// Seed a bare upstream + a clone wired to it as `vco_upstream`, so
    /// `compute_base_sha` / `compute_theirs_sha` resolve for real.
    fn init_upstream_pair() -> (tempfile::TempDir, PathBuf) {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path().to_path_buf();
        let remote = root.join("remote.git");
        let seed = root.join("seed");
        let local = root.join("local");

        let g = |dir: &Path, args: &[&str]| {
            let ok = StdCommand::new("git")
                .args(args)
                .current_dir(dir)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .expect("git")
                .success();
            assert!(ok, "git {:?} failed", args);
        };

        assert!(StdCommand::new("git")
            .args(["init", "--bare", "--initial-branch=main"])
            .arg(&remote)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .expect("git init --bare")
            .success());

        std::fs::create_dir_all(seed.join("vco_lib")).unwrap();
        std::fs::create_dir_all(seed.join("knowledge").join("concepts")).unwrap();
        g(&seed, &["init", "--initial-branch=main"]);
        g(&seed, &["config", "user.email", "t@example.com"]);
        g(&seed, &["config", "user.name", "T"]);
        std::fs::write(seed.join("CLAUDE.md"), "# stub\n").unwrap();
        std::fs::write(seed.join("other.txt"), "base\n").unwrap();
        std::fs::write(seed.join("vco_lib").join("foo.py"), "def base(): pass\n").unwrap();
        std::fs::write(seed.join("vco_lib").join("bar.py"), "def base(): pass\n").unwrap();
        std::fs::write(
            seed.join("knowledge").join("concepts").join("foo.md"),
            "# foo\nbase\n",
        )
        .unwrap();
        g(&seed, &["add", "."]);
        g(&seed, &["commit", "-m", "seed"]);
        g(&seed, &["remote", "add", "origin", remote.to_str().unwrap()]);
        g(&seed, &["push", "origin", "main"]);

        assert!(StdCommand::new("git")
            .args(["clone"])
            .arg(remote.to_str().unwrap())
            .arg(&local)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .expect("git clone")
            .success());
        g(&local, &["config", "user.email", "t@example.com"]);
        g(&local, &["config", "user.name", "T"]);
        g(
            &local,
            &["remote", "add", VCO_UPSTREAM_REMOTE, remote.to_str().unwrap()],
        );

        // Advance upstream by one commit that touches `other.txt` only, then
        // make the clone's `vco_upstream/main` current.
        std::fs::write(seed.join("other.txt"), "base\nupstream\n").unwrap();
        g(&seed, &["add", "."]);
        g(&seed, &["commit", "-m", "upstream moves"]);
        g(&seed, &["push", "origin", "main"]);
        g(&local, &["fetch", VCO_UPSTREAM_REMOTE]);

        (tmp, local)
    }

    /// Helper: commit an extra upstream change to `rel`, then refresh the
    /// clone's remote-tracking ref.
    fn upstream_touch(tmp: &tempfile::TempDir, local: &Path, rel: &str, body: &str) {
        let seed = tmp.path().join("seed");
        let target = seed.join(rel);
        std::fs::create_dir_all(target.parent().unwrap()).unwrap();
        std::fs::write(&target, body).unwrap();
        for args in [
            vec!["add", "."],
            vec!["commit", "-m", "upstream change"],
            vec!["push", "origin", "main"],
        ] {
            assert!(StdCommand::new("git")
                .args(&args)
                .current_dir(&seed)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .expect("git")
                .success());
        }
        assert!(StdCommand::new("git")
            .args(["fetch", VCO_UPSTREAM_REMOTE])
            .current_dir(local)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .expect("git fetch")
            .success());
    }

    /// THE DEFECT, end to end at the guard: a rendered CLAUDE.md that upstream
    /// ALSO changed — the exact 0.2.93→0.2.94 shape — no longer refuses.
    #[tokio::test]
    async fn dirty_rendered_file_upstream_changed_does_not_refuse() {
        skip_if_no_git!();
        let (tmp, repo) = init_upstream_pair();
        upstream_touch(&tmp, &repo, "CLAUDE.md", "# stub v2\n");
        std::fs::write(repo.join("CLAUDE.md"), "# rendered\nMY OWN NOTES\n").unwrap();

        assert_eq!(
            first_change_at_risk(&repo, "main", &z(&[" M CLAUDE.md"])).await,
            None,
            "a RENDERED path is resolved by resolve_rendered_files_keep_local before the pull"
        );
    }

    /// The v0.2.58 model, now on this surface: a tracked-modified file upstream
    /// did NOT touch cannot pop-conflict, so it must not block. A fork that
    /// tracks its KG nodes hit this on every single update.
    #[tokio::test]
    async fn dirty_tracked_file_upstream_did_not_touch_does_not_refuse() {
        skip_if_no_git!();
        let (_tmp, repo) = init_upstream_pair();
        std::fs::write(
            repo.join("knowledge").join("concepts").join("foo.md"),
            "# foo\nmy local edit\n",
        )
        .unwrap();

        assert_eq!(
            first_change_at_risk(&repo, "main", &z(&[" M knowledge/concepts/foo.md"])).await,
            None,
            "upstream's commit touched other.txt only — this file cannot conflict"
        );
    }

    /// The refusal STANDS where content can genuinely be lost, and it names the
    /// path. This is the case git itself aborts on ("Your local changes to the
    /// following files would be overwritten by merge").
    #[tokio::test]
    async fn dirty_tracked_file_upstream_also_changed_still_refuses_and_names_it() {
        skip_if_no_git!();
        let (tmp, repo) = init_upstream_pair();
        upstream_touch(&tmp, &repo, "vco_lib/foo.py", "def upstream(): pass\n");
        std::fs::write(repo.join("vco_lib").join("foo.py"), "def mine(): pass\n").unwrap();

        assert_eq!(
            first_change_at_risk(&repo, "main", &z(&[" M vco_lib/foo.py"])).await,
            Some("vco_lib/foo.py".to_string())
        );
    }

    /// The guard picks the path that can actually conflict, not merely the
    /// first dirty one — the reason the caller needs the whole list.
    #[tokio::test]
    async fn the_named_path_is_the_one_upstream_changed() {
        skip_if_no_git!();
        let (tmp, repo) = init_upstream_pair();
        upstream_touch(&tmp, &repo, "vco_lib/bar.py", "def upstream(): pass\n");
        std::fs::write(repo.join("vco_lib").join("foo.py"), "def mine(): pass\n").unwrap();
        std::fs::write(repo.join("vco_lib").join("bar.py"), "def mine(): pass\n").unwrap();

        assert_eq!(
            first_change_at_risk(&repo, "main", &z(&[" M vco_lib/foo.py", " M vco_lib/bar.py"])).await,
            Some("vco_lib/bar.py".to_string()),
            "foo.py is dirty but upstream never touched it; bar.py is the real hazard"
        );
    }

    // --- v0.2.95 MINOR-A: the two parsers must spell a path identically ---

    /// A `-z` rename is TWO records (new path, then old). The guard must read
    /// the NEW path — the one the merge cares about, and the one the risk-set
    /// helper reports — not `old -> new` (which is what NON-`-z` porcelain
    /// gives, and which can never intersect the risk set).
    #[test]
    fn a_staged_rename_is_read_as_the_new_path() {
        let porcelain = z(&["R  vco_lib/renamed.py", "vco_lib/foo.py"]);
        assert_eq!(blocking_changes(&porcelain), vec!["vco_lib/renamed.py"]);
    }

    /// `-z` reports paths literally; NON-`-z` porcelain would hand back
    /// `"caf\303\251 note.py"` (quoted + octal-escaped under `core.quotePath`),
    /// a spelling the risk set never produces.
    #[test]
    fn unusual_paths_are_read_literally() {
        let porcelain = z(&[" M vco_lib/café note.py"]);
        assert_eq!(blocking_changes(&porcelain), vec!["vco_lib/café note.py"]);
    }

    /// Both sides of the intersection come from ONE function, so a fixture
    /// cannot be parsed two ways.
    #[test]
    fn the_guard_and_the_risk_set_share_one_parser() {
        let porcelain = z(&["R  vco_lib/renamed.py", "vco_lib/foo.py", " M vco_lib/café.py"]);
        assert_eq!(
            crate::commands::git_user_editable_merge::parse_tracked_modified_z(&porcelain),
            blocking_changes(&porcelain),
            "no class excludes these paths, so the shared parse must be the whole answer"
        );
    }

    /// THE BEHAVIOURAL PROOF of MINOR-A. A staged rename onto a path upstream
    /// also added is a genuine hazard. Parsed as `old -> new` it could never
    /// intersect the risk set, so the guard waved it through and the user met
    /// an opaque autostash abort instead of a sentence naming the file.
    #[tokio::test]
    async fn a_staged_rename_onto_an_upstream_path_still_refuses() {
        skip_if_no_git!();
        let (tmp, repo) = init_upstream_pair();
        upstream_touch(&tmp, &repo, "vco_lib/renamed.py", "def upstream(): pass\n");
        assert!(StdCommand::new("git")
            .args(["mv", "vco_lib/foo.py", "vco_lib/renamed.py"])
            .current_dir(&repo)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .expect("git mv")
            .success());

        // Read the REAL `-z` status, exactly as production does, so this test
        // cannot drift from the call site's format.
        let status = StdCommand::new("git")
            .args(["status", "--porcelain", "-z"])
            .current_dir(&repo)
            .output()
            .expect("git status");
        assert!(
            status.stdout.starts_with(b"R "),
            "fixture sanity: the rename must be staged as an R record, got {:?}",
            String::from_utf8_lossy(&status.stdout)
        );
        assert_eq!(
            first_change_at_risk(&repo, "main", &status.stdout).await,
            Some("vco_lib/renamed.py".to_string())
        );
    }

    /// Unknown ⇒ block. If the upstream tip cannot be resolved we cannot prove
    /// anything is safe, and the user's uncommitted work is unrecoverable if we
    /// guess wrong.
    #[tokio::test]
    async fn an_unresolvable_upstream_refuses_conservatively() {
        skip_if_no_git!();
        let (_tmp, repo) = init_repo();
        // No `vco_upstream` remote at all ⇒ compute_theirs_sha yields None.
        assert_eq!(
            first_change_at_risk(&repo, "main", &z(&[" M vco_lib/foo.py"])).await,
            Some("vco_lib/foo.py".to_string())
        );
    }

    /// A clean tree never refuses, whatever the upstream state.
    #[tokio::test]
    async fn nothing_dirty_never_refuses() {
        skip_if_no_git!();
        let (_tmp, repo) = init_upstream_pair();
        assert_eq!(first_change_at_risk(&repo, "main", b"").await, None);
        assert_eq!(
            first_change_at_risk(&repo, "main", &z(&["?? scratch.txt"])).await,
            None
        );
    }

    #[tokio::test]
    async fn ensure_upstream_remote_creates_when_absent() {
        skip_if_no_git!();
        // Hold the env mutex: these tests read `default_upstream_url()`
        // which inspects VCO_UPSTREAM_URL. Without serialization an
        // env-override test could mutate it mid-read.
        let _guard = ENV_MUTEX.lock().unwrap_or_else(|p| p.into_inner());

        let (_tmp, repo) = init_repo();
        assert!(get_remote_url_sync(&repo, VCO_UPSTREAM_REMOTE).is_none());

        ensure_upstream_remote(&repo).await.expect("ensure ok");

        let url = get_remote_url_sync(&repo, VCO_UPSTREAM_REMOTE).expect("remote exists");
        assert_eq!(url, default_upstream_url());
    }

    #[tokio::test]
    async fn ensure_upstream_remote_updates_when_url_mismatched() {
        skip_if_no_git!();
        let _guard = ENV_MUTEX.lock().unwrap_or_else(|p| p.into_inner());

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
        let _guard = ENV_MUTEX.lock().unwrap_or_else(|p| p.into_inner());

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
        let _guard = ENV_MUTEX.lock().unwrap_or_else(|p| p.into_inner());

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
        let _guard = ENV_MUTEX.lock().unwrap_or_else(|p| p.into_inner());

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
    // v0.2.32 UB1 (2026-05-23): fetch-with-retry-on-failure.
    // ---------------------------------------------------------------------
    //
    // These tests exercise the retry helper directly via an injected
    // closure that simulates success/failure counts. We don't shell out
    // to a real `git` binary here — the helper is intentionally
    // parametric so the retry policy is the unit under test, independent
    // of the git invocation.
    //
    // Under `cfg(test)` FETCH_RETRY_DELAYS_MS is in milliseconds (1, 5,
    // 30, 120), so all five attempts complete in <200ms of wall time.

    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    #[tokio::test]
    async fn fetch_upstream_with_retry_succeeds_first_attempt() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result = fetch_with_retry(Path::new("/tmp/fake"), &FETCH_RETRY_DELAYS_MS, move || {
            let calls_c = calls_c.clone();
            async move {
                calls_c.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }
        })
        .await;
        assert!(result.is_ok(), "should succeed first attempt");
        assert_eq!(
            calls.load(Ordering::SeqCst),
            1,
            "should only call fetch once when the first attempt succeeds"
        );
    }

    #[tokio::test]
    async fn fetch_upstream_with_retry_succeeds_on_third_attempt() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result = fetch_with_retry(Path::new("/tmp/fake"), &FETCH_RETRY_DELAYS_MS, move || {
            let calls_c = calls_c.clone();
            async move {
                let n = calls_c.fetch_add(1, Ordering::SeqCst) + 1;
                if n < 3 {
                    Err(format!("simulated failure {}", n))
                } else {
                    Ok(())
                }
            }
        })
        .await;
        assert!(result.is_ok(), "should succeed on third attempt");
        assert_eq!(
            calls.load(Ordering::SeqCst),
            3,
            "should call fetch exactly three times (2 failures + 1 success)"
        );
    }

    #[tokio::test]
    async fn fetch_upstream_with_retry_fails_after_all_attempts() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result = fetch_with_retry(Path::new("/tmp/fake"), &FETCH_RETRY_DELAYS_MS, move || {
            let calls_c = calls_c.clone();
            async move {
                let n = calls_c.fetch_add(1, Ordering::SeqCst) + 1;
                Err(format!("permanent failure {}", n))
            }
        })
        .await;
        assert!(result.is_err(), "should error after exhausting retries");
        // 5 attempts total: 1 immediate + 4 delayed retries
        // (matches the length of FETCH_RETRY_DELAYS_MS + 1).
        assert_eq!(
            calls.load(Ordering::SeqCst),
            5,
            "should call fetch 5 times (1 immediate + 4 retries)"
        );
        // The error should carry the LAST stderr-derived message so the UI
        // shows the most-recent failure, not the first one.
        let err = result.unwrap_err();
        assert!(
            err.contains("permanent failure 5"),
            "error should contain the last attempt's failure message, got: {}",
            err
        );
    }

    #[tokio::test]
    async fn fetch_upstream_with_retry_carries_empty_stderr_as_sentinel() {
        // Some git failure modes (network reset mid-transfer) drain stderr
        // before exit. The helper must still return a non-empty error
        // string in that case so the UI doesn't render a blank toast.
        let result = fetch_with_retry(Path::new("/tmp/fake"), &FETCH_RETRY_DELAYS_MS, move || async move {
            Err::<(), String>(String::new())
        })
        .await;
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(
            !err.is_empty(),
            "error should be non-empty even when every attempt returned empty stderr"
        );
    }

    // ---------------------------------------------------------------------
    // v0.2.83 A-F1 / D5: one serialized fetch home.
    // ---------------------------------------------------------------------

    /// D4: parse `git --version` and gate `--no-write-fetch-head` on >=2.29.
    #[test]
    fn git_version_parser_gates_no_write_fetch_head_flag() {
        // Below the 2.29 threshold → flag omitted.
        assert!(!git_version_supports_no_write_fetch_head("git version 2.28.0"));
        assert!(!git_version_supports_no_write_fetch_head("git version 2.17.1"));
        assert!(!git_version_supports_no_write_fetch_head("git version 1.9.5"));
        // At / above the threshold → flag included.
        assert!(git_version_supports_no_write_fetch_head("git version 2.29.0"));
        assert!(git_version_supports_no_write_fetch_head("git version 2.43.5"));
        assert!(git_version_supports_no_write_fetch_head("git version 3.0.0"));
        // Vendored suffixes (macOS/Homebrew/MinGW) still parse.
        assert!(git_version_supports_no_write_fetch_head(
            "git version 2.43.5 (Apple Git-154)"
        ));
        assert!(git_version_supports_no_write_fetch_head(
            "git version 2.44.0.windows.1"
        ));
        // Garbage / unexpected shapes → conservative false (omit the flag).
        assert!(!git_version_supports_no_write_fetch_head("garbage"));
        assert!(!git_version_supports_no_write_fetch_head(""));
        assert!(!git_version_supports_no_write_fetch_head("git version"));
        assert!(!git_version_supports_no_write_fetch_head("git version x.y.z"));
        assert!(!git_version_supports_no_write_fetch_head("2.29.0"));
    }

    /// D5 Quick policy: exactly one retry (2 attempts total) before giving up,
    /// and the error carries git's LAST stderr line (most-recent failure).
    #[tokio::test]
    async fn quick_policy_retries_once_then_reports_last_stderr() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result =
            fetch_with_retry(Path::new("/tmp/fake"), &QUICK_FETCH_DELAYS_MS, move || {
                let calls_c = calls_c.clone();
                async move {
                    let n = calls_c.fetch_add(1, Ordering::SeqCst) + 1;
                    Err(format!("fatal: could not read from remote (attempt {})", n))
                }
            })
            .await;
        assert!(result.is_err(), "Quick policy should fail after its retries");
        // Quick = 1 immediate attempt + 1 delayed retry = 2 total (matches
        // QUICK_FETCH_DELAYS_MS.len() + 1).
        assert_eq!(
            calls.load(Ordering::SeqCst),
            2,
            "Quick policy makes exactly 2 attempts (1 immediate + 1 retry)"
        );
        let err = result.unwrap_err();
        assert!(
            err.contains("attempt 2"),
            "error must carry the LAST attempt's stderr line, got: {}",
            err
        );
    }

    /// D5 Quick policy: a first-attempt failure that then succeeds on the
    /// single retry returns Ok (the transient index.lock / FETCH_HEAD case).
    #[tokio::test]
    async fn quick_policy_succeeds_on_the_one_retry() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result =
            fetch_with_retry(Path::new("/tmp/fake"), &QUICK_FETCH_DELAYS_MS, move || {
                let calls_c = calls_c.clone();
                async move {
                    let n = calls_c.fetch_add(1, Ordering::SeqCst) + 1;
                    if n < 2 {
                        Err("Unable to create '.git/FETCH_HEAD.lock': File exists.".into())
                    } else {
                        Ok(())
                    }
                }
            })
            .await;
        assert!(result.is_ok(), "should recover on the single retry");
        assert_eq!(calls.load(Ordering::SeqCst), 2);
    }

    /// REGRESSION PIN (A-RC3): `locked_fetch_with_retry` serializes concurrent
    /// callers behind `UPSTREAM_FETCH_LOCK` — two tasks fetching the same repo
    /// never run their attempts concurrently. Each injected attempt flips an
    /// AtomicBool "in-flight" and asserts it was NOT already set; a yield +
    /// tiny sleep inside the critical section widens the overlap window so an
    /// UNSERIALIZED implementation would reliably observe in_flight==true and
    /// fail. With the lock, the flag is never observed already-true.
    #[tokio::test]
    async fn concurrent_fetches_are_serialized_by_the_process_lock() {
        use std::sync::atomic::AtomicBool;

        let in_flight = Arc::new(AtomicBool::new(false));
        let overlap_detected = Arc::new(AtomicBool::new(false));

        let make_task = || {
            let in_flight = in_flight.clone();
            let overlap_detected = overlap_detected.clone();
            async move {
                // NOTE: pass an empty delays slice so a fetch failure would NOT
                // retry — but our injected attempt always succeeds, so the
                // critical section runs exactly once per task, cleanly.
                let in_flight_a = in_flight.clone();
                let overlap_a = overlap_detected.clone();
                locked_fetch_with_retry(Path::new("/tmp/fake"), &[], move || {
                    let in_flight_a = in_flight_a.clone();
                    let overlap_a = overlap_a.clone();
                    async move {
                        // If another task is already inside the critical
                        // section, the lock failed to serialize us.
                        if in_flight_a.swap(true, Ordering::SeqCst) {
                            overlap_a.store(true, Ordering::SeqCst);
                        }
                        // Widen the window: force a scheduler hand-off so an
                        // unserialized peer would interleave here.
                        tokio::task::yield_now().await;
                        tokio::time::sleep(Duration::from_millis(5)).await;
                        in_flight_a.store(false, Ordering::SeqCst);
                        Ok::<(), String>(())
                    }
                })
                .await
                .expect("attempt succeeds");
            }
        };

        // Run several concurrent tasks to make an unserialized failure reliable.
        let t1 = tokio::spawn(make_task());
        let t2 = tokio::spawn(make_task());
        let t3 = tokio::spawn(make_task());
        let t4 = tokio::spawn(make_task());
        let (_, _, _, _) = tokio::join!(t1, t2, t3, t4);

        assert!(
            !overlap_detected.load(Ordering::SeqCst),
            "UPSTREAM_FETCH_LOCK must serialize concurrent fetches — two attempts overlapped"
        );
    }

    /// M-2 (v0.2.83): a never-resolving fetch attempt must TIME OUT (retryable
    /// Err) rather than hang the ladder — and it must NOT poison
    /// `UPSTREAM_FETCH_LOCK`. A `std::future::pending()` attempt (mirrors a
    /// `git fetch` stuck on a dead network under the global lock) is capped by
    /// `FETCH_ATTEMPT_TIMEOUT` (ms-scaled under cfg(test)); the call returns the
    /// timeout error, and a SUBSEQUENT `locked_fetch_with_retry` proceeds —
    /// proving the lock was released when the timed-out attempt's future dropped.
    #[tokio::test]
    async fn never_resolving_attempt_times_out_and_releases_lock() {
        // Empty delays slice → no retry ladder: the single attempt hangs, so
        // the timeout is the ONLY thing that can end it.
        let hung = locked_fetch_with_retry(Path::new("/tmp/fake"), &[], || {
            // Never resolves — simulates a fetch subprocess stuck forever.
            std::future::pending::<Result<(), String>>()
        })
        .await;

        assert!(
            hung.is_err(),
            "a never-resolving attempt must surface a timeout error, not hang"
        );
        let msg = hung.unwrap_err();
        assert!(
            msg.contains("timed out"),
            "the surfaced error must name the timeout, got: {msg:?}"
        );

        // The lock must be free now: a follow-up fetch (bounded so if the lock
        // were still held, THIS would block and the test would hang) completes.
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let follow_up = tokio::time::timeout(
            Duration::from_secs(5),
            locked_fetch_with_retry(Path::new("/tmp/fake"), &[], move || {
                let calls_c = calls_c.clone();
                async move {
                    calls_c.fetch_add(1, Ordering::SeqCst);
                    Ok::<(), String>(())
                }
            }),
        )
        .await;

        assert!(
            follow_up.is_ok(),
            "the UPSTREAM_FETCH_LOCK must have been released after the timeout — \
             a subsequent fetch blocked (deadlock) instead of proceeding"
        );
        assert!(
            follow_up.unwrap().is_ok(),
            "the follow-up fetch attempt should succeed"
        );
        assert_eq!(
            calls.load(Ordering::SeqCst),
            1,
            "the follow-up attempt must have actually run (lock was free)"
        );
    }

    /// M-2 companion: a timeout is RETRYABLE — with a non-empty delays ladder a
    /// first-attempt hang is followed by a retry that succeeds. Proves the
    /// timeout error flows through the same retry path as a git failure.
    #[tokio::test]
    async fn timeout_is_retryable_and_a_later_attempt_can_succeed() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        // One-element delays slice → two attempts total. First hangs (times
        // out), second returns Ok.
        let result = fetch_with_retry(Path::new("/tmp/fake"), &[1], move || {
            let calls_c = calls_c.clone();
            async move {
                let n = calls_c.fetch_add(1, Ordering::SeqCst) + 1;
                if n < 2 {
                    // First attempt hangs → the per-attempt timeout fires.
                    std::future::pending::<Result<(), String>>().await
                } else {
                    Ok(())
                }
            }
        })
        .await;
        assert!(
            result.is_ok(),
            "a timed-out first attempt must retry; the second attempt succeeds"
        );
        assert_eq!(
            calls.load(Ordering::SeqCst),
            2,
            "exactly two attempts: the hung one (timed out) then the good one"
        );
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
        // Also the inverse direction (dev box with a future binary):
        // still flag it — drift in either direction is a UX surprise the
        // user deserves to see.
        assert!(running_version_lags_tag("0.2.35", "v0.2.34"));
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

    /// v0.2.71 Sweep-A#3: prove the relocated shared deferral writer is
    /// reachable + usable from THIS module (the `use` import resolves) and
    /// that the `NonFastForward` shape the two `apply_launcher_update`
    /// failure paths now build produces a durable, parseable
    /// `UPDATE_DEFERRED.md` trace. This is the contract the self-update
    /// surface relies on: PRE-v0.2.71 a failed launcher self-update returned
    /// ONLY a transient modal error; now both failure paths leave the SAME
    /// durable record the installer surface does, so a terminal Claude can
    /// find the stuck state at session start.
    ///
    /// We exercise the writer directly (the full `apply_launcher_update`
    /// command needs an `AppHandle` + live git network, so a whole-command
    /// integration test is impractical).
    ///
    /// CORRECTED v0.2.95 phase 2: this used to add "we use the EXACT kind +
    /// detail string the autostash-pop-conflict success-path branch passes, so
    /// this guards that specific call-site's shape". That call site is gone —
    /// `apply_launcher_update` pulls through `update_pipeline`, which
    /// classifies an autostash-pop conflict as its OWN condition
    /// (`write_autostash_pop_conflict_deferral`) rather than folding it into
    /// the diverged one, and writes the diverged record for the conflict and
    /// non-FF classes. So what this pins is the WRITER's durable output shape,
    /// which is what both surfaces now reach. Naming a call site it no longer
    /// guards would be the false claim, not the coverage.
    #[test]
    fn self_update_failure_writes_durable_launcher_update_diverged_deferral() {
        use crate::commands::git_user_editable_merge::{
            write_launcher_update_diverged_deferral, LauncherUpdateDivergedKind,
        };
        let dir = tempfile::tempdir().expect("tempdir");
        let install = dir.path().to_path_buf();

        let detail = "git pull (auto-merge) left unmerged files (autostash-pop conflict)";
        write_launcher_update_diverged_deferral(
            &install,
            "main",
            LauncherUpdateDivergedKind::NonFastForward {
                local_sha: Some("dead001".into()),
                remote_sha: Some("beef002".into()),
                detail: detail.to_string(),
            },
        );

        let target = install.join(".claude/context/UPDATE_DEFERRED.md");
        let body = std::fs::read_to_string(&target)
            .expect("self-update failure must leave a durable UPDATE_DEFERRED.md");

        // Same single condition_id as the installer surface → self-clears on
        // the next successful install.py run.
        assert!(
            body.contains("condition_ids: [launcher_update_diverged]"),
            "frontmatter must carry the shared condition_id"
        );
        assert!(body.contains("## launcher_update_diverged (warning)"));
        // SHAs + the autostash-pop detail must be embedded for diagnosis.
        assert!(body.contains("dead001"), "local sha must appear");
        assert!(body.contains("beef002"), "remote sha must appear");
        assert!(
            body.contains("autostash-pop conflict"),
            "the self-update failure detail must be embedded for diagnosis"
        );
        // The recovery instructions a terminal Claude needs.
        assert!(body.contains("**For your Claude assistant**"));
        assert!(body.contains("python install.py --update"));
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

        /// The tri-state itself, reproducing the FIELD SHAPE precisely:
        /// `ls-remote` SUCCEEDS (so a real, current remote SHA is obtained
        /// and persisted) while `rev-list` FAILS (so the distance is
        /// unknowable). That exact combination is what the reported
        /// `launcher-update-state.json` contained — a correct
        /// `last_known_remote_sha` beside `last_known_commit_count: 0` — and
        /// it is why the incident was first misread as a network problem.
        ///
        /// Achieved by deleting the local tracking refs while leaving the
        /// remote reachable: `ls-remote` goes to the remote, `rev-list` reads
        /// local refs.
        #[tokio::test]
        async fn check_reports_unknown_not_up_to_date_when_rev_list_fails() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();
            let _ = std::fs::remove_dir_all(local.join(".git/refs/remotes/vco_upstream"));
            let _ = std::fs::remove_file(local.join(".git/packed-refs"));

            // Precondition: exactly one of the two questions is answerable.
            assert!(
                ls_remote_sha(&local, "main").await.is_ok(),
                "fixture precondition: the remote must still answer"
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

        /// A broken remote (nothing resolves at all) is ALSO Unknown, not a
        /// quiet "up to date". Distinct from the test above: there the remote
        /// answered, here it does not.
        #[tokio::test]
        async fn check_reports_unknown_when_the_remote_is_unreachable() {
            skip_if_no_git!();
            let (tmp, local, _remote) = detached_upstream_fixture();
            let nowhere = tmp.path().join("no-such-remote.git");
            git(
                &local,
                &["remote", "set-url", "vco_upstream", nowhere.to_str().unwrap()],
            );

            let status = evaluate_against_fetched_refs(&local, None).await.0;
            assert!(
                status.remote_check.is_unknown(),
                "got {:?}",
                status.remote_check
            );
            assert!(!status.available);
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

        /// `apply_launcher_update`'s rebuild gating: when the pre-pull diff
        /// cannot be computed, BOTH builds must run.
        ///
        /// Asserted at the decision boundary rather than by driving the
        /// whole command (which pulls, rebuilds and restarts the process).
        /// The production code path is three lines below this logic and
        /// shares the same `Err ⇒ (true, true)` shape.
        #[tokio::test]
        async fn apply_rebuilds_everything_when_diff_unknown() {
            skip_if_no_git!();
            let (_tmp, local, _remote) = detached_upstream_fixture();

            // The exact call the pre-pull gating makes, against the ref that
            // does not exist in a `remote add` clone — i.e. what the shipped
            // code passed while detached. The absence is a PINNED property of
            // the fixture (`git_cmd::pin_absent_remote_head`); a git >= 2.48
            // creates that ref on fetch, and without the pin this `diff`
            // succeeds, `broken.is_err()` fails, and the red is the test's
            // fault rather than the code's.
            let broken = git_cmd::run_git(
                &local,
                &["diff", "--name-only", "HEAD..vco_upstream/HEAD"],
            ).await;
            assert!(
                broken.is_err(),
                "precondition: the missing-ref diff must ERROR, not return empty"
            );

            let (needs_cargo, needs_npm) = match broken {
                Ok(d) => (changed_paths_need_cargo(&d), changed_paths_need_npm(&d)),
                Err(_) => (true, true),
            };
            assert!(
                needs_cargo && needs_npm,
                "an undetermined diff must rebuild EVERYTHING — the pre-fix \
                 `.unwrap_or_default()` produced an empty string here, and an empty diff \
                 means 'nothing changed', so the launcher pulled new source and skipped \
                 both builds"
            );

            // Leave-alone half: a diff that really is empty still skips.
            let empty = git_cmd::run_git(&local, &["diff", "--name-only", "HEAD..HEAD"]).await
                .expect("HEAD..HEAD resolves");
            assert!(!changed_paths_need_cargo(&empty));
            assert!(!changed_paths_need_npm(&empty));

            // …and a real diff against the RESOLVED branch gates correctly.
            let real = git_cmd::run_git(
                &local,
                &["diff", "--name-only", "HEAD..vco_upstream/main"],
            ).await
            .expect("resolved ref works even while detached");
            assert!(
                changed_paths_need_cargo(&real),
                "the fixture's upstream touches launcher/src-tauri/**; got: {real:?}"
            );
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

    // ===================================================================
    // v0.2.95 phase 3 — the RESYNC surface's hub choreography
    // ===================================================================
    //
    // `force_resync_launcher`'s `git reset --hard` writes every tracked file
    // that differs from the target, and `launcher/dist/<arch>/vct-hub{,.exe}`
    // IS tracked — so this surface carried the hazard v0.2.21 Step 12 closed
    // for `update_orchestrator` and phase 2 closed for `apply_launcher_update`.
    //
    // WHY THESE TESTS ARE SAFE ON A DEVELOPER'S MACHINE, which is the reason
    // no earlier test ever drove this code:
    //   * `VCT_STATE_DIR` is redirected, so the hub stop reads a SCRATCH
    //     `hub.pid` rather than `~/.vct/hub.pid`;
    //   * the pid it names is provably DEAD (spawn + reap), so nothing is
    //     signalled and `vct-hub --stop` is never spawned;
    //   * `update_gate::pre_update_hub_kill_sweep` refuses under a test
    //     harness (v0.2.92), so the process-identity backstop reaps nothing;
    //   * `installer::ensure_hub_started_after_update` refuses under a test
    //     harness too (v0.2.95 phase 3 — the spawning half of the same
    //     defect), so the failure arm below cannot start a REAL hub bound to
    //     the scratch state dir.
    //
    // THE OBSERVABLE both tests key on is that the stop's own side effect — a
    // stale `hub.pid` is REMOVED — is visible after the call. That is what
    // makes "the hub was stopped" a fact rather than an adjacency in the
    // source, and the second test makes it an ORDERING fact: the removal is
    // there even when the tree-write FAILS, so the stop provably preceded it.
    mod resync_hub_choreography {
        use super::*;
        use crate::commands::git_user_editable_merge::tests::{
            init_repo_pair, push_upstream_change, run_git as fixture_git,
        };
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

        /// A pid that is provably dead: spawn a trivial child, reap it, give
        /// the kernel a moment. Same pattern as
        /// `installer::tests::hub_stop_tests::ensure_hub_stopped_cleans_up_stale_dead_pid`.
        fn provably_dead_pid() -> u32 {
            #[cfg(unix)]
            let mut child = StdCommand::new("true")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .expect("spawn true");
            #[cfg(windows)]
            let mut child = StdCommand::new("cmd")
                .args(["/c", "exit"])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .expect("spawn cmd /c exit");
            let pid = child.id();
            let _ = child.wait();
            std::thread::sleep(std::time::Duration::from_millis(50));
            pid
        }

        /// A clone that has DIVERGED from upstream: one local-only commit,
        /// one upstream-only commit. A resync must discard the first and
        /// land the second — which is how the tests below tell a reset that
        /// ran from one that did not.
        fn diverged_clone() -> (tempfile::TempDir, PathBuf, String) {
            let (tmp, _remote, local) = init_repo_pair();
            let seed = tmp.path().join("seed");
            push_upstream_change(&seed, &local, "upstream_only.txt", "from upstream\n");

            std::fs::write(local.join("local_only.txt"), "my divergent work\n").unwrap();
            fixture_git(&local, &["add", "-A"]);
            fixture_git(&local, &["commit", "-m", "local divergence"]);

            let head = StdCommand::new("git")
                .args(["rev-parse", "HEAD"])
                .current_dir(&local)
                .output()
                .expect("git rev-parse");
            let head_before = String::from_utf8_lossy(&head.stdout).trim().to_string();
            (tmp, local, head_before)
        }

        fn head_of(repo: &Path) -> String {
            let out = StdCommand::new("git")
                .args(["rev-parse", "HEAD"])
                .current_dir(repo)
                .output()
                .expect("git rev-parse");
            String::from_utf8_lossy(&out.stdout).trim().to_string()
        }

        /// ACT ARM. The resync lands, and the hub was stopped on the way.
        ///
        /// MUTATION RED-PROOF: delete the
        /// `stop_hub_and_rename_binaries_aside` call from
        /// `stop_hub_then_hard_reset` and the `hub.pid` assertion below fails
        /// — the reset still succeeds, which is exactly why "the reset
        /// worked" was never evidence that the hub was handled.
        #[tokio::test]
        async fn resync_stops_the_hub_before_it_hard_resets_the_tree() {
            skip_if_no_git!();
            let (_tmp, local, head_before) = diverged_clone();

            let env = vct_launcher_core::test_env::state_dir_guard();
            let pid_file = env.path().join("hub.pid");
            std::fs::write(&pid_file, format!("{}\n", provably_dead_pid())).unwrap();
            assert!(pid_file.exists(), "fixture: the stale hub.pid must exist");

            let renames = stop_hub_then_hard_reset(&local, "vco_upstream/main")
                .await
                .expect("the resync reset must land on a diverged clone");

            assert!(
                !pid_file.exists(),
                "the hub must be STOPPED before `git reset --hard` writes the \
                 tree: `launcher/dist/<arch>/vct-hub` is a TRACKED file, so \
                 the reset overwrites it under a running hub (Windows: the \
                 whole reset aborts; POSIX: the hub serves old code from a \
                 deleted inode for the rest of the session). The stale hub.pid \
                 is still here, so the stop never ran."
            );
            assert_ne!(head_of(&local), head_before, "the reset must have moved HEAD");
            assert!(
                local.join("upstream_only.txt").is_file(),
                "the tree must now carry upstream's content"
            );
            assert!(
                !local.join("local_only.txt").exists(),
                "a hard reset discards the local divergence — that is the point \
                 of this surface"
            );
            // POSIX: nothing is renamed (git replaces the inode safely).
            #[cfg(not(windows))]
            assert_eq!(
                renames,
                crate::commands::update_pipeline::PrePullRenames::default(),
                "the pre-pull renames are Windows-only"
            );
            #[cfg(windows)]
            let _ = renames;
        }

        /// FAILURE ARM — and the ORDERING claim.
        ///
        /// The reset cannot run (its target ref does not exist). Two things
        /// must hold: the tree is UNTOUCHED, and the hub was stopped anyway —
        /// because the stop happens BEFORE the write is attempted. Move the
        /// stop after the reset and this test goes red while the act-arm test
        /// above stays green.
        ///
        /// It also drives the recovery leg: `stop_hub_then_hard_reset` must
        /// route a failed reset through `abort_update_restore_binaries_and_hub`
        /// so the hub it stopped comes back. Without that, a failed resync
        /// leaves a perma-stopped hub — the failure `07101d30` added the
        /// revert-on-every-early-return for on the installer surface.
        #[tokio::test]
        async fn a_resync_whose_reset_fails_leaves_the_tree_alone_but_still_stopped_the_hub_first() {
            skip_if_no_git!();
            let (_tmp, local, head_before) = diverged_clone();

            let env = vct_launcher_core::test_env::state_dir_guard();
            let pid_file = env.path().join("hub.pid");
            std::fs::write(&pid_file, format!("{}\n", provably_dead_pid())).unwrap();

            let err = stop_hub_then_hard_reset(&local, "vco_upstream/no-such-branch")
                .await
                .expect_err("a reset to a ref that does not exist must fail");
            assert!(!err.is_empty(), "the git error must reach the caller: {err}");

            assert!(
                !pid_file.exists(),
                "ORDER: the hub stop must precede the tree write, so its stale \
                 hub.pid is gone even when the write never happened. Finding it \
                 here means the stop was moved after the reset, where it \
                 protects nothing."
            );
            assert_eq!(
                head_of(&local),
                head_before,
                "a failed reset must leave the tree exactly as it was"
            );
            assert!(
                local.join("local_only.txt").is_file(),
                "the local divergence must survive a reset that never ran"
            );
        }
    }
}
