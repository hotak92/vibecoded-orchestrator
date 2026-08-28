// SPDX-License-Identifier: AGPL-3.0-or-later
//! v0.2.91 WP-A — the ONE home for "is the dist binary on disk the one HEAD
//! says it should be, and what do we do when it is not".
//!
//! ## Why this module exists (the field failure it closes)
//!
//! A Windows field tester ran a hand-frozen v0.2.88 `vct-launcher.exe` for a
//! month while source updates landed cleanly every time. Every fix shipped in
//! 0.2.89/0.2.90 was compiled INTO the component that was frozen, so the
//! update machinery could never deliver a fix to itself. Two root causes made
//! that state (a) reachable and (b) permanent:
//!
//! * **RC-1 (the producer)** — `revert_pre_pull_rename` was an unconditional
//!   `std::fs::rename(backup → canonical)`. On the
//!   autostash-pop-conflict-AFTER-merge-landed path the pull had ALREADY
//!   written the NEW binary to the canonical path (freed by the pre-pull
//!   rename); the abort tail then renamed the OLD running exe back over those
//!   new bytes. Source advanced, exe reverted, exe now git-dirty vs HEAD — and
//!   a dirty dist exe re-enters the same loop on every subsequent update.
//! * **RC-2 (the permanence)** — NO code path in the product ever re-examined
//!   "on-disk dist binary vs HEAD / vs the running version" outside the tail of
//!   a SUCCESSFUL pull. `update_orchestrator` early-returned on "Already up to
//!   date" BEFORE staging; `check_for_launcher_update` compared git SHAs only;
//!   boot recovery inspected lock files only; the self-update surface
//!   dead-ended at its clean-tree guard.
//!
//! This module is the single place where all of that is decided, so no call
//! site grows its own copy of the logic:
//!
//! | Concern | Entry point |
//! |---|---|
//! | pre-pull rename of the running binary (Windows) | [`pre_pull_rename_running_binary`] |
//! | non-clobbering revert of that rename (WI-3) | [`revert_pre_pull_rename`] + pure [`decide_revert`] |
//! | freshness probe (running ↔ metadata.json ↔ git status) | [`probe_freshness`] + pure [`decide_binary_freshness`] |
//! | stage `<target>.new` from `git show HEAD:<rel>` | [`stage_dirty_binaries`] / [`stage_locked_binaries_for_handoff`] |
//! | shared post-update staging + handoff tail (WI-4) | [`stage_and_handoff_after_update`] |
//! | at-rest reconcile: boot + update-check (WI-1/WI-2) | [`reconcile_dist_at_rest`] |
//! | arm/perform the no-relaunch swap at process exit | [`arm_stage1_swap_on_exit`] / [`perform_armed_swap_on_exit`] |
//! | observability conditions (WI-7) | [`emit_clobber_averted_condition`], [`emit_handoff_skipped_while_dirty`], [`emit_binary_stale_condition`] |
//!
//! ## Testability posture (the v0.2.90 `#[tokio::test]`-masking lesson)
//!
//! Every DECISION in here is a pure function over plain data
//! ([`decide_revert`], [`decide_binary_freshness`], [`canonical_path_for_backup`],
//! [`swap_candidate_rel_paths`]) and is unit-tested WITHOUT a tokio reactor, on
//! every host OS. The I/O wrappers around them are thin. The Windows-only
//! behaviour is confined to *gating* (which candidates, whether staging runs at
//! all) — the staging MECHANISM itself ([`stage_dirty_binaries`]) is
//! OS-agnostic and therefore exercised by the Linux/macOS test runs too, which
//! is the only way this code gets real coverage outside the Windows CI leg.
//!
//! ## Standing ruling honoured here: no auto-restart, no auto-quit
//!
//! The at-rest reconcile NEVER restarts or quits the launcher. It stages the
//! new bytes, arms a swap that runs when the user themselves quits, and says so
//! honestly through a durable `launcher_binary_stale` deferral. The swap armed
//! by [`arm_stage1_swap_on_exit`] deliberately writes `relaunch: None` into the
//! updater lock, so quitting means quitting.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use vct_launcher_core::paths::vct_root_dir;
use vct_launcher_core::process::CommandExt as _;

use crate::commands::installer::{
    launcher_binary_filename, launcher_dist_subdir, read_on_disk_binary_version,
    version_is_outdated,
};

// ---------------------------------------------------------------------------
// Pure decisions (no I/O — unit-tested on every host, reactor-free)
// ---------------------------------------------------------------------------

/// State of the canonical binary path at the moment an abort tail wants to
/// revert a pre-pull rename.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CanonicalState {
    /// Nothing at the canonical path — the pull wrote nothing there (the
    /// ordinary "pull failed / found nothing" case).
    Absent,
    /// A file is there and its bytes equal the backup's — reverting is a
    /// content no-op that merely restores the canonical NAME.
    Identical,
    /// A file is there whose bytes DIFFER from the backup: the pull landed a
    /// NEW binary. Renaming the backup over it is the RC-1 clobber.
    Differs,
    /// Could not read one of the two sides (I/O error). Treated as
    /// `Differs` by [`decide_revert`] — see its docs for why.
    Unknown,
}

/// What the abort tail should do with a pre-pull-rename backup.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RevertDecision {
    /// Rename the backup back onto the canonical path.
    RestoreBackup,
    /// Leave the canonical file alone (it holds newer bytes) and leave the
    /// backup parked at `<name>.old-<pid>` for the boot sweep to collect.
    KeepCanonicalParkBackup,
}

/// WI-3 — the pure revert/keep decision. THIS is the gate that stopped the
/// field install from ever refreshing.
///
/// Destructive-gate discipline: this function decides whether a
/// `REPLACE_EXISTING` rename runs over an existing file, so BOTH legs are
/// unit-tested (act + leave-alone).
///
/// `Unknown` (an unreadable side) maps to `KeepCanonicalParkBackup`: when we
/// cannot POSITIVELY confirm that the canonical file is the same content we
/// renamed aside, we must not overwrite it. False-keep costs the user one
/// `.old-<pid>` file that the boot sweep deletes; false-clobber costs them a
/// permanently frozen binary — which is the exact incident this closes.
pub(crate) fn decide_revert(state: CanonicalState) -> RevertDecision {
    match state {
        CanonicalState::Absent | CanonicalState::Identical => RevertDecision::RestoreBackup,
        CanonicalState::Differs | CanonicalState::Unknown => {
            RevertDecision::KeepCanonicalParkBackup
        }
    }
}

/// Recover the canonical path from a `<name>.old-<pid>` backup path.
///
/// Pure: no filesystem access. Returns `None` when `backup` does not carry the
/// `.old-<pid>` shape this module writes (never guess a canonical name).
pub(crate) fn canonical_path_for_backup(backup: &Path) -> Option<PathBuf> {
    let parent = backup.parent()?;
    let fname = backup.file_name().and_then(|s| s.to_str())?;
    let (stem, pid) = fname.rsplit_once(".old-")?;
    if stem.is_empty() || pid.is_empty() {
        return None;
    }
    Some(parent.join(stem))
}

/// Everything [`decide_binary_freshness`] needs, as plain data.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct FreshnessInputs {
    /// `CARGO_PKG_VERSION` of the RUNNING process.
    pub running_version: String,
    /// `launcher_version` from `launcher/dist/<arch>/<binary>.metadata.json`,
    /// when the sidecar exists and parses.
    pub on_disk_version: Option<String>,
    /// `git status --porcelain -- launcher/dist/<arch>/` produced output.
    pub dist_dirty: bool,
}

/// Why the on-disk dist state is considered stale.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum StaleReason {
    /// The dist sidecar declares a version NEWER than the running process —
    /// the update landed but the binary never reached the running slot.
    OnDiskNewerThanRunning,
    /// `launcher/dist/<arch>/` diverges from HEAD — git considers the bytes
    /// that SHOULD be on disk to not be on disk.
    DistDirtyVsHead,
    /// Both signals fire (WFT's exact state: hand-copied old exe + advanced
    /// source).
    Both,
}

/// Verdict of the at-rest freshness probe.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum FreshnessVerdict {
    /// Nothing to do.
    Fresh,
    /// The dist tree needs reconciling; `reason` says which signal(s) fired.
    Stale(StaleReason),
    /// v0.2.91 fix-round MAJOR-2(b): an update owns the tree right now, so the
    /// probe did not run at all. Deliberately NOT `Fresh` — we did not look, and
    /// claiming a clean bill of health we never established is the kind of
    /// dishonest state this whole module exists to remove.
    NotProbed,
}

/// What the AT-REST pass owes for a given verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum AtRestAction {
    /// Do nothing at all, silently.
    Nothing,
    /// Write the durable record, but stage NOTHING and arm NOTHING.
    SurfaceOnly,
    /// Stage `<target>.new` from HEAD, arm the exit swap, and record.
    StageAndArm,
}

/// v0.2.91 fix-round MINOR-3 — the pure at-rest action decision.
///
/// Arming is DESTRUCTIVE: the armed swap renames HEAD's blob over whatever sits
/// at the canonical path when the user next quits. So the destructive-gate rule
/// applies — both legs decided here, both legs unit-tested.
///
/// A `DistDirtyVsHead`-ONLY verdict — versions EQUAL, tree dirty — is exactly
/// what a compile-from-source developer looks like after `cargo build` drops a
/// fresh binary into the dist slot. Staging HEAD's blob and arming a swap there
/// destroys their build at the next quit, with no update running and nobody
/// having asked. install.py's WI-5 repair leg may act on dirty-alone because the
/// user invoked it; this pass runs at boot and on every poll, so it only
/// SURFACES (the durable `launcher_binary_stale` record still fires and names
/// the state honestly).
///
/// `OnDiskNewerThanRunning` is different in kind: the dist SIDECAR positively
/// declares a newer launcher than the one executing — the
/// update-landed-but-binary-never-swapped state the arming exists for. `Both` is
/// WFT's field state and carries that same positive signal.
pub(crate) fn decide_at_rest_action(verdict: &FreshnessVerdict) -> AtRestAction {
    match verdict {
        FreshnessVerdict::Fresh | FreshnessVerdict::NotProbed => AtRestAction::Nothing,
        FreshnessVerdict::Stale(StaleReason::DistDirtyVsHead) => AtRestAction::SurfaceOnly,
        FreshnessVerdict::Stale(
            StaleReason::OnDiskNewerThanRunning | StaleReason::Both,
        ) => AtRestAction::StageAndArm,
    }
}

/// WI-1 — the pure freshness decision.
///
/// Deliberately conservative: an ABSENT/unparseable sidecar contributes
/// nothing (we never infer staleness from missing metadata), and a running
/// version NEWER than the sidecar is `Fresh` (a developer running a local
/// cargo build against an older dist slot must not be nagged).
pub(crate) fn decide_binary_freshness(inputs: &FreshnessInputs) -> FreshnessVerdict {
    let on_disk_newer = inputs
        .on_disk_version
        .as_deref()
        .filter(|v| !v.is_empty())
        // version_is_outdated(a, b) == (a < b)
        .map(|on_disk| version_is_outdated(&inputs.running_version, on_disk))
        .unwrap_or(false);

    match (on_disk_newer, inputs.dist_dirty) {
        (true, true) => FreshnessVerdict::Stale(StaleReason::Both),
        (true, false) => FreshnessVerdict::Stale(StaleReason::OnDiskNewerThanRunning),
        (false, true) => FreshnessVerdict::Stale(StaleReason::DistDirtyVsHead),
        (false, false) => FreshnessVerdict::Fresh,
    }
}

/// The dist-relative paths this host's stage1 swap can act on, in swap order.
///
/// Pure (only reads the compile-time OS/arch resolvers), so a test can assert
/// the shape on any host. `vct-updater` itself is deliberately NOT a candidate:
/// it is the process performing the swap.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))] // Windows path + tests
pub(crate) fn swap_candidate_rel_paths() -> Vec<String> {
    let subdir = launcher_dist_subdir();
    let launcher = launcher_binary_filename();
    #[cfg(target_os = "windows")]
    let hub = "vct-hub.exe";
    #[cfg(not(target_os = "windows"))]
    let hub = "vct-hub";
    vec![
        format!("launcher/dist/{}/{}", subdir, launcher),
        format!("launcher/dist/{}/{}", subdir, hub),
    ]
}

/// `launcher/dist/<arch>/` — the pathspec the git status probe scopes to.
pub(crate) fn dist_dir_rel_path() -> String {
    format!("launcher/dist/{}/", launcher_dist_subdir())
}

/// Compute the `<path>.new` staging filename. For `foo.exe` → `foo.exe.new`.
///
/// Relocated here from `commands::installer` in v0.2.91 so the staging writer
/// and its callers share one definition. MUST stay byte-compatible with
/// `commands::update_handoff::with_new_suffix` (the reader side) — the
/// launcher writes where the updater looks.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))] // Windows path + tests
pub(crate) fn path_with_new_suffix(path: &Path) -> PathBuf {
    let parent = path.parent().unwrap_or_else(|| Path::new(""));
    let name = path.file_name().and_then(|s| s.to_str()).unwrap_or("");
    parent.join(format!("{}.new", name))
}

// ---------------------------------------------------------------------------
// I/O probes (thin wrappers over the pure decisions above)
// ---------------------------------------------------------------------------

/// SHA-256 of a file's bytes, or `None` when it cannot be read.
fn hash_file(path: &Path) -> Option<[u8; 32]> {
    use sha2::{Digest, Sha256};
    let bytes = std::fs::read(path).ok()?;
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    Some(hasher.finalize().into())
}

/// Classify the canonical path against a pre-pull-rename backup.
///
/// Size is compared first (cheap reject for the common "new binary is a
/// different size" case); equal-size files fall through to a content hash so a
/// same-size rebuild is still detected as `Differs`.
pub(crate) fn compare_backup_to_canonical(backup: &Path, canonical: &Path) -> CanonicalState {
    if !canonical.exists() {
        return CanonicalState::Absent;
    }
    let (bmeta, cmeta) = match (std::fs::metadata(backup), std::fs::metadata(canonical)) {
        (Ok(b), Ok(c)) => (b, c),
        _ => return CanonicalState::Unknown,
    };
    if bmeta.len() != cmeta.len() {
        return CanonicalState::Differs;
    }
    match (hash_file(backup), hash_file(canonical)) {
        (Some(b), Some(c)) if b == c => CanonicalState::Identical,
        (Some(_), Some(_)) => CanonicalState::Differs,
        _ => CanonicalState::Unknown,
    }
}

