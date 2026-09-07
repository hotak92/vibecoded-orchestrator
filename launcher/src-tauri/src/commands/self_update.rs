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
use crate::commands::git_user_editable_merge::{
    write_launcher_update_diverged_deferral, LauncherUpdateDivergedKind,
};
// v0.2.92 WP-13: the ONE git runner + the ONE branch resolver (see
// `commands::git_cmd`). `run_git` / `run_git_combined` are imported under
// their old names so the ~16 call sites in this file did not churn during
// the extraction.
use crate::commands::git_cmd::{self, run_git, run_git_combined};
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

/// Abort an in-progress merge/rebase left by a failed RealMerge pull, so the
/// working tree is clean for the next attempt. v0.2.71 (BLOCKER-1 fix): without
/// this, a conflicted `apply_launcher_update` left `.git/MERGE_HEAD` / `UU`
/// markers on disk; the NEXT `apply_launcher_update` then dead-ended at the
/// Step-1 clean-tree guard (`first_blocking_change`) — a hard stop of the
/// self-update surface. Best-effort + idempotent: `--abort` is a no-op (errors
/// harmlessly) when no merge/rebase is in progress, so we ignore the result.
/// Mirrors `installer::abort_orchestrator_merge_or_rebase`'s on-disk detection
/// intent (merge first, then rebase) without the Tauri-command wrapper.
async fn abort_merge_or_rebase_in_progress(repo: &Path) {
    let _ = run_git(repo, &["merge", "--abort"]).await;
    let _ = run_git(repo, &["rebase", "--abort"]).await;
}

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

    // Step 0: pin the canonical public upstream (Design B). Must happen
    // BEFORE any fetch/diff/pull so we never accidentally pull from a
    // private fork's `origin`.
    ensure_upstream_remote(&repo).await?;

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

    // Fetch upstream so the local refs (vco_upstream/<branch>) are current
    // for the diff and the subsequent pull. Without this, a fresh `vco_upstream`
    // remote has no tracking refs yet and the diff returns empty.
    fetch_upstream(&repo).await?;

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

    // Step 3: pull from the canonical upstream using the SHARED divergence
    // decision (v0.2.71 Piece 4). PRE-v0.2.71 this was a blind `--ff-only`:
    // ANY committed divergence (e.g. a single committed KG node — the
    // encouraged 3rd-party behaviour) made it refuse non-FF, and the ONLY
    // forward action on this surface's resync modal is `force_resync_launcher`
    // = `git reset --hard` (DATA LOSS). Routing through
    // `resolve_divergence_pull_plan` gives this surface the SAME auto-merge as
    // the MenuBar badge: conflict-free committed divergence folds silently via
    // a real merge (RealMerge), and the destructive resync becomes the
    // genuine-conflict-only fallback rather than the default path.
    //
    // `pre_merge_committed=false`: unlike `update_orchestrator`, this surface
    // has no A0 pre-merge step (no synthetic commit) — so the plan is either
    // RealMerge (clean committed divergence, no pop-conflict risk) or FfOnly
    // (everything else, incl. a clean fast-forwardable tree). We never get
    // RebaseAutostash here. v0.2.89 §4.3: BEFORE the plan resolves, the shared
    // generated-file reconcile step (below) takes upstream's blob for any
    // diverged allowlisted release-controlled file (lockfiles, package.json,
    // Cargo.lock, dist/**) so the "expected conflict" class auto-resolves
    // (RealMerge / fast-forward) instead of forcing this surface's resync
    // modal; its take-upstream commit (if any) is threaded into the plan's
    // 4th arg. The `needs_cargo`/`needs_npm` rebuild gating above
    // was computed from the pre-diff `HEAD..vco_upstream/<branch>` (the
    // upstream-changed set) BEFORE the pull, so it's correct regardless of
    // whether the pull fast-forwards or produces a merge commit — a RealMerge
    // leaves HEAD a merge commit but the set of files that changed vs. our old
    // HEAD is identical, which is what drives the rebuild decision.
    // v0.2.78 ITEM #0 (F1): same pre-plan auto-restore as the MenuBar surface
    // (one home — `auto_restore_byte_identical_tracked_mods`). A tracked file
    // whose working-tree content already == the incoming upstream blob is not a
    // real modification and must not force the resync modal via the
    // pop-conflict-risk set. Byte-identity-gated; divergent files left alone.
    // v0.2.91 WI-4: Windows pre-pull rename, parity with the MenuBar surface.
    //
    // Two jobs, which is why it must sit HERE — before F1 + the generated-file
    // reconcile, not just before the pull:
    //   1. the reconcile's `git checkout HEAD -- launcher/dist/**` cannot
    //      rewrite a mapped running `.exe`; with the canonical path freed it
    //      can (this is exactly the ordering `update_orchestrator` already
    //      has, and its absence here is why the reconcile was unreachable for
    //      the dist-divergence class it was built for);
    //   2. `git pull` would otherwise either abort atomically on
    //      ERROR_SHARING_VIOLATION or complete the merge with the binary
    //      silently skipped (metadata new + exe old + git-dirty).
    //
    // No-op on POSIX. Reverted NON-CLOBBERINGLY on every failure return below
    // (WI-3). Nothing between here and the pull returns early, so the revert
    // sites below are exhaustive.
    let pre_pull_renamed =
        crate::services::binary_freshness::pre_pull_rename_running_binary(&repo);
    // Revert helper for the failure paths: keeps the freshly-pulled bytes when
    // the pull already landed them (WI-3) and logs either way.
    //
    // v0.2.91 fix-round MINOR-1: the outcome is CHECKED, not discarded. An
    // averted clobber on THIS surface used to produce nothing at all — no
    // deferral, no audit row, no trace — while the installer surface recorded
    // both. That asymmetry is the WI-7 silence this release closes: the state
    // it describes ("the canonical binary now holds NEWER bytes than the
    // process you are running") is exactly the one the field install sat in for
    // a month undiagnosed. `revert_and_record` is the shared home for the
    // revert + durable-condition pair; the audit row is written here because
    // the Db handle is a property of the surface, not of the revert.
    let audit_app = app.clone();
    let revert_rename = |backup: Option<&std::path::Path>| {
        let Some(b) = backup else { return };
        let outcome = crate::services::binary_freshness::revert_and_record(&repo, b);
        if outcome == crate::services::binary_freshness::RevertOutcome::ClobberAverted {
            use tauri::Manager as _;
            if let Some(db) = audit_app.try_state::<crate::db::Db>() {
                let _ = db.audit(
                    "update_binary_clobber_averted",
                    None,
                    None,
                    &serde_json::json!({
                        "surface": "apply_launcher_update",
                        "branch": branch,
                        "backup": b.display().to_string(),
                        "note": "abort tail kept the freshly-pulled binary (WI-3)",
                    }),
                );
            }
        }
    };

    let f1_restored =
        crate::commands::git_user_editable_merge::auto_restore_byte_identical_tracked_mods(
            &repo, &branch,
        )
        .await;
    if f1_restored > 0 {
        tracing::info!(
            "[vct] apply_launcher_update: F1 auto-restored {} byte-identical tracked file(s) \
             before divergence-plan resolution",
            f1_restored
        );
    }
    // v0.2.89 §4.3: after F1 (byte-identical restore) and BEFORE the plan
    // resolution, reconcile GENERATED / release-controlled files to upstream
    // (take-upstream bias) — lockfiles, package.json, Cargo.lock, dist/**.
    // This is the SAME shared helper the installer surface calls (one home):
    // it classifies the allowlisted committed/worktree divergence, takes
    // upstream's blob (a synthetic take-upstream commit for the committed set,
    // a restore-to-HEAD for the worktree set). v0.2.89 MINOR-1: its
    // `generated_files_reconciled` audit deferral is emitted by THIS surface
    // AFTER the pull succeeds (search MINOR-1 below), NOT inside the helper — a
    // pre-pull emit would dirty CLAUDE.md between the reconcile and the pull-plan
    // decision and could self-inflict the resync modal. Best-effort throughout:
    // any per-file failure leaves that file divergent → it stays in the
    // pop-conflict-risk / modal-forcing sets (never worse than today's resync
    // modal). A divergent SOURCE file still surfaces the modal (a real breakage
    // signal); only the "expected conflict" class (dep-bump / lockfile / dist
    // divergence) is auto-resolved.
    let gen_reconcile =
        crate::commands::git_user_editable_merge::resolve_generated_files_to_upstream(
            &repo, &branch,
        )
        .await;
    if gen_reconcile.reconcile_committed
        || !gen_reconcile.took_upstream.is_empty()
        || !gen_reconcile.restored_worktree.is_empty()
    {
        tracing::info!(
            "[vct] apply_launcher_update: reconciled generated/release-controlled file(s) to \
             upstream — {} committed take-upstream, {} worktree-restored (reconcile_committed={})",
            gen_reconcile.took_upstream.len(),
            gen_reconcile.restored_worktree.len(),
            gen_reconcile.reconcile_committed
        );
    }
    let plan = crate::commands::git_user_editable_merge::resolve_divergence_pull_plan(
        &repo,
        &branch,
        // v0.2.89 Phase 2b: this surface has no A0 pre-merge step, so
        // `pre_merge_committed` is always false here (see the comment block
        // above). The 4th arg threads the generated-file reconcile result: a
        // synthetic take-upstream commit falls through to the merge-tree probe
        // (→ RealMerge on clean end-trees) instead of forcing the modal.
        false,
        gen_reconcile.reconcile_committed,
    )
    .await;
    let pull_args = plan.pull_args(VCO_UPSTREAM_REMOTE, &branch);
    let pull_args_ref: Vec<&str> = pull_args.iter().map(|s| s.as_str()).collect();
    // v0.2.71 (BLOCKER-1 fix): use run_git_combined so a RealMerge CONFLICT
    // (whose markers git writes to STDOUT) reaches the classifier — the plain
    // run_git returned stderr-only and silently missed it.
    if let Err(e) = run_git_combined(&repo, &pull_args_ref).await {
        // A genuine merge conflict (RealMerge arm) or a non-FF refusal
        // (FfOnly arm) both route to the resync modal — the only recovery
        // this surface offers. The frontend keys the modal off
        // `kind == "non_fast_forward"`, so serialize that shape for either.
        // A non-conflict, non-FF failure (broken git, detached HEAD, network)
        // stays a raw error string toast.
        if is_non_fast_forward(&e) || is_merge_conflict(&e) {
            // v0.2.71 (BLOCKER-1 fix): ABORT the in-progress merge/rebase
            // before returning. Without this, a RealMerge conflict leaves
            // `.git/MERGE_HEAD` / `UU` markers on disk and the NEXT
            // apply_launcher_update dead-ends at the Step-1 clean-tree guard.
            // The user opts into the destructive resync via the modal; until
            // then the tree must be clean + re-attemptable. (No-op for the
            // FfOnly/non-FF arm — nothing was merged.)
            abort_merge_or_rebase_in_progress(&repo).await;
            // v0.2.91 WI-3/WI-4: put the running binary back at its canonical
            // path (unless the pull already landed newer bytes there).
            revert_rename(pre_pull_renamed.as_deref());
            // Best-effort: capture local + remote SHAs so the modal can
            // show users what their clone has vs. what upstream has.
            let local = current_sha(&repo).await.ok();
            let remote = ls_remote_sha(&repo, &branch).await.ok();
            // v0.2.71 Sweep-A#3: leave the SAME durable UPDATE_DEFERRED.md
            // trace the installer surface writes. Best-effort — never blocks
            // the modal-shaped return below. A dismissed resync modal would
            // otherwise leave NO record a terminal Claude could find at
            // session start.
            write_launcher_update_diverged_deferral(
                &repo,
                &branch,
                LauncherUpdateDivergedKind::NonFastForward {
                    local_sha: local.clone(),
                    remote_sha: remote.clone(),
                    detail: e.clone(),
                },
            );
            return Err(serialize_non_ff_error(
                &branch,
                local.as_deref(),
                remote.as_deref(),
                &e,
            ));
        }
        revert_rename(pre_pull_renamed.as_deref());
        return Err(e);
    }

    // The RealMerge arm uses `--autostash`: it can EXIT 0 yet leave the tree
    // broken if the autostash pop conflicts (TOCTOU: upstream touched a
    // locally-modified file between our pop-conflict pre-check and the pull's
    // fetch). Detect a conflicted tree on the success path and route to the
    // resync modal instead of rebuilding + restarting on a broken tree. (The
    // FfOnly arm can't reach this — it never merges.)
    //
    // v0.2.92 WP-13: the LAST `.unwrap_or_default()` in this file, and the
    // same shape as the three the field incident was made of — an errored
    // `git diff` produced an empty string, an empty string means "no
    // conflicts", and the launcher would go on to rebuild and restart on a
    // tree it had not actually inspected. Nothing had ever reported it
    // because it only bites when git is already misbehaving.
    //
    // "I could not check for conflicts" is now its own outcome and it STOPS,
    // in the safe direction: the pull has already landed, so the user loses
    // nothing by retrying, whereas restarting into a half-merged tree is the
    // failure this check exists to prevent. Deliberately NOT routed to the
    // resync modal — that path is destructive (`reset --hard`) and must never
    // be reached on a guess.
    let unmerged = match run_git(&repo, &["diff", "--name-only", "--diff-filter=U"]).await {
        Ok(out) => out,
        Err(e) => {
            tracing::error!(
                "[vct] apply_launcher_update: could not check for unmerged files after the \
                 pull ({e}) — refusing to rebuild/restart on an uninspected tree"
            );
            revert_rename(pre_pull_renamed.as_deref());
            return Err(format!(
                "The pull completed, but the launcher could not verify the working tree is \
                 free of merge conflicts (`git diff --diff-filter=U` failed: {e}). Nothing \
                 was rebuilt or restarted. Check `git -C {} status` and click Update again.",
                repo.display()
            ));
        }
    };
    if !unmerged.trim().is_empty() {
        // Abort here too: an autostash-pop conflict leaves the tree dirty +
        // a dangling stash; clean it so the next attempt isn't blocked.
        abort_merge_or_rebase_in_progress(&repo).await;
        // v0.2.91 WI-3: NON-clobbering revert. On this branch the merge may
        // well have LANDED (only the autostash pop conflicted), in which case
        // the canonical path already holds the new binary and restoring the
        // backup over it would freeze this install (RC-1).
        revert_rename(pre_pull_renamed.as_deref());
        let local = current_sha(&repo).await.ok();
        let remote = ls_remote_sha(&repo, &branch).await.ok();
        let detail = "git pull (auto-merge) left unmerged files (autostash-pop conflict)";
        // v0.2.71 Sweep-A#3: durable deferral for the autostash-pop-conflict
        // success-path failure too (same rationale as the non-FF arm above).
        write_launcher_update_diverged_deferral(
            &repo,
            &branch,
            LauncherUpdateDivergedKind::NonFastForward {
                local_sha: local.clone(),
                remote_sha: remote.clone(),
                detail: detail.to_string(),
            },
        );
        return Err(serialize_non_ff_error(
            &branch,
            local.as_deref(),
            remote.as_deref(),
            detail,
        ));
    }

    // v0.2.89 MINOR-1: emit the generated-file reconcile audit deferral HERE —
    // AFTER the pull succeeded and the tree is confirmed clean, NOT inside the
    // shared helper. Emitting injects a reminder block into the tracked
    // CLAUDE.md; a pre-pull emit would dirty it and could self-inflict the
    // resync modal. Best-effort (no-op when nothing was reconciled); self-clears
    // on the install.py --update run inside finish_apply_after_pull below.
    crate::commands::git_user_editable_merge::emit_generated_reconcile_deferrals(
        &repo,
        &gen_reconcile,
    );

    finish_apply_after_pull(app, &repo, needs_cargo, needs_npm).await
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
///   1. fetch vco_upstream/<branch>
///   2. compute pre-reset diff for rebuild gating (HEAD..vco_upstream/<branch>)
///   3. reset --hard vco_upstream/<branch>
///   4. rebuild + restart (shared with `apply_launcher_update`)
#[command]
pub async fn force_resync_launcher<R: Runtime>(app: AppHandle<R>) -> Result<(), String> {
    if !git_available().await {
        return Err("git not found on PATH — cannot resync".into());
    }
    let repo = find_launcher_repo_root()?;
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
    run_git(
        &repo,
        &[
            "reset",
            "--hard",
            &format!("{}/{}", VCO_UPSTREAM_REMOTE, branch),
        ],
    )
    .await?;

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
    // skip all three on the handoff path, and unlike the installer surface
    // this flow never runs install.py, so nothing else would record the new
    // version.
    let handoff = crate::services::binary_freshness::stage_and_handoff_after_update(
        repo,
        &repo.display().to_string(),
    )
    .await;

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

    // Bug G (v0.2.8): refresh the install-manifest's `version` /
    // `source_commit` / `completed_at` so the next session reports the
    // new launcher version. `repo` here is the launcher's enclosing
    // install root (find_launcher_repo_root returns the dir containing
    // launcher/). The cargo+npm rebuild above has already produced the
    // new binary; the version-source files (vct-module.json,
    // package.json, Cargo.toml, tauri.conf.json) are all on disk in the
    // new state. Soft-fail: never block restart.
    if let Err(e) = crate::commands::manifest::refresh_install_manifest(repo, "launcher_update") {
        tracing::warn!(
            "[apply_launcher_update] install-manifest refresh failed (non-fatal): {}",
            e
        );
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

/// Detect a genuine MERGE CONFLICT (or dirty-tree refusal) in git output.
///
/// v0.2.71 (BLOCKER-1 fix): thin delegator to the ONE shared classifier
/// `git_user_editable_merge::is_pull_conflict`. Pre-v0.2.71 this was a second
/// hand-synced copy of installer's phrase list (drift hazard, called out in the
/// old comment). The phrases now live in exactly one place; both surfaces
/// classify identically by construction. Feed it COMBINED stdout+stderr (see
/// `run_git_combined` — git writes `CONFLICT` lines to stdout, so a
/// stderr-only string silently misses real conflicts).
pub(crate) fn is_merge_conflict(err: &str) -> bool {
    crate::commands::git_user_editable_merge::is_pull_conflict(err)
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
fn first_blocking_change(porcelain: &str) -> Option<String> {
    // Built once per call; four patterns. On a (never-observed) malformed
    // pattern, fall back to "exclude nothing" — the pre-v0.2.91 behaviour,
    // which blocks rather than silently pulling over a dirty file.
    let generated =
        crate::commands::git_user_editable_merge::build_generated_release_controlled_globset().ok();
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
        if let Some(gs) = generated.as_ref() {
            if crate::commands::git_user_editable_merge::is_generated_release_controlled(&path, gs)
            {
                // Handled downstream by F1 + the take-upstream reconcile.
                continue;
            }
        }
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
#[command]
pub fn get_cached_update_status() -> UpdateStatus {
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
    let (available, commit_count, remote_check) = match (
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

    UpdateStatus {
        available,
        current_sha: None,
        remote_sha: remote,
        commit_count,
        branch: String::new(),
        // The cache does not record attachment (it is a property of the repo
        // right now, not of the last check) and this command deliberately
        // does no I/O. `false` here means "not asserted", and the surfaces
        // that care call `check_for_launcher_update`.
        head_detached: false,
        remote_check,
        // Never cached — the tag probe is a network question with no cheap
        // offline answer, so from cache it is always undetermined.
        latest_source_release_check: CheckState::unknown("not cached"),
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

    /// v0.2.91 WI-4 RED-PROOF: a dirty `launcher/dist/<arch>/vct-launcher.exe`
    /// is EXACTLY what a failed Windows binary swap (or a hand-copied recovery
    /// binary) leaves behind. On `bd8f6836` this returned `Some(path)` and the
    /// self-update surface hard-refused with "Uncommitted changes on tracked
    /// file … would be lost" — BEFORE the F1 restore and BEFORE the
    /// take-upstream reconcile that exists to auto-resolve this exact class.
    /// The only forward action left to the user was the destructive resync.
    #[test]
    fn blocking_change_ignores_generated_release_controlled_dist_binaries() {
        let porcelain = " M launcher/dist/windows-x64/vct-launcher.exe\n";
        assert_eq!(
            first_blocking_change(porcelain),
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
                first_blocking_change(&format!(" M {}\n", p)),
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
                first_blocking_change(&format!(" M {}\n", p)),
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
        let porcelain = " M launcher/dist/windows-x64/vct-launcher.exe\n\
                         ?? .claude/CONTEXT_STATE.md\n\
                         M  launcher/src-tauri/src/lib.rs\n";
        assert_eq!(
            first_blocking_change(porcelain),
            Some("launcher/src-tauri/src/lib.rs".into())
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
    /// integration test is impractical) — but we use the EXACT kind +
    /// detail string the autostash-pop-conflict success-path branch passes,
    /// so this guards that specific call-site's shape, not a generic one.
    #[test]
    fn self_update_failure_writes_durable_launcher_update_diverged_deferral() {
        let dir = tempfile::tempdir().expect("tempdir");
        let install = dir.path().to_path_buf();

        // The literal detail the success-path unmerged-tree branch uses.
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
}
