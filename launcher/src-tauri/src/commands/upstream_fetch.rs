// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
//! The launcher's ONE upstream `git fetch` home: serialized per repository,
//! coalescing, bounded, classified (transient vs deterministic), and logged
//! under the real caller's name.
//!
//! v0.2.100 F-W4-02: moved VERBATIM out of `self_update.rs` (which had grown
//! to 3 740 lines, most of it this ladder and its tests) — the design notes
//! below (v0.2.83 A-F1/D5, v0.2.100 WP-05, W4R-03 stale ref-lock, R18-10
//! prune message) travelled with the code; nothing in the behaviour changed.
//! Callers: `self_update` (the daily check), `installer` (the badge check),
//! `update_pipeline` / `update_run` (the one update pipeline).

use std::path::{Path, PathBuf};
use std::sync::{LazyLock, OnceLock};
use std::time::Duration;

use tokio::process::Command as TokioCommand;
use vct_launcher_core::process::CommandExt as _;

use crate::commands::self_update::VCO_UPSTREAM_REMOTE;

// ---------------------------------------------------------------------------
// v0.2.83 A-F1 / D5: one serialized fetch home.
// v0.2.100 WP-05: bounded, coalescing, classified.
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
// its callers per repository, and it appends `--no-write-fetch-head`
// (git >=2.29) so FETCH_HEAD is never written at all — immune to that whole
// lock class even against EXTERNAL fetchers (VS Code autofetch, a CLI in
// another terminal).
//
// v0.2.100 WP-05 (L2-F03, I-05, the v0.2.98 incident): the serialization used
// to be an UNBOUNDED wait on a process-wide mutex held across the whole retry
// ladder, and the ladder retried EVERY failure. A deterministic refusal (the
// tag-clobber: exit 1, sub-second, no stderr) therefore burned the 156 s ladder
// on every call, and the badge check queued behind it for minutes. Now:
//   * a caller whose fetch is already in flight (same repo, a target the
//     in-flight fetch covers) COALESCES on that result instead of running a
//     second ladder;
//   * every wait — for the lock or for a coalesced result — is bounded by the
//     caller's policy ([`fetch_wait_bound`]); past it the caller gets
//     `Err("another fetch in progress …")`, which every caller renders as an
//     `Unknown` check state, never as a verdict;
//   * only failures CLASSIFIED transient are retried
//     ([`fetch_failure_is_transient`]); a deterministic refusal returns after
//     one attempt, with its exit status;
//   * a timed-out attempt's git child is KILLED (`kill_on_drop`), not orphaned
//     holding the ref locks the next attempt needs;
//   * every log line names the real caller (`file:line`, `#[track_caller]`).

/// The prefix of the error a fetch caller receives when it could not get a
/// turn within its bound. Callers turn fetch errors into
/// `CheckState::Unknown(<error>)` / `UpdateStatus::unavailable(<error>)`, so
/// this is the typed "we could not check just now" — never "up to date".
pub(crate) const FETCH_BUSY: &str = "another fetch in progress";

/// One repository's fetch slot: the serialization lock plus who holds it (for
/// the busy message — a user told "busy" deserves to know by what).
struct RepoFetchSlot {
    lock: tokio::sync::Mutex<()>,
    holder: std::sync::Mutex<Option<String>>,
}

/// Per-repository serialization for upstream fetches. Our startup actors
/// (badge check + self-update daily check) fetch the SAME install-root repo;
/// without a lock they race on ref locks and the loser soft-fails (A-RC3).
/// Keyed per canonical repo path (was one process-wide mutex): production has
/// exactly one install-root clone, so the behaviour there is unchanged, while
/// fetches of unrelated repos (tests, a second clone) no longer wait on each
/// other.
static UPSTREAM_FETCH_LOCKS: LazyLock<
    std::sync::Mutex<std::collections::HashMap<PathBuf, std::sync::Arc<RepoFetchSlot>>>,
> = LazyLock::new(|| std::sync::Mutex::new(std::collections::HashMap::new()));

/// The slot for `repo_key` (created on first use; never removed — one entry
/// per repository the process ever fetched, i.e. one in production).
fn upstream_fetch_slot(repo_key: &Path) -> std::sync::Arc<RepoFetchSlot> {
    let mut map = UPSTREAM_FETCH_LOCKS
        .lock()
        .unwrap_or_else(|p| p.into_inner());
    map.entry(repo_key.to_path_buf())
        .or_insert_with(|| {
            std::sync::Arc::new(RepoFetchSlot {
                lock: tokio::sync::Mutex::new(()),
                holder: std::sync::Mutex::new(None),
            })
        })
        .clone()
}

/// The key two callers must share to coalesce or serialize: the canonical
/// path (the badge and the daily check reach the same clone by different
/// routes), or the path as given when it cannot be canonicalized.
fn fetch_repo_key(repo: &Path) -> PathBuf {
    std::fs::canonicalize(repo).unwrap_or_else(|_| repo.to_path_buf())
}

/// Cached result of the `git --version` >=2.29 probe. `None` until first
/// probed; `Some(true)` when git supports `--no-write-fetch-head`. Probed once
/// per process (D4) — the git binary can't change under a running launcher.
static GIT_SUPPORTS_NO_WRITE_FETCH_HEAD: OnceLock<bool> = OnceLock::new();

/// Fetch retry policy (D5). Selects the backoff ladder, the wait bound and
/// the fetched refspec.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum FetchPolicy {
    /// One retry with a short (~2s) backoff. Used by the interactive /
    /// startup-latency-sensitive surfaces (badge check, pre-merge/rebase
    /// fetches) where a long ladder would stall the UI.
    Quick,
    /// The v0.2.32 UB1 ladder (1/5/30/120s, 5 attempts). Used by the launcher
    /// self-update check and the reattach guard, which must absorb a TRANSIENT
    /// network blip at boot rather than surface a false negative. Since
    /// v0.2.100 only transient failures climb it.
    Persistent,
    /// Tag warming: the forced tag refspec on the QUICK ladder. Its only
    /// caller (`get_latest_source_release_tag`, the Updates page) documents
    /// the fetch as soft-fail and NOT load-bearing — it answers from
    /// `ls-remote` — so it must never hold the repo's fetch slot for the
    /// Persistent ladder's minutes (v0.2.100 WP-05; it did in v0.2.98).
    Tags,
}

/// Quick-policy backoff: a single retry after ~2s. Under `cfg(test)` the unit
/// is milliseconds (matching `FETCH_RETRY_DELAYS_MS`) so tests don't burn
/// wall-time; production interprets it as seconds.
#[cfg(not(test))]
const QUICK_FETCH_DELAYS_MS: [u64; 1] = [2_000];
#[cfg(test)]
const QUICK_FETCH_DELAYS_MS: [u64; 1] = [2];

/// The longest a caller waits for its turn — for the repo's fetch lock, or for
/// the result of an in-flight fetch it coalesced on — before it gives up with
/// [`FETCH_BUSY`]. Chosen per policy: the interactive surfaces must answer
/// fast, the background daily check can afford to wait for a peer's ladder,
/// and tag warming is not load-bearing at all.
fn fetch_wait_bound(policy: FetchPolicy) -> Duration {
    #[cfg(not(test))]
    let (quick, persistent, tags) = (
        Duration::from_secs(15),
        Duration::from_secs(60),
        Duration::from_secs(5),
    );
    #[cfg(test)]
    let (quick, persistent, tags) = (
        Duration::from_millis(1_500),
        Duration::from_millis(1_500),
        Duration::from_millis(1_500),
    );
    match policy {
        FetchPolicy::Quick => quick,
        FetchPolicy::Persistent => persistent,
        FetchPolicy::Tags => tags,
    }
}

/// The retry ladder for `policy`.
fn fetch_retry_delays(policy: FetchPolicy) -> &'static [u64] {
    match policy {
        FetchPolicy::Quick | FetchPolicy::Tags => &QUICK_FETCH_DELAYS_MS,
        FetchPolicy::Persistent => &FETCH_RETRY_DELAYS_MS,
    }
}

/// What a fetch fetches, for coalescing. A `Remote` fetch (the remote's
/// default refspec, `+refs/heads/*:refs/remotes/vco_upstream/*`) also updates
/// every `Branch(_)`'s tracking ref, so a branch caller may coalesce on it; a
/// `Tags` fetch updates tags only and covers nothing else.
#[derive(Debug, Clone, PartialEq, Eq)]
enum FetchTarget {
    Remote,
    Branch(String),
    Tags,
}

impl FetchTarget {
    fn of(policy: FetchPolicy, refspec: Option<&str>) -> Self {
        match (policy, refspec) {
            (FetchPolicy::Tags, _) => FetchTarget::Tags,
            (_, None) => FetchTarget::Remote,
            (_, Some(branch)) => FetchTarget::Branch(branch.to_string()),
        }
    }

    /// `true` when a fetch of `self` leaves `wanted`'s refs as fresh as a
    /// fetch of `wanted` would.
    fn covers(&self, wanted: &FetchTarget) -> bool {
        self == wanted || (*self == FetchTarget::Remote && matches!(wanted, FetchTarget::Branch(_)))
    }
}

/// The published outcome of an in-flight fetch: `None` until it finishes.
type FetchOutcome = Option<Result<(), String>>;