/// True when `git status --porcelain --untracked-files=no -- launcher/dist/<arch>/`
/// reports anything. Errors resolve to `false` (fail-SAFE: a git hiccup must not
/// manufacture a staleness claim we then nag the user about).
///
/// **`--untracked-files=no` is load-bearing (v0.2.91 fix-round MAJOR-1).**
/// Without it every `??` row counts as divergence, and the dist directory is
/// exactly where untracked debris accumulates: the user's own
/// `vct-launcher.old-may7` / `*.bak` copies, the `.old-<pid>` backups this very
/// module parks for the boot sweep, and the `<target>.new` siblings it stages.
/// A healthy install with any of those present reported
/// `Stale(DistDirtyVsHead)` on every boot — and the remediation the resulting
/// `launcher_binary_stale` deferral hands the user (`git checkout --
/// launcher/dist/`) cannot remove an untracked file, so the warning was
/// un-clearable by its own instructions (live-observed: 9 stray files on the
/// dogfood machine). Tracked divergence is the only thing that means "the bytes
/// git wrote are not the bytes on disk".
///
/// This also makes the Rust probe agree with its Python sibling
/// `vco_lib.dist_binary_repair.dist_dirty_paths`, which has always skipped
/// `??` rows — two legs of one delivery chain must classify identically.
pub(crate) async fn dist_is_dirty(install_path: &Path) -> bool {
    let rel = dist_dir_rel_path();
    match tokio::process::Command::new("git")
        .silent()
        .args([
            "status",
            "--porcelain",
            "--untracked-files=no",
            "--",
            rel.as_str(),
        ])
        .current_dir(install_path)
        .output()
        .await
    {
        Ok(o) if o.status.success() => !o.stdout.is_empty(),
        Ok(o) => {
            tracing::warn!(
                "[vct] binary_freshness: git status for {} exited {:?}; treating dist as clean. \
                 stderr: {}",
                rel,
                o.status.code(),
                String::from_utf8_lossy(&o.stderr).trim(),
            );
            false
        }
        Err(e) => {
            tracing::warn!(
                "[vct] binary_freshness: git status for {} could not spawn ({}); treating dist \
                 as clean",
                rel, e,
            );
            false
        }
    }
}

/// Gather [`FreshnessInputs`] from disk + git for `install_path`.
pub(crate) async fn probe_freshness(install_path: &Path) -> FreshnessInputs {
    FreshnessInputs {
        running_version: env!("CARGO_PKG_VERSION").to_string(),
        on_disk_version: read_on_disk_binary_version(install_path),
        dist_dirty: dist_is_dirty(install_path).await,
    }
}

// ---------------------------------------------------------------------------
// Pre-pull rename + non-clobbering revert (relocated from installer.rs)
// ---------------------------------------------------------------------------

/// v0.2.17 (plan 0.0.B): pre-pull rename helper for Windows. Relocated to this
/// module in v0.2.91 so BOTH update surfaces (`installer::update_orchestrator`
/// and `self_update::apply_launcher_update`) call one implementation.
///
/// On Windows, `git pull` fails with ERROR_SHARING_VIOLATION when it tries to
/// overwrite the running launcher's binary. Windows DOES allow renaming a
/// running process's own `.exe` (Chrome, VS Code and npm-on-Windows all rely on
/// this), so we move ourselves aside first and leave the canonical path free
/// for git to write into.
///
/// Linux / macOS skip this — both kernels handle running-binary overwrite via
/// inode/vnode ref-counting.
///
/// Returns the backup path on Windows when the rename happened, else `None`
/// (non-Windows, running outside the install tree, or a soft rename failure).
#[cfg(windows)]
pub(crate) fn pre_pull_rename_running_binary(install_path: &Path) -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let exe_canon = dunce::canonicalize(&exe).unwrap_or(exe);
    let install_canon =
        dunce::canonicalize(install_path).unwrap_or_else(|_| install_path.to_path_buf());
    if !exe_canon.starts_with(&install_canon) {
        // Running from outside the install tree (e.g. a cargo dev build) —
        // git pull won't try to overwrite us.
        return None;
    }

    let pid = std::process::id();
    let backup_name = format!("{}.old-{}", exe_canon.file_name()?.to_string_lossy(), pid);
    let backup_path = exe_canon.parent()?.join(backup_name);

    match std::fs::rename(&exe_canon, &backup_path) {
        Ok(()) => {
            tracing::info!(
                "[vct] binary_freshness: pre-pull renamed running launcher binary to {} \
                 (Windows). New binary will be written to {} by git pull.",
                backup_path.display(),
                exe_canon.display(),
            );
            Some(backup_path)
        }
        Err(e) => {
            tracing::warn!(
                "[vct] binary_freshness: pre-pull rename FAILED ({}). git pull will likely \
                 fail with ERROR_SHARING_VIOLATION or silently skip the binary. Continuing — \
                 the at-rest reconcile heals the skipped-binary case.",
                e,
            );
            None
        }
    }
}

#[cfg(not(windows))]
pub(crate) fn pre_pull_rename_running_binary(_install_path: &Path) -> Option<PathBuf> {
    // POSIX kernels handle running-binary overwrite cleanly via inode
    // ref-counting. No rename needed.
    None
}

/// Outcome of one [`revert_pre_pull_rename`] call. Callers use this to decide
/// whether the WI-7 observability condition + a staging pass are owed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RevertOutcome {
    /// The backup was renamed back onto the canonical path.
    Reverted,
    /// WI-3: the canonical path held DIFFERENT (newer) bytes. They were kept;
    /// the backup stays parked as `<name>.old-<pid>` for the boot sweep.
    ClobberAverted,
    /// The rename was attempted and failed (logged; caller continues).
    Failed,
    /// `backup` did not carry the `.old-<pid>` shape — nothing was touched.
    NotABackupPath,
}

/// v0.2.17: revert the pre-pull rename on a git-pull failure / abort.
/// v0.2.91 WI-3: **non-clobbering**.
///
/// Pre-v0.2.91 this was an unconditional `std::fs::rename(backup → canonical)`
/// — on Windows a `MoveFileExW(..., REPLACE_EXISTING)`. On the
/// autostash-pop-conflict-after-merge-landed path the merge HAS landed and the
/// pull already wrote the NEW binary at the canonical path, so the "revert"
/// silently restored the OLD exe over it: source advanced, exe reverted, exe
/// left git-dirty. That is RC-1 — the producer of a permanently frozen
/// launcher.
///
/// Now the two files are compared first ([`compare_backup_to_canonical`]) and
/// the action is chosen by the pure [`decide_revert`]. Best-effort throughout:
/// every failure is logged and swallowed, because the caller is already on an
/// error path and must surface ITS error, not ours.
pub(crate) fn revert_pre_pull_rename(backup_path: &Path) -> RevertOutcome {
    let canonical_path = match canonical_path_for_backup(backup_path) {
        Some(p) => p,
        None => return RevertOutcome::NotABackupPath,
    };

    let state = compare_backup_to_canonical(backup_path, &canonical_path);
    match decide_revert(state) {
        RevertDecision::KeepCanonicalParkBackup => {
            tracing::info!(
                "[vct] binary_freshness: NOT reverting pre-pull rename — {} holds bytes that \
                 differ from the backup {} (state={:?}). The pull landed a NEWER binary there; \
                 restoring the backup over it would freeze this install on the old binary \
                 (v0.2.91 WI-3). Keeping the new bytes; the backup stays parked for the boot \
                 sweep.",
                canonical_path.display(),
                backup_path.display(),
                state,
            );
            RevertOutcome::ClobberAverted
        }
        RevertDecision::RestoreBackup => match std::fs::rename(backup_path, &canonical_path) {
            Ok(()) => {
                tracing::info!(
                    "[vct] binary_freshness: reverted pre-pull rename ({} → {})",
                    backup_path.display(),
                    canonical_path.display(),
                );
                RevertOutcome::Reverted
            }
            Err(e) => {
                tracing::warn!(
                    "[vct] binary_freshness: could not revert pre-pull rename ({} → {}): {}. \
                     The renamed file is left in place; the running launcher continues to work \
                     and the boot sweep collects the `.old-<pid>` sibling once this PID exits.",
                    backup_path.display(),
                    canonical_path.display(),
                    e,
                );
                RevertOutcome::Failed
            }
        },
    }
}

/// v0.2.91 fix-round MINOR-1 — revert, then RECORD what the revert decided.
///
/// [`revert_pre_pull_rename`] returns a [`RevertOutcome`] precisely so the abort
/// tail can react to `ClobberAverted`; a caller that throws it away
/// (`let _ = revert_pre_pull_rename(b);`) reintroduces the silence WI-7 exists
/// to end — the launcher self-update surface's abort paths did exactly that, so
/// an averted clobber there produced no deferral, no audit row and no log the
/// user could find.
///
/// Emission is injected so the decision (`did we check the outcome?`) is
/// testable without spawning the python deferral helper; the production wrapper
/// [`revert_and_record`] supplies [`emit_clobber_averted_condition`].
pub(crate) fn revert_and_record_with(
    backup: &Path,
    emit: &mut dyn FnMut(&Path, &Path),
) -> RevertOutcome {
    let outcome = revert_pre_pull_rename(backup);
    if outcome == RevertOutcome::ClobberAverted {
        if let Some(canonical) = canonical_path_for_backup(backup) {
            emit(backup, &canonical);
        }
    }
    outcome
}

/// Production wiring of [`revert_and_record_with`]: writes the durable
/// `launcher_binary_clobber_averted` record into `install_path`. Returns the
/// outcome so a caller that ALSO owns an audit row (the update surfaces do) can
/// write it.
pub(crate) fn revert_and_record(install_path: &Path, backup: &Path) -> RevertOutcome {
    revert_and_record_with(backup, &mut |b, c| {
        emit_clobber_averted_condition(install_path, b, c)
    })
}

// ---------------------------------------------------------------------------
// Staging (`<target>.new` from `git show HEAD:<rel>`)
// ---------------------------------------------------------------------------

/// v0.2.52 V52-AH staging, relocated + generalised in v0.2.91.
///
/// For each candidate relative path that `git status --porcelain` reports as
/// dirty, extract HEAD's blob into `<target>.new`. `vct-updater` renames those
/// siblings onto the canonical paths once the launcher PID exits.
///
/// **OS-agnostic on purpose**: the mechanism is git + a file write, so the
/// Linux/macOS test runs exercise the real code path. Whether staging runs AT
/// ALL in production is decided by the callers / by
/// [`stage_locked_binaries_for_handoff`].
///
/// `candidates` are repo-relative, forward-slashed paths. Callers pass
/// [`swap_candidate_rel_paths`]; tests pass their own so no fixture has to
/// mimic the host's dist layout.
///
/// Returns the relative paths that were successfully staged. Soft-fail
/// per-candidate: a git or I/O failure skips that file and continues.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))] // Windows path + tests
pub(crate) async fn stage_dirty_binaries(
    install_path: &Path,
    candidates: &[String],
) -> Vec<String> {
    let mut staged: Vec<String> = Vec::new();
    for rel_path in candidates {
        let status_out = match tokio::process::Command::new("git")
            .silent()
            .args(["status", "--porcelain", "--", rel_path.as_str()])
            .current_dir(install_path)
            .output()
            .await
        {
            Ok(o) => o,
            Err(e) => {
                tracing::warn!(
                    "[vct] binary_freshness: git status failed for {}: {} (skipping; the \
                     handoff gracefully no-ops for this file)",
                    rel_path, e
                );
                continue;
            }
        };
        if status_out.stdout.is_empty() {
            // Clean — the binary already matches HEAD. Nothing to stage.
            continue;
        }

        let target_abs = install_path.join(rel_path);
        let staged_abs = path_with_new_suffix(&target_abs);

        let show_out = match tokio::process::Command::new("git")
            .silent()
            .args(["show", &format!("HEAD:{}", rel_path)])
            .current_dir(install_path)
            .output()
            .await
        {
            Ok(o) if o.status.success() => o,
            Ok(o) => {
                tracing::warn!(
                    "[vct] binary_freshness: git show HEAD:{} exited non-zero ({:?}); skipping. \
                     stderr: {}",
                    rel_path,
                    o.status.code(),
                    String::from_utf8_lossy(&o.stderr).trim(),
                );
                continue;
            }
            Err(e) => {
                tracing::warn!(
                    "[vct] binary_freshness: git show HEAD:{} spawn failed: {} (skipping)",
                    rel_path, e
                );
                continue;
            }
        };

        // Ensure the parent exists (a dist dir can be missing entirely on a
        // clone whose binaries were never checked out).
        if let Some(parent) = staged_abs.parent() {
            if let Err(e) = std::fs::create_dir_all(parent) {
                tracing::warn!(
                    "[vct] binary_freshness: mkdir {} failed: {} (skipping {})",
                    parent.display(),
                    e,
                    rel_path,
                );
                continue;
            }
        }

        // Write atomically (write to `.tmp`, rename onto `.new`) so a killed
        // launcher can never leave a truncated `.new` for the updater to
        // rename over a working binary.
        let tmp_path = staged_abs.with_extension("new.tmp");
        if let Err(e) = std::fs::write(&tmp_path, &show_out.stdout) {
            tracing::warn!(
                "[vct] binary_freshness: write {} failed: {} (skipping)",
                tmp_path.display(),
                e,
            );
            continue;
        }
        if let Err(e) = std::fs::rename(&tmp_path, &staged_abs) {
            tracing::warn!(
                "[vct] binary_freshness: rename {} → {} failed: {} (skipping)",
                tmp_path.display(),
                staged_abs.display(),
                e,
            );
            let _ = std::fs::remove_file(&tmp_path);
            continue;
        }
        tracing::info!(
            "[vct] binary_freshness: staged {} → {} ({} bytes)",
            rel_path,
            staged_abs.display(),
            show_out.stdout.len(),
        );
        staged.push(rel_path.clone());
    }
    staged
}

// ---------------------------------------------------------------------------
// Stale `<target>.new` invalidation (v0.2.91 fix-round MAJOR-2)
// ---------------------------------------------------------------------------
//
// WI-1 made `.new` files LONG-LIVED for the first time. Before v0.2.91 they
// only ever existed for the few seconds between `finalize_update_and_restart`
// staging them and `vct-updater` consuming them. Now the boot/check-time
// at-rest reconcile can stage one and leave it on disk for the whole session,
// which resurrects the very clobber WI-3 closed, by a different route:
//
//   1. boot stages `<launcher>.new` from HEAD-at-boot;
//   2. LATER IN THE SAME SESSION a real update pulls newer binaries — the
//      canonical path now holds the NEW bytes and is CLEAN vs the new HEAD;
//   3. the post-update handoff (`prepare_windows_update_handoff`) swaps ANY
//      existing `<target>.new` with no freshness check of its own, so
//      `vct-updater` renames step 1's OLD bytes over the freshly-pulled binary.
//
// The invariant that closes it: **a `.new` sibling of a candidate whose
// canonical file is CLEAN vs HEAD is stale BY DEFINITION.** Staging only ever
// happens for a DIRTY candidate, so "canonical == HEAD" means either the file
// was repaired or a pull landed HEAD's bytes there; either way the staged copy
// has nothing left to contribute and can only do harm.
//
// Conservative on uncertainty: an unreadable / erroring git probe yields
// `None` and NOTHING is deleted (a false-keep costs one stale file the next
// pass re-evaluates; a false-delete throws away the only copy of bytes a
// locked-file install is waiting on).

/// Is `rel_path` CLEAN vs HEAD? `None` = could not tell (never act on it).
///
/// Same probe shape as [`dist_is_dirty`], scoped to one file and with the same
/// `--untracked-files=no` posture (MAJOR-1) so the two never disagree.
async fn probe_path_clean_vs_head(install_path: &Path, rel_path: &str) -> Option<bool> {
    let out = tokio::process::Command::new("git")
        .silent()
        .args([
            "status",
            "--porcelain",
            "--untracked-files=no",
            "--",
            rel_path,
        ])
        .current_dir(install_path)
        .output()
        .await
        .ok()?;
    if !out.status.success() {
        return None;
    }
    Some(out.stdout.is_empty())
}

/// Blocking sibling of [`probe_path_clean_vs_head`] for the process-EXIT path,
/// which runs inside Tauri's `RunEvent::Exit` handler (a sync context, and the
/// v0.2.90 lesson says do not conjure a reactor there). One local `git status`
/// per candidate — instant, and bounded by the two-entry candidate list.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))] // Windows path + tests
fn probe_path_clean_vs_head_blocking(install_path: &Path, rel_path: &str) -> Option<bool> {
    let out = std::process::Command::new("git")
        .silent()
        .args([
            "status",
            "--porcelain",
            "--untracked-files=no",
            "--",
            rel_path,
        ])
        .current_dir(install_path)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    Some(out.stdout.is_empty())
}

/// Delete the `<candidate>.new` sibling of every candidate whose canonical file
/// is CLEAN vs HEAD.
///
/// Takes already-probed verdicts so the async and blocking call sites share ONE
/// decision body (and so a test can drive the decision with no git repo at
/// all). `verdicts` is `(repo-relative candidate path, clean_vs_head)`;
/// `None` means "unknown" and is a leave-alone.
///
/// Returns the repo-relative candidate paths whose `.new` sibling was removed.
pub(crate) fn invalidate_stale_new_siblings(
    install_path: &Path,
    verdicts: &[(String, Option<bool>)],
) -> Vec<String> {
    let mut dropped: Vec<String> = Vec::new();
    for (rel, clean) in verdicts {
        if *clean != Some(true) {
            // Dirty (its `.new` may be the fix that is waiting) or unknown
            // (never act on uncertainty). Leave it alone.
            continue;
        }
        let staged = path_with_new_suffix(&install_path.join(rel));
        if !staged.is_file() {
            continue;
        }
        match std::fs::remove_file(&staged) {
            Ok(()) => {
                tracing::warn!(
                    "[vct] binary_freshness: dropped STALE staged binary {} — its canonical \
                     path {} already matches HEAD, so swapping the staged copy in would \
                     rename OLDER bytes over the current one (v0.2.91 MAJOR-2)",
                    staged.display(),
                    rel,
                );
                dropped.push(rel.clone());
            }
            Err(e) => tracing::warn!(
                "[vct] binary_freshness: could not remove stale staged binary {}: {} \
                 (leaving it; the swap paths re-evaluate it next pass)",
                staged.display(),
                e,
            ),
        }
    }
    dropped
}

/// Probe + invalidate, async. Used by the shared post-update tail.
///
/// OS-agnostic on purpose (same posture as [`stage_dirty_binaries`]): POSIX
/// never stages a `.new`, so this is a no-op there in production while still
/// giving the mechanism real coverage on the Linux/macOS test runs.
pub(crate) async fn invalidate_stale_new_siblings_for(
    install_path: &Path,
    candidates: &[String],
) -> Vec<String> {
    let mut verdicts: Vec<(String, Option<bool>)> = Vec::with_capacity(candidates.len());
    for rel in candidates {
        verdicts.push((
            rel.clone(),
            probe_path_clean_vs_head(install_path, rel).await,
        ));
    }
    invalidate_stale_new_siblings(install_path, &verdicts)
}

/// Probe + invalidate, blocking. Used by the process-exit swap path.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))] // Windows path + tests
pub(crate) fn invalidate_stale_new_siblings_for_blocking(
    install_path: &Path,
    candidates: &[String],
) -> Vec<String> {
    let verdicts: Vec<(String, Option<bool>)> = candidates
        .iter()
        .map(|rel| {
            (
                rel.clone(),
                probe_path_clean_vs_head_blocking(install_path, rel),
            )
        })
        .collect();
    invalidate_stale_new_siblings(install_path, &verdicts)
}

/// Windows: stage every dirty dist binary for the stage1 handoff.
/// POSIX: no-op (inode ref-counting + the rename pattern already handle binary
/// overwrite, and there is no `vct-updater` to consume a `.new` sibling).
pub(crate) async fn stage_locked_binaries_for_handoff(install_path: &Path) -> Vec<String> {
    #[cfg(target_os = "windows")]
    {
        stage_dirty_binaries(install_path, &swap_candidate_rel_paths()).await
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = install_path;
        Vec::new()
    }
}

// ---------------------------------------------------------------------------
// Shared post-update tail (WI-4: one home for BOTH update surfaces)
// ---------------------------------------------------------------------------

/// Result of [`stage_and_handoff_after_update`].
#[derive(Debug, Clone, Default)]
pub(crate) struct HandoffTail {
    /// Relative paths staged as `<target>.new` this pass. Read by the WI-7
    /// emitter below and by callers that log the tail's decision; kept on the
    /// struct so a future caller does not re-derive it with a second git pass.
    #[allow(dead_code)]
    pub staged: Vec<String>,
    /// True iff `vct-updater` was spawned and the caller MUST exit now.
    pub handoff_active: bool,
    /// Absolute path of the updater lock file (diagnostics / forensics).
    pub lock_path: Option<PathBuf>,
    /// Why the handoff did not fire (when `handoff_active == false`).
    pub skip_reason: Option<String>,
}

/// The shared "we just pulled; make sure the binaries actually land" tail.
///
/// v0.2.91 WI-4: `installer::finalize_update_and_restart` and
/// `self_update::finish_apply_after_pull` both call THIS instead of carrying
/// their own copy (Surface B previously had no staging and no handoff at all,
/// so on Windows it relaunched the same stale exe by construction).
///
/// Uses the RELAUNCHING handoff (`prepare_windows_update_handoff`): the user
/// clicked Update, so a restart is expected and consented-to. The at-rest path
/// ([`arm_stage1_swap_on_exit`]) is the non-relaunching sibling.
///
/// Soft-fail: on any failure the caller falls through to its legacy restart
/// path, which is exactly the pre-v0.2.52 behaviour.
pub(crate) async fn stage_and_handoff_after_update(
    install_path: &Path,
    install_path_string: &str,
) -> HandoffTail {
    // v0.2.91 fix-round MAJOR-2(a): drop any LEFTOVER `<target>.new` whose
    // canonical file already matches HEAD before we stage or hand off. A `.new`
    // staged earlier in this session (WI-1's at-rest reconcile makes those
    // long-lived for the first time) carries the PRE-pull bytes; the pull we
    // just finished put the post-pull bytes at the canonical path, and
    // `prepare_windows_update_handoff` swaps ANY existing `.new` with no
    // freshness check of its own — so without this the updater would rename the
    // OLD binary over the fresh one. Clean canonical ⇒ the staged copy is stale
    // by definition. Runs BEFORE staging so a still-dirty candidate immediately
    // gets a fresh `.new` written below.
    let dropped = invalidate_stale_new_siblings_for(install_path, &swap_candidate_rel_paths()).await;
    if !dropped.is_empty() {
        tracing::warn!(
            "[vct] binary_freshness: dropped {} stale staged binary/binaries before the \
             handoff: {:?}",
            dropped.len(),
            dropped,
        );
    }

    let staged = stage_locked_binaries_for_handoff(install_path).await;
    if !staged.is_empty() {
        tracing::info!(
            "[vct] binary_freshness: staged {} binary/binaries for handoff: {:?}",
            staged.len(),
            staged,
        );
    }

    let handoff_result = match crate::commands::update_handoff::prepare_windows_update_handoff(
        install_path_string.to_string(),
    )
    .await
    {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!(
                "[vct] binary_freshness: handoff returned error ({}); falling through to the \
                 caller's legacy restart path",
                e,
            );
            crate::commands::update_handoff::HandoffResult::default()
        }
    };

    // WI-7 (b): a handoff that is skipped while a dist binary is git-dirty is
    // the silent state that let the field install freeze. Record it durably.
    if !handoff_result.handoff_active && !staged.is_empty() {
        emit_handoff_skipped_while_dirty(
            install_path,
            &staged,
            handoff_result.skip_reason.as_deref().unwrap_or("unknown"),
        );
    }

    HandoffTail {
        staged,
        handoff_active: handoff_result.handoff_active,
        lock_path: handoff_result.lock_path,
        skip_reason: handoff_result.skip_reason,
    }
}

// ---------------------------------------------------------------------------
// At-rest reconcile (WI-1 / WI-2)
// ---------------------------------------------------------------------------

/// Outcome of [`reconcile_dist_at_rest`].
#[derive(Debug, Clone)]
pub(crate) struct ReconcileOutcome {
    pub verdict: FreshnessVerdict,
    /// The three probed values behind `verdict` (running / on-disk / dirty).
    /// Carried so a caller can render the diagnosis without re-probing.
    #[allow(dead_code)]
    pub inputs: FreshnessInputs,
    /// Relative paths staged as `<target>.new` (Windows only).
    pub staged: Vec<String>,
    /// True iff a no-relaunch stage1 swap is now armed for process exit.
    pub armed: bool,
    /// v0.2.91 fix-round MAJOR-2(b): an update owns the tree, so this pass did
    /// nothing at all — no probe, no staging, no arming, no record.
    pub stood_down: bool,
}

impl ReconcileOutcome {
    /// Only a POSITIVE stale verdict is stale. `NotProbed` must never read as
    /// stale (we did not look) and must never read as fresh either — callers
    /// that care about the difference read `stood_down`.
    pub fn is_stale(&self) -> bool {
        matches!(self.verdict, FreshnessVerdict::Stale(_))
    }
}

/// Is an orchestrator update or a stage1 handoff currently in charge of the
/// tree? Path-injectable core so both legs are unit-testable without mutating
/// the process-wide `VCT_STATE_DIR`.
///
/// * `gate_lock` — `<vct_root>/.update-in-progress.json`, the RAII lockfile
///   `update_orchestrator` arms (installer.rs, `UpdateInProgressGuard::new()`)
///   before the pull and drops after install.py. Read through the EXISTING
///   `update_gate::skip_if_update_in_progress_at` so this stand-down inherits
///   its deadline-based self-healing (a crashed update cannot wedge the
///   reconcile forever) and logs in the same voice as every other poller that
///   stands down for an update.
/// * `handoff_lock` — `<vct_root>/update.lock.json`, written when a stage1
///   `vct-updater` handoff has been armed. Its swap list + relaunch semantics
///   own the binaries until the updater consumes it.
///
/// **Our OWN update is not a stand-down** (`started_by_pid == this pid`). WI-2's
/// whole point is that `update_orchestrator`'s "Already up to date" early
/// returns still reconcile the binary — and those returns happen INSIDE the
/// window where this process holds the gate. A pid-blind check would stand
/// WI-2 down against itself and quietly restore the RC-2 dead end this release
/// exists to close. What must be stood down is an update we do NOT drive: a
/// terminal `python install.py --update` (whose own WI-5 repair leg is
/// rewriting the same dist files) or a second launcher process.
pub(crate) fn update_owns_the_tree_at(gate_lock: &Path, handoff_lock: &Path) -> bool {
    if handoff_lock.exists() {
        tracing::info!(
            "[vct] binary_freshness: standing down — a stage1 update handoff lock is present \
             at {} (its swap list owns the dist binaries)",
            handoff_lock.display(),
        );
        return true;
    }
    if !crate::commands::update_gate::skip_if_update_in_progress_at(
        "binary_freshness_reconcile",
        gate_lock,
    ) {
        return false;
    }
    match crate::commands::update_gate::read_lockfile_at(gate_lock) {
        Some(p) if p.started_by_pid == std::process::id() => {
            tracing::warn!(
                "[vct] binary_freshness: the in-progress update is OURS (pid {}) — reconciling \
                 anyway; this is the WI-2 'Already up to date still heals' path",
                p.started_by_pid,
            );
            false
        }
        _ => true,
    }
}

/// Production wiring of [`update_owns_the_tree_at`] against `~/.vct/`.
pub(crate) fn update_owns_the_tree() -> bool {
    let root = vct_root_dir();
    update_owns_the_tree_at(
        &root.join(crate::commands::update_gate::LOCKFILE_BASENAME),
        &root.join(crate::commands::update_handoff::UPDATE_LOCK_FILE),
    )
}

/// WI-1 — reconcile the dist binaries **at rest**: no successful pull required,
/// no update in flight.
///
/// Called from (a) launcher boot, next to the update-lock recovery probe, and
/// (b) the update-check command — the two moments where the product previously
/// looked at git SHAs and lock files but never at the binary actually on disk.
/// [`reconcile_dist_at_rest`] is also the "Already up to date still heals" body
/// (WI-2), invoked from the early-return branches of the update commands.
///
/// Actions, in order:
///  0. Stand down entirely when an update owns the tree (MAJOR-2(b)).
///  1. Probe (running version ↔ dist sidecar ↔ `git status -- launcher/dist/<arch>/`).
///  2. Fresh → return, silently. This is the overwhelmingly common case and it
///     must stay free of side effects and of user-facing noise.
///  3. Stale **with the `on_disk_newer` signal** → on Windows, stage
///     `<target>.new` from HEAD and ARM a no-relaunch swap for the next quit.
///     On POSIX, stage nothing (the kernel makes a plain `git checkout --`
///     sufficient, and that is the user-invoked `install.py --update` leg, not
///     something we do behind their back). Stale from a DIRTY tree ALONE is
///     surface-only — see [`at_rest_may_stage`] (MINOR-3).
///  4. Emit the durable `launcher_binary_stale` condition naming all three
///     versions and the ONE manual action.
///
/// **Never restarts and never quits.** Standing ruling.
pub(crate) async fn reconcile_dist_at_rest(install_path: &Path) -> ReconcileOutcome {
    reconcile_dist_at_rest_gated(install_path, update_owns_the_tree()).await
}