/// A fetch that is running (or waiting for its repo's lock), joinable by
/// later callers whose target it covers.
struct InFlightFetch {
    id: u64,
    repo_key: PathBuf,
    target: FetchTarget,
    caller: String,
    outcome: tokio::sync::watch::Receiver<FetchOutcome>,
}

static IN_FLIGHT_FETCHES: LazyLock<std::sync::Mutex<Vec<InFlightFetch>>> =
    LazyLock::new(|| std::sync::Mutex::new(Vec::new()));
static NEXT_FETCH_ID: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// The leader's side of an [`InFlightFetch`]. Dropping it (normally, or
/// because the leader's future was cancelled) removes the registry entry; a
/// follower whose leader vanished without publishing re-enters the join.
struct FetchLeadership {
    id: u64,
    publish: tokio::sync::watch::Sender<FetchOutcome>,
}

impl FetchLeadership {
    fn publish(&self, result: &Result<(), String>) {
        let _ = self.publish.send(Some(result.clone()));
    }
}

impl Drop for FetchLeadership {
    fn drop(&mut self) {
        let mut reg = IN_FLIGHT_FETCHES.lock().unwrap_or_else(|p| p.into_inner());
        reg.retain(|f| f.id != self.id);
    }
}

enum FetchRole {
    /// Another caller's fetch covers ours: await its outcome.
    Follow {
        outcome: tokio::sync::watch::Receiver<FetchOutcome>,
        leader: String,
    },
    /// Nobody covers us: run the fetch, publishing to anyone who joins.
    Lead(FetchLeadership),
}

/// Join a covering in-flight fetch, or register as the leader of a new one —
/// one atomic decision under the registry lock, so two callers arriving
/// together cannot both lead the same fetch.
fn join_or_lead_fetch(repo_key: &Path, target: &FetchTarget, caller: &str) -> FetchRole {
    let mut reg = IN_FLIGHT_FETCHES.lock().unwrap_or_else(|p| p.into_inner());
    if let Some(f) = reg
        .iter()
        .find(|f| f.repo_key == repo_key && f.target.covers(target))
    {
        return FetchRole::Follow {
            outcome: f.outcome.clone(),
            leader: f.caller.clone(),
        };
    }
    let (publish, outcome) = tokio::sync::watch::channel(None);
    let id = NEXT_FETCH_ID.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    reg.push(InFlightFetch {
        id,
        repo_key: repo_key.to_path_buf(),
        target: target.clone(),
        caller: caller.to_string(),
        outcome,
    });
    FetchRole::Lead(FetchLeadership { id, publish })
}

/// The [`FETCH_BUSY`] error, naming who holds the turn and how long we waited.
fn fetch_busy_error(repo: &Path, holder: &str, waited: Duration) -> String {
    format!(
        "{FETCH_BUSY} at {} (held by {holder}); gave up after {:.1}s instead of queueing \
         behind it — check again in a moment",
        repo.display(),
        waited.as_secs_f32(),
    )
}

/// Where a fetch was requested from — `file:line` of the real caller, carried
/// into every log line and busy message (I-05: the shared helper logged
/// `check_for_updates:` for EVERY caller, so the v0.2.98 logs could not say
/// which surface was burning the ladder).
#[derive(Debug, Clone, Copy)]
pub(crate) struct FetchCaller(&'static std::panic::Location<'static>);

impl FetchCaller {
    #[track_caller]
    pub(crate) fn here() -> Self {
        FetchCaller(std::panic::Location::caller())
    }
}

impl std::fmt::Display for FetchCaller {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}:{}", self.0.file(), self.0.line())
    }
}

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

/// The `git fetch` argv for `policy`.
///
/// No `--quiet` (v0.2.100 WP-05): under `--quiet` git suppresses its ref
/// update report, rejections included, which is how the v0.2.98 tag-clobber
/// became "exit 1, no stderr". stderr is captured, never shown raw, and git
/// prints no progress meter to a pipe, so the only cost of dropping it is the
/// evidence we now keep.
fn fetch_args(policy: FetchPolicy, refspec: Option<&str>, no_write_fetch_head: bool) -> Vec<String> {
    // v0.2.100 final review (blocker): a smart-HTTP fetch cannot resume, so a
    // TOTAL-time cap kills every attempt on a slow link (a one-release pack is
    // ~30 MB) and the update is never offered. A STALLED transfer is what must
    // end an attempt: git aborts when it moves < 1000 B/s for 60 s.
    let mut args: Vec<String> = vec![
        "-c".into(),
        format!("http.lowSpeedLimit={FETCH_LOW_SPEED_LIMIT_BPS}"),
        "-c".into(),
        format!("http.lowSpeedTime={FETCH_LOW_SPEED_TIME_SECS}"),
        "fetch".into(),
    ];
    if no_write_fetch_head {
        args.push("--no-write-fetch-head".into());
    }
    args.push(VCO_UPSTREAM_REMOTE.into());
    if let Some(branch) = refspec {
        args.push(branch.into());
    }
    // v0.2.99: the Tags policy fetches an EXPLICIT forced refspec instead of
    // `--tags`. `--tags` refuses to move a local tag that differs from the
    // remote's — "would clobber existing tag", exit 1 — and the release
    // workflow re-points tags after committing the dist binaries, so any clone
    // that fetched between the first tag push and the re-point was wedged on
    // every subsequent tag fetch. The leading `+` is git's canonical
    // force-update form; upstream release tags are canonical for this fetch.
    if matches!(policy, FetchPolicy::Tags) {
        args.push("+refs/tags/*:refs/tags/*".into());
    }
    args
}

/// One failed fetch attempt: the evidence, and whether retrying can help.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct FetchAttemptError {
    message: String,
    transient: bool,
}

impl FetchAttemptError {
    fn transient(message: impl Into<String>) -> Self {
        Self { message: message.into(), transient: true }
    }

    fn deterministic(message: impl Into<String>) -> Self {
        Self { message: message.into(), transient: false }
    }
}

/// stderr substrings (lower-cased) that mark a failure retrying can fix: the
/// network, the server, or a lock another process holds for a moment. (A lock
/// that is NOT momentary — older than one attempt — is reclassified by
/// [`reclassify_stale_ref_lock`], W4R-03.)
const TRANSIENT_FETCH_MARKERS: &[&str] = &[
    "could not resolve host",
    "could not resolve proxy",
    "temporary failure in name resolution",
    "timed out",
    "connection refused",
    "connection reset",
    "failed to connect",
    "couldn't connect",
    "network is unreachable",
    "no route to host",
    "early eof",
    "the remote end hung up unexpectedly",
    "unexpected disconnect",
    "rpc failed",
    "gnutls",
    "ssl_read",
    "ssl_connect",
    "tls connection",
    "http/2 stream",
    "returned error: 429",
    "returned error: 500",
    "returned error: 502",
    "returned error: 503",
    "returned error: 504",
    // Ref-update contention — only the shapes another RUNNING git causes.
    // `cannot lock ref` alone is NOT a marker (R18-10): it also prefixes the
    // deterministic directory/file ref conflict ([`ref_dir_file_conflict`]).
    ".lock': file exists",
    "': reference already exists",
    " but expected ",
];

/// Should a failed fetch be retried?
///
/// Retry ONLY on evidence of transience: the child was killed by a signal (we
/// cannot know why — an OOM kill, an external `kill`), or stderr names a
/// network / server / lock condition ([`TRANSIENT_FETCH_MARKERS`]). Everything
/// else — "would clobber existing tag", "couldn't find remote ref",
/// authentication, a missing repository, and above all a non-zero exit with
/// NO stderr — is deterministic: the next attempt fails identically, and the
/// Persistent ladder spent 156 s proving it on every call in v0.2.98. The
/// conservative direction is deliberate: an unrecognised transient message
/// costs one missed retry (the next check runs soon); a deterministic failure
/// treated as transient costs minutes of every caller's time.
pub(crate) fn fetch_failure_is_transient(killed_by_signal: bool, stderr: &str) -> bool {
    if killed_by_signal {
        return true;
    }
    let low = stderr.to_ascii_lowercase();
    TRANSIENT_FETCH_MARKERS.iter().any(|m| low.contains(m))
}

/// The one stderr line worth surfacing: the last `fatal:` / `error:` / `!`
/// (ref-rejection) line, else the last non-empty one. Without `--quiet`, git
/// prints a `From <url>` header and per-ref lines, so "the last line" alone
/// could be an innocuous ` * [new tag]` row.
fn fetch_evidence_line(stderr: &str) -> &str {
    let lines: Vec<&str> = stderr.lines().map(str::trim).filter(|l| !l.is_empty()).collect();
    lines
        .iter()
        .rev()
        .find(|l| l.starts_with("fatal:") || l.starts_with("error:") || l.starts_with('!'))
        .or(lines.last())
        .copied()
        .unwrap_or("")
}

/// Turn a finished, failed fetch into its error: the evidence line AND the
/// exit status, always (I-06/I-10: an exit status is the datum that separates
/// refusal (1) from git-fatal (128) from a kill (signal)).
fn describe_failed_fetch(status: &std::process::ExitStatus, stderr: &str) -> FetchAttemptError {
    let line = fetch_evidence_line(stderr);
    let message = if line.is_empty() {
        format!("(no stderr; git {status})")
    } else {
        format!("{line} (git {status})")
    };
    let killed_by_signal = status.code().is_none();
    FetchAttemptError {
        transient: fetch_failure_is_transient(killed_by_signal, stderr),
        message,
    }
}