/// Gate-injectable core of [`reconcile_dist_at_rest`].
///
/// `update_owns_tree` comes from [`update_owns_the_tree`] in production and is
/// supplied directly by tests, so the stand-down leg is provable without
/// mutating the process-wide `VCT_STATE_DIR` under test parallelism.
///
/// Why standing down matters (MAJOR-2(b)): this pass runs at boot AND on every
/// update-check poll, and a poll can land in the middle of a real update. If it
/// staged then, it would write `<target>.new` files from the PRE-pull HEAD into
/// the exact directory the running update is about to fill with post-pull bytes
/// — and the update's own handoff would then swap the older staged copy in. An
/// update already owns the whole delivery chain end to end; this pass exists
/// only for the at-REST case, so when the tree is not at rest it does nothing.
pub(crate) async fn reconcile_dist_at_rest_gated(
    install_path: &Path,
    update_owns_tree: bool,
) -> ReconcileOutcome {
    if update_owns_tree {
        tracing::info!(
            "[vct] binary_freshness: at-rest reconcile stands down for {} — an update owns the \
             tree (it drives its own staging + handoff)",
            install_path.display(),
        );
        return ReconcileOutcome {
            verdict: FreshnessVerdict::NotProbed,
            inputs: FreshnessInputs {
                running_version: env!("CARGO_PKG_VERSION").to_string(),
                on_disk_version: None,
                dist_dirty: false,
            },
            staged: Vec::new(),
            armed: false,
            stood_down: true,
        };
    }

    let inputs = probe_freshness(install_path).await;
    let verdict = decide_binary_freshness(&inputs);
    let action = decide_at_rest_action(&verdict);

    if action == AtRestAction::Nothing {
        // v0.2.91 WP-B registry pairing: `launcher_binary_stale` is
        // `action_required` with a PROBE-DRIVEN clear, and THIS is the probe.
        // Python's leg cannot see the running process's version, so without
        // this call the record could only be retired by a weaker heuristic.
        clear_stale_condition_if_fresh(install_path, &verdict);
        // Its sibling record — "binaries staged, handoff never fired" — is
        // over once the tree matches HEAD again and no `.new` sibling is left
        // waiting. This branch is the only safe place to ask: we probed, and
        // this pass stages nothing, so the `.new` reading is not our own.
        clear_handoff_skipped_if_delivered(install_path, &verdict, inputs.dist_dirty);
        return ReconcileOutcome {
            verdict,
            inputs,
            staged: Vec::new(),
            armed: false,
            stood_down: false,
        };
    }

    tracing::warn!(
        "[vct] binary_freshness: dist binaries are STALE at rest (running v{}, dist sidecar v{}, \
         dist dirty vs HEAD={}) — verdict {:?}",
        inputs.running_version,
        inputs.on_disk_version.as_deref().unwrap_or("<unknown>"),
        inputs.dist_dirty,
        verdict,
    );

    // MINOR-3: dirty-alone surfaces but never stages/arms — see
    // `decide_at_rest_action`.
    let (staged, armed) = if action == AtRestAction::StageAndArm {
        let staged = stage_locked_binaries_for_handoff(install_path).await;
        let armed = if staged.is_empty() {
            false
        } else {
            arm_stage1_swap_on_exit(install_path)
        };
        (staged, armed)
    } else {
        tracing::info!(
            "[vct] binary_freshness: dist tree diverges from HEAD but the versions MATCH — \
             surfacing only. Nothing is staged and no swap is armed: an equal-version dirty \
             dist slot is what a local `cargo build` looks like, and overwriting it from HEAD \
             behind the user's back would destroy their build (v0.2.91 MINOR-3). \
             `python install.py --update` repairs it on request.",
        );
        (Vec::new(), false)
    };

    emit_binary_stale_condition(install_path, &inputs, &staged, armed);

    ReconcileOutcome {
        verdict,
        inputs,
        staged,
        armed,
        stood_down: false,
    }
}

// ---------------------------------------------------------------------------
// Arm / perform the NO-RELAUNCH stage1 swap at process exit
// ---------------------------------------------------------------------------

/// Install root whose staged `.new` siblings should be swapped when this
/// process exits. `None` = nothing armed.
static ARMED_SWAP_ROOT: Mutex<Option<PathBuf>> = Mutex::new(None);

/// One `launcher_binary_stale` emit per process — the at-rest reconcile runs on
/// boot AND on every update-check poll, and the condition is last-write-wins
/// anyway, so re-emitting would only burn a python subprocess per poll.
static STALE_CONDITION_EMITTED: AtomicBool = AtomicBool::new(false);

/// PURE: may this verdict retire the `launcher_binary_stale` record?
///
/// ONLY a positively-probed `Fresh`. `NotProbed` means an update owned the tree
/// and the probe never ran — clearing an action-required record on the strength
/// of a look we did not take is precisely the dishonest state this module
/// exists to remove (it is why `NotProbed` is not `Fresh` in the first place).
/// `Stale(_)` obviously keeps it.
pub(crate) fn should_clear_stale_condition(verdict: &FreshnessVerdict) -> bool {
    matches!(verdict, FreshnessVerdict::Fresh)
}

/// Is `cid` actually recorded on disk for this install?
///
/// Cheap pre-check so the common case — a healthy install polling the
/// update-check every few minutes — never spends a python subprocess just to
/// resolve nothing. Both the JSON sidecar (authoritative for Python reads) and
/// the Markdown are checked, so an install that has only one of them is still
/// cleared.
///
/// One home for both probe-driven clears in this module (`launcher_binary_stale`
/// and `launcher_binary_handoff_skipped_dirty`) — the "is it there" question is
/// the same question, so it gets one implementation.
fn condition_is_recorded(install_path: &Path, cid: &str) -> bool {
    let ctx = install_path.join(".claude").join("context");
    ["UPDATE_DEFERRED.json", "UPDATE_DEFERRED.md"]
        .iter()
        .any(|name| {
            std::fs::read_to_string(ctx.join(name))
                .map(|body| body.contains(cid))
                .unwrap_or(false)
        })
}

/// Retire the `launcher_binary_stale` record once the binary is demonstrably
/// fresh again (the user quit and relaunched, or `install.py --update` put
/// HEAD's bytes back).
///
/// Also clears [`STALE_CONDITION_EMITTED`]: that latch exists to stop one
/// process re-emitting the same record on every poll, but once the record is
/// GONE the latch would suppress a legitimate re-emit if the binary went stale
/// again later in the same process — the banner would silently never return.
/// Clearing the record and clearing the latch are one operation.
///
/// Best-effort: a failed resolve leaves the record in place (false-keep is
/// recoverable — the next boot probes again; a false-clear is not).
fn clear_stale_condition_if_fresh(install_path: &Path, verdict: &FreshnessVerdict) {
    clear_stale_condition_if_fresh_with(install_path, verdict, production_resolve);
}

/// The production resolve side-effect shared by both probe-driven clears.
///
/// Extracted so the two `*_with` cores below take it as a parameter: an ACT leg
/// asserted only through "python was spawned" is an act leg nothing can test
/// hermetically, and the acts here retire `action_required` records.
fn production_resolve(install_path: &Path, cids: &[&str]) -> Result<(), String> {
    crate::services::deferral::resolve_deferral_conditions(install_path, install_path, cids)
}

/// Resolver-injectable core of [`clear_stale_condition_if_fresh`].
fn clear_stale_condition_if_fresh_with<F>(
    install_path: &Path,
    verdict: &FreshnessVerdict,
    resolve: F,
) where
    F: FnOnce(&Path, &[&str]) -> Result<(), String>,
{
    if !should_clear_stale_condition(verdict) {
        return;
    }
    if !condition_is_recorded(install_path, CID_BINARY_STALE) {
        return;
    }
    match resolve(install_path, &[CID_BINARY_STALE]) {
        Ok(()) => {
            STALE_CONDITION_EMITTED.store(false, Ordering::SeqCst);
            tracing::info!(
                "[vct] binary_freshness: the running binary matches the dist tree again — \
                 retired the {} record",
                CID_BINARY_STALE,
            );
        }
        Err(e) => tracing::warn!(
            "[vct] binary_freshness: could not retire the {} record (non-fatal, the next \
             boot probes again): {}",
            CID_BINARY_STALE, e,
        ),
    }
}

/// PURE: may this pass retire the `launcher_binary_handoff_skipped_dirty`
/// record?
///
/// The record says: new bytes are parked as `<target>.new` siblings, the dist
/// tree diverges from HEAD, and nothing is scheduled to move them. It is over
/// exactly when BOTH halves of that are gone — the tree matches HEAD again AND
/// no candidate still has a staged sibling waiting. Either half alone leaves a
/// half-delivered state, which is the state the record exists to name.
///
/// `NotProbed` refuses regardless of the other two: a stand-down means an update
/// owned the tree and this pass never looked, and the two inputs are then
/// placeholders, not observations. Same tri-state discipline as
/// [`should_clear_stale_condition`] — a probe that did not run proves nothing.
pub(crate) fn should_clear_handoff_skipped_condition(
    verdict: &FreshnessVerdict,
    dist_dirty: bool,
    new_siblings_remain: bool,
) -> bool {
    if matches!(verdict, FreshnessVerdict::NotProbed) {
        return false;
    }
    !dist_dirty && !new_siblings_remain
}

/// Does ANY swap candidate still have a `<target>.new` sibling parked on disk?
///
/// The second half of the handoff-skipped truth condition. Reads the same
/// candidate list and the same `.new` naming the staging writer and
/// `vct-updater` use, so "a sibling is waiting" means the same thing on every
/// side of the delivery chain.
fn any_staged_new_sibling_remains(install_path: &Path) -> bool {
    swap_candidate_rel_paths()
        .iter()
        .any(|rel| path_with_new_suffix(&install_path.join(rel)).is_file())
}

/// Retire the `launcher_binary_handoff_skipped_dirty` record once the staged
/// bytes have actually been delivered (or discarded): dist clean vs HEAD and no
/// `.new` sibling left waiting.
///
/// Called ONLY from the at-rest reconcile's do-nothing branch — i.e. after a
/// real probe, and at a moment when this pass has staged nothing itself, so the
/// `.new` reading below cannot be observing our own work-in-progress.
///
/// `dist_dirty` is the value THIS pass already probed
/// ([`FreshnessInputs::dist_dirty`]) — passed in rather than re-probed so the
/// clear cannot disagree with the verdict it hangs off, and so no second `git
/// status` subprocess runs per poll.
///
/// The decision itself is [`should_clear_handoff_skipped_condition`]; placing
/// the only call in the settled branch is the extra conservatism on top of it.
/// False-keep is recoverable (the next boot probes again); false-clear tells a
/// user with undelivered binaries that everything is fine.
///
/// Best-effort throughout — a resolve failure leaves the record in place.
fn clear_handoff_skipped_if_delivered(
    install_path: &Path,
    verdict: &FreshnessVerdict,
    dist_dirty: bool,
) {
    clear_handoff_skipped_if_delivered_with(install_path, verdict, dist_dirty, production_resolve);
}

/// Resolver-injectable core of [`clear_handoff_skipped_if_delivered`].
fn clear_handoff_skipped_if_delivered_with<F>(
    install_path: &Path,
    verdict: &FreshnessVerdict,
    dist_dirty: bool,
    resolve: F,
) where
    F: FnOnce(&Path, &[&str]) -> Result<(), String>,
{
    // Order matters for cost: the cheap on-disk record check gates the
    // filesystem walk, which gates the python subprocess.
    if !condition_is_recorded(install_path, CID_HANDOFF_SKIPPED) {
        return;
    }
    let siblings_remain = any_staged_new_sibling_remains(install_path);
    if !should_clear_handoff_skipped_condition(verdict, dist_dirty, siblings_remain) {
        return;
    }
    match resolve(install_path, &[CID_HANDOFF_SKIPPED]) {
        Ok(()) => tracing::info!(
            "[vct] binary_freshness: the dist tree matches HEAD and no staged `.new` sibling \
             remains — retired the {} record",
            CID_HANDOFF_SKIPPED,
        ),
        Err(e) => tracing::warn!(
            "[vct] binary_freshness: could not retire the {} record (non-fatal, the next \
             boot probes again): {}",
            CID_HANDOFF_SKIPPED, e,
        ),
    }
}

/// Arm a stage1 binary swap to run when the user quits.
///
/// Returns true iff the arming took effect. Refuses (returns false) when an
/// updater lock already exists on disk: that means a real UPDATE handoff is in
/// flight, and overwriting its lock would drop the post-update relaunch the
/// user is waiting for.
pub(crate) fn arm_stage1_swap_on_exit(install_path: &Path) -> bool {
    let lock_path = vct_root_dir().join(crate::commands::update_handoff::UPDATE_LOCK_FILE);
    if lock_path.exists() {
        tracing::info!(
            "[vct] binary_freshness: NOT arming an at-rest swap — an update handoff lock \
             already exists at {} (a real update is finishing; its relaunch must win)",
            lock_path.display(),
        );
        return false;
    }
    match ARMED_SWAP_ROOT.lock() {
        Ok(mut slot) => {
            *slot = Some(install_path.to_path_buf());
            tracing::info!(
                "[vct] binary_freshness: armed an at-rest binary swap for {} — it runs when YOU \
                 quit the launcher (no auto-restart, no auto-quit)",
                install_path.display(),
            );
            true
        }
        Err(e) => {
            tracing::warn!("[vct] binary_freshness: could not arm at-rest swap: {}", e);
            false
        }
    }
}