/// v0.2.100 (W4R-03): the lock file a ref-lock failure names, resolved on
/// disk. Two shapes of git's message: `Unable to create '<path>.lock': File
/// exists` (the path as git printed it — absolute, or relative to git's cwd,
/// i.e. `repo`, else to the git dir) and `cannot lock ref '<refname>'` with no
/// path (→ `<gitdir>/<refname>.lock`). `None` when stderr names no lock.
fn ref_lock_path_from_stderr(repo: &Path, stderr: &str) -> Option<std::path::PathBuf> {
    fn quoted_after<'s>(s: &'s str, low: &str, marker: &str) -> Option<&'s str> {
        let at = low.find(marker)? + marker.len();
        let rest = &s[at..];
        let end = rest.find('\'')?;
        Some(&rest[..end])
    }
    let low = stderr.to_ascii_lowercase();
    let git_dir = git_dir_of(repo);
    if let Some(p) = quoted_after(stderr, &low, "unable to create '") {
        if p.ends_with(".lock") {
            let path = Path::new(p);
            if path.is_absolute() {
                return Some(path.to_path_buf());
            }
            let from_cwd = repo.join(path);
            return Some(if from_cwd.exists() { from_cwd } else { git_dir.join(path) });
        }
    }
    let refname = quoted_after(stderr, &low, "cannot lock ref '")?;
    if refname.is_empty() || refname.contains("..") {
        return None;
    }
    Some(git_dir.join(format!("{refname}.lock")))
}

/// `<repo>/.git` (a directory, or a `gitdir: <path>` file as in a worktree),
/// else `repo` itself (a bare repository). No git is spawned.
fn git_dir_of(repo: &Path) -> std::path::PathBuf {
    let dot_git = repo.join(".git");
    if dot_git.is_dir() {
        return dot_git;
    }
    if let Ok(text) = std::fs::read_to_string(&dot_git) {
        if let Some(target) = text.lines().find_map(|l| l.strip_prefix("gitdir:")) {
            let target = Path::new(target.trim());
            return if target.is_absolute() { target.to_path_buf() } else { repo.join(target) };
        }
    }
    repo.to_path_buf()
}

/// v0.2.100 (W4R-03): a ref-lock failure is transient only while the lock is
/// FRESH — a live git holds it for a moment. A lock older than one whole
/// fetch attempt ([`FETCH_ATTEMPT_TIMEOUT`]) was left by a git that crashed
/// or was killed, and every retry would fail identically (git never removes
/// another process's lock): reclassify as deterministic and name the file.
/// A lock that cannot be stat'ed (already gone, unreadable) stays transient.
fn reclassify_stale_ref_lock(repo: &Path, stderr: &str, err: FetchAttemptError) -> FetchAttemptError {
    if !err.transient {
        return err;
    }
    let Some(lock) = ref_lock_path_from_stderr(repo, stderr) else {
        return err;
    };
    let age = std::fs::metadata(&lock)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.elapsed().ok());
    match age {
        Some(age) if age > FETCH_ATTEMPT_TIMEOUT => FetchAttemptError::deterministic(format!(
            "{} — {} is a stale lock file (last modified {} s ago) left by an earlier git that \
             did not finish; it can be deleted once no git process is running in this repository",
            err.message,
            lock.display(),
            age.as_secs()
        )),
        _ => err,
    }
}

/// v0.2.100 (R18-10): the remote a ref-update failure names — `refs/remotes/<remote>/…`
/// in a `cannot lock ref` line — when git's message is a DIRECTORY/FILE ref
/// conflict: upstream renamed a branch so that one ref name is now a prefix
/// of another (`a` ↔ `a/b`), and the stale local remote-tracking ref blocks
/// the new one. Git's shapes: `'<ref>' exists; cannot create '<ref>'`
/// (2.x, both directions), `there is a non-empty directory '…' blocking
/// reference '…'`, and the older `unable to resolve reference '…': Not a
/// directory`. Deterministic — every retry fails identically until the stale
/// ref is pruned. `Some("")` when the conflict names no remote-tracking ref.
fn ref_dir_file_conflict(stderr: &str) -> Option<String> {
    const MARKER: &str = "cannot lock ref '";
    stderr.lines().find_map(|line| {
        // ASCII lower-casing keeps byte offsets, so `at` indexes `line` too.
        let low = line.to_ascii_lowercase();
        let conflict = low.contains("' exists; cannot create '")
            || low.contains("blocking reference '")
            || low.contains(": not a directory");
        if !conflict {
            return None;
        }
        let at = low.find(MARKER)? + MARKER.len();
        let refname = line[at..].split('\'').next()?;
        Some(
            refname
                .strip_prefix("refs/remotes/")
                .and_then(|r| r.split('/').next())
                .unwrap_or("")
                .to_string(),
        )
    })
}

/// v0.2.100 (R18-10): a directory/file ref conflict is deterministic, and its
/// error says the one thing that fixes it: `git remote prune <remote>` in
/// this repository (it deletes the stale remote-tracking ref that blocks the
/// renamed branch; local branches are untouched). Any other failure passes
/// through unchanged.
fn classify_ref_dir_file_conflict(repo: &Path, stderr: &str, err: FetchAttemptError) -> FetchAttemptError {
    let Some(remote) = ref_dir_file_conflict(stderr) else {
        return err;
    };
    let remote = if remote.is_empty() { "<remote>".to_string() } else { remote };
    FetchAttemptError::deterministic(format!(
        "{} — an upstream branch was renamed so that its new name collides with a stale \
         remote-tracking ref in this clone; retrying cannot fix it. Run `git remote prune {}` in \
         {} (it removes only stale remote-tracking refs), then update again",
        err.message,
        remote,
        repo.display()
    ))
}

/// One `git fetch` attempt. `kill_on_drop`: when the per-attempt timeout in
/// [`fetch_with_retry`] drops this future, the child dies with it.
async fn run_fetch_attempt(
    program: &std::ffi::OsStr,
    repo: &Path,
    args: &[String],
) -> Result<(), FetchAttemptError> {
    let out = crate::commands::git_cmd::git_network_command(program)
        .args(args)
        .current_dir(repo)
        .output()
        .await
        .map_err(|e| FetchAttemptError::deterministic(format!("git fetch could not be started: {e}")))?;
    if out.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&out.stderr);
    let err = describe_failed_fetch(&out.status, &stderr);
    let err = classify_ref_dir_file_conflict(repo, &stderr, err);
    Err(reclassify_stale_ref_lock(repo, &stderr, err))
}

/// The ONE production upstream fetch (D5). Coalesces with a covering fetch
/// already in flight, otherwise takes the repo's fetch lock (bounded), appends
/// `--no-write-fetch-head` when git supports it, and retries TRANSIENT
/// failures per `policy`. Caller MUST have run `ensure_upstream_remote` first
/// (unchanged contract).
///
/// `refspec`: `None` fetches the remote's default refspecs (`vco_upstream`);
/// `Some(branch)` fetches exactly that branch (`vco_upstream <branch>`).
///
/// `#[track_caller]` on a plain fn returning the future (an `async fn` cannot
/// carry it on stable): the caller's `file:line` is captured here, before the
/// first poll, and names the caller in every log line (I-05). The git program
/// is resolved here too, so a test's per-thread lookup `PATH` is honoured
/// even though the command is built later.
///
/// `Err` is never empty: it carries git's evidence line and exit status, the
/// timeout, or [`FETCH_BUSY`].
#[track_caller]
pub(crate) fn serialized_fetch_upstream<'a>(
    repo: &'a Path,
    policy: FetchPolicy,
    refspec: Option<&'a str>,
) -> impl std::future::Future<Output = Result<(), String>> + Send + 'a {
    let caller = FetchCaller::here();
    let program = crate::commands::git_cmd::git_program();
    async move {
        let no_write_fetch_head = supports_no_write_fetch_head().await;
        let args = fetch_args(policy, refspec, no_write_fetch_head);
        let attempt = || run_fetch_attempt(&program, repo, &args);
        coalesced_fetch(
            repo,
            FetchTarget::of(policy, refspec),
            policy,
            &caller.to_string(),
            attempt,
        )
        .await
    }
}