/// Run the armed swap, if any. Called from the Tauri `RunEvent::Exit` handler.
///
/// Writes an updater lock with **`relaunch: None`** and spawns `vct-updater`
/// detached. The user asked to quit, so the swap happens and the process stays
/// gone — the fresh binary is what their NEXT manual launch executes.
///
/// Windows-only in effect (POSIX has no updater binary and needs none).
/// Best-effort and bounded: pure file I/O plus one detached spawn, so it cannot
/// stall the exit path.
pub(crate) fn perform_armed_swap_on_exit() {
    let root = match ARMED_SWAP_ROOT.lock() {
        Ok(mut slot) => match slot.take() {
            Some(r) => r,
            None => return,
        },
        Err(_) => return,
    };
    swap_on_exit_impl(&root);
}

#[cfg(target_os = "windows")]
fn swap_on_exit_impl(install_root: &Path) {
    use crate::commands::update_handoff::{
        prepare_update_handoff_impl, UPDATE_LOCK_FILE,
    };

    // v0.2.91 fix-round MAJOR-2(a): same stale-`.new` invalidation the shared
    // post-update tail runs. Between ARMING (at boot / update-check) and this
    // exit, a real update may have landed newer binaries at the canonical
    // paths; the copy we staged back then is then older than what is on disk,
    // and swapping it in would undo the update the user just took.
    //
    // MUST stay BEFORE the delegation below: the handoff decides what to swap
    // purely from which `<target>.new` siblings exist, so a stale sibling that
    // is still on disk when it runs is a stale sibling that gets swapped in.
    let dropped =
        invalidate_stale_new_siblings_for_blocking(install_root, &swap_candidate_rel_paths());
    if !dropped.is_empty() {
        tracing::warn!(
            "[vct] binary_freshness: armed swap dropped {} stale staged binary/binaries: {:?}",
            dropped.len(),
            dropped,
        );
    }

    // Yield to a REAL update handoff. `prepare_update_handoff_impl` overwrites
    // the lock unconditionally (correct for the update surface, which owns
    // it); at-rest we must not, or we would drop the post-update relaunch the
    // user is waiting for. Same check `arm_stage1_swap_on_exit` makes at
    // arming time — re-checked here because the window between them is wide.
    let lock_path = vct_root_dir().join(UPDATE_LOCK_FILE);
    if lock_path.exists() {
        tracing::warn!(
            "[vct] binary_freshness: armed swap skipped — update handoff lock present at {}",
            lock_path.display(),
        );
        return;
    }

    // v0.2.91 wave-2 (WP-F carry-over ii): ONE home for "write the updater
    // lock and spawn vct-updater detached". `relaunch: false` is the whole
    // difference from the update surface — the user QUIT, so quitting is what
    // happens; the fresh binary is what their next manual launch executes
    // (standing no-auto-restart ruling).
    match prepare_update_handoff_impl(install_root, false) {
        Ok(res) if res.handoff_active => tracing::info!(
            "[vct] binary_freshness: at-rest swap handed to vct-updater (no relaunch); lock={:?}",
            res.lock_path,
        ),
        Ok(res) => {
            // no_swaps_needed is the common, silent case (nothing staged).
            if res.skip_reason.as_deref() != Some("no_swaps_needed") {
                tracing::warn!(
                    "[vct] binary_freshness: armed swap skipped — {}",
                    res.skip_reason.as_deref().unwrap_or("unknown reason"),
                );
            }
        }
        Err(e) => tracing::warn!("[vct] binary_freshness: armed swap could not run: {}", e),
    }
}

#[cfg(not(target_os = "windows"))]
fn swap_on_exit_impl(_install_root: &Path) {
    // POSIX never stages `.new` siblings (nothing to swap) and ships no
    // updater binary. No-op by construction.
}

// ---------------------------------------------------------------------------
// WI-7 — observability conditions
// ---------------------------------------------------------------------------

/// Condition id for "a restore would have renamed old bytes over a newer
/// canonical binary". Informational record: the clobber was AVERTED, so no user
/// action is required — but the state must stop being silent.
pub(crate) const CID_CLOBBER_AVERTED: &str = "launcher_binary_clobber_averted";

/// Condition id for "a stage1 handoff was skipped while a dist binary is
/// git-dirty" — the silent state that let the field install freeze.
pub(crate) const CID_HANDOFF_SKIPPED: &str = "launcher_binary_handoff_skipped_dirty";

/// Condition id for "the binary on disk is not the binary that is running".
/// Action-required: the ONE manual action is a full quit + relaunch.
pub(crate) const CID_BINARY_STALE: &str = "launcher_binary_stale";

fn emit(install_path: &Path, fields: &crate::services::deferral::DeferralEntryFields<'_>) {
    if let Err(e) =
        crate::services::deferral::emit_deferral_entry(install_path, install_path, fields)
    {
        tracing::warn!(
            "[vct] binary_freshness: deferral emit for {} failed (non-fatal): {}",
            fields.condition_id, e,
        );
    }
}

/// WI-7 (a): record that an abort tail declined to restore old bytes over a
/// newer canonical binary.
pub(crate) fn emit_clobber_averted_condition(
    install_path: &Path,
    backup_path: &Path,
    canonical_path: &Path,
) {
    let detected = format!(
        "An update abort/conflict path was about to restore the pre-pull backup `{backup}` over \
         `{canonical}`, but the two files differ — the pull had ALREADY written a NEWER binary \
         to the canonical path. The restore was declined and the new bytes were kept (v0.2.91 \
         WI-3). The backup stays parked and is deleted by the launcher's boot sweep once this \
         PID exits.",
        backup = backup_path.display(),
        canonical = canonical_path.display(),
    );
    let fields = crate::services::deferral::DeferralEntryFields {
        condition_id: CID_CLOBBER_AVERTED,
        title: "Update abort kept the freshly-pulled binary (clobber averted)",
        detected: &detected,
        why_deferred:
            "Informational record — nothing is broken and no action is required. Before v0.2.91 \
             this exact moment silently reverted the new binary and left the install frozen on \
             the old one, with no trace anywhere. The record exists so the state is diagnosable.",
        command_to_apply:
            "# No action required. To confirm the canonical binary matches HEAD:\n\
             git status --porcelain -- launcher/dist/",
        severity: "info",
    };
    emit(install_path, &fields);
}

/// WI-7 (b): record that a handoff was skipped while dist binaries were staged
/// / dirty — i.e. the new bytes are on disk but nothing will move them.
pub(crate) fn emit_handoff_skipped_while_dirty(
    install_path: &Path,
    staged: &[String],
    skip_reason: &str,
) {
    let detected = format!(
        "The stage1 binary handoff was SKIPPED (reason: `{reason}`) while {n} dist binary/binaries \
         were staged for swapping: {list}. The new bytes are on disk as `<target>.new` siblings \
         but nothing is scheduled to rename them onto the canonical paths, so the launcher will \
         keep running the old binary.",
        reason = skip_reason,
        n = staged.len(),
        list = staged.join(", "),
    );
    let fields = crate::services::deferral::DeferralEntryFields {
        condition_id: CID_HANDOFF_SKIPPED,
        title: "Binary swap staged but the handoff did not fire",
        detected: &detected,
        why_deferred:
            "The launcher cannot overwrite its own running binary on Windows; the swap needs \
             `vct-updater` to run after the launcher exits. When the handoff is skipped the \
             staged bytes simply wait. Pre-v0.2.91 this state produced no record at all.",
        command_to_apply:
            "# 1. Fully quit the launcher (tray -> Quit) and stop the hub:\n\
             vct-hub --stop\n\
             # 2. From the orchestrator install root, restore the dist binaries from HEAD:\n\
             git checkout -- launcher/dist/\n\
             # 3. Remove any leftover staged siblings, then relaunch:\n\
             #    launcher/dist/<arch>/*.new\n\
             python install.py --update",
        severity: "warning",
    };
    emit(install_path, &fields);
}

/// WI-1 surfacing: the durable, honest record that the running binary is not
/// the binary on disk. Names all three versions and the ONE manual action.
///
/// Emitted at most once per launcher process (the reconcile runs at boot and on
/// every update-check poll; the entry is last-write-wins, so re-emitting would
/// only spend a subprocess).
pub(crate) fn emit_binary_stale_condition(
    install_path: &Path,
    inputs: &FreshnessInputs,
    staged: &[String],
    armed: bool,
) {
    if STALE_CONDITION_EMITTED.swap(true, Ordering::SeqCst) {
        return;
    }
    let on_disk = inputs.on_disk_version.as_deref().unwrap_or("<unknown>");
    let next_step = if armed {
        "A swap has been STAGED and armed: quit the launcher normally (tray -> Quit) and the \
         staged binary is put in place before the process is gone. Then start the launcher \
         again — no automatic restart happens, by design."
    } else {
        "Nothing was staged and no swap was armed. Either this platform does not need one \
         (POSIX overwrites a running binary safely), or the only signal was a dirty dist tree \
         at an EQUAL version — which is also what a local `cargo build` looks like, and the \
         launcher will not overwrite your own build from HEAD behind your back (v0.2.91). \
         Quit the launcher and run the command below when you want HEAD's binaries restored."
    };
    let detected = format!(
        "The launcher process is running v{running}, the dist sidecar on disk declares \
         v{on_disk}, and `git status --porcelain -- {dist}` reports the dist tree as \
         {dirty}. That means the binary git says should be on disk is NOT the binary that is \
         executing. Staged this pass: {staged}. {next}",
        running = inputs.running_version,
        on_disk = on_disk,
        dist = dist_dir_rel_path(),
        dirty = if inputs.dist_dirty { "DIRTY (diverged from HEAD)" } else { "clean" },
        staged = if staged.is_empty() { "none".to_string() } else { staged.join(", ") },
        next = next_step,
    );
    let fields = crate::services::deferral::DeferralEntryFields {
        condition_id: CID_BINARY_STALE,
        title: "Launcher is running an older binary than the one on disk",
        detected: &detected,
        why_deferred:
            "Replacing a running executable is not something the launcher may do to itself \
             without the user's consent, and the standing rule is that a post-update restart is \
             the USER's action — the product never restarts or quits itself. So the swap is \
             prepared and this record explains it instead.",
        command_to_apply:
            "# 1. Fully quit the launcher (tray -> Quit) and stop the hub:\n\
             vct-hub --stop\n\
             # 2. From the orchestrator install root, put HEAD's binaries back on disk:\n\
             git checkout -- launcher/dist/\n\
             # 3. Reconcile hooks / MCP registrations against the current source:\n\
             python install.py --update\n\
             # 4. Relaunch the launcher through your usual entrypoint.",
        severity: "warning",
    };
    emit(install_path, &fields);
}