/// Join a covering in-flight fetch or lead a new one, every wait bounded by
/// [`fetch_wait_bound`]. Parametrised over the attempt so the coalescing,
/// bound and serialization tests inject fakes without spawning git.
///
/// A leader registers BEFORE it waits for the lock, so a caller arriving
/// while the leader queues still coalesces on it. A leader that cannot get
/// the lock in time publishes the busy error to its own followers too.
async fn coalesced_fetch<F, Fut>(
    repo: &Path,
    target: FetchTarget,
    policy: FetchPolicy,
    caller: &str,
    mut attempt_fn: F,
) -> Result<(), String>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<(), FetchAttemptError>>,
{
    let key = fetch_repo_key(repo);
    let bound = fetch_wait_bound(policy);
    let deadline = tokio::time::Instant::now() + bound;
    loop {
        match join_or_lead_fetch(&key, &target, caller) {
            FetchRole::Follow { mut outcome, leader } => {
                let waited = tokio::time::timeout_at(deadline, async {
                    outcome
                        .wait_for(|o| o.is_some())
                        .await
                        .map(|published| published.clone())
                })
                .await;
                match waited {
                    Ok(Ok(Some(result))) => {
                        tracing::info!(
                            "[vct] git fetch ({caller}): coalesced on the in-flight fetch \
                             started by {leader} at {} → {}",
                            repo.display(),
                            match &result {
                                Ok(()) => "ok".to_string(),
                                Err(e) => e.clone(),
                            }
                        );
                        return result;
                    }
                    // The leader was dropped without publishing (its caller was
                    // cancelled): join or lead afresh, within the same deadline.
                    Ok(Ok(None)) | Ok(Err(_)) => continue,
                    Err(_elapsed) => {
                        let e = fetch_busy_error(repo, &leader, bound);
                        tracing::warn!("[vct] git fetch ({caller}): {e}");
                        return Err(e);
                    }
                }
            }
            FetchRole::Lead(leadership) => {
                let slot = upstream_fetch_slot(&key);
                let guard = match tokio::time::timeout_at(deadline, slot.lock.lock()).await {
                    Ok(guard) => guard,
                    Err(_elapsed) => {
                        let holder = slot
                            .holder
                            .lock()
                            .unwrap_or_else(|p| p.into_inner())
                            .clone()
                            .unwrap_or_else(|| "an unnamed caller".to_string());
                        let e = fetch_busy_error(repo, &holder, bound);
                        tracing::warn!("[vct] git fetch ({caller}): {e}");
                        let result = Err(e);
                        leadership.publish(&result);
                        return result;
                    }
                };
                *slot.holder.lock().unwrap_or_else(|p| p.into_inner()) = Some(caller.to_string());
                let result =
                    fetch_with_retry(repo, caller, fetch_retry_delays(policy), &mut attempt_fn).await;
                *slot.holder.lock().unwrap_or_else(|p| p.into_inner()) = None;
                drop(guard);
                leadership.publish(&result);
                return result;
            }
        }
    }
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

/// M-2 (v0.2.83): per-ATTEMPT ceiling for the serialized upstream fetch. One
/// hung fetch (dead network, hung credential helper, stuck DNS) would
/// otherwise stall the ladder forever. A timeout is a RETRYABLE error, and since
/// v0.2.100 the timed-out child is killed (`kill_on_drop`).
///
/// Production: 600s (v0.2.100 final review). It was 30s, which — once the
/// child is killed — made every fetch on a link slower than ~8 Mbit/s fail
/// for good (a fetch cannot resume). A slow but MOVING transfer now finishes;
/// a stalled one is ended by git itself (`http.lowSpeedLimit`/`lowSpeedTime`,
/// see [`fetch_args`]). Callers never queue behind a long fetch: their wait
/// is bounded by [`fetch_wait_bound`].
/// Under `cfg(test)` it is milliseconds, sized so a fake `git` script has
/// time to start before the timeout fires.
#[cfg(not(test))]
const FETCH_ATTEMPT_TIMEOUT: Duration = Duration::from_secs(600);

/// Stall detection passed to git (see [`fetch_args`]): an attempt whose
/// transfer stays below this many bytes/s for [`FETCH_LOW_SPEED_TIME_SECS`]
/// is aborted by git, so a dead link fails fast while a slow one completes.
const FETCH_LOW_SPEED_LIMIT_BPS: u32 = 1000;
const FETCH_LOW_SPEED_TIME_SECS: u32 = 60;
#[cfg(test)]
const FETCH_ATTEMPT_TIMEOUT: Duration = Duration::from_millis(400);

/// Fetch the canonical upstream (NOT `origin`) on the Persistent ladder.
/// Caller MUST have run `ensure_upstream_remote` first.
///
/// v0.2.32 UB1 (2026-05-23): replaced a single-shot `git fetch` that left the
/// launcher stuck on stale state after a transient network hiccup at boot.
/// v0.2.83 (D5): a thin wrapper over `serialized_fetch_upstream`.
/// v0.2.100 (WP-05): only transient failures climb the ladder, and
/// `#[track_caller]` passes the REAL caller through to the log lines.
#[track_caller]
pub(crate) fn fetch_upstream(repo: &Path) -> impl std::future::Future<Output = Result<(), String>> + Send + '_ {
    serialized_fetch_upstream(repo, FetchPolicy::Persistent, None)
}

/// Inner retry loop, parametrised over the actual fetch attempt so unit
/// tests can swap in a closure that simulates failures without invoking
/// a real `git` binary. The first attempt is immediate; subsequent
/// attempts sleep for `delays[i-1]` before retrying — but ONLY after a
/// failure classified transient. A deterministic failure returns at once.
///
/// `repo` and `caller` are for diagnostic logging only — the closure
/// already captures the directory it needs.
async fn fetch_with_retry<F, Fut>(
    repo: &Path,
    caller: &str,
    delays: &[u64],
    mut attempt_fn: F,
) -> Result<(), String>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<(), FetchAttemptError>>,
{
    let total = delays.len() + 1;
    let mut attempt = 0;
    loop {
        if attempt > 0 {
            tokio::time::sleep(Duration::from_millis(delays[attempt - 1])).await;
        }
        // M-2 (v0.2.83): cap each attempt so one hung fetch can't stall the
        // ladder forever. Dropping the timed-out future kills its git child
        // (`kill_on_drop`, v0.2.100) — a timeout is transient.
        let result = match tokio::time::timeout(FETCH_ATTEMPT_TIMEOUT, attempt_fn()).await {
            Ok(inner) => inner,
            Err(_elapsed) => Err(FetchAttemptError::transient(format!(
                "git fetch timed out after {:.1}s (the git child was killed)",
                FETCH_ATTEMPT_TIMEOUT.as_secs_f32()
            ))),
        };
        match result {
            Ok(()) => {
                if attempt > 0 {
                    tracing::info!(
                        "[vct] git fetch ({caller}): succeeded after {} retries at {}",
                        attempt,
                        repo.display()
                    );
                }
                return Ok(());
            }
            Err(e) => {
                let last = attempt + 1 == total;
                tracing::warn!(
                    "[vct] git fetch ({caller}): attempt {}/{} failed at {}: {}{}",
                    attempt + 1,
                    total,
                    repo.display(),
                    e.message,
                    if !e.transient {
                        " — deterministic failure, not retried"
                    } else if last {
                        " — retries exhausted"
                    } else {
                        " — transient, retrying"
                    }
                );
                if !e.transient || last {
                    return Err(e.message);
                }
            }
        }
        attempt += 1;
    }
}