// ---------------------------------------------------------------------------
// Tests — reactor-free by construction (no #[tokio::test] for mechanism).
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::{Command as StdCommand, Stdio};

    // -- WP-F carry-over (ii): one home for the updater handoff -------------

    /// The at-rest exit swap must DELEGATE to
    /// `update_handoff::prepare_update_handoff_impl`, not carry a second copy
    /// of the lock-write + detached-spawn sequence. Wave-1 shipped that copy
    /// (~12 lines, its own creation flags); a second copy is exactly how the
    /// two sides drift when the wire contract or the flags change.
    ///
    /// The needles are assembled from fragments so this test's own source
    /// cannot satisfy the search it performs.
    #[test]
    fn at_rest_swap_has_no_second_detached_spawn() {
        let src = include_str!("binary_freshness.rs");
        let flag = concat!("CREATE_NEW_", "PROCESS_GROUP");
        assert!(
            !src.contains(flag),
            "the detached-spawn creation flags belong to update_handoff's \
             spawn_updater — this module must call it, not re-implement it",
        );
        let detached = concat!("DETACHED_", "PROCESS");
        assert!(!src.contains(detached));
        assert!(
            src.contains(concat!("prepare_update_", "handoff_impl")),
            "the exit swap must route through the shared handoff impl",
        );
    }

    // -- WP-B registry pairing: the probe-driven clears are WIRED IN ---------

    /// The clears are only worth anything if the at-rest reconcile calls them.
    /// Both live in the do-nothing branch — the one moment we have a probed
    /// verdict AND have staged nothing ourselves — so this pins the call site,
    /// not just the helpers. Deleting either call turns this red.
    ///
    /// Needles are assembled from fragments so this test's own source cannot
    /// satisfy the search; the slice is bounded to the reconcile function so a
    /// call from anywhere else would not count either.
    #[test]
    fn the_at_rest_reconcile_wires_both_probe_driven_clears() {
        let src = include_str!("binary_freshness.rs");
        let start = src
            .find(concat!("pub(crate) async fn reconcile_dist_at_", "rest_gated"))
            .expect("the gated reconcile must exist");
        let end = src[start..]
            .find("Arm / perform the NO-RELAUNCH")
            .map(|i| start + i)
            .expect("the section that follows the reconcile must exist");
        let body = &src[start..end];

        assert!(
            body.contains(concat!("clear_stale_condition_if_", "fresh(install_path")),
            "the do-nothing branch must retire `launcher_binary_stale` when the \
             probe says Fresh — otherwise the once-per-process emit latch means \
             nothing can ever clear it",
        );
        assert!(
            body.contains(concat!("clear_handoff_skipped_if_", "delivered(install_path")),
            "the do-nothing branch must retire \
             `launcher_binary_handoff_skipped_dirty` once the staged bytes are \
             delivered",
        );
    }

    // -- WP-B registry pairing: the probe-driven clear of the stale record ----

    /// Both sides of the destructive-ish gate (retiring an `action_required`
    /// record). Only a verdict we POSITIVELY established may clear it.
    #[test]
    fn only_a_probed_fresh_verdict_retires_the_stale_record() {
        assert!(should_clear_stale_condition(&FreshnessVerdict::Fresh));

        assert!(
            !should_clear_stale_condition(&FreshnessVerdict::NotProbed),
            "NotProbed means an update owned the tree and we never looked — \
             clearing on it invents a clean bill of health",
        );
        for reason in [
            StaleReason::OnDiskNewerThanRunning,
            StaleReason::DistDirtyVsHead,
            StaleReason::Both,
        ] {
            assert!(!should_clear_stale_condition(&FreshnessVerdict::Stale(reason)));
        }
    }

    #[test]
    fn the_stale_record_is_detected_in_either_deferral_file() {
        for name in ["UPDATE_DEFERRED.json", "UPDATE_DEFERRED.md"] {
            let tmp = tempfile::tempdir().expect("tempdir");
            let ctx = tmp.path().join(".claude").join("context");
            std::fs::create_dir_all(&ctx).expect("mkdir");
            assert!(
                !condition_is_recorded(tmp.path(), CID_BINARY_STALE),
                "nothing on disk yet",
            );
            std::fs::write(
                ctx.join(name),
                format!("## {} (warning)\n\n**Title**: foo\n", CID_BINARY_STALE),
            )
            .expect("write");
            assert!(
                condition_is_recorded(tmp.path(), CID_BINARY_STALE),
                "{} must be enough to trigger the clear",
                name,
            );
        }
    }

    /// `condition_is_recorded` asks `body.contains(cid)` — a SUBSTRING test.
    ///
    /// That is safe for the two cids it serves today (neither is a substring of
    /// the other) and its failure direction is benign anyway: a false positive
    /// only spends a python subprocess, because resolving an id that is not
    /// recorded is a no-op. It stops being safe the moment it is pointed at a
    /// cid that is a PREFIX of another registered one — the registry already
    /// contains such a pair (`deprecated_mcp_` vs
    /// `deprecated_mcp_removal_summary`), where a sibling's record would be
    /// read as this one's and an `action_required` entry could be retired while
    /// its condition still holds.
    ///
    /// So the substring semantics are allowed to stay only while the call set
    /// stays scoped. This pins that scope: generalising the helper to a third
    /// cid fails here and forces the anchored-match decision to be made
    /// deliberately rather than inherited by accident. (Raised by WP-B on the
    /// registry review, 2026-08-26.)
    #[test]
    fn the_record_presence_check_stays_scoped_to_its_two_cids() {
        let src = include_str!("binary_freshness.rs");
        // Production call sites only — the fixtures below legitimately call it
        // with the same two constants.
        let production = src.split("mod tests {").next().expect("module has tests");
        let needle = concat!("condition_is_", "recorded(install_path, ");
        let call_args: Vec<&str> = production
            .match_indices(needle)
            .map(|(idx, _)| {
                production[idx + needle.len()..]
                    .split(')')
                    .next()
                    .unwrap_or("")
                    .trim()
            })
            .collect();

        assert!(
            !call_args.is_empty(),
            "expected the probe-driven clears to consult the presence helper",
        );
        for arg in &call_args {
            assert!(
                matches!(*arg, "CID_BINARY_STALE" | "CID_HANDOFF_SKIPPED"),
                "`condition_is_recorded` does a SUBSTRING match, which is only \
                 sound for cids that are not prefixes of another registered id. \
                 Called with `{arg}` — either anchor the match (`## {{cid}} (` in \
                 the markdown, `\"condition_id\": \"{{cid}}\"` in the JSON) or keep \
                 the call set to the two ids this was reasoned about.",
            );
        }
    }

    // -- WP-B registry pairing: the handoff-skipped record's probe-driven clear --

    /// The full truth table of the second probe-driven clear, both directions.
    ///
    /// `launcher_binary_handoff_skipped_dirty` says "new bytes are parked and
    /// nothing will move them". It is over only when BOTH halves are gone; a
    /// half-delivered state must keep it.
    #[test]
    fn the_handoff_record_retires_only_when_both_halves_are_gone() {
        // Delivered: tree matches HEAD, nothing left staged.
        assert!(should_clear_handoff_skipped_condition(
            &FreshnessVerdict::Fresh,
            false,
            false,
        ));

        // Still dirty vs HEAD ⇒ the divergence half still holds.
        assert!(
            !should_clear_handoff_skipped_condition(&FreshnessVerdict::Fresh, true, false),
            "a dirty dist tree is half the condition — it may not clear",
        );
        // A `.new` sibling is still parked ⇒ the undelivered half still holds.
        assert!(
            !should_clear_handoff_skipped_condition(&FreshnessVerdict::Fresh, false, true),
            "a staged sibling still waiting is the other half — it may not clear",
        );
        assert!(!should_clear_handoff_skipped_condition(
            &FreshnessVerdict::Fresh,
            true,
            true,
        ));

        // Tri-state: a stand-down never clears anything, whatever the other
        // two inputs happen to say (they are placeholders, not observations).
        for (dirty, staged) in [(false, false), (false, true), (true, false), (true, true)] {
            assert!(
                !should_clear_handoff_skipped_condition(
                    &FreshnessVerdict::NotProbed,
                    dirty,
                    staged,
                ),
                "NotProbed means we never looked — it may not clear \
                 (dirty={dirty}, staged={staged})",
            );
        }
    }

    /// The `.new` probe reads the SAME candidate list and suffix the staging
    /// writer and `vct-updater` use. Both legs: nothing parked, then one parked.
    #[test]
    fn the_staged_sibling_probe_sees_a_parked_new_file() {
        let tmp = tempfile::tempdir().expect("tempdir");
        assert!(
            !any_staged_new_sibling_remains(tmp.path()),
            "an empty tree has nothing parked",
        );

        let rel = swap_candidate_rel_paths()
            .into_iter()
            .next()
            .expect("at least one swap candidate on every host");
        let target = tmp.path().join(&rel);
        std::fs::create_dir_all(target.parent().expect("candidate has a parent")).expect("mkdir");
        // The canonical binary alone must NOT read as "staged".
        std::fs::write(&target, b"canonical").expect("write");
        assert!(
            !any_staged_new_sibling_remains(tmp.path()),
            "the canonical binary is not a staged sibling",
        );

        std::fs::write(path_with_new_suffix(&target), b"staged").expect("write");
        assert!(
            any_staged_new_sibling_remains(tmp.path()),
            "a parked `{}.new` must be seen",
            rel,
        );
    }

    /// Write `cid` into a temp install's `UPDATE_DEFERRED.md` and hand back the
    /// tempdir. Shared by the ACT/leave-alone legs below.
    fn install_with_recorded_condition(cid: &str) -> tempfile::TempDir {
        let tmp = tempfile::tempdir().expect("tempdir");
        let ctx = tmp.path().join(".claude").join("context");
        std::fs::create_dir_all(&ctx).expect("mkdir");
        std::fs::write(
            ctx.join("UPDATE_DEFERRED.md"),
            format!("## {} (warning)\n\n**Title**: foo\n", cid),
        )
        .expect("write");
        tmp
    }

    /// Park a `<candidate>.new` sibling in `root`, returning the relative path.
    fn park_staged_sibling(root: &Path) -> String {
        let rel = swap_candidate_rel_paths()
            .into_iter()
            .next()
            .expect("at least one swap candidate on every host");
        let target = root.join(&rel);
        std::fs::create_dir_all(target.parent().expect("candidate has a parent")).expect("mkdir");
        std::fs::write(path_with_new_suffix(&target), b"staged").expect("write");
        rel
    }

    /// ACT leg for the STALE clear (wave-1 shipped the leave-alone legs only).
    /// A probed `Fresh` over a recorded entry must actually reach the resolver,
    /// with exactly that one cid, and must drop the emit latch so a later
    /// re-staling can surface again in the same process.
    #[test]
    fn a_fresh_verdict_over_a_recorded_stale_entry_resolves_it() {
        let tmp = install_with_recorded_condition(CID_BINARY_STALE);
        let mut seen: Vec<String> = Vec::new();
        STALE_CONDITION_EMITTED.store(true, Ordering::SeqCst);

        clear_stale_condition_if_fresh_with(
            tmp.path(),
            &FreshnessVerdict::Fresh,
            |root, cids| {
                assert_eq!(root, tmp.path(), "the resolve must target this install");
                seen.extend(cids.iter().map(|c| c.to_string()));
                Ok(())
            },
        );

        assert_eq!(seen, vec![CID_BINARY_STALE.to_string()]);
        assert!(
            !STALE_CONDITION_EMITTED.load(Ordering::SeqCst),
            "clearing the record must clear the latch, or a later re-stale in \
             this process could never surface",
        );
        STALE_CONDITION_EMITTED.store(false, Ordering::SeqCst);
    }

    /// Leave-alone legs for the STALE clear at the CALL level: neither a stale
    /// verdict nor a stand-down may reach the resolver.
    #[test]
    fn a_non_fresh_verdict_never_reaches_the_stale_resolver() {
        for verdict in [
            FreshnessVerdict::Stale(StaleReason::Both),
            FreshnessVerdict::Stale(StaleReason::OnDiskNewerThanRunning),
            FreshnessVerdict::Stale(StaleReason::DistDirtyVsHead),
            FreshnessVerdict::NotProbed,
        ] {
            let tmp = install_with_recorded_condition(CID_BINARY_STALE);
            let mut called = false;
            clear_stale_condition_if_fresh_with(tmp.path(), &verdict, |_, _| {
                called = true;
                Ok(())
            });
            assert!(!called, "{:?} must not retire an action-required record", verdict);
        }
    }

    /// ACT leg for the HANDOFF clear: recorded + probed + tree clean + nothing
    /// parked ⇒ the resolver runs with exactly that cid.
    #[test]
    fn a_delivered_tree_retires_the_handoff_record() {
        let tmp = install_with_recorded_condition(CID_HANDOFF_SKIPPED);
        let mut seen: Vec<String> = Vec::new();

        clear_handoff_skipped_if_delivered_with(
            tmp.path(),
            &FreshnessVerdict::Fresh,
            false,
            |root, cids| {
                assert_eq!(root, tmp.path());
                seen.extend(cids.iter().map(|c| c.to_string()));
                Ok(())
            },
        );

        assert_eq!(seen, vec![CID_HANDOFF_SKIPPED.to_string()]);
    }

    /// Leave-alone legs for the HANDOFF clear at the CALL level, one per way
    /// the condition can still hold: a dirty tree, a parked sibling, a
    /// stand-down verdict, and no record at all.
    #[test]
    fn an_undelivered_or_unprobed_state_never_reaches_the_handoff_resolver() {
        // (a) tree still diverges from HEAD.
        let tmp = install_with_recorded_condition(CID_HANDOFF_SKIPPED);
        let mut called = false;
        clear_handoff_skipped_if_delivered_with(
            tmp.path(),
            &FreshnessVerdict::Fresh,
            true,
            |_, _| {
                called = true;
                Ok(())
            },
        );
        assert!(!called, "a dirty dist tree is half the condition — keep the record");

        // (b) a staged `.new` sibling is still parked.
        let tmp = install_with_recorded_condition(CID_HANDOFF_SKIPPED);
        let rel = park_staged_sibling(tmp.path());
        let mut called = false;
        clear_handoff_skipped_if_delivered_with(
            tmp.path(),
            &FreshnessVerdict::Fresh,
            false,
            |_, _| {
                called = true;
                Ok(())
            },
        );
        assert!(!called, "`{}.new` is still waiting — keep the record", rel);

        // (c) the probe never ran (an update owned the tree).
        let tmp = install_with_recorded_condition(CID_HANDOFF_SKIPPED);
        let mut called = false;
        clear_handoff_skipped_if_delivered_with(
            tmp.path(),
            &FreshnessVerdict::NotProbed,
            false,
            |_, _| {
                called = true;
                Ok(())
            },
        );
        assert!(!called, "a probe that never ran proves nothing");

        // (d) a FOREIGN record is on disk; ours is not. Nothing to resolve, and
        //     nothing of anyone else's to touch.
        let tmp = install_with_recorded_condition(CID_BINARY_STALE);
        let mut called = false;
        clear_handoff_skipped_if_delivered_with(
            tmp.path(),
            &FreshnessVerdict::Fresh,
            false,
            |_, _| {
                called = true;
                Ok(())
            },
        );
        assert!(!called, "no handoff record on disk — spend nothing");
    }

    /// Leave-alone leg + the "don't spend a subprocess for nothing" guard: with
    /// no record on disk the clear must return WITHOUT invoking the python
    /// resolve helper. Hermetic precisely because it never reaches python.
    #[test]
    fn a_fresh_verdict_with_no_record_on_disk_does_nothing() {
        let tmp = tempfile::tempdir().expect("tempdir");
        std::fs::create_dir_all(tmp.path().join(".claude").join("context")).expect("mkdir");
        STALE_CONDITION_EMITTED.store(true, Ordering::SeqCst);
        clear_stale_condition_if_fresh(tmp.path(), &FreshnessVerdict::Fresh);
        assert!(
            STALE_CONDITION_EMITTED.load(Ordering::SeqCst),
            "no record was cleared, so the emit latch must be left exactly as it was",
        );
        STALE_CONDITION_EMITTED.store(false, Ordering::SeqCst);
    }

    /// A stale verdict must not even LOOK at the deferral files, let alone
    /// resolve — the record is what the user is being asked to act on.
    #[test]
    fn a_stale_verdict_never_reaches_the_resolve_path() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let ctx = tmp.path().join(".claude").join("context");
        std::fs::create_dir_all(&ctx).expect("mkdir");
        let md = ctx.join("UPDATE_DEFERRED.md");
        let body = format!("## {} (warning)\n\n**Title**: foo\n", CID_BINARY_STALE);
        std::fs::write(&md, &body).expect("write");

        clear_stale_condition_if_fresh(tmp.path(), &FreshnessVerdict::Stale(StaleReason::Both));

        assert_eq!(
            std::fs::read_to_string(&md).expect("read"),
            body,
            "the record must survive a stale verdict untouched",
        );
    }

    /// WP-F carry-over (iv): the installer's abort tail must revert AND record
    /// through the shared helper. It used to inline the outcome-check →
    /// canonical-resolve → emit sequence; a second copy is how one surface
    /// ends up silently dropping the `launcher_binary_clobber_averted` record
    /// (which is precisely what `self_update.rs` did before the wave-1 fix).
    #[test]
    fn the_installer_abort_tail_reverts_through_the_shared_recorder() {
        let installer = include_str!("../commands/installer.rs");
        assert!(
            installer.contains(concat!("binary_freshness::revert_and_", "record(")),
            "installer.rs's abort tail must call the shared revert+record helper",
        );
        assert!(
            !installer.contains(concat!("emit_clobber_averted_", "condition(")),
            "emitting the clobber-averted record is the helper's job — an inline \
             emit here is the duplicate that was consolidated in v0.2.91",
        );
    }

    // -- WI-3: the pure revert/keep decision, both legs ---------------------

    /// RED-PROOF: on `bd8f6836` there is no decision function at all —
    /// `revert_pre_pull_rename` renames unconditionally. This asserts the
    /// leave-alone leg, i.e. the exact branch whose ABSENCE froze the field
    /// install (RC-1).
    #[test]
    fn differing_canonical_is_never_clobbered() {
        assert_eq!(
            decide_revert(CanonicalState::Differs),
            RevertDecision::KeepCanonicalParkBackup,
            "a canonical binary whose bytes differ from the backup holds freshly-pulled bytes; \
             restoring over it is the RC-1 clobber"
        );
    }

    /// Both-sides discipline: the ACT leg. Identical bytes ⇒ the rename is a
    /// pure name restore and must still happen (otherwise every ordinary
    /// aborted pull would leave the canonical path missing).
    #[test]
    fn identical_or_absent_canonical_still_reverts() {
        assert_eq!(
            decide_revert(CanonicalState::Identical),
            RevertDecision::RestoreBackup
        );
        assert_eq!(
            decide_revert(CanonicalState::Absent),
            RevertDecision::RestoreBackup
        );
    }

    /// Conservative default on a best-effort path: an unreadable side cannot
    /// positively confirm "same content", so we do NOT overwrite.
    #[test]
    fn unknown_state_prefers_keeping_the_canonical_file() {
        assert_eq!(
            decide_revert(CanonicalState::Unknown),
            RevertDecision::KeepCanonicalParkBackup
        );
    }

    #[test]
    fn canonical_path_recovery_is_exact_and_refuses_foreign_names() {
        assert_eq!(
            canonical_path_for_backup(Path::new("/d/vct-launcher.exe.old-4242")),
            Some(PathBuf::from("/d/vct-launcher.exe"))
        );
        assert_eq!(
            canonical_path_for_backup(Path::new("/d/vct-launcher.old-1")),
            Some(PathBuf::from("/d/vct-launcher"))
        );
        // Not our shape → never guess.
        assert_eq!(canonical_path_for_backup(Path::new("/d/vct-launcher")), None);
        assert_eq!(
            canonical_path_for_backup(Path::new("/d/vct-launcher.exe.old-")),
            None
        );
    }

    // -- WI-3 end-to-end over the real filesystem (no reactor) -------------

    fn tmpdir(label: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-binfresh-{}-{}",
            label,
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    /// RED-PROOF (WI-3): the clobber-averted case. Against `bd8f6836`'s
    /// unconditional rename this test FAILS on both assertions — the canonical
    /// file would hold `OLD-BYTES` and the backup would be gone.
    #[test]
    fn revert_keeps_freshly_pulled_bytes_and_parks_the_backup() {
        let dir = tmpdir("clobber");
        let canonical = dir.join("vct-launcher");
        let backup = dir.join("vct-launcher.old-999999");
        std::fs::write(&canonical, b"NEW-BYTES-FROM-THE-PULL").unwrap();
        std::fs::write(&backup, b"OLD-BYTES").unwrap();

        let outcome = revert_pre_pull_rename(&backup);

        assert_eq!(outcome, RevertOutcome::ClobberAverted);
        assert_eq!(
            std::fs::read(&canonical).unwrap(),
            b"NEW-BYTES-FROM-THE-PULL",
            "the freshly-pulled binary must survive the abort tail"
        );
        assert!(
            backup.is_file(),
            "the backup stays parked for the boot sweep"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Both-sides: the ordinary aborted-pull case still restores.
    #[test]
    fn revert_restores_when_canonical_is_absent() {
        let dir = tmpdir("absent");
        let canonical = dir.join("vct-launcher");
        let backup = dir.join("vct-launcher.old-999998");
        std::fs::write(&backup, b"OLD-BYTES").unwrap();

        let outcome = revert_pre_pull_rename(&backup);

        assert_eq!(outcome, RevertOutcome::Reverted);
        assert_eq!(std::fs::read(&canonical).unwrap(), b"OLD-BYTES");
        assert!(!backup.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Same-length-but-different content must be detected by the hash, not
    /// only by the size fast-path.
    #[test]
    fn same_size_different_content_is_detected_as_differing() {
        let dir = tmpdir("samesize");
        let canonical = dir.join("vct-launcher");
        let backup = dir.join("vct-launcher.old-999997");
        std::fs::write(&canonical, b"AAAAAAAA").unwrap();
        std::fs::write(&backup, b"BBBBBBBB").unwrap();

        assert_eq!(
            compare_backup_to_canonical(&backup, &canonical),
            CanonicalState::Differs
        );
        assert_eq!(revert_pre_pull_rename(&backup), RevertOutcome::ClobberAverted);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Identical content ⇒ the canonical NAME is restored (and the duplicate
    /// backup disappears).
    #[test]
    fn identical_bytes_revert_and_consume_the_backup() {
        let dir = tmpdir("identical");
        let canonical = dir.join("vct-launcher");
        let backup = dir.join("vct-launcher.old-999996");
        std::fs::write(&canonical, b"SAME").unwrap();
        std::fs::write(&backup, b"SAME").unwrap();

        assert_eq!(revert_pre_pull_rename(&backup), RevertOutcome::Reverted);
        assert!(!backup.exists());
        assert_eq!(std::fs::read(&canonical).unwrap(), b"SAME");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn revert_refuses_paths_that_are_not_backups() {
        let dir = tmpdir("foreign");
        let f = dir.join("vct-launcher");
        std::fs::write(&f, b"X").unwrap();
        assert_eq!(revert_pre_pull_rename(&f), RevertOutcome::NotABackupPath);
        assert!(f.is_file(), "a non-backup path must never be moved");
        let _ = std::fs::remove_dir_all(&dir);
    }

    // -- WI-1: the pure freshness decision ---------------------------------

    fn inputs(running: &str, on_disk: Option<&str>, dirty: bool) -> FreshnessInputs {
        FreshnessInputs {
            running_version: running.to_string(),
            on_disk_version: on_disk.map(|s| s.to_string()),
            dist_dirty: dirty,
        }
    }

    /// RED-PROOF (WI-1): nothing on `bd8f6836` computes this verdict anywhere
    /// outside the tail of a successful pull. This is WFT's exact state.
    #[test]
    fn on_disk_newer_and_dirty_is_stale_for_both_reasons() {
        assert_eq!(
            decide_binary_freshness(&inputs("0.2.88", Some("0.2.90"), true)),
            FreshnessVerdict::Stale(StaleReason::Both)
        );
    }

    #[test]
    fn on_disk_newer_alone_is_stale() {
        assert_eq!(
            decide_binary_freshness(&inputs("0.2.88", Some("0.2.90"), false)),
            FreshnessVerdict::Stale(StaleReason::OnDiskNewerThanRunning)
        );
    }

    #[test]
    fn dirty_dist_alone_is_stale() {
        assert_eq!(
            decide_binary_freshness(&inputs("0.2.90", Some("0.2.90"), true)),
            FreshnessVerdict::Stale(StaleReason::DistDirtyVsHead)
        );
    }

    /// Leave-alone leg: the steady state must be silent.
    #[test]
    fn matching_versions_and_clean_dist_are_fresh() {
        assert_eq!(
            decide_binary_freshness(&inputs("0.2.91", Some("0.2.91"), false)),
            FreshnessVerdict::Fresh
        );
    }

    /// A local dev build running AHEAD of the dist slot must not be nagged.
    #[test]
    fn running_newer_than_dist_is_fresh() {
        assert_eq!(
            decide_binary_freshness(&inputs("0.2.91", Some("0.2.88"), false)),
            FreshnessVerdict::Fresh
        );
    }

    /// Missing / unparseable sidecar contributes nothing — never infer
    /// staleness from absent metadata.
    #[test]
    fn absent_sidecar_never_makes_a_clean_tree_stale() {
        assert_eq!(
            decide_binary_freshness(&inputs("0.2.91", None, false)),
            FreshnessVerdict::Fresh
        );
        assert_eq!(
            decide_binary_freshness(&inputs("0.2.91", Some(""), false)),
            FreshnessVerdict::Fresh
        );
    }

    // -- staging paths ------------------------------------------------------

    #[test]
    fn new_suffix_matches_the_updater_reader_convention() {
        assert_eq!(
            path_with_new_suffix(Path::new("/d/vct-launcher.exe")),
            PathBuf::from("/d/vct-launcher.exe.new")
        );
        assert_eq!(
            path_with_new_suffix(Path::new("/d/vct-launcher")),
            PathBuf::from("/d/vct-launcher.new")
        );
    }

    #[test]
    fn swap_candidates_cover_launcher_and_hub_under_this_hosts_dist_slot() {
        let c = swap_candidate_rel_paths();
        assert_eq!(c.len(), 2, "launcher + hub (never the updater itself)");
        let prefix = format!("launcher/dist/{}/", launcher_dist_subdir());
        assert!(c.iter().all(|p| p.starts_with(&prefix)), "got {:?}", c);
        assert!(c[0].contains("vct-launcher"), "got {:?}", c);
        assert!(c[1].contains("vct-hub"), "got {:?}", c);
        assert!(
            !c.iter().any(|p| p.contains("vct-updater")),
            "the updater performs the swap; it must never be a swap target"
        );
        assert_eq!(dist_dir_rel_path(), prefix);
    }

    // -- staging MECHANISM over a real git repo (runs on every OS) ---------

    fn git(repo: &Path, args: &[&str]) {
        let ok = StdCommand::new("git")
            .args(args)
            .current_dir(repo)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        assert!(ok, "git {:?} failed in {}", args, repo.display());
    }

    fn have_git() -> bool {
        StdCommand::new("git")
            .arg("--version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }

    /// Build a repo with a committed `dist/<slot>/bin` blob, then dirty it.
    fn repo_with_dirty_binary(label: &str, rel: &str) -> PathBuf {
        let repo = tmpdir(label);
        git(&repo, &["init", "-q"]);
        git(&repo, &["config", "user.email", "t@t"]);
        git(&repo, &["config", "user.name", "t"]);
        let abs = repo.join(rel);
        std::fs::create_dir_all(abs.parent().unwrap()).unwrap();
        std::fs::write(&abs, b"HEAD-BINARY-BYTES").unwrap();
        git(&repo, &["add", "-A"]);
        git(&repo, &["commit", "-q", "-m", "seed"]);
        // Dirty it exactly the way a hand-copied stale exe does.
        std::fs::write(&abs, b"STALE-HAND-COPIED-BYTES").unwrap();
        repo
    }

    /// RED-PROOF (WI-2, mechanism half): a DIRTY dist binary must produce a
    /// `<target>.new` carrying HEAD's bytes. `bd8f6836` only ever reaches this
    /// staging code from inside `finalize_update_and_restart`, i.e. never on
    /// the "Already up to date" branch nor at rest — which is why the field
    /// install could not heal. Exercised on every host because the staging
    /// mechanism is deliberately OS-agnostic.
    #[tokio::test]
    async fn staging_extracts_head_bytes_for_a_dirty_binary() {
        if !have_git() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let rel = "launcher/dist/test-slot/vct-launcher";
        let repo = repo_with_dirty_binary("stage-dirty", rel);

        let staged = stage_dirty_binaries(&repo, &[rel.to_string()]).await;

        assert_eq!(staged, vec![rel.to_string()]);
        let new_sibling = repo.join(format!("{}.new", rel));
        assert_eq!(
            std::fs::read(&new_sibling).unwrap(),
            b"HEAD-BINARY-BYTES",
            "the staged sibling must carry HEAD's blob, not the stale on-disk bytes"
        );
        // The canonical path is untouched until the updater renames.
        assert_eq!(
            std::fs::read(repo.join(rel)).unwrap(),
            b"STALE-HAND-COPIED-BYTES"
        );
        let _ = std::fs::remove_dir_all(&repo);
    }

    /// Leave-alone leg: a CLEAN binary must not be staged (staging writes a
    /// file the updater will later rename over a working binary — a clean tree
    /// must never trigger that).
    #[tokio::test]
    async fn staging_skips_a_clean_binary() {
        if !have_git() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let rel = "launcher/dist/test-slot/vct-launcher";
        let repo = repo_with_dirty_binary("stage-clean", rel);
        // Undo the dirtying.
        git(&repo, &["checkout", "--", rel]);

        let staged = stage_dirty_binaries(&repo, &[rel.to_string()]).await;

        assert!(staged.is_empty(), "clean tree must stage nothing");
        assert!(
            !repo.join(format!("{}.new", rel)).exists(),
            "no `.new` sibling may be created for a clean binary"
        );
        let _ = std::fs::remove_dir_all(&repo);
    }

    /// The dirty-probe must scope to the dist dir: an unrelated dirty file
    /// elsewhere in the repo must not be read as a stale binary.
    #[tokio::test]
    async fn dirty_probe_is_scoped_to_the_dist_directory() {
        if !have_git() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let repo = tmpdir("scope");
        git(&repo, &["init", "-q"]);
        git(&repo, &["config", "user.email", "t@t"]);
        git(&repo, &["config", "user.name", "t"]);
        let dist = repo.join(dist_dir_rel_path());
        std::fs::create_dir_all(&dist).unwrap();
        std::fs::write(dist.join("vct-launcher-marker"), b"a").unwrap();
        std::fs::write(repo.join("README.md"), b"a").unwrap();
        git(&repo, &["add", "-A"]);
        git(&repo, &["commit", "-q", "-m", "seed"]);

        assert!(!dist_is_dirty(&repo).await, "seeded tree is clean");

        std::fs::write(repo.join("README.md"), b"edited").unwrap();
        assert!(
            !dist_is_dirty(&repo).await,
            "a dirty file OUTSIDE launcher/dist/<arch>/ must not read as a stale binary"
        );

        std::fs::write(dist.join("vct-launcher-marker"), b"edited").unwrap();
        assert!(dist_is_dirty(&repo).await, "a dirty dist file must be seen");
        let _ = std::fs::remove_dir_all(&repo);
    }

    /// The at-rest reconcile must be a no-op on a healthy tree: no staging, no
    /// arming, no `.new` siblings. (Destructive-gate leave-alone leg for the
    /// WI-1 entry point.)
    ///
    /// Drives the gate-injectable core with `update_owns_tree = false` so the
    /// verdict comes from the tree under test and not from whatever happens to
    /// sit in this machine's real `~/.vct/`.
    #[tokio::test]
    async fn at_rest_reconcile_is_silent_on_a_clean_tree() {
        if !have_git() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let repo = tmpdir("atrest-clean");
        git(&repo, &["init", "-q"]);
        git(&repo, &["config", "user.email", "t@t"]);
        git(&repo, &["config", "user.name", "t"]);
        std::fs::write(repo.join("README.md"), b"a").unwrap();
        git(&repo, &["add", "-A"]);
        git(&repo, &["commit", "-q", "-m", "seed"]);

        // No dist sidecar at all → on_disk_version is None → never stale.
        let outcome = reconcile_dist_at_rest_gated(&repo, false).await;

        assert!(!outcome.is_stale(), "clean tree with no sidecar is Fresh");
        assert!(outcome.staged.is_empty());
        assert!(!outcome.armed);
        assert!(!outcome.stood_down);
        let _ = std::fs::remove_dir_all(&repo);
    }

    // ======================================================================
    // v0.2.91 FIX ROUND
    // ======================================================================

    // -- MAJOR-1: untracked files are not divergence -----------------------

    /// RED-PROOF (MAJOR-1): the wave-1 probe ran `git status --porcelain`
    /// WITHOUT `--untracked-files=no`, so any `??` row under the dist dir made
    /// a healthy install `Stale(DistDirtyVsHead)` forever. On the dogfood
    /// machine that was 9 stray `vct-launcher.old-may7` / `*.bak` files — and
    /// the deferral's own remediation (`git checkout -- launcher/dist/`) cannot
    /// delete an untracked file, so the warning could not be cleared by
    /// following its instructions. Worse, this module's OWN `.old-<pid>`
    /// backups and `.new` staging files re-trigger it.
    ///
    /// Against the pre-fix body this test fails on its first assertion.
    #[tokio::test]
    async fn untracked_files_under_dist_are_not_divergence() {
        if !have_git() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let repo = tmpdir("untracked");
        git(&repo, &["init", "-q"]);
        git(&repo, &["config", "user.email", "t@t"]);
        git(&repo, &["config", "user.name", "t"]);
        let dist = repo.join(dist_dir_rel_path());
        std::fs::create_dir_all(&dist).unwrap();
        std::fs::write(dist.join("vct-launcher-marker"), b"a").unwrap();
        git(&repo, &["add", "-A"]);
        git(&repo, &["commit", "-q", "-m", "seed"]);

        // Exactly the field shapes: a hand-kept old copy, a `.bak`, one of our
        // own parked backups, and a staged sibling.
        for name in [
            "vct-launcher.old-may7",
            "vct-hub.v0.2.22.bak",
            "vct-launcher-marker.old-4242",
            "vct-launcher-marker.new",
        ] {
            std::fs::write(dist.join(name), b"stray").unwrap();
        }

        assert!(
            !dist_is_dirty(&repo).await,
            "untracked debris under launcher/dist/ is NOT divergence — counting \
             it produced a permanent, un-clearable `launcher_binary_stale` warning"
        );

        // Both-sides: TRACKED divergence must still be seen (the signal this
        // probe exists for).
        std::fs::write(dist.join("vct-launcher-marker"), b"edited").unwrap();
        assert!(
            dist_is_dirty(&repo).await,
            "a tracked, modified dist file must still read as dirty"
        );
        let _ = std::fs::remove_dir_all(&repo);
    }

    // -- MAJOR-2(a): stale `.new` invalidation ------------------------------

    #[test]
    fn stale_new_invalidation_acts_only_on_a_confirmed_clean_candidate() {
        let dir = tmpdir("invalidate-decide");
        let rels = ["a/bin", "b/bin", "c/bin"];
        for rel in rels {
            let target = dir.join(rel);
            std::fs::create_dir_all(target.parent().unwrap()).unwrap();
            std::fs::write(&target, b"canonical").unwrap();
            std::fs::write(path_with_new_suffix(&target), b"staged").unwrap();
        }

        let dropped = invalidate_stale_new_siblings(
            &dir,
            &[
                ("a/bin".to_string(), Some(true)),  // clean  → stale `.new`
                ("b/bin".to_string(), Some(false)), // dirty  → the `.new` IS the fix
                ("c/bin".to_string(), None),        // unknown → never act
            ],
        );

        assert_eq!(dropped, vec!["a/bin".to_string()]);
        assert!(!dir.join("a/bin.new").exists(), "stale staged copy must go");
        assert!(
            dir.join("b/bin.new").is_file(),
            "a DIRTY candidate's staged copy is the pending repair — never drop it"
        );
        assert!(
            dir.join("c/bin.new").is_file(),
            "unknown cleanliness must be a leave-alone (conservative default)"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// RED-PROOF (MAJOR-2(a)): the end-to-end sequence, over a real repo.
    ///
    /// WI-1 made `.new` files long-lived. Boot stages one from HEAD-at-boot;
    /// later in the SAME session an update pulls newer binaries (canonical now
    /// clean vs the new HEAD); the post-update handoff then swaps ANY existing
    /// `.new`, so `vct-updater` renames the boot-time OLD bytes over the
    /// freshly-pulled binary. Pre-fix nothing deletes that leftover and this
    /// test fails: the `.new` survives and would be handed to the updater.
    #[tokio::test]
    async fn a_stale_new_for_a_clean_candidate_is_dropped_before_the_handoff() {
        if !have_git() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let rel = "launcher/dist/test-slot/vct-launcher";
        let repo = repo_with_dirty_binary("invalidate-e2e", rel);
        // The update landed: canonical now matches HEAD again.
        git(&repo, &["checkout", "HEAD", "--", rel]);
        // …but a `.new` staged earlier in the session is still lying around.
        let staged = repo.join(format!("{}.new", rel));
        std::fs::write(&staged, b"OLD-BOOT-TIME-BYTES").unwrap();

        let dropped = invalidate_stale_new_siblings_for(&repo, &[rel.to_string()]).await;

        assert_eq!(dropped, vec![rel.to_string()]);
        assert!(
            !staged.exists(),
            "a `.new` whose canonical already matches HEAD is stale by definition; \
             handing it to vct-updater renames OLDER bytes over the current binary"
        );
        assert_eq!(
            std::fs::read(repo.join(rel)).unwrap(),
            b"HEAD-BINARY-BYTES",
            "the canonical binary itself is never touched by the invalidation"
        );
        let _ = std::fs::remove_dir_all(&repo);
    }

    /// Both-sides leave-alone: while the canonical binary is still DIRTY the
    /// staged sibling is the pending repair and must survive.
    #[tokio::test]
    async fn a_fresh_new_for_a_dirty_candidate_survives_invalidation() {
        if !have_git() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let rel = "launcher/dist/test-slot/vct-launcher";
        let repo = repo_with_dirty_binary("invalidate-keep", rel);
        let staged = stage_dirty_binaries(&repo, &[rel.to_string()]).await;
        assert_eq!(staged, vec![rel.to_string()], "precondition: staged");

        let dropped = invalidate_stale_new_siblings_for(&repo, &[rel.to_string()]).await;

        assert!(dropped.is_empty(), "nothing may be dropped: {:?}", dropped);
        assert_eq!(
            std::fs::read(repo.join(format!("{}.new", rel))).unwrap(),
            b"HEAD-BINARY-BYTES",
            "the pending repair must still be there for the updater"
        );
        let _ = std::fs::remove_dir_all(&repo);
    }

    /// A git probe that cannot answer (not a repo at all) must leave every
    /// staged sibling alone — the conservative-default rule for a best-effort
    /// destructive path.
    #[tokio::test]
    async fn invalidation_leaves_everything_alone_outside_a_git_repo() {
        let dir = tmpdir("invalidate-nogit");
        let rel = "launcher/dist/test-slot/vct-launcher";
        let target = dir.join(rel);
        std::fs::create_dir_all(target.parent().unwrap()).unwrap();
        std::fs::write(&target, b"canonical").unwrap();
        std::fs::write(path_with_new_suffix(&target), b"staged").unwrap();

        let dropped = invalidate_stale_new_siblings_for(&dir, &[rel.to_string()]).await;

        assert!(dropped.is_empty());
        assert!(path_with_new_suffix(&target).is_file());
        let _ = std::fs::remove_dir_all(&dir);
    }

    // -- MAJOR-2(b): stand down while an update owns the tree ---------------

    #[test]
    fn update_ownership_is_read_from_both_locks() {
        let dir = tmpdir("owns-tree");
        let gate = dir.join(".update-in-progress.json");
        let handoff = dir.join("update.lock.json");

        // Neither lock → at rest.
        assert!(!update_owns_the_tree_at(&gate, &handoff));

        // The stage1 handoff lock alone is enough (its swap list owns the
        // binaries and its relaunch semantics must win).
        std::fs::write(&handoff, b"{}").unwrap();
        assert!(update_owns_the_tree_at(&gate, &handoff));
        std::fs::remove_file(&handoff).unwrap();

        // The update-gate lockfile, armed by ANOTHER process, deadline in the
        // FUTURE: a terminal `install.py --update` is rewriting the same files.
        let deadline = (chrono::Utc::now() + chrono::Duration::minutes(15))
            .format("%Y-%m-%dT%H:%M:%SZ")
            .to_string();
        let write_gate = |pid: u32| {
            std::fs::write(
                &gate,
                serde_json::json!({
                    "started_at": "2026-08-26T00:00:00Z",
                    "started_by_pid": pid,
                    "phase": "git_pull",
                    "expected_completion_by": deadline,
                })
                .to_string(),
            )
            .unwrap();
        };
        write_gate(std::process::id().wrapping_add(1));
        assert!(update_owns_the_tree_at(&gate, &handoff));

        // …but OUR OWN update is not a stand-down: WI-2's "Already up to date
        // still heals" reconcile runs from inside `update_orchestrator`, i.e.
        // inside the window where THIS process holds the gate. Standing down
        // there would restore the RC-2 dead end.
        write_gate(std::process::id());
        assert!(
            !update_owns_the_tree_at(&gate, &handoff),
            "our own in-flight update must not stand the WI-2 reconcile down"
        );

        // A lockfile whose deadline has PASSED is stale — `update_gate`'s own
        // self-healing rule — and must not wedge the reconcile forever.
        std::fs::write(
            &gate,
            serde_json::json!({
                "started_at": "2026-08-26T00:00:00Z",
                "started_by_pid": 4242,
                "phase": "git_pull",
                "expected_completion_by": "2020-01-01T00:00:00Z",
            })
            .to_string(),
        )
        .unwrap();
        assert!(
            !update_owns_the_tree_at(&gate, &handoff),
            "an expired update lock must not stand the reconcile down forever"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// RED-PROOF (MAJOR-2(b)): with an update in flight the pass must not probe,
    /// stage, arm or record. Pre-fix there was no gate at all — the reconcile
    /// probed and staged straight into the directory the running update was
    /// about to fill, and the update's own handoff would then swap the older
    /// staged copy in.
    #[tokio::test]
    async fn at_rest_reconcile_stands_down_while_an_update_owns_the_tree() {
        if !have_git() {
            eprintln!("skipping: git not on PATH");
            return;
        }
        let rel = "launcher/dist/test-slot/vct-launcher";
        let repo = repo_with_dirty_binary("atrest-standdown", rel);

        let outcome = reconcile_dist_at_rest_gated(&repo, true).await;

        assert!(outcome.stood_down, "the pass must report that it did nothing");
        assert_eq!(
            outcome.verdict,
            FreshnessVerdict::NotProbed,
            "we did not look, so we must not claim Fresh"
        );
        assert!(!outcome.is_stale());
        assert!(outcome.staged.is_empty());
        assert!(!outcome.armed);
        assert!(
            !repo.join(format!("{}.new", rel)).exists(),
            "standing down means staging nothing"
        );
        let _ = std::fs::remove_dir_all(&repo);
    }

    // -- MINOR-3: dirty-alone is surface-only ------------------------------

    /// RED-PROOF (MINOR-3): pre-fix EVERY stale verdict staged + armed, so a
    /// developer whose `cargo build` left the dist slot dirty at an EQUAL
    /// version had HEAD's blob staged and a swap armed — destroying their build
    /// at the next quit, with no update running and nobody having asked.
    #[test]
    fn dirty_dist_at_an_equal_version_is_surfaced_but_never_armed() {
        assert_eq!(
            decide_at_rest_action(&FreshnessVerdict::Stale(StaleReason::DistDirtyVsHead)),
            AtRestAction::SurfaceOnly,
        );
    }

    /// Both-sides ACT leg: a positive "the binary on disk is NEWER than me"
    /// signal is exactly what the arming exists for.
    #[test]
    fn a_newer_on_disk_version_still_stages_and_arms() {
        assert_eq!(
            decide_at_rest_action(&FreshnessVerdict::Stale(
                StaleReason::OnDiskNewerThanRunning
            )),
            AtRestAction::StageAndArm,
        );
        assert_eq!(
            decide_at_rest_action(&FreshnessVerdict::Stale(StaleReason::Both)),
            AtRestAction::StageAndArm,
            "WFT's field state must still heal"
        );
    }

    #[test]
    fn a_fresh_or_unprobed_verdict_owes_nothing() {
        assert_eq!(
            decide_at_rest_action(&FreshnessVerdict::Fresh),
            AtRestAction::Nothing
        );
        assert_eq!(
            decide_at_rest_action(&FreshnessVerdict::NotProbed),
            AtRestAction::Nothing
        );
    }

    // -- MINOR-1: the revert outcome must be CHECKED -----------------------

    /// RED-PROOF (MINOR-1): the self-update surface's abort tail discarded the
    /// `RevertOutcome` (`let _ = revert_pre_pull_rename(b);`), so an averted
    /// clobber there produced no deferral, no audit row and no trace — while
    /// the installer surface recorded both. This pins that the outcome drives
    /// the record.
    #[test]
    fn an_averted_clobber_is_reported_to_the_caller_for_recording() {
        let dir = tmpdir("revert-record");
        let canonical = dir.join("vct-launcher");
        let backup = dir.join("vct-launcher.old-999995");
        std::fs::write(&canonical, b"NEW-BYTES-FROM-THE-PULL").unwrap();
        std::fs::write(&backup, b"OLD-BYTES").unwrap();

        let mut recorded: Vec<(PathBuf, PathBuf)> = Vec::new();
        let outcome = revert_and_record_with(&backup, &mut |b, c| {
            recorded.push((b.to_path_buf(), c.to_path_buf()))
        });

        assert_eq!(outcome, RevertOutcome::ClobberAverted);
        assert_eq!(
            recorded,
            vec![(backup.clone(), canonical.clone())],
            "the averted clobber must be recorded exactly once, naming both paths"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Both-sides leave-alone: an ORDINARY revert is not an incident and must
    /// not write a record (a false `clobber_averted` row would train the user
    /// to ignore the real one).
    #[test]
    fn an_ordinary_revert_records_nothing() {
        let dir = tmpdir("revert-record-quiet");
        let backup = dir.join("vct-launcher.old-999994");
        std::fs::write(&backup, b"OLD-BYTES").unwrap();

        let mut recorded = 0usize;
        let outcome = revert_and_record_with(&backup, &mut |_b, _c| recorded += 1);

        assert_eq!(outcome, RevertOutcome::Reverted);
        assert_eq!(recorded, 0);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