#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command as StdCommand;

    // ---------------------------------------------------------------------
    // v0.2.32 UB1 / v0.2.83 D5 / v0.2.100 WP-05: the fetch ladder.
    // ---------------------------------------------------------------------
    //
    // The retry, coalescing, bound and serialization tests inject a closure
    // attempt; the `fake_git` tests below drive the REAL production path
    // (`serialized_fetch_upstream`) against a POSIX `sh` fake `git` put first
    // on this thread's lookup PATH (`paths::with_lookup_path`, never the
    // process PATH). Under `cfg(test)` every delay is milliseconds.

    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    /// A repo path unique to one test, so the per-repo fetch slot and the
    /// in-flight registry never couple two concurrently running tests.
    fn fake_repo(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!("vct-wp05-{tag}-{}", std::process::id()))
    }

    fn transient(msg: &str) -> FetchAttemptError {
        FetchAttemptError::transient(msg)
    }

    #[cfg(unix)]
    fn exit_status(code: i32) -> std::process::ExitStatus {
        use std::os::unix::process::ExitStatusExt;
        std::process::ExitStatus::from_raw(code << 8)
    }
    #[cfg(windows)]
    fn exit_status(code: i32) -> std::process::ExitStatus {
        use std::os::windows::process::ExitStatusExt;
        std::process::ExitStatus::from_raw(code as u32)
    }

    /// `ExitStatus`'s Display for exit code 1 on this OS.
    fn exit_1_text() -> &'static str {
        if cfg!(windows) { "exit code: 1" } else { "exit status: 1" }
    }

    #[tokio::test]
    async fn fetch_upstream_with_retry_succeeds_first_attempt() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result = fetch_with_retry(&fake_repo("r1"), "test", &FETCH_RETRY_DELAYS_MS, move || {
            let calls_c = calls_c.clone();
            async move {
                calls_c.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }
        })
        .await;
        assert!(result.is_ok(), "should succeed first attempt");
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn fetch_upstream_with_retry_succeeds_on_third_attempt() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result = fetch_with_retry(&fake_repo("r3"), "test", &FETCH_RETRY_DELAYS_MS, move || {
            let calls_c = calls_c.clone();
            async move {
                let n = calls_c.fetch_add(1, Ordering::SeqCst) + 1;
                if n < 3 {
                    Err(transient(&format!("fatal: early EOF {n}")))
                } else {
                    Ok(())
                }
            }
        })
        .await;
        assert!(result.is_ok(), "should succeed on third attempt");
        assert_eq!(calls.load(Ordering::SeqCst), 3);
    }

    #[tokio::test]
    async fn transient_failures_climb_the_whole_ladder_and_report_the_last() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result = fetch_with_retry(&fake_repo("r5"), "test", &FETCH_RETRY_DELAYS_MS, move || {
            let calls_c = calls_c.clone();
            async move {
                let n = calls_c.fetch_add(1, Ordering::SeqCst) + 1;
                Err(transient(&format!("transient failure {n}")))
            }
        })
        .await;
        // 5 attempts total: 1 immediate + 4 delayed retries.
        assert_eq!(calls.load(Ordering::SeqCst), 5);
        let err = result.unwrap_err();
        assert!(err.contains("transient failure 5"), "got: {err}");
    }

    /// v0.2.100 WP-05 — THE v0.2.98 incident shape: a deterministic refusal
    /// (exit 1) makes exactly ONE attempt, even on the Persistent ladder.
    /// Pre-fix it made five and slept 156 s between them.
    #[tokio::test]
    async fn deterministic_failure_is_not_retried() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let started = std::time::Instant::now();
        let result = fetch_with_retry(&fake_repo("det"), "test", &FETCH_RETRY_DELAYS_MS, move || {
            let calls_c = calls_c.clone();
            async move {
                calls_c.fetch_add(1, Ordering::SeqCst);
                Err(describe_failed_fetch(&exit_status(1), ""))
            }
        })
        .await;
        assert_eq!(calls.load(Ordering::SeqCst), 1, "a refusal must not be retried");
        let err = result.unwrap_err();
        assert!(err.contains(exit_1_text()), "the exit status is the evidence, got: {err}");
        assert!(started.elapsed() < Duration::from_millis(100), "no backoff was slept");
    }

    /// The classification table behind the retry decision.
    #[test]
    fn fetch_failures_are_classified_by_evidence() {
        // Deterministic: silent exit 1 (the v0.2.98 clobber), refusals,
        // missing refs, auth.
        assert!(!describe_failed_fetch(&exit_status(1), "").transient);
        assert!(!fetch_failure_is_transient(
            false,
            " ! [rejected]        vX -> vX  (would clobber existing tag)"
        ));
        assert!(!fetch_failure_is_transient(false, "fatal: couldn't find remote ref nope"));
        assert!(!fetch_failure_is_transient(
            false,
            "fatal: Authentication failed for 'https://example.invalid/r.git/'"
        ));
        // Transient: network, server, momentary lock, a kill.
        assert!(fetch_failure_is_transient(
            false,
            "fatal: unable to access 'https://x/': Could not resolve host: x"
        ));
        assert!(fetch_failure_is_transient(
            false,
            "fatal: unable to access 'https://x/': Failed to connect to x port 443: Connection refused"
        ));
        assert!(fetch_failure_is_transient(false, "fatal: early EOF"));
        assert!(fetch_failure_is_transient(
            false,
            "error: Unable to create '/r/.git/refs/remotes/vco_upstream/main.lock': File exists."
        ));
        assert!(fetch_failure_is_transient(false, "The requested URL returned error: 503"));
        assert!(fetch_failure_is_transient(true, ""), "a signal kill is retried");
    }

    /// Every failed attempt's message carries the exit status — with stderr
    /// and without — and names the rejection line, not the `From` header.
    #[test]
    fn failed_fetch_message_always_carries_the_exit_status() {
        let silent = describe_failed_fetch(&exit_status(1), "");
        assert_eq!(silent.message, format!("(no stderr; git {})", exit_1_text()));
        let noisy = describe_failed_fetch(
            &exit_status(1),
            "From /tmp/remote\n ! [rejected]  vX -> vX  (would clobber existing tag)\n * [new tag] vY -> vY\n",
        );
        assert!(noisy.message.contains("would clobber existing tag"), "{}", noisy.message);
        assert!(noisy.message.contains(exit_1_text()), "{}", noisy.message);
    }

    /// No policy passes `--quiet` any more (it suppressed the rejection
    /// report); Tags keeps the v0.2.99 forced refspec; a branch refspec is
    /// passed through.
    #[test]
    fn fetch_args_keep_evidence_and_the_forced_tag_refspec() {
        for policy in [FetchPolicy::Quick, FetchPolicy::Persistent, FetchPolicy::Tags] {
            let args = fetch_args(policy, None, true);
            assert!(!args.iter().any(|a| a == "--quiet"), "{policy:?}: {args:?}");
            // Stall detection precedes the subcommand (git -c … fetch).
            assert_eq!(
                &args[..5],
                &["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60", "fetch"]
            );
            assert!(args.iter().any(|a| a == "--no-write-fetch-head"));
        }
        assert!(fetch_args(FetchPolicy::Tags, None, false)
            .iter()
            .any(|a| a == "+refs/tags/*:refs/tags/*"));
        assert!(!fetch_args(FetchPolicy::Quick, None, false)
            .iter()
            .any(|a| a.contains("refs/tags")));
        assert_eq!(
            fetch_args(FetchPolicy::Quick, Some("main"), false),
            vec![
                "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60",
                "fetch", "vco_upstream", "main"
            ]
        );
    }

    /// The Tags policy (non-load-bearing tag warming) rides the QUICK ladder.
    #[test]
    fn tag_warming_uses_the_quick_ladder() {
        assert_eq!(fetch_retry_delays(FetchPolicy::Tags), &QUICK_FETCH_DELAYS_MS[..]);
        assert_eq!(fetch_retry_delays(FetchPolicy::Quick), &QUICK_FETCH_DELAYS_MS[..]);
        assert_eq!(fetch_retry_delays(FetchPolicy::Persistent), &FETCH_RETRY_DELAYS_MS[..]);
    }

    /// v0.2.100 final review (blocker): a slow but moving fetch must be able to
    /// finish. The production per-attempt ceiling is minutes, not seconds — a
    /// ~30 MB pack needs > 30 s below ~8 Mbit/s and a killed fetch cannot
    /// resume — and stall detection is what ends a dead transfer.
    #[test]
    fn a_slow_link_can_finish_a_release_fetch() {
        const PRODUCTION_CEILING_SECS: u64 = 600;
        assert!(PRODUCTION_CEILING_SECS >= 300, "ceiling must allow a slow pack");
        assert!(FETCH_LOW_SPEED_TIME_SECS >= 30 && FETCH_LOW_SPEED_TIME_SECS < PRODUCTION_CEILING_SECS as u32);
        let src = include_str!("upstream_fetch.rs");
        let line = src
            .lines()
            .find(|l| l.contains("const FETCH_ATTEMPT_TIMEOUT") && !l.contains("from_millis"))
            .expect("production FETCH_ATTEMPT_TIMEOUT");
        assert!(
            line.contains(&format!("from_secs({PRODUCTION_CEILING_SECS})")),
            "production fetch ceiling changed: {line}"
        );
    }

    /// D4: parse `git --version` and gate `--no-write-fetch-head` on >=2.29.
    #[test]
    fn git_version_parser_gates_no_write_fetch_head_flag() {
        assert!(!git_version_supports_no_write_fetch_head("git version 2.28.0"));
        assert!(!git_version_supports_no_write_fetch_head("git version 2.17.1"));
        assert!(!git_version_supports_no_write_fetch_head("git version 1.9.5"));
        assert!(git_version_supports_no_write_fetch_head("git version 2.29.0"));
        assert!(git_version_supports_no_write_fetch_head("git version 2.43.5"));
        assert!(git_version_supports_no_write_fetch_head("git version 3.0.0"));
        assert!(git_version_supports_no_write_fetch_head(
            "git version 2.43.5 (Apple Git-154)"
        ));
        assert!(git_version_supports_no_write_fetch_head(
            "git version 2.44.0.windows.1"
        ));
        assert!(!git_version_supports_no_write_fetch_head("garbage"));
        assert!(!git_version_supports_no_write_fetch_head(""));
        assert!(!git_version_supports_no_write_fetch_head("git version"));
        assert!(!git_version_supports_no_write_fetch_head("git version x.y.z"));
        assert!(!git_version_supports_no_write_fetch_head("2.29.0"));
    }

    /// D5 Quick policy: exactly one retry (2 attempts) on a transient failure.
    #[tokio::test]
    async fn quick_policy_retries_a_transient_failure_once() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result = fetch_with_retry(&fake_repo("q2"), "test", &QUICK_FETCH_DELAYS_MS, move || {
            let calls_c = calls_c.clone();
            async move {
                let n = calls_c.fetch_add(1, Ordering::SeqCst) + 1;
                Err(transient(&format!("fatal: the remote end hung up unexpectedly ({n})")))
            }
        })
        .await;
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert!(result.unwrap_err().contains("(2)"));
    }

    /// REGRESSION PIN (A-RC3), updated for v0.2.100: fetches of the same repo
    /// that do NOT cover each other (a tag fetch and a remote fetch) never
    /// run their attempts concurrently — the repo's slot serializes them.
    #[tokio::test]
    async fn non_covering_fetches_of_one_repo_are_serialized() {
        use std::sync::atomic::AtomicBool;
        let repo = fake_repo("serial");
        let in_flight = Arc::new(AtomicBool::new(false));
        let overlap = Arc::new(AtomicBool::new(false));
        let run = |target: FetchTarget, policy: FetchPolicy| {
            let (in_flight, overlap, repo) = (in_flight.clone(), overlap.clone(), repo.clone());
            async move {
                coalesced_fetch(&repo, target, policy, "test", move || {
                    let (in_flight, overlap) = (in_flight.clone(), overlap.clone());
                    async move {
                        if in_flight.swap(true, Ordering::SeqCst) {
                            overlap.store(true, Ordering::SeqCst);
                        }
                        tokio::task::yield_now().await;
                        tokio::time::sleep(Duration::from_millis(20)).await;
                        in_flight.store(false, Ordering::SeqCst);
                        Ok(())
                    }
                })
                .await
            }
        };
        let (a, b) = tokio::join!(
            run(FetchTarget::Remote, FetchPolicy::Quick),
            run(FetchTarget::Tags, FetchPolicy::Tags)
        );
        assert!(a.is_ok() && b.is_ok());
        assert!(!overlap.load(Ordering::SeqCst), "two fetches of one repo overlapped");
    }

    /// v0.2.100 WP-05: callers arriving while a covering fetch is in flight
    /// COALESCE on its result — one attempt runs, every caller gets its
    /// outcome (failures included), and a branch fetch coalesces on a
    /// remote-wide one.
    #[tokio::test]
    async fn concurrent_callers_coalesce_on_the_in_flight_fetch() {
        for outcome in [Ok(()), Err("fatal: early EOF (git exit status: 128)".to_string())] {
            let repo = fake_repo(if outcome.is_ok() { "coal-ok" } else { "coal-err" });
            let calls = Arc::new(AtomicUsize::new(0));
            let run = |target: FetchTarget| {
                let (calls, repo, outcome) = (calls.clone(), repo.clone(), outcome.clone());
                async move {
                    coalesced_fetch(&repo, target, FetchPolicy::Quick, "test", move || {
                        let (calls, outcome) = (calls.clone(), outcome.clone());
                        async move {
                            calls.fetch_add(1, Ordering::SeqCst);
                            tokio::time::sleep(Duration::from_millis(100)).await;
                            // Deterministic, so the leader makes one attempt.
                            outcome.map_err(FetchAttemptError::deterministic)
                        }
                    })
                    .await
                }
            };
            let (a, b, c) = tokio::join!(
                run(FetchTarget::Remote),
                run(FetchTarget::Remote),
                run(FetchTarget::Branch("main".into()))
            );
            assert_eq!(calls.load(Ordering::SeqCst), 1, "one fetch for three callers");
            assert_eq!(a, outcome);
            assert_eq!(b, outcome);
            assert_eq!(c, outcome);
        }
    }

    #[test]
    fn fetch_target_coverage() {
        let branch = FetchTarget::Branch("main".into());
        assert!(FetchTarget::Remote.covers(&branch));
        assert!(FetchTarget::Remote.covers(&FetchTarget::Remote));
        assert!(!FetchTarget::Remote.covers(&FetchTarget::Tags));
        assert!(!FetchTarget::Tags.covers(&FetchTarget::Remote));
        assert!(!branch.covers(&FetchTarget::Remote));
        assert!(!branch.covers(&FetchTarget::Branch("dev".into())));
    }

    /// v0.2.100 WP-05 (L2-F03): a caller that cannot get the repo's fetch
    /// slot within its bound returns the typed busy error — it does NOT queue
    /// behind the holder, and it never runs its own attempt.
    #[tokio::test]
    async fn lock_wait_is_bounded_and_returns_busy() {
        let repo = fake_repo("busy");
        let slot = upstream_fetch_slot(&fetch_repo_key(&repo));
        let _held = slot.lock.lock().await;
        *slot.holder.lock().unwrap() = Some("the-holder.rs:1".into());

        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let bound = fetch_wait_bound(FetchPolicy::Quick);
        let started = std::time::Instant::now();
        // W4R-12: an outer bound, so a regression that drops the wait bound
        // FAILS with a message instead of hanging the suite.
        let result = tokio::time::timeout(
            bound * 4,
            coalesced_fetch(&repo, FetchTarget::Remote, FetchPolicy::Quick, "test", move || {
                let calls_c = calls_c.clone();
                async move {
                    calls_c.fetch_add(1, Ordering::SeqCst);
                    Ok(())
                }
            }),
        )
        .await
        .unwrap_or_else(|_| {
            *slot.holder.lock().unwrap() = None;
            panic!("the lock wait is unbounded: still queued after {:?} (bound {bound:?})", bound * 4)
        });
        let elapsed = started.elapsed();
        let err = result.expect_err("a held slot past the bound is busy");
        assert!(err.starts_with(FETCH_BUSY), "got: {err}");
        assert!(err.contains("the-holder.rs:1"), "names the holder: {err}");
        assert_eq!(calls.load(Ordering::SeqCst), 0, "no attempt ran");
        assert!(elapsed >= bound, "gave up early: {elapsed:?}");
        assert!(elapsed < bound + Duration::from_secs(2), "waited past the bound: {elapsed:?}");
        *slot.holder.lock().unwrap() = None;
    }

    /// M-2 (v0.2.83) + v0.2.100: a never-resolving attempt TIMES OUT (a
    /// transient error, retried on the ladder) and releases the repo's slot.
    #[tokio::test]
    async fn never_resolving_attempt_times_out_and_releases_the_slot() {
        let repo = fake_repo("hang");
        let hung = coalesced_fetch(&repo, FetchTarget::Remote, FetchPolicy::Quick, "test", || {
            std::future::pending::<Result<(), FetchAttemptError>>()
        })
        .await;
        let msg = hung.expect_err("a hung attempt must time out");
        assert!(msg.contains("timed out"), "got: {msg:?}");

        let follow_up = tokio::time::timeout(
            Duration::from_secs(5),
            coalesced_fetch(&repo, FetchTarget::Remote, FetchPolicy::Quick, "test", || async {
                Ok(())
            }),
        )
        .await;
        assert!(
            matches!(follow_up, Ok(Ok(()))),
            "the slot must be free after the timeout, got {follow_up:?}"
        );
    }

    /// M-2 companion: a timeout is RETRYABLE — a hung first attempt is
    /// followed by a retry that succeeds.
    #[tokio::test]
    async fn timeout_is_retryable_and_a_later_attempt_can_succeed() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_c = calls.clone();
        let result = fetch_with_retry(&fake_repo("tmo"), "test", &[1], move || {
            let calls_c = calls_c.clone();
            async move {
                let n = calls_c.fetch_add(1, Ordering::SeqCst) + 1;
                if n < 2 {
                    std::future::pending::<Result<(), FetchAttemptError>>().await
                } else {
                    Ok(())
                }
            }
        })
        .await;
        assert!(result.is_ok());
        assert_eq!(calls.load(Ordering::SeqCst), 2);
    }

    /// A minimal `tracing` subscriber that records every event's message, so
    /// a test can assert on what the fetch ladder LOGGED. (`cfg(unix)`: its
    /// only user drives the POSIX fake git.)
    #[cfg(unix)]
    struct CaptureLogs(Arc<std::sync::Mutex<Vec<String>>>);

    #[cfg(unix)]
    struct MessageOf(String);
    #[cfg(unix)]
    impl tracing::field::Visit for MessageOf {
        fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
            if field.name() == "message" {
                self.0 = format!("{value:?}");
            }
        }
    }

    #[cfg(unix)]
    impl tracing::Subscriber for CaptureLogs {
        fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
            true
        }
        fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
            tracing::span::Id::from_u64(1)
        }
        fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
        fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
        fn event(&self, event: &tracing::Event<'_>) {
            let mut m = MessageOf(String::new());
            event.record(&mut m);
            self.0.lock().unwrap().push(m.0);
        }
        fn enter(&self, _: &tracing::span::Id) {}
        fn exit(&self, _: &tracing::span::Id) {}
    }

    /// POSIX fake `git`: a `sh` script in a fresh dir, put first on THIS
    /// thread's lookup PATH by the caller. `body` runs after the invocation
    /// is appended to `<dir>/calls` (one line per spawn, with the pid).
    #[cfg(unix)]
    fn fake_git(body: &str) -> tempfile::TempDir {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().expect("tempdir");
        let calls = dir.path().join("calls");
        let script = format!(
            "#!/bin/sh\necho \"$$ $*\" >> '{}'\n{body}\n",
            calls.display()
        );
        let git = dir.path().join("git");
        std::fs::write(&git, script).unwrap();
        std::fs::set_permissions(&git, std::fs::Permissions::from_mode(0o755)).unwrap();
        dir
    }

    #[cfg(unix)]
    fn fake_git_calls(dir: &tempfile::TempDir) -> Vec<String> {
        std::fs::read_to_string(dir.path().join("calls"))
            .unwrap_or_default()
            .lines()
            .map(str::to_string)
            .collect()
    }

    /// `true` while `pid` is a live, non-zombie process.
    #[cfg(unix)]
    fn process_running(pid: u32) -> bool {
        let out = StdCommand::new("ps")
            .args(["-o", "stat=", "-p", &pid.to_string()])
            .output()
            .expect("ps");
        let stat = String::from_utf8_lossy(&out.stdout).trim().to_string();
        !stat.is_empty() && !stat.starts_with('Z')
    }

    /// v0.2.100 WP-05 (L2-F03): a timed-out `git fetch` child is KILLED, not
    /// orphaned. The fake git records its pid and `exec`s a 30 s sleep; the
    /// ladder times out each attempt; afterwards no recorded pid is running.
    /// Pre-fix (`git_command()` without `kill_on_drop`) both sleeps outlived
    /// the Err by half a minute, holding whatever locks a real fetch held.
    #[cfg(unix)]
    #[tokio::test]
    async fn timed_out_fetch_leaves_no_orphan_child() {
        let git = fake_git("exec sleep 30");
        let repo = tempfile::tempdir().unwrap();
        let fut = vct_launcher_core::paths::with_lookup_path(Some(git.path().as_os_str()), || {
            serialized_fetch_upstream(repo.path(), FetchPolicy::Quick, None)
        });
        let err = fut.await.expect_err("a hung fetch must time out");
        assert!(err.contains("timed out"), "got: {err}");

        let pids: Vec<u32> = fake_git_calls(&git)
            .iter()
            .filter_map(|l| l.split_whitespace().next()?.parse().ok())
            .collect();
        assert_eq!(pids.len(), 2, "Quick = two attempts, each spawned: {pids:?}");
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        while pids.iter().any(|p| process_running(*p)) && std::time::Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(50));
        }
        let orphans: Vec<&u32> = pids.iter().filter(|p| process_running(**p)).collect();
        assert!(orphans.is_empty(), "orphaned git children still running: {orphans:?}");
    }

    /// Exit-1 refusal through the REAL fetch path: ONE spawn even on the
    /// Persistent ladder, and the error carries the exit status.
    #[cfg(unix)]
    #[tokio::test]
    async fn real_path_refusal_spawns_git_once() {
        let git = fake_git("exit 1");
        let repo = tempfile::tempdir().unwrap();
        let fut = vct_launcher_core::paths::with_lookup_path(Some(git.path().as_os_str()), || {
            serialized_fetch_upstream(repo.path(), FetchPolicy::Persistent, None)
        });
        let err = fut.await.expect_err("exit 1 is a failure");
        assert!(err.contains("(no stderr; git exit status: 1)"), "got: {err}");
        assert_eq!(fake_git_calls(&git).len(), 1, "a refusal must not be retried");
    }

    /// A transient failure through the REAL fetch path IS retried.
    #[cfg(unix)]
    #[tokio::test]
    async fn real_path_transient_failure_is_retried() {
        let git = fake_git(
            "echo \"fatal: unable to access 'https://x/': Could not resolve host: x\" >&2; exit 128",
        );
        let repo = tempfile::tempdir().unwrap();
        let fut = vct_launcher_core::paths::with_lookup_path(Some(git.path().as_os_str()), || {
            serialized_fetch_upstream(repo.path(), FetchPolicy::Quick, None)
        });
        let err = fut.await.expect_err("still failing");
        assert!(err.contains("Could not resolve host"), "got: {err}");
        assert!(err.contains("exit status: 128"), "got: {err}");
        assert_eq!(fake_git_calls(&git).len(), 2, "Quick retries a transient failure once");
    }

    /// W4R-03: a temp repo (a `.git` directory — no git is spawned, so this
    /// runs on every OS) holding a planted ref lock whose mtime is `age` ago,
    /// and git's exact "File exists" text for it. The path in the text is
    /// written the way git prints it on this OS: on Windows git prints
    /// forward slashes (`C:/Users/…/main.lock`), which is what R18-14 covers.
    fn repo_with_ref_lock(age: Duration) -> (tempfile::TempDir, std::path::PathBuf, String) {
        let repo = tempfile::tempdir().unwrap();
        let dir = repo.path().join(".git").join("refs").join("remotes").join("vco_upstream");
        std::fs::create_dir_all(&dir).unwrap();
        let lock = dir.join("main.lock");
        plant_lock(&lock, age);
        let printed = git_printed_path(&lock);
        let msg = format!(
            "error: cannot lock ref 'refs/remotes/vco_upstream/main': Unable to create '{printed}': File exists.\n\n\
             Another git process seems to be running in this repository, e.g.\n\
             an editor opened by 'git commit'. Please make sure all processes\n\
             are terminated then try again. If it still fails, a git process\n\
             may have crashed in this repository earlier:\n\
             remove the file manually to continue.\n \
             ! [new branch]      main       -> vco_upstream/main  (unable to update local ref)"
        );
        (repo, lock, msg)
    }

    /// Create `lock` with an mtime `age` in the past (std `File::set_modified`:
    /// every OS; the handle is opened for writing, which Windows requires).
    fn plant_lock(lock: &Path, age: Duration) {
        let f = std::fs::OpenOptions::new().create(true).truncate(true).write(true).open(lock).unwrap();
        f.set_modified(std::time::SystemTime::now() - age).unwrap();
        drop(f);
        let got = std::fs::metadata(lock).unwrap().modified().unwrap().elapsed().unwrap_or_default();
        assert!(got + Duration::from_secs(5) >= age, "mtime was not set back: {got:?} < {age:?}");
    }

    /// `path` as git prints it in an error on this OS (forward slashes on
    /// Windows; unchanged elsewhere).
    fn git_printed_path(path: &Path) -> String {
        let s = path.display().to_string();
        if cfg!(windows) {
            s.replace('\\', "/")
        } else {
            s
        }
    }

    /// W4R-03 (act): a STALE ref lock — older than one attempt — is
    /// deterministic: ONE spawn even on the Persistent ladder, and the error
    /// names the lock file and says it can be deleted.
    #[cfg(unix)]
    #[tokio::test]
    async fn real_path_stale_ref_lock_is_not_retried_and_named() {
        let (repo, lock, msg) = repo_with_ref_lock(Duration::from_secs(3600));
        let git = fake_git(&format!("printf '%s\\n' \"{}\" >&2; exit 1", msg.replace('"', "\\\"")));
        let fut = vct_launcher_core::paths::with_lookup_path(Some(git.path().as_os_str()), || {
            serialized_fetch_upstream(repo.path(), FetchPolicy::Persistent, None)
        });
        let err = tokio::time::timeout(Duration::from_secs(20), fut)
            .await
            .expect("a stale lock must not climb the Persistent ladder")
            .expect_err("still failing");
        assert_eq!(fake_git_calls(&git).len(), 1, "a stale lock must not be retried: {err}");
        assert!(err.contains(&lock.display().to_string()), "names the lock file: {err}");
        assert!(err.contains("stale lock") && err.contains("deleted"), "{err}");
    }

    /// R18-10 through the REAL fetch path: git's directory/file ref-conflict
    /// text makes ONE spawn even on the Persistent ladder, and the error the
    /// caller gets tells it to prune the remote.
    #[cfg(unix)]
    #[tokio::test]
    async fn real_path_ref_dir_file_conflict_is_not_retried_and_says_prune() {
        let msg = "error: cannot lock ref 'refs/remotes/vco_upstream/a/b': 'refs/remotes/vco_upstream/a' exists; \
                   cannot create 'refs/remotes/vco_upstream/a/b'\n \
                   ! [new branch]      a/b        -> vco_upstream/a/b  (unable to update local ref)";
        let git = fake_git(&format!("printf '%s\\n' \"{}\" >&2; exit 1", msg.replace('"', "\\\"")));
        let repo = tempfile::tempdir().unwrap();
        let fut = vct_launcher_core::paths::with_lookup_path(Some(git.path().as_os_str()), || {
            serialized_fetch_upstream(repo.path(), FetchPolicy::Persistent, None)
        });
        let err = tokio::time::timeout(Duration::from_secs(20), fut)
            .await
            .expect("a D/F ref conflict must not climb the Persistent ladder")
            .expect_err("still failing");
        assert_eq!(fake_git_calls(&git).len(), 1, "a D/F conflict must not be retried: {err}");
        assert!(err.contains("git remote prune vco_upstream"), "{err}");
    }

    /// W4R-03 (leave-alone): a FRESH ref lock is a moment's contention —
    /// still retried (Quick = two spawns).
    #[cfg(unix)]
    #[tokio::test]
    async fn real_path_fresh_ref_lock_is_retried() {
        let (repo, _lock, msg) = repo_with_ref_lock(Duration::ZERO);
        let git = fake_git(&format!("printf '%s\\n' \"{}\" >&2; exit 1", msg.replace('"', "\\\"")));
        let fut = vct_launcher_core::paths::with_lookup_path(Some(git.path().as_os_str()), || {
            serialized_fetch_upstream(repo.path(), FetchPolicy::Quick, None)
        });
        let err = fut.await.expect_err("still failing");
        assert_eq!(fake_git_calls(&git).len(), 2, "a fresh lock is retried: {err}");
        assert!(!err.contains("stale lock"), "{err}");
    }

    /// W4R-03: both message shapes resolve to the lock file on disk — the
    /// bare `cannot lock ref '<ref>'` form via the git dir — and a lock that
    /// cannot be stat'ed stays transient.
    #[test]
    fn ref_lock_path_is_resolved_from_either_message_shape() {
        let (repo, lock, msg) = repo_with_ref_lock(Duration::from_secs(3600));
        assert_eq!(ref_lock_path_from_stderr(repo.path(), &msg), Some(lock.clone()));
        let bare = "error: cannot lock ref 'refs/remotes/vco_upstream/main': reference already exists";
        assert_eq!(
            ref_lock_path_from_stderr(repo.path(), bare),
            Some(repo.path().join(".git").join("refs/remotes/vco_upstream/main.lock"))
        );
        let stale = reclassify_stale_ref_lock(repo.path(), bare, FetchAttemptError::transient("x"));
        assert!(!stale.transient, "the bare form finds the stale lock too");
        let gone = "error: cannot lock ref 'refs/remotes/vco_upstream/nope': x";
        assert!(reclassify_stale_ref_lock(repo.path(), gone, FetchAttemptError::transient("x")).transient);
        assert_eq!(ref_lock_path_from_stderr(repo.path(), "fatal: early EOF"), None);
    }

    /// W4R-03 / R18-14 (every OS): the decision the Persistent ladder acts on,
    /// from a failed attempt's stderr — `describe_failed_fetch` then the two
    /// reclassifiers, exactly as `run_fetch_attempt` chains them. A stale
    /// lock (git's absolute path, as printed on THIS OS) is deterministic and
    /// named; a fresh one stays transient.
    #[test]
    fn stale_and_fresh_ref_locks_are_classified_on_every_os() {
        let classify = |repo: &Path, msg: &str| {
            let err = describe_failed_fetch(&exit_status(1), msg);
            let err = classify_ref_dir_file_conflict(repo, msg, err);
            reclassify_stale_ref_lock(repo, msg, err)
        };
        let (repo, lock, msg) = repo_with_ref_lock(Duration::from_secs(3600));
        let stale = classify(repo.path(), &msg);
        assert!(!stale.transient, "a stale lock is deterministic: {}", stale.message);
        assert!(stale.message.contains(&lock.display().to_string()), "{}", stale.message);
        assert!(stale.message.contains("stale lock"), "{}", stale.message);

        let (repo, _lock, msg) = repo_with_ref_lock(Duration::ZERO);
        let fresh = classify(repo.path(), &msg);
        assert!(fresh.transient, "a fresh lock is a moment's contention: {}", fresh.message);
    }

    /// R18-14 (every OS): the lock path as git prints it resolves to the file
    /// — absolute with forward slashes (Windows' form), relative to the repo,
    /// and through a worktree's `gitdir:` file.
    #[test]
    fn ref_lock_path_resolves_absolute_relative_and_worktree_forms() {
        let (repo, lock, _msg) = repo_with_ref_lock(Duration::from_secs(3600));
        let fwd = format!("error: Unable to create '{}': File exists.", lock.display().to_string().replace('\\', "/"));
        assert_eq!(ref_lock_path_from_stderr(repo.path(), &fwd), Some(lock.clone()), "forward-slash absolute");

        let rel = "error: Unable to create '.git/refs/remotes/vco_upstream/main.lock': File exists.";
        let got = ref_lock_path_from_stderr(repo.path(), rel).expect("relative form");
        assert!(got.exists() && got.ends_with("main.lock"), "{got:?}");

        // A worktree: `<wt>/.git` is a FILE `gitdir: <abs git dir>`.
        let wt = tempfile::tempdir().unwrap();
        let gitdir = wt.path().join("real-gitdir");
        let dir = gitdir.join("refs").join("remotes").join("vco_upstream");
        std::fs::create_dir_all(&dir).unwrap();
        let wt_lock = dir.join("main.lock");
        plant_lock(&wt_lock, Duration::from_secs(3600));
        let checkout = wt.path().join("checkout");
        std::fs::create_dir_all(&checkout).unwrap();
        std::fs::write(checkout.join(".git"), format!("gitdir: {}\n", git_printed_path(&gitdir))).unwrap();
        let bare = "error: cannot lock ref 'refs/remotes/vco_upstream/main': reference already exists";
        assert_eq!(ref_lock_path_from_stderr(&checkout, bare), Some(gitdir.join("refs/remotes/vco_upstream/main.lock")));
        let stale = reclassify_stale_ref_lock(&checkout, bare, FetchAttemptError::transient("x"));
        assert!(!stale.transient, "the worktree's stale lock is found: {}", stale.message);
    }

    /// R18-10: git's exact directory/file ref-conflict text (git 2.43,
    /// captured from a real `git fetch` against a local repo whose branch
    /// `a` was renamed `a/b`, and the reverse) is DETERMINISTIC — even
    /// though it starts with `cannot lock ref` — and the error says to run
    /// `git remote prune <remote>`. ACT side.
    #[test]
    fn ref_dir_file_conflict_is_deterministic_and_names_the_prune() {
        let repo = tempfile::tempdir().unwrap();
        let shapes = [
            "error: cannot lock ref 'refs/remotes/vco_upstream/a/b': 'refs/remotes/vco_upstream/a' exists; \
             cannot create 'refs/remotes/vco_upstream/a/b'\nFrom https://example.invalid/r\n \
             ! [new branch]      a/b        -> vco_upstream/a/b  (unable to update local ref)\n",
            "error: cannot lock ref 'refs/remotes/vco_upstream/a': 'refs/remotes/vco_upstream/a/b' exists; \
             cannot create 'refs/remotes/vco_upstream/a'\nFrom https://example.invalid/r\n \
             ! [new branch]      a          -> vco_upstream/a  (unable to update local ref)\n",
            "error: cannot lock ref 'refs/remotes/vco_upstream/a': there is a non-empty directory \
             '.git/refs/remotes/vco_upstream/a' blocking reference 'refs/remotes/vco_upstream/a'\n",
            "error: cannot lock ref 'refs/remotes/vco_upstream/a/b': unable to resolve reference \
             'refs/remotes/vco_upstream/a/b': Not a directory\n",
        ];
        for msg in shapes {
            assert_eq!(ref_dir_file_conflict(msg).as_deref(), Some("vco_upstream"), "{msg}");
            let err = describe_failed_fetch(&exit_status(1), msg);
            let err = classify_ref_dir_file_conflict(repo.path(), msg, err);
            let err = reclassify_stale_ref_lock(repo.path(), msg, err);
            assert!(!err.transient, "a D/F ref conflict must not climb the ladder: {}", err.message);
            assert!(err.message.contains("git remote prune vco_upstream"), "{}", err.message);
            assert!(err.message.contains(&repo.path().display().to_string()), "{}", err.message);
        }
    }

    /// R18-10 LEAVE-ALONE: the lock-held shapes another running git causes
    /// stay transient and get no prune advice; an unrelated failure passes
    /// through `classify_ref_dir_file_conflict` untouched.
    #[test]
    fn ref_contention_stays_transient_and_other_failures_pass_through() {
        for msg in [
            "error: cannot lock ref 'refs/remotes/vco_upstream/main': Unable to create '/nonexistent/main.lock': File exists.",
            "error: cannot lock ref 'refs/remotes/vco_upstream/main': reference already exists",
            "error: cannot lock ref 'refs/remotes/vco_upstream/main': is at 1111111 but expected 2222222",
        ] {
            assert_eq!(ref_dir_file_conflict(msg), None, "{msg}");
            assert!(fetch_failure_is_transient(false, msg), "contention is retried: {msg}");
        }
        let repo = tempfile::tempdir().unwrap();
        let other = FetchAttemptError::transient("fatal: early EOF (git exit status: 128)");
        let out = classify_ref_dir_file_conflict(repo.path(), "fatal: early EOF", other.clone());
        assert_eq!(out, other);
        // A `cannot lock ref` git does not explain (a broken ref) is no
        // longer retried on the word "lock" alone.
        assert!(!fetch_failure_is_transient(
            false,
            "error: cannot lock ref 'refs/remotes/vco_upstream/x': unable to resolve reference 'refs/remotes/vco_upstream/x': reference broken"
        ));
    }

    /// Coalescing through the REAL fetch path: two callers arriving together
    /// spawn ONE git.
    #[cfg(unix)]
    #[tokio::test]
    async fn real_path_concurrent_callers_spawn_one_git() {
        let git = fake_git("sleep 0.3; exit 0");
        let repo = tempfile::tempdir().unwrap();
        let (a, b) = vct_launcher_core::paths::with_lookup_path(Some(git.path().as_os_str()), || {
            (
                serialized_fetch_upstream(repo.path(), FetchPolicy::Quick, None),
                serialized_fetch_upstream(repo.path(), FetchPolicy::Quick, Some("main")),
            )
        });
        let (a, b) = tokio::join!(a, b);
        assert!(a.is_ok() && b.is_ok(), "{a:?} / {b:?}");
        assert_eq!(fake_git_calls(&git).len(), 1, "the second caller coalesced");
    }

    /// I-05: the ladder's log lines name the REAL caller (`file:line` of the
    /// call), not a hard-coded `check_for_updates:` shared by every caller.
    #[cfg(unix)]
    #[tokio::test]
    async fn fetch_log_lines_name_the_real_caller() {
        let logs = Arc::new(std::sync::Mutex::new(Vec::new()));
        let _sub = tracing::subscriber::set_default(CaptureLogs(logs.clone()));
        let git = fake_git("exit 1");
        let repo = tempfile::tempdir().unwrap();
        let (fut, call_line) =
            vct_launcher_core::paths::with_lookup_path(Some(git.path().as_os_str()), || {
                (serialized_fetch_upstream(repo.path(), FetchPolicy::Quick, None), line!())
            });
        let _ = fut.await;
        let want = format!("git fetch ({}:{call_line})", file!());
        let logs = logs.lock().unwrap();
        assert!(
            logs.iter().any(|l| l.contains(&want) && l.contains("attempt 1/2 failed")),
            "no log line names the caller {want}: {logs:#?}"
        );
        assert!(
            !logs.iter().any(|l| l.contains("check_for_updates:")),
            "a shared hard-coded caller name is back: {logs:#?}"
        );
    }
}
